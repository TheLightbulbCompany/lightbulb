"""Stateless paired-desktop transport; DynamoDB fences every worker and reconnect.

No provider RPC or process registry. Dispatched means transport attempted; only a
desktop journal receipt can establish native admission. Unknown blocks its process generation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from functools import lru_cache
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import Field, TypeAdapter, ValidationError

from core.config import settings
from core.constants import SYSTEM_ACTOR_ID
from core.dynamodb import utc_now_iso
from core.observability.product_analytics import capture_product_event
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import MESSAGE_TEXT_MAX_BYTES, ApiProblem, ContractModel, make_live_event

CLIENT_ID = "lightbulb-local-sessions"
LIVENESS_SECONDS = 30
CLAIM_SECONDS = 5
PAUSED_NOTICE = "Sharing was paused here. Activity from this point until sharing resumed is not shown."
logger = logging.getLogger(__name__)
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:local-session-lifecycle")


class Descriptor(ContractModel):
    workspace: sessions.WorkspaceSummary = Field(default_factory=sessions.WorkspaceSummary)
    provider: Literal["claude", "codex"]
    profile_fingerprint: sessions.DeviceId
    native_conversation_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    provider_version: str = Field(min_length=1, max_length=128)
    status: sessions.INPUT_STATUSES
    active_turn_id: sessions.OpaqueId | None = None
    control_unavailable_reason: sessions.ControlUnavailableReason | None = None
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    kind: Literal["interactive", "background"] = "interactive"
    start_id: sessions.OpaqueId | None = None


class Register(ContractModel):
    type: Literal["local.session.register"]
    session: Descriptor
    previous_generation: sessions.OpaqueId | None = None


class HeartbeatSession(ContractModel):
    """One live mirror as the device sees it. Null labels are silence, not a clear."""

    session_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    status: sessions.INPUT_STATUSES
    active_turn_id: sessions.OpaqueId | None = None
    control_unavailable_reason: sessions.ControlUnavailableReason | None = None
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
    workspace: sessions.WorkspaceSummary = Field(default_factory=sessions.WorkspaceSummary)
    # Sent only by a desktop that saw FEATURES in the registration ACK. Null is silence.
    stats: sessions.SessionStats | None = None
    room_reply_request_id: bool | None = None
    # A newer desktop sends this only after the relay advertises support.
    capabilities: sessions.Capabilities | None = None


#: What this relay reads beyond the frozen frames; the desktop sends a field only once listed.
FEATURES = [
    "sessionStats",
    "sessionSubagents",
    "roomReplyRequestId",
    "nativeControlCapabilities",
    "privateSendApproval",
]


class DeviceHeartbeat(ContractModel):
    """Every mirror the device still owns, split across as many frames as it takes.

    The 16 KiB envelope is checked before validation, so a cap the envelope cannot
    carry would reject the whole frame as INVALID_REQUEST instead of rejecting the
    extra entries. 24 golden-shaped entries fit with room to spare; a device with
    more sessions sends consecutive frames, each of them complete on its own.
    """

    type: Literal["local.device.heartbeat"]
    sessions: Annotated[list[HeartbeatSession], Field(max_length=24)]


class Ended(ContractModel):
    type: Literal["local.session.ended"]
    session_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    reason: Literal["exited", "process-exited"]
    ended_at: int = Field(ge=0)


Event = sessions.LocalSessionEventBody


class Events(ContractModel):
    type: Literal["local.session.events"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    batch_id: sessions.OpaqueId
    after_seq: int = Field(ge=0, lt=10**20 - 33)
    events: list[Event] = Field(min_length=1, max_length=32)
    private_approval: sessions.PrivateSendApproval | None = None


class Receipt(ContractModel):
    type: Literal["local.session.receipt"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    request_id: sessions.OpaqueId
    outcome: Literal["applied", "rejected", "unknown"]
    provider_message_id: sessions.OpaqueId | None = None
    held: bool = False
    settled: bool = False


_WIRE = TypeAdapter(Register | DeviceHeartbeat | Events | Receipt | Ended)


def check_envelope(body: dict) -> None:
    # Match ManagementApiClient's *actual* encoding, including non-ASCII expansion.
    if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > sessions.MAX_ENVELOPE_BYTES:
        raise ApiProblem("INVALID_REQUEST", "Local session envelope exceeds 16 KiB.")


def _revised(row: dict, **changes) -> dict:
    return {**row, **changes, "revision": str(uuid.uuid4())}


def _capture_lifecycle(row: dict, event: str, **properties: str) -> None:
    capture_product_event(
        distinct_id=row["humanUserId"],
        event=event,
        properties={
            "workspace_id": row["ownerId"],
            "session_id": row["sessionId"],
            "provider": row["provider"],
            **properties,
        },
        event_uuid=str(uuid.uuid5(_ANALYTICS_NS, f"{row['ownerId']}:{row['sessionId']}:{row['generation']}:{event}")),
    )


def _conflict(reason: str, **fields: object) -> ApiProblem:
    # Ids, seqs and reasons only — never frame content.
    logger.warning("local session conflict: %s", reason, extra={"reason": reason, **fields})
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed; reconcile before retrying.")


async def _actor_label(command: dict) -> dict | None:
    """Room-derived handle and name of the sender; the desktop shows it in the owner's terminal."""
    from core.services.messaging import human_emails, with_human_handles

    if (command.get("actor") or {}).get("kind") == "session":
        return {"kind": "session", "displayName": "Shared native session"}
    # Room mentions are authorized as roomless commands, but retain their
    # server-stored source room for sender attribution on the desktop.
    room_id = command.get("roomId") or command.get("originRoomId")
    if not room_id:
        return None
    kind, member_id = ("agent", command["actor"]["id"]) if "actor" in command else ("human", command["actorUserId"])
    room_members = await store.list_members(room_id)
    members = with_human_handles(room_members, await human_emails(room_members))
    member = next((m for m in members if m.get("kind") == kind and str(m.get("id")) == member_id), None)
    if member is None:
        return None
    return {
        "kind": kind,
        "handle": member.get("mentionHandle") or None,
        "displayName": member.get("displayName") or None,
    }


async def publish_resources_changed(owner: str, human: str, *, transcript: bool = False) -> None:
    """One ``workspace.sessions.changed`` per change, on each workspace member's own channel.

    Clients refetch their open room's resources (and so its transcript) from it. It used to
    fan out a ``room.resources.changed`` to every room the human is in, one membership read and
    publish at a time: ~28 round trips on every relay frame for the founder, which crossed the
    desktop's 10 s ack deadline and looped reconnects (prod 2026-09-24). Owner ruling: one hint.
    ``transcript`` marks a streaming batch: open panes refetch, the sidebar list ignores it (it
    lists liveness, not content, and a streaming teammate would refetch it once a second).
    """
    from core.services.live_events import publish_owner_users

    try:
        await publish_owner_users(
            owner,
            make_live_event(
                "workspace.sessions.changed", {"humanUserId": human, **({"transcript": True} if transcript else {})}
            ),
        )
    except Exception:
        # Hints are expendable; authorized HTTP polling repairs lost hints.
        logger.warning("Workspace session hint failed")


