"""Room-authorized local harness directory and durable command admission.

No native transport lives here. register_session is for a future verified machine
connection; its identity arguments must come from that connection, never its payload.
"""

from __future__ import annotations

import asyncio
import itertools
import json
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
from core.services.messaging import ApiProblem, ContractModel, require_room_member, room_agent, validate_uuid
from core.services.local_session_grants import AgentInvocation

STATUSES = Literal["idle", "busy", "waiting-approval", "unknown", "offline", "ended"]
# Why a running session has no live terminal, as its Mac says (spec 2026-10-07 §5.1).
TerminalReason = Literal["plain", "no-daemon", "app", "sdk", "unknown"]
# Why an ended session ended. "login-session-ended": the Mac found the holder older than its
# loginwindow (its provider lost DNS and the keychain for good while still reporting Idle).
# "idle-stopped": the Mac stopped a held session idle about an hour with nobody on it, as Claude
# Code does its background sessions; the conversation stays and it resumes.
EndedReason = Literal["exited", "process-exited", "on-purpose", "login-session-ended", "idle-stopped"]
# What a device may claim. `offline` is the backend's verdict on a lapsed lease and
# `ended` is written by the ended frame; neither is ever a status the device reports.
INPUT_STATUSES = Literal["idle", "busy", "waiting-approval", "unknown"]
# The coding agents a Mac mirrors (agent network §3). Cursor and Grok CLI have no session
# registry and no push channel: a session exists from its first hook, and a message reaches it
# at its next hook (its next tool call or turn end).
Provider = Literal["claude", "codex", "cursor", "grok"]
HOOKED_PROVIDERS = frozenset({"cursor", "grok"})
PROVIDER_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "grok": "Grok CLI"}


def iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


logger = logging.getLogger(__name__)

LIVE_STATUSES = frozenset(get_args(INPUT_STATUSES))
WORKSPACE_SESSION_CAP = 100


OpaqueId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
NetworkSessionId = Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9._-]{1,64}/[a-z]+:[A-Za-z0-9._-]{1,128}$")]
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
    provider: Provider
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
    # The engine's id for this session, so a client can join the network list to this one.
    network_session_id: NetworkSessionId | None = None
    start_id: OpaqueId | None = None


# Bounded well inside a DynamoDB number (38 digits) and a JS number's exact range.
TokenCount = Annotated[int, Field(default=0, ge=0, le=2**53)]


class TokenUsage(ContractModel):
    input_tokens: TokenCount = 0
    output_tokens: TokenCount = 0
    cache_read_tokens: TokenCount = 0
    cache_write_tokens: TokenCount = 0


Label = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\x21-\x7e]+$")]

Count = Annotated[int, Field(ge=0, le=1_000_000_000)]


class SessionChanges(ContractModel):
    """Files the session changed since it started, from git on its Mac (spec 2026-10-26 (f) gap 7)."""

    files: Count
    additions: Count
    deletions: Count


class SessionPr(ContractModel):
    """The pull request of the branch checked out in the session's folder (`gh pr view`, gap 15)."""

    number: Annotated[int, Field(ge=1, le=99_999_999)]
    state: Literal["open", "merged", "closed"]
    url: Annotated[str, Field(max_length=300, pattern=r"^https://[^\s]+$")]
    # Failing when any check failed, pending while any runs, passing; absent with no checks.
    checks: Literal["passing", "failing", "pending"] | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("checks",))


#: How many subagents a session has running (gap 16).
AgentCount = Annotated[int, Field(ge=1, le=1000)]


class SessionStats(ContractModel):
    """What the harness wrote about its own run; each field is absent until it does."""

    model: Label | None = None
    permission_mode: Label | None = None
    usage: TokenUsage | None = None


# Claude's session mode switches; "session" is Codex's "allow for this conversation"; "similar" is
# Claude's own suggested allow rule ("npm run *"), applied for the rest of the session only.
ApprovalMode = Literal["acceptEdits", "plan", "default", "session", "similar"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def _question_text(value: str, limit: int) -> str:
    if (
        not value.strip()
        or len(value.encode("utf-8")) > limit
        or any(ord(c) < 32 and c not in "\n\r\t" or 127 <= ord(c) <= 159 for c in value)
    ):
        raise ValueError("Question text must be visible and bounded")
    return value


class QuestionOption(ContractModel):
    label: str
    description: str | None = None

    @field_validator("label")
    @classmethod
    def label_text(cls, value):
        return _question_text(value, 500)

    @field_validator("description")
    @classmethod
    def description_text(cls, value):
        return _question_text(value, 2000) if value else value

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("description",))


class SessionQuestion(ContractModel):
    question: str
    header: str | None = None
    options: list[QuestionOption] = Field(min_length=1, max_length=9)
    multi_select: bool = Field(default=False, strict=True)

    @field_validator("question", "header")
    @classmethod
    def question_text(cls, value):
        return _question_text(value, 2000) if value is not None else value

    @model_validator(mode="after")
    def unique_options(self):
        if len({option.label for option in self.options}) != len(self.options):
            raise ValueError("Question options must be unique")
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("header",))


