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
from urllib.parse import quote
from pathlib import PurePath
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from botocore.exceptions import ClientError
from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.auth import is_org_owner_id
from core.config import settings
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event, schedule_trial_first_agent_response
from core.dynamodb import run_in_thread
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services.push_service import send_push_to_user
from core.services import clerk_admin, hermes_profiles, livekit_huddles, run_analytics
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
#: Agent messages at this hop (see create_agent_message) wake nobody: a human message is
#: hop 0, so a chain runs at most this many agent replies before a human speaks again.
AGENT_REPLY_CAP = 2

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
# xlsx/docx/pptx are zip containers under the hood (OOXML) -- same ceiling as
# ZIP/PDF, same "signature-only, no archive parsing" posture (see
# _ATTACHMENT_SIGNATURES below).
OFFICE_MAX_BYTES = 20 * 1024 * 1024
# Plain text has no byte signature to check; a spreadsheet export or a long
# log is still generous at the image tier.
TEXT_MAX_BYTES = IMAGE_MAX_BYTES
# A short product video (a 30 s 1080p H.264 render is ~5-50 MB). Too big for API Gateway's
# 10 MB body in either direction, so a video never rides an inline POST or the byte-serving
# GET: an agent uploads it straight to the attachment key (``adopt_uploaded_attachment``) and a
# client plays it from a presigned URL (``attachment_url``).
VIDEO_MAX_BYTES = 100 * 1024 * 1024
VIDEO_CONTENT_TYPES = ("video/mp4", "video/webm", "video/quicktime")
ATTACHMENT_MAX_BYTES_BY_TYPE = {
    "image/png": IMAGE_MAX_BYTES,
    "image/jpeg": IMAGE_MAX_BYTES,
    "image/webp": IMAGE_MAX_BYTES,
    "image/gif": IMAGE_MAX_BYTES,
    "application/pdf": PDF_MAX_BYTES,
    "application/zip": ZIP_MAX_BYTES,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": OFFICE_MAX_BYTES,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": OFFICE_MAX_BYTES,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": OFFICE_MAX_BYTES,
    "text/csv": TEXT_MAX_BYTES,
    "text/plain": TEXT_MAX_BYTES,
    "text/markdown": TEXT_MAX_BYTES,
    **{t: VIDEO_MAX_BYTES for t in VIDEO_CONTENT_TYPES},
}
# The largest file that travels THROUGH the API (upload route, inline agent attachment). Video
# is excluded on purpose: its cap only applies to the S3-direct path.
ATTACHMENT_MAX_BYTES = max(v for t, v in ATTACHMENT_MAX_BYTES_BY_TYPE.items() if t not in VIDEO_CONTENT_TYPES)
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
# returns early on any room that already has messages): the first thing a new
# user reads. Refers to the CEO by ROLE, never a name: agent names are user-editable.
#: The three pre-typed replies. The frontend's FirstReplyChips.tsx carries the same strings;
#: the third's prefix is what onboarding_brief.first_message_intake recognises as "you pick".
FIRST_REPLIES: tuple[str, str, str] = (
    "Here's our website: ",
    "I'm starting ",
    "Not sure yet. Pick something useful and show me how you work.",
)

_FIRST_MESSAGE = (
    "Welcome to {workspace}. I'm your CEO. Growth, Engineering, Finance, Analytics and "
    "Customer Ops are in this room with me.\n\n"
    "One thing gets us started: what's the business? A website or a sentence is plenty.\n\n"
    "→ Here's our website: …\n"
    "→ I'm starting …\n"
    "→ Not sure yet. Pick something useful and show me how you work.\n\n"
    "I'll turn your answer into a plan and hand the first pieces to the team. "
    "You'll watch them work here."
)


def default_room_welcome(workspace_name: str | None) -> str:
    """The CEO's first message. Only the workspace name is interpolated; a leading @ or #
    is stripped so the name cannot render as a mention or a channel link."""
    name = " ".join((workspace_name or "").lstrip("@#").split()) or "your workspace"
    return _FIRST_MESSAGE.format(workspace=name)


ROOM_ARCHIVED_MESSAGE = "This channel is archived. Unarchive it to post."
API_ERROR_STATUS = {
    "LOCAL_SESSION_FORBIDDEN": 403,
    "LOCAL_SESSION_NOT_FOUND": 404,
    "LOCAL_SESSION_CONFLICT": 409,
    "LOCAL_SESSION_UNSUPPORTED": 409,
    "LOCAL_SESSION_OFFLINE": 409,
    "LOCAL_SESSION_QUEUE_FULL": 409,
    "INVALID_REQUEST": 400,
    "ROOM_FORBIDDEN": 403,
    "ROOM_NOT_FOUND": 404,
    "ACTIVITY_NOT_FOUND": 404,
    "ACTION_UNAVAILABLE": 409,
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
    "ROOM_NOT_DELETABLE": 409,
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
        "room.browser.frame",
        "room.metadata.changed",
        "room.huddle.changed",
        "room.meeting.changed",
        "room.read.changed",
        "room.delivery.changed",
    }
)
USER_LIVE_EVENT_TYPES = frozenset(
    {
        "room.message.available",
        "room.activity.changed",
        "room.subscription.rotated",
        "room.access.removed",
        "runtime.operation.changed",
        "runtime.resource.invalidated",
        "paperclip.event",
        "room.member.updated",
        "workspace.sessions.changed",
        "workspace.session.attention",
        "workspace.session.screen",
        "workspace.session.typing",
        "digest.ready",
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


class LastMessage(ContractModel):
    """``META.lastMessage``: the newest live top-level message, as a <=140-char
    plain preview. Thread replies never change it."""

    message_id: str
    seq: int = Field(ge=1)
    author_id: str
    author_kind: AuthorKind
    author_name: str
    text_preview: str
    has_attachments: bool = False
    created_at: datetime
    thread_root_message_id: str | None = None


class RoomSummary(ContractModel):
    room_id: uuid.UUID
    name: str | None
    kind: RoomKind
    room_revision: int = Field(ge=1)
    last_message_seq: int = Field(ge=0)
    # The latest live top-level message's time, else the room's createdAt. The
    # room list is sorted on it (newest first, ties by roomId).
    last_message_at: datetime | None
    last_message: LastMessage | None = None
    # Top-level messages above the caller's read floor, never their own, capped
    # at 99 (a deleted one still counts until read). The list is the reconcile point.
    unread_count: int = Field(ge=0)
    # Messages @mentioning the caller above that floor, thread replies included, capped at 99.
    mention_count: int = Field(ge=0, default=0)
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
    # The viewer belongs to another workspace than the room's owner (a vendor shared-channel guest).
    guest: bool = False
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
    # Where the run answers: None is the main conversation (the ledger's ``threadRootId``).
    thread_root_message_id: str | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None


class MemberReadCursor(ContractModel):
    user_id: str
    last_read_seq: int = Field(ge=0)


class AgentDelivery(ContractModel):
    """How far the agent's runtime has accepted the room: past a message addressed to
    it means that turn was delivered."""

    agent_id: str
    last_delivered_seq: int = Field(ge=0)


class RoomDetail(RoomSummary):
    # Re-declared as required: the list tolerates their absence for a client
    # holding a response from before they existed, the detail never does.
    members: list[RoomMember]
    last_read_seq: int = Field(ge=0)
    delivery_key: uuid.UUID
    created_at: datetime
    pinned_message_ids: list[str] = []
    active_runs: list[ActiveRun] = []
    # Receipts: every CURRENT human member's read cursor (the viewer's included) and
    # every CURRENT agent member's delivery cursor. Kept live by room.read.changed /
    # room.delivery.changed on the room channel.
    read_cursors: list[MemberReadCursor] = []
    agent_deliveries: list[AgentDelivery] = []


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
    via: Literal["lightbulb_mcp"] | None = None
    # An agent reply's trigger message (the `to` edge Jev reads on agent lines); absent otherwise.
    in_reply_to: str | None = None
    # The agents this message addresses -- a `user_request` dispatch target
    # (an @mention, a name-addressed open, or a DM), not a `room_event` ambient
    # candidate: those are merely eligible for Jev to route, and Jev picks at
    # most one (or none), so listing every candidate here would call a plain
    # message "Delivered" to agents nobody chose. Only the agent ids ride here;
    # nothing else from `dispatchTargets` (e.g. eventKind) is public.
    # See packages/shared/src/messaging/readState.ts.
    addressed_agent_ids: list[str] = Field(default_factory=list)
    attachments: list["MessageAttachment"] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)
    # Session chips: references, like document links (owner ruling 2026-10-02). They never
    # wake the session; the labels are the readable snapshot taken when the message was sent.
    session_mentions: list[str] = Field(default_factory=list)
    session_target_labels: dict[str, str] = Field(default_factory=dict)
    reactions: dict[str, list[str]] = Field(default_factory=dict)
    reaction_revision: int = Field(default=0, ge=0)
    message_revision: int = Field(default=0, ge=0)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler):
        result = handler(self)
        if self.via is None:
            result.pop("via", None)
        if self.in_reply_to is None:
            result.pop("inReplyTo", None)
            result.pop("in_reply_to", None)
        if not self.session_mentions:
            for key in ("sessionMentions", "session_mentions", "sessionTargetLabels", "session_target_labels"):
                result.pop(key, None)
        return result


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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/csv",
        "text/plain",
        "text/markdown",
        "video/mp4",
        "video/webm",
        "video/quicktime",
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
    session_mentions: list[str] = Field(default_factory=list, max_length=4)
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


class UploadedAttachment(ContractModel):
    """A file the runtime already PUT to its own attachment key (too big for an inline POST).

    ``attachment_id`` names ``<owner>/hermes/attachments/<room>/<id><suffix>``; the backend
    HEADs and sniffs that object before it becomes an attachment (``adopt_uploaded_attachment``).
    """

    attachment_id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)


class AgentMessageRequest(ContractModel):
    event_id: str
    room_id: uuid.UUID
    agent_id: uuid.UUID
    text: str
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None
    run_id: uuid.UUID | None = None
    attachments: list[InlineAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)
    uploaded_attachments: list[UploadedAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)

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
        if not self.text.strip() and not self.attachments and not self.uploaded_attachments:
            raise ValueError("message requires text or an attachment")
        if len(self.attachments) + len(self.uploaded_attachments) > ATTACHMENT_MAX_COUNT:
            raise ValueError(f"at most {ATTACHMENT_MAX_COUNT} attachments")
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


PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
QUOTA_PROVIDER_NAMES = {"chatgpt": "ChatGPT", "openai": "OpenAI", "anthropic": "Anthropic"}


