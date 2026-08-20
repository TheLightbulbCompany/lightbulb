#!/usr/bin/env python3
"""Export or replay opt-in WEBHOOK_DELIVERY_CAPTURE CloudWatch records.

Examples:
  AWS_PROFILE=isol8-connector python apps/backend/scripts/webhook_capture_replay.py \
    export --log-group /aws/api-gateway/isol8-prod-http --hours 24 --out captures.jsonl
  python apps/backend/scripts/webhook_capture_replay.py manifest captures.jsonl --out manifest.json
  python apps/backend/scripts/webhook_capture_replay.py replay captures.jsonl --url https://example.test/hooks/t/o/p --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

PREFIX = "WEBHOOK_DELIVERY_CAPTURE "


def _records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if PREFIX in line:
            line = line.split(PREFIX, 1)[1]
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def cmd_export(args: argparse.Namespace) -> None:
    start = int(time.time() - args.hours * 3600)
    end = int(time.time())
    query = (
        "fields @timestamp, @message | filter @message like /WEBHOOK_DELIVERY_CAPTURE/ "
        "| sort @timestamp asc | limit 10000"
    )
    start_out = subprocess.check_output(
        [
            "aws",
            "logs",
            "start-query",
            "--region",
            args.region,
            "--log-group-name",
            args.log_group,
            "--start-time",
            str(start),
            "--end-time",
            str(end),
            "--query-string",
            query,
            "--query",
            "queryId",
            "--output",
            "text",
        ],
        text=True,
    ).strip()
    status = "Running"
    results: dict[str, Any] = {}
    deadline = time.time() + args.timeout_seconds
    while status in {"Scheduled", "Running"}:
        if time.time() > deadline:
            raise SystemExit(f"Timed out waiting for CloudWatch Logs Insights query {start_out}")
        time.sleep(2)
        results = json.loads(
            subprocess.check_output(
                ["aws", "logs", "get-query-results", "--region", args.region, "--query-id", start_out, "--output", "json"],
                text=True,
            )
        )
        status = results.get("status", "Unknown")
    if status != "Complete":
        raise SystemExit(f"CloudWatch Logs Insights query {start_out} ended with status {status}")
    with Path(args.out).open("w") as fh:
        for row in results.get("results", []):
            fields = {item["field"]: item["value"] for item in row}
            message = fields.get("@message", "")
            if PREFIX not in message:
                continue
            fh.write(message.split(PREFIX, 1)[1].strip() + "\n")


def cmd_manifest(args: argparse.Namespace) -> None:
    records = _records(Path(args.file))
    by_type: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for event_type in record.get("provider_event_types") or [record.get("provider_event_type") or "unknown"]:
            by_type.setdefault(event_type, []).append(
                {
                    "provider": record.get("provider"),
                    "delivery_id": record.get("provider_delivery_id"),
                    "delivery_timestamp": record.get("provider_delivery_timestamp"),
                    "signature_headers": sorted((record.get("signature_headers") or {}).keys()),
                }
            )
    Path(args.out).write_text(json.dumps({"capture_count": len(records), "event_types": by_type}, indent=2, sort_keys=True) + "\n")


def cmd_replay(args: argparse.Namespace) -> None:
    for record in _records(Path(args.file)):
        body = base64.b64decode(record["raw_payload_base64"])
        headers = {k: v for k, v in (record.get("signature_headers") or {}).items()}
        if record.get("content_type"):
            headers["content-type"] = record["content_type"]
        if args.dry_run:
            print(json.dumps({"would_replay": record.get("provider_delivery_id"), "event": record.get("provider_event_type")}, sort_keys=True))
            continue
        req = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=args.timeout_seconds) as resp:  # noqa: S310 - operator-provided URL
            print(json.dumps({"delivery_id": record.get("provider_delivery_id"), "status": resp.status}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--log-group", required=True)
    export.add_argument("--region", default="us-east-1")
    export.add_argument("--hours", type=int, default=24)
    export.add_argument("--timeout-seconds", type=int, default=120)
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("file")
    manifest.add_argument("--out", required=True)
    manifest.set_defaults(func=cmd_manifest)
    replay = sub.add_parser("replay")
    replay.add_argument("file")
    replay.add_argument("--url", required=True)
    replay.add_argument("--dry-run", action="store_true")
    replay.add_argument("--timeout-seconds", type=int, default=30)
    replay.set_defaults(func=cmd_replay)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
