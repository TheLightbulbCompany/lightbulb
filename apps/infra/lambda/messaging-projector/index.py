"""Project canonical messaging stream inserts to AppSync Events."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import boto3
import botocore.session
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MESSAGING_TABLE = os.environ.get("MESSAGING_TABLE", "")
USERS_TABLE = os.environ.get("USERS_TABLE", "")
SEARCH_INDEX_QUEUE_URL = os.environ.get("SEARCH_INDEX_QUEUE_URL", "")  # edits/deletes: immediate
SEARCH_INDEX_BATCH_QUEUE_URL = os.environ.get("SEARCH_INDEX_BATCH_QUEUE_URL", "")  # new messages: drained on a schedule
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
MAX_PUSH_TOKENS = 100
PUSH_BODY_CHARS = 180


_sqs_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _enqueue_search_index(item: dict[str, Any], meta: dict[str, Any], queue_url: str) -> None:
    """Point the search indexer at this row. The indexer re-reads the row, so
    duplicates and reordering are harmless. Keyed on the ROOM owner (META), whose
    index holds the room -- a shared room's foreign members search it there."""
    if not queue_url:
        return
    owner_id = meta.get("ownerId")
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("room metadata has no owner")
    _enqueue_job({"ownerId": owner_id, "roomId": item["roomId"], "sk": item["sk"]}, queue_url)


def _enqueue_job(job: dict[str, Any], queue_url: str) -> None:
    if queue_url:
        _sqs().send_message(QueueUrl=queue_url, MessageBody=json.dumps(job, separators=(",", ":")))

# Markdown -> one plain line for a push body, which iOS renders raw. Mirrors the web
# toast's ``plainPreview`` (apps/frontend/src/lib/plainPreview.ts); keep them together.
_PLAIN_RULES = (
    (re.compile(r"^\s*```.*$", re.M), ""),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.M), ""),
    (re.compile(r"^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M), ""),
    (re.compile(r"\*\*|__|~~|`"), ""),
    (re.compile(r"\s+"), " "),
)


def _plain_preview(text: str) -> str:
    text = text[:1000]  # bounded first: a push body never needs the whole message
    for pattern, repl in _PLAIN_RULES:
        text = pattern.sub(repl, text)
    return text.strip()


PREVIEW_CHARS = 140
MAX_MENTIONED_USERS = 32  # core.repositories.messaging_repo.MAX_ROOM_MEMBERS


def _last_message(message: dict[str, Any]) -> dict[str, Any]:
    """The room-list preview of one message. A copy of the backend's
    ``messaging_repo.last_message_preview``, which writes ``META.lastMessage``;
    the backend suite compares the two."""
    text = message.get("editedText") if int(message.get("messageRevision", 0)) else message.get("text")
    author = message["author"]
    return {
        "messageId": message["messageId"],
        "seq": int(message["messageSeq"]),
        "authorId": author["id"],
        "authorKind": author["kind"],
        "authorName": author.get("displayName") or author["id"],
        "textPreview": _plain_preview(text or "")[:PREVIEW_CHARS],
        "hasAttachments": bool(message.get("attachments")),
        "createdAt": message["createdAt"],
        "threadRootMessageId": message.get("threadRootMessageId"),
    }


HTTP_ENDPOINT = os.environ.get("APPSYNC_EVENTS_HTTP_ENDPOINT", "")
REGION = os.environ.get("APPSYNC_EVENTS_REGION", os.environ.get("AWS_REGION", ""))
MAX_EVENT_BYTES = 230 * 1024
MAX_MESSAGE_BYTES = 100_000
# Lockstep with core.services.messaging.ATTACHMENT_MAX_COUNT and the shared
# client guard in packages/shared/src/live/types.ts.
MAX_ATTACHMENTS = 4
# Must equal core.services.messaging.LIVE_EVENT_TYPES. Drift does not lose an
# event silently: _validate_live_event raises on an unlisted type, so the record
# is retried, then parked in the failure bucket and paged
# (messaging-projector-failures-stored). Today only room.access.removed and
# room.subscription.rotated reach this path (_remove_member_with_notices is the
# one LIVE# writer), so the equality test in the backend's
# tests/unit/services/test_live_event_registry.py guards against future drift.
LIVE_EVENT_TYPES = frozenset(
    {
        "room.access.removed",
        "room.activity.changed",
        "room.huddle.changed",
        "room.meeting.changed",
        "room.member.updated",
        "room.message.available",
        "room.message.created",
        "room.message.reactions.changed",
        "room.message.updated",
        "room.metadata.changed",
        "room.read.changed",
        "room.delivery.changed",
        "room.run.frame",
        "room.browser.frame",
        "room.subagent.event",
        "room.subscription.rotated",
        "room.typing.changed",
        "paperclip.event",
        "runtime.operation.changed",
        "runtime.resource.invalidated",
        "workspace.access.removed",
        "workspace.sessions.changed",
        "workspace.session.attention",
        "workspace.session.screen",
        "workspace.session.typing",
        "digest.ready",
    }
)

_deserializer = TypeDeserializer()
_serializer = TypeSerializer()
_ddb_client = None


def _ddb():
    global _ddb_client
    if _ddb_client is None:
        # A pool as wide as the member fan-out: at the default 10, the extra workers'
        # connections are discarded after each call and the next one pays a new TLS handshake.
        _ddb_client = boto3.client("dynamodb", config=Config(max_pool_connections=MAX_FANOUT_WORKERS))
    return _ddb_client


def _env_dimension() -> str:
    # MESSAGING_TABLE is "lightbulb-{env}-messaging" (table-names.ts); no
    # separate env var exists on this Lambda, so derive it from the one it
    # already reads instead of adding one.
    parts = MESSAGING_TABLE.split("-")
    return parts[1] if len(parts) >= 3 else "unknown"


def _emit_metric(name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
    """Print one EMF line. Never raises: a metrics bug must never fail the record.

    Dimensions never carry owner/user/room/member ids.
    """
    try:
        dims = {"env": _env_dimension(), "service": "messaging-projector", **dimensions}
        # One write per line (end=""): member fan-out runs on threads, and a
        # separate newline write could interleave two EMF lines.
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "LightbulbAI",
                    "Dimensions": [list(dims.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }],
            },
            **dims,
            name: value,
        }) + "\n", end="")
    except Exception as exc:  # noqa: BLE001 - metrics are best-effort
        logger.warning("projector metric emit failed name=%s error_type=%s", name, type(exc).__name__)


