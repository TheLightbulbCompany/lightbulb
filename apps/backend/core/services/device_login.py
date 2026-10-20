"""Device login for `lightbulb join`: a Mac with no browser session gets paired by a
signed-in person approving a short code in their browser.

1. ``start`` (no auth): the CLI sends the Mac's two public keys and gets a secret ``code``
   (what it polls with) and a short ``userCode`` (what the person sees in the browser).
2. ``approve`` (Clerk auth): the router pairs and enrolls the Mac server-side AS the
   approver, in the approver's own workspace, then ``finish`` stores the result.
3. ``poll`` (no auth): returns the stored result once, then the row is gone.

The CLI never holds a Clerk token. ``code`` is a bearer secret for the pairing result
(gateway token included), so only its SHA-256 is stored, and it is single use.
Expiry is ``LOGIN_TTL_S``, enforced on every read; DynamoDB's ``expiresAt`` TTL only
garbage-collects.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.config import settings
from core.repositories import messaging_repo

LOGIN_TTL_S = 600
POLL_INTERVAL_S = 2
# No 0/O/1/I: the person may read the code off a terminal and compare it by eye.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _user_code() -> str:
    chars = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _live(row: dict | None) -> bool:
    return bool(row) and int(row.get("expiresAt", 0)) > time.time()


async def start(tauri_mac_pubkey: str, cli_pubkey: str) -> dict:
    code = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + LOGIN_TTL_S
    fields = {"status": "pending", "tauriMacPubKey": tauri_mac_pubkey, "cliPubKey": cli_pubkey}
    for _ in range(3):  # 32^8 codes; a collision with a live one is vanishingly rare
        user_code = _user_code()
        try:
            await messaging_repo.create_device_login(_hash(code), user_code, fields, expires_at)
            break
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    else:
        raise HTTPException(status_code=503, detail="Could not start device login. Try again.")
    return {
        "code": code,
        "userCode": user_code,
        "verifyUrl": f"{settings.FRONTEND_URL.rstrip('/')}/auth/connect-machine?code={user_code}",
        "interval": POLL_INTERVAL_S,
        "expiresIn": LOGIN_TTL_S,
    }


async def claim(user_code: str, owner_id: str, user_id: str) -> dict:
    """Bind the login to this approver and return the row (with the Mac's keys).

    410 when the code is unknown, used or expired; 409 when someone else approved it.
    Re-approving as the same person in the same workspace is allowed (pair and enroll
    are idempotent), so a lost approve response can simply be retried."""
    row = await messaging_repo.get_device_login_by_user_code(user_code.strip().upper())
    if not _live(row):
        raise HTTPException(status_code=410, detail="This code expired. Run `lightbulb join` again.")
    code_hash = row["pk"].removeprefix("DEVICELOGIN#")
    if not await messaging_repo.claim_device_login(code_hash, owner_id, user_id):
        raise HTTPException(status_code=409, detail="Someone else already connected this Mac.")
    return row


async def finish(row: dict, user_id: str, result: dict) -> None:
    await messaging_repo.finish_device_login(row["pk"].removeprefix("DEVICELOGIN#"), user_id, result)


async def poll(code: str) -> dict:
    code_hash = _hash(code)
    row = await messaging_repo.get_device_login(code_hash)
    if not _live(row):
        if row:
            await messaging_repo.take_device_login(code_hash, approved_only=False)
        return {"status": "expired"}
    if row.get("status") != "approved":
        return {"status": "pending"}
    taken = await messaging_repo.take_device_login(code_hash, approved_only=True)
    if not taken:  # a racing poll took it first
        return {"status": "expired"}
    return {"status": "approved", **taken["result"]}