def _reset_epoch(value: Any) -> int | None:
    """ISO-8601 or epoch seconds -> epoch seconds, or None for anything else (or implausible)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):  # NaN/Infinity parse as JSON
            return None
        epoch = int(value)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        epoch = int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
    else:
        return None
    return epoch if 1_000_000_000 <= epoch < 10_000_000_000 else None


def parse_quota_detail(raw: Any) -> dict[str, Any] | None:
    """The bridge's optional ``error_detail`` reduced to what we render: a short provider
    identifier and ``resets_at`` as epoch seconds. Anything else is dropped. Bridge-supplied,
    so never trusted as text: the provider only ever selects a display name."""
    if not isinstance(raw, dict):
        return None
    detail: dict[str, Any] = {}
    provider = raw.get("provider")
    if isinstance(provider, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", provider.lower()):
        detail["provider"] = provider.lower()
    resets_at = _reset_epoch(raw.get("resets_at", raw.get("resetsAt")))
    if resets_at is not None:
        detail["resets_at"] = resets_at
    return detail or None


def provider_quota_message(detail: dict[str, Any] | None) -> str:
    """Plain sentence for an out-of-quota provider. Timezone-neutral: a stored message can't be
    localized by the client, so the reset time is spelled in UTC."""
    detail = detail or {}
    name = QUOTA_PROVIDER_NAMES.get(detail.get("provider") or "")
    resets_at = detail.get("resets_at")
    if resets_at:
        at = datetime.fromtimestamp(resets_at, timezone.utc)
        when = f"{at:%b} {at.day}, {at:%H:%M} UTC"
    else:
        when = "soon"
    if name:
        subject, upgrade = f"Your {name} plan", f"Upgrade your {name} plan"
    else:
        subject, upgrade = "Your AI provider", "Upgrade your plan with them"
    return (
        f"{subject} has hit its usage limit — it resets {when}. {upgrade}, "
        "or switch this workspace's model in Settings → Model & Credits."
    )


class RunEventRequest(ContractModel):
    run_id: uuid.UUID
    type: Literal["run.started", "run.heartbeat", "run.ended"]
    at: datetime
    session_key: str | None = Field(default=None, max_length=512)
    outcome: Literal["completed", "error", "aborted", "ignored", "gate_error"] | None = None
    error_kind: str | None = Field(default=None, max_length=128)
    # Only meaningful with error_kind="provider_quota_exhausted"; normalized, never a 422.
    error_detail: dict[str, Any] | None = None
    usage: RunUsage | None = None
    # A completed ambient turn the agent declined ([SILENT]): no reply was posted, by choice.
    silent: bool = False

    @field_validator("error_detail", mode="before")
    @classmethod
    def _normalize_error_detail(cls, value: Any) -> dict[str, Any] | None:
        return parse_quota_detail(value)

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
    # A voice run runs on the room's own key but has no room ledger row, so it reports here.
    family: Literal["voice"] | None = None


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
        via=item.get("via"),
        in_reply_to=item.get("inReplyTo"),
        addressed_agent_ids=[
            t["agentId"]
            for t in item.get("dispatchTargets") or []
            if t.get("agentId") and t.get("eventKind") == "user_request"
        ],
        attachments=[] if deleted else item.get("attachments") or [],
        # Messages from before 2026-10-02 carry no sessionMentions; their labels name the same ids.
        session_mentions=[] if deleted else item.get("sessionMentions") or list(item.get("sessionTargetLabels") or {}),
        session_target_labels={} if deleted else item.get("sessionTargetLabels") or {},
        reactions={} if deleted else item.get("reactions") or {},
        reaction_revision=int(item.get("reactionRevision", 0)),
        message_revision=0 if original_request else int(item.get("messageRevision", 0)),
        edited_at=None if original_request else item.get("editedAt"),
        deleted_at=None if original_request else item.get("deletedAt"),
    )


# Local file, empty archive, and spanned archive signatures -- shared by
# application/zip and the three Office types below, which are zip containers
# (OOXML) under the hood. No archive parsing follows this bounded signature
# check for ANY of the four: the same "outer container only" posture already
# accepted for plain ZIP, not a new one introduced for Office docs.
_ZIP_SIGNATURE = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ATTACHMENT_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/gif": ((b"GIF87a", b"GIF89a"), ".gif"),
    "image/webp": (b"RIFF", ".webp"),
    "application/pdf": (b"%PDF-", ".pdf"),
    "application/zip": (_ZIP_SIGNATURE, ".zip"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (_ZIP_SIGNATURE, ".xlsx"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (_ZIP_SIGNATURE, ".docx"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (_ZIP_SIGNATURE, ".pptx"),
    # None signature = no fixed byte prefix to check; validate_attachment_bytes
    # requires valid UTF-8 instead, the natural analog of a signature for text.
    "text/csv": (None, ".csv"),
    "text/plain": (None, ".txt"),
    "text/markdown": (None, ".md"),
    # ISO BMFF (mp4 / QuickTime): the ``ftyp`` box sits at offset 4, after its 4-byte size --
    # checked in ``_signature_ok``. WebM is Matroska: the EBML magic at offset 0.
    "video/mp4": (b"ftyp", ".mp4"),
    "video/quicktime": (b"ftyp", ".mov"),
    "video/webm": (b"\x1a\x45\xdf\xa3", ".webm"),
}
#: The bytes ``_signature_ok`` needs to judge any signed type: a staged upload is sniffed from
#: a ranged GET of exactly this much, never read whole.
SNIFF_BYTES = 16


def _signature_ok(content_type: str, head: bytes) -> bool:
    signature = _ATTACHMENT_SIGNATURES[content_type][0]
    if content_type in ("video/mp4", "video/quicktime"):
        return head[4:8] == signature
    signatures = signature if isinstance(signature, tuple) else (signature,)
    valid = any(head.startswith(value) for value in signatures)
    if content_type == "image/webp":
        valid = valid and len(head) >= 12 and head[8:12] == b"WEBP"
    return valid


def canonical_attachment_content_type(file_name: str, content_type: str) -> str:
    declared = content_type.lower().split(";", 1)[0].strip()
    zip_name = PurePath(file_name).suffix.lower() == ".zip"
    if declared in {"application/zip", "application/x-zip-compressed"}:
        if not zip_name:
            raise ValueError("ZIP attachments must use a .zip filename")
        return "application/zip"
    if zip_name:
        if declared in {"", "application/octet-stream"}:
            return "application/zip"
        raise ValueError("attachment filename extension does not match the declared content type")
    return declared


def validate_attachment_bytes(content_type: str, data: bytes) -> str:
    expected = _ATTACHMENT_SIGNATURES.get(content_type)
    cap = ATTACHMENT_MAX_BYTES_BY_TYPE.get(content_type)
    if not expected or not cap or not data or len(data) > cap:
        raise ValueError(
            "attachment must be PNG, JPEG, WebP, GIF, PDF, ZIP, XLSX, DOCX, PPTX, CSV, TXT, MD, MP4, WebM, or MOV, "
            "and at most 5 MiB for images/text, 20 MiB for PDF/ZIP/Office documents or 100 MiB for video"
        )
    signature, suffix = expected
    if signature is None:
        # Text types carry no fixed byte prefix -- the natural analog of a
        # signature check is that the bytes are valid UTF-8 text at all.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("attachment bytes do not match the declared content type") from exc
        return suffix
    if not _signature_ok(content_type, data[:SNIFF_BYTES]):
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
    safe_name = _safe_file_name(file_name, suffix)
    attachment = MessageAttachment(id=attachment_id, file_name=safe_name, content_type=content_type, size=len(data))
    hermes_profiles.put_owner_object(attachment_key(owner_id, room_id, f"{attachment_id}{suffix}"), data)
    hermes_profiles.put_owner_object(
        attachment_key(owner_id, room_id, f"{attachment_id}.json"),
        attachment.model_dump_json(by_alias=True).encode(),
    )
    return attachment


def _safe_file_name(file_name: str, suffix: str) -> str:
    safe_name = "".join(
        char for char in PurePath(file_name).name if ord(char) >= 0x20 and ord(char) != 0x7F and char not in "/\\"
    ).strip()
    return safe_name.encode("utf-8")[:255].decode("utf-8", "ignore") or f"attachment{suffix}"


def adopt_uploaded_attachment(
    owner_id: str, room_id: str, attachment_id: str, file_name: str, content_type: str
) -> MessageAttachment:
    """Turn an object the runtime PUT itself into an attachment, judged like an inline one.

    The runtime chose the id but not the key shape: the object must sit at
    ``attachments/<room>/<id><suffix>`` for a signed type (text has no head to sniff, and is
    small enough to go inline). Size comes from a HEAD and the type from a ranged GET of the
    first bytes, so a 100 MB video is never pulled through the backend. Re-adopting the same id
    (a retried reply) just rewrites the same metadata."""
    content_type = canonical_attachment_content_type(file_name, content_type)
    expected = _ATTACHMENT_SIGNATURES.get(content_type)
    if not expected or expected[0] is None:
        raise ValueError(f"{content_type} cannot be uploaded directly")
    normalized = str(uuid.UUID(attachment_id))
    suffix = expected[1]
    key = attachment_key(owner_id, room_id, f"{normalized}{suffix}")
    size = hermes_profiles.owner_object_size(key)
    if size is None:
        raise ValueError("uploaded attachment not found")
    if not 0 < size <= ATTACHMENT_MAX_BYTES_BY_TYPE[content_type]:
        raise ValueError(f"attachment must be at most {ATTACHMENT_MAX_BYTES_BY_TYPE[content_type]} bytes")
    if not _signature_ok(content_type, hermes_profiles.get_owner_object_head(key, SNIFF_BYTES)):
        raise ValueError("attachment bytes do not match the declared content type")
    attachment = MessageAttachment(
        id=uuid.UUID(normalized), file_name=_safe_file_name(file_name, suffix), content_type=content_type, size=size
    )
    hermes_profiles.put_owner_object(
        attachment_key(owner_id, room_id, f"{normalized}.json"),
        attachment.model_dump_json(by_alias=True).encode(),
    )
    return attachment


ATTACHMENT_URL_TTL_S = 900


def attachment_url(owner_id: str, room_id: str, attachment_id: str) -> str:
    """A short-lived presigned GET for one attachment -- how a client PLAYS a video (``<video
    src>`` sends no bearer and API Gateway cannot return 100 MB). Metadata is read first, so an
    id with no stored attachment behind it never yields a URL."""
    normalized = str(uuid.UUID(attachment_id))
    try:
        raw = hermes_profiles.get_owner_object(attachment_key(owner_id, room_id, f"{normalized}.json"))
        if raw is None:
            raise ValueError("attachment metadata not found")
        metadata = MessageAttachment.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Attachment not found.") from exc
    disposition = "attachment" if metadata.content_type == "application/zip" else "inline"
    return hermes_profiles.presign_owner_object(
        attachment_key(owner_id, room_id, f"{normalized}{_ATTACHMENT_SIGNATURES[metadata.content_type][1]}"),
        content_type=metadata.content_type,
        disposition=f"{disposition}; filename*=UTF-8''{quote(metadata.file_name, safe='')}",
        expires_in=ATTACHMENT_URL_TTL_S,
    )


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


async def refresh_agent_member_names(
    owner_id: str, agent_id: str, roster: list[dict[str, Any]] | None = None
) -> dict[str, str | None]:
    """A Paperclip rename onto every room member row of this agent (labels and @handle), then
    ``room.member.updated`` to each room's people. Returns ``{roomId: old displayName}``.

    ponytail: only this agent's handle is re-stamped; another agent whose collision suffix the
    rename changed keeps its old handle until it is next upserted."""
    roster = roster if roster is not None else await load_agent_roster(owner_id)
    row = next((r for r in roster if r["id"] == agent_id), None)
    if row is None:
        return {}
    handle = mention_handles(roster).get(agent_id)
    publisher = get_live_event_publisher()
    old: dict[str, str | None] = {}
    for entry in await messaging_repo.list_rooms_for_member(owner_id, "agent", agent_id):
        room_id, room_owner = entry["meta"]["roomId"], entry["meta"]["ownerId"]
        if not await messaging_repo.set_agent_names(room_id, agent_id, row["name"], handle):
            continue
        old[room_id] = entry["member"].get("displayName")
        event = make_live_event("room.member.updated", {"roomId": room_id})
        for member in await messaging_repo.list_members(room_id):
            if member.get("kind") != "human":
                continue
            try:
                await publisher.publish_user(messaging_repo.member_home(member, room_owner), member["id"], event)
            except Exception:  # noqa: BLE001 -- labels already landed; a missed refresh heals on reload
                logger.warning("room.member.updated publish failed for %s in %s", member["id"], room_id)
    return old


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


async def require_room_member(owner_id: str, user_id: str, room_id: str) -> tuple[dict, dict]:
    room_id = validate_uuid(room_id, label="room ID")
    meta, member = await asyncio.gather(
        messaging_repo.get_room(room_id),
        messaging_repo.get_member(room_id, "human", user_id),
    )
    # A deleted room is gone for everyone: every member door answers 404, never a reconcile hint.
    if meta is None or meta.get("deletedAt"):
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


async def _mention_count(meta: dict, member: dict, read: dict | None, user_id: str) -> int:
    floor = messaging_repo.read_floor(member, read)
    if floor >= int(meta["lastSeq"]):
        return 0  # a read room costs no query
    return await messaging_repo.mention_count(meta["roomId"], user_id, floor)


def _room_summary_fields(meta: dict, member: dict, read: dict | None, mention_count: int = 0) -> dict[str, Any]:
    """The RoomSummary half of a room projection — RoomDetail extends it."""
    return {
        "room_id": meta["roomId"],
        "name": meta.get("name"),
        "kind": meta["kind"],
        "room_revision": int(meta["revision"]),
        "last_message_seq": int(meta["lastSeq"]),
        "last_message_at": (meta.get("lastMessage") or {}).get("createdAt") or meta.get("createdAt"),
        "last_message": meta.get("lastMessage"),
        "unread_count": messaging_repo.unread_count(meta, member, read),
        "mention_count": mention_count,
        "last_read_seq": int((read or {}).get("lastReadSeq", 0)),
        "updated_at": meta["updatedAt"],
        "starred": bool(member.get("starred")),
        "muted": bool(member.get("muted")),
        "section_id": member.get("sectionId"),
        "topic": meta.get("topic"),
        "workspace_id": meta.get("workspaceId"),
        "shared": is_vendor_room(meta),
        "guest": messaging_repo.member_home(member, meta["ownerId"]) != meta["ownerId"],
        "is_default": is_default_room(meta),
        "archived_at": meta.get("archivedAt"),
        "archived_by": meta.get("archivedBy"),
    }


async def _room_detail(meta: dict, user_id: str, member: dict | None = None) -> RoomDetail:
    member = member or await messaging_repo.get_member(meta["roomId"], "human", user_id)
    if member is None:
        raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.")
    members, read, open_runs, cursors, deliveries = await asyncio.gather(
        messaging_repo.list_members(meta["roomId"]),
        messaging_repo.get_read_row(meta["roomId"], user_id),
        # The run ledger is partitioned by the ROOM's owner — the customer org
        # even on a room shared with the vendor workspace.
        messaging_repo.list_open_runs_for_room(meta["ownerId"], meta["roomId"]),
        messaging_repo.list_read_cursors(meta["roomId"]),
        messaging_repo.list_agent_deliveries(meta["roomId"]),
    )
    emails, mentions = await asyncio.gather(human_emails(members), _mention_count(meta, member, read, user_id))
    agent_ids = {row["id"] for row in members if row["kind"] == "agent"}
    human_ids = {row["id"] for row in members if row["kind"] == "human"}
    return RoomDetail(
        **_room_summary_fields(meta, member, read, mentions),
        delivery_key=meta["deliveryKey"],
        members=[_room_member(row) for row in with_human_handles(members, emails)],
        created_at=meta["createdAt"],
        pinned_message_ids=[str(value) for value in meta.get("pinnedMessageIds", [])],
        # A run whose agent has left the room is as over as one that never
        # existed — the rule GET /messaging/runs/{id} already applies.
        active_runs=[
            ActiveRun(
                run_id=row["runId"],
                agent_id=row["agentId"],
                trigger_message_id=row.get("triggerMessageId"),
                thread_root_message_id=row.get("threadRootId"),
                started_at=row.get("startedAt"),
                last_heartbeat_at=row.get("lastHeartbeatAt"),
            )
            for row in open_runs
            if row["agentId"] in agent_ids
        ],
        # An ex-member's leftover row is not a receipt.
        read_cursors=[
            MemberReadCursor(user_id=row["userId"], last_read_seq=int(row.get("lastReadSeq", 0)))
            for row in cursors
            if row.get("userId") in human_ids
        ],
        agent_deliveries=[
            AgentDelivery(
                agent_id=row["agentId"], last_delivered_seq=int(row.get("lastDeliveredSeq", row.get("lastSeq", 0)))
            )
            for row in deliveries
            if row.get("agentId") in agent_ids
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
# Note this bounds ROOMS, and each one issues three calls concurrently (plus one
# small mention COUNT query for a room with unread) — so the
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
    discovered = [
        row
        for row in await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
        # ``hidden``: a Google Meet's room -- the meeting's record, never a place people go.
        if not row["meta"].get("deletedAt") and not row["meta"].get("hidden")
    ]
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def summarise(row: dict) -> RoomSummary | None:
        meta = row["meta"]
        room_id = meta["roomId"]
        async with limit:
            member, read, members = await asyncio.gather(
                messaging_repo.get_member(room_id, "human", user_id),
                messaging_repo.get_read_row(room_id, user_id),
                messaging_repo.list_members(room_id),
            )
            if member is None:
                return None
            emails, mentions = await asyncio.gather(human_emails(members), _mention_count(meta, member, read, user_id))
        return RoomSummary(
            **_room_summary_fields(meta, member, read, mentions),
            members=[_room_member(item) for item in with_human_handles(members, emails)],
        )

    settled = await asyncio.gather(*(summarise(row) for row in discovered))
    summaries = [summary for summary in settled if summary is not None]
    summaries.sort(key=lambda room: str(room.room_id))
    summaries.sort(key=lambda room: room.last_message_at or room.updated_at, reverse=True)
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
            "listenMode": "active",
        }
        for key in _STARTER_KEYS
    )
    client_room_id = f"workspace-bootstrap-v2:{owner_id}"
    # Resolve the welcome BEFORE the room exists: an empty room is visible the moment
    # create_room returns, and a Clerk call that pauses there would let the owner's
    # first message take seq 1 -- which hides the starter chips for good.
    org = await clerk_admin.get_organization(owner_id) or {}
    welcome = default_room_welcome(org.get("name"))
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
        req_hash=messaging_repo.request_hash(welcome, ceo_id),
        author={"kind": "agent", "id": ceo_id, "displayName": agents[ceo_id]["name"]},
        text=welcome,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=ceo_id,
        client_message_id=None,
    )
    return room_id


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


async def _find_existing_dm(owner_id: str, user_id: str, counterpart: CreateRoomMember) -> dict | None:
    """The caller's own DM with this exact counterpart, if one already exists.

    A ``dm`` room is 1:1 by construction (creator + one counterpart), so the
    pair (creator, counterpart) determines the room. The frontend's own
    ``resolveDm`` (packages/shared/src/messaging/useRooms.ts) already reuses an
    existing DM for the roster-row click path, but that check is client-side
    and per-path -- the generic create-conversation dialog (and any other
    caller: mobile, an agent-driven create, a race between two tabs) posts
    straight to this endpoint with nothing to catch a duplicate. This is the
    server-side twin, so every caller gets the same guarantee.

    ``list_rooms_for_member`` is discovery-only (the ``by-member`` GSI), so
    each candidate is rechecked against the creator's own strongly consistent
    ``MEMBER#`` row before it's trusted -- same rule ``require_room_member`` follows.
    """
    candidates = await messaging_repo.list_rooms_for_member(owner_id, counterpart.kind, counterpart.id)
    for candidate in candidates:
        meta = candidate["meta"]
        if meta.get("kind") != "dm" or meta.get("deletedAt"):
            continue
        if await messaging_repo.get_member(meta["roomId"], "human", user_id) is not None:
            return meta
    return None


async def create_room(auth: AuthContext, request: CreateRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    keys = [(member.kind, member.id) for member in request.members]
    if len(keys) != len(set(keys)) or ("human", auth.user_id) in keys:
        raise ApiProblem("INVALID_REQUEST", "Room members must be unique and omit the creator.")
    if len(request.members) + 1 > messaging_repo.MAX_ROOM_MEMBERS:
        raise ApiProblem("MEMBER_LIMIT", f"Rooms hold at most {messaging_repo.MAX_ROOM_MEMBERS} members.")
    if request.kind == "dm" and len(request.members) == 1:
        existing = await _find_existing_dm(owner_id, auth.user_id, request.members[0])
        if existing is not None:
            return await _room_detail(existing, auth.user_id)
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
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    updates = request.named_updates()
    # A group DM has no admins -- only its creator holds a role (owner); every other member
    # is a plain member (both clients create it that way) -- and it is their shared
    # conversation, so any member may archive/unarchive one. Everything else is owner/admin.
    if member.get("role") not in {"owner", "admin"} and (updates or meta.get("kind") != "group-dm"):
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
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
    if updates.get("archivedAt"):
        await livekit_huddles.close_room(room_id)  # an archived channel holds no huddle; never raises
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)


def _require_archivable(meta: dict) -> None:
    """A plain channel or a group DM archives; a 1:1 DM does not. #general is where
    join_default_room seats every new teammate and onboarding renders; the shared vendor
    channel is the customer's support line."""
    if meta.get("kind") not in {"channel", "group-dm"} or is_default_room(meta) or is_vendor_room(meta):
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
    if meta.get("kind") in {"dm", "group-dm"} and request.role != "member":
        raise ApiProblem("INVALID_REQUEST", "Group chat members must use the member role.")
    existing = await messaging_repo.get_member(room_id, request.kind, request.id)
    # Any member may change an agent's listen mode, on every plan (owner ruling 2026-09-24);
    # Channels keep owner/admin membership management. A direct or group chat
    # has no privileged administrator, so any participant may add another
    # workspace member (always at the ordinary member role).
    # Only the room-owning workspace's own members: in the shared vendor room a
    # Lightbulb-side member would be spending the customer's inference.
    mode_change = (
        request.kind == "agent"
        and existing is not None
        and existing.get("role") == request.role
        and messaging_repo.member_home(actor, owner_id) == owner_id
    )
    # An existing ordinary member is the retry of an addition that already committed
    # (lost response, lost race): a no-op, not a privilege change.
    chat_addition = (
        meta.get("kind") in {"dm", "group-dm"}
        and request.role == "member"
        and (existing is None or existing.get("role") == "member")
    )
    if actor.get("role") not in {"owner", "admin"} and not mode_change and not chat_addition:
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    if existing and existing.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner role cannot be changed.")
    roster = await load_agent_roster(owner_id) if request.kind == "agent" else []
    member = await _resolved_member(owner_id, request, roster)
    if request.kind == "agent":
        # A person's choice of mode; a system sweep never overrides it.
        member = {**member, "modeSetBy": "user"}
    if request.kind == "human":
        await messaging_repo.ensure_user_stream(owner_id, request.id)
    handles = mention_handles(roster) if roster else {}
    current = await messaging_repo.list_members(room_id)
    room_agents = [row for row in current if row.get("kind") == "agent"]
    handle_updates = {row["id"]: handles[row["id"]] for row in room_agents if row["id"] in handles}
    try:
        meta = await messaging_repo.upsert_member(
            room_id=room_id,
            owner_id=owner_id,
            member=member,
            handle_updates=handle_updates,
            room_kind="group-dm" if meta.get("kind") == "dm" and existing is None else None,
        )
    except messaging_repo.MemberLimitExceeded as exc:
        raise ApiProblem("MEMBER_LIMIT", str(exc)) from exc
    if existing is None:
        _emit_member_added(
            owner_id, auth.user_id, request.kind, by="owner" if actor.get("role") == "owner" else "member"
        )
        # Every person in the room re-reads it: the newcomer has not discovered the room
        # yet, and the others may be viewing another room while its kind or roster changes.
        # Sidebars refresh only on this private event (useRooms does not poll the list).
        humans = [
            (messaging_repo.member_home(row, owner_id), row["id"]) for row in current if row.get("kind") == "human"
        ]
        if request.kind == "human":
            humans.append((owner_id, request.id))
        publisher = get_live_event_publisher()
        event = make_live_event("room.member.updated", {"roomId": room_id})
        # Best effort and concurrent: the membership already committed, and a retry takes
        # the existing-member path, so one failed stream must not starve the others.
        results = await asyncio.gather(
            *(publisher.publish_user(home, user_id, event) for home, user_id in humans), return_exceptions=True
        )
        for (_, user_id), result in zip(humans, results):
            if isinstance(result, Exception):
                logger.warning("room.member.updated publish failed for %s in %s: %s", user_id, room_id, result)
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
    """A member removes themselves. A channel's owner must transfer ownership or delete
    the channel instead, so this is a hard refusal. A group DM's
    "owner" is just whoever started it and holds no powers there, so they may leave too."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    _deny_vendor_human_change(meta, "human")
    if member.get("role") == "owner" and meta.get("kind") != "group-dm":
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

    new_key = str(uuid.uuid4())
    notices = await _rotation_notices(meta, new_key, removed={member_id})
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
    # The same revocation for the room's huddle: best effort, never raises.
    await livekit_huddles.remove_participant(meta["roomId"], member_id)
    return new_meta


async def _rotation_notices(meta: dict, new_key: str, *, removed: set[str] | None = None) -> list[dict]:
    """The durable ``LIVE`` rows a deliveryKey rotation writes in its own transaction:
    ``room.access.removed`` to each human in ``removed`` (every human when None),
    ``room.subscription.rotated`` with the new key to the rest."""
    members = await messaging_repo.list_members(meta["roomId"])
    humans = [row for row in members if row.get("kind") == "human"]
    revision = int(meta["revision"]) + 1
    notices: list[dict] = []
    for human in humans:
        home = messaging_repo.member_home(human, meta["ownerId"])
        key = await messaging_repo.ensure_user_stream(home, human["id"])
        is_removed = removed is None or human["id"] in removed
        event = make_live_event(
            "room.access.removed" if is_removed else "room.subscription.rotated",
            (
                {"roomId": meta["roomId"], "roomRevision": revision}
                if is_removed
                else {"roomId": meta["roomId"], "deliveryKey": new_key, "roomRevision": revision}
            ),
            room_id=meta["roomId"],
            room_revision=revision,
        )
        notices.append({"userId": human["id"], "ownerId": home, "channelKey": key, "event": event})
    return notices


async def delete_room(auth: AuthContext, room_id: str) -> None:
    """Owner-only channel delete, as a TOMBSTONE: META gains deletedAt/deletedBy and the
    deliveryKey rotates with a ``room.access.removed`` notice to every human in the same
    transaction -- the revocation contract of a human removal, applied to the whole room.
    Messages and members stay. #general, the shared vendor channel and DMs refuse, like
    archive; a room already deleted is a 404 from require_room_member."""
    owner_id = resolve_owner_id(auth)
    for attempt in range(3):
        meta, member = await require_room_member(owner_id, auth.user_id, room_id)
        if member.get("role") != "owner":
            raise ApiProblem("ROOM_FORBIDDEN", "Only the channel's owner can delete it.")
        if meta.get("kind") != "channel" or is_default_room(meta) or is_vendor_room(meta):
            name = "#general" if is_default_room(meta) else "This room"
            raise ApiProblem("ROOM_NOT_DELETABLE", f"{name} can't be deleted.")
        new_key = str(uuid.uuid4())
        notices = await _rotation_notices(meta, new_key)
        try:
            await messaging_repo.delete_room(
                meta=meta, deleted_by=auth.user_id, new_delivery_key=new_key, live_events=notices
            )
            await livekit_huddles.close_room(meta["roomId"])  # everyone leaves its huddle; never raises
            return
        except messaging_repo.RoomNotFound:
            # Lost the revision CAS (or a concurrent delete): re-read. The next
            # require_room_member answers 404 if the room is now gone.
            if attempt == 2:
                raise


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


def _root_seq(links: dict[str, dict], thread_root_message_id: Any) -> int | None:
    """The validated thread root's seq, so the reply can name its root by key."""
    row = links.get(str(thread_root_message_id)) if thread_root_message_id else None
    return int(row["messageSeq"]) if row else None


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