def _notify_latency_ms(created_at: str) -> float:
    created = _parse_rfc3339(created_at)
    return (datetime.now(timezone.utc) - created).total_seconds() * 1000


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def _deserialize_map(image: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(image, dict):
        raise ValueError("invalid DynamoDB image")
    return _normalise({key: _deserializer.deserialize(value) for key, value in image.items()})


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID")
    return value


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an RFC-3339 timestamp")
    try:
        parsed = _parse_rfc3339(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC-3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


SUPER_AGENT_ATTRS = ("taskRefs", "specialist", "routedTo", "forwardedFrom", "kind", "automationId")


def _canonical_message(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "messageId",
        "roomId",
        "messageSeq",
        "roomRevision",
        "author",
        "text",
        "mentions",
        "replyToMessageId",
        "threadRootMessageId",
        "createdAt",
        "runId",
    )
    try:
        message = {field: item[field] for field in fields}
    except KeyError as exc:
        raise ValueError(f"missing canonical message field {exc.args[0]}") from exc

    _require_uuid(message["messageId"], "messageId")
    room_id = _require_uuid(message["roomId"], "roomId")
    message_seq = _require_int(message["messageSeq"], "messageSeq", minimum=1)
    _require_int(message["roomRevision"], "roomRevision", minimum=1)
    if item.get("pk") != f"ROOM#{room_id}" or item.get("sk") != f"MSG#{message_seq:020d}":
        raise ValueError("message key does not match canonical identifiers")

    author = message["author"]
    if (
        not isinstance(author, dict)
        or author.get("kind") not in {"human", "agent", "system"}
        or not isinstance(author.get("id"), str)
        or not author["id"]
        or not isinstance(author.get("displayName"), str)
    ):
        raise ValueError("invalid canonical author")
    message["author"] = {
        "kind": author["kind"],
        "id": author["id"],
        "displayName": author["displayName"],
    }

    text = message["text"]
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("invalid canonical message text")

    mentions = message["mentions"]
    if not isinstance(mentions, list) or len(mentions) > 128:
        raise ValueError("invalid canonical mentions")
    clean_mentions = []
    for mention in mentions:
        if (
            not isinstance(mention, dict)
            or mention.get("kind") not in {"human", "agent"}
            or not isinstance(mention.get("id"), str)
            or not mention["id"]
        ):
            raise ValueError("invalid canonical mention")
        clean_mentions.append({"kind": mention["kind"], "id": mention["id"]})
    message["mentions"] = clean_mentions

    # The agents this message addresses (an @mention, a name-addressed open, or
    # a DM) -- ids only, mirrors core.services.messaging's canonical_message. A
    # `room_event` target is a mere candidate for Jev to route, not an
    # addressee: Jev picks at most one of them (or none), and `_room_dispatch`
    # advances every candidate's delivery cursor regardless of who Jev picked,
    # so including one here would call a plain message "Delivered" to an agent
    # nobody chose to run. Always present (like mentions), not omitted-when-empty
    # like attachments below: an empty list here is a real fact (nobody was
    # addressed), not missing data, so readState must not fall back on it.
    dispatch_targets = item.get("dispatchTargets") or []
    if not isinstance(dispatch_targets, list):
        raise ValueError("invalid canonical dispatchTargets")
    addressed_agent_ids = []
    for target in dispatch_targets:
        agent_id = target.get("agentId") if isinstance(target, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("invalid canonical dispatch target")
        if target.get("eventKind") == "user_request":
            addressed_agent_ids.append(agent_id)
    message["addressedAgentIds"] = addressed_agent_ids

    # Attachments are OPTIONAL on the row but load-bearing in the payload: the
    # shared client guard accepts `attachments === undefined`, so omitting them
    # here does not drop the event — it merges an attachment-less copy over the
    # one the HTTP read carried and the image silently disappears from the
    # timeline. Sanitised like author/mentions above rather than passed through.
    attachments = item.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("invalid canonical attachments")
    clean_attachments = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("invalid canonical attachment")
        clean = {key: attachment.get(key) for key in ("id", "fileName", "contentType")}
        if not all(isinstance(value, str) and value for value in clean.values()):
            raise ValueError("invalid canonical attachment")
        clean["size"] = _require_int(attachment.get("size"), "attachment size", minimum=1)
        clean_attachments.append(clean)
    # Omitted, not empty, when there are none: keeps a text message's payload
    # byte-identical to what it published before.
    if clean_attachments:
        message["attachments"] = clean_attachments

    # A session reference (owner ruling 2026-10-02: a chip is a link, never a wake): the
    # ids plus the readable label snapshot. Load-bearing like attachments -- the live copy
    # merges over the sender's POST response at the same revision, so leaving these out
    # strips the sender's own card and shows recipients none. Omitted when there are none.
    session_mentions = item.get("sessionMentions") or []
    if not isinstance(session_mentions, list) or len(session_mentions) > 4:
        raise ValueError("invalid canonical sessionMentions")
    labels = item.get("sessionTargetLabels") or {}
    if not isinstance(labels, dict):
        raise ValueError("invalid canonical sessionTargetLabels")
    if session_mentions:
        clean_labels = {}
        for session_id in session_mentions:
            _require_uuid(session_id, "sessionMentions")
            label = labels.get(session_id)
            if label is not None and (not isinstance(label, str) or len(label) > 256):
                raise ValueError("invalid canonical sessionTargetLabels")
            if label:
                clean_labels[session_id] = label
        message["sessionMentions"] = list(session_mentions)
        message["sessionTargetLabels"] = clean_labels

    # A mid-run progress line (core.services.messaging's canonical_message). Omitted otherwise.
    if item.get("progress") is True:
        message["progress"] = True
    # Super-agent message attrs (taskRefs, specialist, routedTo, forwardedFrom, kind, automationId), as the HTTP read carries them:
    # the live copy merges over it, so leaving them out dropped a Router reply's task cards until a reload.
    # Omitted when absent, so a legacy message's payload is unchanged.
    for attr in SUPER_AGENT_ATTRS:
        value = item.get(attr)
        if value is not None:
            if not isinstance(value, (str, list, dict)):
                raise ValueError(f"invalid canonical {attr}")
            message[attr] = value

    for field in ("replyToMessageId", "threadRootMessageId"):
        if message[field] is not None:
            _require_uuid(message[field], field)
    _require_timestamp(message["createdAt"], "createdAt")
    if message["runId"] is not None and not isinstance(message["runId"], str):
        raise ValueError("runId must be a string or null")
    return message


def _validate_live_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("live event must be an object")
    allowed = {
        "schemaVersion",
        "eventId",
        "type",
        "occurredAt",
        "roomId",
        "messageSeq",
        "roomRevision",
        "payload",
    }
    required = {"schemaVersion", "eventId", "type", "occurredAt", "payload"}
    if not required.issubset(event) or not set(event).issubset(allowed):
        raise ValueError("invalid live event fields")
    if event["schemaVersion"] != 1 or event["type"] not in LIVE_EVENT_TYPES:
        raise ValueError("unknown live event schema or type")
    _require_uuid(event["eventId"], "eventId")
    _require_timestamp(event["occurredAt"], "occurredAt")
    if "roomId" in event:
        _require_uuid(event["roomId"], "roomId")
    if "messageSeq" in event:
        _require_int(event["messageSeq"], "messageSeq", minimum=1)
    if "roomRevision" in event:
        _require_int(event["roomRevision"], "roomRevision", minimum=1)
    if not isinstance(event["payload"], dict):
        raise ValueError("live event payload must be an object")
    return event


def _get_item(pk: str, sk: str) -> dict[str, Any] | None:
    if not MESSAGING_TABLE:
        raise RuntimeError("MESSAGING_TABLE is not configured")
    response = _ddb().get_item(
        TableName=MESSAGING_TABLE,
        Key={"pk": {"S": pk}, "sk": {"S": sk}},
        ConsistentRead=True,
    )
    image = response.get("Item")
    return _deserialize_map(image) if image else None


def _workspace_id_for_meta(pk: str, sk: str, meta: dict[str, Any]) -> str | None:
    """The row's workspaceId, healed lazily for META rows that predate it.

    Mirrors the backend's ``core.repositories.messaging_repo.workspace_id_for_meta``
    (read the owner's ``OWNER#<owner>/WORKSPACE`` row, conditionally stamp this
    row with it). Read-only against that owner row — the backend mints it on
    every owner-facing write path, so a missing one here means the id is
    genuinely unresolvable, not just late. Returns ``None`` in that case; the
    caller must skip the record rather than raise, since a poisoned stream
    record stalls the shard fleet-wide while a lost live hint just falls back
    to the client's own HTTP reconcile.
    """
    existing = meta.get("workspaceId")
    if isinstance(existing, str) and existing:
        try:
            return _require_uuid(existing, "workspaceId")
        except ValueError:
            pass  # corrupt row; fall through and try to heal it below
    owner_id = meta.get("ownerId")
    owner_row = (
        _get_item(f"OWNER#{owner_id}", "WORKSPACE") if isinstance(owner_id, str) and owner_id else None
    )
    try:
        workspace_id = _require_uuid(owner_row.get("workspaceId") if owner_row else None, "workspaceId")
    except ValueError:
        logger.warning("messaging projector heal: unresolvable workspaceId pk=%s sk=%s", pk, sk)
        _emit_metric("messaging.projector.heal", 1, "Count", {"outcome": "unresolvable"})
        return None
    try:
        _ddb().update_item(
            TableName=MESSAGING_TABLE,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="SET workspaceId = :ws",
            ConditionExpression="attribute_not_exists(workspaceId)",
            ExpressionAttributeValues={":ws": {"S": workspace_id}},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    meta["workspaceId"] = workspace_id
    _emit_metric("messaging.projector.heal", 1, "Count", {"outcome": "stamped"})
    return workspace_id


def _event_json(event: dict[str, Any]) -> str:
    encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("live event exceeds 230 KiB")
    return encoded


def _query_rows(pk: str, prefix: str) -> list[dict[str, Any]]:
    rows = []
    cursor = {}
    while True:
        response = _ddb().query(
            TableName=MESSAGING_TABLE,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": {"S": pk}, ":prefix": {"S": prefix}},
            ConsistentRead=True,
            **cursor,
        )
        rows.extend(_deserialize_map(row) for row in response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            return rows
        cursor = {"ExclusiveStartKey": response["LastEvaluatedKey"]}


def _push_tokens(user_id: str) -> list[str]:
    """Expo tokens registered by one user, newest device order irrelevant.

    Best-effort by contract: a users-table fault must never stop the AppSync
    delivery hints the rest of the loop is publishing.
    """
    if not USERS_TABLE:
        return []
    try:
        item = _ddb().get_item(
            TableName=USERS_TABLE,
            Key={"user_id": {"S": user_id}},
            ConsistentRead=True,
        ).get("Item")
        devices = (_deserialize_map(item).get("push_tokens") if item else None) or {}
        return [
            entry["token"]
            for entry in devices.values()
            if isinstance(entry, dict) and isinstance(entry.get("token"), str) and entry["token"]
        ]
    except Exception as exc:  # noqa: BLE001 - push is best-effort
        logger.warning("projector push token read failed error_type=%s", type(exc).__name__)
        _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "token_read_error"})
        return []


def _push_where(meta: dict[str, Any], threaded: bool) -> str:
    """Where it was said, so a DM, a group chat and a channel never read alike.
    Same rule as ``roomWhere`` in packages/shared/src/messaging/notificationCopy.ts."""
    name = meta.get("name")
    kind = meta.get("kind")
    if kind == "dm":
        where = "DM"
    elif kind == "channel":
        where = f"#{name}" if name else "Channel"
    else:
        where = name or "Group chat"
    return f"Thread in {where}" if threaded else where


def _push_title(message: dict[str, Any], meta: dict[str, Any], mentioned: bool) -> str:
    """Line 1, who · where: "Pulse · #GTM Strategy". Outside a DM a mention says
    so, because on a channel the mention is the only reason the push exists at
    all; in a DM every message is already addressed to you. An agent needs no
    marker: ``displayName`` already IS the agent's name.
    """
    author = message["author"]["displayName"]
    who = f"{author} mentioned you" if mentioned and meta.get("kind") != "dm" else author
    if meta.get("kind") == "dm":
        return who
    return f"{who} \u00b7 {_push_where(meta, bool(message.get('threadRootMessageId')))}"


def _render_push(message: dict[str, Any], meta: dict[str, Any], mentioned: bool) -> dict[str, Any]:
    """The one banner text: Expo sends it to phones and the user channel carries
    it to desktop/web, so every surface says the same thing. Two lines: who \u00b7
    where, then the message (no subtitle: a third bold line was one too many)."""
    return {
        "title": _push_title(message, meta, mentioned),
        # A whitespace-only caption is an attachment, not a body.
        "body": _plain_preview(message["text"])[:PUSH_BODY_CHARS] or "Sent an image",
        # APNs thread-id: iOS stacks one conversation into one group instead of
        # N unrelated banners. Deliberately NOT collapseId, which REPLACES the
        # undelivered notification and would hide every message in a burst but
        # the newest.
        "threadId": message["roomId"],
        "data": {
            "roomId": message["roomId"],
            "messageId": message["messageId"],
            "messageSeq": message["messageSeq"],
            "threadRootMessageId": message["threadRootMessageId"],
            "roomKind": meta.get("kind"),
            # An agent reply names its run, so a client that also tracks the run
            # notifies once for the two (RunNotifications / MessageNotifications).
            **({"runId": message["runId"]} if message.get("runId") else {}),
        },
    }


def _send_push(
    recipients: list[tuple[str, bool]], message: dict[str, Any], meta: dict[str, Any]
) -> None:
    """One Expo request for every (token, mentioned) this message notified.

    Never raises. No ticket handling: the projector has read-only DynamoDB
    access. The title is per recipient because "mentioned you" is.
    """
    # ponytail: no token pruning; prune DeviceNotRegistered when the projector
    # gets write access to the users table.
    # One banner per phone: a token registered twice (two device rows, or two
    # accounts on one phone) must not be sent the same message twice.
    unique: dict[str, bool] = {}
    for token, mentioned in recipients:
        unique[token] = unique.get(token, False) or mentioned
    recipients = list(unique.items())
    if not recipients:
        return
    try:
        if len(recipients) > MAX_PUSH_TOKENS:
            # ponytail: one request, extra tokens dropped. Chunk into batches of
            # 100 when a single room outgrows it.
            logger.warning(
                "projector push capped tokens=%d sent=%d", len(recipients), MAX_PUSH_TOKENS
            )
            _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "capped"})
            recipients = recipients[:MAX_PUSH_TOKENS]
        body = json.dumps([
            {"to": token, **_render_push(message, meta, mentioned), "sound": "default"}
            for token, mentioned in recipients
        ]).encode("utf-8")
        request = urllib.request.Request(
            EXPO_PUSH_URL,
            data=body,
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST",
        )
        # A non-2xx arrives as HTTPError, so the one except below covers it.
        with urllib.request.urlopen(request, timeout=5):
            pass
        _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "ok"})
    except Exception as exc:  # noqa: BLE001 - push is best-effort
        logger.warning(
            "projector push send failed tokens=%d error_type=%s",
            len(recipients),
            type(exc).__name__,
        )
        _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "send_error"})