#: Heartbeat fields whose change counts as the user doing something (stamps lastEventAt).
_PUBLIC_FIELDS = ("status", "activeTurnId", "title", "cwdName", "lastAssistantMessage", "controlUnavailableReason")


#: A session "in a turn": working, or paused on an approval inside that turn.
_TURN_STATUSES = ("busy", "waiting-approval")
#: A turn shorter than this finishing is not worth a notification (the report's 60 s).
FINISHED_NOTIFY_MIN_S = 60
ACTIVITY_TTL_S = 90 * 24 * 3600  # the projector's message rows keep the same
_tasks: set[asyncio.Task] = set()


#: One "finished" notice per moment: a background subagent ending and the parent turn it
#: belongs to ending land 1-15 s apart (measured 2026-09-26), and are one thing to the owner.
FINISHED_COLLAPSE_S = 30


def _attention(row: dict, status: str, now: int) -> Literal["waiting", "finished"] | None:
    """The owner-facing state change a heartbeat carries, if any."""
    if status == "waiting-approval" and row["status"] != "waiting-approval":
        return "waiting"
    busy_since = row.get("busySince")
    if (
        status == "idle"
        and row["status"] in _TURN_STATUSES
        # A subagent that ended inside this turn deferred its notice to the turn's end.
        and (row.get("subagentEnded") or (busy_since and now - busy_since >= FINISHED_NOTIFY_MIN_S))
        and now - row.get("finishedNotifiedAt", 0) >= FINISHED_COLLAPSE_S
    ):
        return "finished"
    return None


def _spawn(coro) -> None:
    # Off the heartbeat path: the desktop acks within 10 s and a push is an Expo round trip.
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def attention_copy(row: dict, state: str, subagent: str | None = None) -> tuple[str, str]:
    """ "Claude · <session title>" over "Waiting for you: …" / "Finished: <reply's first line>".
    ``subagent`` is a finished background subagent's own line ('Agent "…" finished'), which is the body."""
    from core.services import notification_copy as copy

    tool = {"claude": "Claude", "codex": "Codex"}.get(row.get("provider", ""), "Session")
    where = copy.line(row.get("title"), 40) or row["sessionId"][:8]
    if subagent:
        return copy.title(tool, where), copy.line(subagent)
    if state == "waiting":
        return copy.title(tool, where), "Waiting for you: approve or answer it in your terminal"
    reply = copy.line(row.get("lastAssistantMessage"))
    return copy.title(tool, where), copy.line(f"Finished: {reply}") if reply else "Finished its turn"


async def notify_attention(row: dict, state: str, subagent: Event | None = None) -> None:
    """Tell the session's OWNER (sessions have no watchers): an Activity row, a live
    event for desktop/web notifications, and a phone push. Best effort, each on its own.
    ``subagent`` is a background subagent's end event; it notifies as its parent would."""
    from core.services.live_events import get_live_event_publisher
    from core.services.push_service import send_push_to_user

    owner, human, sid = row["ownerId"], row["humanUserId"], row["sessionId"]
    line = (subagent.text.strip() or "A subagent finished.")[:180] if subagent else None
    title, body = attention_copy(row, state, line)
    created = utc_now_iso()
    # A subagent ends once; its own id keeps a replayed batch to one notification.
    key = f"subagent|{subagent.subagent}" if subagent else row["revision"]
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-attention|{sid}|{key}"))
    try:
        await store.put_activity_row(
            {
                "pk": store.activity_pk(owner, human),
                "sk": store.activity_sk(created, f"session:{event_id}"),
                "kind": "session",
                "subtype": state,
                "sessionId": sid,
                **({"waitingSince": row["waitingSince"]} if state == "waiting" else {}),
                **({"subagent": line} if line else {}),
                "createdAt": created,
                "expiresAt": int(time.time()) + ACTIVITY_TTL_S,
            }
        )
    except Exception:
        logger.warning("Session attention activity row failed", exc_info=True)
    push = {"title": title, "body": body, "data": {"source": "session", "sessionId": sid}}
    try:
        await get_live_event_publisher().publish_user(
            owner,
            human,
            make_live_event(
                "workspace.session.attention", {"sessionId": sid, "state": state, "push": push}, event_id=event_id
            ),
        )
    except Exception:
        logger.warning("Session attention live event failed")
    await send_push_to_user(human, title, body, push["data"])