# --------------------------------------------------------------------------- addressing stage
#
# Routing v2 §3.1 (docs/superpowers/specs/2026-10-02-voice-architecture-v2-design.md): who a human's
# message speaks TO, decided once, before targets are picked. Chat (create_human_message) and Live
# (voice_calls.route_live_line) both call ``address``. A name anywhere is a candidate; one Jev call
# labels each, with a deterministic fast path and fallback. Measured: bench [AX] cued recall
# 0.62/0.56 -> 1.00/1.00, false-speak flat.

#: The opening grammar of an address ("Hey Friday, ...", "Friday can you ..."): the fast path's test.
_NAME_GREETING = r"(?:(?:hey|hi|hello|yo)[\s,]+)?"
_NAME_FOLLOW = r"(?:\s*,|\s+[-–—]\s|\s+(?:can|could|would|will)\s+(?:you|u)\b|\s+please\b)"
_STRONG_LEAD = r"\s*(?:hey|ok|okay)[\s,]+@?"
#: Our agents' names collide with weekdays ("Friday"): never a fast path, never addressed on a fallback.
WEEKDAYS = frozenset({"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"})
#: A weekday used as a day: "by Friday", "next Friday", "Friday at 3", "Friday, we ship the beta".
_DAY_USE = (
    r"(?:\b(?:on|by|until|till|before|after|this|next|last|every|from)\s+{day}\b"
    r"|\b{day}(?:'s)?\s+(?:morning|afternoon|evening|night|at\b|\d)"
    r"|\b{day}[\s,:-]+(?:we|we'll|we're|let's|i'll|everyone)\s+(?:\w+\s+){{0,2}}?"
    r"(?:ship|launch|release|deploy|meet|demo|present|freeze|start|kick|wrap|close|review|finish|cut|go\s+live)\b)"
)
ADDRESSING_TIMEOUT_S = 2.0
#: Below this P(complete) a Live line is held for the speaker's next final; chat routes anyway.
HOLD_BELOW = 0.5
ADDRESS_INSTRUCTIONS = (
    "People and AI teammates share this chat. For the one name given, decide how the LATEST message "
    "uses it. addressed: the message speaks TO them, at the start, middle or end: calls, greets, asks "
    "or tells them something ('Friday, ...', 'Hey guys, hey Friday', 'What do you think, Finance?'). "
    "relayed: someone else is asked to get them to act, or they are proposed for the job ('@leo can "
    "you get Finance to tell us the runway?': Finance relayed, Leo addressed; 'Leo wants to know from "
    "Pulse ...': Pulse relayed; 'let's have Pulse draft it': Pulse relayed). mentioned: talked about, "
    "thanked, or told NOT to act, with nothing asked of them ('Friday said it was fine', 'thanks "
    "Friday', 'Not you Friday, Atlas, can you check?': Friday mentioned, Atlas addressed). "
    "not_a_name: the word is not this teammate or person here, but a weekday, a date or a common "
    "word ('Friday, we ship the beta' is the day; 'by Friday'; 'Pulse check'; 'Forge ahead')."
)
ADDRESS_LABELS = {
    "addressed": "Spoken to: called, greeted, asked or told something",
    "relayed": "Someone else is asked to get them to do it, or they are proposed to do it",
    "mentioned": "Talked about, thanked, or told not to act; nothing asked of them",
    "not_a_name": "Not this teammate or person: a weekday, date or common word",
}
COMPLETE_INSTRUCTIONS = (
    "Is the latest message a finished thought? No only when the speaker stopped mid-sentence and is "
    "still typing or talking ('Friday, we're trying to', 'we were hoping someone could', 'so the "
    "thing is'). Short messages ('hey', 'thanks Friday', 'Friday, are you there?') are finished."
)
ANYONE_INSTRUCTIONS = (
    "Apart from what it asks of the people or teammates it names, does the latest message also put "
    "a separate question or request to the room in general, to nobody in particular? ('how's the "
    "burn looking, and Ana, did you book the room?': yes; 'Ana, can you send me the contract?': no; "
    "'Ana, can you ask Gauge for the cohort numbers?': no)"
)
SPLIT_INSTRUCTIONS = (
    "The latest message may ask several teammates for different things in one breath. For the one name "
    "given: is the ask for that teammate only the part that follows its name ('own'), or does the whole "
    "message ask them together with the others ('Forge and Friday, check the deploy': 'all')?"
)