class PendingQuestion(ContractModel):
    prompt_id: OpaqueId
    input_digest: Digest
    questions: list[SessionQuestion] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({question.question for question in self.questions}) != len(self.questions):
            raise ValueError("Question texts must be unique")
        # Native bounds the original input to 10 KiB. Projection adds bounded IDs,
        # digest and default fields; it must not reject an input already held by the hook.
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > 10 * 1024 + 1024:
            raise ValueError("Question descriptor exceeds its bounded envelope")
        largest = {}
        for question in self.questions:
            selectable = (
                ", ".join(option.label for option in question.options)
                if question.multi_select
                else max(
                    (option.label for option in question.options),
                    key=lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
                )
            )
            largest[question.question] = max(
                (selectable, "x" * 128), key=lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            )
        if len(json.dumps(largest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 2000:
            raise ValueError("Question cannot fit its answer envelope")
        return self


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
    modes: list[ApprovalMode] = Field(default_factory=list, max_length=4)
    # What "similar" allows, as the approver must see it before choosing it.
    similar: Annotated[str, Field(max_length=120)] | None = None
    # ``exact`` is never widened (old clients gate Allow on it alone). ``full`` says ``input`` IS
    # the raw call (uncut, unredacted; ``input_digest`` is its digest), so a viewer who was shown
    # all of it may allow -- naming that digest as ``shownDigest``. The 16 KiB envelope caps it.
    full: bool = False
    input: dict[str, Any] | None = None

    @model_validator(mode="after")
    def full_needs_input(self):
        # A viewer cannot have been shown a call the prompt does not carry: it stays deny-only.
        if not self.input:
            self.full = False
        # Nobody can choose "similar" without being shown what it allows.
        if not self.similar:
            self.modes = [mode for mode in self.modes if mode != "similar"]
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("input", "similar"))


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
    private_repo: bool | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(
            self,
            handler(self),
            info,
            ("state", "reason", "snapshot_at", "bytes", "has_code", "repo", "branch", "expires_at", "private_repo"),
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
    # Why the Mac refused: ALREADY_RUNNING, CONTINUED, FOLDER_MISSING, FOLDER_NOT_APPROVED, FOLDER_NOT_TRUSTED,
    # TOOL_UNAVAILABLE, SHARING_OFF, LAUNCH_FAILED.
    reason: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("reason",))


#: The Mac's feature for the panel size on /input (`session_screen.SessionInput.size`).
REMOTE_RESIZE_FEATURE = "remoteResize"


class LocalSession(ContractModel):
    workspace: WorkspaceSummary = Field(default_factory=WorkspaceSummary)
    session_id: str
    device_id: str
    human_user_id: str
    provider: Provider
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
    ended_reason: EndedReason | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
    # Only its own human sees the exact remote start that opened this native run.
    start_id: OpaqueId | None = None
    # A Claude Code background job: agent knowledge, never a human listing.
    kind: Literal["interactive", "background"] = "interactive"
    # Which live terminal a running session has: "held" (a Lightbulb holder runs it) or "attach"
    # (a Claude background session the Mac attaches to). Absent: its transcript is all there is.
    terminal: Literal["held", "attach"] | None = None
    # Why a running session has no live terminal (spec 2026-10-07 §4 f, g); absent when it has one.
    terminal_reason: TerminalReason | None = None
    # An ended held session whose final screen its owner's Mac still shows (spec §5.4); `terminal`
    # stays set while it is. Absent otherwise.
    screen_available: bool | None = None
    # An ended session that was held: its kept screen may still be reported (the Mac does that on
    # its first heartbeat after the provider exits), so a viewer waits before saying it is gone.
    was_held: bool | None = None
    # Its person ended it on purpose (End, the provider's own exit): no list shows it, a read by id
    # still does, for a panel that was open on it. Absent otherwise.
    ended_on_purpose: bool | None = None
    # The engine's id for this session, so a client can join the network list to this one.
    network_session_id: NetworkSessionId | None = None
    # The session this one continues: a Claude session moved to the background (<-) is a fork.
    forked_from: OpaqueId | None = None
    # A live session only, from a Mac that reports them (`sessionAttention`): a background job whose
    # turn is over, why a prompt is open in its terminal, and a job's hand-off note (a message for
    # its person, never a prompt). Absent otherwise.
    done: bool | None = None
    waiting_for: Annotated[str, Field(max_length=120)] | None = None
    note: Annotated[str, Field(max_length=300)] | None = None
    # A live session only, from a Mac that reports them (`sessionSignals`): files changed since it
    # started (git), its running subagents, its branch's pull request. Absent otherwise.
    changes: SessionChanges | None = None
    agents: AgentCount | None = None
    pr: SessionPr | None = None
    # False while the owner has paused sharing this one session: only they can see it.
    shared: bool = True
    # Where this running session's browser connection is open now: the Browser
    # tab beside its terminal shows while it is set. None: no open connection.
    browser: Literal["mac", "cloud"] | None = None
    # Owner opt-in: workspace members may use this session's controls (off by default, cleared on end).
    team_control: bool = False
    # People the owner let in one by one ("Ask to join"): they may use the controls too. Cleared on end.
    allowed_senders: list[str] = Field(default_factory=list)
    # The READER's own ask to join, while it is pending or after it was declined; absent otherwise.
    join_request: Literal["pending", "declined"] | None = None
    # Which request that is, so a client can tell a newer answer from its own older reply.
    join_request_id: str | None = None
    # When the reader last asked (the ask, or their latest "Ask again"): the client offers Ask again
    # ASK_AGAIN_AFTER_S after it.
    join_requested_at: str | None = None
    # The READER's own ask was dropped unanswered when the session ended or restarted; absent once
    # they ask again, are let in, or JOIN_CLOSED_TTL_S passes.
    join_request_closed: Literal["restarted"] | None = None
    pickup_requested_at: str | None = None
    # The owner's own view only: who is waiting for an answer, so the session itself can offer Allow/Decline.
    pending_joins: list[str] | None = None
    # The owner's own view only: names for the people asking from the public page, who are not in the
    # workspace yet ("Priya Shah (priya@acme.com)"), so the owner knows who they would let in.
    pending_join_names: dict[str, str] | None = None
    # Request id per waiting person, so an answer from the session names the request it saw.
    pending_join_ids: dict[str, str] | None = None
    # The READER's own ask to pick up, while pick-up is off; absent otherwise.
    pickup_request: Literal["pending", "declined"] | None = None
    # The owner's own view only: who is waiting for pick-up, and under which request id.
    pending_pickups: list[str] | None = None
    pending_pickup_ids: dict[str, str] | None = None
    pending_approval: PendingApproval | None = None
    pending_question: PendingQuestion | None = None
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
    # A request is pending and the owner's older Mac (no bandOwnerOnly) drops remote typing
    # until it is answered.
    input_paused: bool = False
    # The live terminal takes the viewer's panel size on /input (owner ruling 2026-10-14: the session
    # takes the size of whoever used it last): the session's Mac advertised remoteResize and its own
    # holder said it takes the size (an older holder does not). A web view sends a size only then.
    terminal_resize: bool = False
    # The viewer's seat in this session's chat, once they are in it (PR #3807).
    chat_room_id: str | None = None
    chat_unread: int | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(
            self,
            handler(self),
            info,
            (
                "pending_approval",
                "pending_question",
                "start_id",
                "terminal",
                "terminal_reason",
                "screen_available",
                "was_held",
                "ended_on_purpose",
                "forked_from",
                "network_session_id",
                "done",
                "waiting_for",
                "note",
                "changes",
                "agents",
                "pr",
                "model",
                "display_title",
                "permission_mode",
                "usage",
                "pickup",
                "picked_up_from",
                "resume",
                "join_request",
                "join_request_id",
                "join_requested_at",
                "join_request_closed",
                "pickup_requested_at",
                "pending_joins",
                "pending_join_names",
                "pending_join_ids",
                "pickup_request",
                "pending_pickups",
                "pending_pickup_ids",
                "chat_room_id",
                "chat_unread",
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
_APPROVAL_FIELDS = ("approval_id", "decision", "mode", "input_digest", "shown_digest")
_QUESTION_FIELDS = ("prompt_id", "answers")
# A current held hook supplies its own process/prompt proof. The native answer adapter
# rechecks that proof even when history replay or ordinary engine discovery is incomplete.
_QUESTION_RECOVERABLE_REASONS = (None, "history-gap", "runtime-unverified")


class LocalSessionCommand(ContractModel):
    request_id: OpaqueId
    expected_generation: OpaqueId
    kind: Literal["send", "interrupt", "approve", "answer", "end"]
    text: str | None = Field(default=None, max_length=MAX_COMMAND_BYTES)
    expected_turn_id: OpaqueId | None = None
    approval_id: OpaqueId | None = None
    decision: Literal["allow", "deny"] | None = None
    mode: ApprovalMode | None = None
    input_digest: Digest | None = None
    # The client's digest of the whole call it displayed. Kept off the wire and off the stored
    # command's public keys (it is stored as ``approvalShownDigest``): the Mac's Command is
    # deny_unknown_fields.
    shown_digest: Digest | None = None
    prompt_id: OpaqueId | None = None
    answers: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_command(self):
        if self.kind == "send" and (self.text is None or not self.text.strip()):
            raise ValueError("send requires text")
        if self.kind == "interrupt" and self.text is not None:
            raise ValueError("interrupt does not accept text")
        if self.kind == "end" and (self.text is not None or self.expected_turn_id is not None):
            raise ValueError("end takes no text and no turn")
        approval = [getattr(self, name) is not None for name in _APPROVAL_FIELDS]
        if self.kind == "approve":
            if not (self.approval_id and self.decision and self.input_digest) or self.expected_turn_id:
                raise ValueError("approve requires approvalId, decision and inputDigest")
            if self.mode is not None and self.decision != "allow":
                raise ValueError("a mode switch only accompanies allow")
            if self.text is not None and (self.decision != "deny" or len(self.text.encode("utf-8")) > 500):
                raise ValueError("only a deny carries a short message")
        elif self.kind == "answer":
            if not self.prompt_id or not self.input_digest or not self.answers or len(self.answers) > 4:
                raise ValueError("answer requires promptId, inputDigest and one to four answers")
            if any(
                getattr(self, key) is not None
                for key in ("approval_id", "decision", "mode", "shown_digest", "text", "expected_turn_id")
            ):
                raise ValueError("answer takes no approval, text or turn fields")
            for key, value in self.answers.items():
                _question_text(key, 2000)
                _question_text(value, 2000)
            if len(json.dumps(self.answers, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 2000:
                raise ValueError("answers exceed 2000 UTF-8 bytes")
        elif any(approval):
            raise ValueError("approval fields belong to approve")
        if self.kind != "answer" and any(getattr(self, name) is not None for name in _QUESTION_FIELDS):
            raise ValueError("question fields belong to answer")
        if (
            len(self.model_dump_json(by_alias=True, include=set(LocalSessionCommand.model_fields)).encode("utf-8"))
            > MAX_COMMAND_BYTES
        ):
            raise ValueError("command exceeds 8 KiB")
        return self

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, (*_APPROVAL_FIELDS, *_QUESTION_FIELDS))


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
    # ``awaiting_approval``: a person's session send held until the session's owner allows them.
    state: Literal["awaiting_approval", "pending", "dispatched", "applied", "rejected", "unknown"]
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
            (
                "actor_user_id",
                "actor",
                "invocation_expires_at",
                "cancelled_at",
                "native_hop",
                *_APPROVAL_FIELDS,
                *_QUESTION_FIELDS,
            ),
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
        or meta.get("deletedAt")  # a deleted room keeps its member rows; its turns act for no one
        or meta.get("ownerId") != owner
        or await room_agent(room, auth.agent_id, meta) is None  # the feed: a roster author
        # An agent answers everyone in the room, and a shared room's foreign
        # members must never hear this workspace's sessions.
        or await messaging_repo.has_foreign_members(room, owner)
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")


def command_caller(owner: str, command: dict) -> AuthContext:
    """The person who admitted ``command``. Only people command a session; a row an agent or a
    session admitted before they left the hub is refused at delivery."""
    if "actor" in command:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only a person can command a session.")
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


def conversation_only(row: dict) -> bool:
    """A session that never has a terminal (it runs inside an app, an IDE or an SDK; a Mac older
    than 2026-10-08 said `unknown`). The clients' `conversationOnly`. There is no public page for
    a conversation, so it is never "Anyone with the link"."""
    return session_terminal(row) is None and row.get("terminalReason") in ("app", "sdk", "unknown")


def screen_available(row: dict, now: int | None = None) -> bool:
    """An ended held session whose holder still shows its final screen (spec 2026-10-07 §5.4). The
    Mac says so on the end and renews `screenUntil` with every heartbeat while it lasts; it lapses
    when the app quits, relaunches or closes that holder."""
    if row.get("endedAt") is None or session_terminal(row) != "held":
        return False
    return int(row.get("screenUntil") or 0) > (int(time.time()) if now is None else now)


def browser_open(row: dict, now: int | None = None) -> Literal["mac", "cloud"] | None:
    """Where the session's browser connection is open, while session_browser keeps renewing it."""
    opened = row.get("browserOpen") or {}
    if int(opened.get("until") or 0) <= (int(time.time()) if now is None else now):
        return None
    return opened.get("where") if opened.get("where") in ("mac", "cloud") else None


def session_activity(old_status: str | None, new_status: str, old_turn: str | None, new_turn: str | None) -> bool:
    """Whether a change of status is the session doing something: a turn starting or ending. Not
    the Mac becoming unsure ("unknown", after every restart) or losing it ("offline"), and not the
    restart's re-check settling "unknown" back to "idle". "unknown" -> "busy" is a real turn."""
    if old_turn != new_turn:
        return True
    if old_status == new_status or new_status in ("unknown", "offline"):
        return False
    return not (old_status == "unknown" and new_status == "idle")


def reconnect_last_event_at(
    previous: dict | None, generation: str, status: str, active_turn_id: str | None
) -> int | None:
    """A reconnect re-registers every session of its Mac, and that is not activity: the same live
    run keeps the time it last did something, unless the registration itself carries a turn
    starting or ending. A new run (a resume, a new process) is activity."""
    if (
        previous
        and previous.get("generation") == generation
        and previous.get("endedAt") is None
        and not session_activity(previous.get("status"), status, previous.get("activeTurnId"), active_turn_id)
    ):
        return previous.get("lastEventAt")
    return None


def ended_empty(provider: str, status: str, event_seq: int, control_unavailable_reason: str | None) -> bool:
    """A Claude Code session that ended before its first message: Claude writes a transcript only
    on that message, so there is nothing to show or resume (`claude --resume` answers "No
    conversation found"). Not Codex: its rollout holds the thread before any event is public, so
    `codex resume` can still find it. A row whose history the Mac could not read
    (`controlUnavailableReason`) may hold a real conversation: not empty."""
    return provider == "claude" and status == "ended" and event_seq == 0 and control_unavailable_reason is None


#: `endedReason` of a session its person ended on purpose. The relay writes it (`_ended`).
ENDED_ON_PURPOSE = "on-purpose"


async def landed_ends(owner: str, row: dict) -> list[dict]:
    """The owner's End commands for this run that reached its Mac: sent there, and neither refused
    nor left unanswered (`unknown`) by it. Any one makes the end on purpose; one that
    `remove_session` sent (`removesOnEnd`) makes the end a removal too."""
    return [
        command
        for command in await repo.list_commands(owner, row["sessionId"])
        if command.get("kind") == "end"
        and command.get("expectedGeneration") == row["generation"]
        and command.get("state") in {"dispatched", "applied"}
    ]


def owner_auth(row: dict) -> AuthContext:
    """The session's own human, for work the relay finishes in their name."""
    owner, user = row["ownerId"], row["humanUserId"]
    return AuthContext(user_id=user, org_id=owner if owner != user else None)


def _without_moved(found: list[LocalSession]) -> list[LocalSession]:
    """A stopped session that a later one of the same person continues (`forkedFrom`) is listed once:
    as that later one. Offline counts as stopped: a Mac still draining an ended origin re-registers it
    and then lets it lapse. A running origin is never hidden: `claude --bg --resume` copies a running
    session.

    A fork that ended before its first turn is the exception: Claude writes the copied history into
    the fork's transcript only on that turn, so the fork has no events and `claude --resume` cannot
    find it. It is hidden instead, and its origin (which holds the history) stays listed.

    Any other session that ended before its first message (`ended_empty`) is not listed either; it
    stays readable by id, for a panel that is open on it. Not one that had a live terminal
    (`wasHeld`): a held session's first events take 40-60 s to reach us, so one ended in its first
    minute reads as zero events while people were watching real work in it (prod 2026-10-08). The
    two cannot be told apart from here, so a held session is listed; its owner can remove it.

    A session its person ended on purpose (`endedOnPurpose`: End, the provider's own exit) stays
    listed until its owner removes it (owner ruling 2026-10-27, superseding 2026-10-08's hide:
    ended work vanished from Recent minutes after End)."""
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
        and not (ended_empty(s.provider, s.status, s.latest_event_seq, s.control_unavailable_reason) and not s.was_held)
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
    screen = public["status"] == "ended" and screen_available(row)
    public["terminal"] = session_terminal(row) if public["status"] not in ("ended", "offline") or screen else None
    if screen:
        public["screenAvailable"] = True
    if public["status"] == "ended" and session_terminal(row) == "held":
        public["wasHeld"] = True
    if public["status"] == "ended" and row.get("endedReason") == ENDED_ON_PURPOSE:
        public["endedOnPurpose"] = True
    # Re-registering pops endedReason (relay); a row that is not ended never shows a stale one.
    if row.get("endedAt") is None:
        public.pop("endedReason", None)
    reason = public.pop("terminalReason", None)
    # A session inside an app never had a terminal: ended or offline, its panel is still its conversation.
    live = public["status"] not in ("ended", "offline")
    public["browser"] = browser_open(row) if live else None
    if not live:
        for key in ("done", "waitingFor", "note", "changes", "agents", "pr"):
            public.pop(key, None)
    if public["terminal"] is None and reason and (live or reason not in ("plain", "no-daemon")):
        public["terminalReason"] = reason
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
        public["joinRequestedAt"] = _asked_at(request)
    elif viewer and row.get("endedAt") is None and viewer in closed_joins(row) and not may_control(row, viewer):
        public["joinRequestClosed"] = "restarted"
    if viewer and viewer == row["humanUserId"] and row.get("endedAt") is None:
        waiting = {
            who: r["requestId"] for who, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"
        }
        if waiting:
            public["pendingJoins"] = sorted(waiting)
            public["pendingJoinIds"] = waiting
            names = {who: r["name"] for who, r in row["joinRequests"].items() if who in waiting and r.get("name")}
            if names:
                public["pendingJoinNames"] = names
    from core.services import session_pickup  # lazy: it imports this module

    lapsed = session_pickup._expired(row)  # an expired snapshot leaves nothing to ask for or allow
    pick = (row.get("pickupRequests") or {}).get(viewer) if viewer else None
    if pick and not row.get("pickupAllowed") and not lapsed:
        public["pickupRequest"] = pick["state"]
        public["pickupRequestedAt"] = _asked_at(pick)
    if viewer and viewer == row["humanUserId"] and not lapsed:
        waiting = pending_pickups(row)
        if waiting:
            public["pendingPickups"] = sorted(waiting)
            public["pendingPickupIds"] = waiting
    from core.services import session_presence  # lazy: it imports this module

    features = row.get("deviceFeatures", ())
    # Only an older presence Mac freezes; one that advertises bandOwnerOnly never does.
    public["inputPaused"] = bool(
        (session_presence.pending_joins(row) or session_presence.shown_pickups(row))
        and session_presence.FEATURE in features
        and session_presence.BAND_OWNER_ONLY_FEATURE not in features
        and public["terminal"] is not None
    )
    public["terminalResize"] = (
        REMOTE_RESIZE_FEATURE in features and bool(row.get("terminalResize")) and public["terminal"] is not None
    )
    stats = row.get("stats") or {}
    for key in ("model", "permissionMode"):
        if stats.get(key):
            public[key] = stats[key]
    public["shareUsage"] = bool(row.get("shareUsage"))
    if stats.get("usage") and viewer is not None and (viewer == row["humanUserId"] or public["shareUsage"]):
        public["usage"] = stats["usage"]
    # Permission decisions still require ordinary verified control. A held question has
    # its own fenced response route below, so it need not depend on ordinary send support.
    if public["status"] != "waiting-approval" or not row["capabilitiesVerified"] or row.get("controlUnavailableReason"):
        public.pop("pendingApproval", None)
    # The exact held question is its own response capability, independent of ordinary sends.
    if (
        public["status"] != "waiting-approval"
        or row.get("controlUnavailableReason") not in _QUESTION_RECOVERABLE_REASONS
    ):
        public.pop("pendingQuestion", None)
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
        "privateRepo": True if "privateFolder" in snapshot else None,
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


def pickup_expired(row: dict) -> bool:
    """True once the session's snapshot is past its retention: one predicate for opening and sharing a link."""
    stopped = pickup_stopped_at(row)
    return stopped is not None and int(time.time()) >= stopped + PICKUP_RETENTION_SECONDS


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
                    # The session's open browser connection and its chosen place (session_browser).
                    "browserOpen",
                    "browserPlace",
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
            "lastEventAt": reconnect_last_event_at(
                previous, registration.generation, registration.status, registration.active_turn_id
            )
            or int(time.time()),
        }
        if registration.network_session_id is None:
            row.pop("networkSessionId", None)
            if previous and previous.get("networkSessionId"):
                row["networkSessionId"] = previous["networkSessionId"]
        if registration.start_id is None and previous and previous.get("generation") == registration.generation:
            row["startId"] = previous.get("startId")
        elif registration.start_id is None:
            row.pop("startId", None)
        if previous and previous.get("generation") == registration.generation:
            # A reconnect keeps the people the owner let in; a new process (or the end) does not.
            row.update({key: previous[key] for key in (*JOIN_FIELDS, "joinClosed") if key in previous})
        elif previous and (closed := closed_joins(previous)):
            row["joinClosed"] = closed  # the people whose open ask the restart dropped are told so
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
    agent = isinstance(auth, AgentInvocation)
    if room_id:
        room_id = validate_uuid(room_id, label="room ID")
        await require_caller(auth, room_id)
    elif agent and (auth.room_id or auth.expires_at <= int(time.time())):
        # A room grant always names its room; only a Paperclip run's grant is roomless.
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")
    session_id = validate_uuid(session_id, label="local session ID")
    row = await repo.get_session(owner, session_id)
    if row is None or row.get("ownerId") != owner or row.get("removedAt") is not None:
        # Removed by its owner (`remove_session`): gone for everyone, its owner included.
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if row.get("unshared") and (agent or auth.user_id != row["humanUserId"]):
        # A session its owner stopped sharing reads as gone to everyone else, agents included.
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if agent and not ((await messaging_repo.get_user_stream(owner, row["humanUserId"])) or {}).get("active"):
        # Agents reach sessions across the workspace, not through the owner's room seat, so a
        # removed member's leftover rows must read as gone here (their stream is deactivated).
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if agent and not room_id and not await _reachable_from_background(row["humanUserId"]):
        # Spec 2026-10-07-agent-session-approval: background work reaches only the sessions of
        # people who turned that on; for anyone else it reads as gone, like an unshared session.
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if room_id and not agent and await messaging_repo.get_member(room_id, "human", row["humanUserId"]) is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Session owner is no longer in this room.")
    if owner_only and (agent or not room_id):
        # Owner ruling 2026-09-25: a workspace member may control it while its owner has turned
        # on "Let teammates control this session". Agents never ride that opt-in.
        # Owner ruling 2026-09-28: or a person the owner allowed after they asked to join.
        # Agents never control a session: they message it through the engine, as anyone does.
        if agent or not may_control(row, auth.user_id):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
    return row, await _enrolled(owner, row["humanUserId"], row["deviceId"])


async def _reachable_from_background(human: str) -> bool:
    from core.repositories import user_repo

    return user_repo.sessions_reachable_from_background_work(await user_repo.get(human))


def live_for_input(session: LocalSession) -> bool:
    """A live, verified session that takes input at all; who may send is a separate question."""
    return (
        session.status in LIVE_STATUSES
        and session.capabilities_verified
        and session.control_unavailable_reason is None
        and session.capabilities.send_mode != "none"
    )


async def list_resources(auth: Caller, room_id: str | None, *, include_ended: bool = False) -> RoomResources:
    owner = caller_owner(auth)
    agent = isinstance(auth, AgentInvocation)
    if room_id or not agent:
        room_id = validate_uuid(room_id, label="room ID")
        await require_caller(auth, room_id)
    elif auth.room_id or auth.expires_at <= int(time.time()):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")
    devices, sessions, paired_nodes = [], [], []
    # ponytail: bounded room membership, per-human directory reads; batch only if measured slow.
    # Devices and pairings stay the room's; a Paperclip run has no room and sees none.
    for member in await messaging_repo.list_members(room_id) if room_id else []:
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
        for projection in [] if agent else await repo.list_user_sessions(owner, human):
            try:
                row, _ = await authorize_session(auth, room_id, projection["sessionId"])
                public = _session_public(row, _viewer(auth))
                if public.kind == "interactive":
                    sessions.append(public)
            except ApiProblem as exc:
                if exc.error.code not in {"LOCAL_SESSION_FORBIDDEN", "LOCAL_SESSION_NOT_FOUND"}:
                    raise
    if agent:
        sessions = await _agent_sessions(auth, include_ended=include_ended)
    sessions = _without_moved(sessions)
    live = sorted((s for s in sessions if s.status != "ended"), key=lambda s: s.last_event_at or "", reverse=True)
    if include_ended:
        recent = sorted(
            (s for s in sessions if s.status == "ended"), key=lambda s: s.last_event_at or "", reverse=True
        )[:20]
        sessions = live + recent
    else:
        sessions = live
    if room_id:
        await require_caller(auth, room_id)
    return RoomResources(devices=devices, sessions=sessions, paired_nodes=paired_nodes)


async def _agent_sessions(auth: AgentInvocation, *, include_ended: bool) -> list[LocalSession]:
    """Every shared session in the workspace (spec 2026-10-07-agent-session-approval §2.2), background
    jobs included, for an agent to read. A Paperclip run sees only the sessions of people who let
    background work reach them."""
    from core.repositories import user_repo

    owner = auth.owner_id
    humans = [row["userId"] for row in await messaging_repo.list_active_user_streams(owner)]
    # ponytail: one users-table read per workspace member per listing; batch if workspaces grow large.
    prefs = dict(zip(humans, await asyncio.gather(*(user_repo.get(human) for human in humans))))
    if not auth.room_id:
        humans = [human for human in humans if user_repo.sessions_reachable_from_background_work(prefs[human])]
    since = int(time.time()) - RECENT_ENDED_SECONDS if include_ended else None
    found = await _live_public_sessions(owner, humans, include_offline=True, ended_since=since)
    return [session for session in found if session.shared]


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
    # One batched read per 100 kept rows, not one GetItem each: 314 of those per listing queued
    # on the thread pool every DynamoDB call shares and stalled the relay (prod 2026-10-07).
    # ponytail: still every historical row; an owner-keyed live index if history grows large.
    rows = iter(await repo.get_sessions(owner, [p["sessionId"] for human_rows in projections for p in human_rows]))
    for human, human_rows in zip(humans, projections):
        for row in itertools.islice(rows, len(human_rows)):
            if row is None or row.get("ownerId") != owner or row.get("humanUserId") != human:
                continue
            if row.get("removedAt") is not None:
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
    sessions = live + [s for s in found if s.status == "ended"][:RECENT_ENDED_CAP]
    if viewer is not None and sessions:
        from core.services import session_chat  # lazy: it imports this module

        sessions = session_chat.with_chat(
            sessions, await session_chat.chat_state(owner, viewer, [s.session_id for s in sessions])
        )
    return WorkspaceSessions(sessions=sessions)


async def searchable_sessions(auth: AuthContext) -> dict[str, LocalSession]:
    """The sessions whose words this person may search: what the sessions list shows them
    (their own in any sharing state, everyone else's while shared; not one continued by a fork),
    without the list's 7-day window on ended ones."""
    owner = resolve_owner_id(auth)
    if not await still_in_workspace(owner, auth.user_id):
        return {}
    humans = [row["userId"] for row in await messaging_repo.list_active_user_streams(owner)]
    # ponytail: every historical row, like the list; an owner-keyed index if history grows large.
    found = await _live_public_sessions(owner, humans, include_offline=True, ended_since=0, viewer=auth.user_id)
    return {
        s.session_id: s
        for s in _without_moved(
            [s for s in found if s.kind == "interactive" and (s.shared or s.human_user_id == auth.user_id)]
        )
    }


async def still_in_workspace(owner: str, user_id: str) -> bool:
    """The caller is still a member: a removal deactivates their stream (strong read), while
    their Clerk JWT may stay valid for minutes."""
    return bool(((await messaging_repo.get_user_stream(owner, user_id)) or {}).get("active"))


def still_searchable(row: dict | None, user_id: str) -> bool:
    """Re-check after the index answered: sharing paused or the session removed meanwhile."""
    return (
        row is not None
        and row.get("removedAt") is None
        and (not row.get("unshared") or row.get("humanUserId") == user_id)
    )


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
                or row.get("removedAt") is not None
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


async def set_sharing(
    auth: AuthContext, session_id: str, shared: bool, *, generation: str | None = None
) -> LocalSession:
    """The owner pauses or resumes sharing one session. While paused the relay drops its
    transcript instead of storing it (one notice marks the gap) and nobody else can see it.
    ``generation`` pins the change to the run the caller saw (the Mac's terminal decisions):
    checked on the row this write is conditioned on, so a resumed session is never changed."""
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id, owner_only=True)
        # owner_only admits teammates while "Let teammates control" is on; sharing stays the owner's.
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
        if generation is not None and row.get("generation") != generation:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not this Mac's current session.")
        if bool(row.get("unshared")) == shared:  # a change, not a no-op
            updated = {**row, "revision": str(uuid.uuid4())}
            if shared:
                updated.pop("unshared", None)
                updated.pop("pauseNoticed", None)
            else:
                updated["unshared"] = True
                if public_id(row.get("shareLink")):
                    # Only me: an "Anyone" link does not come back public when sharing resumes.
                    updated["shareLink"] = _with_audience(row["shareLink"], "workspace")
                updated.pop("pendingApproval", None)
                updated.pop("pendingQuestion", None)
                updated.pop("privateSendApproval", None)
            try:
                await repo.put_session(updated, row, enrollment)
            except ClientError as exc:
                if not messaging_repo._is_cancelled(exc):
                    raise
                continue
            row = updated
            await _push_presence(row)
        if not shared:
            from core.services import session_chat  # lazy: it imports this module

            # On every pause, not only a change: a retry after a failed silence finishes it.
            await session_chat.silence(row["ownerId"], row["sessionId"], keep=row["humanUserId"])
        return _session_public(row, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def get_session(auth: Caller, session_id: str) -> LocalSession:
    """One session for any workspace member, ended included: the list keeps only live
    ones, but an open view or an old link must still show a session that has ended."""
    row, _ = await authorize_session(auth, None, session_id)
    viewer = _viewer(auth)
    public = _session_public(row, viewer)
    if viewer is None:
        return public
    from core.services import session_chat  # lazy: it imports this module

    state = await session_chat.chat_state(row["ownerId"], viewer, [row["sessionId"]])
    return session_chat.with_chat([public], state)[0]


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
        from core.services import session_chat  # lazy: it imports this module

        try:  # the chat is named after the session: an existing one takes the new title now
            await session_chat.follow_session(owner, row["sessionId"])
        except Exception:  # the next chat open names it too (session_chat._follow_lifecycle)
            logger.warning("session chat rename failed for %s", row["sessionId"], exc_info=True)
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        return _session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


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
    if kind == "interrupt" and not isinstance(auth, AgentInvocation):
        if auth.user_id != row["humanUserId"] and not team_control_on(row):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can stop this run.")
    # Ending is the owner's alone: never a teammate under team control, never an agent.
    if kind == "end" and (isinstance(auth, AgentInvocation) or auth.user_id != row["humanUserId"]):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can end it.")


def _require_control(row: dict, request: LocalSessionCommand, *, allow_offline_send: bool = False) -> None:
    if row["generation"] != request.expected_generation:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session generation changed.")
    if row.get("endedAt") is not None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
    if (row["status"] == "offline" or row["onlineUntil"] <= int(time.time())) and not allow_offline_send:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session is offline.")
    if row.get("unshared") and request.kind != "end":
        # Only me: its Mac carries out nothing but its owner's End (Remove), so nothing else is admitted.
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Share this session with your workspace first.")
    if request.kind == "end":
        # The Mac stops the session's own process: one Lightbulb holds, or a Claude (a plain one,
        # or a background session, which its Mac stops with `claude stop`). A Codex app or daemon
        # process hosts many conversations. Ending needs no send capability, so an unverified
        # session can be ended too.
        # `kind == "background"` is a legacy row no Mac registers any more (the relay drops it).
        if session_terminal(row) != "held" and (row["provider"] != "claude" or row.get("kind") == "background"):
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This session can only be ended on its Mac.")
        return
    caps = row["capabilities"]
    if request.kind == "answer" and row.get("controlUnavailableReason") in _QUESTION_RECOVERABLE_REASONS:
        # A paired desktop reported this exact held prompt, not permission to start a new turn.
        # Its native adapter rechecks the process and hook before resolving the answer.
        _require_pending_question(row, request)
        return
    # Ordinary controls stay closed when the Mac cannot verify them.
    if not row["capabilitiesVerified"] or row.get("controlUnavailableReason"):
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
    if request.decision == "allow" and not (
        pending.get("exact") or (pending.get("full") and request.shown_digest == pending["inputDigest"])
    ):
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This prompt can only be approved in the terminal.")
    if request.mode is not None and request.mode not in pending.get("modes", []):
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "That option is not offered for this prompt.")


def _require_pending_question(row: dict, request: LocalSessionCommand) -> None:
    pending = row.get("pendingQuestion")
    if row["provider"] != "claude" or row["status"] != "waiting-approval" or not pending:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "The session is no longer waiting for an answer.")
    if pending["promptId"] != request.prompt_id or pending["inputDigest"] != request.input_digest:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "That question has changed.")
    if set(request.answers or {}) != {question["question"] for question in pending["questions"]}:
        raise ApiProblem("INVALID_REQUEST", "Answer exactly the questions shown in this prompt.")


