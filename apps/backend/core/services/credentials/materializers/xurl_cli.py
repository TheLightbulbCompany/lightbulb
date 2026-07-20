"""Materializer for X via the `xurl` CLI (baked into the OpenClaw image).

xurl stores its app credentials and OAuth tokens in a plain YAML file at
``~/.xurl``. The runtime container's HOME is the per-owner EFS workspace, so
writing ``owner_efs_root(owner_id) / ".xurl"`` seeds the same store the baked
CLI reads and refreshes.

The file may contain multiple X apps. Connect/update owns only the provider's
app entry; disconnect removes only that app and leaves any owner-managed xurl
entries intact. Token values are never logged or passed on argv.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from core.services.credentials.materializers.base import atomic_write, owner_efs_root

_X_ME_URL = "https://api.x.com/2/users/me"
_DEFAULT_APP = "isol8-x"


def _xurl_path(owner_id: str) -> Path:
    return owner_efs_root(owner_id) / ".xurl"


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"apps": {}, "default_app": ""}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        return {"apps": {}, "default_app": ""}
    apps = data.get("apps")
    if not isinstance(apps, dict):
        data["apps"] = {}
    data.setdefault("default_app", "")
    return data


def _expiration_epoch(credential: dict[str, Any]) -> int:
    expires_in = credential.get("expires_in")
    if expires_in:
        return int(datetime.now(timezone.utc).timestamp()) + int(expires_in)

    token_expiry = credential.get("token_expiry")
    if token_expiry:
        # oauth_dance stores an ISO timestamp. Accept a trailing Z too, for tests
        # and future callers that use RFC3339's UTC spelling.
        dt = datetime.fromisoformat(str(token_expiry).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    return 0


def _fetch_username(access_token: str) -> str:
    resp = httpx.get(_X_ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    username = data.get("username")
    if not username:
        raise ValueError("X /2/users/me response did not include a username")
    return username


def _username(credential: dict[str, Any]) -> str:
    for key in ("username", "handle", "screen_name"):
        value = credential.get(key)
        if value:
            return str(value).lstrip("@")
    return _fetch_username(credential["access_token"])


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    """Seed ``~/.xurl`` and return the connected X username."""
    access_token = credential["access_token"]
    refresh_token = credential.get("refresh_token") or ""
    if not refresh_token:
        raise ValueError("xurl_cli requires an OAuth2 refresh_token; ensure offline.access was granted")

    app_name = credential.get("app_name") or _DEFAULT_APP
    username = _username(credential)

    path = _xurl_path(owner_id)
    store = _load_store(path)
    store["apps"][app_name] = {
        "client_id": credential["client_id"],
        "client_secret": credential.get("client_secret") or "",
        "redirect_uri": credential.get("redirect_uri") or credential.get("callback_url") or "",
        "default_user": username,
        "oauth2_tokens": {
            username: {
                "type": "oauth2",
                "oauth2": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expiration_time": _expiration_epoch(credential),
                },
            }
        },
    }
    # Make the Isol8-managed app the active app after connection. Owner-managed
    # extra app entries remain in the same store and are not modified.
    store["default_app"] = app_name

    body = yaml.safe_dump(store, default_flow_style=False, sort_keys=False)
    atomic_write(path, body.encode())
    return username


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    path = _xurl_path(owner_id)
    if not path.exists():
        return

    store = _load_store(path)
    app_name = credential.get("app_name") or _DEFAULT_APP
    apps = store.get("apps") or {}
    apps.pop(app_name, None)

    if not apps:
        path.unlink(missing_ok=True)
        return

    if store.get("default_app") == app_name:
        store["default_app"] = next(iter(apps.keys()))
    store["apps"] = apps
    body = yaml.safe_dump(store, default_flow_style=False, sort_keys=False)
    atomic_write(path, body.encode())
