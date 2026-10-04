"""Room-authorized local harness directory and durable command admission.

No native transport lives here. register_session is for a future verified machine
connection; its identity arguments must come from that connection, never its payload.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from botocore.exceptions import ClientError
from pydantic import Field, field_validator, model_serializer, model_validator

from core.auth import AuthContext, resolve_owner_id
from core.dynamodb import utc_now_iso
from core.observability.product_analytics import capture_product_event
from core.observability.session_analytics import capture_session_event
from core.repositories import container_repo, local_sessions_repo as repo, messaging_repo
from core.services.messaging import ApiProblem, ContractModel, require_room_member, validate_uuid
from core.services.local_session_grants import AgentInvocation

STATUSES = Literal["idle", "busy", "waiting-approval", "unknown", "offline", "ended"]
# What a device may claim. `offline` is the backend's verdict on a lapsed lease and
# `ended` is written by the ended frame; neither is ever a status the device reports.
INPUT_STATUSES = Literal["idle", "busy", "waiting-approval", "unknown"]


def iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


logger = logging.getLogger(__name__)

LIVE_STATUSES = frozenset(get_args(INPUT_STATUSES))
WORKSPACE_SESSION_CAP = 100


OpaqueId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
DeviceId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
EnrollmentRevision = Annotated[
    str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")
]
MAX_ENVELOPE_BYTES = 16 * 1024
# Leave room for server-authored provenance inside the transport envelope.
MAX_COMMAND_BYTES = 8 * 1024
OFFLINE_SEND_SECONDS = 24 * 3600
OFFLINE_SEND_PENDING_LIMIT = 20


class EnrollmentRequest(ContractModel):
    device_id: DeviceId
    cli_device_id: DeviceId | None = None
    enabled: bool
    expected_revision: OpaqueId | None = None
    owner_id: OpaqueId | None = None
    enable_revision: EnrollmentRevision | None = None
    previous_revision: EnrollmentRevision | None = None

    @model_validator(mode="after")
    def conditional_disable_only(self):
        if self.enabled and self.expected_revision is not None:
            raise ValueError("expectedRevision is only valid for disable")
        predecessor = "previous_revision" in self.model_fields_set
        if self.enable_revision is not None and (not self.enabled or not predecessor):
            raise ValueError("enableRevision requires enable and an explicit predecessor")
        if predecessor:
            if (
                self.owner_id is None
                or (self.enabled and self.enable_revision is None)
                or (not self.enabled and self.expected_revision is None)
            ):
                raise ValueError("A nominated enrollment requires owner and revision")
            revision = self.enable_revision if self.enabled else self.expected_revision
            parsed = uuid.UUID(revision)
            if str(parsed) != revision or parsed.version != 4:
                raise ValueError("Enrollment revision must be a canonical UUIDv4")
            if revision == self.previous_revision:
                raise ValueError("Enrollment revision must be fresh")
        return self


class LocalDevice(ContractModel):
    device_id: str
    human_user_id: str
    cli_device_id: str | None = None


class EnrollmentResponse(LocalDevice):
    owner_id: str
    enabled: bool
    revision: str


class Capabilities(ContractModel):
    send_mode: Literal["none", "queue", "immediate"] = "none"
    steer: bool = False
    interrupt: bool = False


ControlUnavailableReason = Literal["runtime-unverified", "endpoint-unavailable", "unsupported-version", "history-gap"]


class WorkspaceSummary(ContractModel):
    revision: int = Field(default=0, ge=0)
    coverage: Literal["bounded", "unknown"] = "unknown"
    changed: bool = False
    overlap: bool = False


class SessionRegistration(ContractModel):
    workspace: WorkspaceSummary = Field(default_factory=WorkspaceSummary)
    provider: Literal["claude", "codex"]
    profile_fingerprint: DeviceId
    native_conversation_id: OpaqueId
    generation: OpaqueId
    provider_version: Annotated[str, Field(min_length=1, max_length=128)]
    status: STATUSES = "offline"
    control_unavailable_reason: ControlUnavailableReason | None = None
    active_turn_id: OpaqueId | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)
    # The adapter must verify THIS provider version; omitted never inherits trust.
    capabilities_verified: bool = False
    online_until: int = Field(default=0, ge=0)
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    start_id: OpaqueId | None = None


# Bounded well inside a DynamoDB number (38 digits) and a JS number's exact range.
TokenCount = Annotated[int, Field(default=0, ge=0, le=2**53)]


class TokenUsage(ContractModel):
    input_tokens: TokenCount = 0
    output_tokens: TokenCount = 0
    cache_read_tokens: TokenCount = 0
    cache_write_tokens: TokenCount = 0


Label = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\x21-\x7e]+$")]


class SessionStats(ContractModel):
    """What the harness wrote about its own run; each field is absent until it does."""

    model: Label | None = None
    permission_mode: Label | None = None
    usage: TokenUsage | None = None


ApprovalMode = Literal["acceptEdits", "plan", "default"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PendingApproval(ContractModel):
    """The permission prompt a session is holding open for a remote answer (desktop-reported).

    ``exact`` is the desktop's verdict that what a reader sees IS the call: a shell command
    that was cut or redacted cannot be approved remotely. ``input_digest`` binds an answer to
    the exact tool input, so a decision can never land on a different prompt.
    """

    approval_id: OpaqueId
    tool: Annotated[str, Field(max_length=64)]
    summary: Annotated[str, Field(max_length=300)] = ""
    input_digest: Digest
    exact: bool = False
    modes: list[ApprovalMode] = Field(default_factory=list, max_length=3)


class PrivateSendApproval(ContractModel):
    """Exact outbound message, available only to the human who owns this session."""

    approval_id: OpaqueId
    input_digest: Digest
    destination: Annotated[str, Field(min_length=1, max_length=256)]
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    request_id: OpaqueId


#: A pick-up snapshot is kept this long after its session ends (owner ruling 2026-09-27).
PICKUP_RETENTION_SECONDS = 7 * 24 * 3600


class PickupStatus(ContractModel):
    """Pick-up as a reader sees it. Everything past ``allowed`` is absent until it is known."""

    allowed: bool
    state: Literal["preparing", "ready", "unavailable"] | None = None
    reason: str | None = None
    snapshot_at: str | None = None
    bytes: int | None = None
    has_code: bool | None = None
    repo: str | None = None
    branch: str | None = None
    expires_at: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(
            self,
            handler(self),
            info,
            ("state", "reason", "snapshot_at", "bytes", "has_code", "repo", "branch", "expires_at"),
        )


class PickedUpFrom(ContractModel):
    session_id: str
    human_user_id: str


class ResumeStatus(ContractModel):
    """A resume someone asked for on this ended session: waiting for the owner's Mac, or refused by
    it. Never the message itself."""

    state: Literal["pending", "failed"]
    by_user_id: str
    requested_at: str
    # Why the Mac refused: ALREADY_RUNNING, FOLDER_MISSING, FOLDER_NOT_APPROVED, TOOL_UNAVAILABLE, SHARING_OFF, LAUNCH_FAILED.
    reason: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("reason",))


class LocalSession(ContractModel):
    workspace: WorkspaceSummary = Field(default_factory=WorkspaceSummary)
    session_id: str
    device_id: str
    human_user_id: str
    provider: Literal["claude", "codex"]
    generation: str
    status: STATUSES
    provider_version: str
    control_unavailable_reason: ControlUnavailableReason | None = None
    active_turn_id: str | None
    capabilities: Capabilities
    capabilities_verified: bool
    latest_event_seq: int = Field(default=0, ge=0)
    title: Annotated[str, Field(max_length=120)] | None = None
    # Lightbulb-only label; the native provider may keep changing its own title.
    display_title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    last_event_at: str | None = None
    ended_at: str | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
    # Only its own human sees the exact remote start that opened this native run.
    start_id: OpaqueId | None = None
    # A Claude Code background job: agent knowledge, never a human listing.
    kind: Literal["interactive", "background"] = "interactive"
    # Which live terminal a running session has: "held" (a Lightbulb holder runs it) or "attach"
    # (a Claude background session the Mac attaches to). Absent: its transcript is all there is.
    terminal: Literal["held", "attach"] | None = None
    # The session this one continues: a Claude session moved to the background (<-) is a fork.
    forked_from: OpaqueId | None = None
    # False while the owner has paused sharing this one session: only they can see it.
    shared: bool = True
    # Owner opt-in: workspace members may use this session's controls (off by default, cleared on end).
    team_control: bool = False
    # People the owner let in one by one ("Ask to join"): they may use the controls too. Cleared on end.
    allowed_senders: list[str] = Field(default_factory=list)
    # The READER's own ask to join, while it is pending or after it was declined; absent otherwise.
    join_request: Literal["pending", "declined"] | None = None
    # Which request that is, so a client can tell a newer answer from its own older reply.
    join_request_id: str | None = None
    # The owner's own view only: who is waiting for an answer, so the session itself can offer Allow/Decline.
    pending_joins: list[str] | None = None
    # Request id per waiting person, so an answer from the session names the request it saw.
    pending_join_ids: dict[str, str] | None = None
    pending_approval: PendingApproval | None = None
    # Read-only: the harness's own model and permission mode. Neither CLI takes a runtime
    # change from outside its terminal (Claude's mode only rides an approval's setMode).
    model: Label | None = None
    permission_mode: Label | None = None
    # Tokens as the harness reports them: the owner's own account data, so present only for
    # the owner -- and for everyone in the workspace once the owner shares it.
    usage: TokenUsage | None = None
    share_usage: bool = False
    # Present when the owner allows pick-up, and always for the owner (who holds the switch).
    pickup: PickupStatus | None = None
    # Set on the first registration of a session that a teammate's pick-up started.
    picked_up_from: PickedUpFrom | None = None
    # Ended sessions only: who besides the owner may resume it -- the people who could send when it ended.
    resume_allowed: list[str] = Field(default_factory=list)
    # A resume waiting for the owner's Mac, or the Mac's refusal of one (spec §5.7).
    resume: ResumeStatus | None = None
    # Server-computed: ended, not background, and this workspace may start sessions on its Macs.
    resumable: bool = False

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(
            self,
            handler(self),
            info,
            (
                "pending_approval",
                "start_id",
                "terminal",
                "forked_from",
                "model",
                "display_title",
                "permission_mode",
                "usage",
                "pickup",
                "picked_up_from",
                "resume",
                "join_request",
                "join_request_id",
                "pending_joins",
                "pending_join_ids",
            ),
        )


class PairedNode(ContractModel):
    device_id: str
    human_user_id: str
    client_id: Literal["openclaw-macos", "node-host"]
    mac_device_id: str | None


class RoomResources(ContractModel):
    devices: list[LocalDevice]
    sessions: list[LocalSession]
    paired_nodes: list[PairedNode]


class WorkspaceSessions(ContractModel):
    sessions: list[LocalSession]


def _drop_none(model, result: dict, info, names: tuple[str, ...]) -> dict:
    """An unset optional field is absent from the wire, never an explicit null.

    The web distinguishes the two: `"userId" in actor` is true for a null key, and
    `tool.lines != undefined` is true for null, which renders a call's label as its
    own output.
    """
    for name in names:
        if getattr(model, name) is None:
            result.pop(model.model_fields[name].alias if info.by_alias else name, None)
    return result


class EventActor(ContractModel):
    user_id: OpaqueId | None = None
    agent_id: OpaqueId | None = None

    @model_validator(mode="after")
    def one_identity(self):
        if (self.user_id is None) == (self.agent_id is None):
            raise ValueError("actor needs exactly one of userId or agentId")
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("user_id", "agent_id"))


class EventTool(ContractModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["running", "done", "error"]
    input: Any = None
    result: Any = None
    lines: int | None = Field(default=None, ge=0)

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("input", "result", "lines"))


Lifecycle = Literal[
    "turn_start", "turn_end", "turn_aborted", "compaction", "approval_pending", "subagent_start", "subagent_end"
]


class LocalSessionEventBody(ContractModel):
    """One transcript item as the harness drew it. Every field past `truncated` is optional wire growth."""

    kind: Literal["text", "tool", "thinking", "lifecycle"]
    text: str = Field(max_length=MAX_ENVELOPE_BYTES)
    role: Literal["user", "assistant", "system", "tool"]
    truncated: bool = False
    ts: Annotated[str, Field(max_length=40)] | None = None
    item_id: OpaqueId | None = None
    origin_request_id: OpaqueId | None = None
    parent_id: OpaqueId | None = None
    actor: EventActor | None = None
    tool: EventTool | None = None
    lifecycle: Lifecycle | None = None
    #: The subagent (Claude agent id, Codex child thread id) this event belongs to; absent for the session itself.
    subagent: OpaqueId | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        # Old-shape events stay byte-compatible (LocalSessionEventPage cannot use
        # response_model_exclude_none -- next_after_seq's null is meaningful).
        return _drop_none(
            self,
            handler(self),
            info,
            ("ts", "item_id", "origin_request_id", "parent_id", "actor", "tool", "lifecycle", "subagent"),
        )


class LocalSessionEvent(LocalSessionEventBody):
    seq: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_envelope(self):
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError("event exceeds 16 KiB; split text on UTF-8 boundaries")
        return self


class LocalSessionEventPage(ContractModel):
    events: list[LocalSessionEvent]
    next_after_seq: int | None


#: Optional command fields added after the wire froze: absent on the wire, never null, so a send
#: stays byte-identical for desktops (and request hashes) that predate them.
_APPROVAL_FIELDS = ("approval_id", "decision", "mode", "input_digest")


class LocalSessionCommand(ContractModel):
    request_id: OpaqueId
    expected_generation: OpaqueId
    kind: Literal["send", "interrupt", "approve"]
    text: str | None = Field(default=None, max_length=MAX_COMMAND_BYTES)
    expected_turn_id: OpaqueId | None = None
    approval_id: OpaqueId | None = None
    decision: Literal["allow", "deny"] | None = None
    mode: ApprovalMode | None = None
    input_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_command(self):
        if self.kind == "send" and (self.text is None or not self.text.strip()):
            raise ValueError("send requires text")
        if self.kind == "interrupt" and self.text is not None:
            raise ValueError("interrupt does not accept text")
        approval = [getattr(self, name) is not None for name in _APPROVAL_FIELDS]
        if self.kind == "approve":
            if not (self.approval_id and self.decision and self.input_digest) or self.expected_turn_id:
                raise ValueError("approve requires approvalId, decision and inputDigest")
            if self.mode is not None and self.decision != "allow":
                raise ValueError("a mode switch only accompanies allow")
            if self.text is not None and (self.decision != "deny" or len(self.text.encode("utf-8")) > 500):
                raise ValueError("only a deny carries a short message")
        elif any(approval):
            raise ValueError("approval fields belong to approve")
        if (
            len(self.model_dump_json(by_alias=True, include=set(LocalSessionCommand.model_fields)).encode("utf-8"))
            > MAX_COMMAND_BYTES
        ):
            raise ValueError("command exceeds 8 KiB")
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, _APPROVAL_FIELDS)


class AgentActor(ContractModel):
    kind: Literal["agent", "session"]
    id: str


class CommandReceipt(LocalSessionCommand):
    session_id: str
    room_id: str
    actor_user_id: str | None = None
    actor: AgentActor | None = None
    invocation_expires_at: int | None = None
    command_seq: int
    state: Literal["pending", "dispatched", "applied", "rejected", "unknown"]
    created_at: int
    deliver_by: int
    held: bool = False
    provider_message_id: str | None = None
    cancelled_at: int | None = None
    native_hop: int | None = Field(default=None, ge=1, le=2)

    @model_validator(mode="after")
    def author(self):
        if self.actor is None:
            if not self.actor_user_id or self.invocation_expires_at is not None:
                raise ValueError("Invalid command author")
        elif self.actor_user_id is not None or self.invocation_expires_at is None:
            raise ValueError("Invalid command author")
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        # Human wire stays byte-compatible with existing deny_unknown_fields clients.
        return _drop_none(
            self,
            handler(self),
            info,
            ("actor_user_id", "actor", "invocation_expires_at", "cancelled_at", "native_hop", *_APPROVAL_FIELDS),
        )


class OfflineOutbox(ContractModel):
    commands: list[CommandReceipt]


Caller = AuthContext | AgentInvocation


def caller_owner(auth: Caller) -> str:
    return auth.owner_id if isinstance(auth, AgentInvocation) else resolve_owner_id(auth)


async def require_caller(auth: Caller, room: str) -> None:
    owner = caller_owner(auth)
    if not isinstance(auth, AgentInvocation):
        await require_room_member(owner, auth.user_id, room)
        return
    meta = await messaging_repo.get_room(room)
    if (
        auth.room_id != room
        or auth.expires_at <= int(time.time())
        or not meta
        or meta.get("ownerId") != owner
        or await messaging_repo.get_member(room, "agent", auth.agent_id) is None
        # An agent answers everyone in the room, and a shared room's foreign
        # members must never hear this workspace's sessions.
        or await messaging_repo.has_foreign_members(room, owner)
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")


def command_caller(owner: str, command: dict) -> Caller:
    if "actor" in command:
        actor = AgentActor.model_validate(command["actor"])
        if actor.kind == "session":
            # Only the relay's native-source branch may restore this actor:
            # it rechecks the source session, pairing and live connection.
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native source needs device revalidation.")
        return AgentInvocation(
            owner,
            command["roomId"],
            actor.id,
            int(command["invocationExpiresAt"]),
            command["invocationId"],
            command.get("invocationRequesterId"),
        )
    user = command["actorUserId"]
    return AuthContext(user_id=user, org_id=owner if owner != user else None)


def _public(model, row: dict):
    """Allowlist projection: never serialize DynamoDB keys or native identity/config."""
    return model.model_validate(
        {field.alias: row[field.alias] for field in model.model_fields.values() if field.alias in row}
    )


def _viewer(auth: Caller) -> str | None:
    """The human reading a session; an agent never sees owner-private fields."""
    return None if isinstance(auth, AgentInvocation) else auth.user_id


def session_terminal(row: dict) -> Literal["held", "attach"] | None:
    """The Mac's word for the session's live terminal; a desktop that predates `terminal` names only its holder."""
    reported = row.get("terminal")
    if reported in ("held", "attach"):
        return reported
    return "held" if reported is None and row.get("holderId") else None


def ended_empty(provider: str, status: str, event_seq: int, control_unavailable_reason: str | None) -> bool:
    """A Claude Code session that ended before its first message: Claude writes a transcript only
    on that message, so there is nothing to show or resume (`claude --resume` answers "No
    conversation found"). Not Codex: its rollout holds the thread before any event is public, so
    `codex resume` can still find it. A row whose history the Mac could not read
    (`controlUnavailableReason`) may hold a real conversation: not empty."""
    return provider == "claude" and status == "ended" and event_seq == 0 and control_unavailable_reason is None


def _without_moved(found: list[LocalSession]) -> list[LocalSession]:
    """A stopped session that a later one of the same person continues (`forkedFrom`) is listed once:
    as that later one. Offline counts as stopped: a Mac still draining an ended origin re-registers it
    and then lets it lapse. A running origin is never hidden: `claude --bg --resume` copies a running
    session.

    A fork that ended before its first turn is the exception: Claude writes the copied history into
    the fork's transcript only on that turn, so the fork has no events and `claude --resume` cannot
    find it. It is hidden instead, and its origin (which holds the history) stays listed.

    Any other session that ended before its first message (`ended_empty`) is not listed either; it
    stays readable by id, for a panel that is open on it."""
    ids = {(s.human_user_id, s.session_id) for s in found}
    empty = {
        (s.human_user_id, s.session_id)
        for s in found
        if s.forked_from and s.status == "ended" and s.latest_event_seq == 0 and (s.human_user_id, s.forked_from) in ids
    }
    moved = {
        (s.human_user_id, s.forked_from)
        for s in found
        if s.forked_from and (s.human_user_id, s.session_id) not in empty
    }
    return [
        s
        for s in found
        if (s.human_user_id, s.session_id) not in empty
        and not ended_empty(s.provider, s.status, s.latest_event_seq, s.control_unavailable_reason)
        and not (s.status in ("ended", "offline") and (s.human_user_id, s.session_id) in moved)
    ]


def _session_public(row: dict, viewer: str | None = None) -> LocalSession:
    """One projection for every listing: ended beats lease state, an unverified
    capability set reads as none."""
    public = dict(row)
    public.pop("privateSendApproval", None)
    public["latestEventSeq"] = row.get("eventSeq", 0)
    public["lastEventAt"] = iso(row.get("lastEventAt"))
    public["endedAt"] = iso(row.get("endedAt"))
    public["shared"] = not row.get("unshared", False)
    if viewer != row["humanUserId"]:
        public.pop("startId", None)
    if row.get("endedAt") is not None:
        public["status"] = "ended"
    elif row["onlineUntil"] <= int(time.time()):
        public["status"] = "offline"
    public["terminal"] = None if public["status"] in ("ended", "offline") else session_terminal(row)
    if not row["capabilitiesVerified"]:
        public["capabilities"] = Capabilities().model_dump(by_alias=True)
    public["teamControl"] = team_control_on(row)
    public["allowedSenders"] = allowed_senders(row)
    ended = row.get("endedAt") is not None
    empty = ended_empty(row["provider"], public["status"], row.get("eventSeq", 0), row.get("controlUnavailableReason"))
    public["resumeAllowed"] = list(row.get("resumeAllowed") or []) if ended and not empty else []
    from core.services import session_resume  # lazy: it imports this module

    public["resumable"] = (
        ended and not empty and row.get("kind") != "background" and session_resume.submissions_enabled(row["ownerId"])
    )
    # The stored request carries the message; only its state, author and time are public.
    waiting = public.pop("resume", None)
    if ended and waiting and int(waiting["until"]) > int(time.time()):
        public["resume"] = {
            "state": waiting["state"],
            "byUserId": waiting["byUserId"],
            "requestedAt": iso(waiting["at"]),
            "reason": waiting.get("code"),
        }
    request = (row.get("joinRequests") or {}).get(viewer) if viewer else None
    if request and row.get("endedAt") is None:
        public["joinRequest"] = request["state"]
        public["joinRequestId"] = request["requestId"]
    if viewer and viewer == row["humanUserId"] and row.get("endedAt") is None:
        waiting = {
            who: r["requestId"] for who, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"
        }
        if waiting:
            public["pendingJoins"] = sorted(waiting)
            public["pendingJoinIds"] = waiting
    stats = row.get("stats") or {}
    for key in ("model", "permissionMode"):
        if stats.get(key):
            public[key] = stats[key]
    public["shareUsage"] = bool(row.get("shareUsage"))
    if stats.get("usage") and viewer is not None and (viewer == row["humanUserId"] or public["shareUsage"]):
        public["usage"] = stats["usage"]
    if public["status"] != "waiting-approval":
        public.pop("pendingApproval", None)
    if row.get("pickupAllowed") or (viewer is not None and viewer == row["humanUserId"]):
        public["pickup"] = _pickup_public(row)
    return _public(LocalSession, public)


def _pickup_public(row: dict) -> dict:
    if not row.get("pickupAllowed"):
        return {"allowed": False}
    snapshot = row.get("pickupSnapshot") or {}
    out = {
        "allowed": True,
        "state": snapshot.get("state", "preparing"),
        "reason": snapshot.get("reason"),
        "snapshotAt": iso(snapshot.get("at")),
        "bytes": snapshot.get("bytes"),
        "hasCode": snapshot.get("hasCode"),
        "repo": snapshot.get("repo"),
        "branch": snapshot.get("branch"),
    }
    stopped = pickup_stopped_at(row)
    if stopped is not None:
        out["expiresAt"] = iso(stopped + PICKUP_RETENTION_SECONDS)
    return out


def pickup_stopped_at(row: dict, now: int | None = None) -> int | None:
    """When the session was last known alive: its end, else a lapsed lease. A live lease has not
    stopped. A Mac that went away never sends Ended, so without the lease a snapshot would live
    forever; the cost is that a laptop closed for more than the retention loses its snapshot and
    permission while its terminal is still open. Deleting is the safe direction; the author can
    allow it again."""
    if row.get("endedAt") is not None:
        return int(row["endedAt"])
    lease = int(row.get("onlineUntil", 0))
    return lease if lease <= (int(time.time()) if now is None else now) else None


def team_control_on(row: dict) -> bool:
    """The owner's per-session opt-in, which never outlives the session."""
    return bool(row.get("teamControl")) and row.get("endedAt") is None


def allowed_senders(row: dict) -> list[str]:
    """The people the owner let in one by one; like team control, never past the session's end."""
    return list(row.get("allowedSenders") or []) if row.get("endedAt") is None else []


def may_control(row: dict, user_id: str) -> bool:
    """A HUMAN who may use this session's controls: its owner, anyone while team control is on,
    or a person the owner allowed. Agents never ride either opt-in."""
    return user_id == row["humanUserId"] or team_control_on(row) or user_id in allowed_senders(row)


async def _paired(owner_id: str, user_id: str, device_id: str, cli_device_id: str | None) -> dict:
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    pairs = (container or {}).get("paired_devices", {})
    for key, client in ((device_id, "openclaw-macos"), (cli_device_id, "node-host")):
        if key is None:
            continue
        pair = pairs.get(key, {})
        if pair.get("userId") != user_id or pair.get("clientId") != client:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Paired desktop ownership is required.")
    return pairs


async def set_enrollment(auth: AuthContext, request: EnrollmentRequest) -> EnrollmentResponse:
    owner = resolve_owner_id(auth)
    if request.owner_id is not None and request.owner_id != owner:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Enrollment workspace mismatch.")
    await _paired(owner, auth.user_id, request.device_id, request.cli_device_id)
    try:
        row = await repo.put_enrollment(
            owner,
            auth.user_id,
            request.device_id,
            request.cli_device_id,
            request.enabled,
            request.expected_revision,
            enable_revision=request.enable_revision,
            previous_revision=request.previous_revision,
            has_predecessor="previous_revision" in request.model_fields_set,
        )
    except ClientError as exc:
        if not messaging_repo._is_cancelled(exc):
            raise
        if request.expected_revision is not None or request.enable_revision is not None:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "Enrollment changed; cleanup is obsolete.") from exc
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Device pairing changed. Try again.") from exc
    # The workspace session list enumerates humans via their private user
    # stream, which is otherwise only created when someone opens web/mobile
    # chat. A desktop-only sharer would stay invisible (and miss workspace
    # hints) until they did, so enrollment carries the stream with it.
    # Best-effort: enrollment itself already succeeded.
    try:
        await messaging_repo.ensure_user_stream(owner, auth.user_id)
    except Exception:
        logger.warning("Local session enrollment left without a user stream for %s", auth.user_id)
    return _public(EnrollmentResponse, row)