def _current_member(room_id: str, owner_id: str, user_id: str, after_seq: int | None = None) -> dict[str, Any] | None:
    """The strong-read membership row, or None if this person is not a CURRENT member (removed,
    other owner, or joined at/after ``after_seq``). Read at publish time, never from a cached list:
    a member removed before this read gets nothing."""
    member = _get_item(f"ROOM#{room_id}", f"MEMBER#human#{user_id}")
    if not member or member.get("ownerId") != owner_id:
        return None
    if after_seq is not None and after_seq <= member.get("joinedSeq", 0):
        return None
    return member


def _member_candidates(room_id: str) -> list[str]:
    """User ids from the membership query: discovery only, rechecked by _current_member."""
    return [candidate["id"] for candidate in _query_rows(f"ROOM#{room_id}", "MEMBER#human#")]


def _current_members(room_id: str, owner_id: str, after_seq: int | None = None):
    """``(user_id, member)`` for every CURRENT human member, each rechecked right after discovery."""
    for user_id in _member_candidates(room_id):
        member = _current_member(room_id, owner_id, user_id, after_seq)
        if member is not None:
            yield user_id, member


def _stream_channel(user_id: str, member: dict[str, Any], owner_id: str) -> str | None:
    """The member's private user channel, or ``None`` without an active stream."""
    home = member.get("homeOwnerId") or owner_id
    mapping = _get_item(f"OWNER#{home}", f"USERSTREAM#{user_id}")
    if not mapping or mapping.get("active") is not True:
        return None
    key = _require_uuid(mapping.get("channelKey"), "channelKey")
    reverse = _get_item(f"STREAM#{key}", "META")
    if not reverse or reverse.get("active") is not True or reverse.get("ownerId") != home or reverse.get("userId") != user_id:
        return None
    workspace_id = _workspace_id_for_meta(f"STREAM#{key}", "META", reverse)
    if workspace_id is None:
        return None  # this reader's private hint is unresolvable; everyone else still gets theirs
    return f"/ws/{workspace_id}/users/{key}"


