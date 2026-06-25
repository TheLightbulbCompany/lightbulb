"""Materializer for Google via the `gog` CLI (baked into the backend image).

The backend mounts every owner's per-user EFS and runs as root, so we seed gog by
running the REAL gog binary on the backend against the owner's EFS dirs. gog's
keyring is a password-derived JWE blob — only the real binary can produce it, so a
hand-written token file is not a substitute. gog follows the XDG base-dir spec and
splits config from data: it reads its OAuth client config from `XDG_CONFIG_HOME`
(`.config/gogcli`) but stores the file KEYRING under `XDG_DATA_HOME`
(`.local/share/gogcli`). BOTH must point at the owner's EFS — `XDG_DATA_HOME`
especially, because the keyring (the actual tokens) lives there; left unset it
defaults to the backend's own ephemeral `$HOME/.local/share`, off EFS, so the
per-user container never sees it. These EFS dirs map to the container's
`/home/node/.openclaw/{.config,.local/share}/gogcli` (the per-user access point
mounts at `/home/node/.openclaw`). The container's gog reads that same keyring and
is the runtime actor + self-refresher; it decrypts the JWE using the SAME shared
`GOG_KEYRING_PASSWORD`, so the blob is portable between backend and container.

Argv is fixed — caller data never touches the command line. The OAuth client and
refresh token are fed via stdin; the keyring password via the environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import httpx

from core.config import settings
from core.services.credentials.materializers.base import (
    NODE_GID,
    NODE_UID,
    owner_efs_root,
)

# gog has no field for the account email in the OAuth token response, so we fetch it
# from the Gmail profile endpoint. The gmail.modify scope already grants this read.
_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def _fetch_account_email(access_token: str) -> str:
    resp = httpx.get(
        _GMAIL_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()["emailAddress"]


def _chown_tree_to_node(path) -> None:
    # Mirror base.atomic_write: only chown when running as root (in-container). Tests
    # run non-root and skip this, matching the rest of the materializer rail.
    if os.geteuid() != 0:
        return
    # gog may not create every dir on every run (e.g. config-only vs keyring-only
    # writes); skip a missing path rather than raising FileNotFoundError.
    if not os.path.exists(path):
        return
    os.chown(path, NODE_UID, NODE_GID)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), NODE_UID, NODE_GID)


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    """Seed the owner's gog keyring and return the connected account's email.

    The email is the natural identity of a Google account and is derived here
    anyway; returning it lets the connect path record the account in the
    per-owner inventory without a redundant Gmail profile fetch."""
    email = _fetch_account_email(credential["access_token"])

    # gog rejects a flat client object — the OAuth client must be wrapped under "web".
    client_json = json.dumps(
        {
            "web": {
                "client_id": credential["client_id"],
                "client_secret": credential["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
    )
    # The token field is `email`, NOT `account`; other fields are ignored by gog.
    token_json = json.dumps({"email": email, "refresh_token": credential["refresh_token"]})

    cfg = owner_efs_root(owner_id) / ".config"
    data = owner_efs_root(owner_id) / ".local" / "share"
    cfg.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(cfg),
        # gog stores the file keyring under XDG_DATA_HOME (not XDG_CONFIG_HOME).
        # Without this it lands on the backend's ephemeral disk and never reaches
        # the per-user container; the container reads it from this EFS path.
        "XDG_DATA_HOME": str(data),
        "GOG_KEYRING_BACKEND": "file",
        "GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD,
    }

    # Fixed argv; all caller data flows through stdin/env. check=True so a gog failure
    # raises and the caller rolls back + surfaces the error.
    subprocess.run(
        ["gog", "auth", "credentials", "set", "-", "--no-input"],
        input=client_json,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["gog", "auth", "tokens", "import", "-", "--no-input"],
        input=token_json,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    # The backend writes as root; the container reads as uid 1000. Chown BOTH the
    # config (OAuth client) and the data (keyring) trees, or the container's gog
    # can't read the EFS-resident keyring.
    _chown_tree_to_node(cfg / "gogcli")
    _chown_tree_to_node(data / "gogcli")
    return email


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    email = credential.get("email")
    if email:
        # One gog keyring holds every Google account for the owner. Disconnecting a
        # single account must remove only that account's stored token — rmtree of the
        # whole gogcli dir would orphan every other connected account. gog's per-account
        # removal needs BOTH --no-input (never prompt) and --force; without --force it
        # refuses non-interactive removal and exits non-zero.
        cfg = owner_efs_root(owner_id) / ".config"
        data = owner_efs_root(owner_id) / ".local" / "share"
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(cfg),
            # Must match write(): gog reads the keyring from XDG_DATA_HOME, so an
            # unset value would point the per-account remove at an empty keyring
            # on the backend's ephemeral disk and silently drop nothing.
            "XDG_DATA_HOME": str(data),
            "GOG_KEYRING_BACKEND": "file",
            "GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD,
        }
        # Fixed argv; email is a validated address carried as an argv element, never a
        # shell string. check=True so a failure surfaces to the caller.
        subprocess.run(
            ["gog", "auth", "remove", email, "--no-input", "--force"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return

    # No email means the caller intends to disconnect EVERY account — drop the
    # keyring (.local/share/gogcli) AND the OAuth client config (.config/gogcli).
    root = owner_efs_root(owner_id)
    shutil.rmtree(root / ".local" / "share" / "gogcli", ignore_errors=True)
    shutil.rmtree(root / ".config" / "gogcli", ignore_errors=True)