async def _enrolled(owner: str, human: str, device: str) -> dict:
    enrollment = await repo.get_enrollment(owner, human, device)
    if not enrollment or not enrollment.get("enabled"):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")
    # The optional node association is validated at enrollment, not a second
    # credential required for native sharing. Only canonical Mac revocation
    # removes this authority; omit a companion that is no longer eligible.
    pairs = await _paired(owner, human, device, None)
    cli = pairs.get(enrollment.get("cliDeviceId"), {})
    if cli.get("userId") != human or cli.get("clientId") != "node-host":
        enrollment = {**enrollment, "cliDeviceId": None}
    return enrollment


async def register_session(owner_id: str, user_id: str, device_id: str, registration: SessionRegistration) -> dict:
    """Server-internal entry; a machine can report state but cannot enable enrollment."""
    sid = repo.session_id(
        owner_id,
        device_id,
        registration.provider,
        registration.profile_fingerprint,
        registration.native_conversation_id,
    )
    for _ in range(6):
        enrollment = await _enrolled(owner_id, user_id, device_id)
        previous = await repo.get_session(owner_id, sid)
        row = {
            **{
                key: previous[key]
                # Content sharing survives a re-registration; control (teamControl) does not.
                for key in (
                    "workLink",
                    "workLinkRevision",
                    "unshared",
                    "pickupAllowed",
                    "pickupSnapshot",
                    "pickupEpoch",
                    "pickedUpFrom",
                    "displayTitle",
                )
                if previous and key in previous
            },
            **registration.model_dump(by_alias=True),
            "pk": repo.session_pk(owner_id, sid),
            "sk": "META",
            "ownerId": owner_id,
            "humanUserId": user_id,
            "deviceId": device_id,
            "sessionId": sid,
            "revision": str(uuid.uuid4()),
            "commandSeq": (previous or {}).get("commandSeq", 0),
            "lastEventAt": int(time.time()),
        }
        if registration.start_id is None and previous and previous.get("generation") == registration.generation:
            row["startId"] = previous.get("startId")
        elif registration.start_id is None:
            row.pop("startId", None)
        if previous and previous.get("generation") == registration.generation:
            # A reconnect keeps the people the owner let in; a new process (or the end) does not.
            row.update({key: previous[key] for key in JOIN_FIELDS if key in previous})
        try:
            await repo.put_session(row, previous, enrollment)
            # Defense in depth for enrollments that predate the
            # ensure-on-enroll above: the first register after this change
            # makes the sharer enumerable in the workspace list.
            # Best-effort: registration itself already succeeded.
            try:
                await messaging_repo.ensure_user_stream(owner_id, user_id)
            except Exception:
                logger.warning("Local session register left without a user stream for %s", user_id)
            return row
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def authorize_session(
    auth: Caller, room_id: str | None, session_id: str, *, owner_only: bool = False
) -> tuple[dict, dict]:
    """One authorization path for directory hydration, events, commands and receipts.

    ``room_id`` is the agent path (and legacy room-scoped commands): caller and session owner
    must share that room. Without one, a session is a workspace thing (owner ruling
    2026-09-24: sessions left rooms): any member of the workspace may READ it, and only its
    own human may command it or change it (``owner_only``). The desktop's native approval
    still applies on top.
    """
    owner = caller_owner(auth)
    if room_id:
        room_id = validate_uuid(room_id, label="room ID")
        await require_caller(auth, room_id)
    elif isinstance(auth, AgentInvocation):
        # An agent's authority is its room grant; it has no roomless path.
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")
    session_id = validate_uuid(session_id, label="local session ID")
    row = await repo.get_session(owner, session_id)
    if row is None or row.get("ownerId") != owner:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    agent = isinstance(auth, AgentInvocation)
    if row.get("unshared") and (agent or auth.user_id != row["humanUserId"]):
        # A session its owner stopped sharing reads as gone to everyone else, agents included.
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if room_id and await messaging_repo.get_member(room_id, "human", row["humanUserId"]) is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Session owner is no longer in this room.")
    if owner_only and (agent or not room_id):
        # Ruling 2026-09-24: an agent commands a session only on behalf of that session's own
        # human, i.e. when the message that woke it was theirs. Routines have no requester.
        # Owner ruling 2026-09-25: a workspace member may control it while its owner has turned
        # on "Let teammates control this session". Agents never ride that opt-in.
        # Owner ruling 2026-09-28: or a person the owner allowed after they asked to join.
        caller = auth.requester_id if agent else auth.user_id
        allowed = caller == row["humanUserId"] if agent else may_control(row, caller)
        if not allowed:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
    return row, await _enrolled(owner, row["humanUserId"], row["deviceId"])