def _soundex(word: str) -> str:
    """The Live worker's soundex (apps/huddle-agent transcript.py), so a mis-heard name matches the same way."""
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return ""
    code = lambda c: next((str(d) for d, g in enumerate(("bfpv", "cgjkqsxz", "dt", "l", "mn", "r"), 1) if c in g), "")  # noqa: E731
    out, prev = letters[0].upper(), code(letters[0])
    for c in letters[1:]:
        d = code(c)
        if d and d != prev:
            out += d
        if c not in "hw":
            prev = d
    return (out + "000")[:4]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+|[^\s\-a-z0-9']", text.lower())


def _fuzzy(said: tuple[str, ...], name: tuple[str, ...]) -> bool:
    """The worker's STT-tolerant match ("Forj" is Forge, "Guys" is not Gauge). ponytail: a port of
    huddle_agent.transcript._fuzzy (the backend cannot import the worker); keep the two in step."""
    from difflib import SequenceMatcher

    if sum(len(w) for w in name) < 4:
        return False
    s, n = " ".join(said), " ".join(name)
    ratio = SequenceMatcher(None, s, n).ratio()
    if ratio >= 0.8:
        return True
    return ratio >= 0.6 and abs(len(s) - len(n)) <= 1 and all(_soundex(a) == _soundex(b) for a, b in zip(said, name))


def _member_names(member: dict) -> set[str]:
    display = member.get("displayName") or ""
    names = {display, member.get("mentionHandle") or ""}
    if member.get("kind") == "human" and display.split():
        names.add(display.split()[0])
    return {n for n in names if n}


def address_candidates(text: str, members: list[dict], *, live: bool = False) -> list[dict]:
    """Every agent and person named anywhere in ``text``: ``[{name, agents, humans, pos}]`` in text
    order, one per distinct name (a first name an agent shares is one candidate of both kinds).
    Chat matches whole words and @handles; Live also matches the worker's fuzzy/soundex way."""
    found: dict[str, dict] = {}
    tokens = _tokens(text) if live else []
    for member in members:
        if member.get("kind") not in ("agent", "human"):
            continue
        for name in _member_names(member):
            hit = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I)
            pos, said = (hit.start(), hit.group(0)) if hit else (None, None)
            if pos is None and live:
                words = tuple(_tokens(name))
                # ponytail: token index as position; fine for ordering, Live only
                i = next((i for i in range(len(tokens)) if _fuzzy(tuple(tokens[i : i + len(words)]), words)), None)
                if i is not None:
                    pos, said = i * 1000, " ".join(tokens[i : i + len(words)])
            if pos is None:
                continue
            display = member.get("displayName") or name
            key = (display.split()[0] if member["kind"] == "human" else display).lower()
            # ``said``: the words as written or heard ("Fryday"), which the weekday veto reads.
            row = found.setdefault(key, {"name": key, "agents": [], "humans": [], "pos": pos, "said": said})
            row["pos"] = min(row["pos"], pos)
            kind = "agents" if member["kind"] == "agent" else "humans"
            if member["id"] not in row[kind]:
                row[kind].append(member["id"])
            break
    return sorted(found.values(), key=lambda row: row["pos"])