async def submit_command(
    auth: AuthContext,
    room_id: str | None,
    session_id: str,
    request: LocalSessionCommand,
    *,
    room_meta_fenced_by_extra: bool = False,
    replaces_request_id: str | None = None,
    remove_on_end: bool = False,
) -> CommandReceipt:
    """A person's command (a follow-up, an answer, a stop, an end). Agents and sessions never submit
    one: they message a session through the engine."""
    if isinstance(auth, AgentInvocation):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only a person can command a session.")
    owner = caller_owner(auth)
    # A roomless command is stored with roomId "": current desktops require the field as a string.
    room_id = validate_uuid(room_id, label="room ID") if room_id else ""
    session_id = validate_uuid(session_id, label="local session ID")
    if replaces_request_id is not None:
        from pydantic import TypeAdapter, ValidationError

        try:
            TypeAdapter(OpaqueId).validate_python(replaces_request_id)
        except ValidationError as exc:
            raise ApiProblem("INVALID_REQUEST", "Invalid replaced request ID.") from exc
        if request.kind != "send" or replaces_request_id == request.request_id or room_id:
            raise ApiProblem("INVALID_REQUEST", "Only a direct human follow-up can replace an offline send.")
    payload = request.model_dump(by_alias=True, exclude={"shown_digest"})
    author = {"actorUserId": auth.user_id}
    req_hash = messaging_repo.request_hash(
        owner,
        session_id,
        room_id,
        auth.user_id,
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
            not room_id
            and request.kind == "send"
            and request.expected_turn_id is None
            and (row["status"] == "offline" or row["onlineUntil"] <= now)
            and row["capabilities"]["sendMode"] == "queue"
        )
        _require_control(row, request, allow_offline_send=offline_send)
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
            # Backend-only: dispatch re-runs the approval check. No model field names this key,
            # so no ``_public`` projection (receipt, outbox, the Mac's envelope) can carry it.
            **({"approvalShownDigest": request.shown_digest} if request.shown_digest else {}),
            "pk": repo.session_pk(owner, session_id),
            "sk": f"COMMAND#{request.request_id}",
            "sessionId": session_id,
            "roomId": room_id,
            **author,
            "commandSeq": row["commandSeq"] + 1,
            "state": "pending",
            "createdAt": now,
            # Server-side only, never rewritten (the dispatch window rewrites createdAt): the loop limit.
            "submittedAt": now,
            # Server-side only (not in CommandReceipt): the relay's dispatch-latency metric.
            "createdAtMs": int(time.time() * 1000),
            "deliverBy": now + (OFFLINE_SEND_SECONDS if offline_send else 30),
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
            # An "End and remove" (`remove_session`): server-side only, read at the run's end.
            **({"removesOnEnd": True} if remove_on_end else {}),
            **({"replacesRequestId": replaces_request_id} if replaces_request_id else {}),
        }
        # Every remote control action is audited in the same transaction that admits it.
        audit = audit_row(owner, session_id, auth, _audit_action(request), request_id=request.request_id)
        try:
            await repo.admit_command(
                row,
                enrollment,
                receipt,
                allow_offline=offline_send,
                extra=[
                    *([retry_update] if retry_update else []),
                    {"Put": {"TableName": messaging_repo._table().name, "Item": audit}},
                ],
                room_meta_fenced_by_extra=room_meta_fenced_by_extra,
            )
            actor = auth.user_id
            origin = "direct"
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

                await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
            return _public(CommandReceipt, receipt)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry the same requestId.")


