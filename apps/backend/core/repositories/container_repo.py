"""Container repository -- DynamoDB operations for the containers table."""

import uuid

from boto3.dynamodb.conditions import Key

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


async def update_last_active(owner_id: str, iso_ts: str) -> bool:
    """Record the last user-activity timestamp on a running container.

    Conditional update: only writes if the row exists AND status != "stopped".
    Returns True if the row was mutated, False if the condition failed (row
    missing or status=stopped). Callers need to know whether the write
    actually landed so they don't lock out the next retry during cold-start
    transitions where the row is still flipping from "stopped" to "running".
    """
    from botocore.exceptions import ClientError

    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET last_active_at = :t, updated_at = :u",
            ConditionExpression="attribute_exists(owner_id) AND #s <> :stopped",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":t": iso_ts,
                ":u": utc_now_iso(),
                ":stopped": "stopped",
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def mark_stopped_if_running(owner_id: str) -> bool:
    """Flip status from "running" to "stopped" atomically.

    Returns True if this call performed the transition, False if the row
    was already stopped, missing, or in some other state. Safe for multiple
    concurrent reapers.
    """
    from botocore.exceptions import ClientError

    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #s = :stopped, updated_at = :u",
            ConditionExpression="attribute_exists(owner_id) AND #s = :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":stopped": "stopped",
                ":running": "running",
                ":u": utc_now_iso(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def delete(owner_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"owner_id": owner_id})
