"""Canonical multiplayer room policy and public contract models.

DynamoDB owns the ordered room log; OpenClaw owns each agent's native session.
This module is the single policy seam between authenticated humans/internal
agents and :mod:`core.repositories.messaging_repo`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import uuid
from pathlib import PurePath
from datetime import datetime, timezone
from typing import Any, Literal

from botocore.exceptions import ClientError
from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.auth import is_org_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event, schedule_trial_first_agent_response
from core.dynamodb import run_in_thread
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services.provision_gate import has_always_on_entitlement
from core.services.push_service import send_push_to_user
from core.services import hermes_profiles, run_analytics
from core.services.session_keys import parse_lightbulb_session_key, parse_session_key
from core.services.paperclip_provisioning import STARTER_ROLE_KEYS as _STARTER_KEYS
from core.services.usage_service import record_usage

logger = logging.getLogger(__name__)

RoomKind = Literal["channel", "dm", "group-dm"]
MemberKind = Literal["human", "agent"]
MemberRole = Literal["owner", "admin", "member"]
ListenMode = Literal["active", "mentions", "observe"]
AuthorKind = Literal["human", "agent", "system"]
EventKind = Literal["user_request", "room_event"]

MESSAGE_TEXT_MAX_BYTES = 100_000
IMAGE_MAX_BYTES = 5 * 1024 * 1024
# A PDF is a document, not a photo: a real scanned contract or slide deck runs
# well past 5 MiB. 20 MiB is generous enough for that while still bounded --
# this route reads the whole file off EFS and serves it back through the API
# on every read, so it is deliberately not "generous" beyond that.
PDF_MAX_BYTES = 20 * 1024 * 1024
# ZIPs are opaque downloads. The service only checks their container signature;
# it never extracts, previews, or executes their contents.
ZIP_MAX_BYTES = 20 * 1024 * 1024
XLSX_MAX_BYTES = 20 * 1024 * 1024
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ATTACHMENT_MAX_BYTES_BY_TYPE = {
    "image/png": IMAGE_MAX_BYTES,
    "image/jpeg": IMAGE_MAX_BYTES,
    "image/webp": IMAGE_MAX_BYTES,
    "image/gif": IMAGE_MAX_BYTES,
    "application/pdf": PDF_MAX_BYTES,
    "application/zip": ZIP_MAX_BYTES,
    XLSX_CONTENT_TYPE: XLSX_MAX_BYTES,
}
ATTACHMENT_MAX_BYTES = max(ATTACHMENT_MAX_BYTES_BY_TYPE.values())
# Base64 is exactly 4 characters per 3 bytes: the ENCODED ceiling for that largest cap.
ATTACHMENT_MAX_BASE64_CHARS = -(-ATTACHMENT_MAX_BYTES // 3) * 4
ATTACHMENT_MAX_COUNT = 4
ROOM_NAME_MAX_CHARS = 200
ROOM_TOPIC_MAX_CHARS = 250
MAX_PINNED_MESSAGES = messaging_repo.MAX_PINNED_MESSAGES
MAX_SECTIONS = 20
SECTION_NAME_MAX_CHARS = 40
EVENT_ID_MAX_BYTES = 256
DEFAULT_ROOM_NAME = "general"
# Seeded ONCE per workspace as a CEO-authored message (``ensure_default_room``
# returns early on any room that already has messages), so this is the very
# first thing a new user reads. It does NOT re-list the roster — the member
# list already shows it — and instead spends its length on the blank-page
# problem: state the role, name the two first-run doors (connect tools, paste
# existing context — first-run spec 2026-09-17 §4.1), and hand over a
# copy-pasteable prompt for anyone who would rather watch before typing.
# Owner-approved copy (D1, 2026-09-22); keep it easy to swap.
# Refers to the CEO by ROLE, never a name: agent names are user-editable.
DEFAULT_ROOM_WELCOME = (
    "I’m the CEO here — tell me what you want to happen and I’ll put the right people on it.\n\n"
    "Start by connecting your tools in the setup cards, or paste what you’ve already told "
    "another assistant about your business.\n\n"
    "Or try: “Research our top three competitors and tell me where we’re weakest.”"
)
ROOM_ARCHIVED_MESSAGE = "This channel is archived. Unarchive it to post."
API_ERROR_STATUS = {
    "LOCAL_SESSION_FORBIDDEN": 403,
    "LOCAL_SESSION_NOT_FOUND": 404,
    "LOCAL_SESSION_CONFLICT": 409,
    "LOCAL_SESSION_UNSUPPORTED": 409,
    "LOCAL_SESSION_OFFLINE": 409,
    "INVALID_REQUEST": 400,
    "ROOM_FORBIDDEN": 403,
    "PLAN_REQUIRED": 403,
    "ROOM_NOT_FOUND": 404,
    "MEMBER_NOT_FOUND": 404,
    "MESSAGE_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "OPERATION_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "AGENT_LIFECYCLE_CONFLICT": 409,
    "MESSAGE_CONFLICT": 409,
    "MEMBER_LIMIT": 409,
    "PIN_LIMIT": 409,
    "ROOM_ARCHIVED": 409,
    "ROOM_NOT_ARCHIVABLE": 409,
    "RUN_NOT_ACTIVE": 409,
    "RATE_LIMITED": 429,
    "RUNTIME_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}

ROOM_LIVE_EVENT_TYPES = frozenset(
    {
        "room.message.created",
        "room.message.updated",
        "room.message.reactions.changed",
        "room.typing.changed",
        "room.run.frame",
        "room.metadata.changed",
        "room.resources.changed",
    }
)
USER_LIVE_EVENT_TYPES = frozenset(
    {
        "room.message.available",
        "room.subscription.rotated",
        "room.access.removed",
        "runtime.operation.changed",
        "runtime.resource.invalidated",
        "paperclip.event",
        "room.member.updated",
        "workspace.sessions.changed",
    }
)
LIVE_EVENT_TYPES = ROOM_LIVE_EVENT_TYPES | USER_LIVE_EVENT_TYPES


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )


class MemberRef(ContractModel):
    kind: MemberKind
    id: str = Field(min_length=1, max_length=256)


class Author(ContractModel):
    kind: AuthorKind
    id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)


class RoomMember(MemberRef):
    display_name: str
    mention_handle: str | None
    role: MemberRole
    listen_mode: ListenMode | None
    joined_at: datetime
    joined_seq: int = Field(ge=0)


class RoomSummary(ContractModel):
    room_id: uuid.UUID
    name: str | None
    kind: RoomKind
    room_revision: int = Field(ge=1)
    last_message_seq: int = Field(ge=0)
    last_message_at: datetime | None
    unread_count: int = Field(ge=0)
    updated_at: datetime
    starred: bool = False
    muted: bool = False
    section_id: str | None = None
    topic: str | None = None
    # A DM has no name of its own -- it IS its counterpart, so a client cannot
    # label one row of its own sidebar without the member list. Carrying it here
    # is what lets the room LIST be the whole sidebar: before this, every client
    # followed the list with one GET /rooms/{id} per DM purely to read a name,
    # and re-ran that fan-out whenever any room's revision moved (a rename, a
    # delivery-key rotation), dropping the sidebar back to a loading state.
    # `delivery_key` deliberately stays out -- it is the AppSync channel secret
    # and is only needed for the room actually being subscribed.
    members: list[RoomMember] = []
    last_read_seq: int = Field(ge=0, default=0)
    # The ROOM's opaque AppSync segment: a shared channel lives in the owning
    # workspace, so a foreign member's client must subscribe with this, not its
    # own. None only on rows written before the segment was stamped.
    workspace_id: str | None = None
    # The platform-managed channel shared with the Lightbulb workspace.
    shared: bool = False
    # The seeded starter channel, where the onboarding setup band renders.
    is_default: bool = False
    # Set while the channel is archived: history stays readable, nothing new posts.
    archived_at: datetime | None = None
    archived_by: str | None = None


class ActiveRun(ContractModel):
    """One in-progress run, so a client that opens the room mid-turn can render
    it: the live ``run_active`` frame is published once, at turn start."""

    run_id: str
    agent_id: str
    trigger_message_id: str | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None


class RoomDetail(RoomSummary):
    # Re-declared as required: the list tolerates their absence for a client
    # holding a response from before they existed, the detail never does.
    members: list[RoomMember]
    last_read_seq: int = Field(ge=0)
    delivery_key: uuid.UUID
    created_at: datetime
    pinned_message_ids: list[str] = []
    active_runs: list[ActiveRun] = []


class RoomSection(ContractModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=SECTION_NAME_MAX_CHARS)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class SectionsRequest(ContractModel):
    sections: list[RoomSection] = Field(max_length=MAX_SECTIONS)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "SectionsRequest":
        ids = [section.id for section in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("section ids must be unique")
        return self


class RoomListResponse(ContractModel):
    rooms: list[RoomSummary]
    sections: list[RoomSection] = []


class CreateRoomMember(ContractModel):
    kind: MemberKind
    id: str = Field(min_length=1, max_length=256)
    role: Literal["admin", "member"]
    listen_mode: ListenMode | None

    @model_validator(mode="after")
    def validate_member_shape(self) -> "CreateRoomMember":
        _validate_member_shape(self.kind, self.role, self.listen_mode)
        return self


class CreateRoomRequest(ContractModel):
    client_room_id: uuid.UUID
    name: str | None
    kind: RoomKind
    # Absolute payload sanity cap only — the real member-count limit is
    # `messaging_repo.MAX_ROOM_MEMBERS`, enforced below as a 409 MEMBER_LIMIT
    # so callers get a reachable, meaningful error instead of a Pydantic 400.
    members: list[CreateRoomMember] = Field(max_length=256)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_room_name(value)


class PatchRoomRequest(ContractModel):
    name: str | None = None
    topic: str | None = None
    # A PATCH field rather than /archive + /unarchive routes: it rides this route's admin
    # check, revision CAS and metadata live event unchanged.
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_room_name(value)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str | None) -> str | None:
        return validate_room_topic(value)

    def named_updates(self) -> dict[str, Any]:
        """Only the fields the caller sent, so a topic-only PATCH cannot blank
        the name (and vice versa) while an explicit null still clears one."""
        return {key: getattr(self, key) for key in ("name", "topic") if key in self.model_fields_set}


class MemberPrefsRequest(ContractModel):
    starred: bool | None = None
    muted: bool | None = None
    section_id: uuid.UUID | None = None

    def named_updates(self) -> dict[str, Any]:
        """Only the fields the caller actually sent — an omitted field is left
        alone, an explicit null clears it."""
        out: dict[str, Any] = {}
        if "starred" in self.model_fields_set:
            out["starred"] = bool(self.starred)
        if "muted" in self.model_fields_set:
            out["muted"] = bool(self.muted)
        if "section_id" in self.model_fields_set:
            out["sectionId"] = str(self.section_id) if self.section_id else None
        return out


UpsertRoomMemberRequest = CreateRoomMember


class SetReactionRequest(ContractModel):
    emoji: Literal["👍", "❤️", "😂", "🎉", "👀", "✅"]
    active: bool


class UpdateMessageRequest(ContractModel):
    operation_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    text: str | None

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > MESSAGE_TEXT_MAX_BYTES:
            raise ValueError(f"text exceeds {MESSAGE_TEXT_MAX_BYTES} UTF-8 bytes")
        return value


class CanonicalMessage(ContractModel):
    message_id: uuid.UUID
    room_id: uuid.UUID
    message_seq: int = Field(ge=1)
    room_revision: int = Field(ge=1)
    author: Author
    text: str
    mentions: list[MemberRef]
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None
    created_at: datetime
    run_id: str | None
    attachments: list["MessageAttachment"] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)
    reactions: dict[str, list[str]] = Field(default_factory=dict)
    reaction_revision: int = Field(default=0, ge=0)
    message_revision: int = Field(default=0, ge=0)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None


class MessageAttachment(ContractModel):
    id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=256)
    content_type: Literal[
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]
    size: int = Field(gt=0)

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        if (
            not value.strip()
            or len(value.encode("utf-8")) > 255
            or any(ord(char) < 0x20 or ord(char) == 0x7F or char in "/\\" for char in value)
        ):
            raise ValueError("fileName is invalid")
        return value

    @model_validator(mode="after")
    def validate_size_for_type(self) -> "MessageAttachment":
        # Pydantic's `le=` can't be a single constant any more -- the cap is
        # per content type (a real PDF is bigger than a photo), so it has to
        # live here instead of on the `size` field.
        cap = ATTACHMENT_MAX_BYTES_BY_TYPE[self.content_type]
        if self.size > cap:
            raise ValueError(f"size must be at most {cap} bytes for {self.content_type}")
        return self


class CreateMessageRequest(ContractModel):
    client_message_id: uuid.UUID
    text: str
    mentions: list[MemberRef] = Field(default_factory=list, max_length=32)
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MESSAGE_TEXT_MAX_BYTES:
            raise ValueError(f"text exceeds {MESSAGE_TEXT_MAX_BYTES} UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def require_content(self) -> "CreateMessageRequest":
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("message requires text or an attachment")
        return self


class InlineAttachment(ContractModel):
    """One file an agent sends with its reply, base64 on the POST it is already making.

    The runtime has no attachment-upload route of its own and needs none: a screenshot is a
    couple of hundred KB inside a request that is happening anyway.
    """

    file_name: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    # The bound is in ENCODED characters and fires BEFORE the decode, so nothing unbounded is
    # ever decoded -- the list's own ``max_length`` runs after item validation, and the gateway
    # in front of this route is not a bound to depend on. Same reason the human upload route
    # reads to an explicit ceiling rather than to EOF (routers/messaging.py). The real
    # per-content-type cap still fires in :func:`store_attachment`.
    data_base64: Base64Bytes = Field(max_length=ATTACHMENT_MAX_BASE64_CHARS)


class AgentMessageRequest(ContractModel):
    event_id: str
    room_id: uuid.UUID
    agent_id: uuid.UUID
    text: str
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None
    run_id: uuid.UUID | None = None
    attachments: list[InlineAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return validate_event_id(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        # Blank text is judged by ``require_content`` below, not here.
        return validate_message_text(value) if value.strip() else value

    @model_validator(mode="after")
    def require_content(self) -> "AgentMessageRequest":
        # The same rule the human path has always had (``CreateMessageRequest``): the
        # emptiness check moved off the field because a reply whose only line was
        # ``MEDIA:<path>`` strips to empty text and arrives as a pure attachment.
        if not self.text.strip() and not self.attachments:
            raise ValueError("message requires text or an attachment")
        return self


class AgentMessageReceiptResponse(ContractModel):
    message_id: uuid.UUID
    room_id: uuid.UUID
    created_at: datetime


class MessagePage(ContractModel):
    messages: list[CanonicalMessage]
    next_after_seq: int | None
    next_before_seq: int | None = None


class ReadCursorRequest(ContractModel):
    last_read_seq: int = Field(ge=0)


class ReadCursorResponse(ContractModel):
    room_id: uuid.UUID
    last_read_seq: int = Field(ge=0)


class MarkUnreadRequest(ContractModel):
    from_seq: int | None = Field(default=None, ge=1)


class TypingRequest(ContractModel):
    room_id: uuid.UUID
    is_typing: bool


class AbortRunResponse(ContractModel):
    run_id: str
    state: Literal["aborting", "already-terminal"]


class RunStatusResponse(ContractModel):
    """Reconciliation read for a run's terminal state.

    The run frames are EPHEMERAL over the live channel -- unlike
    ``room.message.created`` they have no ``afterSeq`` gap recovery, so a client
    that reconnects mid-run (or misses a frame across a delivery-key rotation)
    can be left showing "running" forever with no other signal. This reads the
    run ledger row, the same truth ``abort``/``steer`` resolve against, so the
    frontend can self-heal on reconnect instead of requiring a page reload.
    """

    run_id: str
    state: Literal["running", "terminal"]


class RunUsage(ContractModel):
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = None


class RunEventRequest(ContractModel):
    run_id: uuid.UUID
    type: Literal["run.started", "run.heartbeat", "run.ended"]
    at: datetime
    session_key: str | None = Field(default=None, max_length=512)
    outcome: Literal["completed", "error", "aborted", "ignored", "gate_error"] | None = None
    error_kind: str | None = Field(default=None, max_length=128)
    usage: RunUsage | None = None

    @model_validator(mode="after")
    def _ended_is_complete(self) -> "RunEventRequest":
        if self.type == "run.ended":
            if self.outcome is None:
                raise ValueError("run.ended requires outcome")
            if self.outcome not in {"ignored", "gate_error"} and self.usage is None:
                raise ValueError("run.ended requires usage for a run that started")
        return self


class RunEventResponse(ContractModel):
    run_id: uuid.UUID
    status: Literal["applied", "replayed"]


class UsageReportRequest(ContractModel):
    session_key: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    success: bool
    duration_ms: int | None = Field(default=None, ge=0)
    usage: RunUsage


class UsageReportResponse(ContractModel):
    run_id: str
    status: Literal["applied", "replayed", "ignored"]


FOLLOW_UP_TEXT_MAX_BYTES = 32_768
FOLLOW_UP_CAP_PER_HOUR = 3


class FollowUpRequest(ContractModel):
    """A finished async delegation the bridge owes back to the agent that started it."""

    follow_up_id: uuid.UUID
    room_id: uuid.UUID
    agent_id: uuid.UUID
    trigger_message_id: uuid.UUID
    event_kind: Literal["user_request", "room_event"]
    reason: Literal["delegation_completed"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > FOLLOW_UP_TEXT_MAX_BYTES:
            raise ValueError("text exceeds 32768 bytes")
        return value


class FollowUpResponse(ContractModel):
    status: Literal["accepted", "replayed"]


class PaperclipRunRequest(ContractModel):
    """Body Paperclip's own ``http`` adapter POSTs: ``{...payloadTemplate, agentId, runId,
    context}`` (fork ``server/src/adapters/http/execute.ts``). Only the two ids are ours to
    rely on; ``context`` is ``parseObject(run.contextSnapshot)``, a free-form record the fork
    grows at will (``projectId``, ``paperclipWorkspace``, ``paperclipEnvironment``,
    ``paperclipSessionHandoffMarkdown``, …), of which we read two optional keys.

    This is an EXTERNAL shape we do NOT control, so it is deliberately the one contract model
    here that does not forbid extras: the first live Paperclip run on a migrated workspace was
    rejected 422 three times because ``context`` was typed as our three documented keys with
    ``ContractModel``'s ``extra="forbid"``. Strictness on a payload someone else owns turns
    every field THEY add into an outage on OUR side. The ids stay strict — a body missing
    those is genuinely unusable and 422s rather than reaching a KeyError.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    run_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    company_id: str | None = None
    context: dict[str, Any] | None = None


