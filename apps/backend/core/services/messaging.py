"""Canonical multiplayer room policy and public contract models.

DynamoDB owns the ordered room log; OpenClaw owns each agent's native session.
This module is the single policy seam between authenticated humans/internal
agents and :mod:`core.repositories.messaging_repo`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import unicodedata
import uuid
from urllib.parse import quote
from pathlib import PurePath
from datetime import datetime, timedelta, timezone
from collections.abc import Collection
from typing import Any, Literal

from botocore.exceptions import ClientError
from pydantic import (
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.auth import is_org_owner_id
from core.config import settings
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event, schedule_trial_first_agent_response
from core.dynamodb import run_in_thread
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services.person_label import email_label, is_relay_email
from core.services.push_service import send_push_to_user
from core.services import clerk_admin, hermes_profiles, livekit_huddles, run_analytics
from core.services.session_keys import parse_lightbulb_session_key, parse_session_key
from core.services.workspace_provisioning import STARTER_ROLE_KEYS as _STARTER_KEYS
from core.services.usage_service import record_usage

# Where a message came from when not the chat surface itself; carried on the stored message.
# voice: said in a Live talk; meeting_notes: a meeting's action items, filed from its notes
MessageVia = Literal["lightbulb_mcp", "ideas", "front_desk", "text", "apps", "voice", "meeting_notes", "meeting_chat"]

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
    "text/html": TEXT_MAX_BYTES,
    **{t: VIDEO_MAX_BYTES for t in VIDEO_CONTENT_TYPES},
}
# The largest file that travels THROUGH the API (upload route, inline agent attachment). Video
# is excluded on purpose: its cap only applies to the S3-direct path.
ATTACHMENT_MAX_BYTES = max(v for t, v in ATTACHMENT_MAX_BYTES_BY_TYPE.items() if t not in VIDEO_CONTENT_TYPES)
# Base64 is exactly 4 characters per 3 bytes: the ENCODED ceiling for that largest cap.
ATTACHMENT_MAX_BASE64_CHARS = -(-ATTACHMENT_MAX_BYTES // 3) * 4
ATTACHMENT_MAX_COUNT = 4
ROOM_NAME_MAX_CHARS = 200
ROOM_TOPIC_MAX_CHARS = 2000  # a project's Instructions (UX slice 6)
# A project's tile (UX slice 6): one of 12 lucide glyphs and one of 6 colour tokens (--project-1..6).
ProjectIcon = Literal[
    "sparkles", "rocket", "shopping-bag", "megaphone", "code", "pen-line",
    "chart-line", "users", "calendar", "package", "leaf", "briefcase",
]  # fmt: skip
ProjectColor = Literal["project-1", "project-2", "project-3", "project-4", "project-5", "project-6"]
PAUSED_ROOM_TEXT = "Lightbulb is paused here. Resume it to get a reply."
MAX_PINNED_MESSAGES = messaging_repo.MAX_PINNED_MESSAGES
MAX_SECTIONS = 20
SECTION_NAME_MAX_CHARS = 40
EVENT_ID_MAX_BYTES = 256
DEFAULT_ROOM_NAME = "general"
# The workspace feed (Activity, spec 2026-10-14 §4.2): one hidden channel whose topic is the
# team's brief. Identified by id (``is_feed_room``), never by this name.
FEED_ROOM_NAME = "feed"
FEED_DEFAULT_TOPIC = "Keep us posted on customers, money and what the team finished. Skip engineering details."
FEED_POST_REASON_MAX_CHARS = 280
# Only people sit in the feed; agents are its AUTHORS (roster-checked, never seated), so the room's
# 32-seat cap is the workspace's people alone. See ``room_agent``.
#: The CEO's one welcome post in the feed (idempotent, like #general's).
FEED_WELCOME = (
    "**This is our feed.** When something here matters to you (money in or out, a customer, "
    "a blocker, finished work), one of us posts it here, one thing per post, and only when it is worth your time.\n\n"
    "The brief at the top says what you want to hear about. Edit it any time. "
    "Press Discuss on a post to talk it through with whoever wrote it.\n\n"
    "Home shows your company today: Needs you for what is waiting on you, Ideas for what we could do next "
    "(Do it starts it, Not now puts it away), and this feed."
)
# Seeded ONCE per workspace as a message from the front agent (``ensure_default_room``
# returns early on any room that already has messages): the first thing a new
# user reads. It speaks as Lightbulb, the coordinator; specialists do the tasks (founder, 6 Nov 2026).
#: The three pre-typed replies. The frontend's FirstReplyChips.tsx carries the same strings;
#: the third's prefix is what onboarding_brief.first_message_intake recognises as "you pick".
FIRST_REPLIES: tuple[str, str, str] = (
    "Here's our website: ",
    "I'm starting ",
    "Not sure yet. Pick something useful and show me how you work.",
)

_FIRST_MESSAGE = (
    "Welcome to {workspace}. I'm Lightbulb. I hand work to Growth, Engineering, Finance, Analytics and "
    "Customer Ops, and show you each task.\n\n"
    "One thing gets us started: what's the business? A website or a sentence is plenty.\n\n"
    "→ Here's our website: …\n"
    "→ I'm starting …\n"
    "→ Not sure yet. Pick something useful and show me how you work.\n\n"
    "I'll turn your answer into a plan and hand the first tasks to the specialists. "
    "You'll watch them work here."
)


#: Posted once by the CEO in #general after the first finished run, while the workspace has one
#: person (08 §3, screen 6). Static on purpose: a model-written ask would not fire reliably or be
#: measurable. The link renders as the inline invite card on the web (``RoomAppLinks``).
INVITE_ASK = (
    "Who should see this with you? Add them now and they'll see the plan the moment it's ready, "
    "and can approve it with you."
)
INVITE_ASK_MARKER = "invite-ask"


def invite_ask_text() -> str:
    return f"{INVITE_ASK}\n\n[Invite someone]({settings.FRONTEND_URL.rstrip('/')}/chat?panel=invite)"


#: Super-agent (UX slice 6): the welcome and its first-reply chips sit in the owner's Lightbulb DM, marked by this
#: message kind (usually seq 1; after the owner's own messages when they wrote before provisioning finished).
#: Clients, onboarding intake and the Hermes bridge find the welcome by it. The room's META ``firstRun`` says the
#: first run is still open: set with the DM, cleared by the owner's first answer (``close_first_run``), so a
#: workspace is "fresh" (lands in the DM) only until then.
FIRST_RUN_KIND = "first_run"
#: ...and #general keeps one line for the people who join later (they are seated there, never in the DM).
TEAM_WELCOME_KIND = "team_welcome"
_TEAM_WELCOME = (
    "Welcome to {workspace}. This is general, for the whole team: ask Lightbulb here and everyone sees the work."
)


def _workspace_label(workspace_name: str | None) -> str:
    # A leading @ or # is stripped so the name cannot render as a mention or a room link.
    return " ".join((workspace_name or "").lstrip("@#").split()) or "your workspace"


def default_room_welcome(workspace_name: str | None) -> str:
    """The front agent's first message. Only the workspace name is interpolated."""
    return _FIRST_MESSAGE.format(workspace=_workspace_label(workspace_name))


def team_welcome(workspace_name: str | None) -> str:
    return _TEAM_WELCOME.format(workspace=_workspace_label(workspace_name))


async def close_first_run(room_id: str) -> None:
    """The owner answered: the first run is no longer open, so the next load lands on Home. Best effort."""
    try:
        meta = await messaging_repo.get_room(room_id)
        if meta and meta.get("firstRun"):
            await messaging_repo.patch_room_meta(room_id, {"firstRun": None})
    except Exception:  # noqa: BLE001 -- a stale flag only keeps the DM as the landing
        logger.warning("first run not closed for room %s", room_id, exc_info=True)


ROOM_ARCHIVED_MESSAGE = "This channel is archived. Unarchive it to post."
API_ERROR_STATUS = {
    "LOCAL_SESSION_FORBIDDEN": 403,
    "LOCAL_SESSION_NOT_FOUND": 404,
    "LOCAL_SESSION_CONFLICT": 409,
    "LOCAL_SESSION_UNSUPPORTED": 409,
    "LOCAL_SESSION_OFFLINE": 409,
    "LOCAL_SESSION_QUEUE_FULL": 409,
    "LOCAL_SESSION_LOOP": 429,
    "LOCAL_SESSION_VIEWERS_FULL": 409,
    "LOCAL_SESSION_ASK_PENDING": 429,
    "LOCAL_SESSION_ASK_SETTLED": 409,
    "LOCAL_SESSION_ASK_DECLINED": 409,
    "INVALID_REQUEST": 400,
    "ROOM_FORBIDDEN": 403,
    "WORKSPACE_MEMBERSHIP_REQUIRED": 403,
    "ROOM_NOT_FOUND": 404,
    "ACTIVITY_NOT_FOUND": 404,
    "ACTION_UNAVAILABLE": 409,
    "HOLD_GONE": 409,
    "NETWORK_NOT_CONNECTED": 409,
    "NETWORK_ALREADY_JOINED": 409,
    "NETWORK_NO_ROUTE": 409,
    "NETWORK_OFFLINE": 409,
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
    "ROOM_NOT_PAUSABLE": 409,
    "ROOM_NOT_DELETABLE": 409,
    "RUN_NOT_ACTIVE": 409,
    "SUBAGENT_NOT_FOUND": 404,
    "RATE_LIMITED": 429,
    "RUNTIME_UNAVAILABLE": 503,
    "SEARCH_UNAVAILABLE": 503,
    "TEXT_RELAY_UNAVAILABLE": 503,
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
        "room.subagent.event",
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
        "workspace.access.removed",
        "workspace.session.attention",
        "workspace.session.screen",
        "workspace.session.typing",
        "digest.ready",
    }
)
#: The one workspace-wide sessions channel, ``/ws/<ws>/sessions``.
SESSIONS_LIVE_EVENT_TYPES = frozenset({"workspace.sessions.changed"})
LIVE_EVENT_TYPES = ROOM_LIVE_EVENT_TYPES | USER_LIVE_EVENT_TYPES | SESSIONS_LIVE_EVENT_TYPES


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


class SessionRef(ContractModel):
    session_id: str


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
    # Super-agent: the owner's Lightbulb DM that holds the first-run welcome; a fresh workspace lands here.
    first_run: bool = False
    # Set while the channel is archived: history stays readable, nothing new posts.
    archived_at: datetime | None = None
    archived_by: str | None = None
    icon: ProjectIcon | None = None
    color: ProjectColor | None = None
    # Set while the project is paused: Lightbulb takes no new message and its schedules skip; running tasks finish.
    paused_at: datetime | None = None
    paused_by: str | None = None
    # A session's chat room (hidden from the room list): the session it talks beside.
    session: SessionRef | None = None
    # The workspace feed (Activity): listed, but clients hide it from the conversation list.
    feed: bool = False


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
    topic: str | None = None
    icon: ProjectIcon | None = None
    color: ProjectColor | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_room_name(value)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str | None) -> str | None:
        return validate_room_topic(value)


class PatchRoomRequest(ContractModel):
    name: str | None = None
    topic: str | None = None
    icon: ProjectIcon | None = None
    color: ProjectColor | None = None
    # A PATCH field rather than /archive + /unarchive routes: it rides this route's admin
    # check, revision CAS and metadata live event unchanged.
    archived: bool | None = None
    # Same shape as `archived`; a workspace admin may also set it (audited).
    paused: bool | None = None

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
        return {key: getattr(self, key) for key in ("name", "topic", "icon", "color") if key in self.model_fields_set}


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


class TaskRef(ContractModel):
    """A task card on a Lightbulb message. Writers: Router apply (started), the result post, question
    cards (+ `qid`: the card's buttons bind to {taskId, qid}). Strict on write (`create_agent_message`);
    `_read_task_refs` keeps a stored row readable whatever it holds."""

    task_id: str
    role: Literal["started"]
    qid: str | None = None

    @model_serializer(mode="wrap")
    def _drop_absent_qid(self, handler):
        result = handler(self)
        if self.qid is None:
            result.pop("qid", None)
        return result


_TASK_REF_KEYS = {"taskId", "role", "qid"}


def _read_task_refs(stored: Any) -> list[TaskRef] | None:
    """Stored taskRefs for a read: unknown keys ignored, a ref that still does not validate dropped. One odd
    stored message must never 400 its whole room."""
    if not isinstance(stored, list):
        return None
    refs = []
    for ref in stored:
        try:
            kept = {k: v for k, v in ref.items() if k in _TASK_REF_KEYS}
            if kept.get("role") == "suggested":  # stored before 5 Nov 2026 (proposals removed): an ordinary card
                kept["role"] = "started"
            refs.append(TaskRef.model_validate(kept))
        except (AttributeError, ValueError):
            logger.warning("unreadable stored taskRef dropped: %r", ref)
    return refs


# Super-agent message attrs: field name -> stored/wire name.
_ATTR_FIELDS = {
    "task_refs": "taskRefs",
    "specialist": "specialist",
    "routed_to": "routedTo",
    "forwarded_from": "forwardedFrom",
    "kind": "kind",
    "automation_id": "automationId",
}


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
    via: MessageVia | None = None
    # An agent reply's trigger message (the `to` edge Jev reads on agent lines); absent otherwise.
    in_reply_to: str | None = None
    # A mid-run progress line, not the run's reply (clients keep the run card); absent otherwise.
    progress: bool = False
    # A feed post's "Why I created this post" line (plain text); absent otherwise.
    post_reason: str | None = None
    # Super-agent attrs (contracts §6, G2), only on messages Lightbulb posts for tasks; absent otherwise.
    task_refs: list[TaskRef] | None = None
    specialist: str | None = None
    routed_to: dict[str, str] | None = None
    forwarded_from: dict[str, str] | None = None
    kind: str | None = None
    automation_id: str | None = None  # the automation an `automation_confirm` card turns on (lane A)
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
        if not self.progress:
            result.pop("progress", None)
        if self.post_reason is None:
            result.pop("postReason", None)
            result.pop("post_reason", None)
        for name in _ATTR_FIELDS:
            if getattr(self, name) is None:
                result.pop(name, None)
                result.pop(_ATTR_FIELDS[name], None)
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
        "text/html",
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
    # A mid-run progress line (the bridge's interim commentary): stored and shown like any agent
    # message, but it wakes nobody, notifies nobody and is not the run's reply.
    progress: bool = False
    # A feed post's "Why I created this post" (plain text, <= 280 chars); stored on the message.
    post_reason: str | None = None
    attachments: list[InlineAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)
    uploaded_attachments: list[UploadedAttachment] = Field(default_factory=list, max_length=ATTACHMENT_MAX_COUNT)

    @field_validator("post_reason")
    @classmethod
    def validate_post_reason(cls, value: str | None) -> str | None:
        return validate_post_reason(value)

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


class NotedFact(ContractModel):
    message_id: str
    fact: str
    noted_at: str


class RoomMemoryResponse(ContractModel):
    items: list[NotedFact]


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
FOLLOW_UP_CAP_SINCE_HUMAN = 10
FOLLOW_UP_CAP_NOTICE = (
    "I paused here so my background work can't loop, and a finished result wasn't delivered. "
    "Ask me here to check where it got to."
)


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


class LiveFeatures(ContractModel):
    super_agent: bool


class LiveConfig(ContractModel):
    schema_version: Literal[1] = 1
    http_endpoint: str
    realtime_endpoint: str
    region: str
    user_channel_key: uuid.UUID
    workspace_id: uuid.UUID
    # Always `{superAgent: true}` since the switch retired; old app builds still read it (contracts-v2 §1).
    features: LiveFeatures | None = None


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


def validate_post_reason(value: str | None) -> str | None:
    """Trimmed; blank is None; over ``FEED_POST_REASON_MAX_CHARS`` is refused."""
    trimmed = (value or "").strip()
    if len(trimmed) > FEED_POST_REASON_MAX_CHARS:
        raise ValueError(f"postReason exceeds {FEED_POST_REASON_MAX_CHARS} characters")
    return trimmed or None


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
    if not email or "@" not in email or is_relay_email(email):
        return None  # a relay's local part is random characters: the handle comes from the name
    local = email.split("@", 1)[0].strip()
    return local or None


#: Handles no agent derives from its name while the workspace's super-agent switch is on: `@lightbulb` is the
#: front agent (contracts-v2 §6), set by `_resolved_member` / `task_conversation._front_agent`.
RESERVED_HANDLES = frozenset({"lightbulb"})
#: The front agent's display name while the switch is on (contracts-v2 §9); its handle is `lightbulb`.
FRONT_AGENT_NAME = "Lightbulb"


