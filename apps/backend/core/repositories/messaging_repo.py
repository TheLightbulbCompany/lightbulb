"""Canonical multiplayer messaging store — ONE DynamoDB table (``MESSAGING_TABLE``).

Key registry (architecture spec §7; the ``by-member`` GSI is discovery ONLY —
every protected operation rechecks the base ``MEMBER#`` row strongly):

    OWNER#<owner>   ROOM#<room>                        room directory projection
    ROOM#<room>     META                               owner, fields, revision, lastSeq, deliveryKey, lastMessage (top-level preview),
                                                       topCount (top-level ordinals, see unread_count)
    ROOM#<room>     MEMBER#<kind>#<id>                 role, listenMode, mentionHandle, joinedSeq, joinedTopOrdinal, homeOwnerId (foreign members of a shared room; memberKey is keyed on it)
    ROOM#<room>     MSG#<20-digit seq>                 original request + public edit overlay + dispatchTargets + topOrdinal;
                                                       a reply carries threadRootSeq, a root threadParticipants (human ids)
    ROOM#<room>     MENTION#<user>#<20-digit seq>      one per @mentioned human (not the author); counted for mentionCount
    ROOM#<room>     IDEMP#EDIT#<user>#<operationId>     durable edit/delete retry receipt
    ROOM#<room>     IDEMP#human#<user>#<clientMessageId>  durable human retry receipt
    OWNER#<owner>   IDEMP#AGENT#<eventId>              durable agent retry receipt (owner-scoped
                                                       because the receipt route carries no roomId)
    ROOM#<room>     READ#human#<user>                  last read seq + its topOrdinal
    ROOM#<room>     DELIVERY#AGENT#<agent>             last drained seq per agent
    ROOM#<room>     WORK#MESSAGE#<seq>#AGENT#<agent>   one-shot outbox -> EventBridge Pipe -> FIFO
    ROOM#<room>     LIVE#<eventId>#USER#<user>         one-shot private-user notification outbox
    OWNER#<owner>   USERSTREAM#<user>                  current opaque private-channel key
    STREAM#<key>    META                               reverse mapping AppSync onSubscribe reads
    OWNER#<owner>   OPERATION#<id>                     async runtime op status/result
    OWNER#<owner>   WORK#OPERATION#<id>                one-shot outbox for runtime work
    OWNER#<owner>   NETWAKE                            network doorbell: tokenHash (sha256), lastWakeAt (no TTL)
    OWNER#<owner>   WORK#NETWAKE#<ulid>                one-shot outbox: wake the parked workspace's network sidecar
    OWNER#<owner>   WORKSPACE                          opaque AppSync channel-path segment
    WORKSPACE#<ws>  META                               owner of the path segment (AppSync onPublish reads it)
    WORKSPACE#<ws>  MEMBER#<user>                      the user may subscribe /ws/<ws>/sessions (onSubscribe
                                                       reads it); written by live-config while their stream
                                                       is active, deleted with it by deactivate_user_stream
    OWNER#<owner>   MCPSEND#<user>#<requestId>          hosted MCP send: its destination (+ roomId once a
                                                       person send's DM is known); no TTL, the receipt index
    OWNER#<owner>   RUN#<runId>                         async turn ledger (Task 9)
    OWNER#<owner>   USAGE#<runId>                       first-writer-wins usage-callback receipt (Task 7)
    OWNER#<owner>   VOICERUN#<runId>                    a Live voice run's room, agent and speaker (2 h TTL; NOT RUN#:
                                                       chat never steers into a voice run)
    ROOM#<room>     FOLLOWUP#<followUpId>               a finished async delegation owed back to its agent
    ROOM#<room>     WORK#FOLLOWUP#<agent>#<followUpId>  one-shot outbox for that follow-up turn
    ROOM#<room>     DECISION#<messageId>                Jev routing decision, claim-first (NOT WORK#:
                                                       the Pipe must never see it; the projector skips it)
    ACTIVITY#<home>#<user>  <createdAt>#<messageId>    Activity feed pointer (no text), written by the
                                                       messaging projector after the message commits; 90-day TTL
    ACTIVITY#<home>#<user>  SEEN                       the reader's Activity ``seenUpTo`` (monotonic)
    OWNER#<owner>   VOICEAGENT                         the voice assistant's settings (Agent page), one JSON
                                                       document in ``config``; no TTL
    OWNER#<owner>   VOICEUSAGE#<transaction>           immutable voice metering event; never TTL'd
    OWNER#<owner>   WORK#VOICEUSAGE#<transaction>      one-shot outbox for Metronome + allowance notice work

``WORK#``, ``LIVE#``, ``RUN#``, ``USAGE#``, ``FOLLOWUP#`` and ``DECISION#`` rows carry ``expiresAt`` (7 days). Every read
here is ``ConsistentRead=True``; the GSIs are queried by ``list_rooms_for_member``
(``by-member``) and ``list_overdue_runs`` (``by-run-deadline``) and nowhere else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import uuid
import weakref
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import _get_resource, run_in_thread, utc_now_iso
from core.dynamodb import ddb_safe as _ddb_safe
from core.dynamodb import plain as _plain

logger = logging.getLogger(__name__)

GSI_BY_MEMBER = "by-member"
GSI_BY_RUN_DEADLINE = "by-run-deadline"
MAX_ROOM_MEMBERS = 32
OUTBOX_TTL_S = 7 * 24 * 3600
RUN_NS = uuid.UUID("2b5f1c7e-9a3d-4e6f-8b1c-0d2e4f6a8c1e")
# A worker can crash after creating the row. Its SQS receipt stays hidden for
# up to 240 s (the Lambda queue's visibility), so sweeping earlier would drop
# a turn that was never invoked instead of letting redelivery retry it.
RUN_DISPATCH_TIMEOUT_S = 300
RUN_HEARTBEAT_TIMEOUT_S = 90
#: How long a stopped run may take to report run.ended(aborted) before the backend closes it
#: itself (``request_abort``). The bridge interrupts in well under this.
RUN_ABORT_GRACE_S = 20
RUN_OUTCOMES = frozenset({"completed", "error", "aborted", "ignored", "gate_error", "not_admitted"})
# Room-create is 3 fixed actions (META, owner catalog, IDEMP) + members + agent
# delivery rows. The 32-member worst case is 66; DynamoDB caps a transaction at 100.
_FIXED_CREATE_ACTIONS = 3
_TRANSACT_LIMIT = 100
_SEQ_RETRY_ATTEMPTS = 8
# Full-jitter backoff between lastSeq conflicts: attempt n sleeps uniform(0, min(cap, base * 2**n)),
# so concurrent writers to one room spread out instead of re-colliding (worst case ~1.6 s total).
_SEQ_BACKOFF_BASE_S = 0.02
_SEQ_BACKOFF_CAP_S = 0.5


# One append lane per room per process. Weak values: a room's lock disappears once no
# writer holds or waits on it, so the map stays as small as the rooms being written.
# ponytail: per process only -- the 2-4 backend tasks still race each other on lastSeq
# (the backoff above covers that); a cross-task sequencer if one room outgrows it.
_room_append_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _room_append_lock(room_id: str) -> asyncio.Lock:
    lock = _room_append_locks.get(room_id)
    if lock is None:
        lock = _room_append_locks[room_id] = asyncio.Lock()
    return lock


class MessagingRepoError(Exception):
    pass


class IdempotencyConflict(MessagingRepoError):
    """Same idempotency key, different canonical request."""


class DmPairTaken(MessagingRepoError):
    """Another create already holds this member pair's DM; ``room_id`` names it."""

    def __init__(self, room_id: str):
        super().__init__(room_id)
        self.room_id = room_id


class MessageConflict(MessagingRepoError):
    pass


class MessageForbidden(MessagingRepoError):
    pass


class ExtraConditionFailed(MessageForbidden):
    """One of the caller's own ``extra_conditions`` did not hold when the message committed."""


class MemberLimitExceeded(MessagingRepoError):
    pass


class RoomNotFound(MessagingRepoError):
    pass


class RoomArchived(MessagingRepoError):
    pass


class PinLimitExceeded(MessagingRepoError):
    pass


class ThreadRootMismatch(MessagingRepoError):
    """``thread_root_seq`` does not name ``thread_root_message_id`` in this room."""


# ---------------------------------------------------------------------------
# keys / ids
# ---------------------------------------------------------------------------


def room_pk(room_id: str) -> str:
    return f"ROOM#{room_id}"


def owner_pk(owner_id: str) -> str:
    return f"OWNER#{owner_id}"


def msg_sk(seq: int) -> str:
    return f"MSG#{int(seq):020d}"


def member_sk(kind: str, member_id: str) -> str:
    return f"MEMBER#{kind}#{member_id}"


def member_key(owner_id: str, kind: str, member_id: str) -> str:
    return f"OWNER#{owner_id}#{kind}#{member_id}"


def member_home(member: dict, room_owner: str) -> str:
    """The workspace a member belongs to. A foreign human (a shared room) carries
    ``homeOwnerId``; every other row belongs to the room's CURRENT owner -- not the
    row's own ``ownerId``, which a META-only migration (#2627) left stale."""
    return member.get("homeOwnerId") or room_owner


def human_idemp_sk(user_id: str, client_message_id: str) -> str:
    return f"IDEMP#human#{user_id}#{client_message_id}"


def agent_idemp_sk(event_id: str) -> str:
    return f"IDEMP#AGENT#{event_id}"


def read_sk(user_id: str) -> str:
    return f"READ#human#{user_id}"


def mention_sk(user_id: str, seq: int) -> str:
    return f"MENTION#{user_id}#{seq:020d}"


def delivery_sk(agent_id: str) -> str:
    return f"DELIVERY#AGENT#{agent_id}"


def work_message_sk(seq: int, agent_id: str) -> str:
    return f"WORK#MESSAGE#{int(seq):020d}#AGENT#{agent_id}"


def live_sk(event_id: str, user_id: str) -> str:
    return f"LIVE#{event_id}#USER#{user_id}"


def userstream_sk(user_id: str) -> str:
    return f"USERSTREAM#{user_id}"


def pref_sk(user_id: str) -> str:
    return f"PREF#{user_id}"


def stream_pk(channel_key: str) -> str:
    return f"STREAM#{channel_key}"


def operation_sk(operation_id: str) -> str:
    return f"OPERATION#{operation_id}"


def work_operation_sk(operation_id: str) -> str:
    return f"WORK#OPERATION#{operation_id}"


def netwake_sk() -> str:
    return "NETWAKE"


def work_netwake_sk(ulid: str) -> str:
    return f"WORK#NETWAKE#{ulid}"


def work_admit_sk(seq: int) -> str:
    """Super-agent admission outbox (contracts-v2 §10): one row per human message in a Router room."""
    return f"WORK#ADMIT#{int(seq):020d}"


def work_admit_trigger_sk(trigger_id: str) -> str:
    """Admission outbox for a non-message source (`admission.enqueue_trigger`), under `OWNER#<ws>`."""
    return f"WORK#ADMIT#T#{trigger_id}"


def followup_sk(follow_up_id: str) -> str:
    return f"FOLLOWUP#{follow_up_id}"


def work_followup_sk(agent_id: str, follow_up_id: str) -> str:
    return f"WORK#FOLLOWUP#{agent_id}#{follow_up_id}"


def voice_usage_sk(transaction_id: str) -> str:
    return f"VOICEUSAGE#{transaction_id}"


def work_voice_usage_sk(transaction_id: str) -> str:
    return f"WORK#VOICEUSAGE#{transaction_id}"