def _member_streams(room_id: str, owner_id: str, after_seq: int | None = None):
    """``(user_id, member, channel)`` for every current human member with an
    active private stream."""
    for user_id, member in _current_members(room_id, owner_id, after_seq):
        channel = _stream_channel(user_id, member, owner_id)
        if channel is not None:
            yield user_id, member, channel


ACTIVITY_TTL_S = 90 * 24 * 3600
MAX_FANOUT_WORKERS = 16
MAX_RECORD_WORKERS = 10  # the event source's batchSize: a batch's messages fan out at once


def _thread_participants(room_id: str, root_id: str, root_seq: Any) -> set[str]:
    """Humans who started the thread, replied in it (``threadParticipants``,
    ADDed to the root MSG by each reply's own transaction) or were mentioned in
    its root. One strong read of the root by key -- the reply carries
    ``threadRootSeq`` -- replacing the old scan of the room's whole MSG# partition.
    A reply without the seq (written before it existed) has no participants."""
    if isinstance(root_seq, bool) or not isinstance(root_seq, int) or root_seq < 1:
        return set()
    root = _get_item(f"ROOM#{room_id}", f"MSG#{root_seq:020d}")
    if not root or root.get("messageId") != root_id:
        return set()
    people = set(root.get("threadParticipants") or ())
    author = root.get("author") or {}
    accountable_human = (
        author.get("id")
        if author.get("kind") == "human" and isinstance(author.get("id"), str)
        else root.get("actorUserId")
        if isinstance(root.get("actorUserId"), str) and root.get("actorUserId")
        else None
    )
    if accountable_human:
        people.add(accountable_human)
    people.update(
        ref["id"] for ref in root.get("mentions") or ()
        if isinstance(ref, dict) and ref.get("kind") == "human" and isinstance(ref.get("id"), str)
    )
    return people