def mention_handles(agents: list[dict[str, Any]], *, reserved: Collection[str] = ()) -> dict[str, str]:
    """Derive workspace-unique, deterministic handles over canonical ID order. A ``reserved`` candidate is
    suffixed like a collision."""
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
        if len(colliders) == 1 and candidate not in reserved:
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

    # Bounded like the room summaries (see _SUMMARY_CONCURRENCY): the room list now
    # passes every room's members at once, so this can be many people.
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def _lookup(user_id: str) -> dict | None:
        try:
            async with limit:
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
    fields = dict(
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
        progress=item.get("progress") is True,
        post_reason=item.get("postReason"),
        **{name: item.get(stored) for name, stored in _ATTR_FIELDS.items() if name != "task_refs"},
        task_refs=_read_task_refs(item.get("taskRefs")),
        addressed_agent_ids=[
            t["agentId"]
            for t in item.get("dispatchTargets") or []
            if isinstance(t, dict) and t.get("agentId") and t.get("eventKind") == "user_request"
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
    try:
        return CanonicalMessage(**fields)  # every valid stored row: exactly today's output
    except ValidationError as exc:
        return _tolerant_message(fields, exc)


# A stored row that no longer validates (a rolled-back enum value, an old extra key, a long name) is repaired
# on READ only, so one message never fails its room; writes stay strict. These fields cannot be repaired.
_CORE_FIELDS = {"message_id", "room_id", "message_seq", "room_revision", "created_at", "text"}
_READ_FALLBACK = {"mentions": [], "run_id": None, "reply_to_message_id": None, "thread_root_message_id": None}
_REPAIR_LOGGED: dict[Any, None] = {}  # insertion-ordered: the oldest id is evicted past 10k


def _repair_author(value: Any) -> dict:
    a = value if isinstance(value, dict) else {}
    # An unknown kind reads as "agent", never "system" (the most trusted kind; readState never counts it unread).
    kind = a.get("kind") if a.get("kind") in ("human", "agent", "system") else "agent"
    return {
        "kind": kind,
        "id": str(a.get("id") or "unknown")[:256],
        "displayName": str(a.get("displayName") or "Someone")[:256],
    }


def _tolerant_message(fields: dict[str, Any], exc: ValidationError) -> CanonicalMessage:
    """Repair what failed and retry: unknown nested keys are ignored, a bad list element or map entry is
    dropped, any other bad field falls back to its default. One warning per message."""
    fields = copy.deepcopy(fields)
    repaired: set[str] = set()
    for _ in range(5):
        drops: dict[str, set[int]] = {}
        for err in exc.errors():
            loc, name = err["loc"], err["loc"][0]
            if name in _CORE_FIELDS:
                raise exc
            repaired.add(str(name))
            value = fields.get(name)
            if name == "author":
                fields["author"] = _repair_author(value)
            elif err["type"] == "extra_forbidden" and len(loc) == 3 and isinstance(value, list):
                value[loc[1]].pop(loc[2], None)  # an unknown key on a list element: ignored
            elif len(loc) >= 2 and isinstance(loc[1], int) and isinstance(value, list):
                drops.setdefault(name, set()).add(loc[1])
            elif len(loc) >= 2 and isinstance(value, dict):
                value.pop(loc[1], None)
            elif err["type"] == "too_long" and isinstance(value, list):
                fields[name] = value[:ATTACHMENT_MAX_COUNT]
            elif name == "deleted_at":
                # A stored deletion is never shown live: the text is already blanked (`deleted`); the marker
                # becomes the message's own createdAt, a valid time clients read as "deleted".
                fields[name] = fields["created_at"]
            else:
                field = CanonicalMessage.model_fields[name]
                fields[name] = (
                    _READ_FALLBACK[name] if name in _READ_FALLBACK else field.get_default(call_default_factory=True)
                )
        for name, idx in drops.items():
            fields[name] = [v for i, v in enumerate(fields[name]) if i not in idx]
        try:
            message = CanonicalMessage(**fields)
        except ValidationError as again:
            exc = again
            continue
        if fields["message_id"] not in _REPAIR_LOGGED:  # once per message per process
            _REPAIR_LOGGED[fields["message_id"]] = None
            if len(_REPAIR_LOGGED) > 10_000:
                _REPAIR_LOGGED.pop(next(iter(_REPAIR_LOGGED)))
            logger.warning("stored message %s read with repaired fields %s", fields["message_id"], sorted(repaired))
        return message
    raise exc


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
    "text/html": (None, ".html"),
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
            "attachment must be PNG, JPEG, WebP, GIF, PDF, ZIP, XLSX, DOCX, PPTX, CSV, TXT, MD, HTML, MP4, WebM, or MOV, "
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
    ``attachments/<room>/<id><suffix>``. Size comes from a HEAD and a signed type from a ranged GET
    of the first bytes, so a 100 MB video is never pulled through the backend; a text type (no head
    to sniff, capped at 5 MiB) is read whole and must be valid UTF-8, the inline path's check
    (``validate_attachment_bytes``) -- a Worker's .md/.csv deliverable arrives this way. Re-adopting
    the same id (a retried reply) just rewrites the same metadata."""
    content_type = canonical_attachment_content_type(file_name, content_type)
    expected = _ATTACHMENT_SIGNATURES.get(content_type)
    if not expected:
        raise ValueError(f"{content_type} cannot be uploaded directly")
    normalized = str(uuid.UUID(attachment_id))
    suffix = expected[1]
    key = attachment_key(owner_id, room_id, f"{normalized}{suffix}")
    size = hermes_profiles.owner_object_size(key)
    if size is None:
        raise ValueError("uploaded attachment not found")
    if not 0 < size <= ATTACHMENT_MAX_BYTES_BY_TYPE[content_type]:
        raise ValueError(f"attachment must be at most {ATTACHMENT_MAX_BYTES_BY_TYPE[content_type]} bytes")
    if expected[0] is None:
        # Bounded by the HEAD's size (already within the cap): an object swapped in after the HEAD is
        # never read past it.
        validate_attachment_bytes(content_type, hermes_profiles.get_owner_object_head(key, size))
    elif not _signature_ok(content_type, hermes_profiles.get_owner_object_head(key, SNIFF_BYTES)):
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
    # Lockstep with the byte route: a zip and an HTML page always download, never render.
    disposition = "attachment" if metadata.content_type in ("application/zip", "text/html") else "inline"
    return hermes_profiles.presign_owner_object(
        attachment_key(owner_id, room_id, f"{normalized}{_ATTACHMENT_SIGNATURES[metadata.content_type][1]}"),
        content_type=metadata.content_type,
        disposition=f"{disposition}; filename*=UTF-8''{quote(metadata.file_name, safe='')}",
        expires_in=ATTACHMENT_URL_TTL_S,
    )


def _attachment_metadata(owner_id: str, room_id: str, normalized: str) -> tuple[MessageAttachment, str]:
    """One attachment's metadata and the key of its bytes; raises ValueError/KeyError/OSError when absent."""
    raw = hermes_profiles.get_owner_object(attachment_key(owner_id, room_id, f"{normalized}.json"))
    if raw is None:
        raise ValueError("attachment metadata not found")
    metadata = MessageAttachment.model_validate_json(raw)
    if str(metadata.id) != normalized:
        raise ValueError("attachment metadata mismatch")
    suffix = _ATTACHMENT_SIGNATURES[metadata.content_type][1]
    return metadata, attachment_key(owner_id, room_id, f"{normalized}{suffix}")


ATTACHMENT_HEAD_MAX = 16 * 1024


def load_attachment_head(
    owner_id: str, room_id: str, attachment_id: str, length: int
) -> tuple[MessageAttachment, bytes]:
    """The first ``length`` bytes (at most 16 KB) of one attachment, by a ranged read: a tile's preview never pulls the
    whole file. The caller has checked room membership, as for ``load_attachment``."""
    normalized = str(uuid.UUID(attachment_id))
    length = max(1, min(length, ATTACHMENT_HEAD_MAX))
    try:
        metadata, key = _attachment_metadata(owner_id, room_id, normalized)
        data = hermes_profiles.get_owner_object_head(key, min(length, metadata.size)) if metadata.size else b""
    except (OSError, ValueError, KeyError, ClientError) as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Attachment not found.") from exc
    return metadata, data


def load_attachment(owner_id: str, room_id: str, attachment_id: str) -> tuple[MessageAttachment, bytes]:
    normalized = str(uuid.UUID(attachment_id))
    try:
        metadata, key = _attachment_metadata(owner_id, room_id, normalized)
        data = hermes_profiles.get_owner_object(key)
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
    """Read the workspace's agent roster from the ``AGENT#`` registry."""
    from core.services import agent_registry

    try:
        rows = await agent_registry.roster(owner_id)
        # The front agent posts and is seated as "Lightbulb" (contracts-v2 §9), whatever its registry name; the
        # same pick as task_conversation._front_agent (the first active ceo).
        front_id = next(
            (
                r.get("id")
                for r in rows
                if isinstance(r, dict) and r.get("role") == "ceo" and r.get("status") == "active"
            ),
            None,
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
        name = (
            FRONT_AGENT_NAME
            if front_id and str(row.get("id")) == str(front_id)
            else str(row.get("name") or agent_id)[:256]
        )
        roster.append({**row, "id": agent_id, "name": name})
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
    handle = (await _handles_for(owner_id, roster)).get(agent_id)
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
        return email_label(str(emails[0]["email_address"]))[:256] or user_id
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
    handles = await _handles_for(owner_id, roster)
    return {
        "kind": "agent",
        "id": member.id,
        "displayName": row["name"],
        "mentionHandle": handles[member.id],
        "role": "member",
        "listenMode": member.listen_mode,
    }


async def _front_agent_id(owner_id: str) -> str | None:
    """The front agent ("Lightbulb"); None when the workspace has none yet."""
    from core.services import task_conversation  # lazy: task_conversation imports this module

    try:
        return await task_conversation.front_agent_id(owner_id)
    except task_conversation.NoFrontAgent:
        return None


async def _handles_for(owner_id: str, roster: list[dict]) -> dict[str, str]:
    """Every agent's handle: name-derived, except that `lightbulb` is reserved for the front
    agent. One derivation for seating, re-seating and renames, so a re-stamp never flips lightbulb → aden."""
    front = await _front_agent_id(owner_id)
    handles = mention_handles(roster, reserved=RESERVED_HANDLES if front else ())
    if front and front in handles:
        handles[front] = "lightbulb"
    return handles


async def _router_room(meta: dict, owner_id: str) -> bool:
    """A room people chat with Lightbulb in: `admission.router_room`."""
    from core.services import admission  # lazy: admission imports this module

    return await admission.router_room(meta, owner_id)


async def _lightbulb_only(owner_id: str, members: list[CreateRoomMember], kind: str) -> list[CreateRoomMember]:
    """A new Router room seats one agent, Lightbulb (contracts-v2 §6); specialists are named by `@handle`
    and the Router pins them. A human-to-human DM stays agent-free; a DM to any agent is a DM to Lightbulb."""
    from core.services import task_conversation

    agents = [m for m in members if m.kind == "agent"]
    humans = [m for m in members if m.kind != "agent"]
    if kind == "dm" and not agents:
        return humans
    try:
        front = await task_conversation.front_agent_id(owner_id)
    except task_conversation.NoFrontAgent:
        raise ApiProblem("MEMBER_NOT_FOUND", "This workspace has no Lightbulb agent yet.") from None
    listen = next((m.listen_mode for m in agents if m.id == front), None) or (agents[0].listen_mode if agents else None)
    return [*humans, CreateRoomMember(kind="agent", id=front, role="member", listen_mode=listen or "active")]


#: Event-id prefixes that may still post in a Router room as another agent: A2A hold cards and Live late answers,
#: until lane Q's Question holds replace them (lead ruling, 5 Nov 2026; lanes/O.md).
_VOICE_EXEMPT_EVENT_PREFIXES = ("a2a:", "live-late:")


async def _require_lightbulb_voice(owner_id: str, meta: dict, agent_id: str, event_id: str = "") -> None:
    """Contracts-v2 §6: in rooms people chat in only Lightbulb (the front agent) posts; a Worker posts only in
    its own task room (the task's specialist). Everything else is refused, `lightbulb-post.sh --room` included."""
    from core.services import task_conversation

    if event_id.startswith(_VOICE_EXEMPT_EVENT_PREFIXES):
        return
    try:
        front = await task_conversation.front_agent_id(owner_id, room_id=meta["roomId"])
    except task_conversation.NoFrontAgent:
        front = None  # a Worker may still finish its own already-authorized task below
    if agent_id == front:
        return
    task = meta.get("task") or {}
    if task.get("taskId"):
        from core.repositories import tasks_repo

        row = await tasks_repo.get_task(owner_id, task["taskId"])
        # ponytail: the task's specialist may post in any of that specialist's task rooms; binding the post to
        # the attempt's runSecret is the upgrade when per-task VMs ship.
        if row and row.get("specialist") == agent_id:
            return
    raise ApiProblem("ROOM_FORBIDDEN", "Only Lightbulb posts here. A task's notes go in its own task room.")


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
        if meta.get("session") and meta["session"].get("ownerId") != user_id:
            # A session's chat: once its owner removed the session, only they are left in it. The
            # removal unseats everyone else, but this door does not wait for that cleanup.
            from core.repositories import local_sessions_repo  # lazy: avoids an import cycle

            row = await local_sessions_repo.get_session(meta["ownerId"], meta["session"]["sessionId"])
            if row is not None and row.get("removedAt") is not None:
                raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        elif meta.get("app"):
            raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")  # Apps are deleted: an app's conversation stays shut
        elif meta.get("task"):
            # A task's conversation: a seat never outlives `can_view` (left the home room, dropped from the
            # audience, left the workspace). Seats are never swept, so this door is the check.
            from core.repositories import tasks_repo  # lazy: avoids an import cycle
            from core.services import tasks_access

            task = await tasks_repo.get_task(meta["ownerId"], meta["task"]["taskId"])
            if task is None or not await tasks_access.can_view(task, user_id):
                raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
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
        "first_run": bool(meta.get("firstRun")),
        "archived_at": meta.get("archivedAt"),
        "archived_by": meta.get("archivedBy"),
        "icon": meta.get("icon"),
        "color": meta.get("color"),
        "paused_at": meta.get("pausedAt"),
        "paused_by": meta.get("pausedBy"),
        "session": SessionRef(session_id=meta["session"]["sessionId"]) if meta.get("session") else None,
        "feed": is_feed_room(meta),
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
# Note this bounds ROOMS, and each one issues two calls concurrently (plus one
# small mention COUNT query for a room with unread) — so the
# real ceiling on submitted work is twice this number. That is deliberate:
# the calls are IO-bound and mostly sit waiting on DynamoDB, so a modest queue
# behind the pool costs nothing, whereas the serial version this replaced cost
# two full round trips per room in sequence.
_SUMMARY_CONCURRENCY = 8


async def _visible_room_metas(owner_id: str, user_id: str) -> list[dict]:
    """Rooms the GSI says the caller belongs to, minus tombstoned rooms and Meet
    rooms. Discovery only: every caller still rechecks the strong ``MEMBER#`` row."""
    return [
        row["meta"]
        for row in await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
        # ``hidden``: a Google Meet's room -- the meeting's record, never a place people go.
        if not row["meta"].get("deletedAt") and not row["meta"].get("hidden")
    ]


async def still_reads_room(owner_id: str, user_id: str, room_id: str, room_owner: str) -> bool:
    """The room door's membership rule, strongly read: a current human member whose home is ``owner_id``."""
    member = await messaging_repo.get_member(room_id, "human", user_id)
    return member is not None and messaging_repo.member_home(member, room_owner) == owner_id


async def still_opens_room(owner_id: str, user_id: str, room_id: str) -> bool:
    """``require_room_member`` as a yes/no: membership plus the session, app and task room rules."""
    try:
        await require_room_member(owner_id, user_id, room_id)
    except ApiProblem:
        return False
    return True


async def searchable_room_metas(owner_id: str, user_id: str) -> list[dict]:
    """The META row of every room the caller can read right now -- the same membership
    rule as ``require_room_member``."""
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def check(meta: dict) -> dict | None:
        async with limit:
            ok = await still_reads_room(owner_id, user_id, meta["roomId"], meta["ownerId"])
        return meta if ok else None

    checked = await asyncio.gather(*(check(meta) for meta in await _visible_room_metas(owner_id, user_id)))
    return [meta for meta in checked if meta]


async def searchable_rooms(owner_id: str, user_id: str) -> dict[str, str]:
    """Room id -> the room's OWNER (whose search index holds it), for every room the caller can read right now."""
    return {meta["roomId"]: meta["ownerId"] for meta in await searchable_room_metas(owner_id, user_id)}


async def _room_summaries(owner_id: str, user_id: str) -> list[RoomSummary]:
    """Every room the caller can see, with the members each row needs to name
    itself.

    The per-room reads run concurrently. They used to run serially -- two
    awaited round trips per room, so a 20-room workspace was a 40-deep serial
    chain before the response could start. The membership recheck stays: the
    GSI backing ``list_rooms_for_member`` is eventually consistent, so a member
    removed a moment ago can still be discovered by it, and the strongly
    consistent ``list_members`` read (which carries the caller's own row) keeps
    them out of the response.
    """
    discovered = await _visible_room_metas(owner_id, user_id)
    limit = asyncio.Semaphore(_SUMMARY_CONCURRENCY)

    async def load(meta: dict) -> tuple | None:
        room_id = meta["roomId"]
        async with limit:
            # list_members is a strongly consistent read of every MEMBER# row, so the
            # caller's own row comes out of it: no separate get_member per room.
            read, members = await asyncio.gather(
                messaging_repo.get_read_row(room_id, user_id),
                messaging_repo.list_members(room_id),
            )
            member = next(
                (row for row in members if row.get("kind") == "human" and str(row.get("id")) == user_id), None
            )
            if member is None:
                return None
            return meta, member, read, members, await _mention_count(meta, member, read, user_id)

    loaded = [row for row in await asyncio.gather(*(load(meta) for meta in discovered)) if row is not None]
    # One users-table read per distinct person, not one per person per room.
    emails = await human_emails([m for _, _, _, members, _ in loaded for m in members])
    summaries = [
        RoomSummary(
            **_room_summary_fields(meta, member, read, mentions),
            members=[_room_member(item) for item in with_human_handles(members, emails)],
        )
        for meta, member, read, members, mentions in loaded
    ]
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
    """Stable identity for the one fresh-workspace general room. Mirrored by the Hermes bridge
    (apps/infra/hermes/bridge/turn.py default_room_id) for the first-run prompt: edit both together."""
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


def is_feed_room(meta: dict) -> bool:
    """The workspace feed (Activity) -- by id, never by name, like ``is_default_room``."""
    owner_id = meta.get("ownerId") or ""
    return bool(owner_id) and str(meta.get("roomId")) == messaging_repo.feed_room_id(owner_id)


def is_session_room(meta: dict) -> bool:
    """A session's chat (PR #3807): hidden, and its humans follow session read access."""
    return bool(meta.get("session"))


def _deny_vendor_human_change(meta: dict, kind: str) -> None:
    # A session chat's people follow session read access (spec 2026-10-07 decision 2).
    # The feed's people are the workspace's: only a workspace join/removal changes them.
    # The feed's membership (people only; agents are its authors) is Lightbulb's alone, every kind.
    if is_feed_room(meta) or (kind == "human" and (is_vendor_room(meta) or is_session_room(meta))):
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
    return await _autojoin(owner_id, default_room_id(owner_id), user_id)


async def join_feed_room(owner_id: str, user_id: str) -> bool:
    """``join_default_room`` for the workspace feed: the same one-time ``AUTOJOIN#`` marker."""
    return await _autojoin(owner_id, messaging_repo.feed_room_id(owner_id), user_id)


async def _autojoin(owner_id: str, room_id: str, user_id: str) -> bool:
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
        if await messaging_repo.ensure_user_stream(owner_id, user_id) is None:
            await messaging_repo.release_autojoin(room_id, user_id)
            return False  # a removed member: only the membership webhook's rejoin turns them back on
        meta = await messaging_repo.upsert_member(room_id=room_id, owner_id=owner_id, member=member)
    except messaging_repo.MemberLimitExceeded:
        # Keep the marker: a full room must not turn every poll into a retry. Not for the feed: a
        # person always belongs there, and get_feed fails loudly until a seat frees.
        if room_id == messaging_repo.feed_room_id(owner_id):
            await messaging_repo.release_autojoin(room_id, user_id)
        logger.warning("room %s full; %s not added for owner %s", room_id, user_id, owner_id)
        return False
    except Exception:
        await messaging_repo.release_autojoin(room_id, user_id)
        raise
    # A workspace removal that finished while we seated them (session_chat's re-check): undo the
    # seat, which rotates the key they may have seen. The feed marker is freed for a later rejoin.
    stream = await messaging_repo.get_user_stream(owner_id, user_id)
    if not (stream and stream.get("active")):
        try:
            await _remove_human_seat(owner_id, room_id, user_id)
        except Exception:
            # Free the feed marker even so: the stream is off, so no read can re-seat them, and
            # revoke's second pass still removes the seat. Logged, never hiding the undo's error.
            if is_feed_room(meta):
                try:
                    await messaging_repo.release_autojoin(room_id, user_id)
                except Exception:
                    logger.exception("feed autojoin release failed owner=%s user=%s", owner_id, user_id)
            raise
        if is_feed_room(meta):
            await messaging_repo.release_autojoin(room_id, user_id)
        return False
    _emit_member_added(owner_id, user_id, "human", by="system")
    await _publish_room_metadata(meta)
    return True


async def ensure_default_room(owner_id: str, owner_user_id: str) -> str | None:
    """Create the starter general room once, never reconcile it afterward."""
    from core.repositories.workspace_repo import WorkspaceRepo

    room_id = default_room_id(owner_id)
    existing_room = await messaging_repo.get_room(room_id)
    if existing_room is not None and int(existing_room.get("lastSeq") or 0) > 0:
        return room_id

    company = await WorkspaceRepo().resolve_owner_row(owner_id, consistent_read=True)
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
    handles = await _handles_for(owner_id, roster)
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
    attrs = None
    front = await _front_agent_id(owner_id)
    if front:
        # Super-agent (UX slice 6): the owner's first run is a chat with Lightbulb, in their DM. The DM goes
        # first: #general's message is the "done" signal above, so a failure in between redoes this, idempotently.
        await _post_first_run_welcome(owner_id, owner_user_id, front, welcome)
        welcome, attrs = team_welcome(org.get("name")), {"kind": TEAM_WELCOME_KIND}
    await messaging_repo.create_message(
        room_id=room_id,
        owner_id=owner_id,
        idemp_pk=messaging_repo.room_pk(room_id),
        idemp_sk=messaging_repo.human_idemp_sk("__system__", f"{client_room_id}:welcome"),
        # The team line's hash leaves the org name out: a rename between attempts must replay, not conflict.
        req_hash=messaging_repo.request_hash(TEAM_WELCOME_KIND if attrs else welcome, ceo_id),
        author={"kind": "agent", "id": ceo_id, "displayName": agents[ceo_id]["name"]},
        text=welcome,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=ceo_id,
        client_message_id=None,
        attrs=attrs,
    )
    return room_id


async def _post_first_run_welcome(owner_id: str, owner_user_id: str, front: str, text: str) -> str:
    """The welcome in the owner's Lightbulb DM, kind ``FIRST_RUN_KIND``. A new DM is created with ``firstRun`` in
    the same write, so it is never visible without it. A DM the owner already wrote in (before provisioning
    finished) still gets the welcome, after their messages. Replays land on the same message."""
    room_id = await _agent_dm_room_id(owner_id, owner_user_id, front, meta_extra={"firstRun": True})
    _, replayed = await messaging_repo.create_message(
        room_id=room_id,
        owner_id=owner_id,
        idemp_pk=messaging_repo.room_pk(room_id),
        idemp_sk=messaging_repo.human_idemp_sk("__system__", f"first-run:{owner_id}:welcome"),
        # The org name is in the text, not the hash: a rename between attempts replays instead of conflicting.
        req_hash=messaging_repo.request_hash(FIRST_RUN_KIND, front),
        author={"kind": "agent", "id": front, "displayName": FRONT_AGENT_NAME},
        text=text,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=front,
        client_message_id=None,
        attrs={"kind": FIRST_RUN_KIND},
    )
    # A DM that existed already (found, not created) gets the flag now; never again on a replay, which would reopen
    # a first run the owner has since answered.
    if not replayed and not (await messaging_repo.get_room(room_id) or {}).get("firstRun"):
        await messaging_repo.patch_room_meta(room_id, {"firstRun": True})
    return room_id


async def _feed_owner(owner_id: str, company, people: list[dict], requester: str | None) -> str | None:
    """The feed's owner: the FIRST ACTIVE workspace member among the company's provisioner, the
    Clerk org's ``created_by``, any org admin, then the requester. Active = in the Clerk org
    and no deactivated stream (``revoke_human_membership`` turns it off before Clerk may catch
    up). Never a departed user: the human guard would make them unremovable."""
    present = {row["user_id"] for row in people}
    org = await clerk_admin.get_organization(owner_id) or {}
    candidates = [
        getattr(company, "provisioner_user_id", ""),
        org.get("created_by"),
        *(row["user_id"] for row in people if row.get("role") in ("org:admin", "admin")),
        requester,
    ]
    for user_id in candidates:
        if user_id and user_id in present:
            stream = await messaging_repo.get_user_stream(owner_id, user_id)
            if stream is None or stream.get("active"):
                return user_id
    return None


async def ensure_feed_room(owner_id: str, requester_user_id: str | None = None) -> str | None:
    """Create the workspace feed once, with the CEO's welcome post (spec 2026-10-14 §4.2).

    Modelled on ``ensure_default_room``: a room with a message returns at once (one consistent
    ``get_room``), so the Activity page can call it on every read and a workspace that predates
    the feed gets it lazily, with no backfill. Only people sit in it: every workspace human is
    seated through the ``AUTOJOIN#`` marker, so a later teammate joins the same way. Agents are its
    authors, never members (``room_agent``). Its owner is chosen by ``_feed_owner`` (the requester only
    as a last resort): the create receipt is per creator, so one identity lets racing callers
    converge. Returns None until the company is active; raises ``ROOM_NOT_FOUND`` (404) for a
    workspace with no active human, and a retryable 503 while Clerk cannot be read."""
    from core.repositories.workspace_repo import WorkspaceRepo
    from core.services.user_provision import ceo_agent_id

    room_id = messaging_repo.feed_room_id(owner_id)
    existing_room = await messaging_repo.get_room(room_id)
    if existing_room is not None and int(existing_room.get("lastSeq") or 0) > 0:
        return room_id

    company = await WorkspaceRepo().resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return None
    ceo_id = ceo_agent_id(company)
    roster = await load_agent_roster(owner_id)
    agents = {row["id"]: row for row in roster}
    if ceo_id not in agents:
        return None
    client_room_id = f"workspace-feed-v1:{owner_id}"
    try:
        people = await clerk_admin.list_organization_members(owner_id, strict=True)
    except Exception as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "Workspace members are unavailable.", retryable=True, retry_after=3
        ) from exc
    if existing_room is None:
        owner_user_id = await _feed_owner(owner_id, company, people, requester_user_id)
        if not owner_user_id:
            raise ApiProblem("ROOM_NOT_FOUND", "This workspace has no active member to own a feed.")
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
        await messaging_repo.ensure_user_stream(owner_id, owner_user_id)
        normalized = [{key: member.get(key) for key in ("kind", "id", "role", "listenMode")} for member in members]
        try:
            await messaging_repo.create_room(
                owner_id=owner_id,
                room_id=room_id,
                client_room_id=client_room_id,
                creator_user_id=owner_user_id,
                name=FEED_ROOM_NAME,
                kind="channel",
                members=members,
                req_hash=messaging_repo.request_hash(FEED_ROOM_NAME, "channel", normalized),
                extra={"topic": FEED_DEFAULT_TOPIC},
            )
            _emit_room_created(owner_id, owner_user_id, "channel", members[1:], by="system")
        except messaging_repo.MessagingRepoError:
            # A racing create won (another roster, or a create from before this rule): same room.
            if await messaging_repo.get_room(room_id) is None:
                raise
    # Every current teammate, before the welcome: a room with a message is the "done" signal. Read
    # again now the room exists: one who joined after the read above found no room to join.
    try:
        people = await clerk_admin.list_organization_members(owner_id, strict=True)
    except Exception as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "Workspace members are unavailable.", retryable=True, retry_after=3
        ) from exc
    for row in people:
        try:
            await join_feed_room(owner_id, row["user_id"])
        except Exception:  # noqa: BLE001 -- one teammate never blocks the feed
            logger.exception("feed seat failed owner=%s user=%s", owner_id, row.get("user_id"))
    await messaging_repo.create_message(
        room_id=room_id,
        owner_id=owner_id,
        idemp_pk=messaging_repo.room_pk(room_id),
        idemp_sk=messaging_repo.human_idemp_sk("__system__", f"{client_room_id}:welcome"),
        req_hash=messaging_repo.request_hash(FEED_WELCOME, ceo_id),
        author={"kind": "agent", "id": ceo_id, "displayName": agents[ceo_id]["name"]},
        text=FEED_WELCOME,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=ceo_id,
        client_message_id=None,
        author_member_check=False,  # the CEO writes the feed as an author, never a member
        attrs={"kind": "welcome"},  # clients recognise the welcome by kind, never by its text
    )
    return room_id


class FeedResponse(ContractModel):
    room_id: uuid.UUID
    topic: str | None


async def get_feed(auth: AuthContext) -> FeedResponse:
    """The Activity page's read: ensure the feed, seat the caller, return its id and brief."""
    owner_id = resolve_owner_id(auth)
    room_id = await ensure_feed_room(owner_id, auth.user_id)
    if room_id is None:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "The feed is not ready yet.", retryable=True, retry_after=3)
    await join_feed_room(owner_id, auth.user_id)
    if await messaging_repo.get_member(room_id, "human", auth.user_id) is None:
        # Never report a feed the reader is not in (a full room, or a removal racing this read).
        logger.error("feed seat missing owner=%s user=%s", owner_id, auth.user_id)
        put_metric("messaging.feed.human_unseated")
        raise ApiProblem("MEMBER_LIMIT", "You could not be added to this workspace's feed.")
    meta = await messaging_repo.get_room(room_id) or {}
    return FeedResponse(room_id=room_id, topic=meta.get("topic"))


