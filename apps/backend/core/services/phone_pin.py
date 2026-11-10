"""The owner's phone PIN (board row 3.29): a member who calls the business's own number from their saved mobile and
types their PIN on the keypad talks as themselves (the orb's owner mode), not as a caller.

Caller ID alone is never proof (it is spoofable): the number only says whose PIN to check. The PIN is stored as a
salted scrypt hash and compared in constant time. Wrong tries lock out per member per UTC day. Each try is reserved atomically before the hash is checked.
Nothing is ever said back about why a try failed.

Rows in the messaging table:
    OWNER#<owner>   PHONEPIN#<user>                 mobile (E.164), salt, hash, updatedAt
    OWNER#<owner>   PHONEPINFAIL#<user>#<YYYY-MM-DD> tries that day (expiresAt: two days on)
"""

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo

DAY_TRIES = 5  # PINs tried (right or wrong) for one member in a UTC day; then it stops working until tomorrow
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


async def _reserve(key: dict, limit: int) -> bool:
    """Take one try, atomically: the counter goes up only while it is under `limit`, so attempts at the same moment
    can never check more PINs than the limit. Every attempt counts, right or wrong."""
    try:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key=key,
            UpdateExpression="ADD tries :one SET expiresAt = :exp",
            ConditionExpression="attribute_not_exists(tries) OR tries < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": int(time.time()) + 2 * 86400},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def check(owner_id: str, caller: str, pin: str) -> str | None:
    """The member whose saved mobile is `caller` and whose PIN is `pin`, or None. Each member the number names gives
    up one of the day's tries before their hash is checked; one with none left is never checked."""
    for row in await holders(owner_id, caller):
        user_id = row["sk"].removeprefix("PHONEPIN#")
        if not await _reserve(_fail_key(owner_id, user_id), DAY_TRIES):
            continue
        if hmac.compare_digest(_hash(pin, bytes.fromhex(row["salt"])), bytes.fromhex(row["hash"])):
            return user_id
    return None
