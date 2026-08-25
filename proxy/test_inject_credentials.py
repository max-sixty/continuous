"""Tests for the credential-injection addon (GitHub + Anthropic).

Run: ``uv run pytest`` from the repo root, which covers every Python suite.
"""

from __future__ import annotations

import base64
import re
from importlib.metadata import version
from pathlib import Path

import pytest
from mitmproxy.test import tflow, tutils
from ruamel.yaml import YAML

from inject_credentials import (
    ANTHROPIC_HOSTS,
    BASIC_HOSTS,
    TOKEN_HOSTS,
    CredentialInjector,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pinned_mitmproxy_matches_the_action() -> None:
    # The addon imports mitmproxy.test, an internal helper, so these tests only
    # mean anything run against the version production runs. That version is
    # named twice — the workspace dev group installs it, claude/action.yaml
    # tells an adopter's job which to fetch — and drift between them greens
    # this suite while every adopter's proxy breaks. Assert on the installed
    # distribution rather than the pyproject text, so a lock that resolved to
    # something else fails here too.
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )
    assert version("mitmproxy") == action["inputs"]["mitmproxy_version"]["default"]


def _allow_hosts_regex() -> re.Pattern[str]:
    setup = (REPO_ROOT / "proxy" / "setup-sandbox.sh").read_text()
    # `[^']*` spans newlines, so a stray example in a comment would silently
    # win the first match — require exactly one occurrence.
    found = re.findall(r"--allow-hosts '([^']*)'", setup)
    assert len(found) == 1, "expected one --allow-hosts flag in proxy/setup-sandbox.sh"
    # mitmproxy compiles --allow-hosts with re.IGNORECASE; model that here.
    return re.compile(found[0], re.IGNORECASE)


def test_allow_hosts_regex_covers_every_injected_host() -> None:
    # The frozensets scope injection; the --allow-hosts regex in setup-sandbox.sh
    # scopes TLS interception. A host in a frozenset the regex misses is never
    # intercepted, so its dummy is never swapped for the real secret and every
    # call to it 401s — with the proxy alive, the CA trusted and both files
    # looking correct in isolation. Both sides carry a "keep in sync" comment;
    # only this asserts it. mitmproxy appends the port to every candidate it
    # matches (peer address, Host header, SNI), so `host:port` is the form
    # interception actually turns on — the bare host is extra strictness, not
    # what production feeds the regex.
    allow_hosts = _allow_hosts_regex()
    for host in BASIC_HOSTS | TOKEN_HOSTS | ANTHROPIC_HOSTS:
        assert allow_hosts.search(host), f"{host} is injected but never intercepted"
        assert allow_hosts.search(f"{host}:443"), f"{host}:443 is never intercepted"


@pytest.mark.parametrize(
    "host",
    [
        # Signed, time-limited URLs; injecting there breaks the download, so the
        # addon leaves it out of TOKEN_HOSTS and the regex must not widen
        # interception to it either.
        "objects.githubusercontent.com",
        # The lookalikes the injection tests below already refuse a credential
        # for. Interception is the outer boundary and should refuse them first.
        "api.github.com.evil.example",
        "raw.githubusercontent.com.evil.example",
        "api.anthropic.com.evil.example",
        "pypi.org",
    ],
)
def test_allow_hosts_regex_intercepts_nothing_extra(host: str) -> None:
    allow_hosts = _allow_hosts_regex()
    assert not allow_hosts.search(host), f"{host} should not be intercepted"
    assert not allow_hosts.search(f"{host}:443"), (
        f"{host}:443 should not be intercepted"
    )


def test_addon_methods_are_real_mitmproxy_hooks() -> None:
    # mitmproxy loads an addon whose method names don't match any event without
    # complaining, and simply never calls them — the proxy would come up and
    # inject nothing, failing every authenticated call. Nothing about the
    # process (alive, listening, CA written) shows that, so pin the names
    # against mitmproxy's own registry.
    import mitmproxy.proxy.layers.http  # noqa: F401  (registers the HTTP hooks)
    from mitmproxy import hooks

    known = {hook.name for hook in hooks.all_hooks.values()}
    declared = {name for name in vars(CredentialInjector) if not name.startswith("_")}
    assert declared, "the addon declares no hooks at all"
    assert declared <= known, f"not mitmproxy events: {sorted(declared - known)}"