async def _expire_pending_command(owner: str, sid: str, row: dict) -> dict:
    if row["state"] not in {"pending", "awaiting_approval"} or row["deliverBy"] > int(time.time()):
        return row
    expired = dict(row)
    expired.update(state="rejected", updatedAt=int(time.time()), revision=str(uuid.uuid4()))
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
                await publish_resources_changed(owner, session["humanUserId"], session_id=session_id)
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
                await publish_resources_changed(owner, session["humanUserId"], session_id=session_id)
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
    "end",
    "approve",
    "answer",
    "deny",
    "team_control_on",
    "team_control_off",
    "team_control_ended",
    "share_link_created",
    "share_link_revoked",
    "share_link_audience",
    "session_copied",
    "pickup_on",
    "pickup_off",
    "picked_up",
    "pickup_expired",
    "join_requested",
    "join_allowed",
    "join_declined",
    "join_allow_undone",
    "ask_to_join_on",
    "ask_to_join_off",
    "pickup_requested",
    "pickup_request_declined",
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
            # Members may type now, so a member's pending ask (left by an older client) is answered.
            # A link viewer's ask (`outsider`) stays: team control does not admit them.
            updated["joinRequests"] = {
                who: r
                for who, r in (row.get("joinRequests") or {}).items()
                if r.get("state") != "pending" or r.get("outsider")
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
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
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

#: A request never expires by the clock: it stays until the owner answers, or the session ends or
#: restarts. So the person who asked may remind the owner, at most once per this window (ISOA-729).
ASK_AGAIN_AFTER_S = 10 * 60


#: How long a person is told that their ask was dropped by the session ending or restarting.
JOIN_CLOSED_TTL_S = 24 * 3600


def closed_joins(row: dict) -> dict[str, int]:
    """Who to tell that their ask to join closed unanswered, and since when: the closures the row
    already carries (inside JOIN_CLOSED_TTL_S) plus every ask still pending on it. Called where a
    run's JOIN_FIELDS are dropped (the Ended frame, a new generation), so the next run's row can
    say it to exactly those people, after any reload (ISOA-729)."""
    now = int(time.time())
    closed = {u: int(at) for u, at in (row.get("joinClosed") or {}).items() if now - int(at) < JOIN_CLOSED_TTL_S}
    closed.update({u: now for u, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"})
    return closed


def _asked_at(request: dict) -> str | None:
    """When this person last asked. A request from before ``askedAt`` has only its ``createdAt``."""
    return iso(int(request["askedAt"])) if request.get("askedAt") else request.get("createdAt")


def _new_request() -> dict:
    return {"state": "pending", "requestId": str(uuid.uuid4()), "createdAt": utc_now_iso(), "askedAt": int(time.time())}


def _ask_again(request: dict | None) -> dict | None:
    """The same pending request, stamped as asked again, once it has waited ASK_AGAIN_AFTER_S;
    None otherwise. The request id stays, so the owner's open Activity item still answers it."""
    now = int(time.time())
    if (request or {}).get("state") != "pending" or now - int(request.get("askedAt") or 0) < ASK_AGAIN_AFTER_S:
        return None
    return {**request, "askedAt": now, "reminded": True}


class JoinDecisionRequest(ContractModel):
    decision: Literal["allow", "decline"]
    request_id: str | None = None


def _valid_user(user_id: str) -> str:
    from pydantic import TypeAdapter, ValidationError

    try:
        return TypeAdapter(OpaqueId).validate_python(user_id)
    except ValidationError as exc:
        raise ApiProblem("INVALID_REQUEST", "Invalid user ID.") from exc


async def _change_session(
    auth: AuthContext, session_id: str, change, audit_action: str, subject: str | None, *, allow_ended: bool = False
):
    """Load, let ``change`` return the new row (or None for "nothing to do"), write it with one
    audit row, and hint the workspace. ``change`` may raise to refuse."""
    from core.services.local_session_relay import publish_resources_changed

    owner = resolve_owner_id(auth)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if row.get("endedAt") is not None and not allow_ended:
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
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        return updated, audit
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def request_join(auth: AuthContext, session_id: str) -> LocalSession:
    """Owner ruling 2026-10-26: trust = membership. A workspace member types while the owner has
    "Let teammates control this session" on and watches otherwise: there is no member ask. Only a
    viewer of an "Anyone with the link" page asks (``session_links.ask_to_join``). Kept for clients
    that still offer the old button."""
    raise ApiProblem(
        "LOCAL_SESSION_FORBIDDEN", "Teammates can type here only while its owner lets teammates control it."
    )


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
        asked = (row.get("joinRequests") or {}).get(user_id) or {}
        answered.update(outsider=bool(asked.get("outsider")), askedAt=asked.get("askedAt"), request=asked)
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
    if audit is not None and decision == "allow" and answered.get("outsider"):
        await _admit_outsider(auth, session_id, user_id, row, answered["request"])
    if audit is not None:  # only a write that landed; a repeated answer is a no-op
        if answered.get("outsider"):  # 06 §2's funnel: a stranger's ask from the public page
            latency = int(time.time()) - int(answered["askedAt"]) if answered.get("askedAt") else None
            _capture_growth(row, "workspace_join_decided", auth.user_id, decision=decision, latency_s=latency)
        else:
            _capture_growth(row, "session_join_decided", auth.user_id, decision=decision)
        if decision == "allow" or not answered.get("outsider"):  # 06 §2: a decline to an outsider is not announced
            await _notify_join_answer(row, user_id, action, answered["requestId"])
        await _push_presence(row)
    return _session_public(row, auth.user_id)


async def _admit_outsider(auth: AuthContext, session_id: str, user_id: str, row: dict, request: dict) -> None:
    """D1: Allow on an ask from the public page also makes that person a workspace member, as the
    join link does (``org:member``, never more). After the Allow committed, so a decline or an end
    that won the race never grants membership; if Clerk refuses, the Allow is undone (the ask is
    pending again) and the error stands, so the owner can try again."""
    from core.services import clerk_admin

    # The call is idempotent (an existing member reads as `already_member`), so a lost response is
    # settled by asking again: only a second refusal is a real one, and only then is the Allow undone.
    try:
        try:
            await clerk_admin.create_organization_membership(org_id=row["ownerId"], user_id=user_id, role="org:member")
        except Exception:
            await clerk_admin.create_organization_membership(org_id=row["ownerId"], user_id=user_id, role="org:member")
    except Exception:

        def undo(current: dict) -> dict | None:
            if user_id not in (current.get("allowedSenders") or []):
                return None
            requests = {**(current.get("joinRequests") or {}), user_id: request}
            senders = [u for u in current.get("allowedSenders") or [] if u != user_id]
            return {**current, "joinRequests": requests, "allowedSenders": senders}

        await _change_session(auth, session_id, undo, "join_allow_undone", user_id, allow_ended=True)
        raise


#: What Allow on an outside ask grants, said where the owner decides (Activity, the push).
OUTSIDER_ALLOW_COPY = (
    "They watched it from its public link. Allow makes them a member of your workspace and lets them use this session."
)


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


def pending_pickups(row: dict) -> dict[str, str]:
    """Each person waiting for the owner to allow pick-up, with the request id they asked under."""
    if row.get("pickupAllowed"):
        return {}
    return {u: r["requestId"] for u, r in (row.get("pickupRequests") or {}).items() if r.get("state") == "pending"}


async def request_pickup_access(auth: AuthContext, session_id: str) -> LocalSession:
    """A teammate asks the owner to allow pick-up (spec 2026-10-05-ask-to-pick-up). Claude only;
    an ended session may be asked for (its snapshot outlives it). Asking while pending or while
    pick-up is on changes nothing, except that a pending ask reminds the owner at most once per
    ASK_AGAIN_AFTER_S; a declined person may ask again."""
    from core.services import session_pickup  # lazy: it imports this module

    me = auth.user_id

    def change(row: dict) -> dict | None:
        if me == row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This is your own session.")
        if row["provider"] != "claude":
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Only Claude Code sessions can be picked up.")
        if session_pickup._expired(row):
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session's snapshot has expired.")
        requests = dict(row.get("pickupRequests") or {})
        if row.get("pickupAllowed"):
            return None
        if (requests.get(me) or {}).get("state") == "pending":
            if (again := _ask_again(requests[me])) is None:
                return None
            requests[me] = again
        else:
            requests[me] = _new_request()
        return {**row, "pickupRequests": requests}

    row, audit = await _change_session(auth, session_id, change, "pickup_requested", None, allow_ended=True)
    if audit is not None:
        await _notify_join_request(row, me, kind="pickup")
        await _push_presence(row)
    return _session_public(row, me)


async def decide_pickup_request(
    auth: AuthContext, session_id: str, user_id: str, decision: str, *, request_id: str | None = None
) -> LocalSession:
    """The owner answers one pick-up request. Allow turns pick-up on for the whole session through
    set_pickup, which also clears every pending request, audits ``pickup_on`` and pushes presence."""
    from core.services import session_pickup

    user_id = _valid_user(user_id)
    row, _ = await authorize_session(auth, None, session_id)
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can answer a request to pick up.")
    current = (row.get("pickupRequests") or {}).get(user_id) or {}
    if current.get("state") != "pending" or (request_id is not None and current.get("requestId") != request_id):
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "That request was already answered.")
    if decision == "allow":
        if session_pickup._expired(row):
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session's snapshot has expired.")
        # Allow creates the link as Workspace only (spec §3.1), first: a session never has pick-up on
        # without its link. A failure before any write leaves the request pending; one after the link
        # exists takes the link back.
        had_link = bool(row.get("shareLink"))
        await put_link(auth, session_id, None)
        try:
            public = await session_pickup.set_pickup(
                auth, session_id, True, expect_request=(user_id, current["requestId"])
            )
        except Exception:
            if not had_link:
                await delete_link(auth, session_id)
            raise
        await _notify_join_answer(row, user_id, "join_allowed", current["requestId"], kind="pickup")
        return public

    def change(r: dict) -> dict | None:
        requests = dict(r.get("pickupRequests") or {})
        cur = requests.get(user_id) or {}
        if cur.get("state") != "pending" or cur.get("requestId") != current["requestId"]:
            return None
        requests[user_id] = {**cur, "state": "declined"}
        return {**r, "pickupRequests": requests}

    row, audit = await _change_session(auth, session_id, change, "pickup_request_declined", user_id, allow_ended=True)
    if audit is not None:
        await _notify_join_answer(row, user_id, "join_declined", current["requestId"], kind="pickup")
        await _push_presence(row)
    return _session_public(row, auth.user_id)


#: What Allow on a pick-up request shares, in the substance of the owner's own pick-up switch
#: warning (SessionDetails.tsx). Activity and the push both say it.
PICKUP_ALLOW_COPY = (
    "Allow lets everyone in the workspace pick up this session on their Mac. It uploads the whole "
    "conversation and your uncommitted changes as they are. Nothing is redacted."
)


def _request_title(name: str, row: dict, verb: str, again: bool) -> str:
    from core.services import notification_copy as copy

    session = copy.line(row.get("displayTitle") or row.get("title") or row.get("cwdName"), 40) or "a session"
    return f"{name} {'is still waiting' if again else 'wants'} to {verb} {session}"


def pickup_request_title(name: str, row: dict, *, again: bool = False) -> str:
    return _request_title(name, row, "pick up", again)


def join_request_title(name: str, row: dict, *, again: bool = False) -> str:
    return _request_title(name, row, "join", again)


async def _notify_join_request(
    row: dict,
    requester: str,
    *,
    kind: str = "join",
) -> None:
    """The owner's "needs you" item, a live event and a push. Best effort, each on its own.
    ``kind="pickup"`` is the same item for a request to pick up (spec 2026-10-05-ask-to-pick-up)."""
    from core.services.local_session_relay import ACTIVITY_TTL_S
    from core.services.messaging import human_display_name

    owner, human, sid = row["ownerId"], row["humanUserId"], row["sessionId"]
    pickup = kind == "pickup"
    request = row["pickupRequests" if pickup else "joinRequests"][requester]
    request_id, created = request["requestId"], request["createdAt"]
    again = bool(request.get("reminded"))
    try:
        name = request.get("name") or await human_display_name(requester)
    except Exception:
        name = "A teammate"
    if pickup:
        title = pickup_request_title(name, row, again=again)
        body = PICKUP_ALLOW_COPY
    elif request.get("outsider"):
        title = join_request_title(name, row, again=again)
        body = OUTSIDER_ALLOW_COPY
    else:
        title = join_request_title(name, row, again=again)
        body = "Allow them to send follow-ups and answer prompts in this session."
    # The ask's Needs-you item is keyed by the ask, so a reminder rewrites it.
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-{kind}|{sid}|{request_id}"))
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
                **({"request": "pickup"} if pickup else {}),
            }
        )
    except Exception:
        logger.warning("Session join request activity row failed", exc_info=True)
    if again:  # its own live event: clients drop one they have already seen
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-{kind}|{sid}|{request_id}|{request['askedAt']}"))
    await _session_notice(owner, human, sid, "join_request", title, body, event_id)


