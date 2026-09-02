"""mitmproxy addon: inject the bot's real credentials into the requests that
need them, so the sandboxed agent never holds the secrets.

Two credentials, one mechanism. This process runs as the privileged runner
user, OUTSIDE the agent's sandbox. The real secrets are read once at startup
and live only in this process's memory. The agent runs as a separate, non-sudo
UID and therefore cannot read this process's environment
(``/proc/<pid>/environ`` is owner-only) or escalate to it. The agent holds only
throwaway dummies; every request it makes to an allowlisted host has its
credential header replaced here with the real one.

- **GitHub** (``TEND_GH_TOKEN``): the git smart-HTTP hosts authenticate with
  Basic (token as the password); the REST, upload, and raw-content hosts take
  the ``token`` scheme.
- **Anthropic** (``TEND_ANTHROPIC_OAUTH_TOKEN`` or ``TEND_ANTHROPIC_API_KEY``):
  ``api.anthropic.com`` only. The agent's ``claude`` binary runs in the SAME
  auth mode with a dummy secret, so it already emits every mode-specific header
  (``anthropic-beta``, ``anthropic-version``, ``x-api-key`` vs
  ``Authorization``); this addon only swaps the dummy secret value for the real
  one. OAuth wins when both are set, matching the action's input precedence.

The Codex harness sets ``TEND_GITHUB_ONLY=1`` because OpenAI's purpose-built
Responses API proxy owns model authentication there.

Security boundary: the injection allowlist below is exact-match on
``flow.request.host`` — the real connection target, NOT ``pretty_host``, which
mitmproxy derives from the spoofable client-supplied Host header. A request to
any other host (including ``api.github.com.evil.com`` or a host intercepted by
misconfiguration) is passed through untouched. The proxy's ``--allow-hosts``
flag is a functionality optimization (don't TLS-intercept non-allowlisted
traffic, so package managers keep working); this allowlist is the credential
boundary.
"""

from __future__ import annotations

import base64
import logging
import os

from mitmproxy import http

# GitHub hosts grouped by the header injected, not by role. git's smart-HTTP
# transport authenticates with Basic (token as the password); the REST, upload,
# and raw-content hosts take the ``token`` scheme. Bare hostnames
# (``flow.request.host`` carries no port).
#
# raw.githubusercontent.com serves private raw file content and authenticates a
# PAT via ``Authorization: token``, so it joins the token group. Deliberately
# NOT covered: objects.githubusercontent.com (release assets, git-LFS objects),
# which download from signed, time-limited URLs — git-LFS gets a short-lived
# object credential from the batch API on github.com (already covered) — so
# injecting the PAT there would collide with the signature and break the
# download; that host stays an untouched tunnel.
#
# These frozensets are the credential boundary. The proxy's --allow-hosts regex
# in setup-sandbox.sh scopes TLS interception and must list the same hosts —
# keep the two in sync (a host here but not in the regex is never intercepted,
# so its dummy is never swapped and auth 401s). The
# `test_allow_hosts_regex_*` tests in test_inject_credentials.py read that
# regex out of the script and fail on drift in either direction.
BASIC_HOSTS = frozenset({"github.com", "codeload.github.com"})
TOKEN_HOSTS = frozenset(
    {"api.github.com", "uploads.github.com", "raw.githubusercontent.com"}
)
ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})


class CredentialInjector:
    def __init__(self) -> None:
        gh_token = os.environ.get("TEND_GH_TOKEN", "")
        if not gh_token:
            raise RuntimeError(
                "TEND_GH_TOKEN is unset — refusing to start the credential "
                "proxy with no GitHub token to inject"
            )
        self._gh_token = gh_token
        # Basic base64("x-access-token:<token>") — GitHub's git endpoint accepts
        # the token as the password with this fixed username.
        self._gh_basic = (
            "Basic " + base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()
        )

        # Exactly one Anthropic scheme is active, matching the action's
        # precedence (OAuth wins when both are present).
        self._anthropic_oauth = os.environ.get("TEND_ANTHROPIC_OAUTH_TOKEN", "")
        self._anthropic_api_key = os.environ.get("TEND_ANTHROPIC_API_KEY", "")
        github_only = os.environ.get("TEND_GITHUB_ONLY") == "1"
        if not github_only and not (self._anthropic_oauth or self._anthropic_api_key):
            raise RuntimeError(
                "Neither TEND_ANTHROPIC_OAUTH_TOKEN nor TEND_ANTHROPIC_API_KEY "
                "is set — refusing to start the credential proxy with no "
                "Anthropic credential to inject"
            )

    def request(self, flow: http.HTTPFlow) -> None:
        # Never attach a credential to a cleartext request — a plain
        # http:// URL to an allowlisted host would otherwise carry the real
        # secret unencrypted (GitHub only redirects to https AFTER the header
        # has transited).
        if flow.request.scheme != "https":
            return
        # Gate on `host` — the real connection target — NOT `pretty_host`,
        # which mitmproxy derives from the client-supplied Host header. A
        # sandboxed agent could otherwise send `Host: api.github.com` to an
        # attacker host and have the real token injected and forwarded there.
        # Lowercase it: clients send the hostname case-preserved on the wire,
        # and mitmproxy's --allow-hosts matches case-insensitively, so a
        # mixed-case `Api.GitHub.Com` is intercepted and must still match.
        host = flow.request.host.lower()
        headers = flow.request.headers
        if host in TOKEN_HOSTS:
            headers["Authorization"] = f"token {self._gh_token}"
        elif host in BASIC_HOSTS:
            headers["Authorization"] = self._gh_basic
        elif host in ANTHROPIC_HOSTS and (
            self._anthropic_oauth or self._anthropic_api_key
        ):
            # Normalize to exactly the active scheme so the injected credential
            # is the one Anthropic honors, even if the agent crafted both.
            if self._anthropic_oauth:
                headers["Authorization"] = f"Bearer {self._anthropic_oauth}"
                if "x-api-key" in headers:
                    del headers["x-api-key"]
            else:
                headers["x-api-key"] = self._anthropic_api_key
                if "Authorization" in headers:
                    del headers["Authorization"]
        else:
            # Not an allowlisted host — never attach a credential. Leave the
            # request exactly as the agent sent it.
            return
        # mitmproxy's documented addon pattern logs through the root logger, which
        # its own handler captures; a named logger would change where this lands.
        logging.info(  # noqa: LOG015
            "injected credential for %s %s", flow.request.method, host
        )

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        # Stream every intercepted response straight through — intentionally
        # for ALL allowlisted hosts, not just Anthropic. Without this, mitmproxy
        # buffers the entire body in memory before relaying a single byte, which
        # breaks two cases: Anthropic's SSE inference responses arrive as one
        # blob only after the full generation (long turns hit the client's
        # first-byte timeout, mitmproxy#4469), and a large git packfile from a
        # GitHub clone/fetch is held whole in proxy memory before relaying.
        # We never inspect response bodies, so streaming is universally safe and
        # cheaper. Do NOT reach for `--set stream_large_bodies` instead: that
        # also streams large REQUEST bodies, whose headers are forwarded upstream
        # before the request hook fires — silently disabling credential injection
        # on exactly the large-context calls that need it.
        flow.response.stream = True


addons = [CredentialInjector()]