class SteerRunRequest(ContractModel):
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_message_text(value)


class SteerRunResponse(ContractModel):
    run_id: str
    state: Literal["accepted"] = "accepted"


class RuntimeErrorDto(ContractModel):
    code: str
    message: str
    retryable: bool


class RuntimeRpcRequest(ContractModel):
    request_id: uuid.UUID
    method: str = Field(min_length=1, max_length=256)
    params: dict[str, Any]


class RuntimeRpcResponse(ContractModel):
    ok: Literal[True] = True
    payload: Any


class RuntimeOperationAccepted(ContractModel):
    operation_id: uuid.UUID


class RuntimeOperation(ContractModel):
    operation_id: uuid.UUID
    state: Literal["queued", "running", "succeeded", "failed"]
    created_at: datetime
    updated_at: datetime
    result: Any | None
    error: RuntimeErrorDto | None


class LiveConfig(ContractModel):
    schema_version: Literal[1] = 1
    http_endpoint: str
    realtime_endpoint: str
    region: str
    user_channel_key: uuid.UUID
    workspace_id: uuid.UUID


class ApiError(ContractModel):
    code: str
    message: str
    retryable: bool
    reconcile_from_seq: int | None = None
    retry_after: float | None = None
    details: dict[str, Any] | None = None


class LiveEvent(ContractModel):
    schema_version: Literal[1] = 1
    event_id: uuid.UUID
    type: str
    occurred_at: datetime
    room_id: uuid.UUID | None = None
    message_seq: int | None = None
    room_revision: int | None = None
    payload: Any

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in LIVE_EVENT_TYPES:
            raise ValueError("unknown live event type")
        return value


class ApiProblem(Exception):
    """Transport-independent error emitted by messaging/runtime HTTP routes."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        reconcile_from_seq: int | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in API_ERROR_STATUS:
            raise ValueError(f"unknown API error code {code}")
        super().__init__(message)
        self.status_code = API_ERROR_STATUS[code]
        self.error = ApiError(
            code=code,
            message=message,
            retryable=retryable,
            reconcile_from_seq=reconcile_from_seq,
            retry_after=retry_after,
            details=details,
        )

    def body(self) -> dict[str, Any]:
        return self.error.model_dump(mode="json", by_alias=True, exclude_none=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_live_event(
    event_type: str,
    payload: Any,
    *,
    room_id: str | None = None,
    message_seq: int | None = None,
    room_revision: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    event = LiveEvent(
        event_id=uuid.UUID(event_id) if event_id else uuid.uuid4(),
        type=event_type,
        occurred_at=utc_now(),
        room_id=uuid.UUID(room_id) if room_id else None,
        message_seq=message_seq,
        room_revision=room_revision,
        payload=payload,
    )
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def validate_uuid(value: str, *, label: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ApiProblem("INVALID_REQUEST", f"Invalid {label}.") from exc
    if str(parsed) != str(value).lower():
        raise ApiProblem("INVALID_REQUEST", f"Invalid {label}.")
    return str(parsed)


def validate_room_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > ROOM_NAME_MAX_CHARS:
        raise ValueError(f"name must be 1..{ROOM_NAME_MAX_CHARS} characters or null")
    return cleaned


def validate_room_topic(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > ROOM_TOPIC_MAX_CHARS:
        raise ValueError(f"topic exceeds {ROOM_TOPIC_MAX_CHARS} characters")
    return trimmed


def validate_message_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("text must not be empty")
    if len(value.encode("utf-8")) > MESSAGE_TEXT_MAX_BYTES:
        raise ValueError(f"text exceeds {MESSAGE_TEXT_MAX_BYTES} UTF-8 bytes")
    return value


def validate_event_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > EVENT_ID_MAX_BYTES:
        raise ValueError(f"eventId must be 1..{EVENT_ID_MAX_BYTES} UTF-8 bytes")
    if any(ord(char) < 0x20 or char == "/" for char in value):
        raise ValueError("eventId contains an unsupported character")
    return value


def _validate_member_shape(kind: str, role: str, listen_mode: str | None) -> None:
    if kind == "agent" and (role != "member" or listen_mode is None):
        raise ValueError("agent members require role member and a listenMode")
    if kind == "human" and listen_mode is not None:
        raise ValueError("human members require listenMode null")


def _uuid_hex_prefix(member_id: str) -> str:
    # Agent IDs are UUIDs; Clerk user IDs are not, so humans get a stable
    # derived one rather than a ValueError.
    try:
        return uuid.UUID(member_id).hex[:8]
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_OID, member_id).hex[:8]


def _handle_candidate(name: str, member_id: str, *, prefix: str = "agent") -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    candidate = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:32]
    # A name with no ASCII in it at all leaves nothing to derive from, so the
    # fallback has to say what the member IS — a human is not an @agent-1f2e3d4c.
    return candidate or f"{prefix}-{_uuid_hex_prefix(member_id)}"


def _email_local_part(email: str | None) -> str | None:
    """The part before ``@`` -- ``_handle_candidate`` does the rest (lowercase,
    charset, length cap), so ``first.last+tag`` still normalises to a legal
    handle exactly like a display name would."""
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].strip()
    return local or None


def mention_handles(agents: list[dict[str, Any]]) -> dict[str, str]:
    """Derive workspace-unique, deterministic handles over canonical ID order."""
    candidates: dict[str, str] = {}
    by_candidate: dict[str, list[str]] = {}
    for agent in sorted(agents, key=lambda row: row["id"]):
        agent_id = validate_uuid(str(agent["id"]), label="agent ID")
        candidate = _handle_candidate(str(agent.get("name") or agent_id), agent_id)
        candidates[agent_id] = candidate
        by_candidate.setdefault(candidate, []).append(agent_id)
    handles: dict[str, str] = {}
    for agent_id, candidate in candidates.items():
        colliders = by_candidate[candidate]
        if len(colliders) == 1:
            handles[agent_id] = candidate
        else:
            handles[agent_id] = f"{candidate[:23]}-{_uuid_hex_prefix(agent_id)}"
    return handles


def with_human_handles(members: list[dict[str, Any]], emails: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Room members with every human carrying a mention handle.

    The candidate is the local part of the human's email (``prasiddha@gmail.com``
    -> ``prasiddha``) when ``emails`` has one for them; a human absent from
    ``emails`` (no email on record) falls back to today's display-name
    derivation. Agent handles stay workspace-derived and never move to make
    room for a human; humans claim their candidate in join order, falling back
    to the same collision suffix agents use. Join order is what keeps a
    sitting member's handle: a later "Aden" is the one that gets suffixed, so a
    stored ``@aden`` never silently re-points at a newcomer.

    ponytail: derived per room, per read. One person can read as ``@aden`` here
    and ``@aden-1f2e3d4c`` next door, and an email change, display-name rename,
    or departure still moves handles under already-stored text. Upgrade path:
    persist the human's handle on the member row and re-stamp it exactly as
    agents do through ``messaging_repo.upsert_member(handle_updates=...)``.
    """
    emails = emails or {}
    taken = {str(row["mentionHandle"]) for row in members if row.get("kind") == "agent" and row.get("mentionHandle")}
    handles: dict[str, str] = {}
    humans = sorted(
        (row for row in members if row.get("kind") == "human"),
        key=lambda row: (int(row.get("joinedSeq") or 0), str(row["id"])),
    )
    for row in humans:
        user_id = str(row["id"])
        source = _email_local_part(emails.get(user_id)) or str(row.get("displayName") or user_id)
        candidate = _handle_candidate(source, user_id, prefix="user")
        if candidate in taken:
            candidate = f"{candidate[:23]}-{_uuid_hex_prefix(user_id)}"
        taken.add(candidate)
        handles[user_id] = candidate
    return [{**row, "mentionHandle": handles[str(row["id"])]} if row.get("kind") == "human" else row for row in members]


async def human_emails(members: list[dict[str, Any]]) -> dict[str, str]:
    """Email addresses of every human member, keyed by user id, for
    :func:`with_human_handles`. Reads the users table -- never Clerk, so this
    stays safe on a per-message or per-room-read path. A missing row, a row
    with no email column, or a failed lookup is simply absent from the
    result -- the caller's handle derivation falls back to the display name
    for those rather than failing the read or dropping the member."""
    from core.repositories import user_repo

    human_ids = sorted({str(row["id"]) for row in members if row.get("kind") == "human"})
    if not human_ids:
        return {}

    async def _lookup(user_id: str) -> dict | None:
        try:
            return await user_repo.get(user_id)
        except Exception:
            logger.warning("human_emails lookup failed", extra={"user_id": user_id}, exc_info=True)
            return None

    rows = await asyncio.gather(*(_lookup(user_id) for user_id in human_ids))
    return {user_id: row["email"] for user_id, row in zip(human_ids, rows) if row and row.get("email")}


