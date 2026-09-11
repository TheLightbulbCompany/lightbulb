#!/usr/bin/env python3
"""Replay one explicit EventBridge Pipe DLQ message into OpenClaw work FIFO."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
from boto3.dynamodb.types import TypeDeserializer

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_deserializer = TypeDeserializer()


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID")
    return value


def _source_event(document: object) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Pipe DLQ body must be a JSON object")
    if isinstance(document.get("event"), dict):
        return document["event"]
    if isinstance(document.get("Records"), list):
        records = document["Records"]
        if len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError("Pipe DLQ body must contain exactly one source event")
        return records[0]
    return document


def validate_work_event(document: object) -> tuple[dict, str, str]:
    event = _source_event(document)
    if event.get("eventName") != "INSERT":
        raise ValueError("Pipe DLQ event is not an INSERT")
    image = (event.get("dynamodb") or {}).get("NewImage")
    if not isinstance(image, dict):
        raise ValueError("Pipe DLQ event has no NewImage")
    sk = (image.get("sk") or {}).get("S")
    if not isinstance(sk, str) or not sk.startswith("WORK#"):
        raise ValueError("Pipe DLQ event is not a WORK insert")
    payload = (image.get("payload") or {}).get("M")
    group_id = (image.get("messageGroupId") or {}).get("S")
    dedup_id = (image.get("messageDeduplicationId") or {}).get("S")
    if (
        not isinstance(payload, dict)
        or not isinstance(group_id, str)
        or not _SHA256.fullmatch(group_id)
        or not isinstance(dedup_id, str)
        or not _SHA256.fullmatch(dedup_id)
    ):
        raise ValueError("Pipe DLQ event has invalid FIFO fields")

    pointer = _normalise({key: _deserializer.deserialize(value) for key, value in payload.items()})
    if set(pointer) == {"kind", "roomId", "agentId", "throughSeq"}:
        if pointer["kind"] != "room_dispatch":
            raise ValueError("unknown work pointer")
        _uuid(pointer["roomId"], "roomId")
        _uuid(pointer["agentId"], "agentId")
        if (
            isinstance(pointer["throughSeq"], bool)
            or not isinstance(pointer["throughSeq"], int)
            or pointer["throughSeq"] < 1
        ):
            raise ValueError("throughSeq must be a positive integer")
    elif set(pointer) == {"kind", "ownerId", "operationId"}:
        if pointer["kind"] != "runtime_operation" or not isinstance(pointer["ownerId"], str) or not pointer["ownerId"]:
            raise ValueError("unknown work pointer")
        _uuid(pointer["operationId"], "operationId")
    else:
        raise ValueError("unknown work pointer")
    # The Pipe forwards the raw stream record (no input template), so the
    # replay must re-send the same record — the worker unwraps payload.M.
    return event, group_id, dedup_id


def _release(sqs, queue_url: str, receipt_handles: list[str]) -> None:
    for receipt_handle in receipt_handles:
        try:
            sqs.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=0,
            )
        except Exception:
            pass


def _find_message(sqs, queue_url: str, message_id: str) -> tuple[dict, list[str]]:
    held: list[str] = []
    for _ in range(100):
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            VisibilityTimeout=300,
        )
        messages = response.get("Messages", [])
        if not messages:
            break
        selected = None
        for message in messages:
            if message.get("MessageId") == message_id:
                selected = message
            else:
                receipt_handle = message.get("ReceiptHandle")
                if receipt_handle:
                    held.append(receipt_handle)
        if selected is not None:
            if not selected.get("ReceiptHandle"):
                _release(sqs, queue_url, held)
                raise ValueError("Pipe DLQ message has no receipt handle")
            return selected, held
    _release(sqs, queue_url, held)
    raise ValueError("Pipe DLQ message ID was not visible")


def replay_message(
    *,
    pipe_dlq_url: str,
    work_queue_url: str,
    message_id: str,
    sqs_client=None,
) -> None:
    if not pipe_dlq_url or not work_queue_url.endswith(".fifo") or not message_id:
        raise ValueError("explicit Pipe DLQ URL, FIFO URL, and message ID are required")
    sqs = sqs_client or boto3.client("sqs")
    message, held = _find_message(sqs, pipe_dlq_url, message_id)
    selected_receipt = message.get("ReceiptHandle")
    deleted = False
    try:
        event, group_id, dedup_id = validate_work_event(json.loads(message.get("Body", "")))
        response = sqs.send_message(
            QueueUrl=work_queue_url,
            MessageBody=json.dumps(event, separators=(",", ":"), sort_keys=True),
            MessageGroupId=group_id,
            MessageDeduplicationId=dedup_id,
        )
        if not response.get("MessageId"):
            raise RuntimeError("FIFO enqueue returned no message ID")
        sqs.delete_message(
            QueueUrl=pipe_dlq_url,
            ReceiptHandle=selected_receipt,
        )
        deleted = True
    finally:
        _release(sqs, pipe_dlq_url, held)
        if selected_receipt and not deleted:
            _release(sqs, pipe_dlq_url, [selected_receipt])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one explicit EventBridge Pipe DLQ work record")
    parser.add_argument("--pipe-dlq-url", required=True)
    parser.add_argument("--work-queue-url", required=True)
    parser.add_argument("--message-id", required=True)
    args = parser.parse_args(argv)
    replay_message(
        pipe_dlq_url=args.pipe_dlq_url,
        work_queue_url=args.work_queue_url,
        message_id=args.message_id,
    )
    print(json.dumps({"messageId": args.message_id, "status": "replayed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
