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

A ``purpose: "network"`` row (``start_network``) is a Lightbulb Connect sign-in instead: it
carries the computer's ``member_key`` and ``computer_name``, and its result is a proof
(``routers/network.py``). Every read names its purpose, so neither flow can see the other's rows.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.config import settings
from core.repositories import messaging_repo
from core.services.desktop_pair import _public_key_raw_bytes

LOGIN_TTL_S = 600
POLL_INTERVAL_S = 2
# No 0/O/1/I: the person may read the code off a terminal and compare it by eye.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MACHINE_NAME_MAX = 64
_EXPIRED = "This code expired. Run `lightbulb join` again."
NETWORK = "network"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _user_code() -> str:
    chars = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _live(row: dict | None) -> bool:
    return bool(row) and int(row.get("expiresAt", 0)) > time.time()


def printable(text: str | None, limit: int) -> str:
    """``text`` as a person will read it: printable characters only, trimmed, at most ``limit``."""
    return "".join(ch for ch in (text or "") if ch.isprintable()).strip()[:limit]


def clean_machine_name(name: str | None) -> str:
    """The Mac's hostname as the approver will read it."""
    return printable(name, MACHINE_NAME_MAX)


async def _create(fields: dict) -> tuple[str, str]:
    """A new login row: its secret code (only the SHA-256 is stored) and its user code."""
    code = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + LOGIN_TTL_S
    for _ in range(3):  # 32^8 codes; a collision with a live one is vanishingly rare
        user_code = _user_code()
        try:
            await messaging_repo.create_device_login(_hash(code), user_code, fields, expires_at)
            return code, user_code
        except ClientError as exc:
            if not messaging_repo.is_cancelled(exc):
                raise
    raise HTTPException(status_code=503, detail="Could not start device login. Try again.")


async def start(tauri_mac_pubkey: str, cli_pubkey: str, machine_name: str | None = None) -> dict:
    # Reject a bad key now: at approve it would fail inside the pairing, with the code bound.
    for pem in (tauri_mac_pubkey, cli_pubkey):
        try:
            _public_key_raw_bytes(pem)
        except Exception as exc:  # noqa: BLE001 -- any parse failure is the caller's bad key
            raise HTTPException(status_code=422, detail="Public keys must be PEM-encoded Ed25519.") from exc
    fields = {
        "status": "pending",
        "tauriMacPubKey": tauri_mac_pubkey,
        "cliPubKey": cli_pubkey,
        "machineName": clean_machine_name(machine_name),
    }
    code, user_code = await _create(fields)
    return {
        "code": code,
        "userCode": user_code,
        "verifyUrl": f"{settings.FRONTEND_URL.rstrip('/')}/auth/connect-machine?code={user_code}",
        "interval": POLL_INTERVAL_S,
        "expiresIn": LOGIN_TTL_S,
    }


async def start_network(member_key: str, computer: str | None) -> dict:
    """A Lightbulb Connect sign-in: the person TYPES the code at ``/device``, so the URI
    carries none (a phished link cannot pre-fill it)."""
    fields = {
        "status": "pending",
        "purpose": NETWORK,
        "member_key": member_key,
        "computer_name": clean_machine_name(computer),
    }
    code, user_code = await _create(fields)
    return {
        "device_code": code,
        "user_code": user_code,
        "verification_uri_complete": f"{settings.FRONTEND_URL.rstrip('/')}/device",
        "interval": POLL_INTERVAL_S,
    }


async def claim(user_code: str, owner_id: str, user_id: str, purpose: str | None = None) -> dict:
    """Bind the login to this approver and return the row (with the Mac's keys).

    410 when the code is unknown, used or expired; 409 when someone else approved it.
    Re-approving as the same person in the same workspace is allowed (pair and enroll
    are idempotent), so a lost approve response can simply be retried."""
    row = await _live_row(user_code, purpose)
    code_hash = row["pk"].removeprefix("DEVICELOGIN#")
    if not await messaging_repo.claim_device_login(code_hash, owner_id, user_id):
        await _live_row(user_code, purpose)  # taken or expired between the read and the claim: 410
        raise HTTPException(status_code=409, detail="Someone else already connected this Mac.")
    return row


async def _live_row(user_code: str, purpose: str | None = None) -> dict:
    row = await messaging_repo.get_device_login_by_user_code(user_code.strip().upper())
    if not _live(row) or row.get("purpose") != purpose:
        raise HTTPException(status_code=410, detail=_EXPIRED)
    return row


async def describe(user_code: str) -> dict:
    """What the approve page shows before the click: the name the Mac reported."""
    row = await _live_row(user_code)
    return {"machineName": row.get("machineName") or ""}


async def describe_network(user_code: str) -> dict:
    """A network sign-in's page: the computer's name and its key's first 12 characters
    (what `lightbulb` prints)."""
    row = await _live_row(user_code, NETWORK)
    return {"computer_name": row.get("computer_name") or "", "key_short": row["member_key"][:12]}


async def finish(row: dict, user_id: str, result: dict) -> None:
    # No expiry check here: an approve that lands just before 600 s still pairs and enrolls,
    # and the poll then reads expired. That is fine: rerunning `lightbulb join` pairs the same
    # keys again (pair and enroll are idempotent), so nothing is left half-done.
    await messaging_repo.finish_device_login(row["pk"].removeprefix("DEVICELOGIN#"), user_id, result)


async def poll(code: str, purpose: str | None = None) -> dict:
    code_hash = _hash(code)
    row = await messaging_repo.get_device_login(code_hash)
    if row and row.get("purpose") != purpose:
        return {"status": "expired"}  # the other flow's row: not ours to read or take
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
