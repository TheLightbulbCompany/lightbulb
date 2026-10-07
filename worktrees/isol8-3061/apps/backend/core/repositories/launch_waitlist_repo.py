"""launch-waitlist table — one row per email, plus a counter row. Post-pivot
this table IS the competition sign-up sheet: a row is sheet membership once
``joined_at`` is stamped (``mark_joined``, from the explicit POST
/launch/join), and the close action writes ``winner`` / ``compete_eligible``
onto the same rows.

Keyed by lowercased email (rows can predate sheet membership — referral
attribution creates them); the join to a Clerk user is ``users.email`` and is
stamped onto the row as ``user_id`` on the first authed read (``claim_user``).
"""

from __future__ import annotations

import secrets
from typing import Literal

from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

COUNTER_KEY = "__counter__"
REFERRAL_CODE_INDEX = "referral-code-index"
# Unambiguous alphabet (no 0/O, 1/I/L) — codes are read off screens and typed.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8

CounterField = Literal["referrals_signed_up", "referrals_subscribed"]


def _table():
    return get_table("launch-waitlist")


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 254:
        raise ValueError("invalid_email")
    return email


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def _plain(item: dict | None) -> dict | None:
    """Decimal → int for the counters DynamoDB hands back."""
    if not item:
        return None
    out = dict(item)
    for k in ("position", "referrals_signed_up", "referrals_subscribed"):
        if k in out:
            out[k] = int(out[k])
    return out


async def get(email: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"email": email})
    return _plain(resp.get("Item"))


async def get_by_referral_code(code: str) -> dict | None:
    from boto3.dynamodb.conditions import Key

    resp = await run_in_thread(
        _table().query,
        IndexName=REFERRAL_CODE_INDEX,
        KeyConditionExpression=Key("referral_code").eq(code),
        Limit=1,
    )
    items = resp.get("Items") or []
    return _plain(items[0]) if items else None


async def _next_position() -> int:
    resp = await run_in_thread(
        _table().update_item,
        Key={"email": COUNTER_KEY},
        UpdateExpression="ADD #p :one",
        ExpressionAttributeNames={"#p": "position"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["position"])


async def join(email: str, *, referred_by: str | None, source: str) -> dict:
    """Idempotent per email. A NEW row takes the next position and a fresh
    referral code; an existing row is returned untouched (first write wins,
    including ``source`` and ``referred_by``)."""
    email = normalize_email(email)
    existing = await get(email)
    if existing:
        return existing

    referrer = await get_by_referral_code(referred_by) if referred_by else None
    if referrer and referrer["email"] == email:
        referrer = None

    # ponytail: a same-email race increments the counter twice and leaves a
    # gap in positions. Harmless — positions are an ordering, not a count.
    position = await _next_position()
    item: dict = {
        "email": email,
        "position": position,
        "referral_code": _new_code(),
        "source": source,
        "created_at": utc_now_iso(),
        "referrals_signed_up": 0,
        "referrals_subscribed": 0,
    }
    if referrer:
        item["referred_by"] = referrer["referral_code"]
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(email)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return await get(email)  # lost the race — the other writer's row is the truth
    if referrer:
        await add_counter(referrer["email"], "referrals_signed_up", 1)
    return _plain(item)


async def claim_user(email: str, user_id: str) -> None:
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET user_id = :u, claimed_at = :t",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(user_id)",
            ExpressionAttributeValues={":u": user_id, ":t": utc_now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def attribute(email: str, referral_code: str) -> bool:
    """Set ``referred_by`` once. Returns True iff this call wrote it."""
    referrer = await get_by_referral_code(referral_code)
    if not referrer or referrer["email"] == email:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET referred_by = :c",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(referred_by)",
            ExpressionAttributeValues={":c": referral_code},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    await add_counter(referrer["email"], "referrals_signed_up", 1)
    return True


async def _stamp_once(email: str, field: str) -> bool:
    """CAS: set ``field`` to now exactly once per row. True iff this call wrote it."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression=f"SET {field} = :t",
            ConditionExpression=f"attribute_exists(email) AND attribute_not_exists({field})",
            ExpressionAttributeValues={":t": utc_now_iso()},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def mark_joined(email: str, *, display_name: str, public: bool) -> bool:
    """Sheet membership stamp + the join-time fields, ONE atomic write —
    first join wins wholesale, so a retry after a partial failure can never
    half-apply (e.g. re-default a private row's ``public`` back to true). A
    row can exist WITHOUT this (referral attribution creates rows); only
    stamped rows are on the sheet. True exactly once per row."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET joined_at = :t, display_name = :d, #pub = :p",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(joined_at)",
            ExpressionAttributeNames={"#pub": "public"},
            ExpressionAttributeValues={":t": utc_now_iso(), ":d": display_name, ":p": bool(public)},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def claim_winner_email(email: str) -> bool:
    """At-most-once claim for the close email — ANY variant (winner, still
    competing, not selected): stamp winner_email_sent_at BEFORE sending, so
    two concurrent close runs can never double-send. The field keeps its
    winner-era name deliberately: renaming it would orphan every row already
    stamped by a partial close. True iff this run owns the send; a failed
    send releases the claim (``release_winner_email``) so a force rerun
    retries."""
    return await _stamp_once(email, "winner_email_sent_at")


async def release_winner_email(email: str) -> None:
    """Undo a close-email claim whose send FAILED — the row goes back to
    unsent so a force rerun retries it."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="REMOVE winner_email_sent_at",
            ConditionExpression="attribute_exists(email)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def mark_referral_counted(email: str) -> bool:
    """Dedup for referral counting: True exactly once per referee row, the
    first time their subscription goes live (launch_referral)."""
    return await _stamp_once(email, "referral_counted_at")


async def update_fields(email: str, fields: dict) -> None:
    """SET the given fields on an EXISTING row (no-op if the row is gone).
    Used by the sheet join (display_name/public) and the close action
    (winner / compete_eligible / coupon + email stamps)."""
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)),
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def add_counter(email: str, field: CounterField, delta: int) -> int | None:
    """Atomic ADD on an EXISTING row. Returns the new value, or ``None`` when
    there is no row to add to."""
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="ADD #f :d",
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":d": delta},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"][field])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return None


async def list_all(*, consistent: bool = False) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {"ConsistentRead": True} if consistent else {}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        items.extend(_plain(i) for i in page.get("Items", []) if i.get("email") != COUNTER_KEY)
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


async def count() -> int:
    row = await run_in_thread(_table().get_item, Key={"email": COUNTER_KEY})
    return int((row.get("Item") or {}).get("position", 0))