async def put_voice_usage_event(owner_id: str, transaction_id: str, event: dict) -> dict:
    """Persist an immutable Metronome event with its FIFO work pointer atomically.

    A prior owner-voice receipt may already occupy ``VOICEUSAGE#``; in that
    case this transaction adds the frozen event to that row. Replays read and
    return the original event, so current plan or changed callback data cannot
    rewrite the billed occurrence.
    """
    import hashlib
    from datetime import UTC, datetime, timedelta

    table = _table()
    key = {"pk": owner_pk(owner_id), "sk": voice_usage_sk(transaction_id)}
    outbox = {
        "pk": key["pk"],
        "sk": work_voice_usage_sk(transaction_id),
        "payload": {"kind": "voice_usage", "ownerId": owner_id, "transactionId": transaction_id},
        "messageGroupId": owner_id,
        "messageDeduplicationId": hashlib.sha256(f"{owner_id}\0{transaction_id}".encode()).hexdigest(),
        "expiresAt": int((datetime.now(UTC) + timedelta(seconds=OUTBOX_TTL_S)).timestamp()),
    }
    source = {
        **key,
        "ownerId": owner_id,
        "voiceUsageEvent": event,
    }
    prior = await _get(**key)
    if prior and prior.get("voiceUsageEvent") is not None:
        work_key = {"pk": key["pk"], "sk": outbox["sk"]}
        if await _get(**work_key):
            return prior
        try:
            await _transact(
                [
                    {
                        "Put": {
                            "TableName": table.name,
                            "Item": _ddb_safe(outbox),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                ]
            )
        except ClientError as exc:
            if not _is_cancelled(exc) or not await _get(**work_key):
                raise
        return prior
    try:
        if prior:
            await _transact(
                [
                    {
                        "Update": {
                            "TableName": table.name,
                            "Key": key,
                            "UpdateExpression": "SET voiceUsageEvent = :event, ownerId = :owner",
                            "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(voiceUsageEvent)",
                            "ExpressionAttributeValues": {":event": event, ":owner": owner_id},
                        }
                    },
                    {
                        "Put": {
                            "TableName": table.name,
                            "Item": _ddb_safe(outbox),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return {**prior, "ownerId": owner_id, "voiceUsageEvent": event}
        await _transact(
            [
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": _ddb_safe(source),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": _ddb_safe(outbox),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )
        return source
    except ClientError as exc:
        if not _is_cancelled(exc):
            raise
        winner = await _get(**key)
        if winner and winner.get("voiceUsageEvent") is not None:
            return winner
        raise


async def mark_voice_usage_ingested(owner_id: str, transaction_id: str) -> None:
    """Record the provider's accepted response after a deduplicated ingest call."""
    await run_in_thread(
        _table().update_item,
        Key={"pk": owner_pk(owner_id), "sk": voice_usage_sk(transaction_id)},
        UpdateExpression="SET metronomeIngestedAt = if_not_exists(metronomeIngestedAt, :at)",
        ConditionExpression="attribute_exists(voiceUsageEvent)",
        ExpressionAttributeValues={":at": utc_now_iso()},
    )


def workspace_sk() -> str:
    return "WORKSPACE"


def workspace_pk(workspace_id: str) -> str:
    return f"WORKSPACE#{workspace_id}"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def voice_run_sk(run_id: str) -> str:
    return f"VOICERUN#{run_id}"


def run_id_for(agent_id: str, message_id: str) -> str:
    """Deterministic per dispatch event so an SQS redelivery lands on the same row."""
    return str(uuid.uuid5(RUN_NS, f"{agent_id}:{message_id}"))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def room_fifo_ids(owner_id: str, room_id: str, agent_id: str, message_id: str) -> tuple[str, str]:
    """(MessageGroupId, MessageDeduplicationId) for a room wake — spec §9."""
    return _sha256(f"room|{owner_id}|{room_id}|{agent_id}"), _sha256(f"message|{message_id}|{agent_id}")


def operation_fifo_ids(owner_id: str, operation_id: str) -> tuple[str, str]:
    return _sha256(f"runtime|{owner_id}"), _sha256(f"operation|{operation_id}")


def deterministic_room_id(owner_id: str, user_id: str, client_room_id: str) -> str:
    """Server room id for a create request. uuid5 over the idempotency triple so
    a replay lands on the SAME ``ROOM#`` partition and the durable IDEMP row can
    live under the spec's ``ROOM#<roomId>/IDEMP#human#…`` key."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-room|{owner_id}|{user_id}|{client_room_id}"))


def dm_pair_sk(*members: tuple[str, str]) -> str:
    """The owner-partition uniqueness row for one DM's member pair, order-free, so
    concurrent creates for the same two members collide in the create transaction."""
    return "DMPAIR#" + _sha256("|".join(sorted(f"{kind}#{member_id}" for kind, member_id in members)))


def vendor_room_id(owner_id: str) -> str:
    """The one platform-managed channel a customer workspace shares with the vendor org."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{owner_id}"))


def feed_room_id(owner_id: str) -> str:
    """The workspace's one feed channel (Activity). Mirrored by the messaging projector
    (apps/infra/lambda/messaging-projector/index.py ``_is_feed_room``): edit both together."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-feed-room|{owner_id}"))


def meeting_room_id(owner_id: str, uid: str) -> str:
    """The channel a calendar invite (by its UID) opens in the owner's workspace."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-meeting|{owner_id}|{uid}"))


def request_hash(*parts: Any) -> str:
    """SHA-256 over canonical JSON request fields."""
    return _sha256(json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str))


def _expires_at() -> int:
    return int(time.time()) + OUTBOX_TTL_S


# ---------------------------------------------------------------------------
# table access
# ---------------------------------------------------------------------------


def _table():
    if not settings.MESSAGING_TABLE:
        raise MessagingRepoError("MESSAGING_TABLE is not configured")
    return _get_resource().Table(settings.MESSAGING_TABLE)


async def _get(pk: str, sk: str) -> dict | None:
    table = _table()
    response = await run_in_thread(table.get_item, Key={"pk": pk, "sk": sk}, ConsistentRead=True)
    item = response.get("Item")
    return _plain(item) if item else None


async def ensure_workspace_id(owner_id: str) -> str:
    """The owner's opaque channel-path segment, minted once (spec §3)."""
    row = await _get(owner_pk(owner_id), workspace_sk())
    if row is not None:
        return row["workspaceId"]
    candidate = str(uuid.uuid4())
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "pk": owner_pk(owner_id),
                "sk": workspace_sk(),
                "workspaceId": candidate,
                "ownerId": owner_id,
                "createdAt": utc_now_iso(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return candidate
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    row = await _get(owner_pk(owner_id), workspace_sk())
    if row is None:
        raise MessagingRepoError("workspace id allocation raced and no row exists")
    return row["workspaceId"]


async def workspace_id_for_meta(pk: str, sk: str, meta: dict) -> str:
    """The row's workspaceId, stamped lazily for rows that predate the id (roll-window rows, fixtures)."""
    workspace_id = meta.get("workspaceId")
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    workspace_id = await ensure_workspace_id(meta["ownerId"])
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET workspaceId = :ws",
            ConditionExpression="attribute_not_exists(workspaceId)",
            ExpressionAttributeValues={":ws": workspace_id},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    meta["workspaceId"] = workspace_id
    return workspace_id


async def scan_growth_rows() -> tuple[list[dict], list[dict]]:
    """``(messages, rooms)`` — every ``MSG#`` row and every room ``META`` row.

    Fleet aggregates only (the admin growth read, ``core/services/ops/growth.py``).
    The projection deliberately omits ``text`` and ``mentions``: message bodies
    never leave this table for a metrics read. ``pk`` is projected because
    ``STREAM#<key>`` rows also carry ``sk = "META"`` — only ``ROOM#`` ones are rooms.

    ponytail: full scan, fine at 10^3 rows; move to a GSI or PostHog when rooms
    pass 10^4.
    """
    table = _table()
    kwargs: dict = {
        "FilterExpression": "begins_with(#sk, :msg) OR #sk = :meta",
        "ProjectionExpression": "#pk, #sk, #ownerId, #roomId, #author, #createdAt, #dispatchTargets",
        "ExpressionAttributeNames": {
            "#pk": "pk",
            "#sk": "sk",
            "#ownerId": "ownerId",
            "#roomId": "roomId",
            "#author": "author",
            "#createdAt": "createdAt",
            "#dispatchTargets": "dispatchTargets",
        },
        "ExpressionAttributeValues": {":msg": "MSG#", ":meta": "META"},
    }
    messages: list[dict] = []
    rooms: list[dict] = []
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        for item in page.get("Items", []):
            pk, sk = str(item.get("pk") or ""), str(item.get("sk") or "")
            if sk.startswith("MSG#"):
                messages.append(_plain(item))
            elif pk.startswith("ROOM#"):
                rooms.append(_plain(item))
        last = page.get("LastEvaluatedKey")
        if not last:
            return messages, rooms
        kwargs["ExclusiveStartKey"] = last


async def _transact(items: list[dict]) -> None:
    """Run one TransactWriteItems. Raises ``ClientError`` on cancellation so the
    caller can triage the reasons."""
    if len(items) > _TRANSACT_LIMIT:
        raise MessagingRepoError(f"transaction has {len(items)} actions (limit {_TRANSACT_LIMIT})")
    table = _table()
    # Every caller's values, not just ours: a shared session's tool call input is free JSON, and
    # one `"scale": 0.6` made boto3 refuse the whole batch -- which the Mac then resent forever
    # (prod 2026-10-12..13, ~16k failures in three hours, each session's history frozen there).
    await run_in_thread(table.meta.client.transact_write_items, TransactItems=_ddb_safe(items))


def _is_cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"


is_cancelled = _is_cancelled  # the public name for callers outside this module


async def _paginate(**kwargs: Any) -> list[dict]:
    """Drain one table query, following ``LastEvaluatedKey`` and honouring ``Limit``."""
    table = _table()
    limit = kwargs.get("Limit")
    items: list[dict] = []
    while True:
        response = await run_in_thread(table.query, **kwargs)
        items.extend(_plain(response.get("Items", [])))
        last = response.get("LastEvaluatedKey")
        if not last or (limit is not None and len(items) >= limit):
            return items[:limit] if limit is not None else items
        kwargs["ExclusiveStartKey"] = last


async def _query(
    pk: str,
    *,
    begins_with: str | None = None,
    ascending: bool = True,
    limit: int | None = None,
    between: tuple[str, str] | None = None,
) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    cond = Key("pk").eq(pk)
    if begins_with is not None:
        cond = cond & Key("sk").begins_with(begins_with)
    elif between is not None:
        cond = cond & Key("sk").between(*between)
    kwargs: dict = {"KeyConditionExpression": cond, "ConsistentRead": True, "ScanIndexForward": ascending}
    if limit is not None:
        kwargs["Limit"] = limit
    return await _paginate(**kwargs)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def get_room(room_id: str) -> dict | None:
    return await _get(room_pk(room_id), "META")


async def get_member(room_id: str, kind: str, member_id: str) -> dict | None:
    return await _get(room_pk(room_id), member_sk(kind, member_id))


async def list_members(room_id: str) -> list[dict]:
    return await _query(room_pk(room_id), begins_with="MEMBER#")


async def has_foreign_members(room_id: str, owner_id: str) -> bool:
    """The room is shared with another organization (#2644): some member's home
    workspace is not ``owner_id``, the room's owner."""
    return any(member_home(m, owner_id) != owner_id for m in await list_members(room_id))


async def get_message(room_id: str, seq: int) -> dict | None:
    return await _get(room_pk(room_id), msg_sk(seq))


async def get_message_by_id(room_id: str, message_id: str) -> dict | None:
    """Strongly find a message by canonical ID inside one room.

    ponytail: founder rooms are small; add a message-id index only after a
    measured room makes this partition scan material.
    """
    for row in await _query(room_pk(room_id), begins_with="MSG#"):
        if row.get("messageId") == message_id:
            return row
    return None


async def get_agent_delivery(room_id: str, agent_id: str) -> dict | None:
    return await _get(room_pk(room_id), delivery_sk(agent_id))


async def ensure_agent_delivery(room_id: str, agent_id: str, last_seq: int) -> None:
    """Create an agent's delivery cursor at ``last_seq`` if it has none. For the feed, whose agents
    are authors and so get no cursor from a seat: their first wake starts it just before itself."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": room_pk(room_id), "sk": delivery_sk(agent_id), "agentId": agent_id, "lastSeq": int(last_seq)},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_read_row(room_id: str, user_id: str) -> dict | None:
    return await _get(room_pk(room_id), read_sk(user_id))


async def list_read_cursors(room_id: str) -> list[dict]:
    """Every human's READ row in the room."""
    return await _query(room_pk(room_id), begins_with=read_sk(""))


async def list_agent_deliveries(room_id: str) -> list[dict]:
    """Every agent's DELIVERY cursor row in the room."""
    return await _query(room_pk(room_id), begins_with=delivery_sk(""))


async def get_read_cursor(room_id: str, user_id: str) -> int:
    row = await get_read_row(room_id, user_id)
    return int(row["lastReadSeq"]) if row else 0


# ---------------------------------------------------------------------------
# exact room-list counts
#
# Every top-level message takes the next dense ordinal (``META.topCount``,
# stamped as ``MSG.topOrdinal``); a thread reply stamps the current one without
# bumping it. A cursor stores the ordinal of the seq it points at, so
# ``unreadCount = topCount - max(READ.topOrdinal, MEMBER.joinedTopOrdinal)``
# needs no read the room list does not already make. Own messages drop out
# because a human's top-level post moves their own cursor to it.
#
# A missing ordinal reads as 0 and the count clamps at 0, so a room written
# before ordinals existed shows 0 unread (never a crash or a negative) until
# the one-off 2026-09-24 room-ordinals backfill (run per env, not kept in the
# repo) stamps it. No legacy read path.
# ---------------------------------------------------------------------------

COUNT_CAP = 99


def _top_count(meta: dict) -> int:
    return int(meta.get("topCount") or 0)


async def _ordinal_at(meta: dict, seq: int) -> int:
    """The top-level ordinal of the room position ``seq`` (0 = before everything)."""
    if seq == int(meta["lastSeq"]):
        return _top_count(meta)
    if seq <= 0:
        return 0
    row = await get_message(meta["roomId"], seq)
    return int((row or {}).get("topOrdinal") or 0)


def read_floor(member: dict, read: dict | None) -> int:
    return max(int((read or {}).get("lastReadSeq", 0)), int(member.get("joinedSeq", 0)))


def unread_count(meta: dict, member: dict, read: dict | None) -> int:
    """Top-level messages above the reader's floor, capped at ``COUNT_CAP``."""
    floor = max(int((read or {}).get("topOrdinal") or 0), int(member.get("joinedTopOrdinal") or 0))
    return min(COUNT_CAP, max(0, _top_count(meta) - floor))


async def mention_count(room_id: str, user_id: str, floor: int) -> int:
    """``MENTION#`` rows above ``floor``, capped. Callers skip a read room."""
    from boto3.dynamodb.conditions import Key

    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("pk").eq(room_pk(room_id))
        & Key("sk").between(mention_sk(user_id, floor + 1), mention_sk(user_id, 10**19)),
        Select="COUNT",
        ConsistentRead=True,
        Limit=COUNT_CAP,
    )
    return min(COUNT_CAP, int(response.get("Count", 0)))


async def get_agent_idempotency(owner_id: str, event_id: str) -> dict | None:
    return await _get(owner_pk(owner_id), agent_idemp_sk(event_id))


async def list_messages(room_id: str, after_seq: int, limit: int) -> tuple[list[dict], int | None]:
    """Ascending ``MSG#`` page strictly after ``after_seq``; ``1 <= limit <= 100``."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if after_seq < 0:
        raise ValueError("afterSeq must be >= 0")
    rows = await _query(room_pk(room_id), between=(msg_sk(after_seq + 1), msg_sk(10**19)), limit=limit + 1)
    page = rows[:limit]
    next_after = page[-1]["messageSeq"] if len(rows) > limit else None
    return page, next_after


async def list_messages_between(room_id: str, from_seq: int, through_seq: int) -> list[dict]:
    """Ascending ``MSG#`` rows with ``from_seq <= seq <= through_seq``."""
    if through_seq < from_seq:
        return []
    return await _query(room_pk(room_id), between=(msg_sk(from_seq), msg_sk(through_seq)))


async def list_messages_before(room_id: str, before_seq: int, limit: int) -> list[dict]:
    """The ``limit`` newest rows with ``seq < before_seq``, returned oldest→newest."""
    if before_seq <= 1 or limit <= 0:
        return []
    rows = await _query(room_pk(room_id), between=(msg_sk(1), msg_sk(before_seq - 1)), ascending=False, limit=limit)
    return list(reversed(rows))


_THREAD_SCAN_LIMIT = 500


async def list_thread(room_id: str, root_message_id: str, before_seq: int) -> list[dict]:
    """A thread's root and replies with ``seq < before_seq``, oldest→newest.

    ponytail: reads only the newest ``_THREAD_SCAN_LIMIT`` messages before ``before_seq`` (it runs
    once per candidate agent), so thread messages older than that are left out; add a thread index
    once a measured room makes it material.
    """
    if before_seq <= 1:
        return []
    rows = await _query(
        room_pk(room_id),
        between=(msg_sk(1), msg_sk(before_seq - 1)),
        ascending=False,
        limit=_THREAD_SCAN_LIMIT,
    )
    return [r for r in reversed(rows) if root_message_id in (r.get("messageId"), r.get("threadRootMessageId"))]


async def list_messages_before_page(room_id: str, before_seq: int, limit: int) -> tuple[list[dict], int | None]:
    """Return a newest-first window plus the cursor for older rows."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if before_seq <= 1:
        return [], None
    rows = await _query(room_pk(room_id), between=(msg_sk(1), msg_sk(before_seq - 1)), ascending=False, limit=limit + 1)
    page = list(reversed(rows[:limit]))
    next_before = page[0]["messageSeq"] if len(rows) > limit else None
    return page, next_before


async def list_rooms_for_member(owner_id: str, kind: str, member_id: str) -> list[dict]:
    """DISCOVERY ONLY: membership rows from the ``by-member`` GSI, joined with a
    strongly consistent batch read of each room's ``META``. Callers must still
    recheck the base membership row before trusting a room."""
    from boto3.dynamodb.conditions import Key

    table = _table()
    rows = await _paginate(
        IndexName=GSI_BY_MEMBER,
        KeyConditionExpression=Key("memberKey").eq(member_key(owner_id, kind, member_id)),
    )
    if not rows:
        return []
    metas: dict[str, dict] = {}
    keys = [{"pk": r["pk"], "sk": "META"} for r in rows]
    for i in range(0, len(keys), 100):
        request = {table.name: {"Keys": keys[i : i + 100], "ConsistentRead": True}}
        while request:
            response = await run_in_thread(table.meta.client.batch_get_item, RequestItems=request)
            for item in response.get("Responses", {}).get(table.name, []):
                item = _plain(item)
                metas[item["roomId"]] = item
            request = response.get("UnprocessedKeys") or {}
    out = []
    for r in rows:
        meta = metas.get(r["roomId"])
        if meta is not None and member_home(r, meta["ownerId"]) == owner_id:
            out.append({"member": r, "meta": meta})
    return out


async def get_messages_by_keys(keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """Strongly consistent batch read of canonical MSG rows by (room_id, seq).
    Missing rows are absent from the result."""
    table = _table()
    wanted = [{"pk": room_pk(r), "sk": msg_sk(s)} for r, s in dict.fromkeys(keys)]
    out: dict[tuple[str, int], dict] = {}
    for i in range(0, len(wanted), 100):
        request = {table.name: {"Keys": wanted[i : i + 100], "ConsistentRead": True}}
        while request:
            response = await run_in_thread(table.meta.client.batch_get_item, RequestItems=request)
            for item in response.get("Responses", {}).get(table.name, []):
                item = _plain(item)
                out[(item["pk"].removeprefix("ROOM#"), int(item["sk"].removeprefix("MSG#")))] = item
            request = response.get("UnprocessedKeys") or {}
    return out


# ---------------------------------------------------------------------------
# room creation
# ---------------------------------------------------------------------------


def _member_item(room_id: str, owner_id: str, m: dict, joined_seq: int, joined_ordinal: int, now: str) -> dict:
    home = m.get("homeOwnerId") or owner_id
    return {
        "pk": room_pk(room_id),
        "sk": member_sk(m["kind"], m["id"]),
        "memberKey": member_key(home, m["kind"], m["id"]),
        "memberSort": room_pk(room_id),
        "roomId": room_id,
        "ownerId": owner_id,
        "homeOwnerId": home,
        "kind": m["kind"],
        "id": m["id"],
        "displayName": m.get("displayName") or m["id"],
        "mentionHandle": m.get("mentionHandle"),
        "role": m["role"],
        "listenMode": m.get("listenMode"),
        "joinedAt": now,
        "joinedSeq": joined_seq,
        "joinedTopOrdinal": joined_ordinal,
        # "user" once a room admin sets this member; system promotions never override it.
        **({"modeSetBy": m["modeSetBy"]} if m.get("modeSetBy") else {}),
    }


async def create_room(
    *,
    owner_id: str,
    room_id: str,
    client_room_id: str,
    creator_user_id: str,
    name: str | None,
    kind: str,
    members: list[dict],
    req_hash: str,
    extra: dict | None = None,
    dm_pair: str | None = None,
    dm_pair_replaces: str | None = None,
) -> dict:
    """One transaction: META + owner catalog + IDEMP + every member row + one
    delivery cursor per agent (at seq 0). ``members`` must already include the
    creator (role ``owner``). Returns the META item; a byte-identical replay
    returns the existing META, a different one raises ``IdempotencyConflict``.
    ``dm_pair`` (``dm_pair_sk``) adds a conditional uniqueness row, so a second
    create for the same pair raises ``DmPairTaken`` instead of a second DM;
    ``dm_pair_replaces`` takes over a row that still names that (stale) room."""
    if len(members) > MAX_ROOM_MEMBERS:
        raise MemberLimitExceeded(f"rooms hold at most {MAX_ROOM_MEMBERS} members")
    agents = [m for m in members if m["kind"] == "agent"]
    actions = _FIXED_CREATE_ACTIONS + len(members) + len(agents) + (dm_pair is not None)
    assert actions <= _TRANSACT_LIMIT, f"room create needs {actions} actions"

    now = utc_now_iso()
    table_name = _table().name
    workspace_id = await ensure_workspace_id(owner_id)
    meta = {
        "pk": room_pk(room_id),
        "sk": "META",
        "roomId": room_id,
        "ownerId": owner_id,
        "name": name,
        "kind": kind,
        "revision": 1,
        "lastSeq": 0,
        "lastMessageAt": None,
        # Present-and-null, not absent: the room-list preview (see last_message_preview).
        "lastMessage": None,
        "topCount": 0,
        "deliveryKey": str(uuid.uuid4()),
        "workspaceId": workspace_id,
        "createdAt": now,
        "updatedAt": now,
        **_ddb_safe(extra or {}),
    }
    items: list[dict] = [
        {"Put": {"TableName": table_name, "Item": meta, "ConditionExpression": "attribute_not_exists(pk)"}},
        {
            "Put": {
                "TableName": table_name,
                "Item": {
                    "pk": owner_pk(owner_id),
                    "sk": room_pk(room_id),
                    "roomId": room_id,
                    "name": name,
                    "kind": kind,
                    "createdAt": now,
                    "updatedAt": now,
                },
            }
        },
        {
            "Put": {
                "TableName": table_name,
                "Item": {
                    "pk": room_pk(room_id),
                    "sk": human_idemp_sk(creator_user_id, client_room_id),
                    "requestHash": req_hash,
                    "roomId": room_id,
                    "createdAt": now,
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        },
    ]
    if dm_pair is not None:
        items.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": {"pk": owner_pk(owner_id), "sk": dm_pair, "roomId": room_id, "createdAt": now},
                    **(
                        {
                            "ConditionExpression": "attribute_not_exists(pk) OR roomId = :stale",
                            "ExpressionAttributeValues": {":stale": dm_pair_replaces},
                        }
                        if dm_pair_replaces
                        else {"ConditionExpression": "attribute_not_exists(pk)"}
                    ),
                }
            }
        )
    for m in members:
        items.append({"Put": {"TableName": table_name, "Item": _member_item(room_id, owner_id, m, 0, 0, now)}})
    for a in agents:
        items.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": {"pk": room_pk(room_id), "sk": delivery_sk(a["id"]), "agentId": a["id"], "lastSeq": 0},
                }
            }
        )
    try:
        await _transact(items)
        return meta
    except ClientError as exc:
        if not _is_cancelled(exc):
            raise
    existing = await _get(room_pk(room_id), human_idemp_sk(creator_user_id, client_room_id))
    if existing is None:
        pair = await _get(owner_pk(owner_id), dm_pair) if dm_pair is not None else None
        if pair is not None:
            raise DmPairTaken(pair["roomId"])
        raise MessagingRepoError("room create cancelled without a durable receipt")
    if existing.get("requestHash") != req_hash:
        raise IdempotencyConflict("clientRoomId was already used with a different request")
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    return meta


# ---------------------------------------------------------------------------
# message creation
# ---------------------------------------------------------------------------


def _work_admit_item(table_name: str, owner_id: str, room_id: str, message_id: str, seq: int) -> dict:
    return {
        "Put": {
            "TableName": table_name,
            "Item": {
                "pk": room_pk(room_id),
                "sk": work_admit_sk(seq),
                "payload": {"kind": "admit", "roomId": room_id, "throughSeq": seq},
                "messageGroupId": _sha256(f"admit|{owner_id}|{room_id}"),
                "messageDeduplicationId": _sha256(f"admit|{message_id}"),
                "expiresAt": _expires_at(),
            },
        }
    }


async def put_admit_trigger(owner_id: str, trigger_id: str) -> None:
    """The one `WORK#ADMIT#` outbox row for a non-message trigger (its TRIG row is already written)."""
    item = {
        "pk": owner_pk(owner_id),
        "sk": work_admit_trigger_sk(trigger_id),
        "payload": {"kind": "admit_trigger", "ownerId": owner_id, "triggerId": trigger_id},
        "messageGroupId": _sha256(f"admit|{owner_id}"),
        "messageDeduplicationId": _sha256(f"admit-trigger|{owner_id}|{trigger_id}"),
        "expiresAt": _expires_at(),
    }
    await run_in_thread(_table().put_item, Item=item)


def _work_message_item(table_name: str, owner_id: str, room_id: str, message_id: str, seq: int, agent_id: str) -> dict:
    group, dedup = room_fifo_ids(owner_id, room_id, agent_id, message_id)
    return {
        "Put": {
            "TableName": table_name,
            "Item": {
                "pk": room_pk(room_id),
                "sk": work_message_sk(seq, agent_id),
                "payload": {"kind": "room_dispatch", "roomId": room_id, "agentId": agent_id, "throughSeq": seq},
                "messageGroupId": group,
                "messageDeduplicationId": dedup,
                "expiresAt": _expires_at(),
            },
        }
    }


# Markdown -> one plain line. A copy of the projector's ``_plain_preview``
# (apps/infra/lambda/messaging-projector/index.py); keep the two identical —
# tests/unit/services/test_messaging_last_message.py compares them.
_PLAIN_RULES = (
    (re.compile(r"^\s*```.*$", re.M), ""),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.M), ""),
    (re.compile(r"^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M), ""),
    (re.compile(r"\*\*|__|~~|(?<!\\)`"), ""),
    # The composer's escapes last, so a typed `x` keeps its backticks and **x** its stars.
    (re.compile(r"\\([*_~`])"), r"\1"),
    (re.compile(r"^(\s*)\\([#>+=-])", re.M), r"\1\2"),
    (re.compile(r"^(\s*\d+)\\([.)])", re.M), r"\1\2"),
    (re.compile(r"\s+"), " "),
)
PREVIEW_CHARS = 140
_PREVIEW_SCAN_ROWS = 50


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
    (``3\\*4``, ``\\_\\_init\\_\\_``) so it shows literally; agents get it back
    clean. Code is left as is."""
    return _AGENT_ESCAPES.sub(_agent_unescape, text) if "\\" in text else text


def _plain_preview(text: str) -> str:
    text = text[:1000]
    for pattern, repl in _PLAIN_RULES:
        text = pattern.sub(repl, text)
    return text.strip()


def last_message_preview(message: dict) -> dict:
    """``META.lastMessage`` for a (non-deleted) message: its current public
    text as one plain line of at most ``PREVIEW_CHARS``. ``@tokens`` stay as text."""
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


async def latest_preview_row(room_id: str, before_seq: int, limit: int | None = _PREVIEW_SCAN_ROWS) -> dict | None:
    """The newest non-deleted top-level ``MSG`` below ``before_seq``, looking at
    most ``limit`` rows back (``None`` = the whole room)."""
    if before_seq <= 1:
        return None
    rows = await _query(room_pk(room_id), between=(msg_sk(1), msg_sk(before_seq - 1)), ascending=False, limit=limit)
    return next((r for r in rows if not r.get("threadRootMessageId") and not r.get("deletedAt")), None)


def _unchanged_message_check(table_name: str, room_id: str, row: dict) -> dict:
    """Fence a preview built from ``row`` against a concurrent edit or delete of it."""
    return {
        "ConditionCheck": {
            "TableName": table_name,
            "Key": {"pk": room_pk(room_id), "sk": msg_sk(row["messageSeq"])},
            "ConditionExpression": "attribute_not_exists(messageRevision) OR messageRevision = :rev",
            "ExpressionAttributeValues": {":rev": int(row.get("messageRevision", 0))},
        }
    }


async def create_message(
    *,
    room_id: str,
    owner_id: str,
    idemp_pk: str,
    idemp_sk: str,
    req_hash: str,
    author: dict,
    text: str,
    mentions: list[dict],
    reply_to_message_id: str | None,
    thread_root_message_id: str | None,
    run_id: str | None,
    dispatch_targets: list[dict],
    source_agent_id: str | None,
    client_message_id: str | None,
    attachments: list[dict] | None = None,
    thread_root_seq: int | None = None,
    agent_hop: int | None = None,
    silent: bool = False,
    expected_room_revision: int | None = None,
    acting_human_id: str | None = None,
    extra_conditions: list[dict] | None = None,
    via: str | None = None,
    text_reply_key: str | None = None,
    session_mentions: list[str] | None = None,
    session_target_labels: dict[str, str] | None = None,
    addressing: dict | None = None,
    in_reply_to: str | None = None,
    progress: bool = False,
    post_reason: str | None = None,
    attrs: dict | None = None,
    author_member_check: bool = True,
    admit: bool = False,
) -> tuple[dict, bool]:
    """Strong-read ``META.lastSeq`` then conditionally transact: bump META, put
    the immutable ``MSG``, put the ``IDEMP`` receipt, put one ``WORK#MESSAGE``
    outbox row per dispatch target. The transaction also carries a
    ``ConditionCheck`` on the author's own ``MEMBER`` row — a caller who
    passed its membership pre-check (``require_room_member`` /
    ``get_member``) can still be removed before this transaction commits;
    without this the message would land anyway. Retries the sequence race;
    a durable replay returns the original message; a changed body raises
    ``IdempotencyConflict``; losing the author-membership race raises
    ``MessageForbidden`` (not retried — the author is gone, not racing).
    ``author_member_check=False`` drops that check for an author who is not a member by design:
    an agent writing the workspace feed (its authors are the roster, its members only people).

    Returns ``(message, replayed)``. ``replayed`` is True when a durable
    receipt already existed and NOTHING was written — the agent path is built
    around retries, so a caller with post-commit side effects that must happen
    once per message (telemetry, latency measurement) has to skip them here.
    """
    prior = await _get(idemp_pk, idemp_sk)
    if prior is not None:
        return await _replay_or_conflict(prior, req_hash), True

    table_name = _table().name
    target_ids = [t["agentId"] for t in dispatch_targets]
    # A platform notice (author kind "system") has no MEMBER row to check.
    system = author["kind"] == "system"
    checked = (not system or acting_human_id) and author_member_check
    # Writers to one room in this process take turns here instead of racing for the
    # same lastSeq in DynamoDB; only writers on OTHER backend tasks still conflict.
    async with _room_append_lock(room_id):
        for attempt in range(_SEQ_RETRY_ATTEMPTS):
            meta = await get_room(room_id)
            if meta is None or meta.get("deletedAt"):
                raise RoomNotFound(room_id)
            if meta.get("archivedAt"):
                raise RoomArchived(room_id)
            if expected_room_revision is not None and int(meta["revision"]) != expected_room_revision:
                raise MessageConflict("Room membership changed; recheck access.")
            expected = int(meta["lastSeq"])
            seq = expected + 1
            top = thread_root_message_id is None
            # A reply shares the current ordinal; only a top-level message takes a new one.
            ordinal = _top_count(meta) + (1 if top else 0)
            now = utc_now_iso()
            message_id = str(uuid.uuid4())
            message = {
                "pk": room_pk(room_id),
                "sk": msg_sk(seq),
                "messageId": message_id,
                "roomId": room_id,
                "ownerId": owner_id,
                "messageSeq": seq,
                "roomRevision": int(meta["revision"]),
                "author": author,
                "text": text,
                "mentions": mentions,
                "replyToMessageId": reply_to_message_id,
                "threadRootMessageId": thread_root_message_id,
                "createdAt": now,
                "runId": run_id,
                "dispatchTargets": dispatch_targets,
                "sourceAgentId": source_agent_id,
                "clientMessageId": client_message_id,
                "requestHash": req_hash,
                "attachments": attachments or [],
                "topOrdinal": ordinal,
            }
            if via:
                message["via"] = via
            if text_reply_key:
                # A member's text posted to the room: where an agent's reply is texted back (text_relay).
                message["textReplyKey"] = text_reply_key
            if acting_human_id:
                # The accountable human behind a system-authored post (hosted MCP receipts read it).
                message["actorUserId"] = acting_human_id
            if session_mentions:
                # A reference, like a document link: it never wakes the session (owner ruling 2026-10-02).
                message["sessionMentions"] = session_mentions
            if session_target_labels:
                message["sessionTargetLabels"] = session_target_labels
            if silent:
                # An advisory line: the projector files no Activity and sends no push for it.
                message["silent"] = True
            if progress:
                # A mid-run progress line: clients keep the run card running under it.
                message["progress"] = True
            if post_reason:
                # A feed post's "Why I created this post" line.
                message["postReason"] = post_reason
            if attrs:
                # Super-agent message attrs (taskRefs, specialist, routedTo, forwardedFrom, kind); absent on legacy posts.
                message.update(attrs)
            if agent_hop is not None:
                # The agent-reply cap reads the trigger's hop (messaging.create_agent_message).
                message["agentHop"] = agent_hop
            if addressing is not None:
                # Who the text spoke to (messaging.address): Jev's to-people rule reads it at dispatch.
                message["addressing"] = addressing
            if in_reply_to:
                # The human message an agent reply answers: the `to` edge Jev sees on agent lines.
                message["inReplyTo"] = in_reply_to
            if thread_root_message_id and thread_root_seq:
                # The projector reads the root by this key instead of scanning the room.
                message["threadRootSeq"] = int(thread_root_seq)
            meta_values: dict[str, Any] = {":seq": seq, ":expected": expected, ":now": now, ":top": ordinal}
            if expected_room_revision is not None:
                meta_values[":room_revision"] = expected_room_revision
            meta_set = "SET lastSeq = :seq, lastMessageAt = :now, updatedAt = :now, topCount = :top"
            if top:
                # The room-list preview follows top-level messages only; thread replies
                # change neither the preview nor the sidebar order.
                meta_set += ", lastMessage = :preview"
                meta_values[":preview"] = last_message_preview(message)
            items: list[dict] = [
                {
                    "Update": {
                        "TableName": table_name,
                        "Key": {"pk": room_pk(room_id), "sk": "META"},
                        "UpdateExpression": meta_set,
                        # An archive or delete committing after the read above cancels this; the retry sees it.
                        "ConditionExpression": (
                            "lastSeq = :expected AND attribute_not_exists(archivedAt) AND attribute_not_exists(deletedAt)"
                            + (" AND revision = :room_revision" if expected_room_revision is not None else "")
                        ),
                        "ExpressionAttributeValues": meta_values,
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": table_name,
                        "Key": {
                            "pk": room_pk(room_id),
                            "sk": member_sk("human", acting_human_id)
                            if acting_human_id
                            else member_sk(author["kind"], author["id"]),
                        },
                        "ConditionExpression": "attribute_exists(sk)",
                    }
                },
                {"Put": {"TableName": table_name, "Item": message, "ConditionExpression": "attribute_not_exists(sk)"}},
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": idemp_pk,
                            "sk": idemp_sk,
                            "requestHash": req_hash,
                            "roomId": room_id,
                            "messageId": message_id,
                            "messageSeq": seq,
                            "createdAt": now,
                        },
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
            ]
            if not checked:
                del items[1]  # the author MEMBER check
            source_condition_start = len(items)
            items.extend(extra_conditions or [])
            if admit:  # a Router room: one admission row; the targets stay on the MSG row for reference
                items.append(_work_admit_item(table_name, owner_id, room_id, message_id, seq))
            else:
                items.extend(_work_message_item(table_name, owner_id, room_id, message_id, seq, a) for a in target_ids)
            # One tiny row per @mentioned human (members only, so <= MAX_ROOM_MEMBERS);
            # with the agent targets that is <= 32 + 4 fixed + 1 READ + 1 thread root, well under 100.
            mentioned = {m["id"] for m in mentions if m.get("kind") == "human"}
            accountable_human_id = author["id"] if author["kind"] == "human" else acting_human_id
            mentioned.discard(accountable_human_id)
            items.extend(
                {"Put": {"TableName": table_name, "Item": {"pk": room_pk(room_id), "sk": mention_sk(uid, seq)}}}
                for uid in sorted(mentioned)
            )
            participants = mentioned | ({accountable_human_id} if accountable_human_id else set())
            root_index = None
            if thread_root_message_id and thread_root_seq and participants:
                # The thread's humans (Activity ``thread_reply`` + notify recipients), kept on
                # the root: one small ADD instead of the projector scanning the room per reply.
                # ``threadTouchedAt`` tells the projector this root MODIFY is not an edit.
                root_index = len(items)
                items.append(
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"pk": room_pk(room_id), "sk": msg_sk(int(thread_root_seq))},
                            "UpdateExpression": "ADD threadParticipants :p SET threadTouchedAt = :now",
                            "ConditionExpression": "messageId = :root",
                            "ExpressionAttributeValues": {
                                ":p": participants,
                                ":root": thread_root_message_id,
                                ":now": now,
                            },
                        }
                    }
                )
            if top and accountable_human_id:
                # Posting marks the room read for its accountable human (Slack's rule).
                # This includes authenticated delegated system posts. Unconditional and
                # still monotonic: nothing can point past the seq this commit creates.
                items.append(
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"pk": room_pk(room_id), "sk": read_sk(accountable_human_id)},
                            "UpdateExpression": "SET lastReadSeq = :seq, topOrdinal = :top, userId = :u, updatedAt = :now",
                            "ExpressionAttributeValues": {
                                ":seq": seq,
                                ":top": ordinal,
                                ":u": accountable_human_id,
                                ":now": now,
                            },
                        }
                    }
                )
            session = meta.get("session")
            session_at = None
            if session:
                # A session's chat takes no post, from a person or an agent, once its owner removed
                # the session (`local_sessions.remove_session`), whatever of its cleanup is still owed.
                session_key = {"pk": f"LOCALSESSION#{meta['ownerId']}#{session['sessionId']}", "sk": "META"}
                # The session posting into its own chat already checks this row (an extra
                # condition), and one transaction may name an item once: that check carries it.
                removed = "attribute_not_exists(removedAt)"
                session_at = next(
                    (i for i, item in enumerate(items) if item.get("ConditionCheck", {}).get("Key") == session_key),
                    None,
                )
                if session_at is None:
                    session_at = len(items)
                    items.append(
                        {
                            "ConditionCheck": {
                                "TableName": table_name,
                                "Key": session_key,
                                "ConditionExpression": removed,
                            }
                        }
                    )
                else:
                    check = items[session_at]["ConditionCheck"]
                    items[session_at] = {
                        "ConditionCheck": {
                            **check,
                            "ConditionExpression": f"({check['ConditionExpression']}) AND {removed}",
                        }
                    }
            try:
                await _transact(items)
                return _plain(message), False
            except ClientError as exc:
                if not _is_cancelled(exc):
                    raise
                reasons = exc.response.get("CancellationReasons", [])
                if (
                    session_at is not None
                    and len(reasons) > session_at
                    and reasons[session_at].get("Code") == "ConditionalCheckFailed"
                    and ((await _get(session_key["pk"], "META")) or {}).get("removedAt")
                ):
                    raise RoomNotFound(room_id) from exc
                if checked and len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                    # The author's own MEMBER row is gone: not a sequence race, don't retry.
                    raise MessageForbidden("Only a current room member can send this message.") from exc
                if any(
                    len(reasons) > i and reasons[i].get("Code") == "ConditionalCheckFailed"
                    for i in range(source_condition_start, source_condition_start + len(extra_conditions or []))
                ):
                    raise ExtraConditionFailed("Native session source is no longer authorized.") from exc
                if (
                    root_index is not None
                    and len(reasons) > root_index
                    and reasons[root_index].get("Code") == ("ConditionalCheckFailed")
                ):
                    # The seq the caller validated does not hold that root: a caller bug, not a race.
                    raise ThreadRootMismatch(thread_root_message_id) from exc
            prior = await _get(idemp_pk, idemp_sk)
            if prior is not None:
                return await _replay_or_conflict(prior, req_hash), True
            # Sequence race (or a same-instant duplicate seq) — reread and retry.
            logger.info("messaging: sequence conflict on room=%s attempt=%d", room_id, attempt + 1)
            # ponytail: one hot room is still serialized on one META.lastSeq counter; move to
            # per-room sequencers if a room ever needs more than ~50 msg/s.
            if attempt + 1 < _SEQ_RETRY_ATTEMPTS:
                await asyncio.sleep(random.uniform(0, min(_SEQ_BACKOFF_CAP_S, _SEQ_BACKOFF_BASE_S * 2**attempt)))
    raise MessagingRepoError(f"could not append to room {room_id} after {_SEQ_RETRY_ATTEMPTS} attempts")