async def _push_presence(row: dict) -> None:
    from core.services import session_presence  # imports this module

    await session_presence.push(row)


async def _session_notice(
    owner: str,
    user: str,
    sid: str,
    state: str,
    title: str,
    body: str,
    event_id: str,
) -> None:
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


async def _notify_join_answer(
    row: dict, requester: str, state: str, request_id: str | None, *, kind: str = "join"
) -> None:
    """Tell the person who asked how the owner answered, once per decision."""
    from core.services import notification_copy as copy
    from core.services.messaging import human_display_name

    sid = row["sessionId"]
    try:
        name = await human_display_name(row["humanUserId"])
    except Exception:
        name = "The session's owner"
    session = copy.line(row.get("displayTitle") or row.get("title"), 40) or sid[:8]
    if kind == "pickup" and state == "join_allowed":
        title, body = f"{name} let you pick up {session}", "Pick it up on your Mac once its snapshot is ready."
    elif kind == "pickup":
        title, body = f"{name} declined your request to pick up {session}", "You can ask again."
    elif state == "join_allowed":
        title, body = f"{name} let you into {session}", "You can send follow-ups and answer its prompts."
    else:
        title, body = f"{name} declined your request", session
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"local-session-{kind}-answer|{sid}|{request_id}|{state}"))
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
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        return _session_public(updated, auth.user_id)
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