def canonical_message(item: dict[str, Any], *, original_request: bool = False) -> CanonicalMessage:
    # Runtime ingress alone retains the dispatched request. Public projections
    # never serialize the original text/attachments of a tombstone.
    deleted = bool(item.get("deletedAt")) and not original_request
    edited = "editedText" in item and not original_request
    return CanonicalMessage(
        message_id=item["messageId"],
        room_id=item["roomId"],
        message_seq=int(item["messageSeq"]),
        room_revision=int(item["roomRevision"]),
        author=item["author"],
        text="" if deleted else item["editedText"] if edited else item["text"],
        mentions=[] if deleted or edited else item.get("mentions") or [],
        reply_to_message_id=item.get("replyToMessageId"),
        thread_root_message_id=item.get("threadRootMessageId"),
        created_at=item["createdAt"],
        run_id=item.get("runId"),
        attachments=[] if deleted else item.get("attachments") or [],
        reactions={} if deleted else item.get("reactions") or {},
        reaction_revision=int(item.get("reactionRevision", 0)),
        message_revision=0 if original_request else int(item.get("messageRevision", 0)),
        edited_at=None if original_request else item.get("editedAt"),
        deleted_at=None if original_request else item.get("deletedAt"),
    )


_ATTACHMENT_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/gif": ((b"GIF87a", b"GIF89a"), ".gif"),
    "image/webp": (b"RIFF", ".webp"),
    "application/pdf": (b"%PDF-", ".pdf"),
    # Local file, empty archive, and spanned archive signatures. No archive
    # parsing follows this bounded signature check.
    "application/zip": ((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"), ".zip"),
    # XLSX is an OOXML ZIP container. Hermes's read_file performs the workbook
    # extraction; this boundary only verifies the bounded container signature.
    XLSX_CONTENT_TYPE: ((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"), ".xlsx"),
}


def canonical_attachment_content_type(file_name: str, content_type: str) -> str:
    declared = content_type.lower().split(";", 1)[0].strip()
    suffix = PurePath(file_name).suffix.lower()
    zip_name = suffix == ".zip"
    xlsx_name = suffix == ".xlsx"
    if declared in {"application/zip", "application/x-zip-compressed"}:
        if not zip_name:
            raise ValueError("ZIP attachments must use a .zip filename")
        return "application/zip"
    if zip_name:
        if declared in {"", "application/octet-stream"}:
            return "application/zip"
        raise ValueError("attachment filename extension does not match the declared content type")
    if declared == XLSX_CONTENT_TYPE:
        if not xlsx_name:
            raise ValueError("XLSX attachments must use a .xlsx filename")
        return XLSX_CONTENT_TYPE
    if xlsx_name:
        if declared in {"", "application/octet-stream"}:
            return XLSX_CONTENT_TYPE
        raise ValueError("attachment filename extension does not match the declared content type")
    return declared


def validate_attachment_bytes(content_type: str, data: bytes) -> str:
    expected = _ATTACHMENT_SIGNATURES.get(content_type)
    cap = ATTACHMENT_MAX_BYTES_BY_TYPE.get(content_type)
    if not expected or not cap or not data or len(data) > cap:
        raise ValueError(
            "attachment must be PNG, JPEG, WebP, GIF, PDF, ZIP, or XLSX, and at most 5 MiB for images or 20 MiB for documents"
        )
    signature, suffix = expected
    signatures = signature if isinstance(signature, tuple) else (signature,)
    valid = any(data.startswith(value) for value in signatures)
    if content_type == "image/webp":
        valid = valid and len(data) >= 12 and data[8:12] == b"WEBP"
    if not valid:
        raise ValueError("attachment bytes do not match the declared content type")
    return suffix


def attachment_key(owner_id: str, room_id: str, name: str) -> str:
    """The S3 key for one attachment object, under the owner's Hermes home.

    ``<owner>/hermes/attachments/<roomId>/<name>`` -- a sibling of the per-agent profile
    prefixes and the root ``auth.json``, so an account wipe's
    ``teardown_hermes_runtime(delete_profiles=True)`` (which deletes everything under
    ``<owner>/hermes/``) takes the attachments with it, and the per-owner IAM grant
    (``<owner>/*``) already covers it. Every path segment is a validated UUID, so there is
    nothing to escape with.
    """
    return f"{hermes_profiles.root_home_prefix(owner_id)}attachments/{uuid.UUID(room_id)}/{name}"


def store_attachment(owner_id: str, room_id: str, file_name: str, content_type: str, data: bytes) -> MessageAttachment:
    content_type = canonical_attachment_content_type(file_name, content_type)
    suffix = validate_attachment_bytes(content_type, data)
    attachment_id = uuid.uuid4()
    safe_name = "".join(
        char for char in PurePath(file_name).name if ord(char) >= 0x20 and ord(char) != 0x7F and char not in "/\\"
    ).strip()
    safe_name = safe_name.encode("utf-8")[:255].decode("utf-8", "ignore") or f"attachment{suffix}"
    attachment = MessageAttachment(id=attachment_id, file_name=safe_name, content_type=content_type, size=len(data))
    hermes_profiles.put_owner_object(attachment_key(owner_id, room_id, f"{attachment_id}{suffix}"), data)
    hermes_profiles.put_owner_object(
        attachment_key(owner_id, room_id, f"{attachment_id}.json"),
        attachment.model_dump_json(by_alias=True).encode(),
    )
    return attachment


def load_attachment(owner_id: str, room_id: str, attachment_id: str) -> tuple[MessageAttachment, bytes]:
    normalized = str(uuid.UUID(attachment_id))
    try:
        raw = hermes_profiles.get_owner_object(attachment_key(owner_id, room_id, f"{normalized}.json"))
        if raw is None:
            raise ValueError("attachment metadata not found")
        metadata = MessageAttachment.model_validate_json(raw)
        if str(metadata.id) != normalized:
            raise ValueError("attachment metadata mismatch")
        suffix = _ATTACHMENT_SIGNATURES[metadata.content_type][1]
        data = hermes_profiles.get_owner_object(attachment_key(owner_id, room_id, f"{normalized}{suffix}"))
        if data is None:
            raise ValueError("attachment bytes not found")
    except (OSError, ValueError, KeyError) as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Attachment not found.") from exc
    try:
        validate_attachment_bytes(metadata.content_type, data)
        if len(data) != metadata.size:
            raise ValueError("attachment size mismatch")
    except ValueError as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Attachment is invalid.") from exc
    return metadata, data


def runtime_operation(item: dict[str, Any]) -> RuntimeOperation:
    return RuntimeOperation(
        operation_id=item["operationId"],
        state=item["state"],
        created_at=item["createdAt"],
        updated_at=item["updatedAt"],
        result=item.get("result"),
        error=item.get("error"),
    )


async def load_agent_roster(owner_id: str) -> list[dict[str, Any]]:
    """Read the current canonical Paperclip/OpenClaw roster."""
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner

    try:
        rows = await fetch_paperclip_agents_for_owner(
            owner_id=owner_id,
            repo=PaperclipRepo(table_name="paperclip-companies"),
            admin_client=shared_admin_client(),
            raise_on_unavailable=True,
        )
    except Exception as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The agent roster is temporarily unavailable.",
            retryable=True,
            retry_after=3,
        ) from exc
    roster: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") == "terminated":
            continue
        try:
            agent_id = validate_uuid(str(row.get("id", "")), label="agent ID")
        except ApiProblem as exc:
            raise ApiProblem("INTERNAL_ERROR", "The agent roster contains an invalid identity.") from exc
        roster.append({**row, "id": agent_id, "name": str(row.get("name") or agent_id)[:256]})
    roster.sort(key=lambda row: row["id"])
    return roster


# A human's display name is fetched from Clerk, which is an HTTPS call with its
# own TLS handshake. It was being made on EVERY message send and EVERY typing
# ping -- two of the three hottest paths on the surface -- for a value that
# changes only when someone edits their Clerk profile. Cached per process (the
# backend runs desiredCount: 1), short enough that a rename shows up on its own.
_DISPLAY_NAME_TTL_SECONDS = 300.0
_display_names: dict[str, tuple[float, str]] = {}


async def human_display_name(user_id: str) -> str:
    cached = _display_names.get(user_id)
    now = time.monotonic()
    if cached and now - cached[0] < _DISPLAY_NAME_TTL_SECONDS:
        return cached[1]
    resolved = await _resolve_human_display_name(user_id)
    # A failed lookup falls back to the user id. Caching that would pin an
    # opaque id onto every message for the whole TTL, so only cache real names.
    if resolved != user_id:
        _display_names[user_id] = (now, resolved)
    return resolved


async def _resolve_human_display_name(user_id: str) -> str:
    from core.services.clerk_admin import get_user

    user = await get_user(user_id)
    if not user:
        return user_id
    name = " ".join(
        part.strip() for part in (user.get("first_name") or "", user.get("last_name") or "") if part.strip()
    )
    if name:
        return name[:256]
    if user.get("username"):
        return str(user["username"])[:256]
    emails = user.get("email_addresses") or []
    if emails and emails[0].get("email_address"):
        return str(emails[0]["email_address"]).split("@", 1)[0][:256]
    return user_id


async def _touch_last_interactive_quietly(owner_id: str) -> None:
    """Bump the owner's idle clock off the send path. A failure here must never
    surface as a failed send -- the message is already durable by this point."""
    try:
        await container_repo.touch_last_interactive(owner_id)
    except Exception:
        logger.warning("touch_last_interactive failed", extra={"owner_id": owner_id}, exc_info=True)


async def _validate_human_in_owner(owner_id: str, user_id: str) -> None:
    if owner_id == user_id:
        return
    if not owner_id.startswith("org_"):
        raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")
    from core.services.clerk_admin import list_user_organizations

    if not any(org.get("id") == owner_id for org in await list_user_organizations(user_id, limit=100)):
        raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")


async def _resolved_member(owner_id: str, member: CreateRoomMember, roster: list[dict]) -> dict:
    if member.kind == "human":
        await _validate_human_in_owner(owner_id, member.id)
        return {
            "kind": "human",
            "id": member.id,
            "displayName": await human_display_name(member.id),
            "mentionHandle": None,
            "role": member.role,
            "listenMode": None,
        }
    agents = {row["id"]: row for row in roster}
    row = agents.get(member.id)
    if row is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "That agent is not in this workspace.")
    handles = mention_handles(roster)
    return {
        "kind": "agent",
        "id": member.id,
        "displayName": row["name"],
        "mentionHandle": handles[member.id],
        "role": "member",
        "listenMode": member.listen_mode,
    }


async def listening_available(owner_id: str) -> bool:
    """Listening (``active``) is an always-on feature — an agent answering
    traffic nobody addressed to it — so it rides the paid predicate. A
    free-tier room's agents reply when mentioned."""
    return has_always_on_entitlement(await billing_repo.get_by_owner_id(owner_id))


async def _require_listening(owner_id: str, room_kind: str | None, members: list[Any]) -> None:
    # Every DM message is addressed, so the mode is inert there — and every
    # DM is created with ``active`` (packages/shared useRooms.selectDm).
    if room_kind == "dm":
        return
    if any(m.kind == "agent" and m.listen_mode == "active" for m in members) and not await listening_available(
        owner_id
    ):
        raise ApiProblem("PLAN_REQUIRED", "Listening is part of the paid plan. Free-tier agents reply when mentioned.")


async def require_room_member(owner_id: str, user_id: str, room_id: str) -> tuple[dict, dict]:
    room_id = validate_uuid(room_id, label="room ID")
    meta, member = await asyncio.gather(
        messaging_repo.get_room(room_id),
        messaging_repo.get_member(room_id, "human", user_id),
    )
    if meta is None:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if member is not None and messaging_repo.member_home(member, meta["ownerId"]) == owner_id:
        return meta, member
    # A human of the room's own workspace who lost access keeps the reconcile hint;
    # everyone else (a stranger, or a foreign member viewing from the wrong org) sees nothing.
    if member is None and meta.get("ownerId") == owner_id:
        raise ApiProblem(
            "ROOM_FORBIDDEN",
            "You no longer have access to this room.",
            reconcile_from_seq=int(meta.get("lastSeq", 0)),
        )
    raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")


async def require_room_admin(owner_id: str, user_id: str, room_id: str) -> tuple[dict, dict]:
    meta, member = await require_room_member(owner_id, user_id, room_id)
    if member.get("role") not in {"owner", "admin"}:
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    return meta, member


def _room_member(item: dict[str, Any]) -> RoomMember:
    return RoomMember(
        kind=item["kind"],
        id=item["id"],
        display_name=item["displayName"],
        mention_handle=item.get("mentionHandle"),
        role=item["role"],
        listen_mode=item.get("listenMode"),
        joined_at=item["joinedAt"],
        joined_seq=int(item["joinedSeq"]),
    )


def _room_summary_fields(meta: dict, member: dict, last_read: int) -> dict[str, Any]:
    """The RoomSummary half of a room projection — RoomDetail extends it."""
    floor = max(last_read, int(member.get("joinedSeq", 0)))
    return {
        "room_id": meta["roomId"],
        "name": meta.get("name"),
        "kind": meta["kind"],
        "room_revision": int(meta["revision"]),
        "last_message_seq": int(meta["lastSeq"]),
        "last_message_at": meta.get("lastMessageAt"),
        "unread_count": max(0, int(meta["lastSeq"]) - floor),
        "updated_at": meta["updatedAt"],
        "starred": bool(member.get("starred")),
        "muted": bool(member.get("muted")),
        "section_id": member.get("sectionId"),
        "topic": meta.get("topic"),
        "workspace_id": meta.get("workspaceId"),
        "shared": is_vendor_room(meta),
        "is_default": is_default_room(meta),
        "archived_at": meta.get("archivedAt"),
        "archived_by": meta.get("archivedBy"),
    }


async def _room_detail(meta: dict, user_id: str, member: dict | None = None) -> RoomDetail:
    member = member or await messaging_repo.get_member(meta["roomId"], "human", user_id)
    if member is None:
        raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.")
    members, last_read, open_runs = await asyncio.gather(
        messaging_repo.list_members(meta["roomId"]),
        messaging_repo.get_read_cursor(meta["roomId"], user_id),
        # The run ledger is partitioned by the ROOM's owner — the customer org
        # even on a room shared with the vendor workspace.
        messaging_repo.list_open_runs_for_room(meta["ownerId"], meta["roomId"]),
    )
    emails = await human_emails(members)
    agent_ids = {row["id"] for row in members if row["kind"] == "agent"}
    return RoomDetail(
        **_room_summary_fields(meta, member, last_read),
        delivery_key=meta["deliveryKey"],
        members=[_room_member(row) for row in with_human_handles(members, emails)],
        last_read_seq=last_read,
        created_at=meta["createdAt"],
        pinned_message_ids=[str(value) for value in meta.get("pinnedMessageIds", [])],
        # A run whose agent has left the room is as over as one that never
        # existed — the rule GET /messaging/runs/{id} already applies.
        active_runs=[
            ActiveRun(
                run_id=row["runId"],
                agent_id=row["agentId"],
                trigger_message_id=row.get("triggerMessageId"),
                started_at=row.get("startedAt"),
                last_heartbeat_at=row.get("lastHeartbeatAt"),
            )
            for row in open_runs
            if row["agentId"] in agent_ids
        ],
    )


async def _user_sections(owner_id: str, user_id: str) -> list[RoomSection]:
    prefs = await messaging_repo.get_user_prefs(owner_id, user_id)
    return [RoomSection(id=row["id"], name=row["name"]) for row in (prefs or {}).get("sections", [])]


# Rooms summarised at once. Every repo call is a thread hop onto the default
# executor (~6 threads on a 2-vCPU task), so an unbounded gather over a large
# workspace would flood it and leave every other request on the process queued
# behind a single room list.
#
# Note this bounds ROOMS, and each one issues three calls concurrently — so the
# real ceiling on submitted work is three times this number. That is deliberate:
# the calls are IO-bound and mostly sit waiting on DynamoDB, so a modest queue
# behind the pool costs nothing, whereas the serial version this replaced cost
# two full round trips per room in sequence.
_SUMMARY_CONCURRENCY = 8


async def _room_summaries(owner_id: str, user_id: str) -> list[RoomSummary]:
    """Every room the caller can see, with the members each row needs to name
    itself.

    The per-room reads run concurrently. They used to run serially -- two
    awaited round trips per room, so a 20-room workspace was a 40-deep serial
    chain before the response could start. The membership recheck stays: the
    GSI backing ``list_rooms_for_member`` is eventually consistent, so a member
    removed a moment ago can still be discovered by it, and ``get_member`` is
    the strongly-consistent read that keeps them out of the response.
    """
    discovered = await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def summarise(row: dict) -> RoomSummary | None:
        meta = row["meta"]
        room_id = meta["roomId"]
        async with limit:
            member, last_read, members = await asyncio.gather(
                messaging_repo.get_member(room_id, "human", user_id),
                messaging_repo.get_read_cursor(room_id, user_id),
                messaging_repo.list_members(room_id),
            )
            if member is None:
                return None
            emails = await human_emails(members)
        return RoomSummary(
            **_room_summary_fields(meta, member, last_read),
            members=[_room_member(item) for item in with_human_handles(members, emails)],
            last_read_seq=last_read,
        )

    settled = await asyncio.gather(*(summarise(row) for row in discovered))
    summaries = [summary for summary in settled if summary is not None]
    summaries.sort(key=lambda room: room.updated_at, reverse=True)
    return summaries


async def list_rooms(auth: AuthContext) -> RoomListResponse:
    owner_id = resolve_owner_id(auth)
    summaries = await _room_summaries(owner_id, auth.user_id)
    return RoomListResponse(rooms=summaries, sections=await _user_sections(owner_id, auth.user_id))


async def put_sections(auth: AuthContext, request: SectionsRequest) -> RoomListResponse:
    """Replace the caller's ordered section list. Sections are per-user view
    state; a room's ``sectionId`` is not validated against them, so a deleted
    section simply drops its rooms back into their default group."""
    owner_id = resolve_owner_id(auth)
    stored = await messaging_repo.put_user_sections(
        owner_id,
        auth.user_id,
        [{"id": str(section.id), "name": section.name} for section in request.sections],
    )
    summaries = await _room_summaries(owner_id, auth.user_id)
    return RoomListResponse(rooms=summaries, sections=[RoomSection(id=row["id"], name=row["name"]) for row in stored])


def default_room_id(owner_id: str) -> str:
    """Stable identity for the one fresh-workspace general room."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-default-room|{owner_id}"))


def is_vendor_room(meta: dict) -> bool:
    """The one channel a customer shares with the vendor org — by id, never by a stored flag."""
    return bool(settings.VENDOR_ORG_ID) and meta.get("roomId") == messaging_repo.vendor_room_id(
        meta.get("ownerId") or ""
    )


def is_default_room(meta: dict) -> bool:
    """The seeded starter channel — by id, never by name.

    ``DEFAULT_ROOM_NAME`` is only the name it is CREATED with; an owner can
    rename it, so matching on "general" would follow the rename onto a room
    that is no longer the starter channel (and off the one that is). Clients
    use this to place the onboarding setup band in exactly one room.
    """
    owner_id = meta.get("ownerId") or ""
    return bool(owner_id) and str(meta.get("roomId")) == default_room_id(owner_id)


def _deny_vendor_human_change(meta: dict, kind: str) -> None:
    if kind == "human" and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "Membership of this channel is managed by Lightbulb.")


def _emit_room_created(owner_id: str, actor_user_id: str, kind: str, added: list[dict], *, by: str) -> None:
    """``room_created`` plus one ``room_member_added`` per member joining with it.

    ``by`` is the actor's standing, not the member's: ``system`` for the seeded
    starter room, otherwise the adder's room role.
    """
    _product_event(distinct_id=actor_user_id, event="room_created", owner_id=owner_id, props={"kind": kind})
    for member in added:
        _emit_member_added(owner_id, actor_user_id, member.get("kind"), by=by)


def _emit_member_added(owner_id: str, actor_user_id: str, member_kind: str | None, *, by: str) -> None:
    _product_event(
        distinct_id=actor_user_id,
        event="room_member_added",
        owner_id=owner_id,
        props={"member_kind": member_kind, "by": by},
    )


async def join_default_room(owner_id: str, user_id: str) -> bool:
    """Seat a workspace teammate in the starter room, at most once per (room, user).

    ``ensure_default_room`` seats only the creator. The ``AUTOJOIN#`` marker is claimed before
    the add and never cleared on success, so a member a room admin later removes is never
    restored (reconciliation never undoes a removal). Raises on real errors; callers wrap it.
    """
    room_id = default_room_id(owner_id)
    if await messaging_repo.get_member(room_id, "human", user_id) is not None:
        return False
    if await messaging_repo.get_room(room_id) is None:
        return False
    member = await _resolved_member(
        owner_id, CreateRoomMember(kind="human", id=user_id, role="member", listen_mode=None), []
    )
    if not await messaging_repo.claim_autojoin(room_id, user_id):
        return False
    try:
        await messaging_repo.ensure_user_stream(owner_id, user_id)
        meta = await messaging_repo.upsert_member(room_id=room_id, owner_id=owner_id, member=member)
    except messaging_repo.MemberLimitExceeded:
        # Keep the marker: a full room must not turn every poll into a retry.
        logger.warning("default room full; %s not added for owner %s", user_id, owner_id)
        return False
    except Exception:
        await messaging_repo.release_autojoin(room_id, user_id)
        raise
    _emit_member_added(owner_id, user_id, "human", by="system")
    await _publish_room_metadata(meta)
    return True


async def ensure_default_room(owner_id: str, owner_user_id: str) -> str | None:
    """Create the starter general room once, never reconcile it afterward."""
    from core.repositories.paperclip_repo import PaperclipRepo

    room_id = default_room_id(owner_id)
    existing_room = await messaging_repo.get_room(room_id)
    if existing_room is not None and int(existing_room.get("lastSeq") or 0) > 0:
        return room_id

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    starter_ids = getattr(company, "starter_agent_ids", None) if company else None
    if (
        getattr(company, "status", None) != "active"
        or not isinstance(starter_ids, dict)
        or set(starter_ids) != set(_STARTER_KEYS)
        or len(set(starter_ids.values())) != len(_STARTER_KEYS)
    ):
        return None

    roster = await load_agent_roster(owner_id)
    agents = {row["id"]: row for row in roster}
    if any(agent_id not in agents for agent_id in starter_ids.values()):
        return None
    handles = mention_handles(roster)
    ceo_mode = "active" if await listening_available(owner_id) else "mentions"
    members = [
        {
            "kind": "human",
            "id": owner_user_id,
            "displayName": await human_display_name(owner_user_id),
            "mentionHandle": None,
            "role": "owner",
            "listenMode": None,
        }
    ]
    members.extend(
        {
            "kind": "agent",
            "id": starter_ids[key],
            "displayName": agents[starter_ids[key]]["name"],
            "mentionHandle": handles[starter_ids[key]],
            "role": "member",
            "listenMode": ceo_mode,
        }
        for key in _STARTER_KEYS
    )
    client_room_id = f"workspace-bootstrap-v2:{owner_id}"
    if existing_room is None:
        await messaging_repo.ensure_user_stream(owner_id, owner_user_id)
        normalized = [{key: member.get(key) for key in ("kind", "id", "role", "listenMode")} for member in members]
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=client_room_id,
            creator_user_id=owner_user_id,
            name=DEFAULT_ROOM_NAME,
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash(DEFAULT_ROOM_NAME, "channel", normalized),
        )
        # members[0] is the owner themselves — the creator is not "added".
        _emit_room_created(owner_id, owner_user_id, "channel", members[1:], by="system")
    ceo_id = starter_ids["ceo"]
    await messaging_repo.create_message(
        room_id=room_id,
        owner_id=owner_id,
        idemp_pk=messaging_repo.room_pk(room_id),
        idemp_sk=messaging_repo.human_idemp_sk("__system__", f"{client_room_id}:welcome"),
        req_hash=messaging_repo.request_hash(DEFAULT_ROOM_WELCOME, ceo_id),
        author={"kind": "agent", "id": ceo_id, "displayName": agents[ceo_id]["name"]},
        text=DEFAULT_ROOM_WELCOME,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=ceo_id,
        client_message_id=None,
    )
    return room_id


async def promote_listening(owner_id: str) -> None:
    """Turn Listening on for every agent when a trial workspace upgrades to paid.

    A room seeded on the free tier keeps its agents on ``mentions`` forever
    (the seed runs once), so this sweeps every room the owner has. Promote
    ONLY an exact ``mentions`` set by nobody but the system — never a user's
    ``observe``, an admin's ``active``, or a ``mentions`` an admin chose
    (``modeSetBy == "user"``). DMs are skipped (every DM message is already
    addressed), and so is any room shared with another workspace — the vendor
    room, or a room carrying a foreign member — since that room follows
    someone else's policy, not this owner's upgrade. Idempotent; the caller
    gates on the transition into paid. Best-effort per room: a room that
    fails to promote is logged and the rest still run."""
    for room_id in await messaging_repo.list_owner_room_ids(owner_id):
        try:
            meta = await messaging_repo.get_room(room_id)
            if meta is None or meta.get("kind") == "dm" or meta.get("ownerId") != owner_id or is_vendor_room(meta):
                continue
            members = await messaging_repo.list_members(room_id)
            if any(messaging_repo.member_home(m, owner_id) != owner_id for m in members):
                continue
            changed_meta = None
            for member in members:
                if (
                    member.get("kind") != "agent"
                    or member.get("listenMode") != "mentions"
                    or member.get("modeSetBy") == "user"
                ):
                    continue
                changed_meta = await messaging_repo.upsert_member(
                    room_id=room_id, owner_id=owner_id, member={**member, "listenMode": "active"}
                )
            if changed_meta is not None:
                await _publish_room_metadata(changed_meta)
        except Exception:
            logger.exception("listening promotion failed for owner %s room %s", owner_id, room_id)


def _display_name_from_membership(row: dict) -> str:
    """Same precedence as ``_resolve_human_display_name``, from the Clerk membership row
    we already hold — one roster read instead of one ``get_user`` per human."""
    name = " ".join(part.strip() for part in (row.get("first_name") or "", row.get("last_name") or "") if part.strip())
    if name:
        return name[:256]
    email = row.get("email") or ""
    return (email.split("@", 1)[0] if email else row["user_id"])[:256]


def _vendor_room_human(row: dict, home_owner_id: str, *, role: str = "member") -> dict:
    return {
        "kind": "human",
        "id": row["user_id"],
        "displayName": _display_name_from_membership(row),
        "mentionHandle": None,
        "role": role,
        "listenMode": None,
        "homeOwnerId": home_owner_id,
    }


async def ensure_vendor_room(owner_id: str) -> str | None:
    """Create the customer's channel shared with the vendor org, once (spec §4).

    Runs on every org-context poll; the steady-state cost is one consistent
    ``get_room``. Creation waits for an ACTIVE Paperclip company so a
    fresh workspace gets its CEO in the room. Returns the room id when it exists."""
    vendor = settings.VENDOR_ORG_ID
    if not vendor or owner_id == vendor or not is_org_owner_id(owner_id):
        return None
    room_id = messaging_repo.vendor_room_id(owner_id)
    if await messaging_repo.get_room(room_id) is not None:
        return room_id

    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.clerk_admin import get_organization, list_organization_members

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return None
    org = await get_organization(owner_id)
    customer_humans = await list_organization_members(owner_id)
    vendor_humans = await list_organization_members(vendor)
    # list_organization_members returns [] on any Clerk failure; a half roster must never seed.
    if org is None or not customer_humans or not vendor_humans:
        return None
    by_id = {row["user_id"]: row for row in customer_humans}
    owner_user_id = (
        org.get("created_by")
        if org.get("created_by") in by_id
        else next((uid for uid in sorted(by_id) if by_id[uid].get("role") == "org:admin"), None)
    )
    if owner_user_id is None:
        return None
    # The platform's own company slug (company hosts, agent mail) is the readable one;
    # Clerk org slugs here carry a timestamp suffix ("acme-1789347059550170402").
    from core.repositories import company_slug_repo

    slug_row = await company_slug_repo.get_by_owner(owner_id)
    slug = (slug_row or {}).get("slug") or re.sub(r"-\d{10,}$", "", org.get("slug") or "")
    try:
        name = validate_room_name(f"lightbulb-{slug}".rstrip("-"))
    except ValueError:
        name = "lightbulb"

    members = [_vendor_room_human(by_id[owner_user_id], owner_id, role="owner")]
    ceo_id = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    if ceo_id:
        roster = await load_agent_roster(owner_id)
        agents = {row["id"]: row for row in roster}
        if ceo_id in agents:
            members.append(
                {
                    "kind": "agent",
                    "id": ceo_id,
                    "displayName": agents[ceo_id]["name"],
                    "mentionHandle": mention_handles(roster)[ceo_id],
                    "role": "member",
                    "listenMode": "mentions",
                }
            )
    members.extend(_vendor_room_human(by_id[uid], owner_id) for uid in sorted(by_id) if uid != owner_user_id)
    seen = {m["id"] for m in members}
    members.extend(
        _vendor_room_human(row, vendor)
        for row in sorted(vendor_humans, key=lambda r: r["user_id"])
        if row["user_id"] not in seen
    )
    if len(members) > messaging_repo.MAX_ROOM_MEMBERS:
        # ponytail: 32-member cap; raise MAX_ROOM_MEMBERS when a customer org exceeds ~25 humans
        put_metric("messaging.vendor_room.capped")
        logger.warning("vendor room capped at %d members for owner %s", messaging_repo.MAX_ROOM_MEMBERS, owner_id)
        members = members[: messaging_repo.MAX_ROOM_MEMBERS]
    for human in (m for m in members if m["kind"] == "human"):
        await messaging_repo.ensure_user_stream(human["homeOwnerId"], human["id"])
    try:
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"vendor-room-v1:{owner_id}",
            creator_user_id=owner_user_id,
            name=name,
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash(name, "channel", [(m["kind"], m["id"]) for m in members]),
        )
    except messaging_repo.IdempotencyConflict:
        return room_id  # a concurrent poll created it with a different roster; it exists
    _emit_room_created(owner_id, owner_user_id, "channel", members[1:], by="system")
    return room_id


async def add_vendor_room_member(owner_id: str, user_id: str, *, home_owner_id: str) -> bool:
    """Add one human to a customer's vendor room, keyed on THEIR home. Idempotent.

    Retries up to 3 attempts on a lost compare-and-swap race on the room's
    revision (``upsert_member`` raises ``ClientError`` on a cancelled
    transaction): each attempt re-reads room + member fresh, so a concurrent
    add that already landed is a clean no-op rather than a dropped join."""
    room_id = messaging_repo.vendor_room_id(owner_id)
    await messaging_repo.ensure_user_stream(home_owner_id, user_id)
    for attempt in range(3):
        meta = await messaging_repo.get_room(room_id)
        if meta is None or await messaging_repo.get_member(room_id, "human", user_id) is not None:
            return False
        member = {
            "kind": "human",
            "id": user_id,
            "displayName": await human_display_name(user_id),
            "mentionHandle": None,
            "role": "member",
            "listenMode": None,
            "homeOwnerId": home_owner_id,
        }
        try:
            meta = await messaging_repo.upsert_member(room_id=room_id, owner_id=owner_id, member=member)
        except messaging_repo.MemberLimitExceeded:
            put_metric("messaging.vendor_room.capped")
            logger.warning("vendor room full; %s not added to owner %s", user_id, owner_id)
            return False
        except ClientError as exc:
            if messaging_repo._is_cancelled(exc) and attempt < 2:
                continue
            raise
        await _publish_room_metadata(meta)
        return True
    return False  # pragma: no cover — loop always returns or raises within 3 attempts


async def on_org_member_joined(org_id: str, user_id: str) -> None:
    """Clerk ``organizationMembership.created``: keep 'everyone in both workspaces' true."""
    vendor = settings.VENDOR_ORG_ID
    if not vendor:
        return
    if org_id != vendor:
        await add_vendor_room_member(org_id, user_id, home_owner_id=org_id)
        return
    # ponytail: full containers scan per vendor join; add a VENDORROOM# catalog row when org count makes this slow
    for row in await container_repo.scan_all(consistent=True):
        customer = row.get("owner_id")
        if isinstance(customer, str) and is_org_owner_id(customer) and customer != vendor:
            try:
                await add_vendor_room_member(customer, user_id, home_owner_id=vendor)
            except Exception:
                logger.exception("vendor room add failed for owner %s user %s", customer, user_id)
                put_metric("messaging.vendor_room.error")


async def create_room(auth: AuthContext, request: CreateRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    keys = [(member.kind, member.id) for member in request.members]
    if len(keys) != len(set(keys)) or ("human", auth.user_id) in keys:
        raise ApiProblem("INVALID_REQUEST", "Room members must be unique and omit the creator.")
    if len(request.members) + 1 > messaging_repo.MAX_ROOM_MEMBERS:
        raise ApiProblem("MEMBER_LIMIT", f"Rooms hold at most {messaging_repo.MAX_ROOM_MEMBERS} members.")
    await _require_listening(owner_id, request.kind, request.members)
    roster = await load_agent_roster(owner_id) if any(member.kind == "agent" for member in request.members) else []
    members = [
        {
            "kind": "human",
            "id": auth.user_id,
            "displayName": await human_display_name(auth.user_id),
            "mentionHandle": None,
            "role": "owner",
            "listenMode": None,
        }
    ]
    for member in request.members:
        members.append(await _resolved_member(owner_id, member, roster))
    for human in (member for member in members if member["kind"] == "human"):
        await messaging_repo.ensure_user_stream(owner_id, human["id"])
    normalized = [{key: member.get(key) for key in ("kind", "id", "role", "listenMode")} for member in members[1:]]
    room_id = messaging_repo.deterministic_room_id(owner_id, auth.user_id, str(request.client_room_id))
    req_hash = messaging_repo.request_hash(request.name, request.kind, normalized)
    try:
        meta = await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=str(request.client_room_id),
            creator_user_id=auth.user_id,
            name=request.name,
            kind=request.kind,
            members=members,
            req_hash=req_hash,
        )
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "clientRoomId was already used with a different request.") from exc
    except messaging_repo.MemberLimitExceeded as exc:
        raise ApiProblem("MEMBER_LIMIT", str(exc)) from exc
    _emit_room_created(owner_id, auth.user_id, request.kind, members[1:], by="owner")
    return await _room_detail(meta, auth.user_id)


async def get_room(auth: AuthContext, room_id: str) -> RoomDetail:
    meta, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    return await _room_detail(meta, auth.user_id, member)


async def patch_room(auth: AuthContext, room_id: str, request: PatchRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_admin(owner_id, auth.user_id, room_id)
    updates = request.named_updates()
    # A DM's display name is derived from its counterpart; a stored name would
    # shadow it with nothing in the UI able to clear it. (Finding from #2483,
    # merged to main — this generalizes the same guard to updates-scoping.)
    if "name" in updates and meta.get("kind") == "dm":
        raise ApiProblem("ROOM_FORBIDDEN", "Direct messages cannot be renamed.")
    if "name" in updates and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "This channel's name is managed by Lightbulb.")
    if request.archived is not None and request.archived != bool(meta.get("archivedAt")):
        if request.archived:
            _require_archivable(meta)
            updates |= {"archivedAt": utc_now().isoformat(), "archivedBy": auth.user_id}
        else:
            updates |= {"archivedAt": None, "archivedBy": None}
    if not updates:
        return await _room_detail(meta, auth.user_id)
    try:
        meta = await messaging_repo.patch_room_meta(room_id, updates)
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)


def _require_archivable(meta: dict) -> None:
    """Only a plain channel archives. #general is where join_default_room seats every new
    teammate and onboarding renders; the shared vendor channel is the customer's support line."""
    if meta.get("kind") != "channel" or is_default_room(meta) or is_vendor_room(meta):
        name = "#general" if is_default_room(meta) else "This room"
        raise ApiProblem("ROOM_NOT_ARCHIVABLE", f"{name} can't be archived.")


def get_live_event_publisher():
    """Module-level indirection so tests can monkeypatch ``messaging.get_live_event_publisher``
    directly. The real lookup stays a lazy import — ``core.services.live_events`` imports
    from this module at load time, so a top-level import here would be circular."""
    from core.services.live_events import get_live_event_publisher as _get_live_event_publisher

    return _get_live_event_publisher()


async def patch_room_me(auth: AuthContext, room_id: str, request: MemberPrefsRequest) -> RoomDetail:
    """The caller's own view preferences for one room. Any member may set them."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    updates = request.named_updates()
    if updates:
        try:
            member = await messaging_repo.set_member_prefs(room_id, "human", auth.user_id, updates)
        except messaging_repo.RoomNotFound as exc:
            raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.") from exc
        await get_live_event_publisher().publish_user(
            owner_id,
            auth.user_id,
            make_live_event("room.member.updated", {"roomId": meta["roomId"]}),
        )
    return await _room_detail(meta, auth.user_id, member)


async def upsert_member(auth: AuthContext, room_id: str, request: UpsertRoomMemberRequest) -> RoomDetail:
    meta, actor = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    _deny_vendor_human_change(meta, request.kind)
    existing = await messaging_repo.get_member(room_id, request.kind, request.id)
    # On a paid workspace any member may change an agent's listen mode (owner ruling
    # 2026-09-23); adding members, roles and everything on a free trial stay owner/admin.
    # Only the room-owning workspace's own members: in the shared vendor room a
    # Lightbulb-side member would be spending the customer's inference.
    mode_change = (
        request.kind == "agent"
        and existing is not None
        and existing.get("role") == request.role
        and messaging_repo.member_home(actor, owner_id) == owner_id
    )
    if actor.get("role") not in {"owner", "admin"} and not (mode_change and await listening_available(owner_id)):
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    if existing and existing.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner role cannot be changed.")
    await _require_listening(owner_id, meta.get("kind"), [request])
    roster = await load_agent_roster(owner_id) if request.kind == "agent" else []
    member = await _resolved_member(owner_id, request, roster)
    if request.kind == "agent":
        # A person's choice of mode; promote_listening never overrides it.
        member = {**member, "modeSetBy": "user"}
    if request.kind == "human":
        await messaging_repo.ensure_user_stream(owner_id, request.id)
    handles = mention_handles(roster) if roster else {}
    room_agents = [row for row in await messaging_repo.list_members(room_id) if row.get("kind") == "agent"]
    handle_updates = {row["id"]: handles[row["id"]] for row in room_agents if row["id"] in handles}
    try:
        meta = await messaging_repo.upsert_member(
            room_id=room_id,
            owner_id=owner_id,
            member=member,
            handle_updates=handle_updates,
        )
    except messaging_repo.MemberLimitExceeded as exc:
        raise ApiProblem("MEMBER_LIMIT", str(exc)) from exc
    if existing is None:
        _emit_member_added(
            owner_id, auth.user_id, request.kind, by="owner" if actor.get("role") == "owner" else "member"
        )
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)


async def remove_member(auth: AuthContext, room_id: str, kind: str, member_id: str) -> None:
    if kind not in {"human", "agent"}:
        raise ApiProblem("INVALID_REQUEST", "Invalid member kind.")
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_admin(owner_id, auth.user_id, room_id)
    _deny_vendor_human_change(meta, kind)
    target = await messaging_repo.get_member(room_id, kind, member_id)
    if target is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Room member not found.")
    if target.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner cannot be removed.")
    await _remove_member_with_notices(meta, kind, member_id)


async def leave_room(auth: AuthContext, room_id: str) -> None:
    """A member removes themselves. The owner must transfer or delete the room
    first — room deletion is out of scope, so this is a hard refusal."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    _deny_vendor_human_change(meta, "human")
    if member.get("role") == "owner":
        raise ApiProblem(
            "INVALID_REQUEST",
            "The room owner cannot leave. Make someone else an admin and transfer ownership first.",
        )
    await _remove_member_with_notices(meta, "human", auth.user_id)


async def _remove_member_with_notices(meta: dict, kind: str, member_id: str) -> dict:
    if kind == "agent":
        new_meta = await messaging_repo.remove_member(meta=meta, kind=kind, member_id=member_id)
        await _publish_room_metadata(new_meta)
        return new_meta

    members = await messaging_repo.list_members(meta["roomId"])
    humans = [row for row in members if row.get("kind") == "human"]
    new_key = str(uuid.uuid4())
    revision = int(meta["revision"]) + 1
    notices: list[dict] = []
    for human in humans:
        home = messaging_repo.member_home(human, meta["ownerId"])
        key = await messaging_repo.ensure_user_stream(home, human["id"])
        removed = human["id"] == member_id
        event = make_live_event(
            "room.access.removed" if removed else "room.subscription.rotated",
            (
                {"roomId": meta["roomId"], "roomRevision": revision}
                if removed
                else {"roomId": meta["roomId"], "deliveryKey": new_key, "roomRevision": revision}
            ),
            room_id=meta["roomId"],
            room_revision=revision,
        )
        notices.append({"userId": human["id"], "ownerId": home, "channelKey": key, "event": event})
    new_meta = await messaging_repo.remove_member(
        meta=meta,
        kind="human",
        member_id=member_id,
        new_delivery_key=new_key,
        live_events=notices,
    )
    if meta["roomId"] == default_room_id(meta["ownerId"]):
        # A removal (or leave) must stick: join_default_room never re-seats a marked user.
        # Best-effort -- the removal already committed; a missed marker only risks one re-seat.
        try:
            await messaging_repo.claim_autojoin(meta["roomId"], member_id)
        except Exception:
            logger.exception("autojoin marker on removal failed room=%s member=%s", meta["roomId"], member_id)
    return new_meta


async def revoke_human_membership(owner_id: str, user_id: str) -> None:
    """Rotate every room key before deactivating this user's private stream."""
    discovered = await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
    for row in discovered:
        room_id = row["meta"]["roomId"]
        member = await messaging_repo.get_member(room_id, "human", user_id)
        if member is None:
            continue
        meta = await messaging_repo.get_room(room_id)
        if meta is None or messaging_repo.member_home(member, meta["ownerId"]) != owner_id:
            continue
        await _remove_member_with_notices(meta, "human", user_id)
    await messaging_repo.deactivate_user_stream(owner_id, user_id)


async def list_messages(
    auth: AuthContext, room_id: str, after_seq: int, limit: int, before_seq: int | None = None
) -> MessagePage:
    await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    try:
        if before_seq is None:
            rows, next_after = await messaging_repo.list_messages(room_id, after_seq, limit)
            return MessagePage(messages=[canonical_message(row) for row in rows], next_after_seq=next_after)
        rows, next_before = await messaging_repo.list_messages_before_page(room_id, before_seq, limit)
    except ValueError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    return MessagePage(
        messages=[canonical_message(row) for row in rows], next_after_seq=None, next_before_seq=next_before
    )


async def update_message(
    auth: AuthContext, room_id: str, message_id: str, request: UpdateMessageRequest
) -> CanonicalMessage:
    meta, _ = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    item = await messaging_repo.get_message_by_id(room_id, validate_uuid(message_id, label="message ID"))
    if item is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Message not found in this room.")
    if request.text is not None and not request.text.strip() and not item.get("attachments"):
        raise ApiProblem("INVALID_REQUEST", "A message needs text or an attachment.")
    try:
        updated = await messaging_repo.update_message(
            owner_id,
            room_id,
            item["messageSeq"],
            auth.user_id,
            str(request.operation_id),
            request.expected_revision,
            request.text,
        )
    except messaging_repo.MessageForbidden as exc:
        raise ApiProblem("ROOM_FORBIDDEN", str(exc)) from exc
    except messaging_repo.MessageConflict as exc:
        raise ApiProblem("MESSAGE_CONFLICT", str(exc)) from exc
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", str(exc)) from exc
    return canonical_message(updated)


async def set_reaction(
    auth: AuthContext, room_id: str, message_id: str, request: SetReactionRequest
) -> CanonicalMessage:
    from core.services.live_events import get_live_event_publisher

    meta, _ = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    message_id = validate_uuid(message_id, label="message ID")
    item = await messaging_repo.get_message_by_id(room_id, message_id)
    if item is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Message not found in this room.")
    try:
        updated = await messaging_repo.set_reaction(
            owner_id, room_id, item["messageSeq"], auth.user_id, request.emoji, request.active
        )
    except messaging_repo.MessagingRepoError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    message = canonical_message(updated)
    if request.active:
        _product_event(
            distinct_id=auth.user_id,
            event="reaction_added",
            owner_id=owner_id,
            props={"room_kind": _room_kind(meta)},
        )
    event = make_live_event(
        "room.message.reactions.changed", message.model_dump(mode="json", by_alias=True), room_id=room_id
    )
    try:
        await get_live_event_publisher().publish_room(room_id, event)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Could not publish message reactions for room %s", room_id)
    return message


async def set_pin(auth: AuthContext, room_id: str, message_id: str, pinned: bool) -> RoomDetail:
    """Pin or unpin a message. Any member may do either (spec)."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    if await messaging_repo.get_message_by_id(room_id, message_id) is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Message not found in this room.")
    previous_revision = int(meta["revision"])
    try:
        meta = await messaging_repo.set_pin(room_id, message_id, pinned)
    except messaging_repo.PinLimitExceeded as exc:
        raise ApiProblem("PIN_LIMIT", f"A room can pin at most {MAX_PINNED_MESSAGES} messages.") from exc
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    # The repo is idempotent (no CAS write, no revision bump) when the requested
    # pin state already holds -- skip the live event too, so a repeated click
    # never fans out a no-op `room.metadata.changed`.
    if int(meta["revision"]) != previous_revision:
        await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id, member)


async def _validate_message_links(
    room_id: str,
    reply_to_message_id: str | None,
    thread_root_message_id: str | None,
) -> dict[str, dict]:
    """Validate the links and hand back the rows already read, keyed by message ID.

    ``create_agent_message`` reuses them to resolve the answered human message;
    ``get_message_by_id`` scans the room partition, so re-reading them would
    double that cost for nothing.
    """
    rows: dict[str, dict] = {}
    for message_id in dict.fromkeys(value for value in (reply_to_message_id, thread_root_message_id) if value):
        row = await messaging_repo.get_message_by_id(room_id, message_id)
        if row is None:
            raise ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.")
        rows[message_id] = row
    return rows


# ---------------------------------------------------------------------------
# telemetry — CloudWatch dimensions stay low-cardinality (never an id), PostHog
# carries the per-person detail. Every capture goes through ``_product_event``
# so an analytics outage can never fail a room write.
# ---------------------------------------------------------------------------

# The window ``create_agent_message`` scans back for the human message a reply
# answers. Bounded because the scan sits on the agent's response path: past
# ~20 rows the reply is almost certainly ambient, not an answer.
_REPLY_SCAN_LIMIT = 20

# Any @handle-shaped token, under the same boundary grammar as the resolver in
# ``parse_agent_mentions`` — handles themselves are only [a-z0-9-].
_MENTION_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])")

# Fenced blocks and inline code spans, stripped before counting unresolved
# mentions: a pasted traceback or `@pytest.mark.asyncio` is not someone failing
# to address a teammate. Fences are tried first so an inline rule cannot eat
# half of one. This does NOT change who gets dispatched — the resolver is
# deliberately untouched, so `@friday` in a code block still wakes Friday.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)


def _room_kind(meta: dict) -> str:
    """Room kind as a bounded metric dimension."""
    kind = meta.get("kind")
    return kind if kind in ("channel", "dm", "group-dm") else "unknown"


def _count_bucket(count: int) -> str:
    return "0" if count == 0 else "1" if count == 1 else "many"


def _product_event(*, distinct_id: str | None, event: str, owner_id: str, props: dict[str, Any]) -> None:
    """Best-effort PostHog capture stamped with the workspace."""
    try:
        capture_product_event(distinct_id=distinct_id, event=event, properties={"workspace_id": owner_id, **props})
    except Exception:
        logger.warning("messaging: %s product event failed", event, exc_info=True)


def _emit_dispatch_targets(targets: list[dict], room_kind: str, source: str) -> None:
    put_metric(
        "messaging.dispatch.targets",
        dimensions={"count_bucket": _count_bucket(len(targets)), "room_kind": room_kind, "source": source},
    )


def _unresolved_mention_reasons(text: str, members: list[dict]) -> list[str]:
    """Why each @handle named in ``text`` failed to resolve to exactly one member."""
    by_handle: dict[str, int] = {}
    for member in members:
        handle = member.get("mentionHandle")
        if member.get("kind") in {"agent", "human"} and isinstance(handle, str) and handle:
            by_handle[handle.lower()] = by_handle.get(handle.lower(), 0) + 1
    prose = _CODE_SPAN_RE.sub(" ", text)
    reasons = []
    for named in sorted({token.lower() for token in _MENTION_TOKEN_RE.findall(prose)}):
        matches = by_handle.get(named, 0)
        if matches == 0:
            reasons.append("no_match")
        elif matches > 1:
            reasons.append("ambiguous")
    return reasons


def _dispatched_event_kind(row: dict, agent_id: str) -> str:
    for target in row.get("dispatchTargets") or []:
        if target.get("agentId") == agent_id:
            kind = target.get("eventKind")
            return kind if kind in ("user_request", "room_event") else "unknown"
    return "unknown"


def _age_ms(created_at: Any) -> float | None:
    """Milliseconds since an ISO-8601 timestamp; None when it will not parse."""
    try:
        return max(0.0, (utc_now() - datetime.fromisoformat(str(created_at))).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


async def _answered_human_message(
    room_id: str, agent_id: str, item: dict, links: dict[str, dict]
) -> tuple[dict, str] | None:
    """The human message this agent reply answers, and how it was matched.

    An explicit reply/thread link wins; otherwise the newest human message in
    the last ``_REPLY_SCAN_LIMIT`` rows that was actually dispatched to this
    agent. None when neither resolves — an unprompted agent message.
    """
    for message_id in (item.get("replyToMessageId"), item.get("threadRootMessageId")):
        row = links.get(message_id) if message_id else None
        if row and (row.get("author") or {}).get("kind") == "human":
            return row, "reply_to"
    rows = await messaging_repo.list_messages_before(room_id, int(item["messageSeq"]), _REPLY_SCAN_LIMIT)
    for row in reversed(rows):
        if (row.get("author") or {}).get("kind") != "human":
            continue
        if any(target.get("agentId") == agent_id for target in row.get("dispatchTargets") or []):
            return row, "scan"
    return None


def select_dispatch_targets(
    members: list[dict],
    mentions: list[dict],
    source_agent_id: str | None,
    *,
    room_kind: str | None = None,
    default_agent_id: str | None = None,
) -> list[dict]:
    """``default_agent_id``: in the starter #general a HUMAN message that selects no agent at all
    goes to that agent (the CEO) as a user_request, so nobody talks into silence (owner ruling
    2026-09-23). Rooms with Listening agents already have candidates, and Jev decides there."""
    mentioned = {ref["id"] for ref in mentions if ref.get("kind") == "agent"}
    targets: dict[str, EventKind] = {}
    for member in sorted(members, key=lambda row: (row.get("kind", ""), row.get("id", ""))):
        if member.get("kind") != "agent" or member.get("id") == source_agent_id:
            continue
        # An observe member never runs — not on ambient traffic, not when
        # mentioned, not in a DM. It only sees history on its next turn.
        if member.get("listenMode") == "observe":
            continue
        agent_id = member["id"]
        # A DM is addressed by construction: without this, an unmentioned DM is
        # an ambient room_event, which OpenClaw runs message-tool-only and the
        # agent's plain reply is muted (observed on dev 2026-09-10).
        if agent_id in mentioned or room_kind == "dm":
            targets[agent_id] = "user_request"
        elif member.get("listenMode") == "active":
            targets.setdefault(agent_id, "room_event")
    if not targets and source_agent_id is None and default_agent_id:
        default = next((m for m in members if m.get("kind") == "agent" and m.get("id") == default_agent_id), None)
        if default is not None and default.get("listenMode") != "observe":
            targets[default_agent_id] = "user_request"
    return [{"agentId": agent_id, "eventKind": event_kind} for agent_id, event_kind in sorted(targets.items())]


async def _validate_mentions(room_id: str, mentions: list[MemberRef]) -> list[dict]:
    normalized = sorted({(ref.kind, ref.id) for ref in mentions})
    for kind, member_id in normalized:
        if await messaging_repo.get_member(room_id, kind, member_id) is None:
            raise ApiProblem("MEMBER_NOT_FOUND", "Mentioned member is not in this room.")
    return [{"kind": kind, "id": member_id} for kind, member_id in normalized]


async def create_human_message(auth: AuthContext, room_id: str, request: CreateMessageRequest) -> CanonicalMessage:
    meta, sender = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    mentions = await _validate_mentions(room_id, request.mentions)
    await _validate_message_links(
        room_id,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    members = await messaging_repo.list_members(room_id)
    targets = select_dispatch_targets(members, mentions, None, room_kind=meta.get("kind"))
    if not targets and room_id == default_room_id(owner_id):
        # Nobody would answer in the starter #general: the CEO does (one read, only on this path).
        from core.repositories.paperclip_repo import PaperclipRepo

        company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(
            owner_id, consistent_read=True
        )
        ceo_id = (getattr(company, "starter_agent_ids", None) or {}).get("ceo") if company else None
        targets = select_dispatch_targets(members, mentions, None, room_kind=meta.get("kind"), default_agent_id=ceo_id)
    room_kind = _room_kind(meta)
    # EFS is NFS. These reads open and read whole attachment files, so running
    # them inline blocked the event loop -- every other request on the
    # process, not just this send -- for the duration of the slowest one.
    # `return_exceptions=True` so a second bad attachment id in the same request
    # does not leave its exception unretrieved -- these are concurrent now, and
    # the default would propagate the first and drop the rest, which asyncio
    # then logs as "exception was never retrieved". The caller still sees the
    # first failure, exactly as it did when this was a serial comprehension.
    loaded = await asyncio.gather(
        *(run_in_thread(load_attachment, owner_id, room_id, str(value)) for value in request.attachment_ids),
        return_exceptions=True,
    )
    for result in loaded:
        if isinstance(result, BaseException):
            raise result
    attachments = [meta.model_dump(mode="json", by_alias=True) for meta, _ in loaded]
    req_hash = messaging_repo.request_hash(
        request.text,
        mentions,
        [str(value) for value in request.attachment_ids],
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    try:
        item, replayed = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=messaging_repo.human_idemp_sk(auth.user_id, str(request.client_message_id)),
            req_hash=req_hash,
            author={"kind": "human", "id": auth.user_id, "displayName": await human_display_name(auth.user_id)},
            text=request.text,
            mentions=mentions,
            reply_to_message_id=str(request.reply_to_message_id) if request.reply_to_message_id else None,
            thread_root_message_id=str(request.thread_root_message_id) if request.thread_root_message_id else None,
            run_id=None,
            dispatch_targets=targets,
            source_agent_id=None,
            client_message_id=str(request.client_message_id),
            attachments=attachments,
        )
    except messaging_repo.RoomArchived as exc:
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE) from exc
    except messaging_repo.MessageForbidden as exc:
        # Lost the race: removed between require_room_member and the transaction committing.
        # Same code require_room_member itself raises for "no longer a member".
        raise ApiProblem("ROOM_FORBIDDEN", str(exc)) from exc
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "clientMessageId was already used with a different request.") from exc
    # Telemetry counts MESSAGES, not attempts — a replay wrote nothing, so
    # emitting here would inflate every counter by the client's retry rate.
    # This is also why the dispatch metric is not adjacent to its selector.
    if not replayed:
        agent_mentions = sum(1 for ref in mentions if ref.get("kind") == "agent")
        _emit_dispatch_targets(targets, room_kind, "human")
        put_metric(
            "messaging.message.created",
            dimensions={"author_kind": "human", "room_kind": room_kind, "has_targets": "true" if targets else "false"},
        )
        _product_event(
            distinct_id=auth.user_id,
            event="room_message_sent",
            owner_id=owner_id,
            props={
                "room_kind": room_kind,
                "mentions_agents": agent_mentions,
                "is_thread_reply": bool(request.reply_to_message_id or request.thread_root_message_id),
                "has_attachment": bool(attachments),
                "first_in_room": int(item.get("messageSeq") or 0) == 1,
                "sender_is_owner": sender.get("role") == "owner",
            },
        )
        # The gap this whole task exists to close: someone addressed an agent
        # and nothing ran. Invisible before — no metric, no log, no event.
        if agent_mentions and not targets:
            _product_event(
                distinct_id=auth.user_id,
                event="agent_dispatch_missed",
                owner_id=owner_id,
                props={"reason": "no_targets", "room_kind": room_kind},
            )
    # Neither of these is part of the response. `touch_last_interactive` writes
    # to the containers table purely to feed the idle/park clock, and it was
    # being awaited after the message was already durable -- a whole round trip
    # of latency the sender paid for nothing.
    asyncio.create_task(_touch_last_interactive_quietly(owner_id))
    from core.services.onboarding_brief import capture_first_workspace_message

    asyncio.create_task(capture_first_workspace_message(owner_id, item))
    return canonical_message(item)


def parse_agent_mentions(text: str, members: list[dict]) -> tuple[list[dict], list[str]]:
    """Structured mentions for every member handle named in agent-authored text,
    plus the reasons the other @handles in it resolved to nobody.

    Callers pass members through :func:`with_human_handles` so humans are
    addressable by the same ``@handle`` grammar agents use.

    The reasons ride along rather than being emitted here so the one caller can
    scan the text once AND report them only after the message actually
    committed — an idempotent replay writes nothing and must count nothing.
    """
    by_handle: dict[str, list[tuple[str, str]]] = {}
    for member in members:
        handle = member.get("mentionHandle")
        if member.get("kind") in {"agent", "human"} and isinstance(handle, str) and handle:
            by_handle.setdefault(handle.lower(), []).append((member["kind"], member["id"]))
    found: set[tuple[str, str]] = set()
    for handle, refs in by_handle.items():
        if len(refs) != 1:
            continue
        if re.search(rf"(?<![A-Za-z0-9_-])@{re.escape(handle)}(?![A-Za-z0-9_-])", text, re.IGNORECASE):
            found.add(refs[0])
    mentions = [{"kind": kind, "id": member_id} for kind, member_id in sorted(found)]
    return mentions, _unresolved_mention_reasons(text, members)


RUN_ERROR_MESSAGES = {
    "agent_did_not_start": "The agent did not start this run.",
    "agent_stopped_responding": "The agent stopped responding.",
}
# `gate_error` is deliberately absent: it is the ambient "should an `active` agent
# answer this plain message?" classifier failing, not a run the user asked for. A
# note there would repeat on every plain message from every listening agent while
# the classifier is down.
RUN_FAILURE_OUTCOMES = frozenset({"error"})

# Where a BUDGET_BLOCK_MESSAGES reason sends the owner to fix it: (path, link
# label). Only appended to the room note below — BUDGET_BLOCK_MESSAGES itself
# stays plain text for its other readers (ChatErrorCard renders from `code`
# alone). Panel keys verified against apps/frontend/src/app/settings/page.tsx's
# `PANELS` registry, not the workspace-dock's separate `llm` alias.
BUDGET_BLOCK_LINKS: dict[str, tuple[str, str]] = {
    "trial_daily_cap": ("/settings?panel=billing", "Upgrade"),
    "trial_expired": ("/settings?panel=billing", "Upgrade"),
    "included_daily_cap": ("/settings?panel=billing", "Upgrade"),
    "out_of_credits": ("/settings?panel=providers", "Add credits"),
}


async def count_first_turn_failure(owner_id: str, error_kind: str | None) -> None:
    """A failed run for a free-tier workspace that has NEVER had an agent reply is a new user's
    first impression dying (the 09-22 gate-413 bug failed every one, silently). Counted for the
    P29 page; ``first_agent_response_at`` is stamped by the trial first-response capture, which
    runs only while ``POSTHOG_PROJECT_WRITE_KEY`` is set -- blank that key and every trial reads as
    "first turn" forever, so P29 pages on any trial failure. The
    bridge-supplied error kind goes to the log, never a dimension (unbounded). Never raises."""
    try:
        from core.repositories import billing_repo

        account = await billing_repo.get_by_owner_id(owner_id) or {}
        if account.get("subscription_status") == "trial_local" and not account.get("first_agent_response_at"):
            logger.warning("free-tier first turn failed owner=%s error_kind=%s", owner_id, error_kind)
            put_metric("messaging.run.first_turn_failed")
    except Exception:
        logger.warning("first-turn failure count failed owner=%s", owner_id, exc_info=True)


async def post_run_failure_message(owner_id: str, row: dict, outcome: str, error_kind: str | None) -> None:
    """Leave one durable agent-authored note in the run's room (and the trigger's thread).

    Run frames are ephemeral, so without this a user who was not watching sees nothing.
    Silent by construction (no mentions, no dispatch targets), and keyed on the runId so a
    retried run.ended or a sweeper/callback race replays the receipt instead of doubling.
    Never raises: the run is already closed by the time this runs."""
    await count_first_turn_failure(owner_id, error_kind)
    try:
        run_id, room_id, agent_id = row["runId"], row["roomId"], row["agentId"]
        # error_kind is bridge-supplied; only an identifier (an exception class
        # name) is shown, so nothing like a URL or key can reach the room.
        if error_kind and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,63}", error_kind):
            error_kind = None
        label = f"{outcome}: {error_kind}" if error_kind else outcome
        reason = RUN_ERROR_MESSAGES.get(error_kind or outcome) or "Agent run failed."
        text = (
            f"I couldn't finish that ({label}). {reason} "
            "Try again, or check Settings → Model & Credits if this keeps happening."
        )
        # A capped/expired trial owner, or a paid owner out of credits, has their
        # run fail at the gate; say that instead of a generic model error (the
        # bridge reports every failed turn as model_unavailable). Trial/cap
        # reasons take priority — an owner cannot be both, but if that ever
        # changes the trial state is the more specific one.
        from core.repositories import billing_repo
        from core.services.chat_gate import BUDGET_BLOCK_MESSAGES
        from core.services.metronome_gate import is_owner_blocked
        from core.services.trial_cap import trial_block_reason

        budget_reason: str | None = None
        if error_kind in (None, "model_unavailable"):  # the sweeper's own kinds are real infra failures
            reason = await trial_block_reason(owner_id)
            if reason:
                text = BUDGET_BLOCK_MESSAGES.get(reason, text)
                budget_reason = reason
            else:
                # Same predicate gate_chat's Layer 3 and the provision gate's
                # Layer 3 use: only openrouter_managed owners draw against the
                # Metronome credit balance.
                account = await billing_repo.get_by_owner_id(owner_id) or {}
                if account.get("provider_choice") == "openrouter_managed" and await is_owner_blocked(owner_id):
                    text = BUDGET_BLOCK_MESSAGES["out_of_credits"]
                    budget_reason = "out_of_credits"
        link = BUDGET_BLOCK_LINKS.get(budget_reason) if budget_reason else None
        if link:
            path, label = link
            text = f"{text} [{label}]({settings.FRONTEND_URL.rstrip('/')}{path})"
        trigger = (
            await messaging_repo.get_message_by_id(room_id, row["triggerMessageId"])
            if row.get("triggerMessageId")
            else None
        )
        thread_root = (trigger or {}).get("threadRootMessageId")
        roster = {agent["id"]: agent for agent in await load_agent_roster(owner_id)}
        name = (roster.get(agent_id) or {}).get("name") or "Agent"
        await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(f"run-failed:{run_id}"),
            req_hash=messaging_repo.request_hash(room_id, agent_id, text, None, thread_root),
            author={"kind": "agent", "id": agent_id, "displayName": name},
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=thread_root,
            run_id=run_id,
            dispatch_targets=[],
            source_agent_id=agent_id,
            client_message_id=None,
        )
        put_metric("messaging.run.failure_message", dimensions={"outcome": outcome})
    except Exception:
        logger.exception("run failure message failed")


async def publish_run_error_frames(row: dict, error_kind: str | None) -> None:
    """The terminal `error` + `run_ended` pair for a run whose container published nothing
    (never started, or closed by the sweeper). Frames carry runId and agentId like the
    plugin's own."""
    publisher = get_live_event_publisher()
    base = {"roomId": row["roomId"], "agentId": row["agentId"], "runId": row["runId"]}
    stamp = {"runId": row["runId"], "agentId": row["agentId"]}
    message = RUN_ERROR_MESSAGES.get(error_kind or "", "Agent run failed")
    for frame in (
        {"type": "error", "message": message, **({"code": error_kind} if error_kind else {}), **stamp},
        {"type": "run_ended", "outcome": "error", **stamp},
    ):
        await publisher.publish_room(
            row["roomId"], make_live_event("room.run.frame", {**base, "frame": frame}, room_id=row["roomId"])
        )


