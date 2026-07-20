"""Materializer for X via the `xurl` CLI.

xurl stores app credentials and OAuth tokens in a single YAML file at ~/.xurl.
The backend writes the owner EFS copy of that file so the owner container's baked
xurl binary can act and refresh as the user without shell-based auth.

The file may also contain user-created xurl apps. This materializer owns only the
`isol8-x` app entry: writes replace that app and preserve every other app;
remove deletes only that app, deleting ~/.xurl only when no apps remain.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from core.services.credentials.materializers.base import atomic_write, owner_efs_root

logger = logging.getLogger(__name__)

_XURL_APP_NAME = "isol8-x"
_XURL_USERS_ME_URL = "https://api.x.com/2/users/me"


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
        apps = {}
    return {"apps": apps, "default_app": data.get("default_app") or ""}


def _dump_store(path: Path, store: dict[str, Any]) -> None:
    # sort_keys=False keeps the on-disk shape readable and stable: apps first,
    # default_app second, matching xurl's own storeFile struct order.
    data = yaml.safe_dump(store, sort_keys=False).encode()
    atomic_write(path, data, mode=0o600)


def _fetch_username(access_token: str) -> str | None:
    try:
        resp = httpx.get(_XURL_USERS_ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        resp.raise_for_status()
        user = (resp.json().get("data") or {}).get("username")
        return str(user) if user else None
    except Exception:
        # Some X app/package combinations reject /2/users/me even when the OAuth
        # token is otherwise valid for the owner's allowed actions. Do not wedge
        # connection materialization; xurl can still use the fallback token key.
        logger.warning("xurl username lookup failed; writing token with fallback identity", exc_info=True)
        return None


def _token_expiration_epoch(credential: dict[str, Any]) -> int:
    # xurl stores an epoch-seconds expiration_time. oauth_dance carries expires_in
    # from the provider; a missing value is represented as 0 (xurl treats it as
    # expired and refreshes before use when a refresh token exists).
    expires_in = credential.get("expires_in")
    try:
        if expires_in is None:
            return 0
        return int(time.time()) + int(expires_in)
    except (TypeError, ValueError):
        return 0


def _token_username(credential: dict[str, Any]) -> str:
    return (
        credential.get("username")
        or credential.get("screen_name")
        or credential.get("handle")
        or _fetch_username(credential["access_token"])
        or credential.get("provider_user_id")
        or "default"
    )


def write(owner_id: str, provider_key: str, credential: dict[str, Any]) -> str:
    """Write/replace Isol8's xurl app entry and return the connected handle/id."""
    path = _xurl_path(owner_id)
    store = _load_store(path)
    username = _token_username(credential)

    store["apps"][_XURL_APP_NAME] = {
        "client_id": credential["client_id"],
        "client_secret": credential.get("client_secret") or "",
        "redirect_uri": credential.get("redirect_uri") or "",
        "default_user": username,
        "oauth2_tokens": {
            username: {
                "type": "oauth2",
                "oauth2": {
                    "access_token": credential["access_token"],
                    "refresh_token": credential.get("refresh_token") or "",
                    "expiration_time": _token_expiration_epoch(credential),
                },
            },
        },
    }
    # Make xurl default to the freshly-connected Isol8 app. Unrelated apps remain
    # in the file and become default again if isol8-x is later removed.
    store["default_app"] = _XURL_APP_NAME
    _dump_store(path, store)
    return username


def remove(owner_id: str, provider_key: str, credential: dict[str, Any] | None = None) -> None:
    """Remove only the Isol8-owned xurl app, preserving unrelated xurl apps."""
    path = _xurl_path(owner_id)
    if not path.exists():
        return
    store = _load_store(path)
    apps = store.get("apps") or {}
    if _XURL_APP_NAME not in apps:
        return
    apps.pop(_XURL_APP_NAME, None)
    if not apps:
        path.unlink(missing_ok=True)
        return
    store["apps"] = apps
    if store.get("default_app") == _XURL_APP_NAME:
        store["default_app"] = sorted(apps)[0]
    _dump_store(path, store)