async def post_session_replies(row: dict) -> None:
    """Match each admitted prompt to its transcript turn, then post idempotently.

    A pointer survives a failed room write. The next idle heartbeat retries it; only a
    successful write (or a definite loss of room visibility) removes the pointer.
    """
    owner, sid = row["ownerId"], row["sessionId"]
    try:
        pointers = await repo.list_replyto(owner, sid)
        if not pointers:
            return
        if row.get("unshared"):
            for pointer in pointers:
                await repo.delete_replyto(owner, sid, pointer["requestId"])
            return
        end_seq = int(row.get("eventSeq", 0))
        start_seq = min(int(pointer["afterEventSeq"]) for pointer in pointers) + 1
        if start_seq > end_seq:
            return
        # ponytail: scan this session's events after the oldest pending command;
        # index provider message ids if long turns make idle heartbeats costly.
        events = await store._query(
            repo.session_pk(owner, sid), between=(f"EVENT#{start_seq:020d}", f"EVENT#{end_seq:020d}")
        )
    except Exception:
        logger.warning("session reply lookup failed", exc_info=True)
        return
    for pointer in pointers:
        request_id = pointer["requestId"]
        try:
            if int(pointer["expiresAt"]) <= int(time.time()):
                await repo.delete_replyto(owner, sid, request_id)
                continue
            start = next(
                (i for i, event in enumerate(events) if _matches_reply_prompt(event, pointer)),
                None,
            )
            if start is None:
                continue  # the transcript has not reached this admitted prompt yet
            ended = next(
                (
                    i
                    for i in range(start + 1, len(events))
                    if events[i].get("lifecycle") in {"turn_end", "turn_aborted"}
                ),
                None,
            )
            if ended is None:
                continue
            if events[ended].get("lifecycle") == "turn_aborted":
                await repo.delete_replyto(owner, sid, request_id)
                continue
            answer_blocks = [
                event
                for event in events[start + 1 : ended]
                if event.get("kind") == "text"
                and event.get("role") == "assistant"
                and not event.get("subagent")
                and event.get("text", "").strip()
            ]
            if not answer_blocks:
                continue
            final = answer_blocks[-1]
            final_id = final.get("itemId")
            reply = "".join(
                event["text"]
                for event in answer_blocks
                if event is final or (final_id and event.get("itemId") == final_id)
            ).strip()
            room_id = pointer["roomId"]
            tool = {"claude": "Claude Code", "codex": "Codex"}.get(pointer["provider"], "Session")
            link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=session&session={sid}"
            prefix = f"**{tool} · {pointer['where']}** replied:\n\n"
            suffix = f"\n\n[Open session]({link})"
            budget = MESSAGE_TEXT_MAX_BYTES - len((prefix + suffix).encode("utf-8")) - 80
            if len(reply.encode("utf-8")) > budget:
                reply = (
                    reply.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip()
                    + "\n\n[Truncated; open the session for the full answer.]"
                )
            text = prefix + reply + suffix
            for attempt in range(2):
                room = await store.get_room(room_id)
                if (
                    room is None
                    or room.get("deletedAt")
                    or room.get("archivedAt")
                    or room.get("ownerId") != owner
                    or await store.has_foreign_members(room_id, owner)
                ):
                    await repo.delete_replyto(owner, sid, request_id)
                    break
                origin = await store.get_message_by_id(room_id, pointer["messageId"])
                if (
                    origin is None
                    or origin.get("deletedAt")
                    or origin.get("author", {}).get("kind") != "human"
                    or await store.get_member(room_id, "human", origin["author"]["id"]) is None
                ):
                    await repo.delete_replyto(owner, sid, request_id)
                    break
                try:
                    await store.create_message(
                        room_id=room_id,
                        owner_id=owner,
                        idemp_pk=store.owner_pk(owner),
                        idemp_sk=store.agent_idemp_sk(f"session-reply:{request_id}"),
                        req_hash=store.request_hash(room_id, request_id, text),
                        author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
                        text=text,
                        mentions=[],
                        reply_to_message_id=origin["messageId"],
                        thread_root_message_id=origin.get("threadRootMessageId") or origin["messageId"],
                        run_id=None,
                        dispatch_targets=[],
                        source_agent_id=None,
                        client_message_id=None,
                        thread_root_seq=origin.get("threadRootSeq") or origin.get("messageSeq"),
                        silent=True,
                        expected_room_revision=int(room["revision"]),
                    )
                except store.MessageConflict:
                    if attempt == 0:
                        continue  # one immediate access refresh, including an ended session
                    raise
                command = await repo.get_command(owner, sid, request_id)
                if command is None:
                    raise RuntimeError("session reply command disappeared before receipt completion")
                if not command.get("replyPosted"):
                    if command.get("state") != "applied":
                        raise RuntimeError("session reply command is not applied")
                    await repo.replace_command(
                        command,
                        _revised(command, replyPosted=True, updatedAt=int(time.time())),
                    )
                await repo.delete_replyto(owner, sid, request_id)
                break
        except Exception:
            logger.warning("session reply line failed request=%s", request_id, exc_info=True)


def _matches_reply_prompt(event: dict, pointer: dict) -> bool:
    if event.get("role") != "user":
        return False
    if pointer["provider"] == "codex":
        request_id = event.get("originRequestId")
        return (
            request_id == pointer["requestId"]
            if request_id is not None
            else event.get("itemId") == pointer["providerMessageId"]
        )
    return event.get("itemId") == pointer["providerMessageId"]


_NO_PROMPT = object()


def finished_background_subagents(events: list[Event]) -> list[Event]:
    """Background subagents this batch reports ended, on the parent's terms: a run shorter
    than FINISHED_NOTIFY_MIN_S is not worth a notification. An unknown duration notifies
    (a failed agent reports none). A foreground subagent's end is the parent's own turn."""
    found = []
    for event in events:
        if event.lifecycle != "subagent_end" or not event.subagent or event.tool is None:
            continue
        result = event.tool.result if isinstance(event.tool.result, dict) else {}
        took = result.get("durationMs")
        if result.get("background") is not True or (
            isinstance(took, int | float) and took < FINISHED_NOTIFY_MIN_S * 1000
        ):
            continue
        found.append(event)
    return found


def pending_approval(events: list[Event]) -> object:
    """The batch's last prompt: a dict when the desktop tagged it answerable (an approval id and
    the digest of the exact tool input), None when it is terminal-only (an older desktop, a
    question), ``_NO_PROMPT`` when the batch reports none -- a later prompt always replaces an
    earlier one."""
    found = _NO_PROMPT
    for event in events:
        tool = event.tool
        if event.lifecycle != "approval_pending":
            continue
        found = None
        if not event.item_id or tool is None:
            continue
        try:
            found = sessions.PendingApproval.model_validate(
                {**(tool.input if isinstance(tool.input, dict) else {}), "approvalId": event.item_id, "tool": tool.name}
            ).model_dump(by_alias=True)
        except ValidationError:
            found = None
    return found