#: How many recent feed posts an agent reads before it writes one.
FEED_AGENT_READ_LIMIT = 20


async def agent_feed(owner_id: str) -> dict[str, Any]:
    """The feed's brief and latest posts for an agent outside a room turn (a scheduled run has
    no signed-in person, so ``lightbulb_read_rooms`` reads nothing). Every workspace human is in
    the feed, so this widens nobody's view."""
    room_id = await ensure_feed_room(owner_id)
    if room_id is None:
        raise ApiProblem("ACTION_UNAVAILABLE", "This workspace has no feed yet.")
    meta = await messaging_repo.get_room(room_id) or {}
    # ponytail: the newest 100 rows, top-level kept; a Discuss thread longer than ~80 replies in
    # that window shortens the list. A top-level index if that is ever seen.
    rows = await messaging_repo.list_messages_before(room_id, int(meta.get("lastSeq") or 0) + 1, 100)
    posts = _listable([row for row in rows if not row.get("deletedAt") and not row.get("threadRootMessageId")])[
        -FEED_AGENT_READ_LIMIT:
    ]
    return {
        "roomId": room_id,
        "topic": meta.get("topic"),
        "posts": [
            {
                "author": post.author.display_name,
                "text": post.text,
                "postReason": post.post_reason,
                "createdAt": post.created_at.isoformat(),
            }
            for post in posts
        ],
    }


async def room_agent(room_id: str, agent_id: str, meta: dict | None = None) -> dict | None:
    """The agent's MEMBER row in this room -- or, in the feed, a roster-backed stand-in.

    The feed seats only people; agents are its authors. Every door that asks "may this agent act
    in this room" (posting, a Discuss turn's dispatch, its run) asks here, so the feed admits
    exactly this owner's roster and every other room keeps its membership rule. ``meta`` saves
    the room read when the caller holds it; the room is read only when there is no member row."""
    member = await messaging_repo.get_member(room_id, "agent", agent_id)
    if member is not None:
        return member
    meta = {**(meta or await messaging_repo.get_room(room_id) or {}), "roomId": room_id}
    if not is_feed_room(meta):
        # A task's room seats Lightbulb and its readers; the task's specialist (the Worker) is its author
        # the same way: a roster stand-in, never a seat (contracts-v2 §6).
        task_id = (meta.get("task") or {}).get("taskId")
        if not task_id:
            return None
        from core.repositories import tasks_repo

        task = await tasks_repo.get_task(meta["ownerId"], task_id)
        if not task or task.get("specialist") != agent_id:
            return None
    return await _stand_in(meta["ownerId"], agent_id)


