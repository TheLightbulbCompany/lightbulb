"""One row per app, keyed by ``app_id``.

Each app gets its OWN Stripe Project (``stripe_project_id``), so its
credentials never mix with the company's or another app's.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from core.dynamodb import ddb_safe, get_table, run_in_thread, utc_now_iso

_PROTECTED_FIELDS = frozenset({"app_id", "owner_id", "created_at"})


def _table():
    return get_table("apps")


async def get(app_id: str, *, consistent_read: bool = False) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"app_id": app_id}, ConsistentRead=consistent_read)
    return resp.get("Item")


async def patch(app_id: str, fields: dict) -> dict:
    sets = ["updated_at = :now"]
    names: dict[str, str] = {}
    values: dict = {":now": utc_now_iso()}
    for i, (key, value) in enumerate(sorted(fields.items())):
        if key in _PROTECTED_FIELDS:
            raise ValueError(f"{key} cannot be set through patch")
        sets.append(f"#f{i} = :f{i}")
        names[f"#f{i}"] = key
        values[f":f{i}"] = ddb_safe(value)
    kwargs = {"ExpressionAttributeNames": names} if names else {}
    resp = await run_in_thread(
        _table().update_item,
        Key={"app_id": app_id},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression="attribute_exists(app_id)",
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
        **kwargs,
    )
    return resp.get("Attributes")


async def set_stripe_project_id(app_id: str, project_id: str) -> bool:
    """Record the app's Project, exactly once. ``False`` = no app row, or
    another worker stored first; the caller re-reads and uses the winner's."""
    if not project_id:
        raise ValueError("project_id must be non-empty")
    try:
        await run_in_thread(
            _table().update_item,
            Key={"app_id": app_id},
            UpdateExpression="SET stripe_project_id = :p, updated_at = :now",
            ConditionExpression="attribute_exists(app_id) AND "
            "(attribute_not_exists(stripe_project_id) OR attribute_type(stripe_project_id, :null_t))",
            ExpressionAttributeValues={":p": project_id, ":now": utc_now_iso(), ":null_t": "NULL"},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True
