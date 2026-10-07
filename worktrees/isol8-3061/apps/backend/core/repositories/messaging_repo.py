"""Canonical multiplayer messaging store — ONE DynamoDB table (``MESSAGING_TABLE``).

Key registry (architecture spec §7; the ``by-member`` GSI is discovery ONLY —
every protected operation rechecks the base ``MEMBER#`` row strongly):

    OWNER#<owner>   ROOM#<room>                        room directory projection
    ROOM#<room>     META                               owner, fields, revision, lastSeq, deliveryKey
    ROOM#<room>     MEMBER#<kind>#<id>                 role, listenMode, mentionHandle, joinedSeq, homeOwnerId (foreign members of a shared room; memberKey is keyed on it)
    ROOM#<room>     MSG#<20-digit seq>                 original request + public edit overlay + dispatchTargets
    ROOM#<room>     IDEMP#EDIT#<user>#<operationId>     durable edit/delete retry receipt
    ROOM#<room>     IDEMP#human#<user>#<clientMessageId>  durable human retry receipt
    OWNER#<owner>   IDEMP#AGENT#<eventId>              durable agent retry receipt (owner-scoped
                                                       because the receipt route carries no roomId)
    ROOM#<room>     READ#human#<user>                  last read seq
    ROOM#<room>     DELIVERY#AGENT#<agent>             last drained seq per agent
    ROOM#<room>     WORK#MESSAGE#<seq>#AGENT#<agent>   one-shot outbox -> EventBridge Pipe -> FIFO
    ROOM#<room>     LIVE#<eventId>#USER#<user>         one-shot private-user notification outbox
    OWNER#<owner>   USERSTREAM#<user>                  current opaque private-channel key
    STREAM#<key>    META                               reverse mapping AppSync onSubscribe reads
    OWNER#<owner>   OPERATION#<id>                     async runtime op status/result
    OWNER#<owner>   WORK#OPERATION#<id>                one-shot outbox for runtime work
    OWNER#<owner>   WORKSPACE                          opaque AppSync channel-path segment
    OWNER#<owner>   RUN#<runId>                         async turn ledger (Task 9)
    OWNER#<owner>   USAGE#<runId>                       first-writer-wins usage-callback receipt (Task 7)
    ROOM#<room>     FOLLOWUP#<followUpId>               a finished async delegation owed back to its agent
    ROOM#<room>     WORK#FOLLOWUP#<agent>#<followUpId>  one-shot outbox for that follow-up turn
    ROOM#<room>     DECISION#<messageId>                Jev routing decision, claim-first (NOT WORK#:
                                                       the Pipe must never see it; the projector skips it)

``WORK#``, ``LIVE#``, ``RUN#``, ``USAGE#``, ``FOLLOWUP#`` and ``DECISION#`` rows carry ``expiresAt`` (7 days). Every read
here is ``ConsistentRead=True``; the GSIs are queried by ``list_rooms_for_member``
(``by-member``) and ``list_overdue_runs`` (``by-run-deadline``) and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import _get_resource, run_in_thread, utc_now_iso
from core.dynamodb import plain as _plain

logger = logging.getLogger(__name__)

GSI_BY_MEMBER = "by-member"
GSI_BY_RUN_DEADLINE = "by-run-deadline"
MAX_ROOM_MEMBERS = 32
OUTBOX_TTL_S = 7 * 24 * 3600
RUN_NS = uuid.UUID("2b5f1c7e-9a3d-4e6f-8b1c-0d2e4f6a8c1e")
RUN_DISPATCH_TIMEOUT_S = 120
RUN_HEARTBEAT_TIMEOUT_S = 90
RUN_OUTCOMES = frozenset({"completed", "error", "aborted", "ignored", "gate_error", "not_admitted"})
# Room-create is 3 fixed actions (META, owner catalog, IDEMP) + members + agent
# delivery rows. The 32-member worst case is 66; DynamoDB caps a transaction at 100.
_FIXED_CREATE_ACTIONS = 3
_TRANSACT_LIMIT = 100
_SEQ_RETRY_ATTEMPTS = 6


class MessagingRepoError(Exception):
    pass


class IdempotencyConflict(MessagingRepoError):
    """Same idempotency key, different canonical request."""


class MessageConflict(MessagingRepoError):
    pass


class MessageForbidden(MessagingRepoError):
    pass


class MemberLimitExceeded(MessagingRepoError):
    pass


class RoomNotFound(MessagingRepoError):
    pass


class RoomArchived(MessagingRepoError):
    pass


class PinLimitExceeded(MessagingRepoError):
    pass


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


def followup_sk(follow_up_id: str) -> str:
    return f"FOLLOWUP#{follow_up_id}"


def work_followup_sk(agent_id: str, follow_up_id: str) -> str:
    return f"WORK#FOLLOWUP#{agent_id}#{follow_up_id}"


def workspace_sk() -> str:
    return "WORKSPACE"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


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


def vendor_room_id(owner_id: str) -> str:
    """The one platform-managed channel a customer workspace shares with the vendor org."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{owner_id}"))