def _opens_with(text: str, name: str, *, strong: bool = False) -> bool:
    lead = _STRONG_LEAD if strong else rf"\s*{_NAME_GREETING}@?"
    follow = r"(?![\w-])" if strong else rf"(?![\w-]){_NAME_FOLLOW}"
    return re.match(rf"{lead}{re.escape(name)}{follow}", text, re.I) is not None


_EDGE = " \t\r\n.,;:?!\"'\u201c\u201d\u2018\u2019-\u2013\u2014"
_TAIL = re.compile(r"(?:^|\s)(?:and|then|also|plus|hey|hi|ok|okay)$", re.I)
_CONNECTORS = {"and", "&", "plus", "with", "then"}


def _name_hits(text: str, cand: dict) -> list[re.Match]:
    """Every place the candidate's name stands in ``text``, as said: a live fuzzy candidate's ``pos`` is a
    token index (x1000), not a character offset."""
    return list(re.finditer(rf"(?<![\w-]){re.escape(cand['said'])}(?![\w-])", text, re.I))


def _clean(seg: str) -> str:
    seg = seg.strip(_EDGE)
    while (m := _TAIL.search(seg)) is not None:
        seg = seg[: m.start()].strip(_EDGE)
    return seg


def _empty(seg: str) -> bool:
    return not [w for w in re.findall(r"\w+|&", seg) if w.lower() not in _CONNECTORS]


def split_tasks(text: str, cands: list[dict]) -> list[tuple[str, str]]:
    """Each addressed agent's own part of a Live line, cut by code at ITS names (conversation engine
    §3.2): the words after a name up to the next given name. ``cands`` are the cut points, so the caller
    passes only the addressed or relayed ones. An empty part (the "and" of "Forge and Friday, check X")
    takes the next part; a name said twice joins its parts. If every part is empty the words before the
    first name are the shared ask; if there are none, ``[]`` (no split). ``[(name, part)]``, text order."""
    spots = sorted((h.start(), h.end(), c["name"]) for c in cands for h in _name_hits(text, c))
    if not spots:
        return []
    segs = [
        _clean(text[end : spots[i + 1][0] if i + 1 < len(spots) else len(text)]) for i, (_, end, _) in enumerate(spots)
    ]
    segs = [("" if _empty(seg) else seg) for seg in segs]
    if not any(segs):
        lead = _clean(text[: spots[0][0]])
        if _empty(lead):
            return []
        segs = [lead] * len(spots)
    for i in range(len(segs) - 2, -1, -1):
        segs[i] = segs[i] or segs[i + 1]
    joined: dict[str, list[str]] = {}
    for (_, _, name), seg in zip(spots, segs):
        joined.setdefault(name, [])
        joined[name] += [seg] if seg else []
    return [(name, " ".join(parts)) for name, parts in joined.items()]


def _opens_clause(text: str, cand: dict) -> bool:
    """The name opens a clause: at the start of the line, after . ? ! ; or after ", " / "and "."""
    return re.search(rf"(?:^\s*|[.?!;]\s+|,\s+|\band\s+){re.escape(cand['said'])}(?![\w-])", text, re.I) is not None


def _fast_path(text: str, cands: list[dict], live: bool) -> str | None:
    """One agent named, at the opening, in a finished message of 4+ words: addressed with no call."""
    if len(cands) != 1 or len(cands[0]["agents"]) != 1 or cands[0]["humans"] or cands[0]["name"] in WEEKDAYS:
        return None
    if len(text.split()) < 4 or (not live and not re.search(r"[.?!]\s*$", text)):
        return None
    return cands[0]["agents"][0] if _opens_with(text, cands[0]["name"]) else None


async def _label(
    latest: dict,
    cands: list[dict],
    members: list[dict],
    history: list[dict],
    live: bool,
    split: list[str] = (),
) -> dict | None:
    """The one Jev call: ``{labels: {name: label}, complete: p, anyone: p}``, or None on any failure."""
    from core.services import jev

    questions = {
        "complete": {"type": "boolean", "instructions": COMPLETE_INSTRUCTIONS},
        "anyone": {"type": "boolean", "instructions": ANYONE_INSTRUCTIONS},
    }
    for i, c in enumerate(cands):
        kind = (
            "both a person and an AI teammate here"
            if c["agents"] and c["humans"]
            else "an AI teammate"
            if c["agents"]
            else "a person in the room"
        )
        questions[f"n{i}"] = {
            "type": "choice",
            "instructions": f"{ADDRESS_INSTRUCTIONS} The name: '{c['name']}' ({kind}).",
            "criteria": ADDRESS_LABELS,
        }
    for i, name in enumerate(split):
        questions[f"t{i}"] = {
            "type": "choice",
            "instructions": f"{SPLIT_INSTRUCTIONS} The name: '{name}'.",
            "criteria": {"own": "Only the part that follows its name", "all": "The whole message"},
        }
    state = {
        "room": {
            "kind": "call" if live else "chat",
            "people": [m.get("displayName") for m in members if m.get("kind") == "human"],
            "aiTeammates": [m.get("displayName") for m in members if m.get("kind") == "agent"],
        },
        "recent": [jev._line(m) for m in history[-6:]],
        "latest": jev._line(latest),
    }
    try:
        answers = await asyncio.wait_for(jev.decide(state, questions), ADDRESSING_TIMEOUT_S)
        return {
            "labels": {c["name"]: answers[f"n{i}"]["choice"] for i, c in enumerate(cands)},
            "complete": float(answers["complete"]["probability"]),
            "anyone": float(answers["anyone"]["probability"]),
            # only "all" moves the text; anything else keeps the agent's own part
            "whole": {name for i, name in enumerate(split) if (answers.get(f"t{i}") or {}).get("choice") == "all"},
        }
    except Exception:  # noqa: BLE001 -- a timeout, a 429 string, a malformed answer: the fallback decides
        return None


async def address(latest: dict, members: list[dict], *, history: Any = (), live: bool = False) -> dict:
    """Who a human's ``latest`` message speaks to. ``{agents, humans, hold, anyone, path}``:
    ``agents`` addressed or relayed (all of them respond, like several @mentions), ``humans``
    addressed, ``hold`` (Live waits for the speaker's next final), ``anyone`` (something is asked
    of the room beyond the named, so the to-people silence rule does not apply). ``history`` is the
    rows before it, or an async loader called only when the Jev call happens."""
    text = str(latest.get("text") or "")
    out: dict = {"agents": [], "humans": [], "hold": False, "anyone": False, "path": "none"}
    cands = address_candidates(text, members, live=live)
    mentioned = {ref.get("id") for ref in latest.get("mentions") or [] if ref.get("kind") == "agent"}
    if not cands or all(c["agents"] and set(c["agents"]) <= mentioned and not c["humans"] for c in cands):
        # Nobody named, or only @mentioned agents (addressed anyway): today's path, no call.
        return out
    if (agent := _fast_path(text, cands, live)) is not None:
        out |= {"agents": [agent], "path": "fast"}
        put_metric("messaging.addressing.stage", dimensions={"path": "fast", "surface": "live" if live else "chat"})
        return out
    # Conversation engine §3.2: a live line naming 2+ agents asks each, in this call, own part or all.
    named = [c["name"] for c in cands if c["agents"]]
    split = len(named) >= 2 and live
    result = await _label(
        latest, cands, members, list(await history() if callable(history) else history), live, named if split else []
    )
    if result is None:
        # A safe fallback, not a substitute ([AX] (c): the common-word false-speaks come back). A weekday
        # that opens a clause of a multi-name live line ("..., Friday, a summary") is that teammate.
        labels = {
            c["name"]: "not_a_name"
            if c["name"] in WEEKDAYS and not (split and _opens_clause(text, c))
            else "addressed"
            if c["agents"]
            else "mentioned"
            for c in cands
        }
        result, out["path"] = {"labels": labels, "complete": 1.0, "anyone": 0.0, "whole": set()}, "fallback"
    else:
        out["path"] = "jev"
    out["hold"] = result["complete"] < HOLD_BELOW
    out["anyone"] = result["anyone"] >= 0.5
    hits = []
    for c in cands:
        label = result["labels"][c["name"]]
        if c["name"] in WEEKDAYS and re.search(_DAY_USE.format(day=re.escape(c["said"])), text, re.I):
            label = "not_a_name"  # domain knowledge, not a model call: our names are weekdays
        if label not in ("addressed", "relayed"):
            continue
        # A name several agents share addresses none of them (Jev picks among the listeners); a unique
        # @handle still names one. A name a person shares goes to the person unless "hey/ok <name>".
        agent = c["agents"][0] if len(c["agents"]) == 1 else None
        if agent and (not c["humans"] or (label == "addressed" and _opens_with(text, c["name"], strong=True))):
            out["agents"] += [agent] if agent not in out["agents"] else []
            hits.append((c, agent, label))
        elif label == "addressed":
            out["humans"] += c["humans"]
    if split and len(hits) >= 2:
        # Each addressed agent's part runs from its name to the next ADDRESSED name, cut only now the labels
        # exist. A relayed agent is no cut point and gets the whole line (it needs who asked what).
        segs = dict(split_tasks(text, [c for c, _, label in hits if label == "addressed"]))
        out["tasks"] = {}  # stays {} when nothing can be cut: /route then sends no per-agent requests
        for c, agent, label in hits if segs else []:
            whole = label == "relayed" or c["name"] in result["whole"]
            out["tasks"].setdefault(agent, text if whole else segs.get(c["name"]) or text)
    put_metric("messaging.addressing.stage", dimensions={"path": out["path"], "surface": "live" if live else "chat"})
    return out


def _stored_addressing(addressing: dict | None) -> dict | None:
    """What dispatch reads later, on the MSG row: only when the stage named someone."""
    if not addressing or addressing["path"] == "none":
        return None
    return {"people": addressing["humans"], "anyone": addressing["anyone"]}


def select_dispatch_targets(
    members: list[dict],
    mentions: list[dict],
    source_agent_id: str | None,
    *,
    room_kind: str | None = None,
) -> list[dict]:
    """``mentions`` carries the agents a message speaks to: its @mentions plus ``address``'s agents."""
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
    if "user_request" in targets.values():
        # A message that speaks to a teammate is not ambient: nobody else chimes in beside them
        # (prod 2026-09-27: "@friday ..." ran Friday and a Listening Pulse).
        targets = {agent_id: kind for agent_id, kind in targets.items() if kind == "user_request"}
    return [{"agentId": agent_id, "eventKind": event_kind} for agent_id, event_kind in sorted(targets.items())]


#: ``messaging.jev.missed`` (routing v2 §10): Jev answered "none" to a person, who then @mentioned an
#: agent within this window -- the chat miss rate.
_JEV_MISS_WINDOW_S = 120


async def _count_jev_miss(room_id: str, user_id: str, item: dict) -> None:
    try:
        rows = await messaging_repo.list_messages_before(room_id, int(item["messageSeq"]), 10)
        now = datetime.fromisoformat(str(item["createdAt"]).replace("Z", "+00:00"))
        for row in reversed(rows):
            author = row.get("author") or {}
            if author.get("kind") != "human" or author.get("id") != user_id:
                continue
            then = datetime.fromisoformat(str(row["createdAt"]).replace("Z", "+00:00"))
            kinds = {t.get("eventKind") for t in row.get("dispatchTargets") or []}
            if (now - then).total_seconds() <= _JEV_MISS_WINDOW_S and kinds == {"room_event"}:
                decision = await messaging_repo.get_decision(room_id, row["messageId"])
                if decision and decision.get("who") == "none" and not decision.get("forced"):
                    put_metric("messaging.jev.missed")
            return
    except Exception:  # noqa: BLE001 -- a metric never fails a send
        logger.info("jev miss check failed", exc_info=True)