def _flow(
    host: str, headers: dict[str, str] | None = None, scheme: str = "https"
) -> object:
    flow = tflow.tflow(req=tutils.treq(host=host))
    flow.request.scheme = scheme
    for name, value in (headers or {}).items():
        flow.request.headers[name] = value
    return flow


@pytest.fixture
def injector(monkeypatch: pytest.MonkeyPatch) -> CredentialInjector:
    """GitHub token + Anthropic OAuth (the default production shape)."""
    monkeypatch.setenv("TEND_GH_TOKEN", "ghp_REALTOKEN")
    monkeypatch.setenv("TEND_ANTHROPIC_OAUTH_TOKEN", "sk-ant-oat01-REAL")
    monkeypatch.delenv("TEND_ANTHROPIC_API_KEY", raising=False)
    return CredentialInjector()


@pytest.fixture
def api_key_injector(monkeypatch: pytest.MonkeyPatch) -> CredentialInjector:
    """GitHub token + Anthropic API key (the alternate auth mode)."""
    monkeypatch.setenv("TEND_GH_TOKEN", "ghp_REALTOKEN")
    monkeypatch.setenv("TEND_ANTHROPIC_API_KEY", "sk-ant-api03-REAL")
    monkeypatch.delenv("TEND_ANTHROPIC_OAUTH_TOKEN", raising=False)
    return CredentialInjector()


# --- GitHub --------------------------------------------------------------