async def update_message(
    owner_id: str, room_id: str, seq: int, user_id: str, operation_id: str, expected_revision: int, text: str | None
) -> dict:
    """Change the public projection, retaining the original request for runtime work.

    The durable receipt makes an uncertain retry safe, including after another edit.
    Neither message sequence nor dispatch/work rows change. When the message is the
    room's ``META.lastMessage`` the same transaction refreshes the preview (edit) or
    replaces it with the newest live top-level message (delete), so a deleted
    message's text is never readable from META. A newer message landing in between
    cancels the transaction and it retries once against the fresh META.
    """
    for attempt in range(2):
        message = await get_message(room_id, seq)
        meta = await get_room(room_id)
        member = await get_member(room_id, "human", user_id)
        if (
            not message
            or not meta
            or meta.get("ownerId") != owner_id
            or not member
            or message.get("author", {}).get("kind") != "human"
            or message["author"].get("id") != user_id
        ):
            raise MessageForbidden("Only the author, while a room member, can change this message.")
        receipt_sk = f"IDEMP#EDIT#{user_id}#{operation_id}"
        req_hash = request_hash(message["messageId"], expected_revision, text)
        receipt = await _get(room_pk(room_id), receipt_sk)
        if receipt:
            return await _replay_or_conflict(receipt, req_hash)
        if message.get("deletedAt") or int(message.get("messageRevision", 0)) != expected_revision:
            raise MessageConflict("This message changed. Refresh the conversation and try again.")
        now = utc_now_iso()
        changes = {
            "editedText": text,
            "messageRevision": expected_revision + 1,
            "editedAt": now,
            "deletedAt": now if text is None else None,
        }
        table_name = _table().name
        meta_item: dict = {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "ConditionExpression": "ownerId = :owner",
                "ExpressionAttributeValues": {":owner": owner_id},
            }
        }
        extra: list[dict] = []
        if (meta.get("lastMessage") or {}).get("messageId") == message["messageId"]:
            if text is None:
                row = await latest_preview_row(room_id, seq)
                preview = last_message_preview(row) if row else None
                if row:
                    extra.append(_unchanged_message_check(table_name, room_id, row))
            else:
                preview = last_message_preview({**message, **changes})
            meta_item = {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": "META"},
                    "UpdateExpression": "SET lastMessage = :preview",
                    "ConditionExpression": "ownerId = :owner AND lastMessage.messageId = :mid",
                    "ExpressionAttributeValues": {
                        ":owner": owner_id,
                        ":mid": message["messageId"],
                        ":preview": preview,
                    },
                }
            }
        try:
            await _transact(
                [
                    meta_item,
                    {
                        "ConditionCheck": {
                            "TableName": table_name,
                            "Key": {"pk": room_pk(room_id), "sk": member_sk("human", user_id)},
                            "ConditionExpression": "attribute_exists(sk)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"pk": room_pk(room_id), "sk": msg_sk(seq)},
                            # REMOVE threadTouchedAt: an edit/delete image never carries it, so the
                            # projector can't mistake it for a reply's root ADD (no clocks involved).
                            "UpdateExpression": "SET editedText = :text, messageRevision = :next, editedAt = :now, deletedAt = :deleted REMOVE threadTouchedAt",
                            "ConditionExpression": "attribute_exists(sk) AND #author.#kind = :human AND #author.id = :user AND (attribute_not_exists(messageRevision) OR messageRevision = :expected)",
                            "ExpressionAttributeNames": {"#author": "author", "#kind": "kind"},
                            "ExpressionAttributeValues": {
                                ":text": text,
                                ":next": expected_revision + 1,
                                ":now": now,
                                ":deleted": changes["deletedAt"],
                                ":human": "human",
                                ":user": user_id,
                                ":expected": expected_revision,
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": {
                                "pk": room_pk(room_id),
                                "sk": receipt_sk,
                                "requestHash": req_hash,
                                "roomId": room_id,
                                "messageId": message["messageId"],
                                "messageSeq": seq,
                                "createdAt": now,
                            },
                            "ConditionExpression": "attribute_not_exists(sk)",
                        }
                    },
                    *extra,
                ]
            )
        except ClientError as exc:
            if not _is_cancelled(exc):
                raise
            receipt = await _get(room_pk(room_id), receipt_sk)
            if receipt:
                return await _replay_or_conflict(receipt, req_hash)
            if attempt == 0:
                continue  # the preview moved (a newer message) or its replacement changed: reread once
            raise MessageConflict("The message or room access changed. Refresh and try again.") from exc
        return {**message, **changes}
    raise AssertionError("unreachable: the second attempt returns or raises")