def _ms_between(start: Any, end: Any) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


async def handle_run_event(owner_id: str, event: RunEventRequest) -> RunEventResponse:
    """Apply one at-least-once callback from the container's run supervisor (spec §4/§5)."""
    run_id = str(event.run_id)
    if event.type == "run.started":
        applied = await messaging_repo.start_run(owner_id, run_id, session_key=event.session_key or "")
    elif event.type == "run.heartbeat":
        applied = await messaging_repo.heartbeat_run(owner_id, run_id)
    else:
        usage = event.usage.model_dump(mode="json", by_alias=True) if event.usage else None
        row = await messaging_repo.end_run(
            owner_id, run_id, outcome=event.outcome, error_kind=event.error_kind, usage=usage
        )
        applied = row is not None
        if row is not None:
            event_kind = row.get("eventKind") or "unknown"
            put_metric("messaging.run.ended", dimensions={"outcome": event.outcome, "event_kind": event_kind})
            for name, start, end in (
                ("messaging.run.start_latency", "dispatchedAt", "startedAt"),
                ("messaging.run.duration", "startedAt", "endedAt"),
            ):
                value = _ms_between(row.get(start), row.get(end)) if row.get(start) and row.get(end) else None
                if value is not None:
                    put_metric(name, value=value, unit="Milliseconds", dimensions={"event_kind": event_kind})
            if event.outcome == "error" and not row.get("startedAt"):
                try:
                    await publish_run_error_frames(row, event.error_kind)
                except Exception:
                    logger.exception("run error frames failed")
            if event.outcome in RUN_FAILURE_OUTCOMES:
                await post_run_failure_message(owner_id, row, event.outcome, event.error_kind)
            try:
                await _after_run_ended(owner_id, row, event.outcome, event.usage)
            except Exception:
                logger.exception("run post-processing failed")
    if not applied and await messaging_repo.get_run(owner_id, run_id) is None:
        put_metric("messaging.run.callback", dimensions={"type": event.type, "outcome": "unknown"})
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    put_metric(
        "messaging.run.callback", dimensions={"type": event.type, "outcome": "applied" if applied else "replayed"}
    )
    return RunEventResponse(run_id=event.run_id, status="applied" if applied else "replayed")


