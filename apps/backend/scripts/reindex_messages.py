"""Re-enqueue messages for the search indexer (backfill, or rebuild an index).

Re-runnable: the indexer re-reads each row, so repeating a run is harmless.

    aws sqs get-queue-url --queue-name lightbulb-<env>-message-search-index --profile isol8-admin
    uv run python scripts/reindex_messages.py --env dev --queue-url <url> [--owner org_x]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable

import boto3
from boto3.dynamodb.conditions import Attr


def jobs_from_rows(rows: Iterable[dict], owner_of: Callable[[str], str | None]) -> list[dict]:
    """``owner_of`` resolves a room's CURRENT owner from its META row: the MSG row's
    own ownerId is stale for rooms the #2627 migration moved."""
    jobs = []
    for row in rows:
        if not str(row.get("sk", "")).startswith("MSG#") or not row.get("roomId"):
            continue
        owner = owner_of(row["roomId"])
        if owner:
            jobs.append({"ownerId": owner, "roomId": row["roomId"], "sk": row["sk"]})
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--owner")
    parser.add_argument("--profile", default="isol8-admin")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    table = session.resource("dynamodb").Table(f"lightbulb-{args.env}-messaging")
    sqs = session.client("sqs")
    owners: dict[str, str | None] = {}

    def owner_of(room_id: str) -> str | None:
        if room_id not in owners:
            meta = table.get_item(Key={"pk": f"ROOM#{room_id}", "sk": "META"}).get("Item")
            owner = meta.get("ownerId") if meta else None
            owners[room_id] = owner if not args.owner or owner == args.owner else None
        return owners[room_id]

    scan = {"FilterExpression": Attr("sk").begins_with("MSG#"), "ProjectionExpression": "pk, sk, roomId"}
    sent = 0
    while True:
        page = table.scan(**scan)
        jobs = jobs_from_rows(page["Items"], owner_of)
        for start in range(0, len(jobs), 10):  # SendMessageBatch takes at most 10
            batch = jobs[start : start + 10]
            response = sqs.send_message_batch(
                QueueUrl=args.queue_url,
                Entries=[
                    {"Id": str(i), "MessageBody": json.dumps(job, separators=(",", ":"))} for i, job in enumerate(batch)
                ],
            )
            if response.get("Failed"):
                raise SystemExit(f"SQS rejected {len(response['Failed'])} job(s); re-run to retry")
            sent += len(batch)
        if "LastEvaluatedKey" not in page:
            break
        scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(f"enqueued {sent} message(s)")


if __name__ == "__main__":
    main()
