"""Prepare and rotate Codex's experimental ChatGPT subscription auth.

Consumer jobs receive an access-only ``auth.json``. The sole refresh workflow
holds the rotating refresh token, updates that full bundle first, then derives
and publishes the next access-only bundle. This keeps concurrent consumers
from racing the refresh-token chain.

The consumer mode is a pinned Codex implementation detail. The refresher uses
Codex's built-in rotation path, then persists the file Codex wrote.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import _common

CONSUMER_AUTH_MODE = "chatgptAuthTokens"
FULL_AUTH_SECRET = "CODEX_REFRESH_AUTH_JSON"
CONSUMER_AUTH_SECRET = "CODEX_AUTH_JSON"
REFRESH_PAT_SECRET = "CODEX_REFRESH_PAT"
TEND_ENVIRONMENT = "tend"
FORCED_STALE_REFRESH = "1970-01-01T00:00:00Z"


class SubscriptionAuthError(ValueError):
    """The configured subscription credentials cannot satisfy their role."""


def _object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubscriptionAuthError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SubscriptionAuthError(f"{label} must contain a JSON object")
    return value


def _tokens(bundle: Mapping[str, Any], label: str) -> dict[str, Any]:
    value = bundle.get("tokens")
    if not isinstance(value, dict):
        raise SubscriptionAuthError(f"{label} is missing tokens")
    return value


def _require_token(tokens: Mapping[str, Any], field: str, label: str) -> str:
    value = tokens.get(field)
    if not isinstance(value, str) or not value:
        raise SubscriptionAuthError(f"{label} is missing tokens.{field}")
    return value


def _validate_full(bundle: Mapping[str, Any]) -> None:
    if bundle.get("auth_mode") != "chatgpt":
        raise SubscriptionAuthError(f"{FULL_AUTH_SECRET} must have auth_mode 'chatgpt'")
    tokens = _tokens(bundle, FULL_AUTH_SECRET)
    for field in ("access_token", "refresh_token", "id_token", "account_id"):
        _require_token(tokens, field, FULL_AUTH_SECRET)


def _validate_consumer(bundle: Mapping[str, Any]) -> None:
    if bundle.get("auth_mode") != CONSUMER_AUTH_MODE:
        raise SubscriptionAuthError(
            f"{CONSUMER_AUTH_SECRET} must have auth_mode '{CONSUMER_AUTH_MODE}'"
        )
    tokens = _tokens(bundle, CONSUMER_AUTH_SECRET)
    for field in ("access_token", "id_token", "account_id"):
        _require_token(tokens, field, CONSUMER_AUTH_SECRET)
    refresh_token = tokens.get("refresh_token", "")
    if refresh_token not in (None, ""):
        raise SubscriptionAuthError(
            f"{CONSUMER_AUTH_SECRET} must not contain a refresh token"
        )


def consumer_auth(full_bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the non-refreshing bundle given to concurrent Codex jobs."""
    _validate_full(full_bundle)
    consumer = copy.deepcopy(dict(full_bundle))
    consumer["auth_mode"] = CONSUMER_AUTH_MODE
    consumer["OPENAI_API_KEY"] = None
    consumer["tokens"]["refresh_token"] = ""
    _validate_consumer(consumer)
    return consumer


