"""Replay one explicit messaging-projector S3 failure object.

Usage: python replay.py s3://bucket/key
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlparse

import boto3

from index import process_record, record_id


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("expected one explicit s3://bucket/key URI")
    return parsed.netloc, parsed.path.lstrip("/")


def records_from_failure(document: object) -> list[dict]:
    if not isinstance(document, dict):
        raise ValueError("failure object must be a JSON object")
    payload = document.get("payload", document.get("requestPayload", document))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("failure object payload is not valid JSON") from exc
    records = payload.get("Records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("failure object contains no DynamoDB records")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("failure object contains a malformed record")
    return records


def replay_uri(uri: str, *, s3_client=None) -> tuple[list[dict[str, str]], bool]:
    bucket, key = parse_s3_uri(uri)
    client = s3_client or boto3.client("s3")
    raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    document = json.loads(raw)
    results = []
    failed = False
    for record in records_from_failure(document):
        identifier = record_id(record)
        try:
            process_record(record)
            status = "success"
        except Exception:  # noqa: BLE001 - report IDs only; never record contents
            status = "failure"
            failed = True
        results.append({"recordId": identifier, "status": status})
    return results, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one explicit messaging-projector S3 failure object")
    parser.add_argument("s3_uri")
    args = parser.parse_args(argv)
    results, failed = replay_uri(args.s3_uri)
    print(json.dumps({"records": results}, separators=(",", ":")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