def request_hash(*parts: Any) -> str:
    """SHA-256 over canonical JSON request fields."""
    return _sha256(json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str))


def _expires_at() -> int:
    return int(time.time()) + OUTBOX_TTL_S


def _ddb_safe(value: Any) -> Any:
    """Convert JSON-compatible floats before passing values to DynamoDB."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(v) for v in value]
    return value


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
    await run_in_thread(table.meta.client.transact_write_items, TransactItems=items)


def _is_cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"


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


async def get_read_cursor(room_id: str, user_id: str) -> int:
    row = await _get(room_pk(room_id), read_sk(user_id))
    return int(row["lastReadSeq"]) if row else 0


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


# ---------------------------------------------------------------------------
# room creation
# ---------------------------------------------------------------------------


def _member_item(room_id: str, owner_id: str, m: dict, joined_seq: int, now: str) -> dict:
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
) -> dict:
    """One transaction: META + owner catalog + IDEMP + every member row + one
    delivery cursor per agent (at seq 0). ``members`` must already include the
    creator (role ``owner``). Returns the META item; a byte-identical replay
    returns the existing META, a different one raises ``IdempotencyConflict``."""
    if len(members) > MAX_ROOM_MEMBERS:
        raise MemberLimitExceeded(f"rooms hold at most {MAX_ROOM_MEMBERS} members")
    agents = [m for m in members if m["kind"] == "agent"]
    actions = _FIXED_CREATE_ACTIONS + len(members) + len(agents)
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
        "deliveryKey": str(uuid.uuid4()),
        "workspaceId": workspace_id,
        "createdAt": now,
        "updatedAt": now,
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
    for m in members:
        items.append({"Put": {"TableName": table_name, "Item": _member_item(room_id, owner_id, m, 0, now)}})
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
    for attempt in range(_SEQ_RETRY_ATTEMPTS):
        meta = await get_room(room_id)
        if meta is None:
            raise RoomNotFound(room_id)
        if meta.get("archivedAt"):
            raise RoomArchived(room_id)
        expected = int(meta["lastSeq"])
        seq = expected + 1
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
        }
        items: list[dict] = [
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": "META"},
                    "UpdateExpression": "SET lastSeq = :seq, lastMessageAt = :now, updatedAt = :now",
                    # An archive committing after the read above cancels this; the retry sees it.
                    "ConditionExpression": "lastSeq = :expected AND attribute_not_exists(archivedAt)",
                    "ExpressionAttributeValues": {":seq": seq, ":expected": expected, ":now": now},
                }
            },
            {
                "ConditionCheck": {
                    "TableName": table_name,
                    "Key": {"pk": room_pk(room_id), "sk": member_sk(author["kind"], author["id"])},
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
        items.extend(_work_message_item(table_name, owner_id, room_id, message_id, seq, a) for a in target_ids)
        try:
            await _transact(items)
            return _plain(message), False
        except ClientError as exc:
            if not _is_cancelled(exc):
                raise
            reasons = exc.response.get("CancellationReasons", [])
            if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                # The author's own MEMBER row is gone: not a sequence race, don't retry.
                raise MessageForbidden("Only a current room member can send this message.") from exc
        prior = await _get(idemp_pk, idemp_sk)
        if prior is not None:
            return await _replay_or_conflict(prior, req_hash), True
        # Sequence race (or a same-instant duplicate seq) — reread and retry.
        logger.info("messaging: sequence conflict on room=%s attempt=%d", room_id, attempt + 1)
    raise MessagingRepoError(f"could not append to room {room_id} after {_SEQ_RETRY_ATTEMPTS} attempts")


async def update_message(
    owner_id: str, room_id: str, seq: int, user_id: str, operation_id: str, expected_revision: int, text: str | None
) -> dict:
    """Change the public projection, retaining the original request for runtime work.

    The durable receipt makes an uncertain retry safe, including after another edit.
    Neither message sequence nor dispatch/work rows change.
    """
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
                        "Key": {"pk": room_pk(room_id), "sk": member_sk("human", user_id)},
                        "ConditionExpression": "attribute_exists(sk)",
                    }
                },
                {
                    "Update": {
                        "TableName": _table().name,
                        "Key": {"pk": room_pk(room_id), "sk": msg_sk(seq)},
                        "UpdateExpression": "SET editedText = :text, messageRevision = :next, editedAt = :now, deletedAt = :deleted",
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
                        "TableName": _table().name,
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
            ]
        )
    except ClientError as exc:
        if not _is_cancelled(exc):
            raise
        receipt = await _get(room_pk(room_id), receipt_sk)
        if receipt:
            return await _replay_or_conflict(receipt, req_hash)
        raise MessageConflict("The message or room access changed. Refresh and try again.") from exc
    return {**message, **changes}


async def set_reaction(owner_id: str, room_id: str, seq: int, user_id: str, emoji: str, active: bool) -> dict:
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
                            "Key": {"pk": room_pk(room_id), "sk": member_sk("human", user_id)},
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
    *, room_id: str, owner_id: str, member: dict, handle_updates: dict[str, str] | None = None
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
    items: list[dict] = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {"pk": room_pk(room_id), "sk": "META"},
                "UpdateExpression": "SET revision = :rev, updatedAt = :now",
                "ConditionExpression": "revision = :expected",
                "ExpressionAttributeValues": {":rev": revision, ":expected": revision - 1, ":now": now},
            }
        }
    ]
    if existing is None:
        items.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": _member_item(room_id, owner_id, member, int(meta["lastSeq"]), now),
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
) -> dict:
    """Delete one membership; bump revision, conditioned on the ``meta`` the
    caller strongly read (so the notices it built for ``revision + 1`` are the
    ones that land). A HUMAN removal must supply ``new_delivery_key`` (the
    rotation) and the ``LIVE`` rows (``{userId, channelKey, event}`` per
    affected human) to write in the same transaction. An agent removal drops
    its delivery cursor. Returns the new META."""
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


_ROOM_META_FIELDS = ("name", "topic", "archivedAt", "archivedBy")
# Cleared by REMOVE, not SET null: create_message's fence is attribute_not_exists(archivedAt).
_ROOM_META_REMOVABLE = ("archivedAt", "archivedBy")


async def patch_room_meta(room_id: str, updates: dict[str, Any]) -> dict:
    """Update the caller-editable META fields under the usual revision CAS.

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
    revision = int(meta["revision"]) + 1
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


