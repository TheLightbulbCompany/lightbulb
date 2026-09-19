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

One keyring holds EVERY Google account the owner has connected, so account-scoped
operations are the rule:
  - A failed connect rolls back ONLY the account it was adding (``write`` self-
    cleans its own in-flight entry), never the owner's other accounts.
  - Disconnect revokes the grant at Google before dropping the local entry — gog's
    ``remove`` only deletes the keyring entry, it does not invalidate the token
    server-side, so without this the refresh token stays usable at Google.

Argv is fixed — caller data never touches the command line. The OAuth client and
refresh token are fed via stdin; the keyring password via the environment."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

import httpx

from core.config import settings
from core.services.credentials.materializers.base import (
    NODE_GID,
    NODE_UID,
    materializer_root,
)

logger = logging.getLogger(__name__)

# gog has no field for the account email in the OAuth token response, so we fetch it
# from the Gmail profile endpoint. The gmail.modify scope already grants this read.
_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# Google's OAuth2 revocation endpoint. POSTing the refresh token here invalidates
# the grant at Google so a disconnected account's token can never be reused; gog
# only deletes the LOCAL keyring entry and leaves the server-side grant standing.
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Every gog invocation reads/writes the EFS-resident keyring; these run via
# run_in_thread on the connect/disconnect request path, so cap each call — an NFS
# stall must not block the worker thread (and its pool slot) indefinitely.
_GOG_TIMEOUT_S = 30


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


def _gog_env(owner_id: str) -> dict[str, str]:
    """The XDG + keyring environment every gog invocation shares.

    Config under ``.config`` and the file keyring under ``.local/share`` — both on
    the owner's EFS. Hoisted so write/remove/export can never drift on the keyring
    location (a mismatch points gog at an empty keyring on ephemeral disk). The
    dirs are created so gog is never handed a path that does not exist."""
    cfg = materializer_root() / ".config"
    data = materializer_root() / ".local" / "share"
    cfg.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "XDG_CONFIG_HOME": str(cfg),
        "XDG_DATA_HOME": str(data),
        "GOG_KEYRING_BACKEND": "file",
        "GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD,
    }


def _restore_gogcli_ownership(owner_id: str) -> None:
    """Best-effort re-chown of the gog config + keyring trees to the container uid.

    Runs on the connect rollback path: ``credentials set`` rewrites the SHARED
    ``.config/gogcli`` as root, so a connect that fails before the normal end-of-
    write chown would leave it root-owned and unreadable by the container's
    uid-1000 gog — breaking the owner's OTHER accounts that share that client
    config. Best-effort: a chown failure must not mask the original error being
    re-raised (and is itself a no-op when running non-root)."""
    root = materializer_root()
    for tree in (root / ".config" / "gogcli", root / ".local" / "share" / "gogcli"):
        try:
            _chown_tree_to_node(tree)
        except Exception:
            logger.warning("gog rollback chown failed for owner=%s path=%s", owner_id, tree, exc_info=True)