def _activity_kind(
    user_id: str, room_kind: str | None, mentioned: set[str], participants: set[str]
) -> str | None:
    """One row per (user, message); precedence dm > mention > thread_reply."""
    if room_kind in {"dm", "group-dm"}:
        return "dm"
    if user_id in mentioned:
        return "mention"
    if user_id in participants:
        return "thread_reply"
    return None


def _write_activity(user_id: str, member: dict[str, Any], owner_id: str, kind: str, message: dict[str, Any],
                    mentioned: bool) -> None:
    """A pointer row in the reader's own Activity partition: no text, no names,
    so leaving a room leaves nothing to purge (the read path re-gates membership
    and renders from the current message). Idempotent on the message: a stream
    replay hits the conditional put and writes nothing."""
    home = member.get("homeOwnerId") or owner_id
    created = message["createdAt"]
    author = message["author"]
    item = {
        "pk": f"ACTIVITY#{home}#{user_id}",
        "sk": f"{created}#{message['messageId']}",
        "kind": kind,
        "roomId": message["roomId"],
        "seq": message["messageSeq"],
        "messageId": message["messageId"],
        "threadRootMessageId": message["threadRootMessageId"],
        "actor": {"kind": author["kind"], "id": author["id"]},
        "mentioned": mentioned,
        "createdAt": created,
        "expiresAt": int(_parse_rfc3339(created).timestamp()) + ACTIVITY_TTL_S,
    }
    try:
        _ddb().put_item(
            TableName=MESSAGING_TABLE,
            Item={key: _serializer.serialize(value) for key, value in item.items()},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


# The away email (T15): a DM or @mention still unread AWAY_DELAY_S later is emailed by the
# backend's needs_you_email sweeper, which re-checks the read cursor and presence then.
AWAY_KINDS = ("dm", "mention")
AWAY_DELAY_S = 300
AWAY_TTL_S = 86400


def _write_away(user_id: str, member: dict[str, Any], owner_id: str, kind: str, message: dict[str, Any]) -> None:
    """A pending away-email row on the shared sparse deadline index (``sweep = "away"``).
    Ids only, like the Activity row. Idempotent on the message."""
    created = int(_parse_rfc3339(message["createdAt"]).timestamp())
    item = {
        "pk": f"AWAY#{user_id}",
        "sk": f"M#{message['roomId']}#{message['messageId']}",
        "sweep": "away",
        "deadlineAt": created + AWAY_DELAY_S,
        "kind": kind,
        "userId": user_id,
        "ownerId": member.get("homeOwnerId") or owner_id,
        "roomId": message["roomId"],
        "messageId": message["messageId"],
        "seq": message["messageSeq"],
        "createdAt": message["createdAt"],
        "expiresAt": created + AWAY_TTL_S,
    }
    try:
        _ddb().put_item(
            TableName=MESSAGING_TABLE,
            Item={key: _serializer.serialize(value) for key, value in item.items()},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _is_feed_room(room_id: str, owner_id: str) -> bool:
    """The workspace feed, by its id (backend ``messaging_repo.feed_room_id``: edit both together).
    Feed posts never push: only "Needs you" notifies (spec 2026-10-14 §4.2)."""
    return room_id == str(uuid5(NAMESPACE_URL, f"lightbulb-feed-room|{owner_id}"))


def _notify_members(
    message: dict[str, Any], meta: dict[str, Any], thread_root_seq: Any = None,
    silent: bool = False, actor_user_id: Any = None,
) -> list[tuple[str, bool]]:
    room_id, seq = message["roomId"], message["messageSeq"]
    owner_id = meta["ownerId"]
    root = message["threadRootMessageId"]
    participants = _thread_participants(room_id, root, thread_root_seq) if root else set()
    mentioned = {ref["id"] for ref in message["mentions"] if ref["kind"] == "human"}
    author = message["author"]
    accountable_human = (
        author["id"]
        if author["kind"] == "human"
        else actor_user_id if isinstance(actor_user_id, str) and actor_user_id else None
    )
    if accountable_human:
        # Native sessions and hosted MCP clients render as system authors, while
        # actorUserId identifies the human who sent through them. That human is
        # still the sender for self-mention, Activity and push purposes.
        mentioned.discard(accountable_human)
    last_message = _last_message(message)
    mentioned_ids = sorted(mentioned)[:MAX_MENTIONED_USERS]
    feed = _is_feed_room(room_id, owner_id)

    def notify_one(user_id: str) -> list[tuple[str, bool]]:
        tokens: list[tuple[str, bool]] = []
        # Membership is strong-read HERE, right before delivery, not when the candidates were
        # listed: a member removed while another worker was busy still gets nothing.
        member = _current_member(room_id, owner_id, user_id, after_seq=seq)
        if member is None:
            return tokens
        own = accountable_human == user_id
        # Your own message is never Activity (nor a notify); you still get the hint's preview.
        # Neither is a silent (advisory) line, e.g. "paused after N replies" under a reply
        # that already notified.
        kind = None if own or silent else _activity_kind(user_id, meta.get("kind"), mentioned, participants)
        if kind is not None:
            # Written before the hint below, so the refetch a notify triggers already sees it.
            _write_activity(user_id, member, owner_id, kind, message, user_id in mentioned)
        if kind in AWAY_KINDS and member.get("muted") is not True:
            _write_away(user_id, member, owner_id, kind, message)
        channel = _stream_channel(user_id, member, owner_id)
        if channel is None:
            return tokens
        read = _get_item(f"ROOM#{room_id}", f"READ#human#{user_id}") or {}
        notify = seq > read.get("lastReadSeq", 0) and kind is not None
        # Mute is client-side everywhere else; push is server-side by nature,
        # so it is the one surface that has to honour it here.
        payload = {"roomId": room_id, "messageId": message["messageId"],
                   "messageSeq": seq, "threadRootMessageId": root, "notify": notify,
                   "lastMessage": last_message, "mentionedUserIds": mentioned_ids}
        if accountable_human:
            payload["accountableHumanId"] = accountable_human
        if notify:
            if feed:
                # By design this covers a human @mentioned in a feed post's Discuss thread too.
                _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "feed"})
            elif member.get("muted") is True:
                _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "muted"})
            else:
                at_me = user_id in mentioned
                tokens.extend((token, at_me) for token in _push_tokens(user_id))
                # The same banner the phone gets, for desktop/web to show verbatim.
                payload["push"] = _render_push(message, meta, at_me)
        # A <=140-char plain preview (and, when it notifies, the phone's banner) may
        # ride the private user channel, to members current at publish time only
        # (_current_member strong-reads each one). The full message stays behind the
        # authenticated HTTP read and the room channel, whose deliveryKey rotation
        # remains the revocation boundary.
        _publish(channel, {
            "schemaVersion": 1, "eventId": str(uuid5(UUID(message["messageId"]), f"notify:{user_id}")),
            "type": "room.message.available", "occurredAt": message["createdAt"],
            "roomId": room_id, "messageSeq": seq,
            "payload": payload,
        })
        return tokens

    # Members fan out in parallel: one HTTPS publish each was the whole cost of a
    # record. pool.map re-raises the first failure only after every member ran
    # (the with-block drains), so the record still fails and replays.
    # ponytail: one room is one partition key, so one stream lane; a busy room's
    # ceiling is now one parallel fan-out per message, not one publish per member.
    with ThreadPoolExecutor(max_workers=MAX_FANOUT_WORKERS) as pool:
        results = list(pool.map(notify_one, _member_candidates(room_id)))
    # (token, was this token's owner @mentioned) — the push title is per reader.
    return [recipient for tokens in results for recipient in tokens]