async def advance_agent_delivery(room_id: str, agent_id: str, expected: int, through: int) -> bool:
    """Compare-and-set the agent cursor ``expected -> through``. False when the
    cursor moved (a concurrent drain) or is absent (agent removed)."""
    if through < expected:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": delivery_sk(agent_id)},
            UpdateExpression="SET lastSeq = :through",
            ConditionExpression="attribute_exists(sk) AND lastSeq = :expected",
            ExpressionAttributeValues={":through": int(through), ":expected": int(expected)},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def set_read_cursor(room_id: str, user_id: str, last_read_seq: int) -> int:
    """Monotonic upsert bounded by the room's current ``lastSeq``."""
    meta = await get_room(room_id)
    if meta is None:
        raise RoomNotFound(room_id)
    target = max(0, min(int(last_read_seq), int(meta["lastSeq"])))
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": read_sk(user_id)},
            UpdateExpression="SET lastReadSeq = :n, userId = :u, updatedAt = :now",
            ConditionExpression="attribute_not_exists(lastReadSeq) OR lastReadSeq <= :n",
            ExpressionAttributeValues={":n": target, ":u": user_id, ":now": utc_now_iso()},
        )
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
        await run_in_thread(
            _table().update_item,
            Key={"pk": room_pk(room_id), "sk": read_sk(user_id)},
            UpdateExpression="SET lastReadSeq = :n, userId = :u, updatedAt = :now",
            ConditionExpression="attribute_exists(lastReadSeq) AND lastReadSeq > :n",
            ExpressionAttributeValues={":n": target, ":u": user_id, ":now": utc_now_iso()},
        )
        return target
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return await get_read_cursor(room_id, user_id)


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