async def _stand_in(owner_id: str, agent_id: str) -> dict | None:
    """A roster-backed author with no seat (the feed, a task room's specialist, an exempt A2A/Live post)."""
    roster = await load_agent_roster(owner_id)
    row = next((agent for agent in roster if agent["id"] == agent_id), None)
    if row is None:
        return None
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": row["name"],
        "mentionHandle": (await _handles_for(owner_id, roster))[agent_id],
        "role": "member",
        "listenMode": "mentions",
        "ownerId": owner_id,
        "standIn": True,  # no MEMBER# row: the write skips the author membership check
    }


def _display_name_from_membership(row: dict) -> str:
    """Same precedence as ``_resolve_human_display_name``, from the Clerk membership row
    we already hold — one roster read instead of one ``get_user`` per human."""
    name = " ".join(part.strip() for part in (row.get("first_name") or "", row.get("last_name") or "") if part.strip())
    if name:
        return name[:256]
    return (email_label(row.get("email")) or row["user_id"])[:256]


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

    from core.repositories.workspace_repo import WorkspaceRepo
    from core.services.clerk_admin import get_organization, list_organization_members

    company = await WorkspaceRepo().resolve_owner_row(owner_id, consistent_read=True)
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
                    "mentionHandle": (await _handles_for(owner_id, roster))[ceo_id],
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
    removed = {
        m["id"]
        for m in members
        if m["kind"] == "human" and await messaging_repo.ensure_user_stream(m["homeOwnerId"], m["id"]) is None
    }
    if owner_user_id in removed:
        return None  # the room's owner is a removed member: no owner, no room yet
    members = [m for m in members if m["id"] not in removed]  # a removed member is not seated
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
    if await messaging_repo.ensure_user_stream(home_owner_id, user_id) is None:
        return False  # a removed member
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


async def _live_dm(room_id: str, user_id: str, counterpart: CreateRoomMember) -> dict | None:
    """The room a ``DMPAIR#`` row names, if it is still this pair's 1:1 DM. Adding a
    member turns a DM into a group DM, and the counterpart can leave one."""
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("kind") != "dm" or meta.get("deletedAt"):
        return None
    if await messaging_repo.get_member(room_id, "human", user_id) is None:
        return None
    if await messaging_repo.get_member(room_id, counterpart.kind, counterpart.id) is None:
        return None
    return meta


async def create_room(auth: AuthContext, request: CreateRoomRequest, *, meta_extra: dict | None = None) -> RoomDetail:
    """``meta_extra``: server-only META fields written with the room (never from a request)."""
    owner_id = resolve_owner_id(auth)
    project_fields = {
        k: v for k, v in (("topic", request.topic), ("icon", request.icon), ("color", request.color)) if v
    }
    front = await _front_agent_id(owner_id)
    if front:  # a new room is a Router room
        request = request.model_copy(update={"members": await _lightbulb_only(owner_id, request.members, request.kind)})
    # As PATCH: only a project has Instructions and a tile -- a channel, or a DM or group chat Lightbulb is in.
    seated = bool(front) and any(m.kind == "agent" and m.id == front for m in request.members)
    if project_fields and request.kind != "channel" and not seated:
        raise ApiProblem("INVALID_REQUEST", "Only a channel or a chat with Lightbulb has instructions and an icon.")
    keys = [(member.kind, member.id) for member in request.members]
    if len(keys) != len(set(keys)) or ("human", auth.user_id) in keys:
        raise ApiProblem("INVALID_REQUEST", "Room members must be unique and omit the creator.")
    if request.kind == "dm" and len(request.members) != 1:
        # Private agent memory treats a dm as one person's; a 3-person "dm" would leak it.
        raise ApiProblem("INVALID_REQUEST", "A direct message is between you and exactly one other member.")
    if len(request.members) + 1 > messaging_repo.MAX_ROOM_MEMBERS:
        raise ApiProblem("MEMBER_LIMIT", f"Rooms hold at most {messaging_repo.MAX_ROOM_MEMBERS} members.")
    if request.kind == "dm":
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
        if await messaging_repo.ensure_user_stream(owner_id, human["id"]) is None:
            # A removed member: the creator (stale JWT) is refused, a removed invitee is not found.
            if human["id"] == auth.user_id:
                raise ApiProblem("WORKSPACE_MEMBERSHIP_REQUIRED", "You are no longer a member of this workspace.")
            raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")
    normalized = [{key: member.get(key) for key in ("kind", "id", "role", "listenMode")} for member in members[1:]]
    room_id = messaging_repo.deterministic_room_id(owner_id, auth.user_id, str(request.client_room_id))
    req_hash = messaging_repo.request_hash(request.name, request.kind, normalized)
    # _find_existing_dm reads an eventually consistent GSI, so two racing creates can both
    # miss; the pair row makes the second one land on the first one's room.
    dm_pair = stale_room = None
    if request.kind == "dm":
        counterpart = request.members[0]
        dm_pair = messaging_repo.dm_pair_sk(("human", auth.user_id), (counterpart.kind, counterpart.id))
    for attempt in range(2):
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
                extra={**project_fields, **(meta_extra or {}), **({"frontAgentId": front} if seated else {})},
                dm_pair=dm_pair,
                dm_pair_replaces=stale_room,
            )
            break
        except messaging_repo.DmPairTaken as exc:
            existing = await _live_dm(exc.room_id, auth.user_id, request.members[0])
            if existing is not None:
                return await _room_detail(existing, auth.user_id)
            if attempt:
                raise
            # A group DM now, or the counterpart left: take the row over in the same
            # conditional write, so a racing create still finds exactly one DM.
            stale_room = exc.room_id
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
    # The feed's topic is the team's brief: any member edits it, like a channel topic (spec §4.2).
    feed_brief = (
        is_feed_room(meta) and set(updates) == {"topic"} and request.archived is None and request.paused is None
    )
    room_admin = member.get("role") in {"owner", "admin"}
    kind = meta.get("kind")
    # A DM or group chat Lightbulb is in is a project too (founder ruling, 6 Nov 2026), run by its people: any member
    # of a group chat, and a DM's one person, may pause it and set its Instructions, icon and colour. Renaming stays
    # the owner's/admin's.
    chat_project = kind in ("dm", "group-dm") and await _router_room(meta, owner_id)
    member_may = (
        set(updates) <= {"topic", "icon", "color"}
        if chat_project
        else kind == "group-dm" and not updates and request.paused is None  # archive/unarchive a group chat
    )
    # Pause/resume alone: a workspace admin may do it too (UX slice 6), but never someone's private DM.
    pause_only = request.paused is not None and not updates and request.archived is None
    if not room_admin and not member_may and not feed_brief and not (pause_only and auth.is_org_admin and kind != "dm"):
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    # A DM's display name is derived from its counterpart; a stored name would
    # shadow it with nothing in the UI able to clear it. (Finding from #2483,
    # merged to main — this generalizes the same guard to updates-scoping.)
    if "name" in updates and meta.get("kind") == "dm":
        raise ApiProblem("ROOM_FORBIDDEN", "Direct messages cannot be renamed.")
    if "name" in updates and is_vendor_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "This channel's name is managed by Lightbulb.")
    if "name" in updates and is_session_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "This chat is named after its session.")
    if "name" in updates and is_feed_room(meta):
        raise ApiProblem("ROOM_FORBIDDEN", "The feed's name is managed by Lightbulb.")
    if request.archived is not None and is_session_room(meta):
        raise ApiProblem("ROOM_NOT_ARCHIVABLE", "This chat opens and closes with its session.")
    if request.archived is not None and request.archived != bool(meta.get("archivedAt")):
        if request.archived:
            _require_archivable(meta)
            updates |= {"archivedAt": utc_now().isoformat(), "archivedBy": auth.user_id}
        else:
            updates |= {"archivedAt": None, "archivedBy": None}
    if (request.icon or request.color) and kind != "channel" and not chat_project:
        raise ApiProblem("ROOM_FORBIDDEN", "Only a channel or a chat with Lightbulb has an icon and colour.")
    paused = None
    if request.paused is not None and request.paused != bool(meta.get("pausedAt")):
        if (
            (kind != "channel" and not chat_project)
            or is_feed_room(meta)
            or is_vendor_room(meta)
            or is_session_room(meta)
        ):
            raise ApiProblem("ROOM_NOT_PAUSABLE", "Only a channel or a chat with Lightbulb can be paused.")
        paused = request.paused
        updates |= (
            {"pausedAt": utc_now().isoformat(), "pausedBy": auth.user_id}
            if paused
            else {"pausedAt": None, "pausedBy": None}
        )
    if not updates:
        return await _room_detail(meta, auth.user_id)
    try:
        meta = await messaging_repo.patch_room_meta(room_id, updates)
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    if paused is not None:
        from core.services import questions  # lazy: the admin-actions audit row

        await questions.audit(
            auth.user_id, owner_id, "project.paused" if paused else "project.resumed", {"roomId": room_id}
        )
    if updates.get("archivedAt"):
        await livekit_huddles.close_room(room_id)  # an archived channel holds no huddle; never raises
    await _publish_room_metadata(meta)
    if paused is False:  # W18-8: the question asked while paused is answered now
        from core.services import admission  # lazy: admission imports this module

        await admission.answer_paused_ask(room_id, auth.user_id)
    elif paused:  # a new pause window starts empty
        await messaging_repo.pop_paused_ask(room_id)
    return await _room_detail(meta, auth.user_id)


def _require_archivable(meta: dict) -> None:
    """A plain channel or a group DM archives; a 1:1 DM does not. #general is where
    join_default_room seats every new teammate and onboarding renders; the shared vendor
    channel is the customer's support line."""
    if (
        meta.get("kind") not in {"channel", "group-dm"}
        or is_default_room(meta)
        or is_vendor_room(meta)
        or is_feed_room(meta)
    ):
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
    if meta.get("kind") == "dm" and existing is None:
        return await _group_from_dm(auth, meta, request)
    if existing and existing.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner role cannot be changed.")
    roster = await load_agent_roster(owner_id) if request.kind == "agent" else []
    member = await _resolved_member(owner_id, request, roster)
    if request.kind == "agent":
        # A person's choice of mode; a system sweep never overrides it.
        member = {**member, "modeSetBy": "user"}
    if request.kind == "human" and await messaging_repo.ensure_user_stream(owner_id, request.id) is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")  # a removed member
    handles = await _handles_for(owner_id, roster) if roster else {}
    current = await messaging_repo.list_members(room_id)
    room_agents = [row for row in current if row.get("kind") == "agent"]
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