async def list_resources(auth: Caller, room_id: str, *, include_ended: bool = False) -> RoomResources:
    owner = caller_owner(auth)
    room_id = validate_uuid(room_id, label="room ID")
    await require_caller(auth, room_id)
    devices, sessions, paired_nodes = [], [], []
    # ponytail: bounded room membership, per-human directory reads; batch only if measured slow.
    for member in await messaging_repo.list_members(room_id):
        if member.get("kind") != "human":
            continue
        human = member["id"]
        enrollments = await repo.list_enrollments(owner, human)
        # Pairing is independent of sharing. Directory rows promise no liveness
        # or physical co-location; only explicit, unambiguous associations group.
        pairs = (await container_repo.get_by_owner_id(owner, consistent=True) or {}).get("paired_devices", {})
        owned = {key: pair for key, pair in pairs.items() if pair.get("userId") == human}
        associations: dict[str, set[str]] = {}
        for enrollment in enrollments:
            mac, cli = enrollment["deviceId"], enrollment.get("cliDeviceId")
            if (
                owned.get(mac, {}).get("clientId") == "openclaw-macos"
                and owned.get(cli, {}).get("clientId") == "node-host"
            ):
                associations.setdefault(cli, set()).add(mac)
        await require_caller(auth, room_id)
        if await messaging_repo.get_member(room_id, "human", human) is None:
            continue
        for key, pair in sorted(owned.items()):
            client = pair.get("clientId")
            if client not in {"openclaw-macos", "node-host"}:
                continue
            matches = associations.get(key, set())
            paired_nodes.append(
                PairedNode(
                    device_id=key,
                    human_user_id=human,
                    client_id=client,
                    mac_device_id=key
                    if client == "openclaw-macos"
                    else next(iter(matches))
                    if len(matches) == 1
                    else None,
                )
            )
        for enrollment in enrollments:
            try:
                await require_caller(auth, room_id)
                if await messaging_repo.get_member(room_id, "human", human) is None:
                    continue
                current = await _enrolled(owner, human, enrollment["deviceId"])
                devices.append(_public(LocalDevice, current))
            except ApiProblem as exc:
                if exc.error.code != "LOCAL_SESSION_FORBIDDEN":
                    raise
        for projection in await repo.list_user_sessions(owner, human):
            try:
                row, _ = await authorize_session(auth, room_id, projection["sessionId"])
                public = _session_public(row, _viewer(auth))
                if public.kind == "interactive" or isinstance(auth, AgentInvocation):
                    sessions.append(public)
            except ApiProblem as exc:
                if exc.error.code not in {"LOCAL_SESSION_FORBIDDEN", "LOCAL_SESSION_NOT_FOUND"}:
                    raise
    sessions = _without_moved(sessions)
    live = sorted((s for s in sessions if s.status != "ended"), key=lambda s: s.last_event_at or "", reverse=True)
    if include_ended:
        recent = sorted(
            (s for s in sessions if s.status == "ended"), key=lambda s: s.last_event_at or "", reverse=True
        )[:20]
        sessions = live + recent
    else:
        sessions = live
    await require_caller(auth, room_id)
    return RoomResources(devices=devices, sessions=sessions, paired_nodes=paired_nodes)


