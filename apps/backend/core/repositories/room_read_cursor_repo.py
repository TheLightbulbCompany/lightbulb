"""DynamoDB persistence for per-human native-room read cursors.

Rows contain product read state only. OpenClaw remains the canonical owner of
room messages, authorship, search, and transcript sequence allocation.
"""

import hashlib
import json
from decimal import Decimal

from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("room-read-cursors")


def _cursor_key(profile_id: str, room_key: str) -> str:
    encoded = json.dumps(
        [profile_id, room_key], ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _project(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {
        "owner_id": item["owner_id"],
        "profile_id": item["profile_id"],
        "room_key": item["room_key"],
        "last_read_message_seq": int(item["last_read_message_seq"]),
        "updated_at": item["updated_at"],
    }


async def get(owner_id: str, profile_id: str, room_key: str) -> dict | None:
    response = await run_in_thread(
        _get_table().get_item,
        Key={"owner_id": owner_id, "cursor_key": _cursor_key(profile_id, room_key)},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is not None and (
        item.get("profile_id") != profile_id or item.get("room_key") != room_key
    ):
        raise RuntimeError("Room cursor key collision")
    return _project(item)


async def advance(
    owner_id: str,
    profile_id: str,
    room_key: str,
    message_seq: int,
) -> dict:
    """Atomically advance a cursor, returning the current row on regression."""
    if isinstance(message_seq, bool) or not isinstance(message_seq, int) or message_seq < 1:
        raise ValueError("message_seq must be a positive integer")
    key = {"owner_id": owner_id, "cursor_key": _cursor_key(profile_id, room_key)}
    for _ in range(3):
        try:
            response = await run_in_thread(
                _get_table().update_item,
                Key=key,
                UpdateExpression=(
                    "SET profile_id = if_not_exists(profile_id, :profile), "
                    "room_key = if_not_exists(room_key, :room), "
                    "last_read_message_seq = :seq, updated_at = :updated"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(profile_id) OR profile_id = :profile) AND "
                    "(attribute_not_exists(room_key) OR room_key = :room) AND "
                    "(attribute_not_exists(last_read_message_seq) OR last_read_message_seq < :seq)"
                ),
                ExpressionAttributeValues={
                    ":profile": profile_id,
                    ":room": room_key,
                    ":seq": Decimal(message_seq),
                    ":updated": utc_now_iso(),
                },
                ReturnValues="ALL_NEW",
            )
            return {**_project(response["Attributes"]), "advanced": True}
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        current = await get(owner_id, profile_id, room_key)
        if current is not None:
            return {**current, "advanced": False}
        # The row disappeared between condition failure and consistent read.
    raise RuntimeError("Room cursor changed repeatedly while advancing")


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete cursor metadata during an owner/account wipe."""
    table = _get_table()
    deleted = 0
    last_key = None
    while True:
        kwargs = {
            "KeyConditionExpression": "owner_id = :owner",
            "ExpressionAttributeValues": {":owner": owner_id},
            "ProjectionExpression": "owner_id, cursor_key",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        items = response.get("Items", [])

        def _delete_page():
            with table.batch_writer() as writer:
                for item in items:
                    writer.delete_item(
                        Key={"owner_id": item["owner_id"], "cursor_key": item["cursor_key"]}
                    )

        if items:
            await run_in_thread(_delete_page)
            deleted += len(items)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted
