"""Admin-actions repository — DynamoDB CRUD for the audit table.

Every write under /api/v1/admin/* appends a row here via the
@audit_admin_action decorator. Reads power /admin/actions and the
per-target audit feed on /admin/users/{user_id}/actions.

Schema (created by apps/infra/lib/stacks/database-stack.ts):
- PK admin_user_id (Clerk user_id of the admin who took the action)
- SK timestamp_action_id ({ISO8601}#{ulid-like} for time-ordering)
- GSI target-timestamp-index (PK target_user_id, SK timestamp_action_id)
"""

import json
import time
import uuid
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("admin-actions")


def _generate_action_id() -> str:
    """ULID-like ID: timestamp-prefixed, sortable, unique.

    Same shape as update_repo._generate_ulid_like to keep the codebase
    consistent (avoids pulling in a uuid6/ulid dependency).
    """
    ts_ms = int(time.time() * 1000)
    ts_hex = f"{ts_ms:012x}"
    rand_hex = uuid.uuid4().hex[:20]
    return f"{ts_hex}{rand_hex}"


def _json_default(o: Any) -> Any:
    """Decimals stay numbers (parse_float turns them back into Decimal); datetimes and the rest become strings."""
    return float(o) if isinstance(o, Decimal) else str(o)


async def create(
    *,
    admin_user_id: str,
    target_user_id: str,
    action: str,
    payload: dict,
    result: str,
    audit_status: str,
    http_status: int,
    elapsed_ms: int,
    error_message: str | None,
    user_agent: str,
    ip: str,
) -> dict:
    """Append an audit row. Returns the persisted item (with generated SK).

    Called synchronously by @audit_admin_action *before* returning the
    response — see CEO review S1 (audit fail-closed). The decorator
    catches DDB errors and degrades to audit_status="panic" + a CRITICAL
    log entry; this function does not retry on its own.
    """
    table = _get_table()
    iso_ts = utc_now_iso()
    action_id = _generate_action_id()
    item: dict[str, Any] = {
        "admin_user_id": admin_user_id,
        "timestamp_action_id": f"{iso_ts}#{action_id}",
        "target_user_id": target_user_id,
        "action": action,
        # DynamoDB rejects float; a float in the payload (e.g. watchdog_ctl's config) would lose the row.
        "payload": json.loads(json.dumps(payload, default=_json_default), parse_float=Decimal),
        "result": result,
        "audit_status": audit_status,
        "http_status": http_status,
        "elapsed_ms": elapsed_ms,
        "user_agent": user_agent,
        "ip": ip,
    }
    if error_message:
        item["error_message"] = error_message
    await run_in_thread(table.put_item, Item=item)
    return item


async def query_by_target(
    target_user_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Newest-first via the target-timestamp-index GSI.

    Returns {items: list[dict], cursor: str | None}.
    cursor is the LastEvaluatedKey's timestamp_action_id, opaque to callers.
    """
    table = _get_table()
    kwargs: dict[str, Any] = {
        "IndexName": "target-timestamp-index",
        "KeyConditionExpression": Key("target_user_id").eq(target_user_id),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = {
            "target_user_id": target_user_id,
            "timestamp_action_id": cursor,
        }
    response = await run_in_thread(table.query, **kwargs)
    last = response.get("LastEvaluatedKey")
    return {
        "items": response.get("Items", []),
        "cursor": last.get("timestamp_action_id") if last else None,
    }


async def scan_recent(
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Newest-first across ALL admins/targets — the unfiltered audit view.

    The table has no global time-ordered index (PK is per-admin, the GSI is
    per-target), so a global newest-first listing scans every row and sorts in
    memory. The cursor is a decimal offset into that sorted list.

    # ponytail: full scan + in-memory sort. Fine for the low-volume audit table
    # (hundreds of rows). If it ever grows past a few thousand, add a
    # fixed-partition GSI (constant PK, timestamp SK) and query that instead.
    """
    table = _get_table()
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = await run_in_thread(table.scan, **scan_kwargs)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    items.sort(key=lambda i: i.get("timestamp_action_id", ""), reverse=True)

    offset = int(cursor) if cursor and cursor.isdigit() else 0
    page = items[offset : offset + limit]
    next_offset = offset + limit
    return {
        "items": page,
        "cursor": str(next_offset) if next_offset < len(items) else None,
    }


async def query_by_admin(
    admin_user_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Newest-first via the base table's PK/SK."""
    table = _get_table()
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("admin_user_id").eq(admin_user_id),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = {
            "admin_user_id": admin_user_id,
            "timestamp_action_id": cursor,
        }
    response = await run_in_thread(table.query, **kwargs)
    last = response.get("LastEvaluatedKey")
    return {
        "items": response.get("Items", []),
        "cursor": last.get("timestamp_action_id") if last else None,
    }