async def set_reaction(
    owner_id: str, room_id: str, seq: int, user_id: str, emoji: str, active: bool, *, member_kind: str = "human"
) -> dict:
    for _ in range(_SEQ_RETRY_ATTEMPTS):
        message = await get_message(room_id, seq)
        if message is None:
            raise MessagingRepoError("Message not found.")
        if message.get("deletedAt"):
            raise MessagingRepoError("Deleted messages cannot receive reactions.")
        reactions = dict(message.get("reactions") or {})
        users = set(reactions.get(emoji, []))
        if active:
            users.add(user_id)
        else:
            users.discard(user_id)
        # ponytail: bound historical membership metadata; use separate reaction rows beyond 256 users per emoji.
        if len(users) > 256:
            raise MessagingRepoError("This reaction has reached its limit.")
        if users:
            reactions[emoji] = sorted(users)
        else:
            reactions.pop(emoji, None)
        if len(json.dumps(reactions, ensure_ascii=False).encode("utf-8")) > 64_000:
            raise MessagingRepoError("This message has reached its reaction limit.")
        revision = int(message.get("reactionRevision", 0))
        try:
            await _transact(
                [
                    {
                        "ConditionCheck": {
                            "TableName": _table().name,
                            "Key": {"pk": room_pk(room_id), "sk": "META"},
                            "ConditionExpression": "ownerId = :owner",
                            "ExpressionAttributeValues": {":owner": owner_id},
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": _table().name,
                            "Key": {"pk": room_pk(room_id), "sk": member_sk(member_kind, user_id)},
                            "ConditionExpression": "attribute_exists(sk)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": _table().name,
                            "Key": {"pk": room_pk(room_id), "sk": msg_sk(seq)},
                            "UpdateExpression": "SET reactions = :reactions, reactionRevision = :next",
                            "ConditionExpression": "attribute_exists(sk) AND (attribute_not_exists(reactionRevision) OR reactionRevision = :expected) AND (attribute_not_exists(messageRevision) OR messageRevision = :content)",
                            "ExpressionAttributeValues": {
                                ":reactions": reactions,
                                ":next": revision + 1,
                                ":expected": revision,
                                ":content": int(message.get("messageRevision", 0)),
                            },
                        }
                    },
                ]
            )
            return {**message, "reactions": reactions, "reactionRevision": revision + 1}
        except ClientError as exc:
            if not _is_cancelled(exc):
                raise
    raise MessagingRepoError("Reaction could not be updated. Refresh and try again.")


async def _replay_or_conflict(receipt: dict, req_hash: str) -> dict:
    if receipt.get("requestHash") != req_hash:
        raise IdempotencyConflict("idempotency key reused with a different request")
    message = await get_message(receipt["roomId"], int(receipt["messageSeq"]))
    if message is None:
        raise MessagingRepoError("durable receipt without its message")
    return message


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


