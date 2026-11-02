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
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import Field, TypeAdapter, ValidationError

from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.observability.session_analytics import capture_session_event
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup, session_presence, session_resume, session_screen
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem, ContractModel, make_live_event

CLIENT_ID = "lightbulb-local-sessions"
LIVENESS_SECONDS = 30
CLAIM_SECONDS = 5
# How often a heartbeat ack asks the desktop about one command still `unknown`.
RECONCILE_EVERY_S = 60
# The desktop's Journal::admit refuses a command whose deliverBy - createdAt exceeds this.
DISPATCH_WINDOW_SECONDS = 30
# True for a Mac of any age: an older one discards what it ran meanwhile, a current one uploads it on sharing again.
PAUSED_NOTICE = "Sharing was stopped here."
logger = logging.getLogger(__name__)
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:local-session-lifecycle")

#: Seconds per named step of the frame being handled, for the router's slow-frame line.
frame_steps: ContextVar[dict[str, float] | None] = ContextVar("local_session_frame_steps", default=None)


@contextmanager
def step(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        steps = frame_steps.get()
        if steps is not None:
            steps[name] = steps.get(name, 0.0) + time.monotonic() - started


#: The Lightbulb holder a session runs in (8 hex). Optional: every session on a paired Mac is
#: tracked (rulings 2026-10-04); a holder only marks one whose live terminal Lightbulb hosts.
HolderId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]


class Descriptor(ContractModel):
    workspace: sessions.WorkspaceSummary = Field(default_factory=sessions.WorkspaceSummary)
    provider: sessions.Provider
    profile_fingerprint: sessions.DeviceId
    native_conversation_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    provider_version: str = Field(min_length=1, max_length=128)
    status: sessions.INPUT_STATUSES
    active_turn_id: sessions.OpaqueId | None = None
    control_unavailable_reason: sessions.ControlUnavailableReason | None = None
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    holder_id: HolderId | None = None
    # The engine's id for this session, so a client can join the network list to this one.
    network_session_id: sessions.NetworkSessionId | None = None
    # A Claude session moved to the background (<-) continues as a fork with a new conversation:
    # this names the session it continues, so a listing shows one session, not two.
    forked_from: sessions.OpaqueId | None = None
    start_id: sessions.OpaqueId | None = None
    # False: its person stopped sharing it on the Mac, which still registers it so the owner keeps
    # it in their own list as "Only you" (2026-10-26). Absent leaves the row's sharing as it is.
    shared: bool | None = None
    # What Claude says beyond `status` (spec 2026-10-26 (e)): a background job's turn is over
    # (`done`), why a prompt is open in its terminal (`waitingFor`), its hand-off note (`note`,
    # Claude's `needs`: a message for its person, never a prompt). Sent only once FEATURES lists
    # sessionAttention; absent is none (an older desktop never sends them).
    done: bool | None = None
    waiting_for: Annotated[str, Field(max_length=120)] | None = None
    note: Annotated[str, Field(max_length=300)] | None = None
    # Files changed since the session started (git), its running subagents, its branch's pull request
    # (spec 2026-10-26 (f) gaps 7, 16, 15). Sent only once FEATURES lists sessionSignals; absent is
    # none or unknown (an older desktop never sends them).
    changes: sessions.SessionChanges | None = None
    agents: sessions.AgentCount | None = None
    pr: sessions.SessionPr | None = None


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
    # ponytail: accepted and ignored -- a Mac registered before this relay stopped listing
    # roomReplyRequestId keeps sending it until it re-registers; delete with that desktop.
    room_reply_request_id: bool | None = None
    # A newer desktop sends this only after the relay advertises support.
    capabilities: sessions.Capabilities | None = None
    # Which live terminal this Mac can show for the session: "held" (its holder), "attach" (a
    # Claude background session through `claude attach`), "none" (transcript only). Sent only
    # once FEATURES lists sessionTerminal. Null is silence.
    terminal: Literal["held", "attach", "none"] | None = None
    # Why `terminal` is "none" (spec 2026-10-07 §5.1). Sent only once FEATURES lists terminalReason.
    terminal_reason: sessions.TerminalReason | None = None
    # This session's holder takes the app panel's size (owner ruling 2026-10-14). Per session: a
    # holder an older build started keeps running after the Mac updates and ignores it. Sent only
    # once FEATURES lists remoteResize.
    terminal_resize: bool = False
    # What Claude says beyond `status` (spec 2026-10-26 (e)): a background job's turn is over
    # (`done`), why a prompt is open in its terminal (`waitingFor`), its hand-off note (`note`,
    # Claude's `needs`: a message for its person, never a prompt). Sent only once FEATURES lists
    # sessionAttention; absent is none (an older desktop never sends them).
    done: bool | None = None
    waiting_for: Annotated[str, Field(max_length=120)] | None = None
    note: Annotated[str, Field(max_length=300)] | None = None
    # Files changed since the session started (git), its running subagents, its branch's pull request
    # (spec 2026-10-26 (f) gaps 7, 16, 15). Sent only once FEATURES lists sessionSignals; absent is
    # none or unknown (an older desktop never sends them).
    changes: sessions.SessionChanges | None = None
    agents: sessions.AgentCount | None = None
    pr: sessions.SessionPr | None = None
    # The engine's id for this session, so a client can join the network list to this one.
    network_session_id: sessions.NetworkSessionId | None = None


