"""ONE-OFF: stamp top-level ordinals on rooms written before exact unread counts.

DELETE this script and its test once it has run in dev AND prod (fix forward):
new rooms are born with ``META.topCount = 0`` and every message, join and read
cursor stamps its ordinal at write time, so nothing reads a legacy shape after
this has run once per environment. Until it runs, an unbackfilled room reads as
0 unread (a missing ordinal is 0 and the count clamps at 0) — never wrong-high,
never a crash.

Per room it recomputes, from the canonical ``MSG#`` sequence:
``META.topCount``, ``MSG.topOrdinal`` (a thread reply shares the ordinal of the
last top-level message before it), ``MEMBER.joinedTopOrdinal`` (from
``joinedSeq``) and ``READ.topOrdinal`` (from ``lastReadSeq``), writing only the
values that differ — so it is idempotent and resumable: re-run it until it
reports no ``raced`` rooms. ``META`` is conditioned on ``lastSeq`` and each
cursor/member on its own seq, so a message, read or join racing the walk
cancels that write and the room is redone.

Runs INSIDE the backend task (ECS exec), after the deploy that ships it.
Dry-run by default:

    uv run python -m scripts.backfill_room_ordinals            # report only
    uv run python -m scripts.backfill_room_ordinals --apply    # write
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo as repo

_ATTEMPTS = 3


async def _room_ids() -> list[str]:
    table = repo._table()
    kwargs: dict = {
        "FilterExpression": "begins_with(pk, :room) AND sk = :meta AND attribute_not_exists(deletedAt)",
        "ExpressionAttributeValues": {":room": "ROOM#", ":meta": "META"},
        "ProjectionExpression": "roomId",
    }
    ids: list[str] = []
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        ids.extend(item["roomId"] for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return ids
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def _update(pk: str, sk: str, update: str, condition: str, values: dict) -> None:
    await run_in_thread(
        repo._table().update_item,
        Key={"pk": pk, "sk": sk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )


async def backfill_room(room_id: str, *, apply: bool) -> str:
    """``ok`` (already exact) | ``would-fix`` | ``fixed`` | ``raced`` | ``skipped``."""
    pk = repo.room_pk(room_id)
    for _ in range(_ATTEMPTS):
        meta = await repo.get_room(room_id)
        if meta is None:
            return "skipped"
        last_seq = int(meta["lastSeq"])
        ordinal_at = {0: 0}
        count = 0
        msg_fixes: list[tuple[str, int]] = []
        for row in await repo._query(pk, begins_with="MSG#"):
            seq = int(row["messageSeq"])
            if seq > last_seq:
                continue  # landed after the META read; the retry picks it up
            if not row.get("threadRootMessageId"):
                count += 1
            ordinal_at[seq] = count
            if row.get("topOrdinal") != count:
                msg_fixes.append((row["sk"], count))
        cursor_fixes = [
            (r["sk"], "lastReadSeq", "topOrdinal", int(r["lastReadSeq"]))
            for r in await repo._query(pk, begins_with="READ#")
            if r.get("topOrdinal") != ordinal_at.get(int(r["lastReadSeq"]), count)
        ] + [
            (m["sk"], "joinedSeq", "joinedTopOrdinal", int(m.get("joinedSeq", 0)))
            for m in await repo._query(pk, begins_with="MEMBER#")
            if m.get("joinedTopOrdinal") != ordinal_at.get(int(m.get("joinedSeq", 0)), count)
        ]
        meta_fix = meta.get("topCount") != count
        if not (msg_fixes or cursor_fixes or meta_fix):
            return "ok"
        if not apply:
            return "would-fix"
        try:
            for sk, ordinal in msg_fixes:
                await _update(pk, sk, "SET topOrdinal = :o", "attribute_exists(sk)", {":o": ordinal})
            if meta_fix:
                await _update(pk, "META", "SET topCount = :c", "lastSeq = :seq", {":c": count, ":seq": last_seq})
            for sk, seq_field, ordinal_field, seq in cursor_fixes:
                await _update(
                    pk,
                    sk,
                    f"SET {ordinal_field} = :o",
                    f"{seq_field} = :seq",
                    {":o": ordinal_at.get(seq, count), ":seq": seq},
                )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            continue  # something moved under the walk: redo the room from fresh reads
        if await backfill_room(room_id, apply=False) == "ok":
            return "fixed"
    return "raced"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)
    outcomes: dict[str, int] = {}
    for room_id in await _room_ids():
        outcome = await backfill_room(room_id, apply=args.apply)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome != "ok":
            print(f"{room_id}: {outcome}")
    print(f"{'applied' if args.apply else 'dry run'}: {outcomes}")
    return 1 if outcomes.get("raced") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