async def _group_from_dm(auth: AuthContext, meta: dict, request: UpsertRoomMemberRequest) -> RoomDetail:
    """Adding someone to a DM starts a NEW group chat with everyone in the DM plus them, as Slack does (founder ruling,
    7 Nov 2026). The DM stays as it was: its history, Instructions, noted facts and tasks are its people's only. A DM
    between two people does the same, so "Add people" means one thing everywhere. The answer is the new chat's detail
    (a different roomId): the client opens it. Its id derives from the DM and the member set, so a retried add, or
    adding the same person again later, lands on the same group chat."""
    others = [
        CreateRoomMember(kind=m["kind"], id=m["id"], role="member", listen_mode=m.get("listenMode"))
        for m in await messaging_repo.list_members(meta["roomId"])
        if not (m.get("kind") == "human" and m["id"] == auth.user_id)
    ]
    others.append(CreateRoomMember(kind=request.kind, id=request.id, role="member", listen_mode=request.listen_mode))
    key = ",".join(sorted(f"{m.kind}:{m.id}" for m in others))
    client_room_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb:dm-add:{meta['roomId']}:{key}")
    return await create_room(
        auth, CreateRoomRequest(client_room_id=client_room_id, name=None, kind="group-dm", members=others)
    )


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
    """A member removes themselves. A channel's owner hands the channel to another
    person in it as they leave (see _channel_heir); with nobody else there they must
    delete it instead. A group DM's "owner" is just whoever started it and holds no
    powers there, so they simply leave. A DM's starter cannot leave it."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    _deny_vendor_human_change(meta, "human")
    if member.get("role") == "owner" and meta.get("kind") == "dm":
        raise ApiProblem("INVALID_REQUEST", "A direct message cannot be left.")
    if (
        member.get("role") == "owner"
        and meta.get("kind") == "channel"
        and _channel_heir(meta, await messaging_repo.list_members(room_id), auth.user_id) is None
    ):
        raise ApiProblem("INVALID_REQUEST", "You are the only person in this channel. Delete it instead of leaving.")
    await _remove_member_with_notices(meta, "human", auth.user_id)


def _channel_heir(meta: dict, members: list[dict], leaving_id: str) -> str | None:
    """Who owns a channel once its owner is gone: an admin first, then whoever joined
    earliest. None when the leaver is not the channel's owner, or nobody else is there.

    With nobody else there, nobody is made owner, on purpose. Every room power is a
    room membership (require_room_member / require_room_admin): there is no
    workspace-admin override and no way to browse or join a channel, so a workspace
    admin seated as its owner would get a private history they were never part of.
    leave_room refuses that case (delete instead); a workspace removal must never be
    blocked, so it removes the last person and the channel is left with only agents,
    which no person can reach."""
    if meta.get("kind") != "channel":
        return None
    humans = [row for row in members if row.get("kind") == "human"]
    if not any(row["id"] == leaving_id and row.get("role") == "owner" for row in humans):
        return None
    others = [row for row in humans if row["id"] != leaving_id]
    if not others:
        return None
    return min(others, key=lambda row: (row.get("role") != "admin", row.get("joinedAt") or ""))["id"]


async def _remove_member_with_notices(meta: dict, kind: str, member_id: str) -> dict:
    if kind == "agent":
        new_meta = await messaging_repo.remove_member(meta=meta, kind=kind, member_id=member_id)
        if is_feed_room(meta):
            await _mark_removed(meta, member_id)  # a later feed post never re-seats it
        await _publish_room_metadata(new_meta)
        return new_meta

    new_key = str(uuid.uuid4())
    members = await messaging_repo.list_members(meta["roomId"])
    notices = await _rotation_notices(meta, new_key, removed={member_id}, members=members)
    new_meta = await messaging_repo.remove_member(
        meta=meta,
        kind="human",
        member_id=member_id,
        new_delivery_key=new_key,
        live_events=notices,
        # A channel never loses its owner: leaving or a workspace removal hands it on.
        new_owner_id=_channel_heir(meta, members, member_id),
    )
    if meta["roomId"] == default_room_id(meta["ownerId"]) or is_feed_room(meta):
        # A removal (or leave) must stick: the autojoin never re-seats a marked user. For the
        # feed (only a workspace removal reaches here) revoke_human_membership frees it again
        # once the stream is off, so a real rejoin re-seats them.
        await _mark_removed(meta, member_id)
    # The same revocation for the room's huddle: best effort, never raises.
    await livekit_huddles.remove_participant(meta["roomId"], member_id)
    return new_meta


async def _remove_human_seat(owner_id: str, room_id: str, user_id: str) -> None:
    """Remove a human's seat (key rotation + notices), re-reading on a lost revision race: a
    concurrent join or edit moves the revision the removal is conditioned on. Raises after three
    tries, so a request that must unseat someone fails loudly instead of leaving the seat."""
    for attempt in range(3):
        meta = await messaging_repo.get_room(room_id)
        member = await messaging_repo.get_member(room_id, "human", user_id)
        if meta is None or member is None or messaging_repo.member_home(member, meta["ownerId"]) != owner_id:
            return
        try:
            await _remove_member_with_notices(meta, "human", user_id)
            return
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 2:
                raise


async def _mark_removed(meta: dict, member_id: str) -> None:
    """Best-effort -- the removal already committed; a missed marker only risks one re-seat."""
    try:
        await messaging_repo.claim_autojoin(meta["roomId"], member_id)
    except Exception:
        logger.exception("autojoin marker on removal failed room=%s member=%s", meta["roomId"], member_id)


async def _rotation_notices(
    meta: dict, new_key: str, *, removed: set[str] | None = None, members: list[dict] | None = None
) -> list[dict]:
    """The durable ``LIVE`` rows a deliveryKey rotation writes in its own transaction:
    ``room.access.removed`` to each human in ``removed`` (every human when None),
    ``room.subscription.rotated`` with the new key to the rest."""
    if members is None:
        members = await messaging_repo.list_members(meta["roomId"])
    humans = [row for row in members if row.get("kind") == "human"]
    revision = int(meta["revision"]) + 1
    notices: list[dict] = []
    for human in humans:
        home = messaging_repo.member_home(human, meta["ownerId"])
        key = await messaging_repo.ensure_user_stream(home, human["id"])
        if key is None:
            continue  # a removed member's stream is off: nothing to tell them, nothing to revive
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
        if (
            meta.get("kind") != "channel"
            or is_default_room(meta)
            or is_vendor_room(meta)
            or is_session_room(meta)
            or is_feed_room(meta)
        ):
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
    own_session_chats: list[str] = []
    for row in discovered:
        room_id = row["meta"]["roomId"]
        member = await messaging_repo.get_member(room_id, "human", user_id)
        if member is None:
            continue
        meta = await messaging_repo.get_room(room_id)
        if meta is None or messaging_repo.member_home(member, meta["ownerId"]) != owner_id:
            continue
        await _remove_member_with_notices(meta, "human", user_id)
        if is_session_room(meta) and meta["session"].get("ownerId") == user_id:
            own_session_chats.append(meta["session"]["sessionId"])
    # Cooperative revocation of /ws/<ws>/sessions: an open AppSync subscription cannot be closed
    # server-side, so the leaver's own client is told to drop it, while its channel still works.
    # Their stream (and sessions member row) then go off, so no new subscribe is admitted.
    try:
        from core.services.live_events import get_live_event_publisher  # imports this module

        workspace_id = await messaging_repo.ensure_workspace_id(owner_id)
        await get_live_event_publisher().publish_user(
            owner_id, user_id, make_live_event("workspace.access.removed", {"workspaceId": workspace_id})
        )
    except Exception:
        logger.warning("workspace.access.removed notice failed")
    await messaging_repo.deactivate_user_stream(owner_id, user_id)
    # Second pass, stream off: a first seat (#general, the feed) that landed after the list above
    # held no marker and passed its own re-check while the stream was still on.
    # The list reads an eventually consistent GSI that can miss a seat committed just now, so the
    # two auto-seated rooms are swept by id as well (consistent reads; a no-op without a seat).
    swept = {row["meta"]["roomId"] for row in await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)}
    swept |= {default_room_id(owner_id), messaging_repo.feed_room_id(owner_id)}
    for room_id in swept:
        await _remove_human_seat(owner_id, room_id, user_id)
    # Only now, with the stream off: a feed read in the window above found the marker held.
    try:
        await messaging_repo.release_autojoin(messaging_repo.feed_room_id(owner_id), user_id)
    except Exception:
        logger.exception("feed autojoin release failed owner=%s user=%s", owner_id, user_id)
    if own_session_chats:
        # Their sessions' chats go silent for everyone: the person they were about is gone. After
        # the stream is off, which the chat door reads once it has seated someone: a seat that
        # lands before this list is removed here, one that lands after it is refused there.
        from core.services import session_chat  # lazy: it imports this module

        for session_id in own_session_chats:
            await session_chat.silence(owner_id, session_id, keep=None)


async def list_messages(
    auth: AuthContext, room_id: str, after_seq: int, limit: int, before_seq: int | None = None
) -> MessagePage:
    await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    try:
        if before_seq is None:
            rows, next_after = await messaging_repo.list_messages(room_id, after_seq, limit)
            return MessagePage(messages=_listable(rows), next_after_seq=next_after)
        rows, next_before = await messaging_repo.list_messages_before_page(room_id, before_seq, limit)
    except ValueError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    return MessagePage(messages=_listable(rows), next_after_seq=None, next_before_seq=next_before)


def _listable(rows: list[dict]) -> list[CanonicalMessage]:
    """A page of messages; a row even the tolerant read cannot show (core fields broken) is skipped and logged,
    never a failed room."""
    out = []
    for row in rows:
        try:
            out.append(canonical_message(row))
        except (AttributeError, KeyError, TypeError, ValueError):  # ValidationError is a ValueError
            logger.warning(
                "stored message unreadable, skipped: room=%s seq=%s", row.get("roomId"), row.get("messageSeq")
            )
    return out


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


def _noted_shown(meta: dict, member: dict, rows: list[dict]) -> list[dict]:
    """The noted rows this member may read. A channel's are everyone's. In a DM or group chat only what was noted since
    they joined: a DM someone was added to keeps its private notes from them (founder ruling, 6 Nov 2026)."""
    joined = member.get("joinedAt")
    if meta.get("kind") == "channel" or not joined:
        return rows
    since = datetime.fromisoformat(joined)
    return [r for r in rows if datetime.fromisoformat(r["notedAt"]) >= since]


async def list_room_memory(auth: AuthContext, room_id: str) -> RoomMemoryResponse:
    """What Lightbulb noted in this room (the Router's `note` decisions): any member reads it (W15-5)."""
    meta, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    rows = _noted_shown(meta, member, await messaging_repo.list_noted(room_id))
    return RoomMemoryResponse(
        items=[NotedFact(message_id=r["messageId"], fact=r["fact"], noted_at=r["notedAt"]) for r in rows]
    )


async def forget_room_memory(auth: AuthContext, room_id: str, message_id: str) -> None:
    """Drop a noted fact from the list: a channel's owner or admin, like Instructions; any member of a DM or group chat
    (founder ruling, 6 Nov 2026), and only a fact they can see. Idempotent."""
    owner_id = resolve_owner_id(auth)
    meta, member = await require_room_member(owner_id, auth.user_id, room_id)
    message_id = validate_uuid(message_id, label="message ID")
    room_admin = member.get("role") in {"owner", "admin"}
    # A chat's members forget only while it is a chat with Lightbulb (as Pause and Instructions): once Lightbulb is
    # removed it is a people-only chat again, back to its owner's say.
    if not room_admin and (meta.get("kind") == "channel" or not await _router_room(meta, owner_id)):
        raise ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    if meta.get("kind") != "channel":
        shown = _noted_shown(meta, member, await messaging_repo.list_noted(room_id))
        if not any(r["messageId"] == message_id for r in shown):
            return  # gone, or noted before they joined: nothing of theirs to forget
    await messaging_repo.delete_noted(room_id, message_id)


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
PART_INSTRUCTIONS = (
    "The latest message was spoken in a call and may ask several AI teammates for different things in "
    "one breath. It was cut into parts at the names. For the one part given, decide who the speaker wants "
    "to act on it or answer it. A name can come before or after its request: 'Forge, check the deploy' "
    "and 'Can you check the deploy, Forge?' both ask Forge. 'Forge and Friday, check the deploy': the "
    "part 'check the deploy' is asked of both. A part that only gives background, greets, thanks someone, "
    "or is said to a person in the room is asked of nobody."
)
#: Conversation understanding §4.2: what one part of a spoken line is. The keys are the contract with
#: voice_calls.route_live_line; the descriptions are tuned on scripts/speak_gate_bench/understand.py.
KIND_INSTRUCTIONS = (
    "The latest message was spoken on a voice call that people share with a team of AI teammates. It was "
    "cut into parts. For the one part given, decide what it is, reading it in the whole message and the "
    "conversation before it."
)
KINDS = {
    "request": (
        "Asks the AI team to find out, check, answer, do, make, send or change something, directly or "
        "indirectly ('what is ...', 'I need to know ...', 'can someone ...', 'Leo needs the ... by Friday'), "
        "or is a plain instruction ('set up their access', 'rerun it')"
    ),
    "context": (
        "Background only: a fact or a detail for a request made in ANOTHER part of the message; it holds no "
        "instruction and no question itself"
    ),
    "more": (
        "Only says that requests or questions are coming, or how the speaker wants to go through them "
        "('I have a few questions', 'let me say them all first', 'couple of things')"
    ),
    "social": (
        "Greeting, thanks, acknowledgement, reaction, thinking aloud, a passing remark or complaint, a "
        "question about the call itself ('can you hear me'), a complaint about the call or about how a "
        "teammate is talking ('can you not talk over me'), or a fragment or false start with no clear ask "
        "('sorry, um, do'); nothing for the team to look up or do"
    ),
    "people": (
        "Said to another person on the call, or to someone in the speaker's own room who is not on the call "
        "('Uber is a business, right?' to a colleague beside them), or people settling something among "
        "themselves; not for the AI team"
    ),
    # A stop is one of two things, and only the first ends work. Not yet run on
    # scripts/speak_gate_bench/understand.py: a new choice moves the others' probabilities.
    "cancel": (
        "Withdraws something the team was asked earlier: the work is no longer wanted, or not now ('forget "
        "the deck', 'never mind', 'cancel that', 'don't bother', 'hold off on that')"
    ),
    "hush": (
        "Tells a teammate to stop talking, be quiet or wait its turn, and withdraws nothing it was asked "
        "('stop', 'that's enough', 'not now', 'okay okay', 'stop talking about the deck')"
    ),
    # Driven voice: "Friday, go ahead" read as a plain instruction and became a run of its own. Not yet
    # run on scripts/speak_gate_bench/understand.py: a new choice moves the others' probabilities.
    "nudge": (
        "Only calls on a teammate, checks that it is there, or tells it to go on with or report on what it "
        "was ALREADY asked ('Friday?', 'go ahead', 'are you there', 'so, what did you find', 'any update'); "
        "it names no new thing to find out or do"
    ),
}
WHO_PART_INSTRUCTIONS = (
    "The latest message was spoken on a voice call with a team of AI teammates, and was cut into parts. For "
    "the one part given, pick the teammate whose job it is: the one who owns this kind of work, judged by "
    "what each teammate does and the tools they have. Do not pick by who spoke last or who is named "
    "elsewhere in the message. Pick unsure when the part is not work for anyone, or no teammate clearly "
    "owns it."
)
WHO_UNSURE = "No single teammate clearly owns this, or it is not work for anyone"
AMENDS_INSTRUCTIONS = (
    "Earlier requests on this call are listed. Does the latest message continue one of them: correct it, "
    "narrow it, add detail to it, or ask a follow-up about its answer? Pick that request. Pick none when "
    "the message asks for something new or is not about any of them."
)
#: Which request a stop is about, whatever kind of stop it is: ``KINDS`` says whether the work is withdrawn
#: (cancel) or only the talk about it is to stop (hush).
DROPS_INSTRUCTIONS = (
    "Earlier requests on this call are listed. Does the latest message call one of them off (forget it, "
    "hold off, no longer needed, already handled), or tell a teammate to stop talking about one of them? "
    "Pick that request. Pick none when the message is about none of them."
)
#: The same question about one part of a line that has several. Each part's stop keeps its own request:
#: "Stop talking about the deck. Cancel the invoice task." silences one and withdraws the other.
PART_DROPS_INSTRUCTIONS = (
    "Earlier requests on this call are listed. Does this part of the latest message call one of them off "
    "(forget it, hold off, no longer needed, already handled), or tell a teammate to stop talking about one "
    "of them? Pick that request. Pick none when this part is about none of them, even if another part is."
)
#: More parts than this and nobody is cut: each agent gets the whole line.
MAX_PARTS = 8
#: A Live line asks Jev a second time when the first answer is this late, and takes whichever comes first:
#: Jev's median is ~0.5 s but on a slow day one call in seven takes over 2 s (measured 2026-10-04).
HEDGE_AFTER_S = 0.7


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


_EDGE = " \t\r\n.,;:?!\"'“”‘’-–—"
#: Filler a speaker puts in front of a request ("ok so ...", "um ...").
_HEAD = re.compile(
    r"^(?:and|then|also|plus|but|so|oh|um|uh|hey|hi|ok|okay|alright|anyway|anyways|like|you\s+know|i\s+mean)"
    r"(?:\s|,|$)",
    re.I,
)
#: A connector left hanging where a part runs straight into a name ("check the deploy and | Friday ...").
_TAIL = re.compile(r"(?:^|\s)(?:and|then|also|plus|hey|hi|ok|okay)$", re.I)
#: A sentence ends at . ? ! ; followed by a space or the end: "v2.1" and "3.5 percent" are one sentence.
#: An ellipsis is a speaker trailing off, not an end: "Can you tell me... What the plan costs?" was cut in
#: two and became two requests for two agents (Meet run 2026-10-07).
_ELLIPSIS = r"(?:\.\.|…)"
_SENTENCE = re.compile(rf".+?(?:(?<![.…])(?!{_ELLIPSIS})[.?!;]+(?=\s|$)|$)", re.S)
# Conversation understanding §4.2: a new request begins inside a sentence ("... and what PRs are open",
# ", then tell me ...", "um first whats our burn", "good morning um can you check ..."). Speech-to-text
# often gives no punctuation, so the cut reads words: after a comma, a joiner, a filler or an ordinal, the
# next words must open a request (a question word, "I need / can you / someone", or an asking verb with
# its object); "can you" and "I need" open one after anything.
_ASK_STRONG = (
    r"(?:(?:can|could|would)\s+(?:you|someone|somebody|one\s+of\s+you)|i\s+(?:also\s+)?(?:need|want)|"
    r"i['’]d\s+(?:like|love)|i\s+would\s+like)\b"
)
_ASK_Q = (
    r"(?:what|what['’]?s|when|when['’]s|where|where['’]s|who|who['’]s|whoever(?:['’]s)?|why|how|how['’]s|which|"
    r"whether|is|are|was|were|did|does|do|has|have|can|could|would|will|should|"
    r"i\s+(?:also\s+)?(?:need|want)|i['’]d\s+(?:like|love)|i\s+would\s+like|we\s+need|"
    r"someone|somebody|anyone|anybody|one\s+of\s+you)\b"
)
# "send it to Dana" after "export the list and then" leans on what came before: it is the same request,
# so a pronoun object (it, them, those) opens no new one.
# An instruction after a joiner: an asking verb with its object, or ANY word followed by a determiner
# ("and rotate the key", "plus send the email": no verb list covers what people say). A pronoun object
# ("send it to Dana") leans on the step before, and "let me know ..." is a tail: neither opens a request.
_NOT_A_VERB = (
    r"(?:all|both|half|even|only|just|then|now|maybe|not|also|is|was|are|were|be|in|on|at|for|with|to|from|of|by|"
    r"about|after|before|like|than|as|if|when|while|because|since|that|what|how|why|where|who|which|whether|it|"
    r"they|we|he|she|you|i|there|this|these|those|the|a|an|and|or|but|so|then|me|us|them|him|her|"
    r"um|uh|well|please|know|mean|okay|ok|alright|oh|anyway|anyways|actually|sorry|yeah|yes|no)"
)
_ASK_V = (
    # verbs that are only ever an instruction here take any object ("and pull signups by country")
    r"(?:(?:check|pull|send|draft|write|fix|find|get|give|tell|show|grab|fetch|remind|schedule|book|summarize|"
    r"compare|merge|rerun|restart|deploy|translate|figure)\s+(?!(?:it|them|those|these|him|her)\b)[a-z0-9]"
    # verbs that are also nouns need an object word after them ("and email the list", not "email open numbers")
    r"|(?:see|make|build|create|look|run|list|update|move|close|ask|set|add|open|review|post|share|call|ping|"
    r"cancel|confirm|verify|forward|file|track|bring|reply|email)"
    r"\s+(?:the|a|an|my|our|your|their|his|her|its|me|us|this|that|if|whether|what|"
    r"what['’]s|which|who|how|why|when|where|out|up|to|for|on|in|into|with|about|at|over|through|all|any|every|"
    r"everyone|some)\b"
    rf"|(?!{_NOT_A_VERB}\b)[a-z][a-z'’-]+\s+(?:the|a|an|my|our|your|their|this|that)\b)"
)
_JOINER = r"(?:and|then|also|plus|but|anyway|anyways|btw|by\s+the\s+way|okay|ok|alright|oh|so(?!\s+that\b))"
_ORDINAL = (
    r"(?:(?:the\s+)?(?:first|second|third|next|last|final)(?:\s+(?:one|thing|up|question))?(?:\s+is)?|"
    r"lastly|finally|one\s+more(?:\s+thing)?)"
)
_FILL_WORD = r"(?:um|uh|like|please|also|then|well|just|you\s+know|i\s+mean|while\s+you['’]re\s+at\s+it)"
_FILLER = rf"(?:{_FILL_WORD}[\s,]+)*"
_CUE = re.compile(
    rf"(?:\s*,\s*(?:{_JOINER}\s+)*|\s+(?:{_JOINER}\s+)+|\s+(?={_FILLER}{_ORDINAL}\b)|\s+(?={_FILL_WORD}[\s,])|"
    rf"\s+(?={_ASK_STRONG}))"
    rf"(?={_FILLER}(?:{_ORDINAL}\s*,?\s+)?{_FILLER}(?:{_ASK_Q}|{_ASK_V}))",
    re.I,
)
#: Words that only open a request ("okay can you also | rerun the job"): never a part of their own.
_OPENER_ONLY = re.compile(
    r"^(?:(?:can|could|would|will)\s+(?:you|someone|somebody|we|one\s+of\s+you)|i\s+(?:also\s+)?(?:need|want)"
    r"(?:\s+you)?(?:\s+to)?|i['’]d\s+(?:like|love)(?:\s+you)?(?:\s+to)?|please|let['’]?s)?"
    r"(?:\s*(?:also|just|please|to))*$",
    re.I,
)
#: A part that opens the way a request does (a question word, "can you", an instruction).
_OPENS_ASK = re.compile(rf"^{_FILLER}(?:{_ASK_STRONG}|{_ASK_Q}|{_ASK_V})", re.I)
#: "tell Priya", "remind the team that": what follows is what to tell them, not a new request.
_REPORTING = re.compile(
    r"\b(?:tell|ask|remind|message|email|text|ping|inform|let)\s+\S+(?:\s+\S+)?(?:\s+(?:that|know))?$", re.I
)
#: Words that only say who is asked, with no ask yet ("whoever handles billing", "someone on the team").
_ADDRESSEE_ONLY = re.compile(
    r"^(?:someone|somebody|anyone|anybody|whoever(?:['’]s)?|one\s+of\s+(?:you|the\s+agents))\b(?:\s+\S+){0,4}$", re.I
)
#: A part the speaker hands to nobody in particular ("... and someone check the queue"): never its named agent's.
_ANYONE = re.compile(r"\b(?:someone|somebody|anyone|anybody|whoever(?:['’]s)?|one\s+of\s+(?:you|the\s+agents))\b", re.I)


def _name_hits(text: str, cand: dict) -> list[re.Match]:
    """Every place the candidate's name stands in ``text``, as said: a live fuzzy candidate's ``pos`` is a
    token index (x1000), not a character offset."""
    return list(re.finditer(rf"(?<![\w-]){re.escape(cand['said'])}(?![\w-])", text, re.I))


def _named_at(text: str, cand: dict) -> list[tuple[int, int]]:
    """Where the candidate's name stands as a NAME. A possessive ("Forge's PR") and a weekday used as a day
    ("by Friday", "last Friday's numbers") are words, not a name."""
    days = (
        [m.span() for m in re.finditer(_DAY_USE.format(day=re.escape(cand["said"])), text, re.I)]
        if cand["name"] in WEEKDAYS
        else []
    )
    return [
        h.span()
        for h in _name_hits(text, cand)
        if not any(s <= h.start() < e for s, e in days) and not re.match(r"['’]s\b", text[h.end() :], re.I)
    ]


def _clean(seg: str, into_name: bool = False) -> str:
    """The words of a part: no edge punctuation, no filler in front ("ok so", "um"), and no connector left
    hanging where the part ran straight ``into_name`` ("check the deploy and")."""
    seg = seg.strip(_EDGE)
    while (m := _HEAD.match(seg)) is not None:
        seg = seg[m.end() :].strip(_EDGE)
    while into_name and (m := _TAIL.search(seg)) is not None:
        seg = seg[: m.start()].strip(_EDGE)
    return seg


_CLAUSE_START = re.compile(r"(?:^\s*|[.?!;]\s+|,\s+|\b(?:and|hey|hi|ok|okay|so)\s+)$", re.I)


def _cue_spans(seg: str) -> list[tuple[int, int]]:
    """One sentence cut where a new request begins (understanding §4.2); a cut that would leave only filler
    in front of it is not made."""
    cuts = [0]
    for m in _CUE.finditer(seg):
        before = _clean(seg[cuts[-1] : m.start()])
        # no part of its own: filler and "can you", or only who is asked ("whoever handles billing | can you ...")
        if _OPENER_ONLY.match(before) or _ADDRESSEE_ONLY.match(before):
            continue
        # the speaker trailed off and went on ("can you tell me... um what it costs"): still one request
        if re.search(rf"{_ELLIPSIS}\s*$", seg[cuts[-1] : m.start()]):
            continue
        # the words to pass on to someone are the same request ("tell Priya | I need the board deck")
        if not m.group(0).strip() and _REPORTING.search(before):
            continue
        cuts.append(m.start())
    return list(zip(cuts, [*cuts[1:], len(seg)]))


def _between_names(pieces: list[tuple]) -> list[tuple]:
    """``_pieces`` with the parts between two names (or a name and an end) merged into one part, re-numbered."""
    out: list[tuple] = []
    for piece in pieces:
        if piece[0] == "part" and out and out[-1][0] == "part":
            out[-1] = ("part", out[-1][1], piece[2], out[-1][3], piece[4])
        else:
            out.append(piece)
    n = iter(range(len(out)))
    return [(k, a, b, next(n) if k == "part" else ref, into) for k, a, b, ref, into in out]


def _pieces(text: str, cands: list[dict], cues: bool = False) -> list[tuple[str, int, int, str | int, bool]]:
    """A Live line in order (conversation engine §3.2): ``("name", start, end, candidate name, False)``
    where a name stands, and ``("part", start, end, index, into_name)`` for the words between names and
    sentence ends that are more than filler; ``into_name``: the words run straight into the next name.
    One request is made of whole parts. ``cues``: a sentence is also cut where a new request begins."""
    names, last = [], 0
    for start, end, name in sorted(
        (start, end, c["name"])
        for c in cands
        for start, end in _named_at(text, c)
        # A word only heard as a name ("last" for Atlas, "forget" for Forge) cuts the line only where a
        # name would open a clause; inside one ("what our CAC was last month") it is a word.
        if not cues or c["said"].lower() == c["name"] or _CLAUSE_START.search(text[:start])
    ):
        if start >= last:  # two candidates on the same words: the first one
            names.append((start, end, name))
            last = end
    out: list[tuple[str, int, int, str | int, bool]] = []
    cursor, parts = 0, 0
    for start, end, name in [*names, (len(text), len(text), "")]:
        for m in _SENTENCE.finditer(text[cursor:start]):
            spans = _cue_spans(m.group(0)) if cues else [(0, len(m.group(0)))]
            for a, b in spans:
                last_span = b == len(m.group(0))
                into_name = (
                    last_span and bool(name) and cursor + m.end() == start and not re.search(r"[.?!;]\s*$", m.group(0))
                )
                if _clean(m.group(0)[a:b], into_name):
                    out.append(("part", cursor + m.start() + a, cursor + m.start() + b, parts, into_name))
                    parts += 1
        if name:
            out.append(("name", start, end, name, False))
        cursor = end
    return out


def _part_choices(names: list[str]) -> dict[str, tuple[str, set[str]]]:
    """Who one part can be asked of: ``{choice key: (description, names)}``. Each name alone, every pair
    when three are named, all of them, or nobody.
    ponytail: with 4+ names there are no subsets but "everyone"; add booleans per name if that is needed."""
    from itertools import combinations

    # "Foo-Bar" and "Foo Bar" are two agents: a slug that is taken gets its position appended.
    slug: dict[str, str] = {}
    for i, n in enumerate(names):
        s = re.sub(r"\W+", "_", n)
        slug[n] = s if s not in slug.values() else f"{s}_{i}"
    key = lambda group: "_and_".join(slug[n] for n in group)  # noqa: E731
    out = {f"only_{key([n])}": (f"Asked of {n.title()} only", {n}) for n in names}
    if len(names) == 3:
        out |= {
            key(g): (f"Asked of {g[0].title()} and {g[1].title()} together", set(g)) for g in combinations(names, 2)
        }
    out["everyone"] = ("Asked of all of them together: " + ", ".join(n.title() for n in names), set(names))
    out["nobody"] = ("Not a request to any of them: background, small talk, or said to a person", set())
    return out


def _task(text: str, pieces: list, mine: set[int], asked_names: set[str]) -> str:
    """One agent's task in the speaker's own words. Its parts that follow one another, with only names
    between them, are one stretch of the line, names included ("get Gauge to pull the numbers"). Another
    agent's part ends the stretch; a name after its last part is not in it ("fix the build. | Leo, ...")
    unless the words run straight into a talked-about name ("review the proposal with Gauge").
    A name that is only talked about and stands right before its first part is ("Gauge broke CI, fix it").
    Stretches that are apart are joined."""
    runs: list[str] = []
    run: list | None = None  # [start, end, last part ran into a name]
    lead: int | None = None  # a talked-about name with no part of mine before it yet
    for kind, start, end, ref, into_name in pieces:
        if kind == "name":
            if run is None:
                lead = (start if lead is None else lead) if ref not in asked_names else None
            elif ref not in asked_names and re.search(r"\w\s*$", text[run[0] : start]) and run[1] >= start - 1:
                # the words run straight into a talked-about name, no comma: it is their object
                # ("review the proposal with Gauge."), not a person being called (", Leo, ...")
                run[1], run[2] = end, False
            continue
        if ref in mine:
            run = [run[0] if run else (lead if lead is not None else start), end, into_name]
        elif run:
            runs.append(_clean(text[run[0] : run[1]], run[2]))
            run = None
        lead = None
    if run:
        runs.append(_clean(text[run[0] : run[1]], run[2]))
    return ". ".join(r for r in runs if r)


def _with_relayers(pieces: list, asked: list[set[str]], labels: dict[str, str]) -> list[set[str]]:
    """A part asked only of relayed agents is also the relayer's: the addressed agent named last before it.
    "Pulse, get Gauge to pull the numbers": "to pull the numbers" is Gauge's, and Pulse must hear it too."""
    out, relayer = [set(names) for names in asked], None
    for kind, _, _, ref, _ in pieces:
        if kind == "name":
            relayer = ref if labels.get(ref) == "addressed" else relayer
        elif relayer and ref < len(out) and out[ref] and all(labels.get(n) == "relayed" for n in out[ref]):
            out[ref].add(relayer)
    return out


def _opens_clause(text: str, cand: dict) -> bool:
    """The name, standing as a name, opens a clause: at the start of the line, after . ? ! ; or after
    ", " / "and ". ("..., Friday, a summary" and "What's the plan, Friday?" do; "the Friday release" does not.)"""
    return any(
        re.search(r"(?:^\s*|[.?!;]\s+|,\s+|\band\s+)$", text[:start], re.I) for start, _ in _named_at(text, cand)
    )


def _fast_path(text: str, cands: list[dict], live: bool) -> str | None:
    """One agent named, at the opening, in a finished message of 4+ words: addressed with no call."""
    if len(cands) != 1 or len(cands[0]["agents"]) != 1 or cands[0]["humans"] or cands[0]["name"] in WEEKDAYS:
        return None
    if len(text.split()) < 4 or (not live and not re.search(r"[.?!]\s*$", text)):
        return None
    return cands[0]["agents"][0] if _opens_with(text, cands[0]["name"]) else None


async def _decide(state: dict, questions: dict, hedge: bool) -> Any:
    """``jev.decide``; with ``hedge``, asked again when the first call is slow, first usable answer wins.
    A slow hour therefore doubles this stage's Jev calls, on the quota room routing shares; a 429 is an
    answer here (the fallback decides), never a retry."""
    from core.services import jev

    first = asyncio.ensure_future(jev.decide(state, questions))
    if not hedge:
        return await first
    tasks = {first}
    try:
        done, _ = await asyncio.wait(tasks, timeout=HEDGE_AFTER_S)
        if not done:
            put_metric("messaging.addressing.hedged")
            tasks.add(asyncio.ensure_future(jev.decide(state, questions)))
        answers = None
        for next_done in asyncio.as_completed(tasks):
            answers = await next_done
            if isinstance(answers, dict):
                break
        return answers
    finally:
        for task in tasks:
            task.cancel()


def _choice(answer: dict | None) -> tuple[str | None, float, float]:
    """A Jev choice as ``(choice, its probability, its lead over the runner-up)``."""
    answer = answer or {}
    probs = sorted((float(p) for p in (answer.get("probabilities") or {}).values()), reverse=True)
    top = float((answer.get("probabilities") or {}).get(answer.get("choice"), probs[0] if probs else 0.0))
    return answer.get("choice"), top, top - (probs[1] if len(probs) > 1 else 0.0)


async def _label(
    latest: dict,
    cands: list[dict],
    members: list[dict],
    history: list[dict],
    live: bool,
    parts: list[str] = (),
    askable: list[str] = (),
    understand: dict | None = None,
    spoken: list[str] = (),
) -> dict | None:
    """The one Jev call: ``{labels: {name: label}, complete: p, anyone: p}``, or None on any failure.
    ``understand`` (conversation understanding §4.2) adds, per spoken part, what it is and whose job it is,
    and which earlier request the message continues or calls off: ``kinds``, ``whos``, ``amends``, ``drops``.
    ``dropped``: per part of a line with several, the request that part calls off or silences."""
    from core.services import jev

    questions = {
        "complete": {"type": "boolean", "instructions": COMPLETE_INSTRUCTIONS},
        "anyone": {"type": "boolean", "instructions": ANYONE_INSTRUCTIONS},
    }
    earlier = (understand or {}).get("open") or []
    if understand is not None:
        cards = {**understand["cards"], "unsure": WHO_UNSURE}
        for i, part in enumerate(spoken):
            given = f" The part: {json.dumps(part)}."
            questions[f"k{i}"] = {"type": "choice", "instructions": KIND_INSTRUCTIONS + given, "criteria": KINDS}
            questions[f"w{i}"] = {"type": "choice", "instructions": WHO_PART_INSTRUCTIONS + given, "criteria": cards}
        if earlier:
            listed = {r["id"]: f'{r["agent"]}: "{r["task"][:160]}" ({r["status"]})' for r in earlier}
            questions["amends"] = {
                "type": "choice",
                "instructions": AMENDS_INSTRUCTIONS,
                "criteria": {**listed, "none": "Something new, or not about any of these"},
            }
            questions["drops"] = {
                "type": "choice",
                "instructions": DROPS_INSTRUCTIONS,
                "criteria": {**listed, "none": "Nothing is called off or told to stop"},
            }
            # One part: the line's answer is its answer.
            for i, part in enumerate(spoken if len(spoken) > 1 else ()):
                questions[f"d{i}"] = {
                    "type": "choice",
                    "instructions": f"{PART_DROPS_INSTRUCTIONS} The part: {json.dumps(part)}.",
                    "criteria": {**listed, "none": "This part calls nothing off and tells nobody to stop"},
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
    choices = _part_choices(list(askable)) if parts else {}
    for i, part in enumerate(parts):
        questions[f"p{i}"] = {
            "type": "choice",
            "instructions": f"{PART_INSTRUCTIONS} The part: {json.dumps(part)}.",
            "criteria": {key: description for key, (description, _) in choices.items()},
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
    if understand is not None:
        if understand.get("floor"):
            state["room"]["floor"] = understand["floor"]
        if understand.get("meeting"):
            state["room"]["setting"] = jev.MEETING_CONTEXT
        if earlier:
            state["earlierRequests"] = [f'{r["agent"]}: "{r["task"][:160]}" ({r["status"]})' for r in earlier]
    try:
        answers = await asyncio.wait_for(_decide(state, questions, hedge=live), ADDRESSING_TIMEOUT_S)
        out = {
            "labels": {c["name"]: answers[f"n{i}"]["choice"] for i, c in enumerate(cands)},
            "complete": float(answers["complete"]["probability"]),
            "anyone": float(answers["anyone"]["probability"]),
            # per part, the names it is asked of; an answer that is missing or unknown asks nobody
            "asked": [
                choices.get((answers.get(f"p{i}") or {}).get("choice"), ("", set()))[1] for i in range(len(parts))
            ],
        }
        if understand is not None:
            out["kinds"] = [_choice(answers.get(f"k{i}")) for i in range(len(spoken))]
            out["whos"] = [_choice(answers.get(f"w{i}")) for i in range(len(spoken))]
            out["amends"], out["drops"] = _choice(answers.get("amends")), _choice(answers.get("drops"))
            out["dropped"] = [_choice(answers[f"d{i}"]) if f"d{i}" in answers else None for i in range(len(spoken))]
        return out
    except TimeoutError:
        # The stage's own limit cancelled the call, which jev.decide reports as "cancelled" (so a hedged
        # twin is no failure): count the timeout here, for the P26 error-rate alarm.
        put_metric("messaging.jev.decision", dimensions={"outcome": "timeout"})
        return None
    except Exception:  # noqa: BLE001 -- a 429 string, a malformed answer: the fallback decides
        return None


async def address(
    latest: dict, members: list[dict], *, history: Any = (), live: bool = False, understand: dict | None = None
) -> dict:
    """Who a human's ``latest`` message speaks to. ``{agents, humans, hold, anyone, path}``:
    ``agents`` addressed or relayed (all of them respond, like several @mentions), ``humans``
    addressed, ``hold`` (Live waits for the speaker's next final), ``anyone`` (something is asked
    of the room beyond the named, so the to-people silence rule does not apply). ``history`` is the
    rows before it, or an async loader called only when the Jev call happens.
    ``understand`` (Live; conversation understanding §4.2): ``{cards, open, floor}``. The line is also cut
    where a new request begins, the same call says what each part is and whose job it is, and the answer
    gains ``parts`` (``{index, text, kind, who, asked, drops}``), ``pieces``, ``named``, ``amends`` and ``drops``."""
    text = messaging_repo.agent_text(str(latest.get("text") or ""))
    out: dict = {"agents": [], "humans": [], "hold": False, "anyone": False, "path": "none"}
    cands = address_candidates(text, members, live=live)
    mentioned = {ref.get("id") for ref in latest.get("mentions") or [] if ref.get("kind") == "agent"}
    if understand is None:
        if not cands or all(c["agents"] and set(c["agents"]) <= mentioned and not c["humans"] for c in cands):
            # Nobody named, or only @mentioned agents (addressed anyway): today's path, no call.
            return out
        if (agent := _fast_path(text, cands, live)) is not None:
            out |= {"agents": [agent], "path": "fast"}
            put_metric("messaging.addressing.stage", dimensions={"path": "fast", "surface": "live" if live else "chat"})
            return out
    # Conversation engine §3.2: a live line naming 2+ agents is cut into parts at the names, and this same
    # call says who each part is asked of.
    pieces = _pieces(text, cands, cues=understand is not None) if live else []
    if understand is not None and sum(kind == "part" for kind, *_ in pieces) > MAX_PARTS:
        # Too many parts to ask about: the sentences between two names become one part, but the names still
        # cut (10-09 prod Meet: a long turn made one part that began before "Forge, what are you working
        # on?", so the words said to Forge were read from its first words, "Fine. Um", and nothing was asked).
        pieces = _between_names(pieces)
        if sum(kind == "part" for kind, *_ in pieces) > MAX_PARTS:
            pieces = [("part", 0, len(text), 0, False)]  # too many names too: the line is one part
    said = {ref for kind, _, _, ref, _ in pieces if kind == "name"}
    askable = [c["name"] for c in cands if len(c["agents"]) == 1 and c["name"] in said]
    parts = [_clean(text[start:end], into) for kind, start, end, _, into in pieces if kind == "part"]
    multi = len(askable) >= 2
    split = multi and 0 < len(parts) <= MAX_PARTS
    result = await _label(
        latest,
        cands,
        members,
        list(await history() if callable(history) else history),
        live,
        parts if split else [],
        askable,
        understand,
        parts if understand is not None else [],
    )
    if result is None:
        # A safe fallback, not a substitute ([AX] (c): the common-word false-speaks come back). A weekday
        # that opens a clause of a multi-name live line ("..., Friday, a summary") is that teammate.
        labels = {
            c["name"]: "not_a_name"
            if c["name"] in WEEKDAYS and not (multi and _opens_clause(text, c))
            else "addressed"
            if c["agents"]
            else "mentioned"
            for c in cands
        }
        result, out["path"] = {"labels": labels, "complete": 1.0, "anyone": 0.0, "asked": []}, "fallback"
    else:
        out["path"] = "jev"
    out["hold"] = result["complete"] < HOLD_BELOW
    out["anyone"] = result["anyone"] >= 0.5
    hits, called = [], []  # agents asked; people spoken to (their names are no part of an agent's task)
    for c in cands:
        label = result["labels"][c["name"]]
        if c["name"] in WEEKDAYS and re.search(_DAY_USE.format(day=re.escape(c["said"])), text, re.I):
            # domain knowledge, not a model call: our names are weekdays. "Pull last Friday's numbers.
            # Friday, what's the plan?" still calls the teammate the second time; "the Friday release
            # slipped, we ship by Friday" does not.
            label = label if _opens_clause(text, c) else "not_a_name"
        if label in ("addressed", "relayed") and _name_hits(text, c) and not _named_at(text, c):
            label = "mentioned"  # only ever a possessive ("grab Friday's notes"): talked about, not asked
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
            called.append(c["name"])
    if live and len(hits) >= 2 and not parts:
        out["tasks"] = {}  # names and nothing else ("Forge and Friday?"): a summons, not a request for each
    elif len(hits) >= 2 and any(result["asked"]):
        # Each agent's task is its own parts, in the speaker's words. An agent with no part of its own
        # (or one word) is missing from ``tasks``: /route then gives it the whole line.
        asked_names = {c["name"] for c, _, _ in hits}
        # "Friday, ask Forge for the numbers": Friday's own part is "ask", so it needs the whole line.
        fewest = 2 if any(label == "relayed" for _, _, label in hits) else 1
        asked = _with_relayers(pieces, result["asked"], {c["name"]: label for c, _, label in hits})
        out["tasks"] = {}
        for c, agent, _ in hits:
            mine = {i for i, names in enumerate(asked) if c["name"] in names}
            task = _task(text, pieces, mine, asked_names)
            if len(task.split()) >= fewest:
                out["tasks"].setdefault(agent, task)
    if understand is not None:
        # Conversation understanding §4.2: what the one call said about each part; voice_calls assembles the requests.
        asked = result["asked"] or [None] * len(parts)
        if result["asked"] and hits:
            asked = _with_relayers(pieces, result["asked"], {c["name"]: label for c, _, label in hits})
        kinds, whos = result.get("kinds") or [], result.get("whos") or []
        dropped = result.get("dropped") or []
        # "Can you get | Forge ..." and "@priya can you and | Analytics look at churn" run into the name;
        # "check the API, and | Ledger, ..." is an ask of its own that ended before it.
        into = [
            into_name
            and (
                not re.search(r"(?:,|\b(?:and|then|also|plus|but))\s*$", text[start:end], re.I)
                or bool(_OPENER_ONLY.match(re.sub(r"^@?[\w-]+[\s,]+(?=\S+\s+\S)", "", parts[i])))
            )
            for i, (kind, start, end, _, into_name) in enumerate(p for p in pieces if p[0] == "part")
        ]
        out["parts"] = [
            {
                "index": i,
                "text": part,
                "into": into[i],  # the words run straight into a name ("Can you get | Forge ...")
                "cue": bool(_OPENS_ASK.match(part)),  # it opens the way a request does
                "kind": kinds[i] if i < len(kinds) else (None, 0.0, 0.0),
                "who": whos[i] if i < len(whos) else (None, 0.0, 0.0),
                "asked": asked[i] if i < len(asked) else None,
                "drops": dropped[i] if i < len(dropped) else None,  # the request this part calls off or silences
            }
            for i, part in enumerate(parts)
        ]
        out["pieces"] = pieces
        out["named"] = [(c["name"], agent, label) for c, agent, label in hits]
        out["called"] = called
        out["amends"], out["drops"] = result.get("amends"), result.get("drops")
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
    feed_roster: Collection[str] | None = None,
    in_thread: bool = False,
) -> list[dict]:
    """``mentions`` carries the agents a message speaks to: its @mentions plus ``address``'s agents.
    ``feed_roster`` marks the feed (its roster's agent ids): see the feed rule below."""
    mentioned = {ref["id"] for ref in mentions if ref.get("kind") == "agent"}
    if feed_roster is not None:
        # The feed: agents are authors, not members. Only a Discuss (thread) reply that @mentions
        # a roster agent wakes it, as a user_request; a plain post wakes nobody.
        if not in_thread:
            return []
        woken = sorted((mentioned & set(feed_roster)) - {source_agent_id})
        return [{"agentId": agent_id, "eventKind": "user_request"} for agent_id in woken]
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
    via: MessageVia | None = None,
    text_reply_key: str | None = None,
    author_override: dict | None = None,
    expected_room_revision: int | None = None,
    wake: bool = True,
) -> CanonicalMessage:
    """``author_override`` is the hosted MCP's client identity; ``auth`` stays the accountable human.
    ``wake=False`` stores and shows the message but dispatches no agent: an Ideas record whose work
    Paperclip already started.
    ``expected_room_revision`` fences the write: a room changed since that read (e.g. a DM turned
    into a group DM) refuses with MESSAGE_CONFLICT instead of posting to the new audience."""
    meta, sender = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    if is_feed_room(meta) and request.thread_root_message_id is None:
        # The feed's posts are the agents'; people answer them in Discuss (a thread reply).
        raise ApiProblem("ROOM_FORBIDDEN", "Reply to a feed post in Discuss; only agents post to the feed.")
    owner_id = meta["ownerId"]  # room-scoped writes are keyed on the ROOM owner, never the caller's workspace
    feed_roster = None
    router = await _router_room(meta, owner_id)
    if is_feed_room(meta):
        # Agents are the feed's authors, not members: an @mention names one on this owner's roster.
        feed_roster = {row["id"] for row in await load_agent_roster(owner_id)}
        named = [ref for ref in request.mentions if ref.kind == "agent"]
        if any(ref.id not in feed_roster for ref in named):
            raise ApiProblem("MEMBER_NOT_FOUND", "Mentioned agent is not in this workspace.")
        people = await _validate_mentions(room_id, [ref for ref in request.mentions if ref.kind != "agent"])
        mentions = sorted([*people, *({"kind": "agent", "id": ref.id} for ref in named)], key=lambda r: r["id"])
    elif router:
        # A Router room seats only Lightbulb: `@<specialist>` names a roster agent, and admission pins it on
        # the trigger (`ref.pinnedSpecialist`) for the Router.
        named = [ref for ref in request.mentions if ref.kind == "agent"]
        if named:
            roster_ids = {row["id"] for row in await load_agent_roster(owner_id)}
            if any(ref.id not in roster_ids for ref in named):
                raise ApiProblem("MEMBER_NOT_FOUND", "Mentioned agent is not in this workspace.")
        people = await _validate_mentions(room_id, [ref for ref in request.mentions if ref.kind != "agent"])
        mentions = sorted([*people, *({"kind": "agent", "id": ref.id} for ref in named)], key=lambda r: r["id"])
    else:
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
                        local_sessions.PROVIDER_NAMES.get(row["provider"], "Coding session"),
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
    author = author_override or {
        "kind": "human",
        "id": auth.user_id,
        "displayName": await human_display_name(auth.user_id),
    }
    addressing = None
    # An app's conversation is addressed to its builder (below), so nothing is routed by reading the text.
    if meta.get("kind") != "dm" and not meta.get("app") and feed_roster is None:
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
        # An app's own conversation (app_conversation) is its builder's: every person's message there is
        # addressed to it, as in a DM.
        room_kind="dm" if meta.get("app") else meta.get("kind"),
        feed_roster=feed_roster,
        in_thread=request.thread_root_message_id is not None,
    )
    if router:
        # Named specialists are not seated: they ride the stored targets as `user_request` (what admission
        # reads); nothing is woken by them, the admit row below is the only work item.
        named_ids = {m["id"] for m in mentions if m.get("kind") == "agent"} - {t["agentId"] for t in targets}
        targets += [{"agentId": agent_id, "eventKind": "user_request"} for agent_id in sorted(named_ids)]
        if any(t["eventKind"] == "user_request" for t in targets):
            targets = [t for t in targets if t["eventKind"] == "user_request"]
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
    req_hash = messaging_repo.request_hash(*hash_parts, *([session_ids] if session_ids else []))
    idemp_sk = messaging_repo.human_idemp_sk(auth.user_id, str(request.client_message_id))
    effective_targets = targets if wake else []  # what is stored is what telemetry reports
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
            dispatch_targets=effective_targets,
            source_agent_id=None,
            client_message_id=str(request.client_message_id),
            via=via,
            text_reply_key=text_reply_key,
            session_mentions=session_ids,
            session_target_labels=session_target_labels,
            attachments=attachments,
            thread_root_seq=_root_seq(links, request.thread_root_message_id),
            acting_human_id=auth.user_id if author_override else None,
            expected_room_revision=expected_room_revision,
            addressing=_stored_addressing(addressing),
            admit=router and wake,  # a Router room: one WORK#ADMIT# row instead of per-agent WORK#MESSAGE# rows
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
        _publish_message_created_soon(item)
        agent_mentions = sum(1 for ref in mentions if ref.get("kind") == "agent")
        _emit_dispatch_targets(effective_targets, room_kind, "human")
        put_metric(
            "messaging.message.created",
            dimensions={
                "author_kind": "human",
                "room_kind": room_kind,
                "has_targets": "true" if effective_targets else "false",
            },
        )
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
        if agent_mentions and not targets:
            _product_event(
                distinct_id=auth.user_id,
                event="agent_dispatch_missed",
                owner_id=owner_id,
                props={"reason": "no_targets", "room_kind": room_kind},
            )
        if agent_mentions:
            await _count_jev_miss(room_id, auth.user_id, item)
        if router and request.reply_to_message_id:
            # A reply to a task card (a Router message carrying `taskRefs`) is copied into the task's room,
            # unless this is that room. Best effort: the send has landed; a failed copy must not 500 it.
            from core import tasks_schema as ts
            from core.services import task_conversation

            card = links.get(str(request.reply_to_message_id)) or {}
            for ref in card.get("taskRefs") or []:
                if ref.get("taskId") and ts.task_room_id(ref["taskId"]) != room_id:
                    try:
                        await task_conversation.forward_card_reply(owner_id, ref["taskId"], item)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "card reply %s not forwarded to task %s", item["messageId"], ref["taskId"], exc_info=True
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
            # No purchase link: the iPhone app renders room notes as-is (App Store 3.1.1).
            text = BUDGET_BLOCK_MESSAGES[budget_reason]
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
        member = await room_agent(row["roomId"], row["agentId"])  # the feed's authors are not members
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
        await _schedule_room_run_analytics(owner_id, row)
        from core.services import meeting_notes  # a meeting's notes run owes its organizer one mail

        await meeting_notes.on_notes_run_ended(owner_id, row)
    return row


