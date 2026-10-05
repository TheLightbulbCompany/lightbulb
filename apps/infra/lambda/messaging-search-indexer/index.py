"""Index one message per SQS job into OpenSearch Serverless (message search).

A job names a row ({ownerId, roomId, sk}). The indexer re-reads the CURRENT row,
writes it (or deletes it when the row is deleted or gone), then re-reads again and
repeats until the row it last wrote is still the row in DynamoDB. That loop is what
keeps a slow job from putting back a deleted message, an edited message's old text,
or a wiped workspace's text. Only public text is indexed."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

import boto3
import botocore.session
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MESSAGING_TABLE = os.environ.get("MESSAGING_TABLE", "")
SEARCH_INDEX_BATCH_QUEUE_URL = os.environ.get("SEARCH_INDEX_BATCH_QUEUE_URL", "")
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_ROUNDS = 5
DRAIN_MIN_REMAINING_MS = 60_000

MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "roomId": {"type": "keyword"},
            "messageId": {"type": "keyword"},
            "messageSeq": {"type": "long"},
            "createdAt": {"type": "date"},
            "text": {"type": "text", "analyzer": "english"},
            "authorName": {"type": "text"},
            "authorKind": {"type": "keyword"},
            "authorId": {"type": "keyword"},
            "threadRootMessageId": {"type": "keyword"},
        },
    }
}

_ddb_client = None
_sqs_client = None
_deserializer = TypeDeserializer()


def index_name(owner_id: str) -> str:
    # MUST match apps/backend/core/services/message_search.py:index_name.
    return "messages-" + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def doc_id(room_id: str, sk: str) -> str:
    return f"{room_id}:{int(sk.removeprefix('MSG#'))}"


def document(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("deletedAt"):
        return None
    author = item.get("author") or {}
    return {
        "roomId": item["roomId"],
        "messageId": item["messageId"],
        "messageSeq": int(item["messageSeq"]),
        "createdAt": item["createdAt"],
        "text": item["editedText"] if "editedText" in item else item.get("text") or "",
        "authorName": author.get("displayName") or "",
        "authorKind": author.get("kind") or "",
        "authorId": author.get("id") or "",
        "threadRootMessageId": item.get("threadRootMessageId"),
    }


def _ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_row(room_id: str, sk: str) -> dict[str, Any] | None:
    response = _ddb().get_item(
        TableName=MESSAGING_TABLE,
        Key={"pk": {"S": f"ROOM#{room_id}"}, "sk": {"S": sk}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return {key: _deserializer.deserialize(value) for key, value in item.items()} if item else None


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else b""
    request = AWSRequest(
        method=method,
        url=f"{SEARCH_ENDPOINT}{path}",
        data=data,
        headers={"content-type": "application/json", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()},
    )
    credentials = botocore.session.get_session().get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "aoss", REGION).add_auth(request)
    prepared = request.prepare()
    outbound = urllib.request.Request(
        prepared.url, data=prepared.body or None, headers=dict(prepared.headers.items()), method=method
    )
    try:
        # 40 s: a NextGen cold start takes 10–30 s.
        with urllib.request.urlopen(outbound, timeout=40) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _ensure_index(name: str) -> None:
    # ponytail: one PUT per write, no in-process cache -- a cache would go stale
    # when a wipe drops the index. If a wipe drops the index between this PUT and
    # the doc PUT, aoss auto-creates a dynamic-mapped index; the verify loop then
    # deletes the doc, leaving an empty mis-mapped index for that owner. Fix by
    # deleting it (scripts/reindex_messages.py rebuilds) if it is ever seen.
    status, raw = _request("PUT", f"/{name}", MAPPING)
    if status >= 300 and b"resource_already_exists" not in raw:
        raise RuntimeError(f"create index failed status={status}")


def process(job: dict[str, Any]) -> None:
    owner_id, room_id, sk = job["ownerId"], job["roomId"], job["sk"]
    if not isinstance(sk, str) or not sk.startswith("MSG#"):
        raise ValueError("job does not name a message row")
    name = index_name(owner_id)
    target = f"/{name}/_doc/{doc_id(room_id, sk)}"
    item = _get_row(room_id, sk)
    for _ in range(MAX_ROUNDS):
        doc = document(item) if item else None
        if doc is None:
            status, _ = _request("DELETE", target)
            if status >= 300 and status != 404:
                raise RuntimeError(f"delete failed status={status}")
            return  # a racing writer's own verify read will see this delete too
        _ensure_index(name)
        status, _ = _request("PUT", target, doc)
        if status not in (200, 201):
            raise RuntimeError(f"index failed status={status}")
        item = _get_row(room_id, sk)
        if item and document(item) == doc:
            return
    raise RuntimeError("row kept changing; retrying the job")


def drain(context: Any) -> dict[str, int]:
    """Index every queued new-message job (the batch queue has no event source;
    a schedule invokes this). Failed jobs stay queued and retry after visibility."""
    drained = failed = 0
    if not SEARCH_INDEX_BATCH_QUEUE_URL:
        return {"drained": 0, "failed": 0}
    while context.get_remaining_time_in_millis() >= DRAIN_MIN_REMAINING_MS:
        messages = _sqs().receive_message(
            QueueUrl=SEARCH_INDEX_BATCH_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=1, VisibilityTimeout=600
        ).get("Messages", [])
        if not messages:
            break
        done = []
        for message in messages:
            if context.get_remaining_time_in_millis() < DRAIN_MIN_REMAINING_MS:
                break  # the rest stay queued for the next drain
            try:
                process(json.loads(message["Body"]))
                done.append({"Id": message["MessageId"], "ReceiptHandle": message["ReceiptHandle"]})
            except Exception as exc:  # noqa: BLE001 - left in the queue to retry
                logger.error("search index job failed message_id=%s error_type=%s", message["MessageId"], type(exc).__name__)
                failed += 1
        if done:
            response = _sqs().delete_message_batch(QueueUrl=SEARCH_INDEX_BATCH_QUEUE_URL, Entries=done)
            if response.get("Failed"):
                logger.warning("search index drain delete failed count=%d", len(response["Failed"]))
        drained += len(done)
    logger.info("search index drain drained=%d failed=%d", drained, failed)
    return {"drained": drained, "failed": failed}


def handler(event: dict, context: Any) -> dict:
    if event.get("drain"):
        return drain(context)
    failures = []
    for record in event.get("Records", []):
        try:
            process(json.loads(record["body"]))
        except Exception as exc:  # noqa: BLE001 - partial-batch contract
            logger.error("search index job failed message_id=%s error_type=%s", record.get("messageId"), type(exc).__name__)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