#: What this relay reads beyond the frozen frames; the desktop sends a field only once listed.
FEATURES = [
    "sessionStats",
    "sessionSubagents",
    "nativeControlCapabilities",
    "liveTerminal",
    "remoteInput",
    "sessionTerminal",
    "sessionPresence",
    "pickupRequests",
    "bandOwnerOnly",
    "endedScreens",
    "terminalReason",
    "endedStartId",
    "agentMessageAsks",
    "endedOnPurpose",
    "approvalInput",
    # The Mac stops a held session left idle with nobody on it only once this is listed.
    "endedIdleStopped",
    # The Mac advertises remoteResize (its holders take the app panel's size) only once this is listed.
    "remoteResize",
    # The Mac sends `done`, `waitingFor` and `note` only once this is listed.
    "sessionAttention",
    # The Mac sends `changes`, `agents` and `pr` only once this is listed.
    "sessionSignals",
    # The Mac sends `networkSessionId` only once this is listed.
    "networkSessionId",
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
    # Ended held sessions whose holder still shows the final screen (spec §5.4). Sent only once
    # FEATURES lists endedScreens, in a frame of its own with no sessions.
    screens: Annotated[list[uuid.UUID], Field(max_length=24)] = []
    # What this Mac can do. Each is one boolean, sent only once FEATURES lists it, and lands
    # on every listed session's META as `deviceFeatures` (session_screen reads it).
    live_terminal: bool = False
    remote_input: bool = False
    # This Mac's holders take the app panel's size on `local.session.input` (owner ruling
    # 2026-10-14: the session takes the size of whoever used it last, tmux's `window-size latest`).
    remote_resize: bool = False
    session_presence: bool = False
    # This Mac's terminal names a pick-up request as one (spec 2026-10-05-ask-to-pick-up). An
    # older Mac would show it as a join request, so presence leaves pick-ups out for it.
    pickup_requests: bool = False
    # This Mac strips ctrl+x and mouse reports from remote input (only the owner operates the
    # band), so it never freezes remote input for a pending request; without it the web says "Paused".
    band_owner_only: bool = False
    # This Mac's band shows an agent's ask to message the session as one (spec
    # 2026-10-07-agent-session-approval), so presence lists those asks for it.
    agent_message_asks: bool = False

    def device_features(self) -> list[str]:
        return sorted(
            k for k, on in self.model_dump(by_alias=True, exclude={"type", "sessions", "screens"}).items() if on
        )


class Ended(ContractModel):
    type: Literal["local.session.ended"]
    session_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    # "on-purpose": the Mac saw its person end it (the provider's own exit, or End on its holder).
    # Sent only once FEATURES lists endedOnPurpose; an older desktop says "exited" for those too.
    reason: sessions.EndedReason
    ended_at: int = Field(ge=0)
    # The holder keeps the final screen (spec §5.4). Sent only once FEATURES lists endedScreens.
    screen: bool = False
    # The remote start this run was bound to while it was verified. A run that ended before the
    # relay heard its start identity (the Mac was offline) can only say so here: its draining
    # register is unverified and may not carry one. Sent only once FEATURES lists endedStartId.
    start_id: sessions.OpaqueId | None = None


Event = sessions.LocalSessionEventBody


class Events(ContractModel):
    type: Literal["local.session.events"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    batch_id: sessions.OpaqueId
    after_seq: int = Field(ge=0, lt=10**20 - 33)
    events: list[Event] = Field(min_length=1, max_length=32)


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
            **({"start_id": row["startId"]} if row.get("startId") else {}),
            **properties,
        },
        event_uuid=str(uuid.uuid5(_ANALYTICS_NS, f"{row['ownerId']}:{row['sessionId']}:{row['generation']}:{event}")),
    )


def _capture_offline_rejection(row: dict, command: dict, reason: Literal["expired", "rejected"]) -> None:
    if command.get("offlineIdentity"):
        capture_session_event(
            row,
            "local_session_command_settled",
            f"{command['requestId']}:rejected:{reason}",
            actor_user_id=command.get("actorUserId"),
            request_id=command["requestId"],
            command_kind="send",
            origin="direct",
            state="rejected",
            offline_queued=True,
            outcome_reason=reason,
        )


def _conflict(reason: str, **fields: object) -> ApiProblem:
    # Ids, seqs and reasons only — never frame content.
    logger.warning("local session conflict: %s", reason, extra={"reason": reason, **fields})
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed; reconcile before retrying.")


async def _actor_label(command: dict, owner: str | None = None) -> dict | None:
    """Handle and name of the person who sent ``command``; the desktop shows it in the owner's
    terminal. The room's member row when there is one; a person with no room (a follow-up sent
    from the session's own page stores roomId "") or no row in it still gets the name presence shows."""
    from core.services.messaging import human_emails, with_human_handles

    member_id = command["actorUserId"]
    member = None
    if room_id := command.get("roomId"):
        room_members = await store.list_members(room_id)
        members = with_human_handles(room_members, await human_emails(room_members))
        member = next((m for m in members if m.get("kind") == "human" and str(m.get("id")) == member_id), None)
    if member is None:
        from core.services.session_presence import _name  # imports this module's siblings

        name = await _name(member_id)
        # A failed lookup answers the bare user id: no label is better than an opaque id.
        return None if name == member_id else {"kind": "human", "handle": None, "displayName": name}
    return {
        "kind": "human",
        "handle": member.get("mentionHandle") or None,
        "displayName": member.get("displayName") or None,
    }


#: In-flight hint fan-outs, referenced so asyncio cannot garbage-collect them before they finish.
_hint_tasks: set[asyncio.Task[None]] = set()
#: One fan-out per (owner, human, transcript, session) at a time: a hint that arrives while one is running
#: marks it dirty and the running task publishes once more at the end, so a streaming burst
#: (one call per session per second) collapses to at most one in-flight + one follow-up per key.
_hint_running: dict[tuple[str, str, bool, str | None], bool] = {}


async def publish_resources_changed(
    owner: str, human: str, *, transcript: bool = False, session_id: str | None = None
) -> None:
    """One ``workspace.sessions.changed`` per change, on the workspace sessions channel.

    Clients refetch their open room's resources (and so its transcript) from it. It used to
    fan out a ``room.resources.changed`` to every room the human is in, one membership read and
    publish at a time: ~28 round trips on every relay frame for the founder, which crossed the
    desktop's 10 s ack deadline and looped reconnects (prod 2026-09-24). Owner ruling: one hint.
    ``transcript`` marks a streaming batch: open panes refetch, the sidebar list ignores it (it
    lists liveness, not content, and a streaming teammate would refetch it once a second).
    ``session_id`` names the one session that changed: clients re-read that row
    (GET /messaging/sessions/{id}, ~0.3 s) instead of the whole list (1.7-2.5 s, measured
    2026-10-27). Without it, the change may add or drop rows, and clients refetch the list.
    """
    from core.services.live_events import get_live_event_publisher

    key = (owner, human, transcript, session_id)
    if key in _hint_running:
        _hint_running[key] = True  # dirty: the running fan-out sends one more at the end
        return
    _hint_running[key] = False
    event = make_live_event(
        "workspace.sessions.changed",
        {
            "humanUserId": human,
            **({"transcript": True} if transcript else {}),
            **({"sessionId": session_id} if session_id else {}),
        },
    )

    async def fan_out() -> None:
        try:
            while True:
                try:
                    await get_live_event_publisher().publish_sessions(owner, event)
                except Exception:
                    # Hints are expendable; authorized HTTP polling repairs lost hints.
                    logger.warning("Workspace session hint failed")
                if not _hint_running.get(key):
                    break
                _hint_running[key] = False
        finally:
            _hint_running.pop(key, None)

    # Off the caller's frame: it was one publish per workspace member (51 members = 2.4-3.2 s of a
    # register frame on dev, 2026-10-08) and is one publish on the sessions channel now, but a slow
    # AppSync still must not hold the desktop's 10 s ack. The task is kept until done so the loop
    # never drops it mid-flight.
    task = asyncio.create_task(fan_out())
    _hint_tasks.add(task)
    task.add_done_callback(_hint_tasks.discard)