async def _schedule_room_run_analytics(owner_id: str, row: dict) -> None:
    """``agent_run_completed`` for a room run (T01), keyed on the human whose message started
    it. Called at the one close door every closer shares (bridge run.ended, Stop, sweeper), and
    only by the call that closed the row. Never raises."""
    outcome = row.get("outcome")
    if outcome not in {"completed", "error", "aborted"}:
        return
    try:
        trigger = (
            await messaging_repo.get_message_by_id(row["roomId"], row["triggerMessageId"])
            if row.get("roomId") and row.get("triggerMessageId")
            else None
        )
        author = (trigger or {}).get("author") or {}
        person = author.get("id") if author.get("kind") == "human" else None
        started_at, ended_at = row.get("startedAt"), row.get("endedAt")
        duration = _ms_between(started_at, ended_at) if started_at and ended_at else None
        run_analytics.schedule_run_completed(
            owner_id=owner_id,
            parsed={"source": "room", "agent_id": row.get("agentId"), "room_id": row.get("roomId")},
            payload={
                "runId": row.get("runId"),
                "data": {
                    "aborted": outcome == "aborted",
                    **({"durationMs": int(duration)} if duration is not None else {}),
                },
            },
            phase="error" if outcome == "error" else "end",
            person_id=person if person and person.startswith("user_") else None,
        )
    except Exception:
        logger.exception("room run analytics scheduling failed")


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


