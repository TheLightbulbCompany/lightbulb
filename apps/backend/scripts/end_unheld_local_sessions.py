"""One-off: end every live local session that is not running in a Lightbulb holder.

Managed session sharing, Phase 3: a session is shared only when Lightbulb started it (the
desktop app, a remote start or pick-up, or `lightbulb claude`), and `local.session.register`
now requires `holderId`. Rows registered before that carry none; this ends the live ones the
way an Ended frame does, which also starts their pick-up retention. An old desktop then has
its heartbeats rejected and its re-registrations refused.

Runs INSIDE the backend task (ECS exec), once per env, after the backend deploy that requires
holderId (like ``delete_per_agent_mail_rows``):

    uv run python -m scripts.end_unheld_local_sessions            # dry run: lists, ends nothing
    uv run python -m scripts.end_unheld_local_sessions --apply

DELETE THIS SCRIPT and its test once it has run with ``--apply`` in dev and prod.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from core.repositories import messaging_repo as store
from core.services import local_sessions as sessions
from core.services.local_session_relay import _capture_lifecycle, _revised, publish_resources_changed


async def unheld() -> list[dict]:
    table = store._table()
    kwargs = {
        "FilterExpression": Attr("pk").begins_with("LOCALSESSION#")
        & Attr("sk").eq("META")
        & Attr("endedAt").not_exists()
        & Attr("holderId").not_exists(),
        "ConsistentRead": True,
    }
    rows: list[dict] = []
    while True:
        response = await store.run_in_thread(table.scan, **kwargs)
        rows += response.get("Items", [])
        if not response.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


async def end(row: dict, now: int) -> dict:
    """What `LocalSessionRelay._ended` writes, fenced by the row's revision."""
    updated = _revised(row, status="ended", endedAt=now, lastEventAt=now, endedReason="exited")
    for key in ("pendingApproval", "privateSendApproval", "teamControl", *sessions.JOIN_FIELDS):
        updated.pop(key, None)
    table = store._table().name
    items = [
        {
            "Put": {
                "TableName": table,
                "Item": updated,
                "ConditionExpression": "revision = :revision",
                "ExpressionAttributeValues": {":revision": row["revision"]},
            }
        }
    ]
    if row.get("teamControl"):
        audit = sessions.audit_row(row["ownerId"], row["sessionId"], None, "team_control_ended")
        items.append({"Put": {"TableName": table, "Item": audit}})
    await store._transact(items)
    return updated


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="end them (default: dry run)")
    args = parser.parse_args(argv)
    now, ended = int(time.time()), 0
    for row in await unheld():
        print(f"{row['ownerId']} {row['sessionId']} {row['provider']} {row.get('title') or ''}")
        if not args.apply:
            continue
        try:
            updated = await end(row, now)
        except ClientError as exc:  # a concurrent write; a re-run picks the row up again
            print(f"  skipped: {exc.response['Error']['Code']}")
            continue
        _capture_lifecycle(updated, "local_session_ended", end_reason="exited")
        await publish_resources_changed(row["ownerId"], row["humanUserId"])
        ended += 1
    print(f"{ended} session(s) ended" if args.apply else "dry run; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