class LocalSessionRelay:
    def __init__(self, connections: ConnectionService, management: ManagementApiClient):
        self.connections, self.management = connections, management

    async def connect(self, connection_id: str) -> tuple[dict, dict]:
        row = await asyncio.to_thread(self.connections.get_connection, connection_id)
        if (
            not row
            or row.get("connection_type") != "local-session"
            or row.get("auth_kind") != "device"
            or row.get("client_id") != CLIENT_ID
            or not row.get("device_id")
        ):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Verified local session connection required.")
        owner = row.get("org_id") or row["user_id"]
        enrollment = await sessions._enrolled(owner, row["user_id"], row["device_id"])
        try:
            int(row["connected_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Connection identity is incomplete.") from exc
        return row, enrollment

    def _connection_check(self, cid: str, connection: dict) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.connections.table_name,
                "Key": {"connectionId": cid},
                "ConditionExpression": "userId = :user AND orgId = :org AND deviceId = :device AND authKind = :auth AND clientId = :client AND connectionType = :type AND connectedAt = :at",
                "ExpressionAttributeValues": {
                    ":user": connection["user_id"],
                    ":org": connection.get("org_id") or "",
                    ":device": connection["device_id"],
                    ":auth": "device",
                    ":client": CLIENT_ID,
                    ":type": "local-session",
                    ":at": connection["connected_at"],
                },
            }
        }

    async def _current(self, cid: str, connection: dict, sid: str, generation: str | None) -> dict:
        owner = connection.get("org_id") or connection["user_id"]
        row = await repo.get_session(owner, sid)
        if not row:
            raise _conflict("missing", sid=sid, cid=cid)
        if row.get("humanUserId") != connection["user_id"] or row.get("deviceId") != connection["device_id"]:
            raise _conflict("ownership", sid=sid, cid=cid)
        if row.get("connectionId") != cid:
            raise _conflict("older connection", sid=sid, cid=cid)
        if generation is not None and row.get("generation") != generation:
            raise _conflict("generation chain", sid=sid, cid=cid)
        return row

    async def handle_message(self, cid: str, body: dict) -> dict:
        check_envelope(body)
        try:
            message = _WIRE.validate_python(body)
        except ValidationError as exc:
            # Never return/log validation input: it can contain transcript or secrets.
            raise ApiProblem("INVALID_REQUEST", "Invalid local session envelope.") from exc
        connection, enrollment = await self.connect(cid)
        owner, human, device = (
            connection.get("org_id") or connection["user_id"],
            connection["user_id"],
            connection["device_id"],
        )
        for _ in range(6):
            try:
                notify_resources = True
                if isinstance(message, Register):
                    descriptor = message.session
                    sid = repo.session_id(
                        owner,
                        device,
                        descriptor.provider,
                        descriptor.profile_fingerprint,
                        descriptor.native_conversation_id,
                    )
                    previous = await repo.get_session(owner, sid)
                    if previous:
                        if previous["humanUserId"] != human or previous["deviceId"] != device:
                            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Session ownership cannot change.")
                        if (int(connection["connected_at"]), cid) < (
                            int(previous.get("connectedAt", "0")),
                            previous.get("connectionId", ""),
                        ):
                            raise _conflict("older connection", sid=sid, cid=cid)
                        if (
                            previous["generation"] != descriptor.generation
                            and message.previous_generation != previous["generation"]
                        ):
                            raise _conflict("generation chain", sid=sid, cid=cid)
                        if (
                            previous["generation"] == descriptor.generation
                            and previous.get("startId")
                            and descriptor.start_id
                            and previous["startId"] != descriptor.start_id
                        ):
                            raise _conflict("start identity changed", sid=sid, cid=cid)
                    if descriptor.start_id and (
                        descriptor.control_unavailable_reason is not None or descriptor.kind != "interactive"
                    ):
                        raise _conflict("unverified start identity", sid=sid, cid=cid)
                    # The desktop is the only party that can verify the native runtime (live process,
                    # transcript rows, inbox socket) and it fails closed with a controlUnavailableReason.
                    # A backend version list could only drift from it, and did (Claude 2.1.269).
                    compatible = descriptor.control_unavailable_reason is None
                    row = _revised(
                        previous or {},
                        **descriptor.model_dump(by_alias=True),
                        pk=repo.session_pk(owner, sid),
                        sk="META",
                        ownerId=owner,
                        humanUserId=human,
                        deviceId=device,
                        sessionId=sid,
                        connectionId=cid,
                        connectedAt=connection["connected_at"],
                        commandSeq=(previous or {}).get("commandSeq", 0),
                        eventSeq=(previous or {}).get("eventSeq", 0),
                        onlineUntil=int(time.time()) + LIVENESS_SECONDS,
                        capabilities=sessions.Capabilities(send_mode="queue" if compatible else "none").model_dump(
                            by_alias=True
                        ),
                        capabilitiesVerified=compatible,
                        roomReplyRequestId=False,
                        lastEventAt=int(time.time()),
                    )
                    if descriptor.start_id is None and previous and previous["generation"] == descriptor.generation:
                        row["startId"] = previous.get("startId")
                    elif descriptor.start_id is None:
                        row.pop("startId", None)
                    # A reconnect may find no prompt in the bounded desktop scan.
                    # Keep the same process's title; a new generation starts fresh.
                    if previous and previous["generation"] == descriptor.generation and descriptor.title is None:
                        row["title"] = previous.get("title")
                    if descriptor.status in _TURN_STATUSES and (previous or {}).get("status") not in _TURN_STATUSES:
                        row["busySince"] = int(time.time())
                    # A resumed conversation re-registers under the same id: registering un-ends it.
                    row.pop("endedAt", None)
                    row.pop("endedReason", None)
                    # A prompt belongs to the generation that asked it; a new one re-reports.
                    # So does teammate control, and so do the people the owner let in: a restarted
                    # process is a new run the owner has not opted into, even when no Ended frame
                    # arrived for the old one. A relay reconnect keeps the generation, and all three.
                    if previous is None or previous["generation"] != descriptor.generation:
                        row.pop("pendingApproval", None)
                        row.pop("privateSendApproval", None)
                        row.pop("nativeSendIntent", None)
                        row.pop("teamControl", None)
                        for key in sessions.JOIN_FIELDS:
                            row.pop(key, None)
                    extra = [self._connection_check(cid, connection)]
                    pointer = row.get("nativeSendIntent") or {}
                    if pointer.get("connectionId") != cid and pointer.get("state") == "pending_approval":
                        intent_key = {"pk": row["pk"], "sk": f"NATIVE_INTENT#{pointer['requestId']}"}
                        intent = await store._get(**intent_key)
                        now = int(time.time())
                        if (
                            previous.get("endedAt") is None
                            and not row.get("unshared")
                            and pointer.get("expiresAt", 0) > now
                            and pointer.get("sourceGeneration") == row["generation"]
                            and intent
                            and intent.get("requestId") == pointer["requestId"]
                            and intent.get("approvalId") == pointer.get("approvalId")
                            and intent.get("inputDigest") == pointer.get("inputDigest")
                            and intent.get("expiresAt") == pointer["expiresAt"]
                            and intent.get("connectionId") == pointer.get("connectionId")
                            and intent.get("sourceGeneration") == row["generation"]
                            and intent.get("sourceConversationId") == row["nativeConversationId"]
                            and intent.get("sourceTurnId") == pointer.get("sourceTurnId")
                            and intent.get("deviceId") == device
                            and intent.get("humanUserId") == human
                            and intent.get("state") == "pending_approval"
                            and intent.get("expiresAt", 0) > now
                        ):
                            row["nativeSendIntent"] = {**pointer, "connectionId": cid}
                            extra.append(
                                {
                                    "Update": {
                                        "TableName": store._table().name,
                                        "Key": intent_key,
                                        "UpdateExpression": "SET connectionId = :new",
                                        "ConditionExpression": (
                                            "connectionId = :old AND approvalId = :approval AND inputDigest = :digest "
                                            "AND sourceGeneration = :generation AND #state = :pending AND expiresAt > :now"
                                        ),
                                        "ExpressionAttributeNames": {"#state": "state"},
                                        "ExpressionAttributeValues": {
                                            ":new": cid,
                                            ":old": pointer["connectionId"],
                                            ":approval": pointer["approvalId"],
                                            ":digest": pointer["inputDigest"],
                                            ":generation": row["generation"],
                                            ":pending": "pending_approval",
                                            ":now": now,
                                        },
                                    }
                                }
                            )
                        else:
                            row.pop("nativeSendIntent", None)
                    # pickupAllowed/pickupSnapshot ride along from `previous`: content sharing, not control.
                    if previous is None:
                        picked_up_from = await session_pickup.lineage(owner, sid)
                        if picked_up_from:
                            row["pickedUpFrom"] = picked_up_from
                    await repo.put_session(row, previous, enrollment, extra=extra)
                    if previous is None or previous["generation"] != row["generation"]:
                        # The desktop may discover an already-running native process: shared, not started.
                        _capture_lifecycle(row, "local_session_shared")
                    # The workspace session list enumerates humans via their
                    # private user stream, which a desktop-only sharer never
                    # creates by opening chat. First register carries it so the
                    # sharer is enumerable (and reachable by workspace hints).
                    # Best-effort: registration itself already succeeded.
                    try:
                        await store.ensure_user_stream(owner, human)
                    except Exception:
                        logger.warning("Local session register left without a user stream for %s", human)
                    await self.dispatch_session(owner, sid)
                    response = {
                        "type": "local.session.registered",
                        "sessionId": sid,
                        "generation": row["generation"],
                        "afterSeq": row["eventSeq"],
                    }
                    logger.info(
                        "local session registered",
                        extra={"sid": sid, "cid": cid, "cursor": row["eventSeq"], "resumed": previous is not None},
                    )
                    response["reconcile"] = await self._reconcile_for(owner, sid, row["generation"])
                    response["serverTime"] = int(time.time())
                    response["features"] = FEATURES
                elif isinstance(message, DeviceHeartbeat):
                    response = await session_pickup.ack_with_pickup(
                        await self._device_heartbeat(cid, connection, enrollment, message), owner, human, device
                    )
                    notify_resources = False
                elif isinstance(message, Ended):
                    response = await session_pickup.ack_with_pickup(
                        await self._ended(cid, connection, enrollment, message), owner, human, device
                    )
                    notify_resources = False
                else:
                    sid = str(message.session_id)
                    # A current connection may reconcile its journal from an older
                    # execution; events/heartbeats must match the current generation.
                    row = await self._current(
                        cid, connection, sid, None if isinstance(message, Receipt) else message.generation
                    )
                    if isinstance(message, Events):
                        response = await self._events(cid, connection, enrollment, row, message)
                        # _events throttles and publishes its own hint; the
                        # generic post-loop publish below must not double it.
                        notify_resources = False
                    else:
                        response = await self._receipt(cid, connection, enrollment, row, message)
                        await self.dispatch_session(owner, sid)
                if notify_resources:
                    await publish_resources_changed(owner, human)
                check_envelope(response)
                return response
            except ClientError as exc:
                if not store._is_cancelled(exc):
                    raise
                connection, enrollment = await self.connect(cid)
        raise _conflict("revision contention", cid=cid)

    async def _reconcile_for(self, owner: str, sid: str, generation: str) -> dict | None:
        """The one command whose outcome the desktop journal must still settle."""
        unresolved_commands = [
            c
            for c in await repo.list_commands(owner, sid)
            if c["state"] in {"dispatched", "unknown"} and not c.get("settled")
        ]
        # Prefer the current generation's blocking receipt. Retain older
        # unknowns and still reconcile them when none is current.
        unresolved = next(
            (c for c in unresolved_commands if c["expectedGeneration"] == generation),
            next(iter(unresolved_commands), None),
        )
        if unresolved is None:
            return None
        return {
            "requestId": unresolved["requestId"],
            "generation": unresolved["expectedGeneration"],
            "state": unresolved["state"],
            **({"expectedTurnId": unresolved["expectedTurnId"]} if unresolved.get("expectedTurnId") else {}),
        }

    async def _device_heartbeat(self, cid: str, connection: dict, enrollment: dict, message: DeviceHeartbeat) -> dict:
        """One frame per device renews every mirror it still owns; the rest come back rejected."""
        # ponytail: a read, a lease write and a delivery pass per listed session, capped at 24;
        # batch the reads if a device ever carries enough live mirrors for it to show.
        owner, now, started = connection.get("org_id") or connection["user_id"], int(time.time()), time.monotonic()
        rejected, reconcile = [], []
        # One workspace hint per frame, not per session: every session here is the same human's.
        # It still fires when the frame fails part-way, because the retry finds those rows already
        # matching and would not hint again.
        hint = False
        try:
            for item in message.sessions:
                row = await repo.get_session(owner, item.session_id)
                if (
                    row is None
                    or row["deviceId"] != connection["device_id"]
                    or row["generation"] != item.generation
                    or row.get("connectionId") != cid
                    or row.get("endedAt") is not None
                ):
                    # Unknown, superseded or ended here: the desktop re-registers it.
                    rejected.append(item.session_id)
                    continue
                labels = {
                    key: value
                    for key, value in (
                        ("title", item.title),
                        ("cwdName", item.cwd_name),
                        ("lastAssistantMessage", item.last_assistant_message),
                    )
                    if value is not None
                }
                updated = _revised(
                    row,
                    status=item.status,
                    activeTurnId=item.active_turn_id,
                    controlUnavailableReason=item.control_unavailable_reason,
                    onlineUntil=now + LIVENESS_SECONDS,
                    workspace=item.workspace.model_dump(by_alias=True),
                    **labels,
                )
                if item.capabilities is not None and row["capabilitiesVerified"]:
                    caps = item.capabilities
                    if caps.send_mode != "queue" or (row["provider"] != "codex" and (caps.steer or caps.interrupt)):
                        rejected.append(item.session_id)
                        continue
                    updated["capabilities"] = caps.model_dump(by_alias=True)
                # Heartbeats may withdraw authority. A fresh registration is
                # required to restore it after runtime revalidation.
                if item.control_unavailable_reason is not None:
                    updated.update(
                        capabilities=sessions.Capabilities().model_dump(by_alias=True), capabilitiesVerified=False
                    )
                if item.status in _TURN_STATUSES and row["status"] not in _TURN_STATUSES:
                    updated["busySince"] = now
                attention = _attention(row, item.status, now)
                if attention == "waiting":
                    updated["waitingSince"] = now  # which wait an Activity item is about
                if attention == "finished":
                    updated["finishedNotifiedAt"] = now
                if item.status not in _TURN_STATUSES:
                    updated.pop("subagentEnded", None)
                if item.stats is not None:
                    updated["stats"] = item.stats.model_dump(by_alias=True, exclude_none=True)
                if item.room_reply_request_id is not None and row["provider"] == "codex":
                    updated["roomReplyRequestId"] = item.room_reply_request_id and row["capabilitiesVerified"]
                # The prompt is answered (here or in the terminal) once the session stops waiting.
                native_intent = row.get("nativeSendIntent") or {}
                keep_native_intent = native_intent.get("expiresAt", 0) > int(time.time())
                if item.status != "waiting-approval" and not keep_native_intent:
                    updated.pop("pendingApproval", None)
                    updated.pop("privateSendApproval", None)
                    updated.pop("nativeSendIntent", None)
                moved = any(row.get(field) != updated.get(field) for field in _PUBLIC_FIELDS) or any(
                    (row.get("stats") or {}).get(key) != (updated.get("stats") or {}).get(key)
                    for key in ("model", "permissionMode")
                )
                scanned = row.get("workspace") != updated.get("workspace")
                controls_changed = row.get("capabilities") != updated.get("capabilities")
                if moved:
                    updated["lastEventAt"] = now
                await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection)])
                if attention:
                    # The write above is revision-fenced, so exactly one frame wins a transition:
                    # one state change is one notification, retries included.
                    _spawn(notify_attention(updated, attention))
                if item.status == "idle":
                    # Transcript upload can follow the busy->idle heartbeat, and a room
                    # write can fail transiently: each idle heartbeat retries pending replies.
                    _spawn(post_session_replies(updated))
                # A workspace scan is worth a refetch but is not the user doing something,
                # so it hints without stamping lastEventAt. A lapsed lease reads as
                # offline, so recovery must reach the room even when nothing else moved;
                # renewal makes that fire once.
                if moved or scanned or controls_changed or row["onlineUntil"] <= now:
                    hint = True
                # Reconcile after delivery: this heartbeat's own dispatch is part of what the journal must settle.
                await self.dispatch_session(owner, item.session_id)
                pending = await self._reconcile_for(owner, item.session_id, row["generation"])
                if pending is not None:
                    reconcile.append({"sessionId": item.session_id, **pending})
            return {
                "type": "local.device.heartbeat.ack",
                "rejected": rejected,
                "reconcile": reconcile,
                "serverTime": now,
            }
        finally:
            if hint:
                await publish_resources_changed(owner, connection["user_id"])
            logger.info(
                "local device heartbeat",
                extra={"cid": cid, "sessions": len(message.sessions), "ms": int((time.monotonic() - started) * 1000)},
            )

    async def _ended(self, cid: str, connection: dict, enrollment: dict, message: Ended) -> dict:
        owner = connection.get("org_id") or connection["user_id"]
        row = await repo.get_session(owner, message.session_id)
        ack = {"type": "local.session.ended.ack", "sessionId": message.session_id}
        if row is None or row["deviceId"] != connection["device_id"] or row["generation"] != message.generation:
            return ack
        if row.get("endedAt") is not None:
            _spawn(post_session_replies(row))  # a replayed Ended ack can repair a failed room write
            return ack
        # A device clock ahead of the server cannot park a session in the future,
        # where it would outrank every live row in a lastEventAt ordering.
        ended_at = min(message.ended_at, int(time.time()))
        updated = _revised(row, status="ended", endedAt=ended_at, lastEventAt=ended_at, endedReason=message.reason)
        updated.pop("pendingApproval", None)
        updated.pop("privateSendApproval", None)
        updated.pop("nativeSendIntent", None)
        extra = [self._connection_check(cid, connection)]
        # "Let teammates control this session" turns itself off with the session.
        if updated.pop("teamControl", False):
            audit = sessions.audit_row(owner, row["sessionId"], None, "team_control_ended")
            extra.append({"Put": {"TableName": store._table().name, "Item": audit}})
        # So do the people the owner let in, and every open ask to join.
        for key in sessions.JOIN_FIELDS:
            updated.pop(key, None)
        await repo.put_session(updated, row, enrollment, extra=extra)
        _capture_lifecycle(updated, "local_session_ended", end_reason=message.reason)
        _spawn(post_session_replies(updated))
        await publish_resources_changed(owner, row["humanUserId"])
        return ack

    async def _events(self, cid: str, connection: dict, enrollment: dict, row: dict, message: Events) -> dict:
        sid = row["sessionId"]
        first_seq = message.after_seq + 1
        previous = await store._get(row["pk"], f"EVENT#{first_seq:020d}")
        hash_body = message.model_dump(mode="json", by_alias=True)
        if hash_body["privateApproval"] is None:
            hash_body.pop("privateApproval")  # Old event batches keep their existing replay hash.
        digest = store.request_hash(hash_body)
        if previous:
            if previous.get("batchHash") != digest:
                raise _conflict("batch hash", sid=sid, first_seq=first_seq)
            return {
                "type": "local.session.events.ack",
                "sessionId": row["sessionId"],
                "batchId": message.batch_id,
                "afterSeq": previous["batchEndSeq"],
            }
        if message.after_seq != row.get("eventSeq", 0):
            raise _conflict("cursor", sid=sid, expected=row.get("eventSeq", 0), got=message.after_seq)
        if row.get("unshared"):
            return await self._drop_paused(cid, connection, enrollment, row, message, digest)
        now = int(time.time() * 1000)
        has_text = any(event.kind == "text" for event in message.events)
        if has_text and now < row.get("nextTextAt", 0):
            raise ApiProblem("RATE_LIMITED", "Text batches are limited to two per second.", retry_after=0.5)
        end = message.after_seq + len(message.events)
        updates = []
        for seq, event in enumerate(message.events, first_seq):
            value = sessions.LocalSessionEvent(seq=seq, **event.model_dump(exclude_none=True)).model_dump(
                by_alias=True, exclude_none=True
            )
            value.update(pk=row["pk"], sk=f"EVENT#{seq:020d}", createdAt=int(time.time()))
            if seq == first_seq:
                value.update(batchHash=digest, batchEndSeq=end)
            updates.append(
                {
                    "Put": {
                        "TableName": store._table().name,
                        "Item": value,
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                }
            )
        hint = now >= row.get("nextHintAt", 0)
        approval = pending_approval(message.events)
        detail = message.private_approval
        if detail is not None:
            from core.services.native_session_tools import _send_args

            _send_args({"destination": detail.destination, "text": detail.text, "requestId": detail.request_id})
            if (
                approval is None
                or approval is _NO_PROMPT
                or approval["tool"] != "mcp__lightbulb_native_sessions__send"
                or approval["approvalId"] != detail.approval_id
                or approval["inputDigest"] != detail.input_digest
            ):
                raise ApiProblem("INVALID_REQUEST", "Private approval does not match this prompt.")
        if approval is not None and approval is not _NO_PROMPT:
            # A prompt is live the moment it is reported, so the hint is not throttled.
            hint = True
        updated = _revised(
            row,
            eventSeq=end,
            lastEventAt=now // 1000,
            nextTextAt=now + 500 if has_text else row.get("nextTextAt", 0),
            **({"nextHintAt": now + 1000} if hint else {}),
        )
        # One notice per moment, not one per subagent: a parent still in its turn will
        # report (with its own summary) when that turn ends; otherwise the last one to
        # end speaks, unless a "finished" just went out for this session.
        ended = finished_background_subagents(message.events)
        notify_ended = None
        if ended and row["status"] in _TURN_STATUSES:
            updated["subagentEnded"] = True
        elif ended and now // 1000 - row.get("finishedNotifiedAt", 0) >= FINISHED_COLLAPSE_S:
            notify_ended = ended[-1]
            updated["finishedNotifiedAt"] = now // 1000
        if approval is not _NO_PROMPT:
            updated.pop("pendingApproval", None)
            updated.pop("privateSendApproval", None)
            if approval is not None:
                updated["pendingApproval"] = approval
                if detail is not None:
                    updated["privateSendApproval"] = detail.model_dump(by_alias=True)
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection), *updates])
        if any(event.lifecycle == "turn_end" for event in message.events):
            _spawn(post_session_replies(updated))
        # The write is cursor-fenced and a replayed batch returned above, so each end notifies once.
        if notify_ended is not None:
            _spawn(notify_attention(updated, "finished", notify_ended))
        if hint:
            await publish_resources_changed(row["ownerId"], row["humanUserId"], transcript=True)
        return {
            "type": "local.session.events.ack",
            "sessionId": row["sessionId"],
            "batchId": message.batch_id,
            "afterSeq": end,
        }

    async def _drop_paused(
        self, cid: str, connection: dict, enrollment: dict, row: dict, message: Events, digest: str
    ) -> dict:
        """The owner paused sharing: acknowledge the batch but store none of it.

        The first dropped batch leaves one notice at its first seq, which also keeps that
        batch replay-idempotent; later ones only advance the cursor, and a lost ack for one
        resyncs like any other cursor gap. The desktop keeps its cursor either way.
        """
        first_seq, end = message.after_seq + 1, message.after_seq + len(message.events)
        updates = []
        if not row.get("pauseNoticed"):
            notice = sessions.LocalSessionEvent(
                seq=first_seq, kind="text", role="system", text=PAUSED_NOTICE
            ).model_dump(by_alias=True, exclude_none=True)
            notice.update(
                pk=row["pk"],
                sk=f"EVENT#{first_seq:020d}",
                createdAt=int(time.time()),
                batchHash=digest,
                batchEndSeq=end,
            )
            updates.append(
                {
                    "Put": {
                        "TableName": store._table().name,
                        "Item": notice,
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                }
            )
        updated = _revised(row, eventSeq=end, pauseNoticed=True)
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection), *updates])
        if updates:
            await publish_resources_changed(row["ownerId"], row["humanUserId"], transcript=True)
        return {
            "type": "local.session.events.ack",
            "sessionId": row["sessionId"],
            "batchId": message.batch_id,
            "afterSeq": end,
        }

    def _session_check(self, row: dict) -> dict:
        return {
            "ConditionCheck": {
                "TableName": store._table().name,
                "Key": {"pk": row["pk"], "sk": "META"},
                "ConditionExpression": "revision = :revision",
                "ExpressionAttributeValues": {":revision": row["revision"]},
            }
        }

    async def _receipt(self, cid: str, connection: dict, enrollment: dict, row: dict, message: Receipt) -> dict:
        sid = row["sessionId"]
        command = await repo.get_command(row["ownerId"], sid, message.request_id)
        if not command or command["expectedGeneration"] != message.generation or command["state"] == "pending":
            raise _conflict("receipt command", sid=sid)
        outcome = dict(state=message.outcome, held=message.held, providerMessageId=message.provider_message_id)
        if message.settled:
            if (
                message.outcome != "unknown"
                or not command.get("expectedTurnId")
                or row["generation"] != message.generation
                or row.get("activeTurnId") == command["expectedTurnId"]
            ):
                raise _conflict("receipt settlement", sid=sid)
            outcome["settled"] = True
        if message.outcome == "applied" and command.get("originRoomId") and not message.provider_message_id:
            raise _conflict("mention receipt has no provider message id", sid=sid)
        if command["state"] in {"applied", "rejected"}:
            if any(command.get(key) != value for key, value in outcome.items()):
                raise _conflict("receipt outcome", sid=sid)
        else:
            extra = []
            if message.outcome == "applied" and command.get("originRoomId"):
                extra.append(
                    {
                        "Put": {
                            "TableName": store._table().name,
                            "Item": repo.replyto_item(
                                row["ownerId"],
                                sid,
                                room_id=command["originRoomId"],
                                message_id=command["originMessageId"],
                                request_id=message.request_id,
                                provider_message_id=message.provider_message_id,
                                after_event_seq=int(command.get("originEventSeq", 0)),
                                provider=row["provider"],
                                where=row.get("cwdName") or row.get("title") or sid[:8],
                            ),
                        }
                    }
                )
            await repo.replace_command(
                command,
                _revised(command, **outcome, updatedAt=int(time.time())),
                checks=[
                    repo.pairing_condition(row["ownerId"], row["humanUserId"], row["deviceId"], None),
                    repo.enrollment_condition(enrollment),
                    self._session_check(row),
                    self._connection_check(cid, connection),
                ],
                extra=extra,
            )
        return {
            "type": "local.session.receipt.ack",
            "sessionId": row["sessionId"],
            "requestId": message.request_id,
            "state": message.outcome,
        }

    async def _dispatch_checks(self, row: dict, command: dict, claim: bool) -> list[dict] | None:
        owner, sid = row["ownerId"], row["sessionId"]
        native_actor = (command.get("actor") or {}).get("kind") == "session"
        source_checks = []
        if native_actor:
            source = await repo.get_session(owner, command["actor"]["id"])
            if (
                not source
                or source.get("humanUserId") != command.get("sourceHumanUserId")
                or source.get("generation") != command.get("sourceSessionGeneration")
                or source.get("activeTurnId") != command.get("sourceTurnId")
                or source.get("unshared")
                or source.get("endedAt") is not None
                or int(source.get("onlineUntil") or 0) < int(time.time())
                or int(command.get("invocationExpiresAt") or 0) <= int(time.time())
            ):
                raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Source native session is no longer authorized.")
            source_connection, source_enrollment = await self.connect(source.get("connectionId") or "")
            if (
                source_connection["device_id"] != source["deviceId"]
                or source_connection["user_id"] != source["humanUserId"]
            ):
                raise _conflict("source connection changed", sid=sid)
            source_checks = [
                repo.pairing_condition(owner, source["humanUserId"], source["deviceId"], None),
                repo.enrollment_condition(source_enrollment),
                self._session_check(source),
                self._connection_check(source["connectionId"], source_connection),
            ]
            auth = sessions.AuthContext(
                user_id=source["humanUserId"], org_id=owner if owner != source["humanUserId"] else None
            )
        else:
            auth = sessions.command_caller(owner, command)
        current, enrollment = await sessions.authorize_session(auth, command["roomId"], sid, owner_only=True)
        if command.get("offlineIdentity") and command.get("actorUserId") != current["humanUserId"]:
            stream = await store.get_user_stream(owner, command["actorUserId"])
            if not stream or not stream.get("active"):
                raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Sender is no longer in this workspace.")
        sessions.require_command_scope(auth, current, command["kind"])
        sessions._require_control(current, sessions._public(sessions.LocalSessionCommand, command))
        if command.get("originRoomId") and current["provider"] == "codex" and not current.get("roomReplyRequestId"):
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Update Lightbulb on the Mac to use Codex room mentions.")
        connection, _ = await self.connect(current["connectionId"])
        if connection["device_id"] != current["deviceId"] or connection["user_id"] != current["humanUserId"]:
            raise _conflict("dispatch identity", sid=sid)
        now = int(time.time())
        if command["deliverBy"] <= now or (not claim and command.get("claimUntil", 0) <= now):
            raise _conflict("dispatch expiry", sid=sid)
        if current["revision"] != row["revision"]:
            # Transcript/liveness/admission writes also change META. Retry the
            # snapshot; only substantive authorization/control failures reject.
            return None
        return [
            *repo.room_conditions(owner, row["humanUserId"], command),
            *source_checks,
            repo.pairing_condition(owner, row["humanUserId"], row["deviceId"], None),
            repo.enrollment_condition(enrollment),
            self._session_check(row),
            self._connection_check(row["connectionId"], connection),
        ]

    async def dispatch_session(self, owner: str, sid: str) -> None:
        for _ in range(6):
            try:
                row = await repo.get_session(owner, sid)
                if not row:
                    return
                commands = await repo.list_commands(owner, sid)
                # Expire even commands behind an unresolved outcome; never queue old input indefinitely.
                for command in commands:
                    if command["state"] == "pending" and command["deliverBy"] <= int(time.time()):
                        await repo.replace_command(
                            command, _revised(command, state="rejected", updatedAt=int(time.time()))
                        )
                commands = await repo.list_commands(owner, sid)
                command = next(
                    (
                        c
                        for c in commands
                        if c["state"] not in {"applied", "rejected"}
                        and not (
                            c["state"] == "unknown"
                            and (c["expectedGeneration"] != row["generation"] or c.get("settled"))
                        )
                    ),
                    None,
                )
                if not command:
                    return
                if command["state"] == "dispatched":
                    if (
                        command["expectedGeneration"] != row["generation"]
                        or command.get("connectionId") != row.get("connectionId")
                        or command["deliverBy"] <= int(time.time())
                    ):
                        await repo.replace_command(
                            command, _revised(command, state="unknown", updatedAt=int(time.time()))
                        )
                        continue
                    return
                if command["state"] == "unknown" or command.get("claimUntil", 0) > int(time.time()):
                    return
                if row["status"] == "offline" or row["onlineUntil"] <= int(time.time()):
                    # The durable offline inbox waits for a fresh device lease. A stale
                    # connection record is not evidence that a native session is ready.
                    return
                if command["expectedGeneration"] != row["generation"] and command.get("offlineIdentity"):
                    identity = {
                        key: row.get(key)
                        for key in ("deviceId", "provider", "profileFingerprint", "nativeConversationId")
                    }
                    if command["offlineIdentity"] != identity:
                        await repo.replace_command(
                            command, _revised(command, state="rejected", updatedAt=int(time.time()))
                        )
                        continue
                    # Only a never-dispatched offline send can follow a verified
                    # registration successor. The session ID is derived from this
                    # native identity, and Register validates its generation chain.
                    await repo.replace_command(
                        command,
                        _revised(command, expectedGeneration=row["generation"]),
                        checks=[self._session_check(row)],
                    )
                    continue
                # Strong record absence proves no network attempt has happened in this path.
                if not row.get("connectionId") or not await asyncio.to_thread(
                    self.connections.get_connection, row["connectionId"]
                ):
                    return
                try:
                    checks = await self._dispatch_checks(row, command, True)
                    if checks is None:
                        continue
                except ApiProblem:
                    await repo.replace_command(command, _revised(command, state="rejected", updatedAt=int(time.time())))
                    continue
                # Resolved before the claim: a membership read inside the claim window
                # can spend the lease and turn a dispatchable command into a rejection.
                actor = await _actor_label(command)
                claim = _revised(command, claimUntil=min(int(time.time()) + CLAIM_SECONDS, command["deliverBy"]))
                await repo.replace_command(command, claim, checks=checks)
                row = await repo.get_session(owner, sid)
                try:
                    checks = await self._dispatch_checks(row, claim, False)
                    if checks is None:
                        # No network attempt occurred. Release only our CAS-fenced
                        # claim so a fresh snapshot can retry without lease delay.
                        await repo.replace_command(claim, _revised(claim, claimUntil=0))
                        continue
                    public = sessions._public(sessions.CommandReceipt, claim).model_dump(mode="json", by_alias=True)
                    public["state"] = "dispatched"
                    public.pop("replyPosted", None)  # HTTP-only progress; the desktop rejects unknown command fields.
                    envelope = {"type": "local.session.command", "command": public, "actor": actor}
                    if command.get("originRoomId"):
                        envelope["roomMention"] = True
                    check_envelope(envelope)
                except ApiProblem:
                    await repo.replace_command(claim, _revised(claim, state="rejected", updatedAt=int(time.time())))
                    continue
                dispatched = _revised(
                    claim, state="dispatched", connectionId=row["connectionId"], updatedAt=int(time.time())
                )
                await repo.replace_command(claim, dispatched, checks=checks)
                if int(time.time()) >= claim["claimUntil"]:
                    # A slow DB call consumed the lease. No network attempt occurred.
                    await repo.replace_command(
                        dispatched, _revised(dispatched, state="rejected", updatedAt=int(time.time()))
                    )
                    continue
                try:
                    sent = await self.management.send_message_async(row["connectionId"], envelope)
                except Exception:
                    sent = False
                if not sent:
                    # Even Gone can follow a delivered SDK retry. Never replay an attempted post.
                    await repo.replace_command(
                        dispatched, _revised(dispatched, state="unknown", updatedAt=int(time.time()))
                    )
                return
            except ClientError as exc:
                if not store._is_cancelled(exc):
                    raise
        # Another worker owns progression. Heartbeat/POST/receipt will drive it again.


@lru_cache(maxsize=1)
def get_local_session_relay() -> LocalSessionRelay:
    return LocalSessionRelay(ConnectionService(), ManagementApiClient())