def _publish_preview_change(item: dict[str, Any], meta: dict[str, Any], revision: int) -> None:
    """An edit or delete of the room's current preview: hand members the new
    ``META.lastMessage`` (the edit/delete transaction already rewrote it)."""
    if item.get("threadRootMessageId") or "lastMessage" not in meta:
        return  # replies never own the preview; a room without the field predates it
    last = meta["lastMessage"]
    seq = int(item["messageSeq"])
    if last is not None and last.get("messageId") != item["messageId"] and int(last["seq"]) > seq:
        return  # a newer message owns the preview; this edit changed nothing visible
    room_id = item["roomId"]
    for user_id, _member, channel in _member_streams(room_id, meta["ownerId"]):
        _publish(channel, {
            "schemaVersion": 1, "eventId": str(uuid5(UUID(item["messageId"]), f"preview:{revision}:{user_id}")),
            "type": "room.message.available", "occurredAt": item["editedAt"],
            "roomId": room_id, "messageSeq": seq,
            "payload": {"roomId": room_id, "messageId": item["messageId"], "messageSeq": seq,
                        "threadRootMessageId": None, "notify": False,
                        "lastMessage": last, "mentionedUserIds": [], "previewOnly": True},
        })


_botocore_session = None


def _current_credentials():
    # One session per process: building a fresh one per publish resolved the whole
    # provider chain each time, ~5 ms of GIL-bound CPU per member on a laptop and
    # several times that on the Lambda. The session caches its Credentials, and
    # get_frozen_credentials() refreshes a RefreshableCredentials when it nears expiry.
    global _botocore_session
    if _botocore_session is None:
        _botocore_session = botocore.session.get_session()
    credentials = _botocore_session.get_credentials()
    if credentials is None:
        raise RuntimeError("AWS role credentials are unavailable")
    return credentials.get_frozen_credentials()