def test_api_host_gets_token_scheme(injector: CredentialInjector) -> None:
    flow = _flow("api.github.com", {"Authorization": "token ghp_dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_REALTOKEN"


def test_git_host_gets_basic_scheme(injector: CredentialInjector) -> None:
    flow = _flow(
        "github.com",
        {"Authorization": "Basic " + base64.b64encode(b"x:dummy").decode()},
    )
    injector.request(flow)
    expected = "Basic " + base64.b64encode(b"x-access-token:ghp_REALTOKEN").decode()
    assert flow.request.headers["Authorization"] == expected


def test_missing_authorization_is_added_for_github(
    injector: CredentialInjector,
) -> None:
    # git's first request is unauthenticated; the proxy authenticates it so git
    # never needs a credential of its own.
    flow = _flow("github.com")
    injector.request(flow)
    assert flow.request.headers["Authorization"].startswith("Basic ")


def test_raw_content_host_gets_token_scheme(injector: CredentialInjector) -> None:
    # Private raw.githubusercontent.com content authenticates a PAT via the
    # ``token`` scheme, same as the API hosts.
    flow = _flow("raw.githubusercontent.com", {"Authorization": "token ghp_dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_REALTOKEN"


def test_raw_lookalike_host_is_untouched(injector: CredentialInjector) -> None:
    flow = _flow(
        "raw.githubusercontent.com.evil.example", {"Authorization": "token ghp_dummy"}
    )
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_object_store_host_is_untouched(injector: CredentialInjector) -> None:
    # objects.githubusercontent.com serves release assets / git-LFS objects from
    # signed URLs and batch-provided tokens; the PAT must NOT be injected or it
    # collides with the signature and breaks the download.
    flow = _flow("objects.githubusercontent.com", {"Authorization": "token ghp_dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_non_github_host_is_untouched(injector: CredentialInjector) -> None:
    flow = _flow("pypi.org", {"Authorization": "token ghp_dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_lookalike_host_is_untouched(injector: CredentialInjector) -> None:
    flow = _flow("api.github.com.evil.example", {"Authorization": "token ghp_dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_spoofed_host_header_does_not_leak_token(injector: CredentialInjector) -> None:
    # The real connection target is an attacker host, but the client spoofs the
    # Host header to a GitHub host. The token must NOT be injected — otherwise it
    # would be forwarded to the attacker. Guards against gating on pretty_host.
    flow = _flow(
        "attacker.example",
        {"Authorization": "token ghp_dummy", "Host": "api.github.com"},
    )
    assert flow.request.pretty_host == "api.github.com"  # the spoofable view
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_mixed_case_host_still_gets_credential(injector: CredentialInjector) -> None:
    # Clients send the hostname case-preserved and --allow-hosts matches
    # case-insensitively, so a mixed-case host is intercepted and must still
    # hit the (lowercase) injection allowlist.
    flow = _flow("Api.GitHub.Com")
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_REALTOKEN"


def test_plain_http_is_never_injected(injector: CredentialInjector) -> None:
    # A cleartext request to an allowlisted host must not carry the real
    # secret — it would transit unencrypted.
    flow = _flow("api.github.com", {"Authorization": "token ghp_dummy"}, scheme="http")
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


def test_uppercase_http_scheme_is_never_injected(
    injector: CredentialInjector,
) -> None:
    # The guard compares `scheme != "https"` case-sensitively, so it holds only
    # while mitmproxy normalizes the scheme it parses off the wire. mitmproxy
    # 12.2.2 started accepting absolute-form requests with an uppercase scheme
    # that earlier versions rejected outright, which routes a new shape here.
    flow = _flow("api.github.com", {"Authorization": "token ghp_dummy"}, scheme="HTTP")
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "token ghp_dummy"


# --- Anthropic -----------------------------------------------------------


def test_anthropic_host_gets_oauth_bearer(injector: CredentialInjector) -> None:
    flow = _flow("api.anthropic.com", {"Authorization": "Bearer sk-ant-oat01-dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer sk-ant-oat01-REAL"


def test_anthropic_oauth_removes_stray_api_key(injector: CredentialInjector) -> None:
    # If the agent crafted both headers, only the active (OAuth) scheme survives.
    flow = _flow(
        "api.anthropic.com",
        {
            "Authorization": "Bearer sk-ant-oat01-dummy",
            "x-api-key": "sk-ant-api03-dummy",
        },
    )
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer sk-ant-oat01-REAL"
    assert "x-api-key" not in flow.request.headers


def test_anthropic_host_gets_api_key(api_key_injector: CredentialInjector) -> None:
    flow = _flow("api.anthropic.com", {"x-api-key": "sk-ant-api03-dummy"})
    api_key_injector.request(flow)
    assert flow.request.headers["x-api-key"] == "sk-ant-api03-REAL"


def test_anthropic_api_key_removes_stray_authorization(
    api_key_injector: CredentialInjector,
) -> None:
    flow = _flow(
        "api.anthropic.com",
        {
            "x-api-key": "sk-ant-api03-dummy",
            "Authorization": "Bearer sk-ant-oat01-dummy",
        },
    )
    api_key_injector.request(flow)
    assert flow.request.headers["x-api-key"] == "sk-ant-api03-REAL"
    assert "Authorization" not in flow.request.headers


def test_anthropic_oauth_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEND_GH_TOKEN", "ghp_REALTOKEN")
    monkeypatch.setenv("TEND_ANTHROPIC_OAUTH_TOKEN", "sk-ant-oat01-REAL")
    monkeypatch.setenv("TEND_ANTHROPIC_API_KEY", "sk-ant-api03-REAL")
    injector = CredentialInjector()
    flow = _flow("api.anthropic.com", {"x-api-key": "sk-ant-api03-dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer sk-ant-oat01-REAL"
    assert "x-api-key" not in flow.request.headers


def test_anthropic_lookalike_host_is_untouched(injector: CredentialInjector) -> None:
    flow = _flow("api.anthropic.com.evil.example", {"x-api-key": "dummy"})
    injector.request(flow)
    assert flow.request.headers["x-api-key"] == "dummy"
    assert "Authorization" not in flow.request.headers


def test_anthropic_mixed_case_host_gets_credential(
    injector: CredentialInjector,
) -> None:
    flow = _flow("Api.Anthropic.Com", {"Authorization": "Bearer sk-ant-oat01-dummy"})
    injector.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer sk-ant-oat01-REAL"


def test_responses_are_streamed(injector: CredentialInjector) -> None:
    # SSE inference responses must not be buffered (mitmproxy#4469) — the
    # addon streams every intercepted response through unmodified.
    flow = _flow("api.anthropic.com")
    flow.response = tutils.tresp()
    injector.responseheaders(flow)
    assert flow.response.stream is True


# --- Startup guards ------------------------------------------------------


def test_no_github_token_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEND_GH_TOKEN", raising=False)
    monkeypatch.setenv("TEND_ANTHROPIC_OAUTH_TOKEN", "sk-ant-oat01-REAL")
    with pytest.raises(RuntimeError, match="TEND_GH_TOKEN is unset"):
        CredentialInjector()


def test_no_anthropic_credential_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEND_GH_TOKEN", "ghp_REALTOKEN")
    monkeypatch.delenv("TEND_ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("TEND_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Anthropic credential"):
        CredentialInjector()