async def upsert_member(
    *,
    room_id: str,
    owner_id: str,
    member: dict,
    handle_updates: dict[str, str] | None = None,
) -> dict:
    """Add or update one member; bump ``META.revision``. A new agent gets its
    delivery cursor at the CURRENT ``lastSeq`` so it never replays history.
    ``handle_updates`` re-stamps other agents' ``mentionHandle`` when the new
    roster changed their collision suffix. Returns the new META."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    existing = await get_member(room_id, member["kind"], member["id"])
    if existing is None:
        current = await list_members(room_id)
        if len(current) + 1 > MAX_ROOM_MEMBERS:
            raise MemberLimitExceeded(f"rooms hold at most {MAX_ROOM_MEMBERS} members")
    now = utc_now_iso()
    table_name = _table().name
    revision = int(meta["revision"]) + 1
    meta_update = "SET revision = :rev, updatedAt = :now"
    values: dict = {":rev": revision, ":expected": revision - 1, ":now": now}
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": meta_update,
                "ConditionExpression": "revision = :expected",
                "ExpressionAttributeValues": values,
            }
        }
    ]
    if existing is None:
        items.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": _member_item(room_id, owner_id, member, int(meta["lastSeq"]), _top_count(meta), now),
                    "ConditionExpression": "attribute_not_exists(sk)",
                }
            }
        )
        if member["kind"] == "agent":
            items.append(
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": room_pk(room_id),
                            "sk": delivery_sk(member["id"]),
                            "agentId": member["id"],
                            "lastSeq": int(meta["lastSeq"]),
                        },
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                }
            )
    else:
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": member_sk(member["kind"], member["id"])},
                    "UpdateExpression": "SET #role = :role, listenMode = :lm, displayName = :dn, mentionHandle = :mh"
                    + (", modeSetBy = :msb" if member.get("modeSetBy") else ""),
                    "ConditionExpression": "attribute_exists(sk)",
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {
                        ":role": member["role"],
                        ":lm": member.get("listenMode"),
                        ":dn": member.get("displayName") or member["id"],
                        ":mh": member.get("mentionHandle"),
                        **({":msb": member["modeSetBy"]} if member.get("modeSetBy") else {}),
                    },
                }
            }
        )
    for agent_id, handle in (handle_updates or {}).items():
        if agent_id == member["id"]:
            continue
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": member_sk("agent", agent_id)},
                    "UpdateExpression": "SET mentionHandle = :mh",
                    "ConditionExpression": "attribute_exists(sk)",
                    "ExpressionAttributeValues": {":mh": handle},
                }
            }
        )
    await _transact(items)
    return {**meta, "revision": revision, "updatedAt": now}


def autojoin_sk(user_id: str) -> str:
    return f"AUTOJOIN#{user_id}"


async def claim_autojoin(room_id: str, user_id: str) -> bool:
    """One-time marker: True only for the first claim, so a removed member is never auto-re-added."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": room_pk(room_id), "sk": autojoin_sk(user_id), "createdAt": utc_now_iso()},
            ConditionExpression="attribute_not_exists(sk)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def claim_room_marker(room_id: str, name: str) -> bool:
    """One-time per-room marker (``MARKER#<name>``): True only for the first claim."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": room_pk(room_id), "sk": f"MARKER#{name}", "createdAt": utc_now_iso()},
            ConditionExpression="attribute_not_exists(sk)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def release_room_marker(room_id: str, name: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": room_pk(room_id), "sk": f"MARKER#{name}"})


async def put_paused_ask(room_id: str, seq: int, author_id: str) -> None:
    """``PAUSEDASK#<userId>``: each person's latest message that spoke to Lightbulb while the room was paused
    (W18-8), answered on Resume. A later seq replaces an earlier one; an older redelivery never wins."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "pk": room_pk(room_id),
                "sk": f"PAUSEDASK#{author_id}",
                "seq": int(seq),
                "authorId": author_id,
                "createdAt": utc_now_iso(),
            },
            ConditionExpression="attribute_not_exists(sk) OR seq < :s",
            ExpressionAttributeValues={":s": int(seq)},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def pop_paused_ask(room_id: str, prefer_author: str | None = None) -> dict | None:
    """The ask Resume answers: ``prefer_author``'s own latest, else the latest anyone's (``{"seq", "authorId"}``).
    Every row goes, each delete returning what it removed: two Resumes answer it once.
    ponytail: one partition query; a pause window holds a few askers."""
    rows = await _query(room_pk(room_id), begins_with="PAUSEDASK#")
    taken = []
    for row in rows:
        resp = await run_in_thread(_table().delete_item, Key={"pk": row["pk"], "sk": row["sk"]}, ReturnValues="ALL_OLD")
        if resp.get("Attributes"):
            taken.append(resp["Attributes"])
    if not taken:
        return None
    own = [r for r in taken if r.get("authorId") == prefer_author]
    best = max(own or taken, key=lambda r: int(r["seq"]))
    return {"seq": int(best["seq"]), "authorId": best.get("authorId")}


async def put_noted(room_id: str, message_id: str, fact: str, *, actor_user_id: str | None = None) -> None:
    """``NOTED#<messageId>``: a message the Router's `note` decision acknowledged, with the fact it read (W15-5:
    what project Settings › Memory lists). Room metadata, not memory; a retried turn keeps the first row."""
    item = {
        "pk": room_pk(room_id),
        "sk": f"NOTED#{message_id}",
        "messageId": message_id,
        "fact": fact,
        "notedAt": utc_now_iso(),
    }
    if actor_user_id:
        item["actorUserId"] = actor_user_id
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def list_noted(room_id: str) -> list[dict]:
    """Newest first. ponytail: one partition query, no paging; a project holds a few dozen notes."""
    return sorted(await _query(room_pk(room_id), begins_with="NOTED#"), key=lambda r: r["notedAt"], reverse=True)


async def delete_noted(room_id: str, message_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": room_pk(room_id), "sk": f"NOTED#{message_id}"})


async def release_autojoin(room_id: str, user_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": room_pk(room_id), "sk": autojoin_sk(user_id)})


_MEMBER_PREF_FIELDS = ("starred", "muted", "sectionId")


async def set_member_prefs(room_id: str, kind: str, member_id: str, updates: dict[str, Any]) -> dict:
    """Per-user, per-room view preferences on the member row.

    Deliberately does NOT bump ``META.revision``: starring a room is one
    person's view of it, and a revision bump would invalidate every other
    member's cached room on every toggle.
    """
    named = {k: v for k, v in updates.items() if k in _MEMBER_PREF_FIELDS}
    if not named:
        return await get_member(room_id, kind, member_id) or {}
    names = {f"#{k}": k for k in named}
    values = {f":{k}": _ddb_safe(v) for k, v in named.items()}
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in named)
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": member_sk(kind, member_id)},
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RoomNotFound(f"{room_id}:{kind}:{member_id}") from exc
        raise
    return _plain(response["Attributes"])


def _live_item(
    table_name: str, room_id: str, owner_id: str, user_id: str, channel_key: str | None, event: dict
) -> dict:
    return {
        "Put": {
            "TableName": table_name,
            "Item": {
                "pk": room_pk(room_id),
                "sk": live_sk(event["eventId"], user_id),
                "roomId": room_id,
                "ownerId": owner_id,
                "userId": user_id,
                "channelKey": channel_key or "",
                "event": event,
                "expiresAt": _expires_at(),
            },
        }
    }


async def remove_member(
    *,
    meta: dict,
    kind: str,
    member_id: str,
    new_delivery_key: str | None = None,
    live_events: list[dict] | None = None,
    new_owner_id: str | None = None,
) -> dict:
    """Delete one membership; bump revision, conditioned on the ``meta`` the
    caller strongly read (so the notices it built for ``revision + 1`` are the
    ones that land). A HUMAN removal must supply ``new_delivery_key`` (the
    rotation) and the ``LIVE`` rows (``{userId, channelKey, event}`` per
    affected human) to write in the same transaction. An agent removal drops
    its delivery cursor. ``new_owner_id`` makes that human member the owner in the
    same transaction (a channel owner leaving hands the room on). Returns the new META."""
    room_id = meta["roomId"]
    owner_id = meta["ownerId"]
    if kind == "human" and not new_delivery_key:
        raise MessagingRepoError("human removal requires a rotated delivery key")
    now = utc_now_iso()
    table_name = _table().name
    revision = int(meta["revision"]) + 1
    update = "SET revision = :rev, updatedAt = :now" + (", deliveryKey = :key" if kind == "human" else "")
    values: dict = {":rev": revision, ":expected": revision - 1, ":now": now}
    if kind == "human":
        values[":key"] = new_delivery_key
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": update,
                "ConditionExpression": "revision = :expected",
                "ExpressionAttributeValues": values,
            }
        },
        {
            "Delete": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": member_sk(kind, member_id)},
                "ConditionExpression": "attribute_exists(sk)",
            }
        },
    ]
    if kind == "agent":
        items.append(
            {"Delete": {"TableName": table_name, "Key": {"pk": room_pk(room_id), "sk": delivery_sk(member_id)}}}
        )
    if new_owner_id:
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": member_sk("human", new_owner_id)},
                    "UpdateExpression": "SET #role = :owner",
                    "ConditionExpression": "attribute_exists(sk)",
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {":owner": "owner"},
                }
            }
        )
    for live in live_events or []:
        items.append(
            _live_item(
                table_name,
                room_id,
                live.get("ownerId") or owner_id,
                live["userId"],
                live.get("channelKey"),
                live["event"],
            )
        )
    await _transact(items)
    return {**meta, "revision": revision, "updatedAt": now, "deliveryKey": new_delivery_key or meta["deliveryKey"]}


async def delete_room(*, meta: dict, deleted_by: str, new_delivery_key: str, live_events: list[dict]) -> dict:
    """Tombstone a room: stamp ``deletedAt``/``deletedBy`` on META, rotate its
    ``deliveryKey`` and write the ``LIVE`` notices in ONE transaction, conditioned
    on the ``meta`` the caller read — the same revocation contract as a human
    removal, so every open subscription on the old channel goes silent. Messages
    and members stay (soft delete). A lost race (already deleted, or any other
    META write) raises ``RoomNotFound``; the caller re-reads to tell them apart."""
    room_id = meta["roomId"]
    now = utc_now_iso()
    table_name = _table().name
    revision = int(meta["revision"]) + 1
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": (
                    "SET deletedAt = :now, deletedBy = :by, deliveryKey = :key, revision = :rev, updatedAt = :now"
                ),
                "ConditionExpression": "revision = :expected AND attribute_not_exists(deletedAt)",
                "ExpressionAttributeValues": {
                    ":now": now,
                    ":by": deleted_by,
                    ":key": new_delivery_key,
                    ":rev": revision,
                    ":expected": revision - 1,
                },
            }
        }
    ]
    for live in live_events:
        items.append(
            _live_item(
                table_name,
                room_id,
                live.get("ownerId") or meta["ownerId"],
                live["userId"],
                live.get("channelKey"),
                live["event"],
            )
        )
    try:
        await _transact(items)
    except ClientError as exc:
        if _is_cancelled(exc):
            raise RoomNotFound(room_id) from exc
        raise
    return {
        **meta,
        "deletedAt": now,
        "deletedBy": deleted_by,
        "deliveryKey": new_delivery_key,
        "revision": revision,
        "updatedAt": now,
    }


_ROOM_META_FIELDS = (
    "name", "topic", "archivedAt", "archivedBy", "meeting", "icon", "color", "pausedAt", "pausedBy", "firstRun", "call",
)  # fmt: skip
# Cleared by REMOVE, not SET null: create_message's fence is attribute_not_exists(archivedAt).
_ROOM_META_REMOVABLE = ("archivedAt", "archivedBy", "icon", "color", "pausedAt", "pausedBy", "firstRun")


async def patch_room_meta(room_id: str, updates: dict[str, Any], *, expected_revision: int | None = None) -> dict:
    """Update the caller-editable META fields under the usual revision CAS.

    ``expected_revision`` binds the write to the revision the CALLER read (a read-merge-write),
    instead of the one read here; a writer in between then cancels the transaction.

    ``name`` is mirrored into the owner catalog row because
    ``list_rooms_for_member`` joins the sidebar off it; ``topic`` is not in
    that projection and is never mirrored.
    """
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    named = {k: v for k, v in updates.items() if k in _ROOM_META_FIELDS}
    removed = [k for k in _ROOM_META_REMOVABLE if k in named and named[k] is None]
    named = {k: v for k, v in named.items() if k not in removed}
    now = utc_now_iso()
    table_name = _table().name
    revision = (int(meta["revision"]) if expected_revision is None else expected_revision) + 1
    names = {f"#{k}": k for k in [*named, *removed]}
    values = {f":{k}": _ddb_safe(v) for k, v in named.items()}
    sets = ", ".join(f"#{k} = :{k}" for k in named)
    remove = f" REMOVE {', '.join(f'#{k}' for k in removed)}" if removed else ""
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": f"SET {sets + ', ' if sets else ''}revision = :rev, updatedAt = :now{remove}",
                "ConditionExpression": "revision = :expected",
                "ExpressionAttributeNames": names or None,
                "ExpressionAttributeValues": {**values, ":rev": revision, ":expected": revision - 1, ":now": now},
            }
        }
    ]
    if "name" in named:
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": owner_pk(meta["ownerId"]), "sk": room_pk(room_id)},
                    "UpdateExpression": "SET #name = :name, updatedAt = :now",
                    "ExpressionAttributeNames": {"#name": "name"},
                    "ExpressionAttributeValues": {":name": _ddb_safe(named["name"]), ":now": now},
                }
            }
        )
    items[0]["Update"] = {k: v for k, v in items[0]["Update"].items() if v is not None}
    await _transact(items)
    return {**{k: v for k, v in meta.items() if k not in removed}, **named, "revision": revision, "updatedAt": now}


async def set_meeting_field_if_absent(room_id: str, field: str, value: Any, also: dict | None = None) -> bool:
    """Set ``meeting.<field>`` (plus ``also``'s meeting fields) only if the meeting has no
    ``field`` yet, bumping the revision. False = another writer got there first: re-read META."""
    also = also or {}
    sets = "".join(f", meeting.#a{i} = :a{i}" for i in range(len(also)))
    try:
        await _transact(
            [
                {
                    "Update": {
                        "TableName": _table().name,
                        "Key": {"pk": room_pk(room_id), "sk": "META"},
                        "UpdateExpression": f"SET meeting.#f = :v{sets}, revision = revision + :one, updatedAt = :now",
                        "ConditionExpression": "attribute_exists(meeting) AND attribute_not_exists(meeting.#f)",
                        "ExpressionAttributeNames": {"#f": field, **{f"#a{i}": k for i, k in enumerate(also)}},
                        "ExpressionAttributeValues": {
                            ":v": _ddb_safe(value),
                            **{f":a{i}": _ddb_safe(v) for i, v in enumerate(also.values())},
                            ":one": 1,
                            ":now": utc_now_iso(),
                        },
                    }
                }
            ]
        )
        return True
    except ClientError as exc:
        if _is_cancelled(exc):
            return False
        raise


MAX_PINNED_MESSAGES = 50