# --- The one link (spec 2026-10-05-public-session-links §3.1) -------------------------------
#
# One link per session, and its audience: the workspace, or anyone holding it (who then watches the
# live terminal with no sign-in, session_links.viewing). A link only shows the session; continuing
# it is the owner's own switch. META.shareLink is the truth: the SHARELINK# lookup row only finds it.

AUDIENCES = Literal["workspace", "anyone"]


def _share_pk(token_hash: str) -> str:
    return f"SHARELINK#{token_hash}"


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LinkRequest(ContractModel):
    # None is Share: create a workspace link if there is none, otherwise keep its audience. Only the
    # audience select sends one, so a stale or failed read can never narrow an "anyone" link.
    audience: AUDIENCES | None = None


class SessionLink(ContractModel):
    # None only when the stored ciphertext no longer decrypts (a rotated ENCRYPTION_KEY): the link
    # still opens, it just cannot be shown again; turning it off and on makes a new one.
    url: str | None
    audience: AUDIENCES


def link_url(token: str) -> str:
    from core.config import settings

    return f"{settings.FRONTEND_URL.rstrip('/')}/s#{token}"


def public_id(link: dict | None, row: dict | None = None) -> str | None:
    """The id an "Anyone with the link" link's watch channel is named by (`/pub/<id>`); None for a
    workspace link. It is what makes a link public: an "anyone" link made before public watch has
    none, so it opens for the workspace only until its owner picks Anyone again (owner ruling
    2026-10-08). Minted new each time a link is made public, so an earlier watcher's channel is dead.
    ``row``: the link's session, at the doors that open it. One with no terminal has nothing public
    to show, so a public link made on it earlier opens for the workspace only."""
    if row is not None and conversation_only(row):
        return None
    return (link or {}).get("publicId")