async def _record_run_usage(
    owner_id: str, user_id: str, usage: RunUsage, *, room_turn: bool, event_kind: str | None
) -> None:
    """Shared ``record_usage`` call for the room (``_after_run_ended``) and
    non-room (``handle_usage_report``) usage paths. Never raises."""
    # No token volume -- nothing to record.
    if usage.input_tokens > 0 or usage.output_tokens > 0:
        try:
            await record_usage(
                owner_id=owner_id,
                user_id=user_id,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
                cache_write=usage.cache_write_tokens,
                room_turn=room_turn,
                event_kind=event_kind,
            )
        except Exception:
            logger.exception("usage record failed")


async def handle_usage_report(owner_id: str, body: UsageReportRequest) -> UsageReportResponse:
    """Usage + product analytics for runs the room contract does not cover (cron, heartbeat, Paperclip)."""
    if parse_lightbulb_session_key(body.session_key) is not None:
        put_metric("messaging.usage.callback", dimensions={"outcome": "room"})
        return UsageReportResponse(run_id=body.run_id, status="ignored")
    if not await messaging_repo.claim_usage_receipt(owner_id, body.run_id):
        put_metric("messaging.usage.callback", dimensions={"outcome": "replayed"})
        return UsageReportResponse(run_id=body.run_id, status="replayed")
    usage = body.usage
    await _record_run_usage(owner_id, owner_id, usage, room_turn=False, event_kind=None)
    data = usage.model_dump(by_alias=True)
    if body.duration_ms is not None:
        data["durationMs"] = body.duration_ms
    try:
        # claim_usage_receipt above is the durable, cross-instance gate that keeps
        # this single-emit at desiredCount >= 2 -- it is a conditional PutItem, so
        # this call is reached at most once per (owner_id, run_id) fleet-wide.
        # run_analytics._seen_run_ids is a per-process accelerator only; it is NOT
        # what prevents a double-emit across instances.
        run_analytics.schedule_run_completed(
            owner_id=owner_id,
            parsed=parse_session_key(body.session_key),
            payload={"runId": body.run_id, "data": data},
            phase="end" if body.success else "error",
        )
    except Exception:
        logger.exception("run analytics scheduling failed")
    put_metric("messaging.usage.callback", dimensions={"outcome": "applied"})
    return UsageReportResponse(run_id=body.run_id, status="applied")