#: Heartbeat fields whose change counts as the user doing something (stamps lastEventAt).
#: What Claude says beyond `status`; each frame from a Mac that sends them says all of them.
ATTENTION_FIELDS = ("done", "waitingFor", "note")
#: What the session's Mac reads beyond its transcript; each frame from a Mac that sends them says all of them.
SIGNAL_FIELDS = ("changes", "agents", "pr")
_PUBLIC_FIELDS = (
    "status",
    "activeTurnId",
    "title",
    "cwdName",
    "lastAssistantMessage",
    "controlUnavailableReason",
    "networkSessionId",
    *ATTENTION_FIELDS,
    *SIGNAL_FIELDS,
)


#: A session "in a turn": working, or paused on an approval inside that turn.
_TURN_STATUSES = ("busy", "waiting-approval")
#: A turn shorter than this finishing is not worth a notification (the report's 60 s).
FINISHED_NOTIFY_MIN_S = 60
ACTIVITY_TTL_S = 90 * 24 * 3600  # the projector's message rows keep the same
_tasks: set[asyncio.Task] = set()


#: One "finished" notice per moment: a background subagent ending and the parent turn it
#: belongs to ending land 1-15 s apart (measured 2026-09-26), and are one thing to the owner.
FINISHED_COLLAPSE_S = 30


def _attention(row: dict, status: str, now: int, note: str | None = None) -> list[str]:
    """The owner-facing state changes a heartbeat carries: "waiting" or "finished". A Claude job's
    hand-off ``note`` notifies nobody (owner 2026-10-27: notes are not shown anywhere)."""
    return [state] if (state := _status_attention(row, status, now)) else []


def _status_attention(row: dict, status: str, now: int) -> Literal["waiting", "finished"] | None:
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


def attention_copy(row: dict, state: str, subagent: str | None = None, prompt_text: str = "") -> tuple[str, str]:
    """ "Claude · <session title>" over "Waiting for you: …" / "Finished: <reply's first line>".
    ``subagent`` is a finished background subagent's own line ('Agent "…" finished'), which is the body."""
    from core.services import notification_copy as copy

    tool = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "grok": "Grok"}.get(
        row.get("provider", ""), "Session"
    )
    # The clients' rule (sessionTitle): never an id.
    where = copy.line(row.get("displayTitle") or row.get("title") or row.get("cwdName"), 40) or "New chat"
    if subagent:
        return copy.title(tool, where), copy.line(subagent)
    if state == "waiting":
        return copy.title(tool, where), waiting_line(row.get("pendingApproval"), prompt_text)
    if state == "attention":
        return copy.title(tool, where), copy.line(row.get("note")) or "Left you a note"
    reply = copy.line(row.get("lastAssistantMessage"))
    return copy.title(tool, where), copy.line(f"Finished: {reply}") if reply else "Finished its turn"


def waiting_line(prompt: dict | None, text: str = "") -> str:
    """What the session waits on, named: the question, or the call it wants to make. Only an
    answerable prompt reaches the row; any other is named by its transcript row's ``text``
    ("Tea or coffee? (Tea / Coffee)", "Wants to run Bash(git push)") and answered in the terminal."""
    from core.services.notification_copy import line

    if not prompt:
        text = line(text, 60)
        if text.startswith("Wants to run"):
            return f"{text}: answer it in your terminal"
        if text:
            return line(f"Has a question: {text}")
        return "Waiting for you: approve or answer it in your terminal"
    tool, summary = prompt.get("tool") or "A tool", line(prompt.get("summary"), 60)
    if tool == "AskUserQuestion":
        return line(f"Has a question: {summary}") if summary else "Has a question for you"
    ask = f'{tool} wants to run "{summary}"' if summary else f"{tool} wants your OK"
    return ask if prompt.get("exact") or prompt.get("full") else f"{ask}: answer it in your terminal"


async def _waiting_text(row: dict) -> str:
    """The text of the prompt this wait is about: the session's LAST stored row, when that row is a
    prompt stored around the moment the wait began. Anything after a prompt (its tool result, a
    reply, a new turn) means it was answered, so an older prompt is never named for a new wait.
    Empty when the new prompt's batch has not landed yet (the Mac sends the status and the prompt
    row separately) or the read fails -- the generic line covers it."""
    try:
        events, _ = await repo.list_events(row["ownerId"], row["sessionId"], max(0, row.get("eventSeq", 0) - 10), 10)
    except Exception:
        logger.warning("Waiting prompt read failed", exc_info=True)
        return ""
    # A held prompt's release re-states it with no text: still the same prompt.
    rows = [e for e in events if not (e.get("lifecycle") == "approval_pending" and not e.get("text"))]
    last = rows[-1] if rows else {}
    if last.get("lifecycle") == "approval_pending" and last.get("createdAt", 0) >= row.get("waitingSince", 0) - 30:
        return last["text"]
    return ""


async def notify_attention(row: dict, state: str, subagent: Event | None = None) -> None:
    """Tell the session's OWNER (sessions have no watchers): an Activity row, a live
    event for desktop/web notifications, and a phone push. Best effort, each on its own.
    ``subagent`` is a background subagent's end event; it notifies as its parent would."""
    from core.repositories import user_repo
    from core.services.live_events import get_live_event_publisher
    from core.services.push_service import send_push_to_user

    owner, human, sid = row["ownerId"], row["humanUserId"], row["sessionId"]
    try:
        prefs = user_repo.session_notifications(await user_repo.get(human))
    except Exception:
        # Fail closed: an unread opt-out must not be overridden, so this notice is badge-only.
        logger.warning("Session notification prefs read failed; no banner", exc_info=True)
        prefs = {**user_repo.session_notifications(None), "waiting": "badge", "finished": "badge", "attention": "badge"}
    # "off" and "badge" never banner; a note is no Activity row (the row's own text is the record).
    level = prefs[state]
    if state == "attention" and level != "banner":
        return
    line = (subagent.text.strip() or "A subagent finished.")[:180] if subagent else None
    prompt_text = await _waiting_text(row) if state == "waiting" and not row.get("pendingApproval") else ""
    title, body = attention_copy(row, state, line, prompt_text)
    created = utc_now_iso()
    # A subagent ends once; its own id keeps a replayed batch to one notification.
    # A note's notice shares its frame's revision with a "finished": its own key keeps both.
    key = f"subagent|{subagent.subagent}" if subagent else row["revision"]
    key = f"attention|{key}" if state == "attention" else key
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-attention|{sid}|{key}"))
    try:
        if state != "attention":
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
    # The clients banner only at "banner", with these two prefs; "badge" is the Dock count alone.
    notice = {"level": level, "sound": prefs["sound"], "onlyUnfocused": prefs["onlyUnfocused"]}
    try:
        await get_live_event_publisher().publish_user(
            owner,
            human,
            make_live_event(
                "workspace.session.attention",
                {"sessionId": sid, "state": state, "push": push, **notice},
                event_id=event_id,
            ),
        )
    except Exception:
        logger.warning("Session attention live event failed")
    if level == "banner":
        await send_push_to_user(human, title, body, push["data"], sound=prefs["sound"])


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
        # A question is never a permission prompt, even if an old or malformed descriptor
        # happens to contain approval-shaped metadata. Old clients must never offer Allow.
        if tool.name == "AskUserQuestion" or isinstance(tool.input, dict) and tool.input.get("question") is True:
            continue
        try:
            found = sessions.PendingApproval.model_validate(
                {**(tool.input if isinstance(tool.input, dict) else {}), "approvalId": event.item_id, "tool": tool.name}
            ).model_dump(by_alias=True)
        except ValidationError:
            found = None
    return found