def _write_auth(bundle: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(bundle, file, separators=(",", ":"))
        file.write("\n")
    destination.chmod(0o600)


def prepare(*, codex_auth_json: str, openai_api_key: str, destination: Path) -> str:
    """Validate the selected auth path and stage an access-only auth file."""
    if codex_auth_json:
        bundle = _object(codex_auth_json, CONSUMER_AUTH_SECRET)
        _validate_consumer(bundle)
        _write_auth(bundle, destination)
        return "subscription"
    if openai_api_key:
        return "api-key"
    raise SubscriptionAuthError(
        f"set either {CONSUMER_AUTH_SECRET} (subscription) or OPENAI_API_KEY"
    )


def _set_environment_secret(
    repository: str, name: str, value: Mapping[str, Any]
) -> None:
    _common.gh(
        "secret",
        "set",
        name,
        "--repo",
        repository,
        "--env",
        TEND_ENVIRONMENT,
        input=json.dumps(value, separators=(",", ":")),
    )


def stage_refresh(
    *,
    codex_auth_json: str,
    refresh_auth_json: str,
    refresh_pat: str,
    destination: Path,
) -> bool:
    """Validate subscription secrets and stage deliberately stale full auth."""
    configured = {
        CONSUMER_AUTH_SECRET: codex_auth_json,
        FULL_AUTH_SECRET: refresh_auth_json,
        REFRESH_PAT_SECRET: refresh_pat,
    }
    if not any(configured.values()):
        return False
    missing = [name for name, value in configured.items() if not value]
    if missing:
        raise SubscriptionAuthError(
            f"subscription auth is partially configured; missing {', '.join(missing)}"
        )

    # Validate the existing consumer too. This catches a stale installation
    # where every job still has the refreshable bundle the split design is
    # specifically meant to remove.
    _validate_consumer(_object(codex_auth_json, CONSUMER_AUTH_SECRET))
    full = _object(refresh_auth_json, FULL_AUTH_SECRET)
    _validate_full(full)

    staged = copy.deepcopy(full)
    staged["last_refresh"] = FORCED_STALE_REFRESH
    _write_auth(staged, destination)
    return True


def publish_refresh(
    *, auth_file: Path, repository: str, codex_succeeded: bool = True
) -> None:
    """Publish the full file Codex refreshed, then its consumer projection."""
    try:
        refreshed = _object(auth_file.read_text(), FULL_AUTH_SECRET)
    except OSError as exc:
        raise SubscriptionAuthError(
            f"Codex auth refresh did not leave a readable auth.json: {exc}"
        ) from exc
    _validate_full(refreshed)
    if refreshed.get("last_refresh") == FORCED_STALE_REFRESH:
        raise SubscriptionAuthError("Codex did not refresh the staged auth.json")

    # Rotating refresh tokens make order part of the durability contract. Once
    # Codex rotates the old token, persist the replacement before updating the
    # disposable consumer view. Persist even if the probe exited non-zero after
    # rotating; otherwise a later model error could strand the only valid token.
    _set_environment_secret(repository, FULL_AUTH_SECRET, refreshed)
    _set_environment_secret(repository, CONSUMER_AUTH_SECRET, consumer_auth(refreshed))
    if not codex_succeeded:
        raise SubscriptionAuthError("Codex failed after refreshing and persisting auth")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit(
            "usage: codex_subscription_auth.py prepare PATH | "
            "stage-refresh PATH | publish-refresh PATH"
        )
    command = args.pop(0)
    try:
        if command == "prepare" and len(args) == 1:
            mode = prepare(
                codex_auth_json=os.environ.get(CONSUMER_AUTH_SECRET, ""),
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
                destination=Path(args[0]),
            )
            if os.environ.get("GITHUB_OUTPUT"):
                _common.set_output("mode", mode)
            print(f"Codex auth: using {mode}")
            return 0
        if command == "stage-refresh" and len(args) == 1:
            configured = stage_refresh(
                codex_auth_json=os.environ.get(CONSUMER_AUTH_SECRET, ""),
                refresh_auth_json=os.environ.get(FULL_AUTH_SECRET, ""),
                refresh_pat=os.environ.get(REFRESH_PAT_SECRET, ""),
                destination=Path(args[0]),
            )
            if os.environ.get("GITHUB_OUTPUT"):
                _common.set_output("configured", str(configured).lower())
            print(
                "Codex subscription auth staged"
                if configured
                else "Codex subscription auth is not configured; nothing to refresh"
            )
            return 0
        if command == "publish-refresh" and len(args) == 1:
            values = _common.require_env("GITHUB_REPOSITORY")
            publish_refresh(
                auth_file=Path(args[0]),
                repository=values["GITHUB_REPOSITORY"],
                codex_succeeded=os.environ.get("CODEX_OUTCOME") == "success",
            )
            print("Codex subscription auth refreshed")
            return 0
    except SubscriptionAuthError as exc:
        raise SystemExit(str(exc)) from exc
    raise SystemExit(
        "usage: codex_subscription_auth.py prepare PATH | "
        "stage-refresh PATH | publish-refresh PATH"
    )


if __name__ == "__main__":
    raise SystemExit(main())