async def _after_run_ended(owner_id: str, row: dict, outcome: str, usage: RunUsage | None) -> None:
    """Usage counters (analytics only; Metronome is billed by the meter Lambda) and the
    failure push. ``outcome`` is the event's own outcome (not re-read off ``row``, which
    the ledger only ever stores this same value into). Never raises into the callback."""
    if outcome not in {"completed", "error", "aborted"}:
        return
    trigger = await messaging_repo.get_message_by_id(row["roomId"], row["triggerMessageId"])
    author = (trigger or {}).get("author") or {}
    member_user_id = author.get("id") if author.get("kind") == "human" else None
    if usage is not None:
        await _record_run_usage(
            owner_id, member_user_id or owner_id, usage, room_turn=True, event_kind=row.get("eventKind")
        )
    # A completed reply is pushed by the projector on room.message.created; only a
    # failure has no message to carry it.
    if outcome == "error" and member_user_id and member_user_id.startswith("user_"):
        try:
            roster = {agent["id"]: agent for agent in await load_agent_roster(owner_id)}
            name = (roster.get(row["agentId"]) or {}).get("name") or "Agent"
            await send_push_to_user(
                member_user_id,
                f"{name} run failed",
                "Agent run failed",
                {"agentId": row["agentId"], "roomId": row["roomId"], "runId": row["runId"]},
            )
        except Exception:
            logger.exception("run failure push failed")


