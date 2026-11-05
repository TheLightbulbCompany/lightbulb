"""Materializer for X via the `xurl` CLI.

xurl stores app credentials and OAuth tokens in a single YAML file at ~/.xurl.
The backend writes the owner EFS copy of that file so the owner container's baked
xurl binary can act and refresh as the user without shell-based auth.

The file may also contain user-created xurl apps. This materializer owns only the
`lightbulb-x` app entry: writes replace that app and preserve every other app;
remove deletes only that app, deleting ~/.xurl only when no apps remain.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from core.config import settings
from core.services.credentials.materializers.base import NODE_GID, NODE_UID, materializer_root

logger = logging.getLogger(__name__)

_XURL_APP_NAME = "lightbulb-x"
_XURL_USERS_ME_URL = "https://api.x.com/2/users/me"


def _xurl_path(owner_id: str) -> Path:
    return materializer_root() / ".xurl"


def _default_redirect_uri(provider_key: str) -> str:
    # Mirrors routers/connections.py:_redirect_uri. Only a fallback: oauth_dance
    # carries the real callback on the credential, and that is what a fresh
    # connect uses. This keeps a credential minted before that field existed
    # from writing an empty redirect_uri into the owner's xurl app entry.
    return f"{settings.PUBLIC_API_BASE}/api/v1/oauth/{provider_key}/callback"


def _parse_store(raw: str) -> dict[str, Any]:
    data = yaml.safe_load(raw) if raw.strip() else None
    if not isinstance(data, dict):
        # Fresh/unparseable file. This key order matches xurl's own storeFile
        # struct, and safe_dump(sort_keys=False) preserves it on the way out.
        return {"apps": {}, "default_app": ""}
    apps = data.get("apps")
    # Spread `data` first so any top-level key xurl grows that we don't model
    # survives our rewrite — same reason we merge apps rather than replace them.
    return {**data, "apps": apps if isinstance(apps, dict) else {}, "default_app": data.get("default_app") or ""}


def _locked_update(path: Path, mutate) -> None:
    """fcntl-locked read-modify-write of the xurl YAML store on EFS.

    ``~/.xurl`` is a SHARED file: it also holds user-created xurl apps, and the
    owner's own in-container ``xurl`` invocations rewrite it (xurl persists
    refreshed tokens there). An unlocked read-then-overwrite lets a racing
    writer's apps vanish — so this uses the same ``fcntl.lockf``
    read-modify-write the retired ``mcporter_vault`` used.

    ``mutate`` receives the parsed store and returns the store to persist, or
    ``None`` to delete the file. The write is in place rather than via
    ``atomic_write``: a temp-file rename would swap the inode out from under any
    concurrent waiter, leaving it holding a lock on an orphan.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)  # O_CREAT mode is umask-masked; the file holds tokens
        store = _parse_store(os.read(fd, 1 << 24).decode())
        result = mutate(store)
        if result is None:
            os.ftruncate(fd, 0)
            path.unlink(missing_ok=True)
            return
        # sort_keys=False keeps the on-disk shape readable and stable: apps first,
        # default_app second, matching xurl's own storeFile struct order.
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        os.write(fd, yaml.safe_dump(result, sort_keys=False).encode())
        if os.geteuid() == 0:  # in-container only; tests run non-root
            os.fchown(fd, NODE_UID, NODE_GID)
    finally:
        fcntl.lockf(fd, fcntl.LOCK_UN)
        os.close(fd)
    if os.geteuid() == 0 and path.exists():
        os.chown(str(path.parent), NODE_UID, NODE_GID)


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
    """Write/replace Isol8's xurl app entry and return the connected handle/id.

    ``custody: backend`` (spec 13): the backend alone refreshes X's single-use refresh token, so
    the file carries no token and no client secret -- the bridge fills the vended access token and
    its expiry in locally each turn and never uploads it."""
    username = _token_username(credential)
    brokered = credential.get("custody") == "backend"
    entry = {
        "client_id": credential["client_id"],
        "client_secret": "" if brokered else credential.get("client_secret") or "",
        "redirect_uri": credential.get("redirect_uri") or _default_redirect_uri(provider_key),
        "default_user": username,
        "oauth2_tokens": {
            username: {
                "type": "oauth2",
                "oauth2": {
                    "access_token": "" if brokered else credential["access_token"],
                    "refresh_token": "" if brokered else credential.get("refresh_token") or "",
                    "expiration_time": 0 if brokered else _token_expiration_epoch(credential),
                },
            },
        },
    }

    def _mutate(store: dict[str, Any]) -> dict[str, Any]:
        store["apps"][_XURL_APP_NAME] = entry
        # Make xurl default to the freshly-connected Lightbulb app. Unrelated apps
        # remain in the file and become default again if lightbulb-x is later removed.
        store["default_app"] = _XURL_APP_NAME
        return store

    _locked_update(_xurl_path(owner_id), _mutate)
    return username


def remove(owner_id: str, provider_key: str, credential: dict[str, Any] | None = None) -> None:
    """Remove only the Lightbulb-owned xurl app, preserving unrelated xurl apps."""
    path = _xurl_path(owner_id)
    if not path.exists():
        return

    def _mutate(store: dict[str, Any]) -> dict[str, Any] | None:
        apps = store.get("apps") or {}
        if _XURL_APP_NAME not in apps:
            return store
        apps.pop(_XURL_APP_NAME, None)
        if not apps:
            return None  # nothing left worth keeping — drop the file
        store["apps"] = apps
        if store.get("default_app") == _XURL_APP_NAME:
            store["default_app"] = sorted(apps)[0]
        return store

    _locked_update(path, _mutate)
