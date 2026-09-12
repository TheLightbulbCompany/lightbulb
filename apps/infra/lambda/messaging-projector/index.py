"""Project canonical messaging stream inserts to AppSync Events."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import boto3
import botocore.session
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MESSAGING_TABLE = os.environ.get("MESSAGING_TABLE", "")
USERS_TABLE = os.environ.get("USERS_TABLE", "")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
MAX_PUSH_TOKENS = 100
PUSH_BODY_CHARS = 180
HTTP_ENDPOINT = os.environ.get("APPSYNC_EVENTS_HTTP_ENDPOINT", "")
REGION = os.environ.get("APPSYNC_EVENTS_REGION", os.environ.get("AWS_REGION", ""))
MAX_EVENT_BYTES = 230 * 1024
MAX_MESSAGE_BYTES = 100_000
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
        "room.member.updated",
        "room.message.available",
        "room.message.created",
        "room.message.reactions.changed",
        "room.message.updated",
        "room.metadata.changed",
        "room.resources.changed",
        "room.run.frame",
        "room.subscription.rotated",
        "room.typing.changed",
        "paperclip.event",
        "runtime.approval.requested",
        "runtime.approval.resolved",
        "runtime.operation.changed",
        "runtime.progress.changed",
        "runtime.question.requested",
        "runtime.question.resolved",
        "runtime.resource.invalidated",
    }
)

_deserializer = TypeDeserializer()
_ddb_client = None


def _ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
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
        }))
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


def _send_push(tokens: list[str], message: dict[str, Any], meta: dict[str, Any]) -> None:
    """One Expo request for every token this message notified. Never raises.

    No ticket handling: the projector has read-only DynamoDB access.
    """
    # ponytail: no token pruning; prune DeviceNotRegistered when the projector
    # gets write access to the users table.
    if not tokens:
        return
    try:
        if len(tokens) > MAX_PUSH_TOKENS:
            # ponytail: one request, extra tokens dropped. Chunk into batches of
            # 100 when a single room outgrows it.
            logger.warning(
                "projector push capped tokens=%d sent=%d", len(tokens), MAX_PUSH_TOKENS
            )
            _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "capped"})
            tokens = tokens[:MAX_PUSH_TOKENS]
        author = message["author"]["displayName"]
        # Every room kind may be nameless (CreateRoomRequest.name is optional).
        name = meta.get("name") if meta.get("kind") == "channel" else None
        title = f"{author} in #{name}" if name else author
        data = {
            "roomId": message["roomId"],
            "messageId": message["messageId"],
            "messageSeq": message["messageSeq"],
            "threadRootMessageId": message["threadRootMessageId"],
        }
        body = json.dumps([
            {
                "to": token,
                "title": title,
                # A whitespace-only caption is an attachment, not a body.
                "body": message["text"].strip()[:PUSH_BODY_CHARS] or "Sent an image",
                "data": data,
                "sound": "default",
            }
            for token in tokens
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
            "projector push send failed tokens=%d error_type=%s", len(tokens), type(exc).__name__
        )
        _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "send_error"})


def _notify_members(message: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    room_id, seq = message["roomId"], message["messageSeq"]
    owner_id = meta["ownerId"]
    root = message["threadRootMessageId"]
    participants = set()
    if root:
        # ponytail: thread replies scan founder-sized rooms; add a thread index
        # when measured room history makes this query expensive.
        participants = {
            row["author"]["id"] for row in _query_rows(f"ROOM#{room_id}", "MSG#")
            if row.get("author", {}).get("kind") == "human"
            and (row.get("messageId") == root or row.get("threadRootMessageId") == root)
        }
    mentioned = {ref["id"] for ref in message["mentions"] if ref["kind"] == "human"}
    tokens: list[str] = []
    for candidate in _query_rows(f"ROOM#{room_id}", "MEMBER#human#"):
        user_id = candidate["id"]
        # Recheck membership after discovery, including remove/rejoin history floor.
        member = _get_item(f"ROOM#{room_id}", f"MEMBER#human#{user_id}")
        if not member or member.get("ownerId") != owner_id or seq <= member.get("joinedSeq", 0):
            continue
        mapping = _get_item(f"OWNER#{owner_id}", f"USERSTREAM#{user_id}")
        if not mapping or mapping.get("active") is not True:
            continue
        key = _require_uuid(mapping.get("channelKey"), "channelKey")
        reverse = _get_item(f"STREAM#{key}", "META")
        if not reverse or reverse.get("active") is not True or reverse.get("ownerId") != owner_id or reverse.get("userId") != user_id:
            continue
        read = _get_item(f"ROOM#{room_id}", f"READ#human#{user_id}") or {}
        notify = (
            not (message["author"]["kind"] == "human" and message["author"]["id"] == user_id)
            and seq > read.get("lastReadSeq", 0)
            and (meta.get("kind") in {"dm", "group-dm"} or user_id in mentioned or user_id in participants)
        )
        # Mute is client-side everywhere else; push is server-side by nature,
        # so it is the one surface that has to honour it here.
        if notify:
            if member.get("muted") is True:
                _emit_metric("messaging.projector.push", 1, "Count", {"outcome": "muted"})
            else:
                tokens.extend(_push_tokens(user_id))
        # Content stays behind an authenticated HTTP read, even if access is
        # removed between this membership check and the user's delivery.
        _publish(f"/users/{key}", {
            "schemaVersion": 1, "eventId": str(uuid5(UUID(message["messageId"]), f"notify:{user_id}")),
            "type": "room.message.available", "occurredAt": message["createdAt"],
            "roomId": room_id, "messageSeq": seq,
            "payload": {"roomId": room_id, "messageId": message["messageId"],
                        "messageSeq": seq, "threadRootMessageId": root, "notify": notify},
        })
    return tokens


def _current_credentials():
    credentials = botocore.session.get_session().get_credentials()
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


def _project_message_update(item: dict[str, Any]) -> None:
    room_id = _require_uuid(item.get("roomId"), "roomId")
    message_id = _require_uuid(item.get("messageId"), "messageId")
    seq = _require_int(item.get("messageSeq"), "messageSeq", minimum=1)
    revision = _require_int(item.get("messageRevision"), "messageRevision", minimum=1)
    meta = _get_item(f"ROOM#{room_id}", "META")
    if not meta:
        raise ValueError("room metadata is missing")
    delivery_key = _require_uuid(meta.get("deliveryKey"), "deliveryKey")
    # Invalidation keeps edited/tombstone content in the HTTP canonical projector.
    # No work dispatch or new-message notification is generated by edits.
    _publish(f"/rooms/{room_id}/{delivery_key}", {
        "schemaVersion": 1, "eventId": str(uuid5(UUID(message_id), f"edit:{revision}")),
        "type": "room.message.updated", "occurredAt": item["editedAt"], "roomId": room_id,
        "payload": {"roomId": room_id, "messageId": message_id, "messageSeq": seq},
    })


def _project_message(item: dict[str, Any]) -> None:
    # A delayed INSERT replay must not republish content subsequently deleted.
    latest = _get_item(item["pk"], item["sk"])
    if latest and int(latest.get("messageRevision", 0)) > 0:
        _project_message_update(latest)
        return
    message = _canonical_message(item)
    meta = _get_item(f"ROOM#{message['roomId']}", "META")
    if not meta:
        raise ValueError("room metadata is missing")
    delivery_key = _require_uuid(meta.get("deliveryKey"), "deliveryKey")
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
    tokens = _notify_members(message, meta)
    _publish(f"/rooms/{message['roomId']}/{delivery_key}", event)
    try:
        _emit_metric(
            "messaging.projector.notify.latency",
            _notify_latency_ms(message["createdAt"]),
            "Milliseconds",
            {"room_kind": meta.get("kind", "unknown")},
        )
    except Exception as exc:  # noqa: BLE001 - metrics are best-effort, never fail the record
        logger.warning("projector notify latency failed error_type=%s", type(exc).__name__)
    # Push LAST: a raising publish replays the whole record, and Expo has no
    # eventId dedupe, so an earlier send would banner every member twice.
    # ponytail: immediate push; add a delayed READ re-check if double-notify annoys.
    _send_push(tokens, message, meta)


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
    _publish(f"/users/{channel_key}", event)


def process_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return
    image = (record.get("dynamodb") or {}).get("NewImage")
    if not isinstance(image, dict):
        raise ValueError("INSERT record has no NewImage")
    sk_value = image.get("sk")
    if not isinstance(sk_value, dict) or not isinstance(sk_value.get("S"), str):
        raise ValueError("INSERT record has no string sort key")
    sk = sk_value["S"]
    if not sk.startswith(("MSG#", "LIVE#")):
        return
    item = _deserialize_map(image)
    if record.get("eventName") == "MODIFY":
        # The stream contains NEW_IMAGE only. Later reaction writes replay the
        # same revision's event ID, which clients already deduplicate.
        if sk.startswith("MSG#") and int(item.get("messageRevision", 0)) > 0:
            _project_message_update(item)
        return
    if sk.startswith("MSG#"):
        _project_message(item)
    else:
        _project_live(item)


def record_id(record: dict[str, Any]) -> str:
    sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
    return str(sequence or record.get("eventID") or "unknown")


def handler(event: dict, context: object) -> dict:
    del context
    failures = []
    records = event.get("Records", []) if isinstance(event, dict) else []
    if not isinstance(records, list):
        records = []
    for record in records:
        identifier = record_id(record) if isinstance(record, dict) else "unknown"
        try:
            process_record(record)
        except Exception as exc:  # noqa: BLE001 - partial-batch contract
            logger.error(
                "messaging projector record failed record_id=%s error_type=%s",
                identifier,
                type(exc).__name__,
            )
            failures.append({"itemIdentifier": identifier})
    return {"batchItemFailures": failures}