async def create_follow_up(owner_id: str, request: FollowUpRequest) -> FollowUpResponse:
    """Queue a follow-up turn for the agent whose delegation finished. The plan
    gate is the consumer's (``openclaw_work._dispatch_message``), not this door's."""
    room_id, agent_id = str(request.room_id), str(request.agent_id)
    follow_up_id = str(request.follow_up_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    # Read-then-write is enough: openclaw_work._room_follow_up re-reads the room and drops archived ones.
    if meta.get("archivedAt"):
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE)
    if await messaging_repo.get_member(room_id, "agent", agent_id) is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not a current room member.")
    if await messaging_repo.get_message_by_id(room_id, str(request.trigger_message_id)) is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Trigger message not found.")
    # Replay BEFORE the cap: a retry after a lost 202 counts its own row.
    if await messaging_repo.get_follow_up(room_id, follow_up_id) is not None:
        put_metric("messaging.followup.created", dimensions={"status": "replayed"})
        return FollowUpResponse(status="replayed")
    if await messaging_repo.count_recent_follow_ups(room_id, agent_id, window_s=3600) >= FOLLOW_UP_CAP_PER_HOUR:
        put_metric("messaging.followup.created", dimensions={"status": "rate_limited"})
        raise ApiProblem("RATE_LIMITED", "Too many follow-ups for this agent in this room.")
    created = await messaging_repo.create_follow_up(
        owner_id=owner_id,
        room_id=room_id,
        agent_id=agent_id,
        follow_up_id=follow_up_id,
        trigger_message_id=str(request.trigger_message_id),
        event_kind=request.event_kind,
        reason=request.reason,
        text=request.text,
    )
    status = "accepted" if created else "replayed"
    put_metric("messaging.followup.created", dimensions={"status": status})
    return FollowUpResponse(status=status)