async def set_pin(room_id: str, message_id: str, pinned: bool) -> dict:
    """Pin/unpin one message on META. Idempotent: a no-op change does not bump
    the revision, so repeated clicks never churn every client's room cache."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    current = [str(value) for value in meta.get("pinnedMessageIds", [])]
    if pinned and message_id in current:
        return meta
    if not pinned and message_id not in current:
        return meta
    if pinned and len(current) >= MAX_PINNED_MESSAGES:
        raise PinLimitExceeded(f"rooms hold at most {MAX_PINNED_MESSAGES} pinned messages")
    updated = [*current, message_id] if pinned else [value for value in current if value != message_id]
    now = utc_now_iso()
    revision = int(meta["revision"]) + 1
    await _transact(
        [
            {
                "Update": {
                    "TableName": _table().name,
                    "Key": {"pk": room_pk(room_id), "sk": "META"},
                    "UpdateExpression": "SET pinnedMessageIds = :pins, revision = :rev, updatedAt = :now",
                    "ConditionExpression": "revision = :expected",
                    "ExpressionAttributeValues": {
                        ":pins": _ddb_safe(updated),
                        ":rev": revision,
                        ":expected": revision - 1,
                        ":now": now,
                    },
                }
            }
        ]
    )
    return {**meta, "pinnedMessageIds": updated, "revision": revision, "updatedAt": now}


# ---------------------------------------------------------------------------
# cursors
# ---------------------------------------------------------------------------


async def advance_agent_delivery(
    room_id: str, agent_id: str, expected: int, through: int, *, accepted: bool = False
) -> bool:
    """Compare-and-set the agent cursor ``expected -> through``. False when the
    cursor moved (a concurrent drain) or is absent (agent removed). Only an
    accepted turn advances the user-visible delivery receipt."""
    if through < expected:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": delivery_sk(agent_id)},
            UpdateExpression=(
                "SET lastSeq = :through, lastDeliveredSeq = :through"
                if accepted
                else "SET lastSeq = :through, lastDeliveredSeq = if_not_exists(lastDeliveredSeq, :expected)"
            ),
            ConditionExpression="attribute_exists(sk) AND lastSeq = :expected",
            ExpressionAttributeValues={":through": int(through), ":expected": int(expected)},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def _write_cursor(meta: dict, user_id: str, target: int, condition: str) -> None:
    """Set ``lastReadSeq`` with the ordinal of that seq, so the pair never splits."""
    await run_in_thread(
        _table().update_item,
        Key={"pk": room_pk(meta["roomId"]), "sk": read_sk(user_id)},
        UpdateExpression="SET lastReadSeq = :n, topOrdinal = :o, userId = :u, updatedAt = :now",
        ConditionExpression=condition,
        ExpressionAttributeValues={
            ":n": target,
            ":o": await _ordinal_at(meta, target),
            ":u": user_id,
            ":now": utc_now_iso(),
        },
    )


async def set_read_cursor(room_id: str, user_id: str, last_read_seq: int) -> int:
    """Monotonic upsert bounded by the room's current ``lastSeq``."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    target = max(0, min(int(last_read_seq), int(meta["lastSeq"])))
    try:
        await _write_cursor(meta, user_id, target, "attribute_not_exists(lastReadSeq) OR lastReadSeq <= :n")
        return target
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return await get_read_cursor(room_id, user_id)


async def mark_unread(room_id: str, user_id: str, *, joined_seq: int, from_seq: int | None = None) -> int:
    """Leave the latest room message (or everything from ``from_seq`` on)
    unread without ever advancing the cursor."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    last_seq = int(meta["lastSeq"])
    ceiling = min(int(from_seq) - 1, last_seq) if from_seq is not None else last_seq - 1
    target = max(int(joined_seq), max(0, ceiling))
    try:
        await _write_cursor(meta, user_id, target, "attribute_exists(lastReadSeq) AND lastReadSeq > :n")
        return target
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return await get_read_cursor(room_id, user_id)


# ---------------------------------------------------------------------------
# Activity feed (rooms F5). Pointer rows are written ONLY by the messaging
# projector (the stream consumer), never inside the message transaction; this
# side reads them, stamps ``doneAt`` and keeps the per-reader seen cursor.
# ---------------------------------------------------------------------------

ACTIVITY_SEEN_SK = "SEEN"
# Every pointer sk starts with an ISO timestamp ("2026-..."), so it sorts below
# this bound and ``SEEN`` sorts above it.
_ACTIVITY_SK_CEILING = "A"


def activity_pk(home_owner_id: str, user_id: str) -> str:
    return f"ACTIVITY#{home_owner_id}#{user_id}"


def activity_sk(created_at: str, message_id: str) -> str:
    return f"{created_at}#{message_id}"


async def list_activity(home_owner_id: str, user_id: str, *, before_sk: str | None, limit: int) -> list[dict]:
    """Newest-first pointer rows strictly below ``before_sk`` (the opaque cursor)."""
    from boto3.dynamodb.conditions import Key

    upper = before_sk if before_sk is not None else _ACTIVITY_SK_CEILING
    return await _paginate(
        KeyConditionExpression=Key("pk").eq(activity_pk(home_owner_id, user_id)) & Key("sk").lt(upper),
        ConsistentRead=True,
        ScanIndexForward=False,
        Limit=limit,
    )


async def list_activity_since(home_owner_id: str, user_id: str, after: str | None, limit: int) -> list[dict]:
    """Up to ``limit`` pointer rows created after ``after`` (all, if None) -- the unseen badge."""
    from boto3.dynamodb.conditions import Key

    lower = f"{after}~" if after else "0"  # "~" sorts after the "#<id>" of a row AT ``after``
    return await _paginate(
        KeyConditionExpression=Key("pk").eq(activity_pk(home_owner_id, user_id))
        & Key("sk").between(lower, _ACTIVITY_SK_CEILING),
        ConsistentRead=True,
        ScanIndexForward=False,
        Limit=limit,
    )


async def put_activity_row(item: dict) -> None:
    """A backend-written pointer row (the projector writes message rows). Idempotent on its key."""
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_activity_row(home_owner_id: str, user_id: str, sk: str) -> dict | None:
    return await _get(activity_pk(home_owner_id, user_id), sk)


async def mark_activity_done(home_owner_id: str, user_id: str, sk: str) -> dict | None:
    """Stamp ``doneAt`` once (a repeat keeps the first stamp). ``None`` if the row is gone."""
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": activity_pk(home_owner_id, user_id), "sk": sk},
            UpdateExpression="SET doneAt = if_not_exists(doneAt, :now)",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":now": utc_now_iso()},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return _plain(response["Attributes"])


async def get_activity_seen(home_owner_id: str, user_id: str) -> str | None:
    row = await _get(activity_pk(home_owner_id, user_id), ACTIVITY_SEEN_SK)
    return row.get("seenUpTo") if row else None


async def set_activity_seen(home_owner_id: str, user_id: str, up_to: str) -> str:
    """Monotonic: an older ``up_to`` never moves the cursor back. Returns the stored value."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": activity_pk(home_owner_id, user_id), "sk": ACTIVITY_SEEN_SK, "seenUpTo": up_to},
            ConditionExpression="attribute_not_exists(seenUpTo) OR seenUpTo < :v",
            ExpressionAttributeValues={":v": up_to},
        )
        return up_to
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return await get_activity_seen(home_owner_id, user_id) or up_to


DIGEST_TTL_S = 3 * 24 * 3600


async def claim_digest(user_id: str, home_owner_id: str, local_date: str) -> bool:
    """Claim the one daily digest for (human, workspace, local date). ``True``
    exactly once: the conditional put is the send's idempotency boundary."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "pk": f"DIGEST#{user_id}#{home_owner_id}#{local_date}",
                "sk": "DIGEST",
                "expiresAt": int(time.time()) + DIGEST_TTL_S,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def batch_get(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Strongly read many ``(pk, sk)`` rows, 100 per request; missing rows are absent."""
    table = _table()
    client = table.meta.client
    unique = list(dict.fromkeys(keys))
    found: dict[tuple[str, str], dict] = {}
    for start in range(0, len(unique), 100):
        request = {
            table.name: {
                "Keys": [{"pk": pk, "sk": sk} for pk, sk in unique[start : start + 100]],
                "ConsistentRead": True,
            }
        }
        while request:
            response = await run_in_thread(client.batch_get_item, RequestItems=request)
            for item in _plain(response.get("Responses", {}).get(table.name, [])):
                found[(item["pk"], item["sk"])] = item
            request = response.get("UnprocessedKeys") or None
    return found


# ---------------------------------------------------------------------------
# private user streams
# ---------------------------------------------------------------------------


async def get_user_stream(owner_id: str, user_id: str) -> dict | None:
    return await _get(owner_pk(owner_id), userstream_sk(user_id))


async def get_stream(channel_key: str) -> dict | None:
    return await _get(stream_pk(channel_key), "META")


async def list_active_user_streams(owner_id: str) -> list[dict]:
    rows = await _query(owner_pk(owner_id), begins_with="USERSTREAM#")
    return [r for r in rows if r.get("active")]


async def ensure_user_stream(owner_id: str, user_id: str, *, rejoin: bool = False) -> str | None:
    """Return the user's CURRENT active channel key, allocating a fresh UUID when no
    stream exists. An INACTIVE stream is a removed member (``revoke_human_membership``):
    it answers None, because a removed member's unexpired JWT still resolves the org and
    reviving the stream here would put them back on live events. Only ``rejoin=True`` —
    the Clerk ``organizationMembership.created`` webhook, a real (re)join — turns it back
    on, under a fresh key (an old key is never reactivated)."""
    current = await get_user_stream(owner_id, user_id)
    if current is not None and current.get("active"):
        return current["channelKey"]
    if current is not None and not rejoin:
        return None
    table_name = _table().name
    now = utc_now_iso()
    key = str(uuid.uuid4())
    workspace_id = await ensure_workspace_id(owner_id)
    mapping = {
        "pk": owner_pk(owner_id),
        "sk": userstream_sk(user_id),
        "ownerId": owner_id,
        "userId": user_id,
        "channelKey": key,
        "active": True,
        "updatedAt": now,
    }
    reverse = {
        "pk": stream_pk(key),
        "sk": "META",
        "channelKey": key,
        "ownerId": owner_id,
        "userId": user_id,
        "active": True,
        "workspaceId": workspace_id,
        "createdAt": now,
    }
    if current is None:
        items = [
            {"Put": {"TableName": table_name, "Item": mapping, "ConditionExpression": "attribute_not_exists(sk)"}},
            {"Put": {"TableName": table_name, "Item": reverse, "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
    else:
        items = [
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": owner_pk(owner_id), "sk": userstream_sk(user_id)},
                    "UpdateExpression": "SET channelKey = :key, active = :t, updatedAt = :now",
                    "ConditionExpression": "active = :f AND channelKey = :old",
                    "ExpressionAttributeValues": {
                        ":key": key,
                        ":t": True,
                        ":f": False,
                        ":now": now,
                        ":old": current["channelKey"],
                    },
                }
            },
            {"Put": {"TableName": table_name, "Item": reverse, "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
    try:
        await _transact(items)
        return key
    except ClientError as exc:
        if not _is_cancelled(exc):
            raise
    # Lost the race to a concurrent allocation — read the winner.
    winner = await get_user_stream(owner_id, user_id)
    if winner is None or not winner.get("active"):
        raise MessagingRepoError("user stream allocation raced and no active mapping exists")
    return winner["channelKey"]


async def enroll_sessions_member(owner_id: str, user_id: str, channel_key: str) -> str | None:
    """Let an ACTIVE member subscribe the workspace sessions channel, ``/ws/<ws>/sessions``:
    the ``WORKSPACE#<ws>`` lookup and member rows the AppSync handler reads, conditioned on the
    user's stream still being on with ``channel_key``. Returns the workspace id, or None when
    the stream went off meanwhile (a concurrent removal: that transaction deletes the row)."""
    from core.repositories.container_repo import retry_conflict

    workspace_id = await ensure_workspace_id(owner_id)
    table_name = _table().name
    now = utc_now_iso()
    # The lookup row is written once, outside the member write: rewriting it on every live-config
    # made concurrent members' enrollments contend on one item (Codex on #3915).
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": workspace_pk(workspace_id), "sk": "META", "ownerId": owner_id, "workspaceId": workspace_id},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    try:
        await retry_conflict(
            lambda: _transact(
                [
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": {
                                "pk": workspace_pk(workspace_id),
                                "sk": f"MEMBER#{user_id}",
                                "ownerId": owner_id,
                                "userId": user_id,
                                "active": True,
                                "updatedAt": now,
                            },
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": table_name,
                            "Key": {"pk": owner_pk(owner_id), "sk": userstream_sk(user_id)},
                            "ConditionExpression": "active = :t AND channelKey = :key",
                            "ExpressionAttributeValues": {":t": True, ":key": channel_key},
                        }
                    },
                ]
            )
        )
        return workspace_id
    except ClientError as exc:
        if _condition_failed(exc):  # the stream went off meanwhile: not a member
            return None
        raise  # a conflict that outlasted the retries is not a removal: the caller answers 5xx


def _condition_failed(exc: ClientError) -> bool:
    return _is_cancelled(exc) and any(
        r.get("Code") == "ConditionalCheckFailed" for r in exc.response.get("CancellationReasons", [])
    )


async def deactivate_user_stream(owner_id: str, user_id: str) -> bool:
    """Mark BOTH mappings inactive and drop the sessions-channel member row, in one write
    (idempotent). Returns whether anything changed."""
    current = await get_user_stream(owner_id, user_id)
    if current is None or not current.get("active"):
        return False
    table_name = _table().name
    now = utc_now_iso()
    workspace = await _get(owner_pk(owner_id), workspace_sk())
    member_row = (
        [
            {
                "Delete": {
                    "TableName": table_name,
                    "Key": {"pk": workspace_pk(workspace["workspaceId"]), "sk": f"MEMBER#{user_id}"},
                }
            }
        ]
        if workspace
        else []
    )
    from core.repositories.container_repo import retry_conflict

    try:
        await retry_conflict(
            lambda: _transact(
                member_row
                + [
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"pk": owner_pk(owner_id), "sk": userstream_sk(user_id)},
                            "UpdateExpression": "SET active = :f, updatedAt = :now",
                            "ConditionExpression": "active = :t AND channelKey = :key",
                            "ExpressionAttributeValues": {
                                ":f": False,
                                ":t": True,
                                ":key": current["channelKey"],
                                ":now": now,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"pk": stream_pk(current["channelKey"]), "sk": "META"},
                            "UpdateExpression": "SET active = :f, deactivatedAt = :now",
                            "ExpressionAttributeValues": {":f": False, ":now": now},
                        }
                    },
                ]
            )
        )
        return True
    except ClientError as exc:
        # Only a failed condition means another write already turned it off (or rotated it). A
        # conflict that outlasted the retries raises, so the removal webhook is redelivered
        # instead of acked with the member still on (Codex on #3915).
        if _condition_failed(exc):
            return False
        raise


async def get_user_prefs(owner_id: str, user_id: str) -> dict | None:
    """The caller's sidebar preferences.

    A dedicated ``PREF#`` item rather than the ``USERSTREAM#`` row: that row's
    writes are conditioned on the delivery-key lifecycle (``ensure_user_stream``
    / ``deactivate_user_stream``), so a sections write there would race key
    rotation and be dropped on revocation.
    """
    return await _get(owner_pk(owner_id), pref_sk(user_id))


async def put_user_sections(owner_id: str, user_id: str, sections: list[dict]) -> list[dict]:
    """Replace the ordered section list wholesale (the client owns the order).

    An ``update_item`` on just the ``sections``/``ownerId``/``userId``/``updatedAt``
    attributes, never ``put_item`` -- the ``PREF#`` row is the caller's general
    preferences, not a sections-only row, and a wholesale put would clobber any
    other attribute a later feature stores on it. No ``ConditionExpression``:
    the row may not exist yet, and ``update_item`` upserts.
    """
    stored = [{"id": str(section["id"]), "name": str(section["name"])} for section in sections]
    await run_in_thread(
        _table().update_item,
        Key={"pk": owner_pk(owner_id), "sk": pref_sk(user_id)},
        UpdateExpression="SET sections = :s, ownerId = :o, userId = :u, updatedAt = :t",
        ExpressionAttributeValues={
            ":s": _ddb_safe(stored),
            ":o": owner_id,
            ":u": user_id,
            ":t": utc_now_iso(),
        },
    )
    return stored


# ---------------------------------------------------------------------------
# runtime operations
# ---------------------------------------------------------------------------


async def create_operation(
    *, owner_id: str, operation_id: str, requester_user_id: str, method: str, params: dict
) -> dict:
    now = utc_now_iso()
    table_name = _table().name
    group, dedup = operation_fifo_ids(owner_id, operation_id)
    row = {
        "pk": owner_pk(owner_id),
        "sk": operation_sk(operation_id),
        "operationId": operation_id,
        "ownerId": owner_id,
        "requesterUserId": requester_user_id,
        "method": method,
        "params": _ddb_safe(params),
        "state": "queued",
        "result": None,
        "error": None,
        "createdAt": now,
        "updatedAt": now,
    }
    await _transact(
        [
            {"Put": {"TableName": table_name, "Item": row, "ConditionExpression": "attribute_not_exists(sk)"}},
            {
                "Put": {
                    "TableName": table_name,
                    "Item": {
                        "pk": owner_pk(owner_id),
                        "sk": work_operation_sk(operation_id),
                        "payload": {"kind": "runtime_operation", "ownerId": owner_id, "operationId": operation_id},
                        "messageGroupId": group,
                        "messageDeduplicationId": dedup,
                        "expiresAt": _expires_at(),
                    },
                    "ConditionExpression": "attribute_not_exists(sk)",
                }
            },
        ]
    )
    return row


