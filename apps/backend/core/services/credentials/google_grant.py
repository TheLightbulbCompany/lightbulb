"""A Google access token for the backend's own reads (the meeting seat's Gmail/Calendar/Drive), from the
grant the workspace already connected -- no new consent screen, no second OAuth client.

Google stays ``refresh_owner: actor``: the workspace computer's gog keyring is the grant's custodian
and the backend retains nothing. To read, the backend does what disconnect's revoke already does: seed
a scratch copy of the keyring from a roster profile in S3, ``gog auth tokens export`` the account's
refresh token, and trade it at Google for an access token with the platform app. Google does not
rotate refresh tokens on use, so this runs beside gog without spending its grant (unlike X/QuickBooks,
whose single-use tokens needed the custody cutover). The refresh and access tokens stay in this
process's memory only. Any failure is a ``ValueError``: a read path never flips the integration row.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from core.dynamodb import run_in_thread
from core.repositories import google_accounts_repo
from core.services.credentials import hermes_cli, oauth_dance
from core.services.credentials.materializers import base, gog_cli

#: owner -> (refresh token, access token, expires_at epoch s)
_grants: dict[str, tuple[str, str, int]] = {}
_MARGIN_S = 120


def _export_sync(owner_id: str, agent_ids: list[str], email: str) -> str | None:
    from core.services import hermes_profiles

    owned = hermes_cli.profile_paths("gog_cli")
    with tempfile.TemporaryDirectory(prefix="google-grant-") as tmp:
        root = Path(tmp)
        for donor in agent_ids:
            hermes_profiles.fetch_profile_subtree(owner_id, donor, owned, root)
            if hermes_cli._snapshot(root, "gog_cli"):
                break
        else:
            return None
        with base.materialize_into(root / hermes_cli.ROOT_DIR["gog_cli"]):
            return gog_cli._export_refresh_token(owner_id, email, gog_cli._gog_env(owner_id))


async def access_token(owner_id: str) -> tuple[str, int]:
    """``(access_token, expires_at)`` for the workspace's default Google account (the oldest connected,
    as an unassigned agent's gog uses)."""
    cached = _grants.get(owner_id)
    if cached and cached[2] > time.time() + _MARGIN_S:
        return cached[1], cached[2]
    refresh = cached[0] if cached else None
    if refresh is None:
        accounts = await google_accounts_repo.list_accounts(owner_id)
        if not accounts:
            raise ValueError("google: no connected account")
        refresh = await run_in_thread(_export_sync, owner_id, await hermes_cli._roster(owner_id), accounts[0]["email"])
        if not refresh:
            raise ValueError("google: no exportable grant in the workspace keyring")
    try:
        cred = await oauth_dance.refresh(owner_id, "google", {"refresh_token": refresh})
    except Exception as exc:  # noqa: BLE001 -- revoked/expired: drop it; the next call re-exports
        _grants.pop(owner_id, None)
        raise ValueError(f"google: refresh failed ({type(exc).__name__})") from None
    expires_at = int(time.time()) + int(cred.get("expires_in") or 3600)
    _grants[owner_id] = (cred["refresh_token"], cred["access_token"], expires_at)
    return cred["access_token"], expires_at