def seconds_since(iso: Any) -> int | None:
    """Whole seconds from an ISO timestamp to now (the Stop ledger's ``stopAfterS``)."""
    if not iso:
        return None
    ms = _ms_between(iso, datetime.now(timezone.utc).isoformat())
    return None if ms is None else int(ms // 1000)


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
            from core.services import text_relay

            await text_relay.signal_working(owner_id, started)
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
            from core.services import text_relay

            await text_relay.signal_working(owner_id, started)  # keeps the typing bubble up
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
            try:
                await ask_who_should_see(owner_id, row, event.outcome)
            except Exception:
                logger.exception("invite ask failed")
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


def _is_task_session(session_key: str | None) -> bool:
    """A super-agent Worker or Router run (`ts.worker_session_key`, `ts.router_session_key`): its usage has one
    writer, the activity (`end_attempt`, `router_turn`), which bills the person who asked."""
    return bool(session_key) and (":lightbulb:task:" in session_key or ":lightbulb:router:" in session_key)


async def handle_usage_report(owner_id: str, body: UsageReportRequest) -> UsageReportResponse:
    """Usage + product analytics for runs the room contract does not cover (cron, heartbeat, Paperclip)."""
    if parse_lightbulb_session_key(body.session_key) is not None and body.family != "voice":
        put_metric("messaging.usage.callback", dimensions={"outcome": "room"})
        return UsageReportResponse(run_id=body.run_id, status="ignored")
    if not await messaging_repo.claim_usage_receipt(owner_id, body.run_id):
        put_metric("messaging.usage.callback", dimensions={"outcome": "replayed"})
        return UsageReportResponse(run_id=body.run_id, status="replayed")
    usage = body.usage
    if not _is_task_session(body.session_key):  # recorded by the task/Router activity, for its requester
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


async def ask_who_should_see(owner_id: str, row: dict, outcome: str) -> None:
    """After the first completed run in #general of a one-person workspace, the CEO asks
    "Who should see this with you?" with the invite card -- once per workspace, ever.

    The marker is claimed before the Clerk read so a busy #general costs one failed
    conditional put per run afterwards; a read that cannot answer releases it to retry.
    A workspace that already has two people keeps the marker: the ask is for one person."""
    room_id = row.get("roomId")
    if outcome != "completed" or not room_id:
        return
    general = default_room_id(owner_id)
    front = await _front_agent_id(owner_id)
    # Super-agent: the first run is in the owner's Lightbulb DM. In a one-person workspace (checked below) any DM
    # with Lightbulb is that DM. The marker stays on #general's id either way: one ask per workspace.
    if room_id != general and not (front and ((await messaging_repo.get_room(room_id)) or {}).get("kind") == "dm"):
        return
    if not await messaging_repo.claim_room_marker(general, INVITE_ASK_MARKER):
        return
    org = await clerk_admin.get_organization(owner_id, include_members_count=True)
    if org is None or org.get("members_count") is None:
        await messaging_repo.release_room_marker(general, INVITE_ASK_MARKER)
        return
    if org["members_count"] != 1:
        return
    from core.repositories.workspace_repo import WorkspaceRepo

    company = await WorkspaceRepo().resolve_owner_row(owner_id)
    ceo_id = (getattr(company, "starter_agent_ids", None) or {}).get("ceo") if company else None
    if not ceo_id or await messaging_repo.get_member(room_id, "agent", ceo_id) is None:
        return  # a legacy roster or a CEO removed from #general: nobody to ask in its voice
    name = (
        FRONT_AGENT_NAME  # super-agent: every post in a people's room is Lightbulb's
        if front
        else next((a["name"] for a in await load_agent_roster(owner_id) if a["id"] == ceo_id), "Lightbulb")
    )
    text = invite_ask_text()
    await messaging_repo.create_message(
        room_id=room_id,
        owner_id=owner_id,
        idemp_pk=messaging_repo.room_pk(room_id),
        idemp_sk=messaging_repo.human_idemp_sk("__system__", f"{room_id}:{INVITE_ASK_MARKER}"),
        req_hash=messaging_repo.request_hash(text, ceo_id),
        author={"kind": "agent", "id": ceo_id, "displayName": name},
        text=text,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=ceo_id,
        client_message_id=None,
    )
    put_metric("messaging.invite_ask.posted")


async def require_agent_in_live_room(owner_id: str, room_id: str, agent_id: str) -> None:
    """The room is this owner's, not deleted or archived, and the agent is a current member --
    what the bridge needs before running a turn in that room's conversation."""
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if meta.get("archivedAt"):
        raise ApiProblem("ROOM_ARCHIVED", ROOM_ARCHIVED_MESSAGE)
    if await room_agent(room_id, agent_id, meta) is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Agent is not a current room member.")


async def _post_follow_up_cap_notice(
    owner_id: str, room_id: str, agent_id: str, human: dict | None, trigger: dict
) -> None:
    """Tell the room once per (room, agent, latest human message) that background work paused. A failure
    to post never hides the 429."""
    try:
        name = next((a["name"] for a in await load_agent_roster(owner_id) if a["id"] == agent_id), "Agent")
        meta = await messaging_repo.get_room(room_id) or {}
        thread_root = trigger.get("threadRootMessageId") or (None if meta.get("kind") == "dm" else trigger["messageId"])
        root_seq = trigger.get("threadRootSeq") if trigger.get("threadRootMessageId") else trigger["messageSeq"]
        marker = f"{room_id}:{agent_id}:followup-cap:{(human or {}).get('messageId', 'none')}"
        await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=messaging_repo.human_idemp_sk("__system__", marker),
            req_hash=messaging_repo.request_hash(FOLLOW_UP_CAP_NOTICE, agent_id),
            author={"kind": "agent", "id": agent_id, "displayName": name},
            text=FOLLOW_UP_CAP_NOTICE,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=thread_root,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=agent_id,
            client_message_id=None,
            thread_root_seq=root_seq if thread_root else None,
        )
        put_metric("messaging.followup.cap_notice")
    except Exception:
        logger.exception("follow-up cap notice failed")


