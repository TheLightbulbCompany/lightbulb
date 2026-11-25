"""Container repository -- the containers table, which now holds Spark's voice-session rows (core.voice.os_voice)."""

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread


def _get_table():
    return get_table("containers")


async def get_by_status(status: str) -> list[dict]:
    """Return every row with the given status from the sparse ``status-index`` GSI.

    Follows DynamoDB's LastEvaluatedKey. Query responses are capped at 1 MB,
    so a single call only returns the first page.
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