def _with_audience(link: dict, audience: str) -> dict:
    kept = {k: v for k, v in link.items() if k != "publicId"}
    return {**kept, "audience": audience, **({"publicId": str(uuid.uuid4())} if audience == "anyone" else {})}


def narrow_link(row: dict) -> None:
    """A session known to have no terminal keeps no public link: the relay narrows it on the row
    it is writing, so a later run of the conversation in a terminal is not public again without a
    new "Anyone with the link" choice. (`public_id`'s `row` covers the time before that write.)"""
    if public_id(row.get("shareLink")):
        row["shareLink"] = _with_audience(row["shareLink"], "workspace")


def _link_public(link: dict, row: dict) -> SessionLink:
    from core import encryption

    try:
        url = link_url(encryption.decrypt(link["tokenEnc"]))
    except ValueError:  # encryption.decrypt's only failure
        logger.warning("session link undecryptable")
        url = None
    return SessionLink(url=url, audience="anyone" if public_id(link, row) else "workspace")


async def _owned_session(auth: AuthContext, session_id: str, verb: str = "share") -> dict:
    """The owner's own session. Reading and turning off skip the enrollment check on purpose: an
    owner who turned sharing off must still be able to find and kill their link."""
    row = await repo.get_session(resolve_owner_id(auth), validate_uuid(session_id, label="local session ID"))
    if row is None:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", f"Only the session's owner can {verb} it.")
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


async def put_link(auth: AuthContext, session_id: str, audience: str | None) -> SessionLink:
    """Create the session's one link, or change its audience. ``audience`` None (Share, and Ask to
    pick up's Allow): create a workspace link if there is none, otherwise leave the link as it is.
    A link only shows the session: continuing it is the owner's own switch (`session_pickup.set_pickup`)."""
    import secrets

    from core import encryption
    from core.services.local_session_relay import publish_resources_changed

    table = messaging_repo._table().name
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can share it.")
        if row.get("unshared"):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "Share this session with your workspace first.")
        if audience == "anyone" and conversation_only(row):
            raise ApiProblem(
                "LOCAL_SESSION_UNSUPPORTED", "Sessions inside an app can be shared with your workspace only."
            )
        if pickup_expired(row):
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session is too old to share.")
        owner, sid, link = row["ownerId"], row["sessionId"], row.get("shareLink")
        if link and (audience is None or ("anyone" if public_id(link) else "workspace") == audience):
            break
        if link:
            new = _with_audience(link, audience)
            extra = [
                {
                    "Put": {
                        "TableName": table,
                        "Item": audit_row(owner, sid, auth, "share_link_audience", detail=audience),
                    }
                }
            ]
        else:
            token = secrets.token_urlsafe(32)
            new = _with_audience(
                {
                    "tokenHash": _token_hash(token),
                    "tokenEnc": encryption.encrypt(token),
                    "createdAt": int(time.time()),
                    "createdBy": auth.user_id,
                },
                audience or "workspace",
            )
            lookup = {"pk": _share_pk(new["tokenHash"]), "sk": "META", "ownerId": owner, "sessionId": sid}
            extra = [
                {"Put": {"TableName": table, "Item": lookup}},
                {
                    "Put": {
                        "TableName": table,
                        "Item": audit_row(owner, sid, auth, "share_link_created", detail=new["audience"]),
                    }
                },
            ]
        try:
            updated = {**row, "shareLink": new, "revision": str(uuid.uuid4())}
            await repo.put_session(updated, row, enrollment, extra=extra)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        if bool(public_id(new, updated)) != bool(public_id(link, row)):
            # The band's "via link" count follows the audience at once.
            await _push_presence(updated)
        if not link:
            _capture_growth(row, "share_link_created", auth.user_id, audience=new["audience"])
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        link = new
        break
    else:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
    return _link_public(link, row)