async def get_operation(owner_id: str, operation_id: str) -> dict | None:
    return await _get(owner_pk(owner_id), operation_sk(operation_id))


async def claim_operation(owner_id: str, operation_id: str, *, lease_token: str, lease_until: int) -> dict | None:
    """queued -> running, or reclaim a ``running`` row whose lease expired.
    Returns the row when this caller holds the lease, else None (terminal or
    someone else's live lease)."""
    now_epoch = int(time.time())
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": operation_sk(operation_id)},
            UpdateExpression="SET #state = :running, leaseToken = :token, leaseUntil = :until, updatedAt = :now",
            ConditionExpression="attribute_exists(sk) AND (#state = :queued OR (#state = :running AND leaseUntil < :now_epoch))",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":running": "running",
                ":queued": "queued",
                ":token": lease_token,
                ":until": int(lease_until),
                ":now": utc_now_iso(),
                ":now_epoch": now_epoch,
            },
            ReturnValues="ALL_NEW",
        )
        return _plain(response["Attributes"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def release_operation(owner_id: str, operation_id: str, *, lease_token: str) -> bool:
    """Return a transiently blocked operation to ``queued`` for SQS retry."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": operation_sk(operation_id)},
            UpdateExpression="SET #state = :queued, updatedAt = :now REMOVE leaseToken, leaseUntil",
            ConditionExpression="#state = :running AND leaseToken = :token",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":queued": "queued",
                ":running": "running",
                ":token": lease_token,
                ":now": utc_now_iso(),
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def complete_operation(
    owner_id: str, operation_id: str, *, lease_token: str, state: str, result: Any, error: dict | None
) -> dict | None:
    """running -> succeeded|failed, only for the lease holder. Returns the row or None."""
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": operation_sk(operation_id)},
            UpdateExpression="SET #state = :state, #result = :result, #error = :error, updatedAt = :now REMOVE leaseToken, leaseUntil",
            ConditionExpression="#state = :running AND leaseToken = :token",
            ExpressionAttributeNames={"#state": "state", "#result": "result", "#error": "error"},
            ExpressionAttributeValues={
                ":state": state,
                ":result": _ddb_safe(result),
                ":error": _ddb_safe(error),
                ":now": utc_now_iso(),
                ":running": "running",
                ":token": lease_token,
            },
            ReturnValues="ALL_NEW",
        )
        return _plain(response["Attributes"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


# ---------------------------------------------------------------------------
# run ledger (async turn contract)
# ---------------------------------------------------------------------------


async def create_run(
    owner_id: str,
    run_id: str,
    *,
    room_id: str,
    agent_id: str,
    workspace_id: str,
    trigger_message_id: str,
    event_kind: str,
    thread_root_id: str | None = None,
    routed_by: str | None = None,
    routing_p: float | None = None,
) -> bool:
    """Put the ledger row as ``dispatched``. False when it already exists (redelivery).
    ``threadRootId`` is the thread the run answers in (None: the main conversation) -- what a
    follow-up is matched against to be steered into this run. ``routedBy`` is who started the
    run (``jev`` / ``fallback`` / ``mention`` / ``dm`` / ``follow_up``) and ``routingP`` Jev's
    probability for this agent when Jev picked it (T35)."""
    now = utc_now_iso()
    item = {
        "pk": owner_pk(owner_id),
        "sk": run_sk(run_id),
        "runId": run_id,
        "ownerId": owner_id,
        "roomId": room_id,
        "agentId": agent_id,
        "workspaceId": workspace_id,
        "triggerMessageId": trigger_message_id,
        "eventKind": event_kind,
        "threadRootId": thread_root_id,
        "routedBy": routed_by,
        **({"routingP": Decimal(str(round(float(routing_p), 4)))} if routing_p is not None else {}),
        "status": "dispatched",
        "dispatchedAt": now,
        "updatedAt": now,
        "sweep": "open",
        "deadlineAt": int(time.time()) + RUN_DISPATCH_TIMEOUT_S,
        "expiresAt": _expires_at(),
    }
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def get_run(owner_id: str, run_id: str) -> dict | None:
    return await _get(owner_pk(owner_id), run_sk(run_id))


VOICE_RUN_TTL_S = 2 * 3600


async def put_voice_run(
    owner_id: str, run_id: str, fields: dict[str, Any], once: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Create the run row, or set ``fields`` on it, and return the row. One atomic SET, never a whole-row
    put: a /turns retry writes the same fields again and cannot drop what another writer set in between
    (``noPost``). A field in ``once`` is written only if the row has none yet (a lowered ``None`` counts),
    so a retry can never undo what a steer lowered."""
    fields = {"runId": run_id, **fields, "expiresAt": int(time.time()) + VOICE_RUN_TTL_S}
    response = await run_in_thread(
        _table().update_item,
        Key={"pk": owner_pk(owner_id), "sk": voice_run_sk(run_id)},
        UpdateExpression="SET "
        + ", ".join(
            f"#f{i} = if_not_exists(#f{i}, :v{i})" if k in once else f"#f{i} = :v{i}" for i, k in enumerate(fields)
        ),
        ExpressionAttributeNames={f"#f{i}": k for i, k in enumerate(fields)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes") or {}


def voice_run_may_post(owner_id: str, run_id: str) -> dict:
    """A transaction ``ConditionCheck``: the run was not told "don't post that". It rides the message's own
    transaction, so a mark that lands any time before the commit refuses the post. A run with no row
    (expired, or never a voice run) may post."""
    return {
        "ConditionCheck": {
            "TableName": _table().name,
            "Key": {"pk": owner_pk(owner_id), "sk": voice_run_sk(run_id)},
            "ConditionExpression": "attribute_not_exists(noPost)",
        }
    }


#: A meeting's "write the notes" request message -> everything its after-meeting mail needs (the
#: meeting room, occurrence, where the request sits, and a snapshot of the meeting), so the mail no
#: longer depends on the live meeting record, which a recurring series rewrites when it advances.
#: Its own once-only markers live here too. Kept a day longer than any notes run.
MEETING_NOTES_TTL_S = 2 * 86400


def meeting_notes_sk(message_id: str) -> str:
    return f"MEETNOTES#{message_id}"


async def put_meeting_notes_request(owner_id: str, message_id: str, fields: dict[str, Any]) -> None:
    item = {
        "pk": owner_pk(owner_id), "sk": meeting_notes_sk(message_id), **fields,
        "expiresAt": int(time.time()) + MEETING_NOTES_TTL_S,
    }  # fmt: skip
    try:  # first write wins: a retried dispatch must not clear a mail claim already taken
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_meeting_notes_request(owner_id: str, message_id: str) -> dict | None:
    row = await _get(owner_pk(owner_id), meeting_notes_sk(message_id))
    return row if row and int(row.get("expiresAt", 0)) > time.time() else None


async def claim_meeting_notes_mail(owner_id: str, message_id: str) -> bool:
    """True once per notes request: the mail is attempted at most once (a lost mail beats a double)."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": meeting_notes_sk(message_id)},
            UpdateExpression="SET mailAttemptedAt = :now",
            ConditionExpression="attribute_exists(pk) AND attribute_not_exists(mailAttemptedAt)",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def get_voice_run(owner_id: str, run_id: str) -> dict | None:
    row = await _get(owner_pk(owner_id), voice_run_sk(run_id))
    # DynamoDB TTL deletes lazily: an expired row must not authorize anything.
    return row if row and int(row.get("expiresAt", 0)) > time.time() else None


async def count_voice_edit(owner_id: str, agent_id: str, kind: str, limit: int) -> bool:
    """One voice edit of ``kind`` for this agent today (UTC); False once ``limit`` are used.
    Not WORK#/LIVE#: neither the projector nor the Pipe reads this sk."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": f"VOICEEDIT#{agent_id}#{kind}#{day}"},
            UpdateExpression="ADD edits :one SET expiresAt = :exp",
            ConditionExpression="attribute_not_exists(edits) OR edits < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": int(time.time()) + 2 * 86400},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def set_agent_names(room_id: str, agent_id: str, display_name: str, mention_handle: str | None) -> bool:
    """SET-only label refresh of one agent member row; False when it is no longer a member."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": member_sk("agent", agent_id)},
            UpdateExpression="SET displayName = :dn, mentionHandle = :mh",
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeValues={":dn": display_name, ":mh": mention_handle},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def redispatch_run(owner_id: str, run_id: str) -> dict | None:
    """A redelivery of a run the runtime never acknowledged: restart its start deadline, so a
    slow retry is not swept as "did not start" before it lands. None when the run already
    started, ended or was stopped -- the caller must not invoke it again."""
    now = utc_now_iso()
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET deadlineAt = :deadline, updatedAt = :now",
        ConditionExpression="attribute_exists(sk) AND #s = :dispatched AND attribute_not_exists(abortRequestedAt)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":deadline": int(time.time()) + RUN_DISPATCH_TIMEOUT_S,
            ":now": now,
            ":dispatched": "dispatched",
        },
    )


async def touch_mac_viewer(owner_id: str, run_id: str, tier: str, at: int) -> bool:
    """One live Mac run's viewer demand; the run row's TTL cleans it up after closure."""
    field = {"view": "macViewAt", "thumb": "macThumbAt"}[tier]
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression=f"SET {field} = :at",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":at": at, ":ended": "ended"},
    )
    return row is not None


# ---------------------------------------------------------------------------
# delegation follow-ups
# ---------------------------------------------------------------------------


async def create_follow_up(
    *,
    owner_id: str,
    room_id: str,
    agent_id: str,
    follow_up_id: str,
    trigger_message_id: str,
    event_kind: str,
    reason: str,
    text: str,
) -> bool:
    """Put the ``FOLLOWUP`` row and its ``WORK#`` pointer in one transaction.
    False when the follow-up already exists (the bridge's retry)."""
    table_name = _table().name
    # The room+agent FIFO group, so a follow-up queues behind that agent's room turns.
    group, dedup = room_fifo_ids(owner_id, room_id, agent_id, follow_up_id)
    try:
        await _transact(
            [
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": room_pk(room_id),
                            "sk": followup_sk(follow_up_id),
                            "followUpId": follow_up_id,
                            "agentId": agent_id,
                            "triggerMessageId": trigger_message_id,
                            "eventKind": event_kind,
                            "reason": reason,
                            "text": text,
                            "createdAt": utc_now_iso(),
                            "expiresAt": _expires_at(),
                        },
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": room_pk(room_id),
                            "sk": work_followup_sk(agent_id, follow_up_id),
                            "payload": {
                                "kind": "room_follow_up",
                                "roomId": room_id,
                                "agentId": agent_id,
                                "followUpId": follow_up_id,
                            },
                            "messageGroupId": group,
                            "messageDeduplicationId": dedup,
                            "expiresAt": _expires_at(),
                        },
                    }
                },
            ]
        )
        return True
    except ClientError as exc:
        if not _is_cancelled(exc):
            raise
    if await get_follow_up(room_id, follow_up_id) is None:
        raise MessagingRepoError("follow-up create cancelled without a durable row")
    return False


async def get_follow_up(room_id: str, follow_up_id: str) -> dict | None:
    return await _get(room_pk(room_id), followup_sk(follow_up_id))


async def latest_human_message(room_id: str) -> dict | None:
    """The newest human-authored ``MSG`` in the room, or None.

    ponytail: reads the whole room partition (like ``get_message_by_id``); add an index once a measured room needs it.
    """
    rows = await _query(room_pk(room_id), begins_with="MSG#", ascending=False)
    return next((r for r in rows if r.get("author", {}).get("kind") == "human"), None)


async def count_follow_ups_since(room_id: str, agent_id: str, since_iso: str | None) -> int:
    """Follow-ups this agent was owed in this room at or after ``since_iso`` (all of them when None).

    ponytail: reads every FOLLOWUP row in the room partition (TTL-bounded to 7 days); add a per-agent key
    if a room ever holds enough to matter.
    """
    rows = await _query(room_pk(room_id), begins_with="FOLLOWUP#")
    return sum(
        1
        for row in rows
        # A re-dispatched lost steer (reason steer_lost) is a human message, not a delegation.
        if row.get("agentId") == agent_id
        and row.get("reason") != "steer_lost"
        and (since_iso is None or row.get("createdAt", "") >= since_iso)
    )


def usage_sk(run_id: str) -> str:
    return f"USAGE#{run_id}"


async def claim_usage_receipt(owner_id: str, run_id: str) -> bool:
    """First writer wins: the usage callback is at-least-once, the counters are not idempotent."""
    item = {"pk": owner_pk(owner_id), "sk": usage_sk(run_id), "expiresAt": _expires_at()}
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def list_open_runs_for_room(owner_id: str, room_id: str) -> list[dict]:
    """The room's in-progress ledger rows — what a client that opened the room
    mid-run has no other way to learn (the live ``run_active`` frame is
    published once, at turn start).

    Owner-partition query, not the ``by-run-deadline`` GSI: that index is
    KEYS_ONLY and has ONE partition-key value ("open") for the whole fleet, so
    a room open would read every tenant's in-flight runs and still need a
    follow-up get per hit. This reads one owner's ``RUN#`` rows — TTL-bounded
    to 7 days — exactly like ``has_open_runs`` below.
    """
    rows = await _query(owner_pk(owner_id), begins_with="RUN#")
    return [row for row in rows if row.get("sweep") == "open" and row.get("roomId") == room_id]


async def has_open_runs(owner_id: str) -> bool:
    """Any ledger row still marked open (``end_run`` moves it to ``followup``)."""
    rows = await _query(owner_pk(owner_id), begins_with="RUN#")
    return any(row.get("sweep") == "open" for row in rows)


async def _run_update(owner_id: str, run_id: str, **kwargs: Any) -> dict | None:
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": run_sk(run_id)},
            ReturnValues="ALL_NEW",
            **kwargs,
        )
        return _plain(response["Attributes"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def start_run(owner_id: str, run_id: str, *, session_key: str) -> dict | None:
    """dispatched -> started. The row as written, or None when the transition did not apply."""
    now = utc_now_iso()
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, sessionKey = :sk, startedAt = :now, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now",
        # A Stop's grace deadline (request_abort) is never pushed back by a late start or beat.
        ConditionExpression="attribute_exists(sk) AND #s = :dispatched AND attribute_not_exists(abortRequestedAt)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":dispatched": "dispatched",
            ":sk": session_key,
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
        },
    )


async def heartbeat_run(owner_id: str, run_id: str) -> dict | None:
    """Extend the deadline while not ended (a heartbeat before ``run.started`` also starts the run).
    The row as written, or None when the run is ended/stopping/unknown."""
    now = utc_now_iso()
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now, startedAt = if_not_exists(startedAt, :now)",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended AND attribute_not_exists(abortRequestedAt)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":ended": "ended",
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
        },
    )


async def add_steered_message(owner_id: str, run_id: str, message_id: str, *, ambient: bool = False) -> bool:
    """Record a follow-up the runtime took into this run, while it is still open. False when
    the run has already ended: the steer can no longer be answered, so the caller dispatches
    a turn instead. An ended row's ``steeredMessageIds`` is what a failed run re-dispatches;
    ``ambientSteerIds`` marks the room events among them, which are routed again rather than
    answered by this agent regardless."""
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="ADD steeredMessageIds :ids"
        + (", ambientSteerIds :ids" if ambient else "")
        + " SET updatedAt = :now",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":ids": {message_id}, ":ended": "ended", ":now": utc_now_iso()},
    )
    return row is not None


