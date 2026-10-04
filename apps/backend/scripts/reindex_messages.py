"""Re-enqueue messages for the search indexer (backfill, or rebuild an index).

Re-runnable: the indexer re-reads each row, so repeating a run is harmless.

    aws sqs get-queue-url --queue-name lightbulb-<env>-message-search-index --profile isol8-admin
    uv run python scripts/reindex_messages.py --env dev --queue-url <url> [--owner org_x]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable

import boto3
from boto3.dynamodb.conditions import Attr


def jobs_from_rows(rows: Iterable[dict]) -> list[dict]:
    return [
        {"ownerId": row["ownerId"], "roomId": row["roomId"], "sk": row["sk"]}
        for row in rows
        if str(row.get("sk", "")).startswith("MSG#") and row.get("ownerId") and row.get("roomId")
    ]


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
    condition = Attr("sk").begins_with("MSG#")
    if args.owner:
        condition = condition & Attr("ownerId").eq(args.owner)
    scan = {"FilterExpression": condition, "ProjectionExpression": "pk, sk, roomId, ownerId"}
    sent = 0
    while True:
        page = table.scan(**scan)
        jobs = jobs_from_rows(page["Items"])
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
