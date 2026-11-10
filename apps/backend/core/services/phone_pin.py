"""The owner's phone PIN (board row 3.29): a member who calls the business's own number from their saved mobile and
types their PIN on the keypad talks as themselves (the orb's owner mode), not as a caller.

Caller ID alone is never proof (it is spoofable): the number only says whose PIN to check. The PIN is stored as a
salted scrypt hash and compared in constant time. Wrong tries lock out per call (the call record's `pinTries`,
routers/voice_phone.py) and per member per UTC day (the counter row here). Nothing is ever said back about why a
try failed.

Rows in the messaging table:
    OWNER#<owner>   PHONEPIN#<user>                 mobile (E.164), salt, hash, updatedAt
    OWNER#<owner>   PHONEPINFAIL#<user>#<YYYY-MM-DD> wrong tries that day (expiresAt: two days on)
"""

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime

from boto3.dynamodb.conditions import Key

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo

CALL_TRIES = 3  # wrong PINs on one call before owner mode is off for it
DAY_TRIES = 5  # wrong PINs for one member in a UTC day before their PIN stops working until tomorrow
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, **_SCRYPT)


def _pin_key(owner_id: str, user_id: str) -> dict:
    return {"pk": messaging_repo.owner_pk(owner_id), "sk": f"PHONEPIN#{user_id}"}


def _fail_key(owner_id: str, user_id: str) -> dict:
    return {"pk": messaging_repo.owner_pk(owner_id), "sk": f"PHONEPINFAIL#{user_id}#{datetime.now(UTC):%Y-%m-%d}"}


async def save(owner_id: str, user_id: str, mobile: str, pin: str) -> None:
    salt = secrets.token_bytes(16)
    item = {**_pin_key(owner_id, user_id), "mobile": mobile, "salt": salt.hex(), "hash": _hash(pin, salt).hex(),
            "updatedAt": utc_now_iso()}  # fmt: skip
    await run_in_thread(messaging_repo._table().put_item, Item=item)


async def get(owner_id: str, user_id: str) -> dict | None:
    """The member's saved mobile, or None. Never the hash."""
    row = await messaging_repo._get(**_pin_key(owner_id, user_id))
    return {"mobile": row["mobile"], "updatedAt": row.get("updatedAt")} if row else None


async def holders(owner_id: str, caller: str) -> list[dict]:
    """The members whose saved mobile is `caller` (each with a PIN: a row is only written with one)."""
    if not caller:
        return []
    args = {
        "KeyConditionExpression": Key("pk").eq(messaging_repo.owner_pk(owner_id)) & Key("sk").begins_with("PHONEPIN#"),
        "ConsistentRead": True,
    }
    rows = []
    while True:
        out = await run_in_thread(messaging_repo._table().query, **args)
        rows += [r for r in out.get("Items", []) if r.get("mobile") == caller]
        if not out.get("LastEvaluatedKey"):
            return rows
        args["ExclusiveStartKey"] = out["LastEvaluatedKey"]


async def _fails_today(owner_id: str, user_id: str) -> int:
    row = await messaging_repo._get(**_fail_key(owner_id, user_id))
    return int((row or {}).get("tries", 0))


async def _count_fail(owner_id: str, user_id: str) -> None:
    await run_in_thread(
        messaging_repo._table().update_item,
        Key=_fail_key(owner_id, user_id),
        UpdateExpression="ADD tries :one SET expiresAt = :exp",
        ExpressionAttributeValues={":one": 1, ":exp": int(time.time()) + 2 * 86400},
    )


async def check(owner_id: str, caller: str, pin: str) -> str | None:
    """The member whose saved mobile is `caller` and whose PIN is `pin`, or None. A member locked for the day is
    never checked; a wrong try counts against every member the number names."""
    tried = []
    for row in await holders(owner_id, caller):
        user_id = row["sk"].removeprefix("PHONEPIN#")
        if await _fails_today(owner_id, user_id) >= DAY_TRIES:
            continue
        if hmac.compare_digest(_hash(pin, bytes.fromhex(row["salt"])), bytes.fromhex(row["hash"])):
            return user_id
        tried.append(user_id)
    for user_id in tried:
        await _count_fail(owner_id, user_id)
    return None
