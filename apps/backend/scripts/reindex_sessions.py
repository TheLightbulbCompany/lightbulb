"""Re-enqueue coding-session prompts and replies for the search indexer (backfill, or rebuild).

Re-runnable: the indexer re-reads each row and its session, so repeating a run is harmless.
Only text rows go out; the indexer still decides what is indexed (prompts and replies).

    aws sqs get-queue-url --queue-name lightbulb-<env>-message-search-index --profile isol8-admin
    uv run python scripts/reindex_sessions.py --env dev --queue-url <url> [--owner org_x]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable

import boto3
from boto3.dynamodb.conditions import Attr


def jobs_from_rows(rows: Iterable[dict]) -> list[dict]:
    jobs = []
    for row in rows:
        pk, sk = str(row.get("pk", "")), str(row.get("sk", ""))
        owner, _, session_id = pk.removeprefix("LOCALSESSION#").rpartition("#")
        if not pk.startswith("LOCALSESSION#") or not sk.startswith("EVENT#") or not owner or not session_id:
            continue
        if row.get("kind") == "text" and row.get("role") in ("user", "assistant"):
            jobs.append({"ownerId": owner, "sessionId": session_id, "sk": sk})
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
    prefix = f"LOCALSESSION#{args.owner}#" if args.owner else "LOCALSESSION#"
    scan = {
        "FilterExpression": Attr("pk").begins_with(prefix) & Attr("sk").begins_with("EVENT#"),
        "ProjectionExpression": "pk, sk, #kind, #role",
        "ExpressionAttributeNames": {"#kind": "kind", "#role": "role"},
    }
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
    print(f"enqueued {sent} session event(s)")


if __name__ == "__main__":
    main()