async def _live_public_sessions(
    owner: str,
    humans: list[str],
    *,
    include_offline: bool = False,
    ended_since: int | None = None,
    viewer: str | None = None,
) -> list[LocalSession]:
    """Per-human fetch/filter loop shared by every workspace-wide listing. Skips rows
    whose owner mismatches, whose device is no longer enrolled, or whose projected
    status is not live (plus `offline` when the caller opts in). Returned
    newest-`last_event_at`-first, every caller's order."""
    projections = await asyncio.gather(*(repo.list_user_sessions(owner, human) for human in humans))
    sessions: list[LocalSession] = []
    # One enrollment verdict per (human, device) per call: history piles up on one Mac.
    enrolled: dict[tuple[str, str], bool] = {}
    for human, human_rows in zip(humans, projections):
        # ponytail: one concurrent read per historical row; an owner-keyed live index if history grows large.
        rows = await asyncio.gather(*(repo.get_session(owner, p["sessionId"]) for p in human_rows))
        for row in rows:
            if row is None or row.get("ownerId") != owner or row.get("humanUserId") != human:
                continue
            key = (human, row["deviceId"])
            if key not in enrolled:
                try:
                    await _enrolled(owner, human, row["deviceId"])
                    enrolled[key] = True
                except ApiProblem as exc:
                    if exc.error.code != "LOCAL_SESSION_FORBIDDEN":
                        raise
                    enrolled[key] = False
            if not enrolled[key]:
                continue
            public = _session_public(row, viewer)
            if (
                public.status in LIVE_STATUSES
                or (include_offline and public.status == "offline")
                or (ended_since is not None and public.status == "ended" and row["endedAt"] >= ended_since)
            ):
                sessions.append(public)
    sessions.sort(key=lambda s: s.last_event_at or "", reverse=True)
    return sessions


RECENT_ENDED_SECONDS = 7 * 24 * 3600
RECENT_ENDED_CAP = 20


async def list_workspace_sessions(auth: Caller, *, include_ended: bool = False) -> WorkspaceSessions:
    """Every live session in the workspace, plus lapsed-lease (`offline`) ones
    so a sleeping Mac reads as Offline rather than vanishing. Visibility is
    workspace-wide by owner ruling (spec §2.3); membership is not consulted.
    ``include_ended`` appends the sidebar's "Recent": sessions that ended in the last
    7 days (their transcripts are kept -- nothing reaps them), newest first."""
    owner = caller_owner(auth)
    humans = [row["userId"] for row in await messaging_repo.list_active_user_streams(owner)]
    since = int(time.time()) - RECENT_ENDED_SECONDS if include_ended else None
    # ponytail: one query per human per call; add an owner-keyed directory row when workspaces pass ~50 humans.
    viewer = _viewer(auth)
    found = _without_moved(
        [
            s
            for s in await _live_public_sessions(owner, humans, include_offline=True, ended_since=since, viewer=viewer)
            # ponytail: legacy rows only; delete once no pre-2026-10-03 row is in the 7-day Recent window
            if s.kind == "interactive" and (s.shared or s.human_user_id == viewer)
        ]
    )
    live = [s for s in found if s.status != "ended"][:WORKSPACE_SESSION_CAP]
    return WorkspaceSessions(sessions=live + [s for s in found if s.status == "ended"][:RECENT_ENDED_CAP])


SESSION_SUMMARY_CAP = 24


async def live_sessions_for_room(owner: str, room_id: str, members: list[dict]) -> list[dict]:
    """Bounded projection of the room's live sessions for agent ingress.
    Never raises: a missing summary is worse than a blocked dispatch, but not by much.
    Session visibility is workspace-wide, so a room shared with another organization
    (#2644) gets no summary: any member whose home differs from the room's owner
    blanks the whole result before anything is queried."""
    if any(messaging_repo.member_home(member, owner) != owner for member in members):
        return []
    try:
        names = {member["id"]: member.get("displayName") for member in members if member.get("kind") == "human"}
        sessions = [s for s in await _live_public_sessions(owner, list(names)) if s.shared]
        # Background jobs are listed too. No `kind` key yet: the baked plugin 400s an unknown
        # session field, so it lands here only after an image whose parser accepts it is rolled.
        include = {"session_id", "provider", "cwd_name", "title", "status", "latest_event_seq", "last_event_at"}
        out = [
            {
                **s.model_dump(by_alias=True, include=include),
                "title": s.display_title or s.title,
                "humanUserId": s.human_user_id,
                "displayName": names.get(s.human_user_id) or "Workspace member",
            }
            for s in sessions
        ]
        return out[:SESSION_SUMMARY_CAP]
    except Exception:
        logger.warning("Live session summary unavailable for room %s", room_id, exc_info=True)
        return []


async def referenced_sessions(owner: str, members: list[dict], session_ids: list[str]) -> list[dict]:
    """The sessions a room message references, projected for agent ingress. A reference is a
    link, not a wake (owner ruling 2026-10-02), so offline and ended sessions resolve too --
    under the message writer's own rule: shared, interactive, owner still in the room, and
    no member from another organization. Never raises: a missing line beats a lost turn."""
    if any(messaging_repo.member_home(member, owner) != owner for member in members):
        return []
    humans = {member["id"] for member in members if member.get("kind") == "human"}
    out: list[dict] = []
    for session_id in dict.fromkeys(session_ids):
        try:
            row = await repo.get_session(owner, session_id)
            if (
                row is None
                or row.get("ownerId") != owner
                or row.get("unshared")
                or row.get("kind") == "background"
                or row.get("humanUserId") not in humans
            ):
                continue
            public = _session_public(row)
            out.append(
                {
                    "sessionId": session_id,
                    "provider": public.provider,
                    "title": public.display_title or public.title or public.cwd_name,
                    "status": public.status,
                }
            )
        except Exception:
            logger.warning("Referenced session %s unresolved", session_id, exc_info=True)
    return out