async def _validate_mentions(room_id: str, mentions: list[MemberRef]) -> list[dict]:
    normalized = sorted({(ref.kind, ref.id) for ref in mentions})
    for kind, member_id in normalized:
        if await messaging_repo.get_member(room_id, kind, member_id) is None:
            raise ApiProblem("MEMBER_NOT_FOUND", "Mentioned member is not in this room.")
    return [{"kind": kind, "id": member_id} for kind, member_id in normalized]


async def create_human_message(
    auth: AuthContext,
    room_id: str,
    request: CreateMessageRequest,
    *,
    native_source_id: str | None = None,
    native_provider: str | None = None,
    native_hop: int | None = None,
    native_conditions: list[dict] | None = None,
    via: Literal["lightbulb_mcp"] | None = None,
    author_override: dict | None = None,
) -> CanonicalMessage:
    """``author_override`` is the hosted MCP's client identity; ``auth`` stays the accountable human."""
    meta, sender = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    mentions = await _validate_mentions(room_id, request.mentions)
    members = await messaging_repo.list_members(room_id)
    session_ids = list(dict.fromkeys(request.session_mentions))
    session_target_labels: dict[str, str] = {}
    if session_ids:
        # A session chip is a reference, like a document link (owner ruling 2026-10-02): it never
        # wakes the session. It may only name a session this room's humans can all see.
        from core.services import local_sessions, notification_copy

        if await messaging_repo.has_foreign_members(room_id, owner_id):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local sessions cannot be referenced here.")
        # A replay may outlive the session's sharing grant; its message already committed.
        prior = await messaging_repo._get(
            messaging_repo.room_pk(room_id),
            messaging_repo.human_idemp_sk(auth.user_id, str(request.client_message_id)),
        )
        if prior is None:
            for session_id in session_ids:
                row, _ = await local_sessions.authorize_session(auth, None, session_id)
                if row.get("unshared") or row.get("kind") == "background":
                    raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
                owner_member = next(
                    (m for m in members if m.get("kind") == "human" and m.get("id") == row["humanUserId"]),
                    None,
                )
                if owner_member is None:
                    raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "The session owner is not in this room.")
                session_target_labels[session_id] = " · ".join(
                    (
                        "Claude Code" if row["provider"] == "claude" else "Codex",
                        notification_copy.line(
                            row.get("displayTitle") or row.get("title") or row.get("cwdName") or "Session", 80
                        ),
                        notification_copy.line(owner_member.get("displayName") or "Workspace member", 40),
                    )
                )
    links = await _validate_message_links(
        room_id,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    author = author_override or (
        {
            "kind": "system",
            "id": native_source_id,
            "displayName": "Codex session" if native_provider == "codex" else "Claude Code session",
        }
        if native_source_id
        else {"kind": "human", "id": auth.user_id, "displayName": await human_display_name(auth.user_id)}
    )
    addressing = None
    if meta.get("kind") != "dm":
        # Who the text speaks to (routing v2 §3.1). Chat never holds: an unfinished message routes
        # anyway, and its continuation reaches the run as a steer.
        addressing = await address(
            {"author": author, "text": request.text, "mentions": mentions},
            members,
            history=lambda: messaging_repo.list_messages_before(room_id, int(meta.get("lastSeq") or 0) + 1, 6),
        )
    targets = select_dispatch_targets(
        members,
        mentions + [{"kind": "agent", "id": a} for a in (addressing or {}).get("agents", [])],
        None,
        room_kind=meta.get("kind"),
    )
    if native_source_id:
        # A native agent's deliberate post does not wake every ambient listener.
        targets = [target for target in targets if target["eventKind"] == "user_request"]
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
    hash_parts = [
        request.text,
        mentions,
        [str(value) for value in request.attachment_ids],
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
        *([via] if via else []),
    ]
    req_hash = messaging_repo.request_hash(
        *hash_parts, *([session_ids] if session_ids else []), *([native_source_id] if native_source_id else [])
    )
    idemp_sk = (
        messaging_repo.agent_idemp_sk(f"native:{native_source_id}:{request.client_message_id}")
        if native_source_id
        else messaging_repo.human_idemp_sk(auth.user_id, str(request.client_message_id))
    )
    try:
        item, replayed = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=idemp_sk,
            req_hash=req_hash,
            author=author,
            text=request.text,
            mentions=mentions,
            reply_to_message_id=str(request.reply_to_message_id) if request.reply_to_message_id else None,
            thread_root_message_id=str(request.thread_root_message_id) if request.thread_root_message_id else None,
            run_id=None,
            dispatch_targets=targets,
            source_agent_id=None,
            client_message_id=str(request.client_message_id),
            via=via,
            session_mentions=session_ids,
            session_target_labels=session_target_labels,
            attachments=attachments,
            thread_root_seq=_root_seq(links, request.thread_root_message_id),
            acting_human_id=auth.user_id if native_source_id or author_override else None,
            expected_room_revision=int(meta["revision"]) if native_source_id else None,
            agent_hop=native_hop if native_source_id else None,
            extra_conditions=native_conditions if native_source_id else None,
            addressing=_stored_addressing(addressing),
        )
    except messaging_repo.RoomArchived as exc:
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE) from exc
    except messaging_repo.RoomNotFound as exc:  # deleted between require_room_member and the commit
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    except messaging_repo.MessageForbidden as exc:
        # Lost the race: removed between require_room_member and the transaction committing.
        # Same code require_room_member itself raises for "no longer a member".
        raise ApiProblem("ROOM_FORBIDDEN", str(exc)) from exc
    except messaging_repo.MessageConflict as exc:
        raise ApiProblem("MESSAGE_CONFLICT", str(exc)) from exc
    except messaging_repo.ThreadRootMismatch as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.") from exc
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "clientMessageId was already used with a different request.") from exc
    # Telemetry counts MESSAGES, not attempts — a replay wrote nothing, so
    # emitting here would inflate every counter by the client's retry rate.
    # This is also why the dispatch metric is not adjacent to its selector.
    if not replayed:
        agent_mentions = sum(1 for ref in mentions if ref.get("kind") == "agent")
        _emit_dispatch_targets(targets, room_kind, "session" if native_source_id else "human")
        put_metric(
            "messaging.message.created",
            dimensions={
                "author_kind": "session" if native_source_id else "human",
                "room_kind": room_kind,
                "has_targets": "true" if targets else "false",
            },
        )
        if not native_source_id:
            _product_event(
                distinct_id=auth.user_id,
                event="room_message_sent",
                owner_id=owner_id,
                props={
                    "room_kind": room_kind,
                    "mentions_agents": agent_mentions,
                    "mentions_sessions": bool(session_ids),
                    "is_thread_reply": bool(request.reply_to_message_id or request.thread_root_message_id),
                    "has_attachment": bool(attachments),
                    "first_in_room": int(item.get("messageSeq") or 0) == 1,
                    "sender_is_owner": sender.get("role") == "owner",
                },
            )
        # The gap this whole task exists to close: someone addressed an agent
        # and nothing ran. Invisible before — no metric, no log, no event.
        if agent_mentions and not targets and not native_source_id:
            _product_event(
                distinct_id=auth.user_id,
                event="agent_dispatch_missed",
                owner_id=owner_id,
                props={"reason": "no_targets", "room_kind": room_kind},
            )
        if agent_mentions and not native_source_id:
            await _count_jev_miss(room_id, auth.user_id, item)
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


#: The runtime refused a dispatch outright (4xx): permanent, closed instead of retried.
INGRESS_REFUSED = "ingress_refused"
RUN_ERROR_MESSAGES = {
    "agent_did_not_start": "The agent did not start this run.",
    "agent_stopped_responding": "The agent stopped responding.",
    # Only a genuine outage reads this: a cap or credit block is re-worded below.
    "model_unavailable": "The AI model didn't answer, usually a brief provider hiccup.",
    PROVIDER_QUOTA_EXHAUSTED: "Your AI provider's usage limit has been reached.",
    INGRESS_REFUSED: "The agent's runtime couldn't accept this message.",
}
# `gate_error` is deliberately absent: it is the ambient "should an `active` agent
# answer this plain message?" classifier failing, not a run the user asked for. A
# note there would repeat on every plain message from every listening agent while
# the classifier is down.
RUN_FAILURE_OUTCOMES = frozenset({"error"})
#: FOLLOWUP reason for a follow-up steered into a run that failed before answering it.
STEER_LOST = "steer_lost"


async def redispatch_lost_steers(owner_id: str, row: dict) -> None:
    """A run that errored took follow-ups (steers) it may never have answered: give each its
    own normal turn, so a steered message is never silently swallowed. At worst the agent
    answers it twice. A room event steered in before Jev (routing v2 §4.1) goes back through
    Jev as a room event, never as an answer forced from this agent. An aborted run is
    deliberately excluded: Stop means stop."""
    ambient = set(row.get("ambientSteerIds") or ())
    for message_id in sorted(row.get("steeredMessageIds") or ()):
        try:
            created = await messaging_repo.create_follow_up(
                owner_id=owner_id,
                room_id=row["roomId"],
                agent_id=row["agentId"],
                follow_up_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-steer-lost:{row['runId']}:{message_id}")),
                trigger_message_id=message_id,
                event_kind="room_event" if message_id in ambient else "user_request",
                reason=STEER_LOST,
                text="",
            )
            put_metric("messaging.run.steer_redispatch", dimensions={"outcome": "queued" if created else "replayed"})
        except Exception:
            put_metric("messaging.run.steer_redispatch", dimensions={"outcome": "error"})
            logger.exception("lost steer redispatch failed")


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


def provider_display_name(account: dict | None) -> str:
    """The model provider the owner chose, as a person would name it."""
    account = account or {}
    choice = account.get("provider_choice")
    if choice == "chatgpt_oauth":
        return "ChatGPT"
    if choice == "byo_key":
        return QUOTA_PROVIDER_NAMES.get(account.get("byo_provider") or "", "Your AI provider")
    return "The AI model"


