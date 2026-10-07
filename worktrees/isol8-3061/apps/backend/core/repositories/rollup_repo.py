"""Rollup day-row repository on the usage-counters table.

Day rollups live on the existing usage-counters table under the SK namespace
``day:{YYYY-MM-DD}``, beside the existing ``"%Y-%m"`` month, ``"lifetime"``,
and ``"member:*"`` values — no new table, no new IAM.

Writes are whole-row SET replacement (not ADD): every writer recomputes the
day from provider truth (Stripe/PostHog/etc.), so the row is a snapshot to
overwrite, never a counter to increment. Money attributes are microcents.
"""

from decimal import Decimal

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread

_SK_PREFIX = "day:"

VALUE_KEYS = (
    "revenue_microcents",
    "visitors",
    "signups",
    "followers",
    "open_threads",
    "agent_spend_microcents",
)


def _get_table():
    return get_table("usage-counters")


async def put_day(owner_id: str, day: str, values: dict[str, int]) -> None:
    """Overwrite the rollup row for owner+day. *day* is ``YYYY-MM-DD``."""
    table = _get_table()
    item = {"owner_id": owner_id, "period": f"{_SK_PREFIX}{day}"}
    for key in VALUE_KEYS:
        item[key] = Decimal(str(values[key]))
    await run_in_thread(table.put_item, Item=item)


async def get_days(owner_id: str, start_day: str, end_day: str) -> list[dict]:
    """Rollup rows for owner between start_day and end_day inclusive, ascending."""
    table = _get_table()
    rows: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "KeyConditionExpression": (
                Key("owner_id").eq(owner_id)
                & Key("period").between(f"{_SK_PREFIX}{start_day}", f"{_SK_PREFIX}{end_day}")
            ),
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        for item in response.get("Items", []):
            row = {"day": item["period"][len(_SK_PREFIX) :]}
            for key in VALUE_KEYS:
                row[key] = int(item.get(key, 0))
            rows.append(row)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return rows


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all ``day:`` rollup rows for an owner. Returns count deleted.

    Mirrors usage_repo.delete_all_for_owner but scoped to the day: namespace,
    so month/lifetime/member counter rows are untouched.
    """
    table = _get_table()
    deleted = 0
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "KeyConditionExpression": (Key("owner_id").eq(owner_id) & Key("period").begins_with(_SK_PREFIX)),
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