async def create_follow_up(owner_id: str, request: FollowUpRequest) -> FollowUpResponse:
    """Queue a follow-up turn for the agent whose delegation finished. The plan
    gate is the consumer's (``openclaw_work._dispatch_message``), not this door's."""
    room_id, agent_id = str(request.room_id), str(request.agent_id)
    follow_up_id = str(request.follow_up_id)
    # Read-then-write is enough: openclaw_work._room_follow_up re-reads the room and drops archived ones.
    await require_agent_in_live_room(owner_id, room_id, agent_id)
    trigger = await messaging_repo.get_message_by_id(room_id, str(request.trigger_message_id))
    if trigger is None:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Trigger message not found.")
    # Replay BEFORE the cap: a retry after a lost 202 counts its own row.
    if await messaging_repo.get_follow_up(room_id, follow_up_id) is not None:
        put_metric("messaging.followup.created", dimensions={"status": "replayed"})
        return FollowUpResponse(status="replayed")
    human = await messaging_repo.latest_human_message(room_id)
    # No human message yet: count the last hour only, so a human-less room is not capped for the 7-day TTL.
    since = human["createdAt"] if human else datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    if await messaging_repo.count_follow_ups_since(room_id, agent_id, since) >= FOLLOW_UP_CAP_SINCE_HUMAN:
        await _post_follow_up_cap_notice(owner_id, room_id, agent_id, human, trigger)
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


async def create_agent_message(
    owner_id: str, request: AgentMessageRequest, *, attrs: dict | None = None, in_reply_to: str | None = None
) -> AgentMessageReceiptResponse:
    """``attrs``: super-agent message attrs by stored name (`_ATTR_FIELDS`), passed only by backend code
    (`task_conversation.post_as_lightbulb`); never part of the runtime's request body. ``in_reply_to``: the human
    message this answers when no run names its trigger (the Router's reply, a thread's answer line): the voice
    matches a spoken ask's answer by it (`agent_lines`' `inReplyTo`)."""
    late_run = request.event_id.removeprefix("live-late:") if request.event_id.startswith("live-late:") else None
    if late_run:
        # Conversation engine §3.6: "don't post that". Both late-answer doors (the worker's /late-answer
        # and the bridge's undelivered-answer POST) arrive here with live-late:<runId>. This read refuses
        # early; the same condition rides the message's transaction below, so a mark that lands while the
        # checks and attachment work in between run is still honoured.
        voice_run = await messaging_repo.get_voice_run(owner_id, late_run)
        if voice_run and voice_run.get("noPost"):
            put_metric("voice.huddle.late_answer_suppressed")
            raise ApiProblem("ACTION_UNAVAILABLE", "The Live call asked not to post this answer.")
    if attrs and attrs.get("taskRefs") is not None:
        # Strict on write: a ref the read model does not know is a bug here, not a stored surprise later.
        attrs = {**attrs, "taskRefs": [TaskRef.model_validate(r).model_dump() for r in attrs["taskRefs"]]}
    room_id = str(request.room_id)
    agent_id = str(request.agent_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("deletedAt") or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    bound = request.run_id or request.reply_to_message_id or request.thread_root_message_id
    if (
        meta.get("hidden")
        and meta.get("meeting")
        and not (bound or request.uploaded_attachments)
        # while the call is on, the meeting's Router answers in its room, where the voice says it
        and not (meta["meeting"].get("status") == "in_call" and agent_id == await _front_agent_id(owner_id))
    ):
        # A Google Meet's room (hidden, with a meeting record) is read by nobody: what an agent posts
        # there (a late Live answer) goes to the meeting's output room when it sits there. A session's
        # chat is hidden too, but people read it, so it keeps its posts. Every door resolves the same room,
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
    feed = is_feed_room({**meta, "roomId": room_id})
    router = await _router_room({**meta, "roomId": room_id}, owner_id)
    if router:
        await _require_lightbulb_voice(owner_id, meta, agent_id, request.event_id)
    if feed and request.thread_root_message_id is None and request.post_reason is None:
        # Every feed post says why (lightbulb-post.sh --why); Discuss replies need none.
        raise ApiProblem("INVALID_REQUEST", "A feed post needs postReason: why it matters.")
    member = await room_agent(room_id, agent_id, meta)
    if member is None and router and request.event_id.startswith(_VOICE_EXEMPT_EVENT_PREFIXES):
        # A Router room seats only Lightbulb: the A2A agent in the holder's DM (or a Live late answer) is not a
        # member, so the exempt post goes out as a roster stand-in, like the feed's authors.
        member = await _stand_in(owner_id, agent_id)
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
    if request.progress:
        # Progress notifies nobody: a stored mention would write a MENTION# row (the person's
        # mention badge) and ride mentionedUserIds. The @name stays in the text as plain words.
        mentions, unresolved = [], []
    # A progress line is not a turn in the conversation: no targets means no WORK rows, no Jev
    # decision, and nothing for the reply cap below to cut or announce.
    targets = (
        []
        if request.progress or router  # a Router room: Lightbulb's posts wake nobody (the Router already ran)
        else select_dispatch_targets(
            members,
            mentions,
            agent_id,
            room_kind=meta.get("kind"),
            feed_roster=set(roster) if feed else None,
            in_thread=request.thread_root_message_id is not None,
        )
    )
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
        # Only when set, so a reason-less message hashes as it always did: a changed reason under
        # one eventId is a conflict, never a silent replay of the first.
        *([request.post_reason] if request.post_reason else []),
    )
    try:
        item, replayed = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(request.event_id),
            req_hash=req_hash,
            author={
                "kind": "agent",
                "id": agent_id,
                "displayName": FRONT_AGENT_NAME
                if meta.get("frontAgentId") == agent_id or member.get("mentionHandle") == "lightbulb"
                else agent["name"],
            },
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
            in_reply_to=(trigger or {}).get("messageId") or in_reply_to,
            extra_conditions=[messaging_repo.voice_run_may_post(owner_id, late_run)] if late_run else None,
            # The projector files no Activity and sends no push for a silent row: the reply
            # that ends the run is the one that notifies.
            silent=request.progress,
            progress=request.progress,
            post_reason=request.post_reason,
            attrs=attrs or None,
            author_member_check=not member.get("standIn"),  # a roster stand-in (feed, task room) has no seat
        )
    except messaging_repo.ExtraConditionFailed as exc:  # "don't post that" landed before the commit
        put_metric("voice.huddle.late_answer_suppressed")
        raise ApiProblem("ACTION_UNAVAILABLE", "The Live call asked not to post this answer.") from exc
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
        _publish_message_created_soon(item)
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
        if not request.progress:  # time-to-reply is the real reply's, not the first progress line's
            await _emit_reply_telemetry(owner_id, room_id, agent_id, item, links, room_kind)
        if trigger and (run or {}).get("eventKind") == "room_event":
            await _emit_reply_after_human_answer(room_id, meta, trigger, item, room_kind)
        if meta.get("kind") == "call" and not request.progress:
            from routers import voice_phone

            try:  # best-effort: the answer is posted either way
                await voice_phone.after_call(room_id, item)
            except Exception:
                logger.warning("call %s: late answer not kept on the record", room_id, exc_info=True)
        if capped:
            put_metric("messaging.agent_chain.capped", dimensions={"room_kind": room_kind})
            await _post_chain_paused_notice(owner_id, item, agent["name"], (trigger or {}).get("author"))
    if trigger and trigger.get("textReplyKey") and not request.progress:
        # A member texted the line: whichever agent answered, the answer goes back by text. On a replay too: the
        # send's idempotency key makes a resend Photon's replay of the first, and a failed send answers 503 so
        # the container retries this same event, which lands here as a replay and sends again.
        from core.services import text_relay

        if await text_relay.relay_reply(owner_id, trigger, request.text) not in text_relay.RELAY_DONE:
            raise ApiProblem("TEXT_RELAY_UNAVAILABLE", "The reply is posted; texting it back failed. Retry.")
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


_write_path_tasks: set[asyncio.Task[None]] = set()


def _publish_message_created_soon(item: dict) -> None:
    """Hand the room's live event to AppSync straight from the write path, as Slack and Discord
    do: the stream gives one room one projector lane, which held a room to ~3-4 live msg/s (dev,
    2026-10-08). The projector still publishes the same eventId as the durable fallback; the
    client drops the second copy (appsyncEvents.ts ``seen``). Off the caller's frame, never
    raises; ``_send`` meters the outcome under ``source=write_path``."""

    async def publish() -> None:
        try:
            await get_live_event_publisher().publish_message_created(item)
        except Exception as exc:
            logger.warning("write-path room event failed (%s)", type(exc).__name__)

    task = asyncio.create_task(publish())
    _write_path_tasks.add(task)
    task.add_done_callback(_write_path_tasks.discard)


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
    """An agent posting OUTSIDE a room turn (``lightbulb-post.sh``).

    Target, first that applies: ``roomId`` → that room (membership checked, never
    auto-joined); ``feed`` → the workspace feed (ensured, the agent seated on ``mentions``);
    ``roomName`` → the one channel of that name the agent is in;
    ``userId``/``handle`` → that workspace member's DM with the agent; otherwise the
    workspace's #general. ``issueId`` is accepted and ignored (an older script's ``--issue``).
    ``postReason`` is the post's "Why I created this post" line (any target)."""

    event_id: str
    agent_id: uuid.UUID
    text: str
    room_id: uuid.UUID | None = None
    room_name: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    handle: str | None = Field(default=None, min_length=1, max_length=64)
    issue_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9-]+$")
    feed: bool = False
    post_reason: str | None = None

    @field_validator("post_reason")
    @classmethod
    def validate_post_reason(cls, value: str | None) -> str | None:
        return validate_post_reason(value)

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
        if sum(x is not None for x in (self.room_id, self.room_name, self.user_id, self.handle)) + self.feed > 1:
            raise ValueError("name at most one of feed, roomId, roomName, userId, handle")
        if self.feed and self.post_reason is None:
            raise ValueError("a feed post needs postReason: why it matters")
        return self


async def _agent_dm_room_id(owner_id: str, user_id: str, agent_id: str, *, meta_extra: dict | None = None) -> str:
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
        meta_extra=meta_extra,
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
        {"kind": "agent", "mentionHandle": h}
        for h in (await _handles_for(owner_id, await load_agent_roster(owner_id))).values()
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


async def resolve_agent_post_room(owner_id: str, request: AgentPostRequest) -> str:
    agent_id = str(request.agent_id)
    if request.room_id is not None:
        return str(request.room_id)
    if request.feed:
        room_id = await ensure_feed_room(owner_id)
        if room_id is None:
            raise ApiProblem("ACTION_UNAVAILABLE", "This workspace has no feed yet.")
        return room_id  # the agent writes as an author; create_agent_message checks the roster
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
    if user_id is not None:
        return await _agent_dm_room_id(owner_id, user_id, agent_id)
    room_id = default_room_id(owner_id)
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("deletedAt"):
        raise ApiProblem("ACTION_UNAVAILABLE", "This workspace has no #general; name a room or a user.")
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
            post_reason=request.post_reason,
        ),
    )
