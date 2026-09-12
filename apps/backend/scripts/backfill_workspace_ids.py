"""ONE-SHOT: stamp workspaceId onto every ROOM#/META and STREAM#/META row that lacks it.

Run once per environment before that environment's cutover deploy, then DELETE this
file (CLAUDE.md: one-shot backfills do not accumulate; git history is the record).

Dry run by default; ``--apply`` writes.

    cd apps/backend && AWS_PROFILE=isol8-admin MESSAGING_TABLE=lightbulb-dev-messaging \
        uv run python scripts/backfill_workspace_ids.py
    cd apps/backend && AWS_PROFILE=isol8-admin MESSAGING_TABLE=lightbulb-dev-messaging \
        uv run python scripts/backfill_workspace_ids.py --apply
"""

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from botocore.exceptions import ClientError  # noqa: E402

from core.dynamodb import run_in_thread  # noqa: E402
from core.repositories import messaging_repo as repo  # noqa: E402


async def main(apply: bool) -> int:
    table = repo._table()
    kwargs = {
        "FilterExpression": "sk = :meta AND attribute_not_exists(workspaceId) AND (begins_with(pk, :room) OR begins_with(pk, :stream))",
        "ExpressionAttributeValues": {":meta": "META", ":room": "ROOM#", ":stream": "STREAM#"},
        "ProjectionExpression": "pk, sk, ownerId",
    }
    stamped = 0
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        for row in page.get("Items", []):
            workspace_id = await repo.ensure_workspace_id(row["ownerId"])
            if apply:
                try:
                    await run_in_thread(
                        table.update_item,
                        Key={"pk": row["pk"], "sk": row["sk"]},
                        UpdateExpression="SET workspaceId = :ws",
                        ConditionExpression="attribute_not_exists(workspaceId)",
                        ExpressionAttributeValues={":ws": workspace_id},
                    )
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
            stamped += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if apply:
        print(f"stamped {stamped} rows")
    else:
        print(f"would stamp {stamped} rows — dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run)")
    raise SystemExit(asyncio.run(main(parser.parse_args().apply)))