async def ensure_user_stream(owner_id: str, user_id: str) -> str:
    """Return the user's CURRENT active channel key, allocating a fresh UUID when
    there is none (first call, or after a deactivation — an old key is never
    reactivated)."""
    current = await get_user_stream(owner_id, user_id)
    if current is not None and current.get("active"):
        return current["channelKey"]
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


async def deactivate_user_stream(owner_id: str, user_id: str) -> bool:
    """Mark BOTH mappings inactive (idempotent). Returns whether anything changed."""
    current = await get_user_stream(owner_id, user_id)
    if current is None or not current.get("active"):
        return False
    table_name = _table().name
    now = utc_now_iso()
    try:
        await _transact(
            [
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
        return True
    except ClientError as exc:
        if _is_cancelled(exc):
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
) -> bool:
    """Put the ledger row as ``dispatched``. False when it already exists (redelivery)."""
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


async def count_recent_follow_ups(room_id: str, agent_id: str, *, window_s: int) -> int:
    """Follow-ups this agent was owed in this room within the last ``window_s``.

    ponytail: reads every FOLLOWUP row in the room partition (TTL-bounded to 7
    days, capped at 3/agent/hour); add a per-agent key if a room ever holds enough
    to matter.
    """
    cutoff = datetime.fromtimestamp(time.time() - window_s, timezone.utc).isoformat()
    rows = await _query(room_pk(room_id), begins_with="FOLLOWUP#")
    return sum(1 for row in rows if row.get("agentId") == agent_id and row.get("createdAt", "") >= cutoff)


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
    return [row for row in rows if "sweep" in row and row.get("roomId") == room_id]


async def has_open_runs(owner_id: str) -> bool:
    """Any ledger row still carrying the sweep marker (removed by ``end_run``)."""
    rows = await _query(owner_pk(owner_id), begins_with="RUN#")
    return any("sweep" in row for row in rows)


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


async def start_run(owner_id: str, run_id: str, *, session_key: str) -> bool:
    """dispatched -> started."""
    now = utc_now_iso()
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, sessionKey = :sk, startedAt = :now, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now",
        ConditionExpression="attribute_exists(sk) AND #s = :dispatched",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":dispatched": "dispatched",
            ":sk": session_key,
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
        },
    )
    return row is not None


async def heartbeat_run(owner_id: str, run_id: str) -> bool:
    """Extend the deadline while not ended (a heartbeat before ``run.started`` also starts the run)."""
    now = utc_now_iso()
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now, startedAt = if_not_exists(startedAt, :now)",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":ended": "ended",
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
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
) -> dict | None:
    """Close once. Returns the row when this call closed it, None on replay/unknown."""
    if outcome not in RUN_OUTCOMES:
        raise MessagingRepoError(f"unknown run outcome {outcome!r}")
    now = utc_now_iso()
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :ended, outcome = :outcome, errorKind = :ek, #u = :usage, endedAt = :now, updatedAt = :now REMOVE sweep, deadlineAt",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status", "#u": "usage"},
        ExpressionAttributeValues={
            ":ended": "ended",
            ":outcome": outcome,
            ":ek": error_kind,
            ":usage": _ddb_safe(usage),
            ":now": now,
        },
    )


async def list_overdue_runs(now_epoch: int, limit: int = 100) -> list[dict]:
    """Open ledger rows whose deadline passed, via the sparse ``by-run-deadline`` GSI."""
    response = await run_in_thread(
        _table().query,
        IndexName=GSI_BY_RUN_DEADLINE,
        KeyConditionExpression="sweep = :open AND deadlineAt < :now",
        ExpressionAttributeValues={":open": "open", ":now": int(now_epoch)},
        Limit=limit,
    )
    return [
        {"ownerId": item["pk"][len("OWNER#") :], "runId": item["sk"][len("RUN#") :]}
        for item in response.get("Items", [])
    ]


# ---------------------------------------------------------------------------
# wipe
# ---------------------------------------------------------------------------


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
    streams = await _query(owner_pk(owner_id), begins_with="USERSTREAM#")
    count = 0
    for s in streams:
        count += await _delete_partition(stream_pk(s["channelKey"]))
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