async def get_link(auth: AuthContext, session_id: str) -> SessionLink:
    row = await _owned_session(auth, session_id)
    if not row.get("shareLink"):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session has no link.")
    if pickup_expired(row):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session is too old to share.")
    return _link_public(row["shareLink"], row)


async def delete_link(auth: AuthContext, session_id: str, *, removed_only: bool = False) -> None:
    """Turn the link off: every page reads "turned off" and no new copy can be made. On a Claude
    session this retires the snapshot too, in retire's own transaction. Idempotent.
    ``removed_only`` is `remove_session`'s cleanup: it acts only on a row that still carries
    `removedAt`, and every write below is fenced on that row's revision, so a session that started
    again meanwhile keeps its link and snapshot."""
    from core.services import session_pickup
    from core.services.local_session_relay import publish_resources_changed

    table = messaging_repo._table().name
    for _ in range(6):
        row = await _owned_session(auth, session_id)
        if removed_only and row.get("removedAt") is None:
            return
        link = row.get("shareLink")
        if not link and not row.get("pickupAllowed"):
            return
        owner, sid = row["ownerId"], row["sessionId"]
        try:
            if row.get("pickupAllowed"):
                # Pick-up switched on by the removed switch has no link: turning off still turns it off.
                action = "share_link_revoked" if link else "pickup_off"
                await session_pickup.retire(owner, sid, row, auth=auth, action=action)
                return
            await messaging_repo._transact(
                [
                    {
                        "Update": {
                            "TableName": table,
                            "Key": {"pk": row["pk"], "sk": "META"},
                            "UpdateExpression": "REMOVE shareLink SET revision = :new",
                            "ConditionExpression": "revision = :old",
                            "ExpressionAttributeValues": {":new": str(uuid.uuid4()), ":old": row["revision"]},
                        }
                    },
                    {"Delete": {"TableName": table, "Key": {"pk": _share_pk(link["tokenHash"]), "sk": "META"}}},
                    {"Put": {"TableName": table, "Item": audit_row(owner, sid, auth, "share_link_revoked")}},
                ]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"], session_id=row["sessionId"])
        return
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def remove_session(auth: AuthContext, session_id: str, *, hint: bool = True) -> None:
    """The owner takes one of their sessions off every list. A stamp (`removedAt`), not a
    delete: events, receipts, audit rows, the chat and the Mac's own mirror all hang off this row,
    and the stamp makes `authorize_session` (and the public link door) answer "not found" at once.
    The stamp is the commit point, fenced on the row still being ended: a resume that lands first
    wins. Only then are the link and the pick-up snapshot turned off, and the chat emptied of
    everyone but the owner (`finish_removal`); a repeat call finishes a cleanup that failed
    part-way. Starting the conversation again on the Mac registers a new run and lists it again.
    Idempotent.

    A LIVE session is ended first (owner ruling 2026-10-08: remove ends it): the owner's End goes
    to its Mac, marked `removesOnEnd`. The relay stamps `removedAt` when that run's end arrives,
    and only if such an End reached the Mac and was not refused (`landed_ends`); a refused,
    expired or unanswered End leaves the session listed and running, and nothing on the row says
    a removal was asked. Only what End can stop: an offline Mac or a Codex app thread is refused
    here (no command waits for a Mac to come back; End has 30 s to arrive)."""
    was_ended = False
    for _ in range(6):
        row = await _owned_session(auth, session_id, "remove")
        if row.get("removedAt") is not None:
            break
        live = row.get("endedAt") is None
        if live and was_ended:
            # It was ended when the owner asked; a resume landed first. That run is not ended for them.
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "This session started again. Remove it once more to end it.")
        was_ended = not live
        if live:
            end = LocalSessionCommand(request_id=str(uuid.uuid4()), expected_generation=row["generation"], kind="end")
            try:
                _require_control(row, end)
                await submit_command(auth, None, session_id, end, remove_on_end=True)
            except ApiProblem as exc:
                current = await repo.get_session(row["ownerId"], row["sessionId"])
                if current is not None and current.get("endedAt") is not None:
                    was_ended = True  # it ended meanwhile: remove the ended row
                    continue
                if exc.error.code != "LOCAL_SESSION_OFFLINE":
                    raise
                raise ApiProblem(
                    "LOCAL_SESSION_OFFLINE",
                    "This session's Mac is offline, so it cannot be stopped from here. "
                    "Remove it when the Mac is back online, or end it on that Mac.",
                ) from exc
            return
        try:
            await messaging_repo._transact(
                [
                    {
                        "Update": {
                            "TableName": messaging_repo._table().name,
                            "Key": {"pk": row["pk"], "sk": "META"},
                            "UpdateExpression": "SET removedAt = :now, revision = :new "
                            "REMOVE resumeAllowed, #resume, screenUntil",
                            # Still ended: a resume that registered meanwhile is a live session.
                            "ConditionExpression": "revision = :old AND attribute_exists(endedAt)",
                            "ExpressionAttributeNames": {"#resume": "resume"},
                            "ExpressionAttributeValues": {
                                ":now": int(time.time()),
                                ":new": str(uuid.uuid4()),
                                ":old": row["revision"],
                            },
                        }
                    }
                ]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        break
    else:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
    await finish_removal(auth, session_id, hint=hint)


async def finish_removal(auth: AuthContext, session_id: str, *, hint: bool = True) -> None:
    """What follows the `removedAt` stamp: the link, the pick-up snapshot, the chat. On every
    call, not only the one that stamped: a repeat DELETE after a failed cleanup finishes it. The
    doors do not wait for it: `authorize_session`, the public link door, a session chat's member
    door (`messaging.require_room_member`) and every post into that chat
    (`messaging_repo.create_message`) refuse a removed row on their own."""
    from core.services import session_chat  # lazy: it imports this module
    from core.services.local_session_relay import publish_resources_changed

    row = await _owned_session(auth, session_id, "remove")
    await delete_link(auth, session_id, removed_only=True)
    # The chat is emptied only while the row is still removed: a new run that registered since
    # the stamp cleared it and reopened the chat, and its readers keep their seats.
    # ponytail: a re-read, not a transaction (seats live on the room, not this row); a run that
    # registers inside this one call's window loses seats that the next chat open gives back.
    current = await repo.get_session(row["ownerId"], row["sessionId"])
    if current is not None and current.get("removedAt") is not None:
        await session_chat.silence(row["ownerId"], row["sessionId"], keep=row["humanUserId"])
    await session_chat.follow_session(row["ownerId"], row["sessionId"])
    if hint:
        await publish_resources_changed(row["ownerId"], row["humanUserId"], session_id=row["sessionId"])


REMOVE_ENDED_CAP = 100


async def remove_ended_sessions(auth: AuthContext) -> int:
    """Remove every ended session of the caller's own that the lists can still show (the last 7
    days). Returns how many were removed."""
    owner = resolve_owner_id(auth)
    since = int(time.time()) - RECENT_ENDED_SECONDS
    listed = await repo.list_user_sessions(owner, auth.user_id)
    rows = await repo.get_sessions(owner, [p["sessionId"] for p in listed])
    mine = [
        row["sessionId"]
        for row in rows
        if row
        and row.get("humanUserId") == auth.user_id
        and row.get("removedAt") is None
        and (row.get("endedAt") or 0) >= since
    ]
    # ponytail: 100 per call, ten at a time, inside API Gateway's 30 s; the caller asks again for more.
    mine = mine[:REMOVE_ENDED_CAP]
    removed = 0
    for start in range(0, len(mine), 10):
        results = await asyncio.gather(
            *(remove_session(auth, sid, hint=False) for sid in mine[start : start + 10]), return_exceptions=True
        )
        for result in results:
            if isinstance(result, ApiProblem):
                continue  # resumed or changed meanwhile: it stays
            if isinstance(result, BaseException):
                raise result
            removed += 1
    if removed:
        from core.services.local_session_relay import publish_resources_changed

        await publish_resources_changed(owner, auth.user_id)  # one hint for the lot
    return removed
