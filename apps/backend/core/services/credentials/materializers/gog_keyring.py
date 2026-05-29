"""Materializer for Google via the `gog` CLI (baked into the image). Seeds TWO
artifacts on EFS: the OAuth client (`~/.config/gogcli/config.json`) and the
refresh token, after which `gog` self-refreshes.

PROVISIONAL: the exact on-disk shape of gog's file-keyring token store is not yet
verified against the real binary. Verify against live `gog auth` output during the
M6 Google smoke (plan Task 6.3) and adjust `tokens.json` here if needed."""

from __future__ import annotations

import json

from core.services.credentials.materializers.base import atomic_write, owner_efs_root


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    root = owner_efs_root(owner_id) / ".config" / "gogcli"
    # 1) OAuth client config (the `gog auth credentials set` artifact).
    client = {
        "installed": {
            "client_id": credential["client_id"],
            "client_secret": credential.get("client_secret", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    atomic_write(root / "config.json", json.dumps(client).encode())
    # 2) Refresh token for the file keyring backend. PROVISIONAL shape — verify in M6.
    token = {
        "account": credential.get("account_email", ""),
        "refresh_token": credential["refresh_token"],
        "access_token": credential.get("access_token", ""),
    }
    atomic_write(root / "tokens.json", json.dumps(token).encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    root = owner_efs_root(owner_id) / ".config" / "gogcli"
    (root / "config.json").unlink(missing_ok=True)
    (root / "tokens.json").unlink(missing_ok=True)