def _signed_request(channel: str, event: dict[str, Any]) -> AWSRequest:
    if not HTTP_ENDPOINT or not REGION:
        raise RuntimeError("AppSync Events endpoint or region is not configured")
    event_json = _event_json(event)
    body = json.dumps(
        {"channel": channel, "events": [event_json]},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    request = AWSRequest(
        method="POST",
        url=HTTP_ENDPOINT,
        data=body,
        headers={"content-type": "application/json"},
    )
    SigV4Auth(_current_credentials(), "appsync", REGION).add_auth(request)
    return request


def _send_request(request: AWSRequest) -> tuple[int, bytes]:
    prepared = request.prepare()
    outbound = urllib.request.Request(
        prepared.url,
        data=prepared.body,
        headers=dict(prepared.headers.items()),
        method=prepared.method,
    )
    with urllib.request.urlopen(outbound, timeout=5) as response:
        return response.status, response.read()


def _publish(channel: str, event: dict[str, Any]) -> None:
    status, raw_body = _send_request(_signed_request(channel, event))
    if status < 200 or status >= 300:
        raise RuntimeError("AppSync Events publish returned non-2xx")
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("AppSync Events publish returned invalid JSON") from exc
    successful = body.get("successful") if isinstance(body, dict) else None
    failed = body.get("failed") if isinstance(body, dict) else None
    if not isinstance(successful, list) or not successful or failed != []:
        raise RuntimeError("AppSync Events rejected an event")


def _enqueue_search_index_on_insert(item: dict[str, Any], meta: dict[str, Any]) -> None:
    # A missed INSERT only leaves the message out of search, and
    # scripts/reindex_messages.py repairs that; failing here would drop the push and
    # stall the shard. Edits/deletes (_project_message_update) keep raising: a missed
    # delete would leave deleted text searchable.
    try:
        # Batch queue falls back to immediate if its env is missing (partial deploy).
        _enqueue_search_index(item, meta, SEARCH_INDEX_BATCH_QUEUE_URL or SEARCH_INDEX_QUEUE_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "search index enqueue failed message_id=%s error_type=%s", item.get("messageId"), type(exc).__name__
        )
        _emit_metric("messaging.projector.search_enqueue.failed", 1, "Count", {})


def _session_search_job(record_name: str, image: dict[str, Any], sk: str) -> Callable[[], None]:
    """A coding session's row, for session search: a new EVENT goes to the batch queue
    (best-effort, like a new message; scripts/reindex_sessions.py repairs a miss), a
    removal (META with ``removedAt``) to the immediate queue and raises on failure, since a
    missed delete would leave a removed session's text searchable."""
    pk = (image.get("pk") or {}).get("S", "")
    owner_id, _, session_id = pk.removeprefix("LOCALSESSION#").rpartition("#")
    if not owner_id or not session_id:
        return _noop
    job = {"ownerId": owner_id, "sessionId": session_id, "sk": sk}
    if sk.startswith("EVENT#") and record_name == "INSERT":
        def enqueue_event() -> None:
            try:
                _enqueue_job(job, SEARCH_INDEX_BATCH_QUEUE_URL or SEARCH_INDEX_QUEUE_URL)
            except Exception as exc:  # noqa: BLE001
                logger.warning("session search enqueue failed error_type=%s", type(exc).__name__)
                _emit_metric("messaging.projector.search_enqueue.failed", 1, "Count", {})
        return enqueue_event
    if sk == "META" and "removedAt" in image:
        job["eventSeq"] = int((image.get("eventSeq") or {}).get("N", "0"))
        return lambda: _enqueue_job(job, SEARCH_INDEX_QUEUE_URL)
    return _noop


def _project_message_update(item: dict[str, Any]) -> None:
    room_id = _require_uuid(item.get("roomId"), "roomId")
    message_id = _require_uuid(item.get("messageId"), "messageId")
    seq = _require_int(item.get("messageSeq"), "messageSeq", minimum=1)
    revision = _require_int(item.get("messageRevision"), "messageRevision", minimum=1)
    meta = _get_item(f"ROOM#{room_id}", "META")
    if not meta:
        raise ValueError("room metadata is missing")
    delivery_key = _require_uuid(meta.get("deliveryKey"), "deliveryKey")
    workspace_id = _workspace_id_for_meta(f"ROOM#{room_id}", "META", meta)
    if workspace_id is None:
        _enqueue_search_index(item, meta, SEARCH_INDEX_QUEUE_URL)  # an unresolvable workspace still reaches the index
        return  # unresolvable; skip this edit rather than poison the shard
    # Invalidation keeps edited/tombstone content in the HTTP canonical projector.
    # No work dispatch or new-message notification is generated by edits.
    _publish(f"/ws/{workspace_id}/rooms/{room_id}/{delivery_key}", {
        "schemaVersion": 1, "eventId": str(uuid5(UUID(message_id), f"edit:{revision}")),
        "type": "room.message.updated", "occurredAt": item["editedAt"], "roomId": room_id,
        "payload": {"roomId": room_id, "messageId": message_id, "messageSeq": seq},
    })
    _publish_preview_change(item, meta, revision)
    _enqueue_search_index(item, meta, SEARCH_INDEX_QUEUE_URL)  # after live delivery: an SQS failure must not delay it


def _fan_out_message(item: dict[str, Any]) -> Callable[[], None]:
    """The order-insensitive half of a new message -- reads, Activity rows and the
    per-member user-channel hints -- run now, possibly beside the batch's other
    messages. Returns the ordered half (room event, search, push), which the handler
    runs in stream order, i.e. ``seq`` order for one room."""
    # A delayed INSERT replay must not republish content subsequently deleted.
    latest = _get_item(item["pk"], item["sk"])
    if latest and int(latest.get("messageRevision", 0)) > 0:
        return lambda: _project_message_update(latest)
    message = _canonical_message(item)
    meta = _get_item(f"ROOM#{message['roomId']}", "META")
    if not meta:
        raise ValueError("room metadata is missing")
    delivery_key = _require_uuid(meta.get("deliveryKey"), "deliveryKey")
    workspace_id = _workspace_id_for_meta(f"ROOM#{message['roomId']}", "META", meta)
    if workspace_id is None:
        # an unresolvable workspace still reaches the index; skip rather than poison the shard
        return lambda: _enqueue_search_index_on_insert(item, meta)
    event = {
        "schemaVersion": 1,
        "eventId": message["messageId"],
        "type": "room.message.created",
        "occurredAt": message["createdAt"],
        "roomId": message["roomId"],
        "messageSeq": message["messageSeq"],
        "roomRevision": message["roomRevision"],
        "payload": message,
    }
    recipients = _notify_members(
        message, meta, item.get("threadRootSeq"), item.get("silent") is True,
        item.get("actorUserId"),
    )

    def finish() -> None:
        _publish(f"/ws/{workspace_id}/rooms/{message['roomId']}/{delivery_key}", event)
        try:
            _emit_metric(
                "messaging.projector.notify.latency",
                _notify_latency_ms(message["createdAt"]),
                "Milliseconds",
                {"room_kind": meta.get("kind", "unknown")},
            )
        except Exception as exc:  # noqa: BLE001 - metrics are best-effort, never fail the record
            logger.warning("projector notify latency failed error_type=%s", type(exc).__name__)
        # Enqueue after the live publish (indexing never stalls delivery) but before
        # push, so a failed enqueue replays the record before anyone is notified.
        _enqueue_search_index_on_insert(item, meta)
        # Push LAST: a raising publish replays the whole record, and Expo has no
        # eventId dedupe, so an earlier send would banner every member twice.
        # ponytail: immediate push; add a delayed READ re-check if double-notify annoys.
        _send_push(recipients, message, meta)

    return finish


def _project_message(item: dict[str, Any]) -> None:
    _fan_out_message(item)()


def _project_live(item: dict[str, Any]) -> None:
    channel_key = _require_uuid(item.get("channelKey"), "channelKey")
    user_id = item.get("userId")
    owner_id = item.get("ownerId")
    if not isinstance(user_id, str) or not user_id or not isinstance(owner_id, str) or not owner_id:
        raise ValueError("live outbox owner/user is invalid")
    event = _validate_live_event(item.get("event"))
    expected_sk = f"LIVE#{event['eventId']}#USER#{user_id}"
    if item.get("sk") != expected_sk:
        raise ValueError("live outbox key does not match event/user")

    mapping = _get_item(f"STREAM#{channel_key}", "META")
    if (
        not mapping
        or mapping.get("active") is not True
        or mapping.get("userId") != user_id
        or mapping.get("ownerId") != owner_id
        or (mapping.get("channelKey") is not None and mapping.get("channelKey") != channel_key)
    ):
        return
    workspace_id = _workspace_id_for_meta(f"STREAM#{channel_key}", "META", mapping)
    if workspace_id is None:
        return  # unresolvable; skip this live notice rather than poison the shard
    _publish(f"/ws/{workspace_id}/users/{channel_key}", event)


def _thread_touch_only(item: dict[str, Any]) -> bool:
    """This MODIFY is a thread reply's ``threadParticipants`` ADD on its root, not an
    edit. The stream carries NEW_IMAGE only, so there is no old image to diff: the
    ADD sets ``threadTouchedAt`` and every edit/delete REMOVEs it, so its presence
    alone says which write produced this image -- no timestamps compared, so a slow
    or clock-skewed edit/delete is never skipped. (A later reaction write also
    reads as "not an edit" -- it only ever replayed the same ``edit:<revision>``
    event, which clients already deduplicate.)"""
    return isinstance(item.get("threadTouchedAt"), str)


def _noop() -> None:
    return None


_TASK_PARTS = {"ATT": "attempt", "Q": "question", "SUB": "subscription"}


def _task_part(sk: str) -> str:
    """TASK#{id} | TASK#{id}#ATT#n | TASK#{id}#Q#q | TASK#{id}#SUB#k; anything else is ''."""
    segments = sk.split("#")
    return "task" if len(segments) == 2 else _TASK_PARTS.get(segments[2], "") if len(segments) > 3 else ""


def _task_changed(record: dict[str, Any]) -> Callable[[], None]:
    """Super-agent tasks table: ids only (I1); clients refetch through the authorized API.
    Publishes only rows that carry ``wsChannel`` (``tasks_repo.stamp_channel``: task, attempt, question and
    effect rows). An effect row (``EFFECT#{idemKey}``) names its task in ``taskId``."""
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return _noop
    stream = record.get("dynamodb") or {}
    item = _deserialize_map(stream.get("NewImage") or {})
    sk, channel = item.get("sk"), item.get("wsChannel")
    if not isinstance(sk, str) or not isinstance(channel, str) or not channel:
        return _noop
    if sk.startswith("EFFECT#") and isinstance(item.get("taskId"), str) and item["taskId"]:
        part, task_id = "effect", item["taskId"]
    elif sk.startswith("TASK#"):
        part, task_id = _task_part(sk), sk.split("#")[1]
    else:
        return _noop
    if not part:
        return _noop
    if record["eventName"] == "MODIFY" and _deserialize_map(stream.get("OldImage") or {}).get("version") == item.get("version"):
        return _noop
    version = item.get("version")
    event = {  # the standard live envelope (packages/shared/src/live/types.ts parseLiveEvent); anything else is dropped
        "schemaVersion": 1,
        "eventId": str(uuid5(NAMESPACE_URL, f"task:{task_id}:{sk}:{version}")),  # per row: a replayed record dedupes
        "type": "task.changed",
        "occurredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {"taskId": task_id, "part": part, "version": version},
    }
    return lambda: _publish(f"/ws/{channel}/tasks", event)


def prepare_record(record: dict[str, Any]) -> Callable[[], None]:
    """Validate a record and run its order-insensitive work; return the ordered rest."""
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if "-tasks/stream/" in str(record.get("eventSourceARN") or ""):
        return _task_changed(record)
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return _noop
    image = (record.get("dynamodb") or {}).get("NewImage")
    if not isinstance(image, dict):
        raise ValueError("INSERT record has no NewImage")
    sk_value = image.get("sk")
    if not isinstance(sk_value, dict) or not isinstance(sk_value.get("S"), str):
        raise ValueError("INSERT record has no string sort key")
    sk = sk_value["S"]
    if ((image.get("pk") or {}).get("S") or "").startswith("LOCALSESSION#"):
        return _session_search_job(record["eventName"], image, sk)
    if not sk.startswith(("MSG#", "LIVE#")):
        return _noop
    item = _deserialize_map(image)
    if record.get("eventName") == "MODIFY":
        # The stream contains NEW_IMAGE only. Later reaction writes replay the
        # same revision's event ID, which clients already deduplicate.
        if sk.startswith("MSG#"):
            if int(item.get("messageRevision", 0)) > 0 and not _thread_touch_only(item):
                return lambda: _project_message_update(item)
        return _noop
    if sk.startswith("MSG#"):
        return _fan_out_message(item)
    return lambda: _project_live(item)


def process_record(record: dict[str, Any]) -> None:
    prepare_record(record)()


def record_id(record: dict[str, Any]) -> str:
    sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
    return str(sequence or record.get("eventID") or "unknown")


def handler(event: dict, context: object) -> dict:
    del context
    failures = []
    records = event.get("Records", []) if isinstance(event, dict) else []
    if not isinstance(records, list):
        records = []
    # One room is one partition key, so one stream lane: a hot room's ceiling is one
    # batch at a time. Its messages' member fan-outs (Activity rows, user-channel
    # hints) overlap here; the ordered rest -- room event, search, push -- runs below
    # in stream order, which is seq order for a room. In-order room events matter:
    # the client merges by seq but treats a skipped seq as a gap and refetches.
    # ponytail: still one lane per room; past ~MAX_RECORD_WORKERS x the serial rate,
    # write-shard a hot room's partition key -- a table change, not this file.
    pool = ThreadPoolExecutor(max_workers=MAX_RECORD_WORKERS)
    prepared = [pool.submit(prepare_record, record) for record in records]
    try:
        for record, future in zip(records, prepared):
            identifier = record_id(record) if isinstance(record, dict) else "unknown"
            try:
                future.result()()
            except Exception as exc:  # noqa: BLE001 - partial-batch contract
                logger.error(
                    "messaging projector record failed record_id=%s error_type=%s",
                    identifier,
                    type(exc).__name__,
                )
                failures.append({"itemIdentifier": identifier})
                # A stream retries from the lowest failed record, so every record after
                # it replays. Their ordered halves (room event, PUSH) never ran, so the
                # retry banners nobody twice; their fan-outs may have run, and replay
                # harmlessly: hints keep their eventId, Activity is a conditional put.
                break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return {"batchItemFailures": failures}