async def post_run_failure_message(
    owner_id: str, row: dict, outcome: str, error_kind: str | None, error_detail: dict | None = None
) -> None:
    """Leave one durable note in the run's room (and the trigger's thread): agent-authored
    for a real run failure, a ``system`` notice when the owner is out of budget.

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
        try:
            roster = {agent["id"]: agent for agent in await load_agent_roster(owner_id)}
        except Exception:  # a roster outage must not swallow the only visible sign of the failure
            logger.warning("run failure note: roster load failed owner=%s", owner_id, exc_info=True)
            roster = {}
        agent_name = (roster.get(agent_id) or {}).get("name") or "Your agent"
        # No internal codes in the room: the kind picks the sentence, it is never shown.
        reason = RUN_ERROR_MESSAGES.get(error_kind or outcome) or "The run failed."
        text = f"{agent_name} couldn't reply. {reason} Try again, or check Settings → Model & Credits if this keeps happening."
        # A capped/expired trial owner, or a paid owner out of credits, has their
        # run fail at the gate; say that instead of a generic model error (the
        # bridge reports every failed turn as model_unavailable). That is the
        # platform talking, not the agent, so it is posted as a system notice.
        from core.services.chat_gate import BUDGET_BLOCK_MESSAGES, budget_block_reason

        budget_reason: str | None = None
        # The bridge refused ingress before the agent saw the message; this
        # note must not count as an agent reply or a read receipt.
        system_notice = error_kind == INGRESS_REFUSED
        if error_kind in (None, "model_unavailable"):  # the sweeper's own kinds are real infra failures
            account = await billing_repo.get_by_owner_id(owner_id)
            budget_reason = await budget_block_reason(owner_id, account)
            if error_kind == "model_unavailable":
                # The bridge sends this only after Hermes has retried and refreshed the
                # credential, so the provider itself refused or failed: say so, by name.
                # Not retried for the user -- a resend is their call.
                system_notice = True
                text = f"{agent_name} couldn't reply: {provider_display_name(account)} is having problems right now. Resend in a bit."
        system_notice = system_notice or bool(budget_reason)
        if error_kind == PROVIDER_QUOTA_EXHAUSTED:
            # The workspace's own provider plan ran out: the platform talking, not the agent.
            system_notice = True
            text = (
                f"{provider_quota_message(error_detail)} "
                f"[Model & Credits]({settings.FRONTEND_URL.rstrip('/')}/settings?panel=providers)"
            )
            if error_detail and error_detail.get("resets_at"):
                try:
                    await billing_repo.set_provider_quota_until(owner_id, error_detail["resets_at"])
                except Exception:
                    logger.warning("provider quota stamp failed owner=%s", owner_id, exc_info=True)
        if budget_reason:
            text = BUDGET_BLOCK_MESSAGES[budget_reason]
            link = BUDGET_BLOCK_LINKS.get(budget_reason)
            if link:
                path, link_label = link
                text = f"{text} [{link_label}]({settings.FRONTEND_URL.rstrip('/')}{path})"
        trigger = (
            await messaging_repo.get_message_by_id(room_id, row["triggerMessageId"])
            if row.get("triggerMessageId")
            else None
        )
        thread_root = (trigger or {}).get("threadRootMessageId")
        if system_notice:
            author = {"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"}
        else:
            author = {"kind": "agent", "id": agent_id, "displayName": agent_name}
        await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(f"run-failed:{run_id}"),
            req_hash=messaging_repo.request_hash(room_id, agent_id, text, None, thread_root),
            author=author,
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=thread_root,
            run_id=run_id,
            dispatch_targets=[],
            source_agent_id=agent_id,
            client_message_id=None,
            # The trigger is a reply in the same thread, so it already names the root's seq.
            thread_root_seq=(trigger or {}).get("threadRootSeq") if thread_root else None,
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


async def announce_run(row: dict, state: Literal["working", "idle"]) -> None:
    """Tell every member's conversation list that this agent started or stopped working here.
    Best-effort: never raises. The run callbacks carry no activity text, so it names the agent only.

    "working" expires with the ledger's heartbeat timeout and each heartbeat refreshes it, so an
    "idle" that is lost or overtaken by a racing heartbeat's "working" heals itself."""
    from core.services import live_events

    try:
        member = await messaging_repo.get_member(row["roomId"], "agent", row["agentId"])
        name = (member or {}).get("displayName") or "Agent"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=messaging_repo.RUN_HEARTBEAT_TIMEOUT_S)
            if state == "working"
            else None
        )
        await live_events.publish_room_activity(
            row["roomId"], {"kind": "agent", "id": row["agentId"], "displayName": name}, state, expires_at
        )
    except Exception:
        logger.warning("run activity hint failed", exc_info=True)


async def end_run_and_announce(owner_id: str, run_id: str, **kwargs: Any) -> dict | None:
    """``messaging_repo.end_run`` for a run that may have started: the one door that also
    clears "working…". A run that never started (``not_admitted``) closes on the bare repo call."""
    row = await messaging_repo.end_run(owner_id, run_id, **kwargs)
    if row is not None:
        await announce_run(row, "idle")
    return row


async def close_aborted_run(owner_id: str, run_id: str, *, forced: bool = False) -> None:
    """End a stopped run in the ledger and on the room's live channel, for a run the runtime
    no longer has (``forced=False``) or one that did not report run.ended(aborted) within the
    Stop grace (``forced=True``: the agent may still finish its current step, and the client
    says so). No-op once the run is closed -- normally by the bridge's own run.ended."""
    row = await end_run_and_announce(owner_id, run_id, outcome="aborted")
    if row is None:
        return  # already closed: its run_ended has been published
    put_metric(
        "messaging.run.ended", dimensions={"outcome": "aborted", "event_kind": row.get("eventKind") or "unknown"}
    )
    if forced:
        put_metric("messaging.run.abort_forced", dimensions={"event_kind": row.get("eventKind") or "unknown"})
    stamp = {"runId": row["runId"], "agentId": row["agentId"]}
    frame = {"type": "run_ended", "outcome": "aborted", **stamp, **({"forced": True} if forced else {})}
    try:
        await get_live_event_publisher().publish_room(
            row["roomId"],
            make_live_event(
                "room.run.frame",
                {"roomId": row["roomId"], **stamp, "frame": frame},
                room_id=row["roomId"],
            ),
        )
    except Exception:
        # The ledger is closed; a client that missed the frame reconciles through GET /runs/{id}.
        logger.exception("aborted run frame failed")


def _ms_between(start: Any, end: Any) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


async def handle_run_event(owner_id: str, event: RunEventRequest) -> RunEventResponse:
    """Apply one at-least-once callback from the container's run supervisor (spec §4/§5)."""
    run_id = str(event.run_id)
    if event.type == "run.started":
        # Announce the row this transition wrote, never a re-read: run.ended may have closed it since.
        # Each heartbeat re-announces, so a client that reconnected mid-run recovers "working" in ~30 s.
        started = await messaging_repo.start_run(owner_id, run_id, session_key=event.session_key or "")
        applied = started is not None
        if started is not None:
            await announce_run(started, "working")
            # "Read" for an agent is its run starting on the message (owner ruling).
            if started.get("triggerMessageId"):
                await _publish_room_event(
                    started["roomId"],
                    make_live_event(
                        "room.read.changed",
                        {
                            "roomId": started["roomId"],
                            "reader": {"kind": "agent", "id": started["agentId"]},
                            "messageId": started["triggerMessageId"],
                        },
                        room_id=started["roomId"],
                    ),
                )
    elif event.type == "run.heartbeat":
        started = await messaging_repo.heartbeat_run(owner_id, run_id)
        applied = started is not None
        if started is not None:
            await announce_run(started, "working")
    else:
        usage = event.usage.model_dump(mode="json", by_alias=True) if event.usage else None
        row = await end_run_and_announce(
            owner_id, run_id, outcome=event.outcome, error_kind=event.error_kind, usage=usage
        )
        applied = row is not None
        if row is not None:
            event_kind = row.get("eventKind") or "unknown"
            put_metric("messaging.run.ended", dimensions={"outcome": event.outcome, "event_kind": event_kind})
            if event.silent and event.outcome == "completed":
                put_metric("messaging.agent.silent", dimensions={"event_kind": event_kind})
                if row.get("steeredMessageIds"):
                    # Routing v2 §4.1: a run that stood down after a human line was steered in.
                    put_metric("messaging.run.silent_after_steer", dimensions={"event_kind": event_kind})
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
                await post_run_failure_message(owner_id, row, event.outcome, event.error_kind, event.error_detail)
                await redispatch_lost_steers(owner_id, row)
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
    if parse_lightbulb_session_key(body.session_key) is not None and body.family != "voice":
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
    # A completed reply is pushed by the projector on room.message.created, and so is the
    # failure note (post_run_failure_message) wherever it notifies the trigger's author: a
    # DM or group chat, or the thread they wrote in. Only a top-level channel trigger's
    # author hears nothing from the note, so only they get this push -- one notice each.
    if outcome == "error" and member_user_id and member_user_id.startswith("user_"):
        try:
            from core.services import notification_copy as copy

            room = await messaging_repo.get_room(row["roomId"]) or {}
            if room.get("kind") != "channel" or trigger.get("threadRootMessageId"):
                return
            roster = {agent["id"]: agent for agent in await load_agent_roster(owner_id)}
            name = (roster.get(row["agentId"]) or {}).get("name") or "Agent"
            reason = RUN_ERROR_MESSAGES.get(row.get("errorKind") or "") or "the run hit an error"
            await send_push_to_user(
                member_user_id,
                copy.title(name, copy.room_where(room.get("kind"), room.get("name"))),
                copy.line(f"Couldn't finish: {reason[0].lower()}{reason[1:].rstrip('.')}"),
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
    if meta is None or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
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
    if meta is None or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    bound = request.run_id or request.reply_to_message_id or request.thread_root_message_id
    if meta.get("hidden") and not (bound or request.uploaded_attachments):
        # A Google Meet's room is read by nobody: what an agent posts there (a late Live answer)
        # goes to the meeting's output room when it sits there. Every door resolves the same room,
        # so a replay hashes the same and stays idempotent. A post bound to the hidden room (a run,
        # a reply, a thread, a runtime-uploaded attachment) stays there: those are checked against it.
        from core.services.meeting_invites import _post_line, output_room

        target = await output_room(owner_id, room_id)
        if target != room_id and await messaging_repo.get_member(target, "agent", agent_id):
            room_id, meta = target, await messaging_repo.get_room(target)
        elif target != room_id and await messaging_repo.get_member(room_id, "agent", agent_id):
            # Not seated in the output room: the answer goes there as a system line naming the agent,
            # keyed by the event id so a replay returns the same line. A gone room falls through.
            agent = next((a for a in await load_agent_roster(owner_id) if a["id"] == agent_id), None)
            title = (meta.get("meeting") or {}).get("title") or "Meeting"
            line = agent and await _post_line(
                owner_id,
                room_id,
                f"meeting-answer:{request.event_id}",
                f"**{agent['name']}**, after **{title}**:\n\n{request.text}",
            )
            if line:
                return AgentMessageReceiptResponse(
                    message_id=line["messageId"], room_id=line["roomId"], created_at=line["createdAt"]
                )
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
    # Agent-reply cap: a human message is hop 0, a room turn's reply is its trigger's hop + 1,
    # and a post from outside a room turn (heartbeat, routine, lightbulb-post.sh) is hop 1.
    hop, trigger, run = 1, None, None
    if run_id is not None:
        # A present run id is a claim about the ledger, not a hint: it must be this
        # owner's run for this room and agent. A run that already closed still accepts
        # its late reply (durable retries, sweeper closures) — delivery is at-least-once.
        run = await messaging_repo.get_run(owner_id, run_id)
        if run is None or run.get("roomId") != room_id or run.get("agentId") != agent_id:
            raise ApiProblem("RUN_NOT_FOUND", "runId does not name this owner's run for this room and agent.")
        if run.get("triggerMessageId"):
            trigger = await messaging_repo.get_message_by_id(room_id, run["triggerMessageId"])
            hop = int((trigger or {}).get("agentHop") or 0) + 1
    # At the cap this message wakes nobody -- @mentions included, or a mention chain walks
    # straight past it. The next human message starts a fresh chain at hop 0.
    capped = targets if hop >= AGENT_REPLY_CAP else []
    if capped:
        targets = []
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
    for uploaded in request.uploaded_attachments:
        try:
            adopted = await run_in_thread(
                adopt_uploaded_attachment,
                owner_id,
                room_id,
                str(uploaded.attachment_id),
                uploaded.file_name,
                uploaded.content_type,
            )
        except ValueError as exc:
            raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
        attachments.append(adopted.model_dump(mode="json", by_alias=True))
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
            thread_root_seq=_root_seq(links, request.thread_root_message_id),
            agent_hop=hop,
            in_reply_to=(trigger or {}).get("messageId"),
        )
    except messaging_repo.RoomArchived as exc:
        # Deliberate: a reply to a turn that began before the archive is refused too, so an
        # archived channel's history ends where the archive did. The bridge drops a 4xx.
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE) from exc
    except messaging_repo.RoomNotFound as exc:  # deleted after the pre-check; the bridge drops a 4xx
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    except messaging_repo.MessageForbidden as exc:
        # Lost the race: removed between the get_member pre-check above and the
        # transaction committing. Same code that pre-check itself raises.
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not a current room member.") from exc
    except messaging_repo.ThreadRootMismatch as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.") from exc
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
        if trigger and (run or {}).get("eventKind") == "room_event":
            await _emit_reply_after_human_answer(room_id, meta, trigger, item, room_kind)
        if capped:
            put_metric("messaging.agent_chain.capped", dimensions={"room_kind": room_kind})
            await _post_chain_paused_notice(owner_id, item, agent["name"], (trigger or {}).get("author"))
    return AgentMessageReceiptResponse(message_id=item["messageId"], room_id=room_id, created_at=item["createdAt"])


