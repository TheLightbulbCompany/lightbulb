"""Container repository -- DynamoDB operations for the containers table."""

import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("containers")


async def get_by_owner_id(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id})
    return response.get("Item")


async def get_by_gateway_token(token: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="gateway-token-index",
        KeyConditionExpression=Key("gateway_token").eq(token),
    )
    items = response.get("Items", [])
    return items[0] if items else None


async def get_by_status(status: str) -> list[dict]:
    """Return every container row with the given status.

    Follows DynamoDB's LastEvaluatedKey. Query responses are capped at 1 MB,
    so a single call only returns the first page — the reaper (and any other
    caller that iterates the full set for correctness) needs every row.
    """
    table = _get_table()
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs = {
            "IndexName": "status-index",
            "KeyConditionExpression": Key("status").eq(status),
        }
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = await run_in_thread(table.query, **kwargs)
        items.extend(response.get("Items", []))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def scan_all() -> list[dict]:
    """Return every container row. Paginates via DDB LastEvaluatedKey.

    Used by the one-shot hook-token backfill (scripts/backfill-hook-tokens.py).
    Not for hot paths -- DDB Scan is expensive at scale.
    """
    table = _get_table()
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = await run_in_thread(table.scan, **kwargs)
        items.extend(response.get("Items", []))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def upsert(owner_id: str, fields: dict) -> dict:
    """Create or update a container record. Preserves id and created_at on existing items."""
    table = _get_table()
    existing = await get_by_owner_id(owner_id)

    now = utc_now_iso()
    item = {
        "owner_id": owner_id,
        "id": existing["id"] if existing else str(uuid.uuid4()),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **fields,
    }
    # Ensure owner_id is not overridden by fields
    item["owner_id"] = owner_id

    await run_in_thread(table.put_item, Item=item)
    return item


async def update_status(owner_id: str, status: str, substatus: str | None = None) -> dict | None:
    fields = {"status": status, "substatus": substatus}
    return await update_fields(owner_id, fields)


async def update_fields(owner_id: str, fields: dict) -> dict | None:
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    existing.update(fields)
    existing["updated_at"] = utc_now_iso()

    table = _get_table()
    await run_in_thread(table.put_item, Item=existing)
    return existing


async def upsert_paired_devices(owner_id: str, entries: dict) -> None:
    """Project desktop device-pubkey entries onto the owner row's ``paired_devices`` map.

    ``entries`` is ``{deviceId: {"publicKey": <b64url-raw>, "userId": <member>,
    "clientId": <str>}}``. This is the authorizer-reachable projection of the
    device pubkeys that also live in the container's EFS ``paired.json`` — it
    gives the WebSocket Lambda authorizer a ``GetItem(containers, owner)`` point
    lookup for device-key WS-upgrade auth, without a separate table.

    Each device is written with a SCOPED, server-atomic ``UpdateItem`` (touching
    only its one map key) rather than a full-item read-modify-write — so a
    concurrent status/hook-token/config writer, or a concurrent write of another
    device, cannot clobber it or resurrect a revoked device. DynamoDB serializes
    item-level UpdateItems.

    NON-RELABEL guard (atomic): ``deviceId = sha256(pubkey)`` and POST /desktop/pair
    does NOT verify private-key possession, so a co-tenant could submit another
    member's PUBLIC key (freely observable) and collide their deviceId. The
    ``ConditionExpression`` only writes when the device is absent OR already owned
    by the SAME member — so one org member can never relabel another's device on
    the authorizer's trust anchor (mirrors the paired.json guard in desktop_pair.py).
    No-op if the container row doesn't exist.
    """
    table = _get_table()
    for device_id, entry in entries.items():
        await _put_paired_device(table, owner_id, device_id, entry)


async def _put_paired_device(table, owner_id: str, device_id: str, entry: dict) -> None:
    # 1. Ensure the paired_devices map exists — a nested-path SET below fails if
    #    its parent map is absent. Idempotent; only touches an existing row.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #pd = if_not_exists(#pd, :empty)",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames={"#pd": "paired_devices"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no container row for this owner — nothing to project onto
        raise
    # 2. Scoped conditional set of the single map key. Condition = device absent
    #    OR already ours -> refuse to relabel another member's device.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #pd.#did = :entry",
            ConditionExpression="attribute_not_exists(#pd.#did) OR #pd.#did.#uid = :uid",
            ExpressionAttributeNames={
                "#pd": "paired_devices",
                "#did": device_id,
                "#uid": "userId",
            },
            ExpressionAttributeValues={":entry": entry, ":uid": entry.get("userId")},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # deviceId already owned by a different member — do not relabel
        raise


async def remove_paired_device(owner_id: str, device_id: str) -> None:
    """Remove a device from the owner row's ``paired_devices`` map (revocation).

    Scoped ``REMOVE`` of the single map key so a racing writer of other row
    attributes cannot resurrect the revoked entry. No-op if the row is absent.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE #pd.#did",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames={"#pd": "paired_devices", "#did": device_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no container row — nothing to remove
        raise


async def set_hook_token(owner_id: str, token: str) -> dict | None:
    """Persist a freshly generated OpenClaw hook token onto the containers row.

    Sets ``hook_token`` and ``hook_token_rotated_at`` together so the rotation
    timestamp always tracks the current token. Returns the updated row, or
    None if the row does not exist.
    """
    return await update_fields(
        owner_id,
        {"hook_token": token, "hook_token_rotated_at": utc_now_iso()},
    )


async def mark_hook_token_provisioned(owner_id: str) -> dict | None:
    """Stamp ``hook_token_provisioned_at`` after the full backfill sequence
    (set_hook_token → refresh_task_definition → wait → patch_openclaw_config)
    has succeeded for this user.

    The backfill orchestrator's idempotency guard tests this field, not
    ``hook_token``, so a crash mid-sequence leaves the user retryable.
    """
    return await update_fields(
        owner_id,
        {"hook_token_provisioned_at": utc_now_iso()},
    )


async def update_error(owner_id: str, error: str) -> dict | None:
    """Record the last error for a container."""
    return await update_fields(
        owner_id,
        {
            "last_error": error,
            "last_error_at": utc_now_iso(),
        },
    )


async def delete(owner_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"owner_id": owner_id})
