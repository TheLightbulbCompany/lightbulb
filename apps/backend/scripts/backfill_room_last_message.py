"""ONE-OFF: stamp ``META.lastMessage`` on rooms created before the room-list preview.

New rooms are born with ``lastMessage: null`` and every top-level message rewrites
it inside the message transaction, so after this has run once per environment the
field is present on every room and the code carries no legacy read path. Delete
this script and its test once it has run in dev and prod (fix forward).

Runs INSIDE the backend task (ECS exec), right after the deploy that ships PR 1:

    uv run python -m scripts.backfill_room_last_message [--dry-run]

Each room is written only while ``lastMessage`` is still absent and ``lastSeq`` is
unchanged, and the chosen message's revision is fenced in the same transaction,
so a message, edit or delete racing the backfill cancels it; the room is retried
(the new message's own write usually wins, which is also correct). Safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo as repo


async def _legacy_rooms() -> list[dict]:
    table = repo._table()
    kwargs: dict = {
        "FilterExpression": "begins_with(pk, :room) AND sk = :meta AND attribute_not_exists(lastMessage)",
        "ExpressionAttributeValues": {":room": "ROOM#", ":meta": "META"},
        "ProjectionExpression": "roomId",
    }
    rooms: list[dict] = []
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        rooms.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return rooms
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def backfill_room(room_id: str) -> str:
    for _ in range(3):
        meta = await repo.get_room(room_id)
        if meta is None or "lastMessage" in meta:
            return "skipped"
        last_seq = int(meta["lastSeq"])
        # Unbounded walk back (the live delete path looks 50 rows back): a one-off.
        row = await repo.latest_preview_row(room_id, last_seq + 1, limit=None)
        table_name = repo._table().name
        items = [
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": repo.room_pk(room_id), "sk": "META"},
                    "UpdateExpression": "SET lastMessage = :preview",
                    "ConditionExpression": "lastSeq = :seq AND attribute_not_exists(lastMessage)",
                    "ExpressionAttributeValues": {
                        ":seq": last_seq,
                        ":preview": repo.last_message_preview(row) if row else None,
                    },
                }
            }
        ]
        if row:
            items.append(repo._unchanged_message_check(table_name, room_id, row))
        try:
            await repo._transact(items)
            return "stamped"
        except ClientError as exc:
            if not repo._is_cancelled(exc):
                raise
    return "raced"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    raced = 0
    for room in await _legacy_rooms():
        outcome = "would-stamp" if args.dry_run else await backfill_room(room["roomId"])
        raced += outcome == "raced"
        print(f"{room['roomId']}: {outcome}")
    return 1 if raced else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