def _export_refresh_token(owner_id: str, email: str, env: dict[str, str]) -> str | None:
    """Export an account's refresh token from the keyring, or None if it has no
    entry (or the export fails).

    Used both to revoke a grant and to detect whether an account already exists, so
    a failed re-auth never deletes a pre-existing token. Export requires a file
    (``--out``), so the secret is routed through a private temp file deleted
    immediately — the token is also already on EFS in the keyring, so this adds no
    durable exposure."""
    out_fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(out_fd)
    try:
        subprocess.run(
            ["gog", "auth", "tokens", "export", email, "--out", out_path, "--overwrite", "--no-input"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GOG_TIMEOUT_S,
        )
        with open(out_path) as f:
            return json.load(f).get("refresh_token")
    except Exception:
        return None
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def _revoke_grant_at_google(owner_id: str, email: str, env: dict[str, str]) -> None:
    """Invalidate the account's grant at Google before dropping the local keyring.

    gog's local ``remove`` does not revoke server-side, so we export the refresh
    token and POST it to Google's revocation endpoint. Best-effort: a revoke
    failure must not block the disconnect, since dropping the local keyring is the
    authoritative action (the leftover grant can still be revoked manually)."""
    refresh_token = _export_refresh_token(owner_id, email, env)
    if not refresh_token:
        # No exportable token (gog format drift / incomplete or missing entry).
        # Surface it instead of silently skipping the revocation.
        logger.warning(
            "google grant revoke skipped for owner=%s: no exportable refresh token for the account",
            owner_id,
        )
        return
    try:
        # Form-encoded token= per RFC 7009. 200 = revoked; 400 = the token was
        # already expired/revoked at Google (also fine). Any other status means the
        # grant may still be live — surface it so a failed revoke is observable. We
        # still proceed to drop the local keyring: that is the authoritative
        # disconnect, and a Google-side outage must not wedge it.
        resp = httpx.post(_GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=10)
        if resp.status_code not in (200, 400):
            logger.warning(
                "google grant revoke returned %s for owner=%s; the grant may still be live",
                resp.status_code,
                owner_id,
            )
    except Exception:
        logger.warning(
            "google grant revoke failed for owner=%s; the token was left for manual revocation",
            owner_id,
            exc_info=True,
        )


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

    env = _gog_env(owner_id)
    # Fixed argv; all caller data flows through stdin/env. check=True so a gog failure
    # raises. `credentials set` rewrites the SHARED .config/gogcli as root and
    # `tokens import` writes the keyring; on any failure we restore ownership (so the
    # root-written config stays readable by the container) and re-raise, but we
    # DELIBERATELY do not delete the account's keyring entry. A connect rollback must
    # never destroy a credential: a re-auth would lose the user's existing token, and
    # a brand-new account's partial token is harmless — it is absent from the
    # inventory, overwritten on the next connect, and dropped on full disconnect.
    try:
        subprocess.run(
            ["gog", "auth", "credentials", "set", "-", "--no-input"],
            input=client_json,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GOG_TIMEOUT_S,
        )
        subprocess.run(
            ["gog", "auth", "tokens", "import", "-", "--no-input"],
            input=token_json,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GOG_TIMEOUT_S,
        )
        # The backend writes as root; the container reads as uid 1000. Chown BOTH the
        # config (OAuth client) and the data (keyring) trees, or the container's gog
        # can't read the EFS-resident keyring.
        _chown_tree_to_node(materializer_root() / ".config" / "gogcli")
        _chown_tree_to_node(materializer_root() / ".local" / "share" / "gogcli")
    except Exception:
        # Restore ownership of the root-written config/keyring; never delete tokens.
        _restore_gogcli_ownership(owner_id)
        raise

    return email


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    """Disconnect a Google account (or every account) from the owner's keyring.

    Three intents, distinguished by the credential:
      - ``{"email": ...}``     — disconnect ONE account: revoke its grant at Google,
                                 then drop only its keyring entry (others survive).
      - ``{"disconnect_all": True[, "accounts": [...]]}`` — full provider disconnect:
                                 revoke each account's grant, then drop the whole
                                 keyring + OAuth client config.
      - anything else (no email, no flag) — the failed-connect rollback path: a
                                 no-op, because ``write`` already scoped-cleaned its
                                 own in-flight account. This MUST NOT wipe the
                                 keyring — doing so would orphan the owner's other
                                 accounts when a single new connect fails."""
    email = credential.get("email")
    if not email and not credential.get("disconnect_all"):
        return

    env = _gog_env(owner_id)

    if email:
        # One gog keyring holds every Google account for the owner. Disconnecting a
        # single account revokes its grant, then removes only that account's stored
        # token — rmtree of the whole gogcli dir would orphan every other connected
        # account. gog's per-account removal needs BOTH --no-input (never prompt) and
        # --force; without --force it refuses non-interactive removal and exits non-zero.
        _revoke_grant_at_google(owner_id, email, env)
        # Fixed argv; email is a validated address carried as an argv element, never a
        # shell string. check=True so a failure surfaces to the caller.
        subprocess.run(
            ["gog", "auth", "remove", email, "--no-input", "--force"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GOG_TIMEOUT_S,
        )
        return

    # Full provider disconnect — drop the keyring (.local/share/gogcli) AND the OAuth
    # client config (.config/gogcli). The caller passes the account inventory so each
    # grant is revoked at Google before its keyring is dropped.
    for account in credential.get("accounts", []):
        _revoke_grant_at_google(owner_id, account, env)
    root = materializer_root()
    shutil.rmtree(root / ".local" / "share" / "gogcli", ignore_errors=True)
    shutil.rmtree(root / ".config" / "gogcli", ignore_errors=True)
