"""User repository -- DynamoDB operations for the users table."""

import logging

from core.dynamodb import get_table, run_in_thread, utc_now_iso

logger = logging.getLogger(__name__)


def _get_table():
    return get_table("users")


async def get(user_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"user_id": user_id})
    return response.get("Item")


async def put(user_id: str, email: str | None = None) -> dict:
    """Persist a users-table row.

    ``email`` is optional; the Clerk ``user.created`` webhook supplies it.
    Writing an empty string would shadow Clerk's actual primary email
    in any future upsert, so we omit the attribute entirely when the
    caller has nothing to record.

    An UpdateItem that keeps every other attribute: a redelivered Clerk
    ``user.created`` must not drop the Spark account.
    """
    table = _get_table()
    sets, values = ["created_at = if_not_exists(created_at, :c)"], {":c": utc_now_iso()}
    if email:
        sets.append("email = :e")
        values[":e"] = email
    got = await run_in_thread(
        table.update_item,
        Key={"user_id": user_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return got["Attributes"]


async def delete(user_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"user_id": user_id})
