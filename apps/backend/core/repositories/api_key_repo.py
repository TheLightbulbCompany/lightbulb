"""API key repository -- DynamoDB operations for the api-keys table.

Composite key: PK=user_id, SK=tool_id.
"""

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("api-keys")


async def get_key(user_id: str, tool_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.get_item,
        Key={"user_id": user_id, "tool_id": tool_id},
    )
    return response.get("Item")


async def set_key(user_id: str, tool_id: str, encrypted_key: str) -> dict:
    table = _get_table()
    now = utc_now_iso()
    item = {
        "user_id": user_id,
        "tool_id": tool_id,
        "encrypted_key": encrypted_key,
        "created_at": now,
        "updated_at": now,
    }
    await run_in_thread(table.put_item, Item=item)
    return item


async def list_keys(user_id: str) -> list[dict]:
    """List all keys for a user, excluding encrypted_key from results."""
    table = _get_table()
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("user_id").eq(user_id),
        ProjectionExpression="user_id, tool_id, created_at, updated_at",
    )
    return response.get("Items", [])


async def set_secret_arn(user_id: str, tool_id: str, secret_arn: str) -> None:
    """Persist the AWS Secrets Manager ARN alongside an existing key row.

    Used by ``key_service`` for LLM-provider keys (OpenAI/Anthropic) where the
    plaintext is mirrored into Secrets Manager so the per-user ECS task can
    reference it via ``secrets:[{name, valueFrom}]``.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"user_id": user_id, "tool_id": tool_id},
        UpdateExpression="SET secret_arn = :a, updated_at = :t",
        ExpressionAttributeValues={
            ":a": secret_arn,
            ":t": utc_now_iso(),
        },
    )


async def delete_key(user_id: str, tool_id: str) -> bool:
    """Delete a key. Returns True if the key existed, False otherwise."""
    existing = await get_key(user_id, tool_id)
    if existing is None:
        return False

    table = _get_table()
    await run_in_thread(
        table.delete_item,
        Key={"user_id": user_id, "tool_id": tool_id},
    )
    return True


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all API key rows for an owner. Returns count deleted.

    Used by the e2e teardown endpoint. The api-keys table uses user_id
    as the partition key (legacy naming), so owner_id maps to user_id.
    Paginates the DDB query so multi-page results don't leak.
    """
    table = _get_table()
    deleted = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("user_id").eq(owner_id),
            "ProjectionExpression": "user_id, tool_id",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        for item in response.get("Items", []):
            await run_in_thread(
                table.delete_item,
                Key={"user_id": item["user_id"], "tool_id": item["tool_id"]},
            )
            deleted += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted


async def count_for_owner(owner_id: str) -> int:
    """Count API key rows for an owner. Used by /debug/ddb-rows.

    PK is ``user_id`` on this table (legacy naming) — owner_id maps to it.
    Paginates so the count matches reality past the 1MB query boundary.
    """
    table = _get_table()
    total = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("user_id").eq(owner_id),
            "Select": "COUNT",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        total += int(response.get("Count", 0))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return total