async def _post_chain_paused_notice(owner_id: str, item: dict, agent_name: str, trigger_author: dict | None) -> None:
    """One muted system line where a capped agent message landed, so the silence after it
    does not look like a bug. Keyed on that message: a retried reply cannot double it."""
    names = [agent_name]
    if trigger_author and trigger_author.get("kind") == "agent":
        names.insert(0, trigger_author.get("displayName"))
    names = [n for i, n in enumerate(names) if n and n not in names[:i]]
    who = " and ".join(names) if names else "Agents"
    text = f"{who} paused after {AGENT_REPLY_CAP} replies. Reply to keep them going."
    root = item.get("threadRootMessageId")
    try:
        await messaging_repo.create_message(
            room_id=item["roomId"],
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(f"chain-paused:{item['messageId']}"),
            req_hash=messaging_repo.request_hash(item["roomId"], text, root),
            author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=root,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
            thread_root_seq=item.get("threadRootSeq") if root else None,
            # The capped reply right before it already notified; this line must not again.
            silent=True,
        )
    except Exception:
        # Advisory: the capped message itself is already stored.
        logger.warning("chain-paused notice failed", exc_info=True)


async def _emit_reply_after_human_answer(room_id: str, meta: dict, trigger: dict, item: dict, room_kind: str) -> None:
    """Routing v2 R1's residual: an ambient reply posted although a human spoke in the same
    conversation after its trigger (the steer missed it, or the run ignored it). Never raises,
    for the same reason as _emit_reply_telemetry."""
    try:
        conversation = {trigger.get("threadRootMessageId"), trigger.get("threadRootMessageId") or trigger["messageId"]}
        rows = await messaging_repo.list_messages_between(
            room_id, int(trigger["messageSeq"]) + 1, int(item["messageSeq"]) - 1
        )
        if any(
            (row.get("author") or {}).get("kind") == "human"
            and (meta.get("kind") == "dm" or row.get("threadRootMessageId") in conversation)
            for row in rows
        ):
            put_metric("messaging.reply.after_human_answer", dimensions={"room_kind": room_kind})
    except Exception:
        logger.warning("messaging: after-human-answer check failed for room %s", room_id, exc_info=True)


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
        asked_by: str | None = None
        if matched is not None:
            row, how = matched
            asked_by = (row.get("author") or {}).get("id")
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
                distinct_id=asked_by,
                event="agent_reply_delivered",
                owner_id=owner_id,
                props=props,
            )
        if latency_ms is None:
            put_metric("messaging.reply.unmatched")
        # The once-ever trial activation signal, on the person who asked (the
        # same key agent_reply_delivered uses) so it sits on their timeline.
        # Only when a human was answered: a proactive or agent-to-agent post
        # would otherwise win the billing CAS on an org-shaped person and the
        # real first reply to a human could never fire it.
        if asked_by:
            schedule_trial_first_agent_response(owner_id, distinct_id=asked_by)
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
    await _publish_human_read(room_id, auth.user_id, value)
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
    # mark_unread can LOWER the cursor, so a sender's "Read" may fall back; accepted.
    # ponytail: keep a monotonic readHighSeq beside lastReadSeq if anyone notices.
    await _publish_human_read(room_id, auth.user_id, value)
    return ReadCursorResponse(room_id=room_id, last_read_seq=value)


async def _publish_human_read(room_id: str, user_id: str, value: int) -> None:
    await _publish_room_event(
        room_id,
        make_live_event(
            "room.read.changed",
            {"roomId": room_id, "reader": {"kind": "human", "id": user_id}, "lastReadSeq": value},
            room_id=room_id,
        ),
    )


async def _publish_room_event(room_id: str, event: dict) -> None:
    """Best-effort receipt publish on the room channel: never raises, clients
    reconcile on the next detail read."""
    from core.services.live_events import get_live_event_publisher

    try:
        await get_live_event_publisher().publish_room(room_id, event)
    except Exception:
        logger.warning("room receipt event failed", exc_info=True)


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


class AgentPostRequest(ContractModel):
    """An agent posting OUTSIDE a room turn (a Paperclip run, heartbeat or routine).

    Target, first that applies: ``roomId`` → that room (membership checked, never
    auto-joined); ``roomName`` → the one channel of that name the agent is in;
    ``userId``/``handle`` → that workspace member's DM with the agent;
    ``issueId`` → the issue creator's DM; otherwise the workspace's #general."""

    event_id: str
    agent_id: uuid.UUID
    text: str
    room_id: uuid.UUID | None = None
    room_name: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    handle: str | None = Field(default=None, min_length=1, max_length=64)
    issue_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9-]+$")

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return validate_event_id(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_message_text(value)

    @model_validator(mode="after")
    def one_target(self) -> "AgentPostRequest":
        if sum(x is not None for x in (self.room_id, self.room_name, self.user_id, self.handle)) > 1:
            raise ValueError("name at most one of roomId, roomName, userId, handle")
        return self


async def _agent_dm_room_id(owner_id: str, user_id: str, agent_id: str) -> str:
    """The human↔agent DM, created through the same ``create_room`` the web client's
    ``selectDm`` calls (agent ``active``) when none exists. The client room id is derived
    from the pair, so two racing posts converge on one room instead of two DMs."""
    for row in await messaging_repo.list_rooms_for_member(owner_id, "human", user_id):
        meta = row["meta"]
        if (
            meta.get("kind") == "dm"
            and not meta.get("deletedAt")
            and await messaging_repo.get_member(meta["roomId"], "human", user_id) is not None
            and await messaging_repo.get_member(meta["roomId"], "agent", agent_id) is not None
        ):
            return meta["roomId"]
    detail = await create_room(
        AuthContext(user_id=user_id, org_id=owner_id),
        CreateRoomRequest(
            client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-agent-dm|{owner_id}|{user_id}|{agent_id}"),
            name=None,
            kind="dm",
            members=[CreateRoomMember(kind="agent", id=agent_id, role="member", listen_mode="active")],
        ),
    )
    return str(detail.room_id)


async def _user_for_handle(owner_id: str, handle: str) -> str:
    """A workspace member by messaging handle, derived exactly as rooms derive it.

    ponytail: handles are per-room derived (see ``with_human_handles``), so this uses the
    workspace-wide derivation; two humans whose handles collide only in some room still
    resolve here. Pass ``userId`` when a handle is ambiguous."""
    from core.services.clerk_admin import list_organization_members

    people = await list_organization_members(owner_id)
    if not people:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not read workspace members.", retryable=True, retry_after=3)
    agents = [
        {"kind": "agent", "mentionHandle": h} for h in mention_handles(await load_agent_roster(owner_id)).values()
    ]
    humans = [
        {
            "kind": "human",
            "id": p["user_id"],
            "displayName": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or p["user_id"],
        }
        for p in people
    ]
    emails = {p["user_id"]: p["email"] for p in people if p.get("email")}
    wanted = handle.removeprefix("@").lower()
    for row in with_human_handles(agents + humans, emails):
        if row["kind"] == "human" and row["mentionHandle"] == wanted:
            return row["id"]
    raise ApiProblem("MEMBER_NOT_FOUND", "No workspace member has that handle.")


async def _issue_creator(owner_id: str, issue_id: str) -> str | None:
    """The Clerk user who created a Paperclip issue in this owner's company, or None when an
    agent created it or the creator is no longer a member. Paperclip→Clerk goes through the
    same workspace-member identity rows ``activity._people`` maps with."""
    import httpx

    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
    from core.services.paperclip_admin_session import get_admin_session_cookie

    repo = PaperclipRepo(table_name="paperclip-companies")
    company = await repo.resolve_owner_row(owner_id)
    if company is None:
        raise ApiProblem("ROOM_FORBIDDEN", "This workspace has no Paperclip company.")
    admin = shared_admin_client()
    try:
        issue = await admin.get_issue(session_cookie=await get_admin_session_cookie(admin._http), issue_id=issue_id)
    except PaperclipApiError as exc:
        if exc.status_code in (403, 404):
            raise ApiProblem("ROOM_FORBIDDEN", "That issue is not in this workspace.") from exc
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not read the issue.", retryable=True, retry_after=3) from exc
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not read the issue.", retryable=True, retry_after=3) from exc
    if (issue or {}).get("companyId") != company.company_id:
        raise ApiProblem("ROOM_FORBIDDEN", "That issue is not in this workspace.")
    creator = issue.get("createdByUserId")
    if not creator:
        return None
    workspace = await repo.get_workspace(owner_id)
    for uid in list(getattr(workspace, "member_user_ids", None) or []):
        identity = await repo.get_identity(uid)
        if identity is not None and identity.paperclip_user_id == creator:
            try:
                await _validate_human_in_owner(owner_id, uid)
            except ApiProblem:
                return None
            return uid
    return None


async def resolve_agent_post_room(owner_id: str, request: AgentPostRequest) -> str:
    agent_id = str(request.agent_id)
    if request.room_id is not None:
        return str(request.room_id)
    if request.room_name is not None:
        # Routine descriptions name a channel ("Post results to: #sales"); only channels the
        # agent is already in qualify -- a post never joins a room.
        wanted = request.room_name.lstrip("#").strip().casefold()
        matches = [
            r["meta"]["roomId"]
            for r in await messaging_repo.list_rooms_for_member(owner_id, "agent", agent_id)
            if r["meta"].get("kind") == "channel"
            and not r["meta"].get("deletedAt")
            and (r["meta"].get("name") or "").lstrip("#").strip().casefold() == wanted
        ]
        if len(matches) != 1:
            raise ApiProblem(
                "ACTION_UNAVAILABLE",
                f"You are in {len(matches)} channels named #{wanted}; pass --room <roomId> instead.",
            )
        return matches[0]
    user_id = request.user_id
    if user_id is not None:
        # _validate_human_in_owner waves the owner id itself through; an org id is never a human.
        if not user_id.startswith("user_") or user_id == owner_id:
            raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")
        await _validate_human_in_owner(owner_id, user_id)
    elif request.handle is not None:
        user_id = await _user_for_handle(owner_id, request.handle)
    elif request.issue_id is not None:
        user_id = await _issue_creator(owner_id, request.issue_id)
    if user_id is not None:
        return await _agent_dm_room_id(owner_id, user_id, agent_id)
    room_id = default_room_id(owner_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("deletedAt"):
        raise ApiProblem("ACTION_UNAVAILABLE", "This workspace has no #general; name a room, user or issue.")
    return room_id


async def post_agent_message(owner_id: str, request: AgentPostRequest) -> AgentMessageReceiptResponse:
    """Resolve the target, then write through THE canonical agent writer."""
    room_id = await resolve_agent_post_room(owner_id, request)
    return await create_agent_message(
        owner_id,
        AgentMessageRequest(
            event_id=request.event_id,
            room_id=room_id,
            agent_id=request.agent_id,
            text=request.text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )
