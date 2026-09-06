from __future__ import annotations

import json
import stat
from pathlib import Path

import codex_subscription_auth
import pytest
from _fakes import FakeGh

FULL_AUTH = {
    "OPENAI_API_KEY": None,
    "auth_mode": "chatgpt",
    "tokens": {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "id_token": "old-id",
        "account_id": "account-123",
    },
    "last_refresh": "2026-08-30T12:00:00Z",
}


def test_consumer_auth_cannot_refresh() -> None:
    consumer = codex_subscription_auth.consumer_auth(FULL_AUTH)

    assert consumer["auth_mode"] == "chatgptAuthTokens"
    assert consumer["tokens"]["refresh_token"] == ""
    assert consumer["tokens"]["access_token"] == "old-access"
    assert FULL_AUTH["tokens"]["refresh_token"] == "old-refresh"


def test_prepare_writes_access_only_auth_with_private_permissions(
    tmp_path: Path,
) -> None:
    destination = tmp_path / ".codex" / "auth.json"
    consumer = codex_subscription_auth.consumer_auth(FULL_AUTH)

    mode = codex_subscription_auth.prepare(
        codex_auth_json=json.dumps(consumer),
        openai_api_key="also-set",
        destination=destination,
    )

    assert mode == "subscription"
    assert json.loads(destination.read_text()) == consumer
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_prepare_rejects_a_refreshable_consumer_bundle(tmp_path: Path) -> None:
    refreshable_consumer = codex_subscription_auth.consumer_auth(FULL_AUTH)
    refreshable_consumer["tokens"]["refresh_token"] = "must-not-leak"
    with pytest.raises(
        codex_subscription_auth.SubscriptionAuthError,
        match="must not contain a refresh token",
    ):
        codex_subscription_auth.prepare(
            codex_auth_json=json.dumps(refreshable_consumer),
            openai_api_key="",
            destination=tmp_path / "auth.json",
        )


def test_prepare_accepts_api_key_without_writing_auth_json(tmp_path: Path) -> None:
    destination = tmp_path / "auth.json"

    mode = codex_subscription_auth.prepare(
        codex_auth_json="",
        openai_api_key="sk-test",
        destination=destination,
    )

    assert mode == "api-key"
    assert not destination.exists()


def test_prepare_cli_publishes_the_selected_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert codex_subscription_auth.main(["prepare", str(tmp_path / "auth.json")]) == 0

    assert output.read_text() == "mode=api-key\n"


def test_stage_refresh_forces_codex_to_use_its_built_in_rotation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-home" / "auth.json"
    original = json.loads(json.dumps(FULL_AUTH))

    configured = codex_subscription_auth.stage_refresh(
        codex_auth_json=json.dumps(codex_subscription_auth.consumer_auth(FULL_AUTH)),
        refresh_auth_json=json.dumps(FULL_AUTH),
        refresh_pat="pat",
        destination=destination,
    )

    assert configured is True
    assert json.loads(destination.read_text()) == {
        **FULL_AUTH,
        "tokens": {
            **FULL_AUTH["tokens"],
            "access_token": codex_subscription_auth.FORCED_REFRESH_ACCESS_TOKEN,
        },
        "last_refresh": codex_subscription_auth.FORCED_STALE_REFRESH,
    }
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert FULL_AUTH == original


def test_stage_refresh_cli_publishes_whether_subscription_auth_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv(
        "CODEX_AUTH_JSON",
        json.dumps(codex_subscription_auth.consumer_auth(FULL_AUTH)),
    )
    monkeypatch.setenv("CODEX_REFRESH_AUTH_JSON", json.dumps(FULL_AUTH))
    monkeypatch.setenv("CODEX_REFRESH_PAT", "pat")

    assert (
        codex_subscription_auth.main(["stage-refresh", str(tmp_path / "auth.json")])
        == 0
    )
    assert output.read_text() == "configured=true\n"


def test_publish_refresh_writes_rotated_state_before_access_only_state(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    fake_gh.respond("secret", "set", with_="")
    auth_file = tmp_path / "auth.json"
    refreshed = json.loads(json.dumps(FULL_AUTH))
    refreshed["tokens"].update(
        access_token="new-access",
        refresh_token="new-refresh",
        id_token="new-id",
    )
    refreshed["last_refresh"] = "2026-09-06T12:00:00Z"
    auth_file.write_text(json.dumps(refreshed))

    codex_subscription_auth.publish_refresh(
        auth_file=auth_file,
        repository="owner/repo",
    )

    assert fake_gh.calls == [
        (
            "secret",
            "set",
            "CODEX_REFRESH_AUTH_JSON",
            "--repo",
            "owner/repo",
            "--env",
            "tend",
        ),
        (
            "secret",
            "set",
            "CODEX_AUTH_JSON",
            "--repo",
            "owner/repo",
            "--env",
            "tend",
        ),
    ]
    full = json.loads(fake_gh.stdins[0] or "")
    consumer = json.loads(fake_gh.stdins[1] or "")
    assert full["tokens"] == {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "id_token": "new-id",
        "account_id": "account-123",
    }
    assert full["last_refresh"] == "2026-09-06T12:00:00Z"
    assert consumer["auth_mode"] == "chatgptAuthTokens"
    assert consumer["tokens"]["refresh_token"] == ""


def test_publish_refresh_persists_rotation_before_reporting_codex_failure(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    fake_gh.respond("secret", "set", with_="")
    auth_file = tmp_path / "auth.json"
    refreshed = json.loads(json.dumps(FULL_AUTH))
    refreshed["tokens"]["refresh_token"] = "new-refresh"
    refreshed["last_refresh"] = "2026-09-06T12:00:00Z"
    auth_file.write_text(json.dumps(refreshed))

    with pytest.raises(
        codex_subscription_auth.SubscriptionAuthError,
        match="failed after refreshing and persisting",
    ):
        codex_subscription_auth.publish_refresh(
            auth_file=auth_file,
            repository="owner/repo",
            codex_succeeded=False,
        )

    assert [call[2] for call in fake_gh.calls] == [
        "CODEX_REFRESH_AUTH_JSON",
        "CODEX_AUTH_JSON",
    ]


def test_publish_refresh_refuses_the_staged_access_token(
    tmp_path: Path, fake_gh: FakeGh
) -> None:
    auth_file = tmp_path / "auth.json"
    stale = json.loads(json.dumps(FULL_AUTH))
    stale["tokens"]["access_token"] = (
        codex_subscription_auth.FORCED_REFRESH_ACCESS_TOKEN
    )
    stale["last_refresh"] = "2026-09-06T12:00:00Z"
    auth_file.write_text(json.dumps(stale))

    with pytest.raises(
        codex_subscription_auth.SubscriptionAuthError,
        match="did not refresh the staged auth.json",
    ):
        codex_subscription_auth.publish_refresh(
            auth_file=auth_file,
            repository="owner/repo",
        )

    assert fake_gh.calls == []


def test_stage_refresh_skips_when_no_subscription_secrets(tmp_path: Path) -> None:
    destination = tmp_path / "auth.json"

    assert not codex_subscription_auth.stage_refresh(
        codex_auth_json="",
        refresh_auth_json="",
        refresh_pat="",
        destination=destination,
    )
    assert not destination.exists()


@pytest.mark.parametrize(
    ("consumer", "refresh_auth", "pat", "missing"),
    [
        (
            json.dumps(codex_subscription_auth.consumer_auth(FULL_AUTH)),
            "",
            "",
            "CODEX_REFRESH_AUTH_JSON",
        ),
        ("", json.dumps(FULL_AUTH), "", "CODEX_REFRESH_PAT"),
        ("", "", "pat", "CODEX_REFRESH_AUTH_JSON"),
    ],
)
def test_stage_refresh_rejects_partial_subscription_configuration(
    tmp_path: Path, consumer: str, refresh_auth: str, pat: str, missing: str
) -> None:
    with pytest.raises(codex_subscription_auth.SubscriptionAuthError, match=missing):
        codex_subscription_auth.stage_refresh(
            codex_auth_json=consumer,
            refresh_auth_json=refresh_auth,
            refresh_pat=pat,
            destination=tmp_path / "auth.json",
        )
