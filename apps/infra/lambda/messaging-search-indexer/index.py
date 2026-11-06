"""Index one message per SQS job into OpenSearch Serverless (message search).

A job names a row ({ownerId, roomId, sk}). The indexer re-reads the CURRENT row,
writes it (or deletes it when the row is deleted or gone), then re-reads again and
repeats until the row it last wrote is still the row in DynamoDB. That loop is what
keeps a slow job from putting back a deleted message, an edited message's old text,
or a wiped workspace's text. Only public text is indexed.

A job that names a session row ({ownerId, sessionId, sk}) indexes what was said in a
coding session into the same owner index (``kind: "session"``): an EVENT row only when
it is a person's prompt or the assistant's reply, never tool output, thinking, lifecycle,
subagent or system rows. A META job deletes a removed session's documents."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
# One attempt can make three sequential OpenSearch calls (40 s timeout each) plus DynamoDB reads.
DRAIN_MIN_REMAINING_MS = 130_000

# Session documents (kind "session") share the owner's index with messages.
SESSION_PROPERTIES = {"kind": {"type": "keyword"}, "sessionId": {"type": "keyword"}, "seq": {"type": "long"}}
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
            **SESSION_PROPERTIES,
        },
    }
}
#: Who answers in a session, for its documents' authorName (a person's name is resolved at read time).
PROVIDER_NAMES = {"claude": "Claude Code", "codex": "Codex"}
BULK_DELETE_CHUNK = 500

_ddb_client = None
_sqs_client = None
_deserializer = TypeDeserializer()


def index_name(owner_id: str) -> str:
    # MUST match apps/backend/core/services/message_search.py:index_name.
    return "messages-" + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def doc_id(room_id: str, sk: str) -> str:
    return f"{room_id}:{int(sk.removeprefix('MSG#'))}"


# Code (a fence, or a span of n backticks closed by n), else one of the composer's escapes:
# * _ ~ ` anywhere, # > - + = and "1." at a line start. Nothing else: `\\server\share`, `a\.b` stay.
_AGENT_ESCAPES = re.compile(
    r"(?P<code>```[\s\S]*?```|(?P<ticks>`+)(?:(?!(?P=ticks)).)+?(?P=ticks))"
    r"|\\(?P<ch>[*_~`])"
    r"|^(?P<lead>[ \t]*)\\(?P<blk>[#>+=-])"
    r"|^(?P<num>[ \t]*\d+)\\(?P<dot>[.)])",
    re.M,
)


def _agent_unescape(match: re.Match[str]) -> str:
    if match["code"]:
        return match["code"]
    if match["ch"]:
        return match["ch"]
    if match["blk"]:
        return match["lead"] + match["blk"]
    return match["num"] + match["dot"]


def agent_text(text: str) -> str:
    """A message's text as an agent reads it: the web composer escapes what a person typed
    (``3\\*4``, ``\\_\\_init\\_\\_``) so it shows literally; agents (and search) get it back
    clean. Code is left as is. A copy of messaging_repo.agent_text
    (apps/backend/core/repositories/messaging_repo.py); test_agent_text keeps them identical."""
    return _AGENT_ESCAPES.sub(_agent_unescape, text) if "\\" in text else text


def document(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("deletedAt"):
        return None
    author = item.get("author") or {}
    return {
        "roomId": item["roomId"],
        "messageId": item["messageId"],
        "messageSeq": int(item["messageSeq"]),
        "createdAt": item["createdAt"],
        # Indexed as typed: an escaped \_\_init\_\_ would tokenize to "_init" and never match __init__.
        "text": agent_text(item["editedText"] if "editedText" in item else item.get("text") or ""),
        "authorName": author.get("displayName") or "",
        "authorKind": author.get("kind") or "",
        "authorId": author.get("id") or "",
        "threadRootMessageId": item.get("threadRootMessageId"),
    }


def session_doc_id(session_id: str, seq: int) -> str:
    return f"session:{session_id}:{seq}"


def session_document(item: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a search may find in one session EVENT row, or None. Only what a person typed and
    what the assistant answered; everything else in a transcript stays out of the index."""
    if meta is None or meta.get("removedAt") is not None:
        return None
    role = item.get("role")
    text = item.get("text") or ""
    if item.get("kind") != "text" or role not in ("user", "assistant") or item.get("subagent") or not text.strip():
        return None
    actor = item.get("actor") or {}
    person = role == "user"
    created = int(item.get("createdAt") or 0)
    return {
        "kind": "session",
        "sessionId": meta["sessionId"],
        "seq": int(item["seq"]),
        "authorKind": "person" if person else "agent",
        "authorId": (actor.get("userId") or meta.get("humanUserId") or "") if person else "",
        "authorName": "" if person else PROVIDER_NAMES.get(meta.get("provider"), meta.get("provider") or ""),
        "createdAt": datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "text": text,
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


def _get_row(room_id: str, sk: str, pk: str | None = None) -> dict[str, Any] | None:
    response = _ddb().get_item(
        TableName=MESSAGING_TABLE,
        Key={"pk": {"S": pk or f"ROOM#{room_id}"}, "sk": {"S": sk}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return {key: _deserializer.deserialize(value) for key, value in item.items()} if item else None


def _request(method: str, path: str, body: dict[str, Any] | bytes | None = None) -> tuple[int, bytes]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
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


def _ensure_index(name: str, *, sessions: bool = False) -> None:
    # ponytail: one PUT per write, no in-process cache -- a cache would go stale
    # when a wipe drops the index. If a wipe drops the index between this PUT and
    # the doc PUT, aoss auto-creates a dynamic-mapped index; the verify loop then
    # deletes the doc, leaving an empty mis-mapped index for that owner. Fix by
    # deleting it (scripts/reindex_messages.py rebuilds) if it is ever seen.
    status, raw = _request("PUT", f"/{name}", MAPPING)
    if status >= 300 and b"resource_already_exists" not in raw:
        raise RuntimeError(f"create index failed status={status}")
    if sessions and status >= 300:
        # An index made before session search has a strict mapping without these fields.
        # ponytail: one extra PUT per session write; cache per container if it shows in latency.
        status, _ = _request("PUT", f"/{name}/_mapping", {"properties": SESSION_PROPERTIES})
        if status >= 300:
            raise RuntimeError(f"session mapping failed status={status}")


def _delete_session_docs(name: str, session_id: str, last_seq: int) -> None:
    """Every document a session could have: ids are its event seqs, so no delete-by-query."""
    for start in range(1, last_seq + 1, BULK_DELETE_CHUNK):
        lines = [
            json.dumps({"delete": {"_index": name, "_id": session_doc_id(session_id, seq)}})
            for seq in range(start, min(start + BULK_DELETE_CHUNK, last_seq + 1))
        ]
        status, raw = _request("POST", "/_bulk", ("\n".join(lines) + "\n").encode())
        if status == 404:
            return  # no index: nothing to delete
        if status >= 300:
            raise RuntimeError(f"bulk delete failed status={status}")
        items = json.loads(raw).get("items", [])
        if any(item.get("delete", {}).get("status") not in (200, 404) for item in items):
            raise RuntimeError("bulk delete left documents")


def process_session(job: dict[str, Any]) -> None:
    owner_id, session_id, sk = job["ownerId"], job["sessionId"], job["sk"]
    pk = f"LOCALSESSION#{owner_id}#{session_id}"
    name = index_name(owner_id)
    if sk == "META":
        meta = _get_row("", "META", pk)
        if meta is not None and meta.get("removedAt") is None:
            return  # resumed (or never removed): its documents stay
        # ponytail: a session resumed after this delete keeps searchable only what it says next;
        # scripts/reindex_sessions.py puts the rest back.
        _delete_session_docs(name, session_id, int((meta or {}).get("eventSeq") or job.get("eventSeq") or 0))
        return
    if not isinstance(sk, str) or not sk.startswith("EVENT#"):
        raise ValueError("job does not name a session event row")
    target = f"/{name}/_doc/{session_doc_id(session_id, int(sk.removeprefix('EVENT#')))}"
    # Events are immutable; the session is not: a removal between write and verify deletes it.
    for _ in range(MAX_ROUNDS):
        item, meta = _get_row("", sk, pk), _get_row("", "META", pk)
        doc = session_document(item, meta) if item else None
        if doc is None:
            status, _ = _request("DELETE", target)
            if status >= 300 and status != 404:
                raise RuntimeError(f"delete failed status={status}")
            return
        _ensure_index(name, sessions=True)
        status, _ = _request("PUT", target, doc)
        if status not in (200, 201):
            raise RuntimeError(f"index failed status={status}")
        meta = _get_row("", "META", pk)
        if session_document(item, meta) == doc:
            return
    raise RuntimeError("session kept changing; retrying the job")


def process(job: dict[str, Any]) -> None:
    if "sessionId" in job:
        return process_session(job)
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


def _release(messages: list[dict[str, Any]]) -> None:
    entries = [{"Id": m["MessageId"], "ReceiptHandle": m["ReceiptHandle"], "VisibilityTimeout": 0} for m in messages]
    failed = 0
    try:
        for i in range(0, len(entries), 10):
            response = _sqs().change_message_visibility_batch(
                QueueUrl=SEARCH_INDEX_BATCH_QUEUE_URL, Entries=entries[i : i + 10]
            )
            failed += len(response.get("Failed", []))
    except Exception as exc:  # noqa: BLE001 - they reappear after the visibility timeout
        logger.warning("search index drain release error_type=%s", type(exc).__name__)
    if failed:
        logger.warning("search index drain release failed count=%d", failed)


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
        for position, message in enumerate(messages):
            if context.get_remaining_time_in_millis() < DRAIN_MIN_REMAINING_MS:
                _release(messages[position:])  # untouched: visible at once, no receive counted against them
                break
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