async def set_sharing(auth: AuthContext, session_id: str, shared: bool) -> LocalSession:
    """The owner pauses or resumes sharing one session. While paused the relay drops its
    transcript instead of storing it (one notice marks the gap) and nobody else can see it."""
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id, owner_only=True)
        # owner_only admits teammates while "Let teammates control" is on; sharing stays the owner's.
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
        if bool(row.get("unshared")) == shared:  # a change, not a no-op
            updated = {**row, "revision": str(uuid.uuid4())}
            if shared:
                updated.pop("unshared", None)
                updated.pop("pauseNoticed", None)
            else:
                updated["unshared"] = True
                updated.pop("pendingApproval", None)
                updated.pop("privateSendApproval", None)
            try:
                await repo.put_session(updated, row, enrollment)
            except ClientError as exc:
                if not messaging_repo._is_cancelled(exc):
                    raise
                continue
            row = updated
        return _session_public(row, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def get_session(auth: Caller, session_id: str) -> LocalSession:
    """One session for any workspace member, ended included: the list keeps only live
    ones, but an open view or an old link must still show a session that has ended."""
    row, _ = await authorize_session(auth, None, session_id)
    return _session_public(row, _viewer(auth))


class DisplayTitleRequest(ContractModel):
    display_title: Annotated[str, Field(max_length=120)] | None

    @field_validator("display_title")
    @classmethod
    def validate_display_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title or any(ord(char) < 0x20 or ord(char) == 0x7F for char in title):
            raise ValueError("displayTitle must be a single non-empty line")
        return title


async def set_display_title(auth: AuthContext, session_id: str, title: str | None) -> LocalSession:
    """The session's own human names its Lightbulb projection, not the native transcript."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can rename it.")
        if row.get("displayTitle") == title:
            return _session_public(row, auth.user_id)
        updated = {**row, "revision": str(uuid.uuid4())}
        if title is None:
            updated.pop("displayTitle", None)
        else:
            updated["displayTitle"] = title
        audit = audit_row(owner, session_id, auth, "display_title_changed")
        try:
            await repo.put_session(
                updated, row, enrollment, extra=[{"Put": {"TableName": messaging_repo._table().name, "Item": audit}}]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return _session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def get_private_send_approval(auth: AuthContext, session_id: str) -> PrivateSendApproval:
    row, _ = await authorize_session(auth, None, session_id)
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Private approval is unavailable.")
    pending, detail = row.get("pendingApproval") or {}, row.get("privateSendApproval") or {}
    if (
        auth.user_id != row["humanUserId"]
        or row["status"] != "waiting-approval"
        or row["onlineUntil"] <= int(time.time())
        or row.get("endedAt") is not None
        or pending.get("tool") != "mcp__lightbulb_native_sessions__send"
        or pending.get("approvalId") != detail.get("approvalId")
        or pending.get("inputDigest") != detail.get("inputDigest")
    ):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Private approval is unavailable.")
    return PrivateSendApproval.model_validate(
        {key: detail.get(key) for key in ("approvalId", "inputDigest", "destination", "text", "requestId")}
    )


async def list_events(
    auth: Caller, room_id: str | None, session_id: str, after_seq: int = 0, limit: int = 50
) -> LocalSessionEventPage:
    session, _ = await authorize_session(auth, room_id, session_id)
    if after_seq < 0 or after_seq >= 10**20 - 1 or not 1 <= limit <= 100:
        raise ApiProblem("INVALID_REQUEST", "Invalid event pagination.")
    rows, next_seq = await repo.list_events(session["ownerId"], session["sessionId"], after_seq, limit)
    if room_id:
        await require_caller(auth, room_id)
    return LocalSessionEventPage(events=[_public(LocalSessionEvent, row) for row in rows], next_after_seq=next_seq)


def require_command_scope(auth: Caller, row: dict, kind: str) -> None:
    """A person let in one by one may send follow-ups and answer prompts, never stop the owner's
    run. Checked when a command is accepted and again when it is delivered."""
    if kind == "approve" and (row.get("pendingApproval") or {}).get("tool") == "mcp__lightbulb_native_sessions__send":
        if isinstance(auth, AgentInvocation) or auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only this session's owner can answer its private send.")
    if kind == "interrupt" and not isinstance(auth, AgentInvocation):
        if auth.user_id != row["humanUserId"] and not team_control_on(row):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can stop this run.")


def _require_control(row: dict, request: LocalSessionCommand, *, allow_offline_send: bool = False) -> None:
    if row["generation"] != request.expected_generation:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session generation changed.")
    if row.get("endedAt") is not None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
    if (row["status"] == "offline" or row["onlineUntil"] <= int(time.time())) and not allow_offline_send:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session is offline.")
    caps = row["capabilities"]
    if not row["capabilitiesVerified"]:
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Sending is unavailable for this session.")
    if request.kind == "approve":
        _require_pending_approval(row, request)
        return
    if request.kind == "interrupt" and not caps["interrupt"]:
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider cannot interrupt.")
    if request.kind == "send" and caps["sendMode"] == "none":
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider cannot accept input.")
    if request.expected_turn_id is not None:
        if request.expected_turn_id != row.get("activeTurnId"):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The active turn changed.")
        if request.kind in {"send", "interrupt"} and row["status"] != "busy":
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The session is not in a steerable turn.")
        if request.kind == "send" and not caps["steer"]:
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider supports queued input only.")
    elif request.kind == "interrupt" or (
        request.kind == "send" and row["status"] == "busy" and caps["sendMode"] == "immediate"
    ):
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "An active turn ID is required.")


def _require_pending_approval(row: dict, request: LocalSessionCommand) -> None:
    """An answer lands only on the exact prompt its sender saw, while it is still open."""
    pending = row.get("pendingApproval")
    if row["status"] != "waiting-approval" or not pending:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "The session is no longer waiting for approval.")
    if pending["approvalId"] != request.approval_id or pending["inputDigest"] != request.input_digest:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "That permission prompt has changed.")
    if pending.get("tool") == "mcp__lightbulb_native_sessions__send":
        detail = row.get("privateSendApproval") or {}
        if request.mode is not None:
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "That option is not offered for this prompt.")
        if request.decision == "allow" and (
            detail.get("approvalId") != request.approval_id or detail.get("inputDigest") != request.input_digest
        ):
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This private send can only be approved in the terminal.")
        return
    if request.decision == "allow" and not pending.get("exact"):
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This prompt can only be approved in the terminal.")
    if request.mode is not None and request.mode not in pending.get("modes", []):
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "That option is not offered for this prompt.")


async def submit_command(
    auth: Caller,
    room_id: str | None,
    session_id: str,
    request: LocalSessionCommand,
    *,
    source_session_id: str | None = None,
    source_generation: str | None = None,
    source_turn_id: str | None = None,
    native_hop: int | None = None,
    source_conditions: list[dict] | None = None,
    source_pairing: tuple[str, str] | None = None,
    room_meta_fenced_by_extra: bool = False,
    replaces_request_id: str | None = None,
) -> CommandReceipt:
    owner = caller_owner(auth)
    # A roomless command is stored with roomId "": current desktops require the field as a string.
    room_id = validate_uuid(room_id, label="room ID") if room_id else ""
    session_id = validate_uuid(session_id, label="local session ID")
    if source_session_id and (request.kind != "send" or native_hop not in {1, 2} or not source_generation):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native sessions can send only bounded messages.")
    if native_hop is not None and not source_session_id:
        raise ApiProblem("INVALID_REQUEST", "Native hop needs a verified source session.")
    if request.kind == "approve" and (isinstance(auth, AgentInvocation) or source_session_id):
        # A permission prompt is a human's decision: an agent (whoever woke it) never answers one.
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only a person can answer a permission prompt.")
    if replaces_request_id is not None:
        from pydantic import TypeAdapter, ValidationError

        try:
            TypeAdapter(OpaqueId).validate_python(replaces_request_id)
        except ValidationError as exc:
            raise ApiProblem("INVALID_REQUEST", "Invalid replaced request ID.") from exc
        if (
            isinstance(auth, AgentInvocation)
            or request.kind != "send"
            or replaces_request_id == request.request_id
            or room_id
            or source_session_id
        ):
            raise ApiProblem("INVALID_REQUEST", "Only a direct human follow-up can replace an offline send.")
    payload = request.model_dump(by_alias=True)
    author = (
        {"actor": {"kind": "session", "id": source_session_id}, "invocationExpiresAt": int(time.time()) + 30}
        if source_session_id
        else (
            {
                "actor": {"kind": "agent", "id": auth.agent_id},
                "invocationExpiresAt": auth.expires_at,
                "invocationId": auth.invocation_id,
                "invocationRequesterId": auth.requester_id,
            }
            if isinstance(auth, AgentInvocation)
            else {"actorUserId": auth.user_id}
        )
    )
    req_hash = messaging_repo.request_hash(
        owner,
        session_id,
        room_id,
        {"kind": "session", "id": source_session_id}
        if source_session_id
        else ({"kind": "agent", "id": auth.agent_id} if isinstance(auth, AgentInvocation) else auth.user_id),
        payload,
        *([{"replacesRequestId": replaces_request_id}] if replaces_request_id else []),
    )
    for _ in range(6):
        row, enrollment = await authorize_session(auth, room_id, session_id, owner_only=True)
        existing = await repo.get_command(owner, session_id, request.request_id)
        if existing:
            if existing["requestHash"] != req_hash:
                raise ApiProblem("IDEMPOTENCY_CONFLICT", "requestId was already used for a different request.")
            return _public(CommandReceipt, existing)
        retry_update: dict | None = None
        if replaces_request_id:
            original = await repo.get_command(owner, session_id, replaces_request_id)
            if (
                original is None
                or original.get("actorUserId") != auth.user_id
                or original.get("kind") != "send"
                or original.get("roomId") != ""
                or not original.get("offlineIdentity")
                or original.get("state") != "rejected"
                or original.get("cancelledAt")
                or original.get("text") != request.text
            ):
                raise ApiProblem("LOCAL_SESSION_CONFLICT", "The saved offline send cannot be retried.")
            prior_replacement = original.get("replacedByRequestId")
            if prior_replacement:
                previous_retry = await repo.get_command(owner, session_id, prior_replacement)
                if (
                    previous_retry is None
                    or previous_retry.get("replacesRequestId") != replaces_request_id
                    or previous_retry.get("actorUserId") != auth.user_id
                    or previous_retry.get("state") != "rejected"
                ):
                    raise ApiProblem("LOCAL_SESSION_CONFLICT", "A retry is already in progress or was delivered.")
            values = {
                ":actor": auth.user_id,
                ":rejected": "rejected",
                ":replacement": request.request_id,
                ":revision": str(uuid.uuid4()),
            }
            pointer_condition = "attribute_not_exists(replacedByRequestId)"
            if prior_replacement:
                pointer_condition = "replacedByRequestId = :previous"
                values[":previous"] = prior_replacement
            retry_update = {
                "Update": {
                    "TableName": messaging_repo._table().name,
                    "Key": {"pk": repo.session_pk(owner, session_id), "sk": f"COMMAND#{replaces_request_id}"},
                    "UpdateExpression": "SET replacedByRequestId = :replacement, revision = :revision",
                    "ConditionExpression": (
                        f"#state = :rejected AND actorUserId = :actor AND attribute_not_exists(cancelledAt) "
                        f"AND {pointer_condition}"
                    ),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": values,
                }
            }
        require_command_scope(auth, row, request.kind)
        now = int(time.time())
        offline_send = (
            not isinstance(auth, AgentInvocation)
            and not room_id
            and not source_session_id
            and request.kind == "send"
            and request.expected_turn_id is None
            and (row["status"] == "offline" or row["onlineUntil"] <= now)
            and row["capabilities"]["sendMode"] == "queue"
        )
        _require_control(row, request, allow_offline_send=offline_send)
        if isinstance(auth, AgentInvocation) and now >= auth.expires_at:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")
        if offline_send and auth.user_id != row["humanUserId"]:
            stream = await messaging_repo.get_user_stream(owner, auth.user_id)
            if not stream or not stream.get("active"):
                raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Sender is no longer in this workspace.")
        if (
            offline_send
            and sum(
                1
                for item in await repo.list_commands(owner, session_id)
                if item.get("offlineIdentity")
                and item["state"] not in {"applied", "rejected"}
                and item["deliverBy"] > now
            )
            >= OFFLINE_SEND_PENDING_LIMIT
        ):
            raise ApiProblem("LOCAL_SESSION_QUEUE_FULL", "This session has too many waiting follow-ups.")
        receipt = {
            **payload,
            "pk": repo.session_pk(owner, session_id),
            "sk": f"COMMAND#{request.request_id}",
            "sessionId": session_id,
            "roomId": room_id,
            **author,
            "commandSeq": row["commandSeq"] + 1,
            "state": "pending",
            "createdAt": now,
            "deliverBy": min(now + 30, auth.expires_at)
            if isinstance(auth, AgentInvocation)
            else now + (OFFLINE_SEND_SECONDS if offline_send else 30),
            "requestHash": req_hash,
            **(
                {
                    "offlineIdentity": {
                        key: row[key] for key in ("deviceId", "provider", "profileFingerprint", "nativeConversationId")
                    }
                }
                if offline_send
                else {}
            ),
            **({"nativeHop": native_hop} if native_hop is not None else {}),
            **({"replacesRequestId": replaces_request_id} if replaces_request_id else {}),
            **(
                {
                    "sourceHumanUserId": auth.user_id,
                    "sourceSessionGeneration": source_generation,
                    "sourceTurnId": source_turn_id,
                }
                if source_session_id
                else {}
            ),
        }
        # Every remote control action is audited in the same transaction that admits it.
        audit = audit_row(owner, session_id, auth, _audit_action(request), request_id=request.request_id)
        if source_session_id:
            audit["sourceSessionId"] = source_session_id
        try:
            await repo.admit_command(
                row,
                enrollment,
                receipt,
                allow_offline=offline_send,
                extra=[
                    *(source_conditions or []),
                    *([retry_update] if retry_update else []),
                    {"Put": {"TableName": messaging_repo._table().name, "Item": audit}},
                ],
                source_pairing=source_pairing,
                room_meta_fenced_by_extra=room_meta_fenced_by_extra,
            )
            actor = getattr(auth, "user_id", None) or row["humanUserId"]
            origin = "native" if source_session_id else "direct"
            capture_session_event(
                row,
                "local_session_command_admitted",
                request.request_id,
                actor_user_id=actor,
                request_id=request.request_id,
                command_kind=request.kind,
                origin=origin,
                offline_queued=offline_send,
            )
            if offline_send:
                from core.services.local_session_relay import publish_resources_changed

                await publish_resources_changed(owner, row["humanUserId"])
            return _public(CommandReceipt, receipt)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry the same requestId.")


async def _expire_pending_command(owner: str, sid: str, row: dict) -> dict:
    if row["state"] != "pending" or row["deliverBy"] > int(time.time()):
        return row
    expired = {**row, "state": "rejected", "updatedAt": int(time.time()), "revision": str(uuid.uuid4())}
    try:
        await repo.replace_command(row, expired)
        if row.get("offlineIdentity"):
            await _capture_offline_terminal(owner, sid, expired, "expired")
        return expired
    except ClientError as exc:
        if not messaging_repo._is_cancelled(exc):
            raise
        return await repo.get_command(owner, sid, row["requestId"])


async def _capture_offline_terminal(
    owner: str, sid: str, command: dict, reason: Literal["cancelled", "expired", "rejected"]
) -> None:
    try:
        session = await repo.get_session(owner, sid)
        if session:
            capture_session_event(
                session,
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
    except Exception:
        logger.warning("offline command analytics capture failed", exc_info=True)


async def get_command(auth: Caller, room_id: str | None, session_id: str, request_id: str) -> CommandReceipt:
    room_id = validate_uuid(room_id, label="room ID") if room_id else ""
    # An agent keeps the requester-is-owner rule; a human reads receipts they may see (below).
    session, _ = await authorize_session(auth, room_id, session_id, owner_only=isinstance(auth, AgentInvocation))
    # Reuse the command identifier contract without accepting payload-owned identity.
    from pydantic import TypeAdapter, ValidationError

    try:
        TypeAdapter(OpaqueId).validate_python(request_id)
    except ValidationError as exc:
        raise ApiProblem("INVALID_REQUEST", "Invalid request ID.") from exc
    row = await repo.get_command(session["ownerId"], session["sessionId"], request_id)
    if row is None or row["roomId"] != room_id:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Command not found in this room.")
    # A receipt is its sender's and the session owner's to read -- a teammate who sent it under
    # "teammates can control" keeps it after the owner turns that off. Agents keep their room path.
    if not isinstance(auth, AgentInvocation) and auth.user_id not in {session["humanUserId"], row.get("actorUserId")}:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
    if room_id:
        await require_caller(auth, room_id)
    return _public(CommandReceipt, await _expire_pending_command(session["ownerId"], session["sessionId"], row))


async def list_offline_outbox(auth: AuthContext, session_id: str) -> OfflineOutbox:
    session, _ = await authorize_session(auth, None, session_id)
    owner, sid, now = session["ownerId"], session["sessionId"], int(time.time())
    commands = await repo.list_commands(owner, sid)
    by_request_id = {row["requestId"]: row for row in commands}
    cutoff = now - 2 * OFFLINE_SEND_SECONDS
    saved = {}
    for row in commands:
        if not row.get("offlineIdentity") or row.get("actorUserId") != auth.user_id:
            continue
        replacement = by_request_id.get(row.get("replacedByRequestId"))
        if row["createdAt"] < cutoff and not (
            replacement
            and replacement.get("replacesRequestId") == row["requestId"]
            and replacement.get("actorUserId") == auth.user_id
            and replacement["createdAt"] >= cutoff
        ):
            continue
        saved[row["requestId"]] = row
    linked: dict[str, dict] = {}
    for original in saved.values():
        if original["state"] != "rejected":
            continue
        replacement = by_request_id.get(original.get("replacedByRequestId"))
        if (
            replacement
            and replacement.get("replacesRequestId") == original["requestId"]
            and replacement.get("actorUserId") == auth.user_id
        ):
            if replacement["state"] == "pending" and replacement["deliverBy"] <= now:
                replacement = await _expire_pending_command(owner, sid, replacement)
                if replacement is None:
                    continue
                by_request_id[replacement["requestId"]] = replacement
            linked[replacement["requestId"]] = original
    visible = []
    for command in commands:
        row = by_request_id[command["requestId"]]
        if row["requestId"] not in saved and row["requestId"] not in linked:
            continue
        if row["state"] == "rejected" and row.get("replacesRequestId") in saved:
            continue  # a failed retry restores its saved original, never a second Retry action
        replacement_id = row.get("replacedByRequestId")
        if (
            row["state"] == "rejected"
            and replacement_id in linked
            and linked[replacement_id]["requestId"] == row["requestId"]
            and by_request_id[replacement_id]["state"] in {"pending", "dispatched", "unknown", "applied"}
        ):
            continue
        visible.append(row)
    rows = [await _expire_pending_command(owner, sid, row) for row in visible]
    active = [
        row
        for row in rows
        if row["state"] not in {"applied", "rejected"} and (row["deliverBy"] > now or row["requestId"] in linked)
    ]
    active_ids = {row["requestId"] for row in active}
    recent = [row for row in rows if row["requestId"] not in active_ids]
    selected = active[-OFFLINE_SEND_PENDING_LIMIT:]
    if len(selected) < OFFLINE_SEND_PENDING_LIMIT:
        selected += recent[-(OFFLINE_SEND_PENDING_LIMIT - len(selected)) :]
    return OfflineOutbox(
        commands=[_public(CommandReceipt, row) for row in sorted(selected, key=lambda row: row["commandSeq"])]
    )


async def cancel_offline_command(auth: AuthContext, session_id: str, request_id: str) -> CommandReceipt:
    receipt = await get_command(auth, None, session_id, request_id)
    if receipt.actor_user_id != auth.user_id:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the sender can cancel this follow-up.")
    session_id, request_id = receipt.session_id, receipt.request_id
    owner = resolve_owner_id(auth)
    session = await repo.get_session(owner, session_id)
    from core.services.local_session_relay import publish_resources_changed

    for _ in range(6):
        row = await repo.get_command(owner, session_id, request_id)
        if not row.get("offlineIdentity"):
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This follow-up cannot be cancelled.")
        if row.get("cancelledAt"):
            if session:
                await publish_resources_changed(owner, session["humanUserId"])
            return _public(CommandReceipt, row)
        if row["state"] != "pending" or row["deliverBy"] <= int(time.time()):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "This follow-up has already left the queue.")
        updated = {
            **row,
            "state": "rejected",
            "cancelledAt": int(time.time()),
            "updatedAt": int(time.time()),
            "revision": str(uuid.uuid4()),
        }
        audit = audit_row(owner, session_id, auth, "cancel", request_id=request_id)
        try:
            await repo.replace_command(
                row, updated, extra=[{"Put": {"TableName": messaging_repo._table().name, "Item": audit}}]
            )
            await _capture_offline_terminal(owner, session_id, updated, "cancelled")
            if session:
                await publish_resources_changed(owner, session["humanUserId"])
            return _public(CommandReceipt, updated)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "This follow-up changed. Check delivery.")


# --- Team control and the audit trail (owner ruling 2026-09-25) -----------------------------

AUDIT_ACTIONS = Literal[
    "display_title_changed",
    # The four native_send_* actions were the retired approval card; old audit rows still carry them.
    "native_send_requested",
    "native_send_approved",
    "native_send_denied",
    "native_send_cancelled",
    "send",
    "cancel",
    "interrupt",
    "approve",
    "deny",
    "team_control_on",
    "team_control_off",
    "team_control_ended",
    "share_link_created",
    "share_link_revoked",
    "pickup_on",
    "pickup_off",
    "picked_up",
    "pickup_expired",
    "join_requested",
    "join_allowed",
    "join_declined",
    "sender_removed",
    "resume_requested",
    "resume_revoked",
]


def _audit_action(request: LocalSessionCommand) -> str:
    if request.kind == "approve":
        return "approve" if request.decision == "allow" else "deny"
    return request.kind


def audit_row(
    owner: str,
    session_id: str,
    auth: Caller | None,
    action: str,
    *,
    request_id: str | None = None,
    detail: str | None = None,
    subject_user_id: str | None = None,
) -> dict:
    """One append-only audit item in the session's own partition. ``auth`` None is the system."""
    at = int(time.time() * 1000)
    row = {
        "pk": repo.session_pk(owner, session_id),
        "sk": f"AUDIT#{at:015d}#{uuid.uuid4()}",
        "at": at,
        "action": action,
    }
    if isinstance(auth, AgentInvocation):
        row["actorAgentId"] = auth.agent_id
    elif auth is not None:
        row["actorUserId"] = auth.user_id
    if request_id:
        row["requestId"] = request_id
    if detail:
        row["detail"] = detail[:200]
    if subject_user_id:
        row["subjectUserId"] = subject_user_id
    return row


class AuditEntry(ContractModel):
    at: str
    action: AUDIT_ACTIONS
    actor_user_id: str | None = None
    actor_agent_id: str | None = None
    request_id: str | None = None
    detail: str | None = None
    # The command's delivery state, when the entry is a command.
    state: str | None = None
    # The person an owner's decision was about (join_allowed, join_declined, sender_removed).
    subject_user_id: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(
            self,
            handler(self),
            info,
            ("actor_user_id", "actor_agent_id", "request_id", "detail", "state", "subject_user_id"),
        )


class AuditPage(ContractModel):
    entries: list[AuditEntry]


async def list_audit(auth: AuthContext, session_id: str) -> AuditPage:
    """Every remote control action on a session, oldest first; any workspace member reads it."""
    session, _ = await authorize_session(auth, None, session_id)
    owner, sid = session["ownerId"], session["sessionId"]
    # ponytail: whole-partition audit read; page it if a session ever collects thousands of actions.
    rows = await messaging_repo._query(repo.session_pk(owner, sid), begins_with="AUDIT#")
    states = {c["requestId"]: c["state"] for c in await repo.list_commands(owner, sid)}
    return AuditPage(
        entries=[
            AuditEntry(
                at=datetime.fromtimestamp(int(row["at"]) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
                action=row["action"],
                actor_user_id=row.get("actorUserId"),
                actor_agent_id=row.get("actorAgentId"),
                request_id=row.get("requestId"),
                detail=row.get("detail"),
                state=states.get(row.get("requestId")) if row.get("requestId") else None,
                subject_user_id=row.get("subjectUserId"),
            )
            for row in rows
        ]
    )


class TeamControlRequest(ContractModel):
    enabled: bool


async def set_team_control(auth: AuthContext, session_id: str, enabled: bool) -> LocalSession:
    """Only the session's own human flips it, and only while the session is running."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can change who controls it.")
        if row.get("endedAt") is not None:
            raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
        if bool(row.get("teamControl")) == enabled:
            return _session_public(row, auth.user_id)
        updated = {**row, "teamControl": enabled, "revision": str(uuid.uuid4())}
        if enabled:
            # Everyone may send now, so a pending ask is answered: it leaves Needs you and the session.
            updated["joinRequests"] = {
                who: r for who, r in (row.get("joinRequests") or {}).items() if r.get("state") != "pending"
            }
        audit = audit_row(owner, row["sessionId"], auth, "team_control_on" if enabled else "team_control_off")
        try:
            await repo.put_session(
                updated, row, enrollment, extra=[{"Put": {"TableName": messaging_repo._table().name, "Item": audit}}]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        capture_session_event(
            updated, "local_session_team_control_changed", updated["revision"], state="on" if enabled else "off"
        )
        return _session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


# --- Ask to join: one teammate at a time (owner ruling 2026-09-28) ----------------------------
#
# Per person, not the whole workspace. Both fields live on the session row, so every change is
# revision-fenced like team control. A relay reconnect re-registers the same generation and keeps
# them; a new process generation and the Ended frame drop them (local_session_relay).

JOIN_FIELDS = ("allowedSenders", "joinRequests")


class JoinDecisionRequest(ContractModel):
    decision: Literal["allow", "decline"]
    request_id: str | None = None


def _valid_user(user_id: str) -> str:
    from pydantic import TypeAdapter, ValidationError

    try:
        return TypeAdapter(OpaqueId).validate_python(user_id)
    except ValidationError as exc:
        raise ApiProblem("INVALID_REQUEST", "Invalid user ID.") from exc


async def _change_session(auth: AuthContext, session_id: str, change, audit_action: str, subject: str | None):
    """Load, let ``change`` return the new row (or None for "nothing to do"), write it with one
    audit row, and hint the workspace. ``change`` may raise to refuse."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if row.get("endedAt") is not None:
            raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
        updated = change(row)
        if updated is None:
            return row, None
        updated = {**updated, "revision": str(uuid.uuid4())}
        audit = audit_row(owner, row["sessionId"], auth, audit_action, subject_user_id=subject)
        try:
            await repo.put_session(
                updated, row, enrollment, extra=[{"Put": {"TableName": messaging_repo._table().name, "Item": audit}}]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return updated, audit
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def request_join(auth: AuthContext, session_id: str) -> LocalSession:
    """A teammate asks the owner to let them send. Asking again while pending changes nothing;
    a declined person may ask again."""
    me = auth.user_id

    def change(row: dict) -> dict | None:
        if me == row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This is your own session.")
        requests = dict(row.get("joinRequests") or {})
        if may_control(row, me) or (requests.get(me) or {}).get("state") == "pending":
            return None
        requests[me] = {"state": "pending", "requestId": str(uuid.uuid4()), "createdAt": utc_now_iso()}
        return {**row, "joinRequests": requests}

    row, audit = await _change_session(auth, session_id, change, "join_requested", None)
    if audit is not None:
        _capture_growth(row, "session_join_requested", me)
        await _notify_join_request(row, me)
    return _session_public(row, me)


async def decide_join(
    auth: AuthContext, session_id: str, user_id: str, decision: str, *, request_id: str | None = None
) -> LocalSession:
    """The owner answers one person's ask. Repeating the same answer is a no-op. ``request_id``
    pins the answer to the request the owner saw, so it never lands on a newer one."""
    user_id = _valid_user(user_id)
    answered: dict = {}

    def change(row: dict) -> dict | None:
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can answer a request to join.")
        requests = dict(row.get("joinRequests") or {})
        current = requests.get(user_id) or {}
        answered["requestId"] = current.get("requestId")
        if request_id is not None and current.get("requestId") != request_id:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That request was already answered.")
        if current.get("state") != "pending":
            if (decision == "allow" and user_id in allowed_senders(row)) or (
                decision == "decline" and current.get("state") == "declined"
            ):
                return None
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That request was already answered.")
        if decision == "allow":
            requests.pop(user_id)
            return {**row, "joinRequests": requests, "allowedSenders": [*allowed_senders(row), user_id]}
        requests[user_id] = {**current, "state": "declined"}
        return {**row, "joinRequests": requests}

    action = "join_allowed" if decision == "allow" else "join_declined"
    row, audit = await _change_session(auth, session_id, change, action, user_id)
    if audit is not None:  # only a write that landed; a repeated answer is a no-op
        _capture_growth(row, "session_join_decided", auth.user_id, decision=decision)
        await _notify_join_answer(row, user_id, action, answered["requestId"])
    return _session_public(row, auth.user_id)


async def remove_sender(auth: AuthContext, session_id: str, user_id: str) -> LocalSession:
    """The owner takes one person's access back; they may ask again."""
    user_id = _valid_user(user_id)

    def change(row: dict) -> dict | None:
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can change who controls it.")
        if user_id not in allowed_senders(row):
            return None
        return {**row, "allowedSenders": [u for u in allowed_senders(row) if u != user_id]}

    row, _ = await _change_session(auth, session_id, change, "sender_removed", user_id)
    return _session_public(row, auth.user_id)


def join_request_title(name: str, row: dict) -> str:
    from core.services import notification_copy as copy

    return f"{name} wants to join {copy.line(row.get('displayTitle') or row.get('title'), 40) or row['sessionId'][:8]}"


async def _notify_join_request(row: dict, requester: str) -> None:
    """The owner's "needs you" item, a live event and a push. Best effort, each on its own."""
    from core.services.local_session_relay import ACTIVITY_TTL_S
    from core.services.messaging import human_display_name

    owner, human, sid = row["ownerId"], row["humanUserId"], row["sessionId"]
    request = row["joinRequests"][requester]
    request_id, created = request["requestId"], request["createdAt"]
    try:
        name = await human_display_name(requester)
    except Exception:
        name = "A teammate"
    title = join_request_title(name, row)
    body = "Allow them to send follow-ups and answer prompts in this session."
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-join|{sid}|{request_id}"))
    try:
        await messaging_repo.put_activity_row(
            {
                "pk": messaging_repo.activity_pk(owner, human),
                "sk": messaging_repo.activity_sk(created, f"session:{event_id}"),
                "kind": "session",
                "subtype": "join_request",
                "sessionId": sid,
                "requesterId": requester,
                "requesterName": name,
                "requestId": request_id,
                "createdAt": created,
                "expiresAt": int(time.time()) + ACTIVITY_TTL_S,
            }
        )
    except Exception:
        logger.warning("Session join request activity row failed", exc_info=True)
    await _session_notice(owner, human, sid, "join_request", title, body, event_id)


async def _session_notice(owner: str, user: str, sid: str, state: str, title: str, body: str, event_id: str) -> None:
    """A live event on ``user``'s own channel and a push, each best effort on its own."""
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event
    from core.services.push_service import send_push_to_user

    push = {"title": title, "body": body, "data": {"source": "session", "sessionId": sid}}
    try:
        await get_live_event_publisher().publish_user(
            owner,
            user,
            make_live_event(
                "workspace.session.attention",
                {"sessionId": sid, "state": state, "push": push},
                event_id=event_id,
            ),
        )
    except Exception:
        logger.warning("Session %s live event failed", state)
    try:
        await send_push_to_user(user, title, body, push["data"])
    except Exception:
        logger.warning("Session %s push failed", state)


async def _notify_join_answer(row: dict, requester: str, state: str, request_id: str | None) -> None:
    """Tell the person who asked how the owner answered, once per decision."""
    from core.services import notification_copy as copy
    from core.services.messaging import human_display_name

    sid = row["sessionId"]
    try:
        name = await human_display_name(row["humanUserId"])
    except Exception:
        name = "The session's owner"
    session = copy.line(row.get("displayTitle") or row.get("title"), 40) or sid[:8]
    if state == "join_allowed":
        title, body = f"{name} let you into {session}", "You can send follow-ups and answer its prompts."
    else:
        title, body = f"{name} declined your request", session
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-join-answer|{sid}|{request_id}|{state}"))
    await _session_notice(row["ownerId"], requester, sid, state, title, body, event_id)


class UsageSharingRequest(ContractModel):
    enabled: bool


async def set_usage_sharing(auth: AuthContext, session_id: str, enabled: bool) -> LocalSession:
    """The owner decides whether teammates see this session's token meter."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can share its usage.")
        if bool(row.get("shareUsage")) == enabled:
            return _session_public(row, auth.user_id)
        updated = {**row, "shareUsage": enabled, "revision": str(uuid.uuid4())}
        try:
            await repo.put_session(updated, row, enrollment)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return _session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


# --- Share links: read-only transcript snapshots (owner ruling 2026-09-25) -------------------
#
# Workspace-only by design: a link opens for a signed-in member of the owner's workspace and
# nobody else. A public variant was judged too risky -- transcripts carry code, paths and
# command lines, and the desktop's credential redaction is a backstop, not a guarantee.

SHARE_DEFAULT_DAYS = 7
SHARE_MAX_DAYS = 30


def _share_pk(token_hash: str) -> str:
    return f"SHARELINK#{token_hash}"


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareLinkRequest(ContractModel):
    expires_in_days: int = Field(default=SHARE_DEFAULT_DAYS, ge=1, le=SHARE_MAX_DAYS)


class ShareLink(ContractModel):
    link_id: str
    created_at: str
    expires_at: str
    snapshot_seq: int
    # Only in the create response: the one time the secret exists outside the owner's clipboard.
    token: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("token",))


class ShareLinks(ContractModel):
    links: list[ShareLink]


class SharedSession(ContractModel):
    session_id: str
    human_user_id: str
    provider: Literal["claude", "codex"]
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    snapshot_seq: int
    created_at: str
    expires_at: str


def _share_public(row: dict, token: str | None = None) -> ShareLink:
    return ShareLink(
        link_id=row["linkId"],
        created_at=iso(row["createdAt"]),
        expires_at=iso(row["expiresAt"]),
        snapshot_seq=row["snapshotSeq"],
        token=token,
    )


async def _owned_session(auth: AuthContext, session_id: str, *, sharing: bool = True) -> dict:
    """The owner's own session. Listing and revoking skip the enrollment check on purpose: an
    owner who turned sharing off must still be able to find and kill their links."""
    if sharing:
        row, _ = await authorize_session(auth, None, session_id)
    else:
        row = await repo.get_session(resolve_owner_id(auth), validate_uuid(session_id, label="local session ID"))
        if row is None:
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can share it.")
    return row


def _capture_growth(row: dict, event: str, distinct_id: str, **props) -> None:
    """One PostHog row per sharing act, on the person who acted, naming the session's author.
    Content-free: ids and counts only, never a title, path, token or transcript line."""
    capture_product_event(
        distinct_id=distinct_id,
        event=event,
        properties={
            "workspace_id": row["ownerId"],
            "session_id": row["sessionId"],
            "provider": row["provider"],
            "author_user_id": row["humanUserId"],
            **props,
        },
    )


async def create_share_link(auth: AuthContext, session_id: str, request: ShareLinkRequest) -> ShareLink:
    import secrets

    row = await _owned_session(auth, session_id)
    owner, sid = row["ownerId"], row["sessionId"]
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    link = {
        "linkId": str(uuid.uuid4()),
        "tokenHash": _token_hash(token),
        "ownerId": owner,
        "sessionId": sid,
        "createdBy": auth.user_id,
        "createdAt": now,
        "expiresAt": now + request.expires_in_days * 86400,
        # The snapshot: what the transcript held when the owner shared it, and never more.
        "snapshotSeq": int(row.get("eventSeq", 0)),
    }
    table = messaging_repo._table().name
    audit = audit_row(owner, sid, auth, "share_link_created", detail=f"{request.expires_in_days}d")
    await messaging_repo._transact(
        [
            # The lookup row, found by the hash of the secret; expiresAt is the table's TTL.
            {"Put": {"TableName": table, "Item": {**link, "pk": _share_pk(link["tokenHash"]), "sk": "META"}}},
            # The owner's listing row, in the session's own partition.
            {
                "Put": {
                    "TableName": table,
                    "Item": {**link, "pk": repo.session_pk(owner, sid), "sk": f"SHARE#{link['linkId']}"},
                }
            },
            {"Put": {"TableName": table, "Item": audit}},
        ]
    )
    _capture_growth(row, "share_link_created", auth.user_id, expires_in_days=request.expires_in_days)
    return _share_public(link, token)


async def list_share_links(auth: AuthContext, session_id: str) -> ShareLinks:
    row = await _owned_session(auth, session_id, sharing=False)
    now = int(time.time())
    rows = await messaging_repo._query(repo.session_pk(row["ownerId"], row["sessionId"]), begins_with="SHARE#")
    return ShareLinks(links=[_share_public(r) for r in rows if int(r["expiresAt"]) > now])


async def revoke_share_link(auth: AuthContext, session_id: str, link_id: str) -> None:
    row = await _owned_session(auth, session_id, sharing=False)
    owner, sid = row["ownerId"], row["sessionId"]
    listing = await messaging_repo._get(repo.session_pk(owner, sid), f"SHARE#{validate_uuid(link_id, label='link ID')}")
    if listing is None:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Share link not found.")
    table = messaging_repo._table().name
    audit = audit_row(owner, sid, auth, "share_link_revoked")
    await messaging_repo._transact(
        [
            {"Delete": {"TableName": table, "Key": {"pk": _share_pk(listing["tokenHash"]), "sk": "META"}}},
            {"Delete": {"TableName": table, "Key": {"pk": listing["pk"], "sk": listing["sk"]}}},
            {"Put": {"TableName": table, "Item": audit}},
        ]
    )


async def _open_share(auth: AuthContext, token: str) -> tuple[dict, dict]:
    """A live link, for a member of the workspace that owns it, while its owner still shares
    from that Mac: turning sharing off closes the links with the live view. Every failure
    reads the same."""
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "This share link has expired or was revoked.")
    if not 16 <= len(token) <= 128:
        raise gone
    link = await messaging_repo._get(_share_pk(_token_hash(token)), "META")
    if link is None or int(link["expiresAt"]) <= int(time.time()) or caller_owner(auth) != link["ownerId"]:
        raise gone
    row = await repo.get_session(link["ownerId"], link["sessionId"])
    # A session its owner paused reads as gone to everyone else, its links included.
    if row is None or row.get("unshared"):
        raise gone
    try:
        await _enrolled(link["ownerId"], row["humanUserId"], row["deviceId"])
    except ApiProblem as exc:
        raise gone from exc
    return link, row


class SharedTokenRequest(ContractModel):
    """The token travels in a body, never a path or query: those land in access logs."""

    token: Annotated[str, Field(min_length=16, max_length=128)]
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


async def get_shared_session(auth: AuthContext, token: str) -> SharedSession:
    link, row = await _open_share(auth, token)
    # The open, not every page of events: one row per person per visit.
    _capture_growth(row, "share_link_opened", auth.user_id)
    return SharedSession(
        session_id=row["sessionId"],
        human_user_id=row["humanUserId"],
        provider=row["provider"],
        title=row.get("title"),
        cwd_name=row.get("cwdName"),
        snapshot_seq=link["snapshotSeq"],
        created_at=iso(link["createdAt"]),
        expires_at=iso(link["expiresAt"]),
    )


async def list_shared_events(
    auth: AuthContext, token: str, after_seq: int = 0, limit: int = 50
) -> LocalSessionEventPage:
    link, _ = await _open_share(auth, token)
    if after_seq < 0 or not 1 <= limit <= 100:
        raise ApiProblem("INVALID_REQUEST", "Invalid event pagination.")
    cap = int(link["snapshotSeq"])
    if after_seq >= cap:
        return LocalSessionEventPage(events=[], next_after_seq=None)
    rows, next_seq = await repo.list_events(link["ownerId"], link["sessionId"], after_seq, min(limit, cap - after_seq))
    rows = [r for r in rows if int(r["seq"]) <= cap]
    last = int(rows[-1]["seq"]) if rows else cap
    return LocalSessionEventPage(
        events=[_public(LocalSessionEvent, r) for r in rows], next_after_seq=last if last < cap and next_seq else None
    )