async def request_abort(
    owner_id: str, run_id: str, *, stopped_by: str | None = None, stop_after_s: int | None = None
) -> bool:
    """The runtime accepted a Stop: the run stays open until its own run.ended(aborted), but
    only for RUN_ABORT_GRACE_S -- heartbeats no longer extend it, so the sweeper closes it if
    the backend's own timer (routers/messaging.abort_run) never fires. First Stop wins.
    ``stoppedBy`` / ``stopAfterS`` (seconds since run.started) feed the Stop ledger (T35)."""
    now = utc_now_iso()
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET abortRequestedAt = :now, deadlineAt = :deadline, updatedAt = :now, "
        "stoppedBy = :by, stopAfterS = :after",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended AND attribute_not_exists(abortRequestedAt)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":ended": "ended",
            ":now": now,
            ":deadline": int(time.time()) + RUN_ABORT_GRACE_S,
            ":by": stopped_by,
            ":after": stop_after_s,
        },
    )
    return row is not None


async def end_run(
    owner_id: str,
    run_id: str,
    *,
    outcome: str,
    error_kind: str | None = None,
    usage: dict | None = None,
    expected_deadline_at: int | None = None,
) -> dict | None:
    """Close once. Returns the row when this call closed it, None on replay/unknown."""
    if outcome not in RUN_OUTCOMES:
        raise MessagingRepoError(f"unknown run outcome {outcome!r}")
    now = utc_now_iso()
    values = {
        ":ended": "ended",
        ":outcome": outcome,
        ":ek": error_kind,
        ":usage": _ddb_safe(usage),
        ":now": now,
        ":followup": "followup",
        ":check_at": int(time.time()) + RUN_FOLLOWUP_CHECK_S,
    }
    condition = "attribute_exists(sk) AND #s <> :ended"
    if expected_deadline_at is not None:
        condition += " AND deadlineAt = :expected_deadline"
        values[":expected_deadline"] = expected_deadline_at
    return await _run_update(
        owner_id,
        run_id,
        # The row stays on the sweep index as ``followup`` until the 30-minute check labels it
        # (T35, ``run_feedback``); ``list_overdue_runs`` reads only ``open``.
        UpdateExpression="SET #s = :ended, outcome = :outcome, errorKind = :ek, #u = :usage, endedAt = :now, "
        "updatedAt = :now, sweep = :followup, deadlineAt = :check_at",
        ConditionExpression=condition,
        ExpressionAttributeNames={"#s": "status", "#u": "usage"},
        ExpressionAttributeValues=values,
    )


#: An ended run is checked this long after it ends: replied, reacted, re-asked or ignored (T35).
RUN_FOLLOWUP_CHECK_S = 30 * 60


async def list_overdue_runs(now_epoch: int, page_size: int = 100, *, marker: str = "open") -> list[dict]:
    """All overdue runs carrying ``marker`` (``open``: unfinished; ``followup``: ended and
    due for the 30-minute check), reading the sparse GSI in bounded pages."""
    query = {
        "IndexName": GSI_BY_RUN_DEADLINE,
        "KeyConditionExpression": "sweep = :open AND deadlineAt < :now",
        "ExpressionAttributeValues": {":open": marker, ":now": int(now_epoch)},
        "Limit": page_size,
    }
    refs: list[dict] = []
    table = _table()
    while True:
        response = await run_in_thread(table.query, **query)
        refs.extend(
            {"ownerId": item["pk"][len("OWNER#") :], "runId": item["sk"][len("RUN#") :]}
            for item in response.get("Items", [])
        )
        last = response.get("LastEvaluatedKey")
        if not last:
            return refs
        query["ExclusiveStartKey"] = last


async def label_run_followup(owner_id: str, run_id: str, follow_up: str) -> dict | None:
    """Write the 30-minute check and take the row off the sweep index. Once: None when
    another sweeper pass already labelled it."""
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET followUp = :label, followUpAt = :now, updatedAt = :now REMOVE sweep, deadlineAt",
        ConditionExpression="attribute_exists(sk) AND sweep = :followup",
        ExpressionAttributeValues={":label": follow_up, ":now": utc_now_iso(), ":followup": "followup"},
    )


# ---------------------------------------------------------------------------
# away-email checks (T15): ``AWAY#<user>`` rows on the same sparse deadline index,
# ``sweep = "away"``; written by the projector and ``needs_you_email.queue_question``
# ---------------------------------------------------------------------------


async def list_due_away_checks(now_epoch: int, page_size: int = 100) -> list[dict]:
    """``[{pk, sk}]`` of every away check whose deadline has passed."""
    query = {
        "IndexName": GSI_BY_RUN_DEADLINE,
        "KeyConditionExpression": "sweep = :away AND deadlineAt < :now",
        "ExpressionAttributeValues": {":away": "away", ":now": int(now_epoch)},
        "Limit": page_size,
    }
    keys: list[dict] = []
    table = _table()
    while True:
        response = await run_in_thread(table.query, **query)
        keys.extend({"pk": item["pk"], "sk": item["sk"]} for item in response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return keys
        query["ExclusiveStartKey"] = last


async def get_away_check(pk: str, sk: str) -> dict | None:
    return await _get(pk, sk)


async def finish_away_check(pk: str, sk: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": pk, "sk": sk})


async def defer_away_check(pk: str, sk: str, deadline_epoch: int) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET deadlineAt = :d",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":d": int(deadline_epoch)},
    )


# ---------------------------------------------------------------------------
# wipe
# ---------------------------------------------------------------------------


async def list_owner_rooms(owner_id: str) -> list[dict]:
    """The owner catalog rows (roomId, name, kind, createdAt): no META read."""
    return await _query(owner_pk(owner_id), begins_with="ROOM#")


def voice_agent_config(config: dict) -> dict:
    """The current app-only meeting settings; a retired email toggle has no behavior or promise."""
    meetings = config.get("meetings")
    return (
        {**config, "meetings": {k: v for k, v in meetings.items() if k != "emailNotes"}}
        if isinstance(meetings, dict)
        else dict(config)
    )


async def get_voice_agent(owner_id: str) -> dict | None:
    row = await _get(owner_pk(owner_id), "VOICEAGENT")
    return voice_agent_config(json.loads(row["config"])) if row else None


async def put_voice_agent(owner_id: str, config: dict, updated_by: str) -> None:
    item = {"pk": owner_pk(owner_id), "sk": "VOICEAGENT", "config": json.dumps(voice_agent_config(config)),
            "updatedAt": utc_now_iso(), "updatedBy": updated_by}  # fmt: skip
    await run_in_thread(_table().put_item, Item=item)


async def modify_voice_agent(owner_id: str, change, updated_by: str) -> bool:
    """Read-modify-write for the phone paths, which must not undo an owner's save made in between (a Pause, a
    permission): the put is conditional on the `updatedAt` it read, and on a clash re-reads and re-applies.
    `change(cfg)` returns the new config, or None for nothing to write. False: no Agent page, or nothing to write.
    The owner's own save (`put_voice_agent`) stays unconditional. ponytail: 5 tries, then the write is dropped."""
    for _ in range(5):
        row = await _get(owner_pk(owner_id), "VOICEAGENT")
        if not row:
            return False
        new = change(voice_agent_config(json.loads(row["config"])))
        if new is None:
            return False
        item = {"pk": owner_pk(owner_id), "sk": "VOICEAGENT", "config": json.dumps(voice_agent_config(new)),
                "updatedAt": utc_now_iso(), "updatedBy": updated_by}  # fmt: skip
        try:
            await run_in_thread(
                _table().put_item,
                Item=item,
                ConditionExpression="updatedAt = :u",
                ExpressionAttributeValues={":u": row["updatedAt"]},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return False


async def list_owner_room_ids(owner_id: str) -> list[str]:
    rows = await _query(owner_pk(owner_id), begins_with="ROOM#")
    return [r["roomId"] for r in rows]


async def _delete_partition(pk: str) -> int:
    table = _table()
    rows = await _query(pk)
    if not rows:
        return 0

    def _batch() -> None:
        with table.batch_writer() as batch:
            for r in rows:
                batch.delete_item(Key={"pk": r["pk"], "sk": r["sk"]})

    await run_in_thread(_batch)
    return len(rows)


async def delete_room_partition(room_id: str) -> int:
    return await _delete_partition(room_pk(room_id))


async def delete_owner_partition(owner_id: str) -> int:
    """Delete owner rows, reverse streams and retained local-session history."""
    from core.services import agent_keys

    await agent_keys.delete_for_owner(owner_id)  # their lookup rows sit outside this partition
    streams = await _query(owner_pk(owner_id), begins_with="USERSTREAM#")
    count = 0
    for s in streams:
        count += await _delete_partition(stream_pk(s["channelKey"]))
    workspace = await _get(owner_pk(owner_id), workspace_sk())
    if workspace:
        count += await _delete_partition(workspace_pk(workspace["workspaceId"]))
    count += await _delete_partition(owner_pk(owner_id))
    from core.repositories import local_sessions_repo

    count += await local_sessions_repo.delete_owner(owner_id)
    return count


def decision_sk(message_id: str) -> str:
    return f"DECISION#{message_id}"


#: A pending claim older than this is a dead winner's; the next consumer may take it over.
_DECISION_CLAIM_STALE_S = 60


async def claim_decision(room_id: str, message_id: str, *, attempts: int = 0) -> bool:
    """Claim-first, so Jev runs once per message: put a ``pending`` placeholder if the row is
    absent, is a rate-limit ``retry`` row past its ``retryAt``, or is a pending claim older than 60 s. Only the winner
    computes. ``attempts`` carries the retry count through the claim."""
    now = int(time.time())
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "pk": room_pk(room_id),
                "sk": decision_sk(message_id),
                "decision": {"pending": True, "claimedAt": now, "attempts": attempts},
                "expiresAt": _expires_at(),
            },
            ConditionExpression=(
                "attribute_not_exists(sk) OR (#d.#r = :t AND #d.#ra <= :now) OR (#d.#p = :t AND #d.#c < :stale)"
            ),
            ExpressionAttributeNames={
                "#d": "decision",
                "#p": "pending",
                "#c": "claimedAt",
                "#r": "retry",
                "#ra": "retryAt",
            },
            ExpressionAttributeValues={":t": True, ":now": now, ":stale": now - _DECISION_CLAIM_STALE_S},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def finalize_decision(
    room_id: str, message_id: str, decision: dict, *, only_if_absent: bool = False, only_if_retry: bool = False
) -> dict:
    """Write the final decision only while the row is still pending, a rate-limit retry, or gone,
    and return whatever final decision is stored. First final write wins, so a late claim winner and a
    loser that timed out and fell back always act on the SAME decision -- never two responders."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "pk": room_pk(room_id),
                "sk": decision_sk(message_id),
                "decision": _ddb_safe(decision),
                "expiresAt": _expires_at(),
            },
            # only_if_absent / only_if_retry: a caller that never claimed (no key) must not
            # overwrite a live claim.
            **(
                {"ConditionExpression": "attribute_not_exists(sk)"}
                if only_if_absent
                else {
                    "ConditionExpression": "#d.#r = :t",
                    "ExpressionAttributeNames": {"#d": "decision", "#r": "retry"},
                    "ExpressionAttributeValues": {":t": True},
                }
                if only_if_retry
                else {
                    "ConditionExpression": "attribute_not_exists(sk) OR #d.#p = :t OR #d.#r = :t",
                    "ExpressionAttributeNames": {"#d": "decision", "#p": "pending", "#r": "retry"},
                    "ExpressionAttributeValues": {":t": True},
                }
            ),
        )
        return decision
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    stored = await get_decision(room_id, message_id)
    if stored is None:
        # Unreachable unless something deletes DECISION rows (a consistent read follows the
        # failed conditional write); loud, because returning our own value could split-brain.
        logger.warning("finalize_decision: row vanished after a conditional failure room=%s", room_id)
        return decision
    return stored


async def get_decision(room_id: str, message_id: str) -> dict | None:
    row = await _get(room_pk(room_id), decision_sk(message_id))
    return row.get("decision") if row else None


# ---------------------------------------------------------------------------
# device login (`lightbulb join`): one row per code hash, plus a pointer row per user code
# ---------------------------------------------------------------------------


def device_login_key(code_hash: str) -> dict:
    return {"pk": f"DEVICELOGIN#{code_hash}", "sk": f"DEVICELOGIN#{code_hash}"}


def device_login_code_key(user_code: str) -> dict:
    return {"pk": f"DEVICELOGINCODE#{user_code}", "sk": f"DEVICELOGINCODE#{user_code}"}


async def create_device_login(code_hash: str, user_code: str, fields: dict, expires_at: int) -> None:
    """Both rows or neither; a user-code collision cancels the transaction (ClientError)."""
    new = "attribute_not_exists(pk)"
    await _transact(
        [
            {
                "Put": {
                    "TableName": settings.MESSAGING_TABLE,
                    "Item": {**device_login_key(code_hash), **fields, "userCode": user_code, "expiresAt": expires_at},
                    "ConditionExpression": new,
                }
            },
            {
                "Put": {
                    "TableName": settings.MESSAGING_TABLE,
                    "Item": {**device_login_code_key(user_code), "codeHash": code_hash, "expiresAt": expires_at},
                    "ConditionExpression": new,
                }
            },
        ]
    )


async def get_device_login(code_hash: str) -> dict | None:
    key = device_login_key(code_hash)
    return await _get(key["pk"], key["sk"])


async def get_device_login_by_user_code(user_code: str) -> dict | None:
    key = device_login_code_key(user_code)
    pointer = await _get(key["pk"], key["sk"])
    return await get_device_login(pointer["codeHash"]) if pointer else None


async def claim_device_login(code_hash: str, owner_id: str, user_id: str) -> bool:
    """Bind a pending login to one approver (workspace + person). True when the claim is
    ours (new or already ours), False when someone else holds it or the row is gone."""
    try:
        await run_in_thread(
            _table().update_item,
            Key=device_login_key(code_hash),
            UpdateExpression="SET approverUserId = :u, approverOwnerId = :o",
            ConditionExpression=(
                "attribute_exists(pk) AND (attribute_not_exists(approverUserId)"
                " OR (approverUserId = :u AND approverOwnerId = :o))"
            ),
            ExpressionAttributeValues={":u": user_id, ":o": owner_id},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def finish_device_login(code_hash: str, user_id: str, result: dict) -> None:
    await run_in_thread(
        _table().update_item,
        Key=device_login_key(code_hash),
        UpdateExpression="SET #s = :approved, #r = :r",
        ConditionExpression="approverUserId = :u",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={":approved": "approved", ":r": _ddb_safe(result), ":u": user_id},
    )


async def take_device_login(code_hash: str, *, approved_only: bool) -> dict | None:
    """Delete the login (and its user-code pointer); return the deleted row. With
    ``approved_only`` only an approved row is taken, so two racing polls get it once."""
    kwargs: dict[str, Any] = {"Key": device_login_key(code_hash), "ReturnValues": "ALL_OLD"}
    if approved_only:
        kwargs.update(
            ConditionExpression="#s = :approved",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":approved": "approved"},
        )
    try:
        old = (await run_in_thread(_table().delete_item, **kwargs)).get("Attributes")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return None
    if old and old.get("userCode"):
        await run_in_thread(_table().delete_item, Key=device_login_code_key(old["userCode"]))
    return _plain(old) if old else None
