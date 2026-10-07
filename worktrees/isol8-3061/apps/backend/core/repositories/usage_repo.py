"""Usage counter repository -- DynamoDB atomic counters for the usage-counters table."""

from decimal import Decimal

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread


def _get_table():
    return get_table("usage-counters")


async def increment(
    owner_id: str,
    period: str,
    spend_microdollars: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    return_new: bool = False,
) -> dict | None:
    """Atomically increment all counters for an owner+period.

    If *return_new* is True, returns the post-increment counter values so
    the caller can do an atomic overage check without a separate read.
    """
    table = _get_table()
    kwargs: dict = dict(
        Key={"owner_id": owner_id, "period": period},
        UpdateExpression=(
            "ADD total_spend_microdollars :spend, "
            "total_input_tokens :inp, "
            "total_output_tokens :out, "
            "total_cache_read_tokens :cr, "
            "total_cache_write_tokens :cw, "
            "request_count :one"
        ),
        ExpressionAttributeValues={
            ":spend": Decimal(str(spend_microdollars)),
            ":inp": Decimal(str(input_tokens)),
            ":out": Decimal(str(output_tokens)),
            ":cr": Decimal(str(cache_read_tokens)),
            ":cw": Decimal(str(cache_write_tokens)),
            ":one": Decimal("1"),
        },
    )
    if return_new:
        kwargs["ReturnValues"] = "ALL_NEW"

    response = await run_in_thread(table.update_item, **kwargs)

    if return_new:
        attrs = response.get("Attributes", {})
        return {
            "total_spend_microdollars": int(attrs.get("total_spend_microdollars", 0)),
            "total_input_tokens": int(attrs.get("total_input_tokens", 0)),
            "total_output_tokens": int(attrs.get("total_output_tokens", 0)),
            "total_cache_read_tokens": int(attrs.get("total_cache_read_tokens", 0)),
            "total_cache_write_tokens": int(attrs.get("total_cache_write_tokens", 0)),
            "request_count": int(attrs.get("request_count", 0)),
        }
    return None


async def get_period_usage(owner_id: str, period: str) -> dict | None:
    """Get usage counters for an owner+period. Returns None if no usage."""
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id, "period": period})
    item = response.get("Item")
    if item is None:
        return None
    return {
        "total_spend_microdollars": int(item.get("total_spend_microdollars", 0)),
        "total_input_tokens": int(item.get("total_input_tokens", 0)),
        "total_output_tokens": int(item.get("total_output_tokens", 0)),
        "total_cache_read_tokens": int(item.get("total_cache_read_tokens", 0)),
        "total_cache_write_tokens": int(item.get("total_cache_write_tokens", 0)),
        "request_count": int(item.get("request_count", 0)),
    }


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all usage-counter rows for an owner. Returns count deleted.

    Used by the e2e teardown endpoint. PK=owner_id, SK=period (which
    includes both period rollup rows and member:{user_id}:{period} rows).
    Paginates the DDB query so org members with many member:* rows beyond
    the 1MB query page boundary still get fully cleared.
    """
    table = _get_table()
    deleted = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "ProjectionExpression": "owner_id, #p",
            "ExpressionAttributeNames": {"#p": "period"},
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        for item in response.get("Items", []):
            await run_in_thread(
                table.delete_item,
                Key={"owner_id": item["owner_id"], "period": item["period"]},
            )
            deleted += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted


async def count_for_owner(owner_id: str) -> int:
    """Count usage-counter rows for an owner. Used by /debug/ddb-rows.

    PK=owner_id, SK=period — counts both rollup rows and per-member rows.
    Paginates so the count matches the actual row total even past the
    1MB query page boundary.
    """
    table = _get_table()
    total = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "Select": "COUNT",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        total += int(response.get("Count", 0))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return total


async def get_member_usage(owner_id: str, period: str) -> list[dict]:
    """Get per-member usage within an org for a period.

    Member records use SK pattern: member:{user_id}:{period}
    """
    table = _get_table()
    prefix = "member:"
    suffix = f":{period}"
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=(Key("owner_id").eq(owner_id) & Key("period").begins_with(prefix)),
    )
    results = []
    for item in response.get("Items", []):
        sk = item.get("period", "")
        if not sk.endswith(suffix):
            continue
        parts = sk.split(":")
        if len(parts) >= 3:
            user_id = parts[1]
            results.append(
                {
                    "user_id": user_id,
                    "total_spend_microdollars": int(item.get("total_spend_microdollars", 0)),
                    "total_input_tokens": int(item.get("total_input_tokens", 0)),
                    "total_output_tokens": int(item.get("total_output_tokens", 0)),
                    "request_count": int(item.get("request_count", 0)),
                }
            )
    return results