def pending_question(events: list[Event]) -> object:
    """Project only the last open prompt, and only a complete immutable question payload."""
    found = _NO_PROMPT
    for event in events:
        if event.lifecycle != "approval_pending":
            continue
        found = None
        tool = event.tool
        if tool is None or tool.name != "AskUserQuestion" or not isinstance(tool.input, dict):
            continue
        tag = tool.input
        if tag.get("question") is not True or tag.get("full") is not True or tag.get("promptId") != event.item_id:
            continue
        source = tag.get("input")
        if (
            not isinstance(source, dict)
            or len(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 10 * 1024
        ):
            continue
        try:
            found = sessions.PendingQuestion.model_validate(
                {
                    "promptId": tag.get("promptId"),
                    "inputDigest": tag.get("inputDigest"),
                    "questions": source.get("questions"),
                }
            ).model_dump(by_alias=True)
        except ValidationError:
            found = None
    return found


class LocalSessionRelay:
    def __init__(self, connections: ConnectionService, management: ManagementApiClient):
        self.connections, self.management = connections, management

    async def connect(self, connection_id: str) -> tuple[dict, dict]:
        row = await store.run_in_thread(self.connections.get_connection, connection_id)
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
        with step("connect"):
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
                            # An ended run has nothing left to fence. A resume is the next run even
                            # when its Mac forgot the old generation (mirrors are reaped a day after
                            # the end).
                            and previous.get("endedAt") is None
                        ):
                            raise _conflict("generation chain", sid=sid, cid=cid)
                        if (
                            previous["generation"] == descriptor.generation
                            and previous.get("startId")
                            and descriptor.start_id
                            and previous["startId"] != descriptor.start_id
                        ):
                            raise _conflict("start identity changed", sid=sid, cid=cid)
                    if descriptor.start_id and descriptor.control_unavailable_reason is not None:
                        raise _conflict("unverified start identity", sid=sid, cid=cid)
                    # The desktop is the only party that can verify the native runtime (live process,
                    # transcript rows, inbox socket) and it fails closed with a controlUnavailableReason.
                    # A backend version list could only drift from it, and did (Claude 2.1.269).
                    compatible = descriptor.control_unavailable_reason is None
                    row = _revised(
                        previous or {},
                        **descriptor.model_dump(by_alias=True, exclude={"shared", "network_session_id"}),
                        networkSessionId=descriptor.network_session_id or (previous or {}).get("networkSessionId"),
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
                        lastEventAt=sessions.reconnect_last_event_at(
                            previous, descriptor.generation, descriptor.status, descriptor.active_turn_id
                        )
                        or int(time.time()),
                    )
                    # The run before had no terminal (it ran inside an app): this one, which may
                    # have one, does not inherit its "Anyone with the link".
                    if previous and sessions.conversation_only(previous):
                        sessions.narrow_link(row)
                    # A row's life is its conversation's, not its process's. A new process the Mac
                    # moves this live row onto (Claude's daemon respawning a worker, the agent view's
                    # restart, a `--resume` in another pid) names the generation we hold: the same
                    # run goes on, with its title and its place in the lists. What the owner granted
                    # a process (teammate control, the people let in) is granted again (below): a new
                    # process is one they have not opted into. A run that ended first is a new one (a
                    # resume), and so is a generation that names none.
                    continuation = bool(
                        previous
                        and previous["generation"] != descriptor.generation
                        and message.previous_generation == previous["generation"]
                        and previous.get("endedAt") is None
                    )
                    new_run = previous is None or (previous["generation"] != descriptor.generation and not continuation)
                    # Paused on the Mac: what `set_sharing(False)` writes, so it reads as "Only you".
                    # Only for a new process: the same one reconnecting may carry a pause its owner has
                    # since lifted from the web while the Mac was offline, and the Mac takes that
                    # from presence. Its own pauses reach us as `set_sharing` (the band, a stop owed).
                    new_process = previous is None or previous["generation"] != descriptor.generation
                    if descriptor.shared is False and new_process and not row.get("unshared"):
                        row["unshared"] = True
                        sessions.narrow_link(row)
                        row.pop("pendingApproval", None)
                        row.pop("pendingQuestion", None)
                        row.pop("privateSendApproval", None)
                    # Absent, never a stored null.
                    for key in ("holderId", "forkedFrom", "networkSessionId", *ATTENTION_FIELDS, *SIGNAL_FIELDS):
                        if not row.get(key):
                            row.pop(key, None)
                    # The hidden `background` kind is gone (Phase 3); a row that still has it is listed now.
                    row.pop("kind", None)
                    if descriptor.start_id is None and previous and previous["generation"] == descriptor.generation:
                        row["startId"] = previous.get("startId")
                    elif descriptor.start_id is None:
                        row.pop("startId", None)
                    # A reconnect may find no prompt in the bounded desktop scan.
                    # Keep the same run's title (also across a continuation); a new run starts fresh.
                    if previous and not new_run and descriptor.title is None:
                        row["title"] = previous.get("title")
                    if descriptor.status in _TURN_STATUSES and (previous or {}).get("status") not in _TURN_STATUSES:
                        row["busySince"] = int(time.time())
                    # A resumed conversation re-registers under the same id: registering un-ends it.
                    row.pop("endedAt", None)
                    row.pop("endedReason", None)
                    row.pop("endAskedAt", None)
                    # A prompt belongs to the generation that asked it; a new one re-reports.
                    # So does teammate control, and so do the people the owner let in: a restarted
                    # process is a new run the owner has not opted into, even when no Ended frame
                    # arrived for the old one. A relay reconnect keeps the generation, and all three.
                    # A new connection may be an older desktop: it re-advertises on its first heartbeat.
                    if (previous or {}).get("connectionId") != cid:
                        row.pop("deviceFeatures", None)
                    if previous is None or previous["generation"] != descriptor.generation:
                        # The end rewrites who may resume; a new run has nobody to resume it. The
                        # same generation re-registering to drain an ended mirror keeps them.
                        row.pop("resumeAllowed", None)
                        # A terminal, a held prompt and control are the old process's, continued or not.
                        row.pop("terminal", None)
                        row.pop("pendingApproval", None)
                        row.pop("pendingQuestion", None)
                        row.pop("privateSendApproval", None)
                        row.pop("teamControl", None)
                        # The people whose open ask this restart drops are told so on the new run.
                        row.pop("joinClosed", None)
                        if closed := sessions.closed_joins(previous or {}):
                            row["joinClosed"] = closed
                        for key in sessions.JOIN_FIELDS:
                            row.pop(key, None)
                    if new_run:
                        # Nor is it still removed from the lists (`sessions.remove_session`): the
                        # owner started it again.
                        row.pop("removedAt", None)
                    # A fork continues its origin's conversation: the name its person gave it there
                    # carries over, since the listing shows the fork in the origin's place.
                    # Also on a late re-register: a fork that registered before its origin had an id
                    # registers again with `forkedFrom` once the origin is known.
                    if descriptor.forked_from and not row.get("displayTitle"):
                        origin = await repo.get_session(owner, descriptor.forked_from)
                        if origin and origin.get("displayTitle") and origin["humanUserId"] == human:
                            row["displayTitle"] = origin["displayTitle"]
                    # After the join fields reset above: a resume's person is let into the new run.
                    extra = [self._connection_check(cid, connection), *await session_resume.on_register(previous, row)]
                    # pickupAllowed/pickupSnapshot ride along from `previous`: content sharing, not control.
                    if previous is None:
                        picked_up_from = await session_pickup.lineage(owner, sid)
                        if picked_up_from:
                            row["pickedUpFrom"] = picked_up_from
                    await repo.put_session(row, previous, enrollment, extra=extra)
                    if previous and previous.get("endedAt") is not None:
                        from core.services import session_chat  # lazy: it imports local_sessions

                        try:  # a resume: its chat takes posts again, also for people who have it open
                            await session_chat.follow_session(owner, sid)
                        except Exception:  # the next chat open reopens it too (_follow_lifecycle)
                            logger.warning("session chat reopen failed for %s", sid, exc_info=True)
                    if new_run:
                        # The desktop may discover an already-running native process: shared, not started.
                        _capture_lifecycle(row, "local_session_shared")
                    elif previous.get("connectionId") != row["connectionId"]:
                        capture_session_event(row, "local_session_reconnected", cid)
                    # The workspace session list enumerates humans via their
                    # private user stream, which a desktop-only sharer never
                    # creates by opening chat. First register carries it so the
                    # sharer is enumerable (and reachable by workspace hints).
                    # Best-effort: registration itself already succeeded.
                    try:
                        await store.ensure_user_stream(owner, human)
                    except Exception:
                        logger.warning("Local session register left without a user stream for %s", human)
                    with step("dispatch"):
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
                    with step("reconcile"):
                        response["reconcile"] = await self._reconcile_for(owner, sid, row["generation"])
                    response["serverTime"] = int(time.time())
                    response["features"] = FEATURES
                    with step("watching"):
                        watch = await session_screen.watch_on_register(row)
                    if watch:
                        response["watch"] = watch
                elif isinstance(message, DeviceHeartbeat):
                    # Resume first, then pick-up: each trims its own list to what is left of the
                    # envelope. Watching last: an empty list read there stops every relay.
                    # First, so this frame's own ack already lists the screens it keeps.
                    with step("screens"):
                        await session_screen.keep_screens(owner, human, device, cid, [str(s) for s in message.screens])
                    with step("renew"):
                        response = await self._device_heartbeat(cid, connection, enrollment, message)
                    with step("resume"):
                        response = await session_resume.ack_with_resume(response, owner, human, device)
                    with step("pickup"):
                        response = await session_pickup.ack_with_pickup(response, owner, human, device)
                    with step("watching"):
                        response = await session_screen.ack_with_watching(response, owner, human, device)
                    if message.session_presence:
                        with step("presence"):
                            response = await session_presence.ack_with_presence(
                                response, owner, device, [str(s.session_id) for s in message.sessions]
                            )
                    notify_resources = False
                elif isinstance(message, Ended):
                    with step("ended"):
                        response = await self._ended(cid, connection, enrollment, message)
                    with step("pickup"):
                        response = await session_pickup.ack_with_pickup(response, owner, human, device)
                    notify_resources = False
                else:
                    sid = str(message.session_id)
                    # A current connection may reconcile its journal from an older
                    # execution; events/heartbeats must match the current generation.
                    with step("session"):
                        row = await self._current(
                            cid, connection, sid, None if isinstance(message, Receipt) else message.generation
                        )
                    if isinstance(message, Events):
                        with step("events"):  # its own workspace hint included
                            response = await self._events(cid, connection, enrollment, row, message)
                        # _events throttles and publishes its own hint; the
                        # generic post-loop publish below must not double it.
                        notify_resources = False
                    else:
                        with step("receipt"):
                            response = await self._receipt(cid, connection, enrollment, row, message)
                        with step("dispatch"):
                            await self.dispatch_session(owner, sid)
                if notify_resources:
                    with step("hint"):
                        await publish_resources_changed(owner, human)
                check_envelope(response)
                return response
            except ClientError as exc:
                if not store._is_cancelled(exc):
                    raise
                lost = exc
                connection, enrollment = await self.connect(cid)
        if isinstance(message, DeviceHeartbeat):
            # The relay lost its own races; a heartbeat has nothing to reconcile. A conflict here
            # names no session, so the Mac cannot quarantine one and ends its whole connection.
            # Raised as it is, the frame is answered INTERNAL and the Mac sends the next heartbeat.
            raise lost
        raise _conflict("revision contention", cid=cid)

    async def _reconcile_for(self, owner: str, sid: str, generation: str, *, spaced: bool = False) -> dict | None:
        """The one command whose outcome the desktop journal must still settle.

        `spaced` (a heartbeat): an `unknown` command is asked about at most once per
        RECONCILE_EVERY_S. Each ask makes the Mac verify the native process, which takes
        seconds, and one that never settles was asked on every heartbeat for a day."""
        now = int(time.time())
        unresolved_commands = [
            c
            for c in await repo.list_commands(owner, sid)
            if c["state"] in {"dispatched", "unknown"}
            and not c.get("settled")
            and not (spaced and c["state"] == "unknown" and now - int(c.get("reconcileAskedAt", 0)) < RECONCILE_EVERY_S)
        ]
        # Prefer the current generation's blocking receipt. Retain older
        # unknowns and still reconcile them when none is current.
        unresolved = next(
            (c for c in unresolved_commands if c["expectedGeneration"] == generation),
            next(iter(unresolved_commands), None),
        )
        if unresolved is None:
            return None
        if spaced and unresolved["state"] == "unknown":
            # Conditional: two frames of one device racing ask once between them.
            if not await repo.mark_reconcile_asked(unresolved, now, RECONCILE_EVERY_S):
                return None
        return {
            "requestId": unresolved["requestId"],
            "generation": unresolved["expectedGeneration"],
            "state": unresolved["state"],
            **({"expectedTurnId": unresolved["expectedTurnId"]} if unresolved.get("expectedTurnId") else {}),
        }

    async def _device_heartbeat(self, cid: str, connection: dict, enrollment: dict, message: DeviceHeartbeat) -> dict:
        """One frame per device renews every mirror it still owns; the rest come back rejected."""
        # Each session's lease and delivery stay ordered; independent sessions overlap below.
        owner, now, started = connection.get("org_id") or connection["user_id"], int(time.time()), time.monotonic()
        rejected, reconcile = [], []
        # One workspace hint per frame, not per session: every session here is the same human's.
        # It still fires when the frame fails part-way, because the retry finds those rows already
        # matching and would not hint again.
        hint = False
        # Sessions are independent, but one frame still waits for all its writes before ACK.
        # Keep duplicate entries and revision retries serial within each session. Four groups
        # overlap their reads and delivery without opening an unbounded DynamoDB request burst.
        groups: dict[str, list[HeartbeatSession]] = {}
        for item in message.sessions:
            groups.setdefault(item.session_id, []).append(item)
        slots = asyncio.Semaphore(4)
        # The lease writes go one at a time. Every one is a transaction that condition-checks the
        # same three rows (pairing, enrollment, this connection), and DynamoDB cancels a
        # transaction that touches an item another one in flight holds, a ConditionCheck included.
        # Four at once cancelled each other: a third of prod frames reran, and about one in a hundred lost
        # six times running and cost the Mac its connection (2026-10-08).
        writes = asyncio.Lock()
        # One strong read for the whole frame. Only a session's first pass may take its row from
        # here: a duplicate entry or a revision retry reads again, and the fenced write below
        # catches a row that moved since. A batched row is never grounds to reject: a session
        # registered while its group waited for a slot reads as missing here.
        first: dict[str, dict | None] = {}

        def foreign(row: dict | None, item: HeartbeatSession) -> bool:
            return (
                row is None
                or row["deviceId"] != connection["device_id"]
                or row["generation"] != item.generation
                or row.get("connectionId") != cid
                or row.get("endedAt") is not None
            )

        async def renew(items: list[HeartbeatSession]) -> tuple[list[str], list[dict]]:
            nonlocal hint
            rejected, reconcile = [], []
            retries: dict[str, int] = {}
            async with slots:
                for item in items:
                    row = first.pop(item.session_id) if item.session_id in first else None
                    if foreign(row, item):
                        row = await repo.get_session(owner, item.session_id)
                    if foreign(row, item):
                        # Unknown, superseded or ended here: the desktop re-registers it.
                        rejected.append(item.session_id)
                        continue
                    labels = {
                        key: value
                        for key, value in (
                            ("title", item.title),
                            ("cwdName", item.cwd_name),
                            ("lastAssistantMessage", item.last_assistant_message),
                            ("networkSessionId", item.network_session_id),
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
                        # Rides the write every heartbeat makes anyway: no extra write, and /viewing
                        # already holds this row.
                        deviceFeatures=message.device_features(),
                        **labels,
                    )
                    updated["terminalResize"] = item.terminal_resize
                    signals = (
                        item.changes and item.changes.model_dump(by_alias=True),
                        item.agents,
                        item.pr and item.pr.model_dump(by_alias=True),
                    )
                    for key, value in zip(
                        (*ATTENTION_FIELDS, *SIGNAL_FIELDS),
                        (item.done, item.waiting_for, item.note, *signals),
                        strict=True,
                    ):
                        if value:
                            updated[key] = value
                        else:
                            updated.pop(key, None)
                    if item.terminal is not None:
                        updated["terminal"] = item.terminal
                        if item.terminal == "none" and item.terminal_reason is not None:
                            updated["terminalReason"] = item.terminal_reason
                        else:
                            updated.pop("terminalReason", None)
                        if sessions.conversation_only(updated):
                            sessions.narrow_link(updated)
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
                    attention = _attention(row, item.status, now, item.note)
                    if "waiting" in attention:
                        updated["waitingSince"] = now  # which wait an Activity item is about
                    if "finished" in attention:
                        updated["finishedNotifiedAt"] = now
                    if item.status not in _TURN_STATUSES:
                        updated.pop("subagentEnded", None)
                    if item.stats is not None:
                        updated["stats"] = item.stats.model_dump(by_alias=True, exclude_none=True)
                    # The prompt is answered (here or in the terminal) once the session stops waiting.
                    if item.status != "waiting-approval":
                        updated.pop("pendingApproval", None)
                        updated.pop("pendingQuestion", None)
                        updated.pop("privateSendApproval", None)
                    moved = any(row.get(field) != updated.get(field) for field in _PUBLIC_FIELDS) or any(
                        (row.get("stats") or {}).get(key) != (updated.get("stats") or {}).get(key)
                        for key in ("model", "permissionMode")
                    )
                    # Activity is a turn starting or ending, not a control, title or scan change.
                    if sessions.session_activity(
                        row["status"], updated["status"], row.get("activeTurnId"), updated.get("activeTurnId")
                    ):
                        updated["lastEventAt"] = now
                    scanned = row.get("workspace") != updated.get("workspace")
                    controls_changed = row.get("capabilities") != updated.get("capabilities")
                    terminal_changed = (
                        row.get("terminal"),
                        row.get("terminalReason"),
                        bool(row.get("terminalResize")),
                    ) != (
                        updated.get("terminal"),
                        updated.get("terminalReason"),
                        bool(updated.get("terminalResize")),
                    )
                    try:
                        async with writes:
                            await repo.put_session(
                                updated, row, enrollment, extra=[self._connection_check(cid, connection)]
                            )
                    except ClientError as exc:
                        if not store._is_cancelled(exc) or retries.get(item.session_id, 0) >= 2:
                            raise
                        retries[item.session_id] = retries.get(item.session_id, 0) + 1
                        items.append(item)
                        continue
                    if attention:
                        # The write above is revision-fenced, so exactly one frame wins a transition:
                        # one state change is one notification, retries included.
                        for state in attention:
                            _spawn(notify_attention(updated, state))
                    # A workspace scan is worth a refetch but is not the user doing something,
                    # so it hints without stamping lastEventAt. A lapsed lease reads as
                    # offline, so recovery must reach the room even when nothing else moved;
                    # renewal makes that fire once.
                    if moved or scanned or controls_changed or terminal_changed or row["onlineUntil"] <= now:
                        hint = True
                    # Reconcile after delivery: this heartbeat's own dispatch is part of what the journal must settle.
                    await self.dispatch_session(owner, item.session_id)
                    pending = await self._reconcile_for(owner, item.session_id, row["generation"], spaced=True)
                    if pending is not None:
                        reconcile.append({"sessionId": item.session_id, **pending})
            return rejected, reconcile

        try:
            # Drain every group even on failure: no lease write may outlive this frame's
            # hint/ACK/error. Preserve the original exception for handle_message's retries.
            first.update(zip(groups, await repo.get_sessions(owner, list(groups))))
            outcomes = await asyncio.gather(*(renew(items) for items in groups.values()), return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
                denied, pending = outcome
                rejected.extend(denied)
                reconcile.extend(pending)
            return {
                "type": "local.device.heartbeat.ack",
                "rejected": rejected,
                "reconcile": reconcile,
                "serverTime": now,
                # A Mac whose sessions have all ended registers nothing after a reconnect: this
                # is where it learns what the relay reads (its kept `screens`). Older desktops
                # read the ack by key and ignore it.
                "features": FEATURES,
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
            return ack
        # A device clock ahead of the server cannot park a session in the future,
        # where it would outrank every live row in a lastEventAt ordering.
        ended_at = min(message.ended_at, int(time.time()))
        # On purpose (owner ruling 2026-10-08): the Mac said so, or the owner's End for this run
        # reached that Mac. Facts only; "exited" alone also covers a closed terminal and a shutdown.
        ends = await sessions.landed_ends(owner, row)
        reason = sessions.ENDED_ON_PURPOSE if ends else message.reason
        updated = _revised(row, status="ended", endedAt=ended_at, lastEventAt=ended_at, endedReason=reason)
        # "End and remove" (`sessions.remove_session` on a live run): this end is its removal.
        # Only if an End it sent reached the Mac and was not refused; else the session stays listed.
        removing = any(command.get("removesOnEnd") for command in ends)
        if removing:
            updated["removedAt"] = int(time.time())
            updated.pop("resume", None)
        if message.screen and sessions.session_terminal(row) == "held" and not removing:
            # The holder keeps the final screen; the Mac's `screens` heartbeats renew this lease.
            updated["screenUntil"] = int(time.time()) + LIVENESS_SECONDS
            updated["screenConnectionId"] = cid
        else:
            updated.pop("screenUntil", None)
        updated.pop("pendingApproval", None)
        updated.pop("pendingQuestion", None)
        updated.pop("privateSendApproval", None)
        # An ended row grants no control, so this opens the transcript for whoever started it and
        # nothing else. A start identity the row already holds is never replaced.
        if message.start_id and not row.get("startId"):
            updated["startId"] = message.start_id
        extra = [self._connection_check(cid, connection)]
        # "Let teammates control this session" turns itself off with the session.
        if updated.pop("teamControl", False):
            audit = sessions.audit_row(owner, row["sessionId"], None, "team_control_ended")
            extra.append({"Put": {"TableName": store._table().name, "Item": audit}})
        # Who may resume it (spec §5.7): the people who could send when it ended. Team control is a
        # switch on the run, not a person the owner let in, so it lets nobody resume.
        resumers = [] if removing else sessions.allowed_senders(row)
        if resumers:
            updated["resumeAllowed"] = resumers
        # So do the people the owner let in, and every open ask to join.
        updated.pop("joinClosed", None)
        if closed := sessions.closed_joins(row):
            updated["joinClosed"] = closed  # carried to the run that resumes it
        for key in sessions.JOIN_FIELDS:
            updated.pop(key, None)
        await repo.put_session(updated, row, enrollment, extra=extra)
        _capture_lifecycle(updated, "local_session_ended", end_reason=reason)
        from core.services import session_chat  # lazy: it imports local_sessions

        try:
            if removing:
                # The link, the pick-up snapshot and the chat. The doors refuse a removed row on
                # their own, so a failure here leaves only stored leftovers nobody can reach.
                await sessions.finish_removal(sessions.owner_auth(row), row["sessionId"], hint=False)
            else:
                await session_chat.follow_session(owner, row["sessionId"])
        except Exception:  # the next chat open archives it too (session_chat._follow_lifecycle)
            logger.warning("session chat archive failed for %s", row["sessionId"], exc_info=True)
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        return ack

    async def _events(self, cid: str, connection: dict, enrollment: dict, row: dict, message: Events) -> dict:
        sid = row["sessionId"]
        first_seq = message.after_seq + 1
        previous = await store._get(row["pk"], f"EVENT#{first_seq:020d}")
        digest = store.request_hash(message.model_dump(mode="json", by_alias=True))
        if previous:
            if previous.get("batchHash") != digest:
                raise _conflict("batch hash", sid=sid, first_seq=first_seq)
            return {
                "type": "local.session.events.ack",
                "sessionId": row["sessionId"],
                "batchId": message.batch_id,
                "afterSeq": previous["batchEndSeq"],
                # The replay of a batch dropped while paused: its row is the notice.
                **({"paused": True} if previous.get("text") == PAUSED_NOTICE else {}),
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
        question = pending_question(message.events)
        approval_changed = approval is not _NO_PROMPT and row.get("pendingApproval") != approval
        question_changed = question is not _NO_PROMPT and row.get("pendingQuestion") != question
        if (
            approval_changed
            or question_changed
            or (approval is not None and approval is not _NO_PROMPT)
            or (question is not None and question is not _NO_PROMPT)
        ):
            # Prompt changes, including release, must refresh the session list immediately.
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
            updated.pop("pendingQuestion", None)
            updated.pop("privateSendApproval", None)
            if approval is not None:
                updated["pendingApproval"] = approval
            if question is not None and question is not _NO_PROMPT and row["provider"] == "claude":
                updated["pendingQuestion"] = question
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection), *updates])
        # The write is cursor-fenced and a replayed batch returned above, so each end notifies once.
        if notify_ended is not None:
            _spawn(notify_attention(updated, "finished", notify_ended))
        if hint:
            await publish_resources_changed(
                row["ownerId"],
                row["humanUserId"],
                transcript=not (approval_changed or question_changed),
                session_id=row["sessionId"],
            )
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
        resyncs like any other cursor gap. The ack says ``paused``: the Mac follows the
        cursor but keeps the events, and uploads them when sharing resumes (owner ruling
        2026-10-08: sharing again shares the whole session). An older desktop ignores the
        field and discards them.
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
            await publish_resources_changed(
                row["ownerId"], row["humanUserId"], transcript=True, session_id=row["sessionId"]
            )
        return {
            "type": "local.session.events.ack",
            "sessionId": row["sessionId"],
            "batchId": message.batch_id,
            "afterSeq": end,
            "paused": True,
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
        if command["state"] in {"applied", "rejected"}:
            if any(command.get(key) != value for key, value in outcome.items()):
                raise _conflict("receipt outcome", sid=sid)
        else:
            await repo.replace_command(
                command,
                _revised(command, **outcome, updatedAt=int(time.time())),
                checks=[
                    repo.pairing_condition(row["ownerId"], row["humanUserId"], row["deviceId"], None),
                    repo.enrollment_condition(enrollment),
                    self._session_check(row),
                    self._connection_check(cid, connection),
                ],
            )
            capture_session_event(
                row,
                "local_session_command_settled",
                f"{message.request_id}:{message.outcome}",
                actor_user_id=command.get("actorUserId") or row["humanUserId"],
                request_id=message.request_id,
                command_kind=command["kind"],
                origin="direct",
                state=message.outcome,
            )
        return {
            "type": "local.session.receipt.ack",
            "sessionId": row["sessionId"],
            "requestId": message.request_id,
            "state": message.outcome,
        }

    async def _dispatch_checks(self, row: dict, command: dict, claim: bool) -> list[dict] | None:
        owner, sid = row["ownerId"], row["sessionId"]
        auth = sessions.command_caller(owner, command)
        current, enrollment = await sessions.authorize_session(auth, command["roomId"], sid, owner_only=True)
        sender = command["actorUserId"]
        if command.get("offlineIdentity") and sender != current["humanUserId"]:
            stream = await store.get_user_stream(owner, sender)
            if not stream or not stream.get("active"):
                raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Sender is no longer in this workspace.")
        sessions.require_command_scope(auth, current, command["kind"])
        sessions._require_control(
            current,
            sessions._public(
                sessions.LocalSessionCommand, {**command, "shownDigest": command.get("approvalShownDigest")}
            ),
        )
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
                expired = False
                for command in commands:
                    if command["state"] in {"pending", "awaiting_approval"} and command["deliverBy"] <= int(
                        time.time()
                    ):
                        await repo.replace_command(
                            command, _revised(command, state="rejected", updatedAt=int(time.time()))
                        )
                        _capture_offline_rejection(row, command, "expired")
                        expired = True
                if expired:  # read again only what this pass changed: every heartbeat comes through here
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
                        _capture_offline_rejection(row, command, "rejected")
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
                if not row.get("connectionId") or not await store.run_in_thread(
                    self.connections.get_connection, row["connectionId"]
                ):
                    return
                try:
                    checks = await self._dispatch_checks(row, command, True)
                    if checks is None:
                        continue
                except ApiProblem:
                    await repo.replace_command(command, _revised(command, state="rejected", updatedAt=int(time.time())))
                    _capture_offline_rejection(row, command, "rejected")
                    continue
                # Resolved before the claim: a membership read inside the claim window
                # can spend the lease and turn a dispatchable command into a rejection.
                actor = await _actor_label(command, owner)
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
                    # A command that may wait (a queued follow-up: 24 h) leaves with a fresh window,
                    # because the Mac admits only 30 s. Its stored deliverBy still bounds expiry;
                    # queuedAt (server-side only) keeps when it was queued.
                    window = {}
                    if claim["deliverBy"] - claim["createdAt"] > DISPATCH_WINDOW_SECONDS:
                        sent_at = int(time.time())
                        window = {
                            "queuedAt": claim.get("queuedAt", claim["createdAt"]),
                            "createdAt": sent_at,
                            "deliverBy": min(claim["deliverBy"], sent_at + DISPATCH_WINDOW_SECONDS),
                        }
                    # A released or offline-queued session send outlived its source turn's 30 s
                    # invocation window (``_dispatch_checks`` skips it); the Mac still checks it, so
                    # it leaves with the dispatch window's own expiry.
                    if (claim.get("actor") or {}).get("kind") == "session" and (
                        claim.get("releasedAt") is not None or claim.get("offlineIdentity")
                    ):
                        window["invocationExpiresAt"] = window.get("deliverBy", claim["deliverBy"])
                    public = sessions._public(sessions.CommandReceipt, {**claim, **window}).model_dump(
                        mode="json", by_alias=True
                    )
                    public["state"] = "dispatched"
                    envelope = {"type": "local.session.command", "command": public, "actor": actor}
                    check_envelope(envelope)
                except ApiProblem:
                    await repo.replace_command(claim, _revised(claim, state="rejected", updatedAt=int(time.time())))
                    _capture_offline_rejection(row, claim, "rejected")
                    continue
                dispatched = _revised(
                    claim, **window, state="dispatched", connectionId=row["connectionId"], updatedAt=int(time.time())
                )
                await repo.replace_command(claim, dispatched, checks=checks)
                if int(time.time()) >= claim["claimUntil"]:
                    # A slow DB call consumed the lease. No network attempt occurred.
                    await repo.replace_command(
                        dispatched, _revised(dispatched, state="rejected", updatedAt=int(time.time()))
                    )
                    _capture_offline_rejection(row, dispatched, "rejected")
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
                elif not window and "createdAtMs" in claim:
                    # Online sends only: a queued follow-up or offline send (window) waits on purpose.
                    put_metric(
                        "session.command.dispatch.latency", time.time() * 1000 - claim["createdAtMs"], "Milliseconds"
                    )
                return
            except ClientError as exc:
                if not store._is_cancelled(exc):
                    raise
        # Another worker owns progression. Heartbeat/POST/receipt will drive it again.


@lru_cache(maxsize=1)
def get_local_session_relay() -> LocalSessionRelay:
    return LocalSessionRelay(ConnectionService(), ManagementApiClient())
