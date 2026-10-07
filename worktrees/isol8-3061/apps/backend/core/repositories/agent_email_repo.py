"""Address-book for provisioned agent email.

One row per provisioned agent email address. The address is the partition
key because inbound mail is routed by recipient; a ``by-owner`` GSI lists an
owner's addresses.

``status`` is ``"active"`` for the current primary address and ``"alias"``
for a superseded one that must keep routing — so an owner can accumulate
several rows over time and readers must select deliberately.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("agent-email-addresses")


def normalize(value: str) -> str:
    """Addresses and sender emails are matched case-insensitively."""
    return value.strip().lower()


async def put(
    *,
    address: str,
    owner_id: str,
    agent_id: str,
    verified_senders: list[str],
) -> dict | None:
    """Claim ``address`` for ``owner_id`` — a CREATE primitive, not an update.

    This is a conditional put mirroring ``company_slug_repo.reserve``:
    ``attribute_not_exists(address) OR owner_id = :owner`` — so a concurrent
    or reordered write can never overwrite another owner's row. Returns the
    written item, or ``None`` when another owner already holds ``address``
    (on ``ConditionalCheckFailedException`` — never raises for that case).

    It ALWAYS writes ``status="active"`` and a fresh ``created_at``, even
    when re-affirming a row this owner already holds. There is deliberately
    no update primitive here: a future rename/alias flow that needs to demote
    a row to ``status="alias"`` without resetting ``created_at`` (the
    deterministic tie-break `owner_facts._lookup_email_address` sorts on)
    must add a dedicated update primitive rather than calling this.
    """
    item = {
        "address": normalize(address),
        "owner_id": owner_id,
        "agent_id": agent_id,
        "verified_senders": [normalize(s) for s in verified_senders],
        "status": "active",
        "created_at": utc_now_iso(),
    }
    table = _get_table()
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(address) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def get_by_address(address: str) -> dict | None:
    """Strongly consistent — this is the mint-time idempotency guard
    (`agent_email.mint_agent_email_address` calls it before `put`). An
    eventually-consistent miss right after a same-address write would let
    `put` "win" a race it should have folded into, overwriting the existing
    row and resetting its `created_at` — which flips the deterministic
    earliest-`created_at` tie-break `owner_facts._lookup_email_address`
    sorts on. Base-table GetItem supports `ConsistentRead`; the `by-owner`
    GSI in `query_by_owner` below does not, so that one stays eventual."""
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"address": normalize(address)}, ConsistentRead=True)
    return response.get("Item")


async def query_by_owner(owner_id: str) -> list[dict]:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Delete every agent email address owned by this owner. Returns them.

    The wipe used to skip this, so a deleted owner's addresses stayed in the
    table and inbound SES mail to them routed at an owner that no longer
    exists. Reads through the by-owner GSI (eventually consistent — a write
    from the same second could be missed, which is acceptable for a teardown
    that runs after the owner's surfaces are already gone).
    """
    rows = await query_by_owner(owner_id)
    table = _get_table()
    addresses: list[str] = []
    for row in rows:
        address = row.get("address")
        if not address:
            continue
        await run_in_thread(table.delete_item, Key={"address": address})
        addresses.append(address)
    return addresses


async def scan_all() -> list[dict]:
    """Every row in the table, paginated.

    Only the orphan sweep uses this — a full scan is the ONLY way to find rows
    whose owner no longer exists, because the owner id is gone from everywhere
    that would let you query for it. Not for request-path use.
    """
    table = _get_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


_UNSET = object()


async def update_settings(
    address: str,
    *,
    verified_senders: list[str] | None = None,
    send_to_anyone: bool | None = None,
    outbound_disabled: str | None | object = _UNSET,
) -> dict | None:
    """SET the supplied owner-editable fields on an existing row; None for
    ``outbound_disabled`` REMOVEs it. Returns the updated item, or None when
    the row does not exist (conditional on ``attribute_exists(address)``)."""
    sets: list[str] = []
    removes: list[str] = []
    values: dict = {}
    if verified_senders is not None:
        sets.append("verified_senders = :vs")
        values[":vs"] = [normalize(s) for s in verified_senders]
    if send_to_anyone is not None:
        sets.append("send_to_anyone = :sta")
        values[":sta"] = bool(send_to_anyone)
    if outbound_disabled is not _UNSET:
        if outbound_disabled is None:
            removes.append("outbound_disabled")
        else:
            sets.append("outbound_disabled = :od")
            values[":od"] = str(outbound_disabled)
    if not sets and not removes:
        return await get_by_address(address)
    expr = ""
    if sets:
        expr += "SET " + ", ".join(sets)
    if removes:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(removes)
    kwargs = {
        "Key": {"address": normalize(address)},
        "UpdateExpression": expr,
        "ConditionExpression": "attribute_exists(address)",
        "ReturnValues": "ALL_NEW",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        resp = await run_in_thread(_get_table().update_item, **kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")