async def create_agent_message(owner_id: str, request: AgentMessageRequest) -> AgentMessageReceiptResponse:
    room_id = str(request.room_id)
    agent_id = str(request.agent_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    member = await messaging_repo.get_member(room_id, "agent", agent_id)
    if member is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not a current room member.")
    roster = {row["id"]: row for row in await load_agent_roster(owner_id)}
    agent = roster.get(agent_id)
    if agent is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not configured in this workspace.")
    links = await _validate_message_links(
        room_id,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    room_members = await messaging_repo.list_members(room_id)
    members = with_human_handles(room_members, await human_emails(room_members))
    mentions, unresolved = parse_agent_mentions(request.text, members)
    targets = select_dispatch_targets(members, mentions, agent_id, room_kind=meta.get("kind"))
    room_kind = _room_kind(meta)
    # Stamp the producing run when the container names it: clients retire the
    # run card on an exact runId match instead of showing card + message side
    # by side (the doubles). None keeps today's text-match fallback.
    run_id = str(request.run_id) if request.run_id else None
    if run_id is not None:
        # A present run id is a claim about the ledger, not a hint: it must be this
        # owner's run for this room and agent. A run that already closed still accepts
        # its late reply (durable retries, sweeper closures) — delivery is at-least-once.
        run = await messaging_repo.get_run(owner_id, run_id)
        if run is None or run.get("roomId") != room_id or run.get("agentId") != agent_id:
            raise ApiProblem("RUN_NOT_FOUND", "runId does not name this owner's run for this room and agent.")
    # Stored before the write, exactly as the human upload route stores them, and
    # deliberately NOT part of ``req_hash``: every store mints a fresh id, so hashing them
    # would turn the container's own retry of the same reply into an idempotency CONFLICT and
    # lose it. A retry therefore replays the first message and leaves its freshly stored twin
    # unreferenced -- an owner-scoped object the account wipe already collects.
    attachments: list[dict[str, Any]] = []
    for item in request.attachments:
        try:
            stored = await run_in_thread(
                store_attachment, owner_id, room_id, item.file_name, item.content_type, item.data_base64
            )
        except ValueError as exc:
            raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
        attachments.append(stored.model_dump(mode="json", by_alias=True))
    req_hash = messaging_repo.request_hash(
        room_id,
        agent_id,
        request.text,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    try:
        item, replayed = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(request.event_id),
            req_hash=req_hash,
            author={"kind": "agent", "id": agent_id, "displayName": agent["name"]},
            text=request.text,
            mentions=mentions,
            reply_to_message_id=str(request.reply_to_message_id) if request.reply_to_message_id else None,
            thread_root_message_id=str(request.thread_root_message_id) if request.thread_root_message_id else None,
            run_id=run_id,
            dispatch_targets=targets,
            source_agent_id=agent_id,
            client_message_id=None,
            attachments=attachments,
        )
    except messaging_repo.RoomArchived as exc:
        # Deliberate: a reply to a turn that began before the archive is refused too, so an
        # archived channel's history ends where the archive did. The bridge drops a 4xx.
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE) from exc
    except messaging_repo.MessageForbidden as exc:
        # Lost the race: removed between the get_member pre-check above and the
        # transaction committing. Same code that pre-check itself raises.
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not a current room member.") from exc
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "eventId was already used with a different request.") from exc
    # Replays write nothing, and this path is BUILT around retries — the
    # receipt is the container's confirmation. Counting here would inflate
    # every counter and, worse, report retry delay as reply latency.
    if not replayed:
        _emit_dispatch_targets(targets, room_kind, "agent")
        put_metric(
            "messaging.message.created",
            dimensions={"author_kind": "agent", "room_kind": room_kind, "has_targets": "true" if targets else "false"},
        )
        # Stamped vs unset tells us whether the doubles fix is actually
        # engaging in prod. Counted here (not at lookup time) so container
        # retries don't inflate it — same rule as every counter on this path.
        put_metric(
            "messaging.agent_message.run_id",
            dimensions={"result": "stamped" if run_id else "unset", "room_kind": room_kind},
        )
        for reason in unresolved:
            put_metric("messaging.mention.unresolved", dimensions={"reason": reason})
        if unresolved:
            _product_event(
                distinct_id=owner_id,
                event="agent_dispatch_missed",
                owner_id=owner_id,
                props={"reason": "unresolved_mention", "room_kind": room_kind},
            )
        await _emit_reply_telemetry(owner_id, room_id, agent_id, item, links, room_kind)
    return AgentMessageReceiptResponse(message_id=item["messageId"], room_id=room_id, created_at=item["createdAt"])


async def _emit_reply_telemetry(
    owner_id: str, room_id: str, agent_id: str, item: dict, links: dict[str, dict], room_kind: str
) -> None:
    """Time-to-reply for the human message this agent message answers.

    Never raises: the receipt this runs under is the container's only
    confirmation that its message landed, so a telemetry read that fails must
    not turn a committed write into a 500.
    """
    try:
        matched = await _answered_human_message(room_id, agent_id, item, links)
        latency_ms: int | None = None
        if matched is not None:
            row, how = matched
            event_kind = _dispatched_event_kind(row, agent_id)
            age_ms = _age_ms(row.get("createdAt"))
            if age_ms is not None:
                latency_ms = int(age_ms)
                put_metric(
                    "messaging.reply.latency",
                    value=age_ms,
                    unit="Milliseconds",
                    dimensions={"room_kind": room_kind, "event_kind": event_kind, "matched": how},
                )
            # Keyed on the human who asked. Never on the owner id — that is a
            # Clerk ORG, and using it as a distinct_id mints org-shaped
            # "people" in PostHog. The unmatched counter covers that case.
            props: dict[str, Any] = {"room_kind": room_kind, "event_kind": event_kind, "agent_id": agent_id}
            if latency_ms is not None:
                props["latency_ms"] = latency_ms
            _product_event(
                distinct_id=(row.get("author") or {}).get("id"),
                event="agent_reply_delivered",
                owner_id=owner_id,
                props=props,
            )
        if latency_ms is None:
            put_metric("messaging.reply.unmatched")
        # Second door for the once-ever trial activation signal: a room reply
        # is a first agent response just as much as a webchat run terminal is.
        schedule_trial_first_agent_response(owner_id)
    except Exception:
        logger.warning("messaging: reply telemetry failed for room %s", room_id, exc_info=True)


async def get_agent_receipt(owner_id: str, event_id: str) -> AgentMessageReceiptResponse:
    try:
        validate_event_id(event_id)
    except ValueError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    receipt = await messaging_repo.get_agent_idempotency(owner_id, event_id)
    if receipt is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Agent message receipt not found.")
    return AgentMessageReceiptResponse(
        message_id=receipt["messageId"],
        room_id=receipt["roomId"],
        created_at=receipt["createdAt"],
    )


async def set_read_cursor(auth: AuthContext, room_id: str, request: ReadCursorRequest) -> ReadCursorResponse:
    await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    value = await messaging_repo.set_read_cursor(room_id, auth.user_id, request.last_read_seq)
    return ReadCursorResponse(room_id=room_id, last_read_seq=value)


async def mark_room_unread(
    auth: AuthContext, room_id: str, request: MarkUnreadRequest | None = None
) -> ReadCursorResponse:
    """Whole-room by default; ``fromSeq`` leaves that message and everything
    after it unread ("mark unread from here")."""
    _, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    value = await messaging_repo.mark_unread(
        room_id,
        auth.user_id,
        joined_seq=int(member.get("joinedSeq", 0)),
        from_seq=request.from_seq if request else None,
    )
    return ReadCursorResponse(room_id=room_id, last_read_seq=value)


async def _publish_room_metadata(meta: dict) -> None:
    from core.services.live_events import get_live_event_publisher

    event = make_live_event(
        "room.metadata.changed",
        {"roomId": meta["roomId"], "roomRevision": int(meta["revision"])},
        room_id=meta["roomId"],
        room_revision=int(meta["revision"]),
    )
    await get_live_event_publisher().publish_room(meta["roomId"], event)


def json_body_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
