"""Canonical multiplayer room policy and public contract models.

DynamoDB owns the ordered room log; OpenClaw owns each agent's native session.
This module is the single policy seam between authenticated humans/internal
agents and :mod:`core.repositories.messaging_repo`.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
import uuid
from pathlib import PurePath
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.repositories import container_repo, messaging_repo
from core.containers import get_workspace
from core.containers.workspace import WorkspaceError

RoomKind = Literal["channel", "dm", "group-dm"]
MemberKind = Literal["human", "agent"]
MemberRole = Literal["owner", "admin", "member"]
ListenMode = Literal["active", "mentions", "observe"]
AuthorKind = Literal["human", "agent", "system"]
EventKind = Literal["user_request", "room_event"]

MESSAGE_TEXT_MAX_BYTES = 100_000
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_COUNT = 4
ROOM_NAME_MAX_CHARS = 200
EVENT_ID_MAX_BYTES = 256
LIVE_SCHEMA_VERSION = 1

API_ERROR_STATUS = {
    "LOCAL_SESSION_FORBIDDEN": 403,
    "LOCAL_SESSION_NOT_FOUND": 404,
    "LOCAL_SESSION_CONFLICT": 409,
    "LOCAL_SESSION_UNSUPPORTED": 409,
    "LOCAL_SESSION_OFFLINE": 409,
    "INVALID_REQUEST": 400,
    "ROOM_FORBIDDEN": 403,
    "ROOM_NOT_FOUND": 404,
    "MEMBER_NOT_FOUND": 404,
    "MESSAGE_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "OPERATION_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "MESSAGE_CONFLICT": 409,
    "MEMBER_LIMIT": 409,
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
        "runtime.approval.requested",
        "runtime.approval.resolved",
        "runtime.question.requested",
        "runtime.question.resolved",
        "runtime.progress.changed",
        "runtime.resource.invalidated",
        "paperclip.event",
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


class RoomDetail(RoomSummary):
    delivery_key: uuid.UUID
    members: list[RoomMember]
    last_read_seq: int = Field(ge=0)
    created_at: datetime


class RoomListResponse(ContractModel):
    rooms: list[RoomSummary]


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
    name: str | None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_room_name(value)


class UpsertRoomMemberRequest(ContractModel):
    kind: MemberKind
    id: str = Field(min_length=1, max_length=256)
    role: Literal["admin", "member"]
    listen_mode: ListenMode | None

    @model_validator(mode="after")
    def validate_member_shape(self) -> "UpsertRoomMemberRequest":
        _validate_member_shape(self.kind, self.role, self.listen_mode)
        return self


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
    attachments: list["ImageAttachment"] = Field(default_factory=list, max_length=IMAGE_MAX_COUNT)
    reactions: dict[str, list[str]] = Field(default_factory=dict)
    reaction_revision: int = Field(default=0, ge=0)
    message_revision: int = Field(default=0, ge=0)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None


class ImageAttachment(ContractModel):
    id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=256)
    content_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    size: int = Field(gt=0, le=IMAGE_MAX_BYTES)

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


class CreateMessageRequest(ContractModel):
    client_message_id: uuid.UUID
    text: str
    mentions: list[MemberRef] = Field(default_factory=list, max_length=32)
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=IMAGE_MAX_COUNT)

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


class AgentMessageRequest(ContractModel):
    event_id: str
    room_id: uuid.UUID
    agent_id: uuid.UUID
    text: str
    reply_to_message_id: uuid.UUID | None
    thread_root_message_id: uuid.UUID | None

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return validate_event_id(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return validate_message_text(value)


class AgentMessageReceiptResponse(ContractModel):
    message_id: uuid.UUID
    room_id: uuid.UUID
    created_at: datetime


class MessagePage(ContractModel):
    messages: list[CanonicalMessage]
    next_after_seq: int | None


class ReadCursorRequest(ContractModel):
    last_read_seq: int = Field(ge=0)


class ReadCursorResponse(ContractModel):
    room_id: uuid.UUID
    last_read_seq: int = Field(ge=0)


class TypingRequest(ContractModel):
    room_id: uuid.UUID
    is_typing: bool


class AbortRunResponse(ContractModel):
    run_id: str
    state: Literal["aborting", "already-terminal"]


class RunStatusResponse(ContractModel):
    """Reconciliation read for a run's terminal state.

    ``room.run.frame`` (which carries ``run_ended``) is EPHEMERAL over the live
    channel -- unlike ``room.message.created`` it has no ``afterSeq`` gap
    recovery, so a client that reconnects mid-run (or misses a frame across a
    delivery-key rotation) can be left showing "running" forever with no other
    signal. This mirrors ``pool.resolve_run``'s own truth (also used by
    ``abort``/``steer``) so the frontend can self-heal on reconnect instead of
    requiring a page reload.
    """

    run_id: str
    state: Literal["running", "terminal"]


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


class ResolveApprovalRequest(ContractModel):
    kind: Literal["exec", "plugin"] = "exec"
    decision: Literal["allow-once", "allow-always", "deny"]


class ResolveQuestionRequest(ContractModel):
    answers: dict[str, Any] | None = None
    secret_store_allowed_hosts: list[str] | None = None
    cancel: bool | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "ResolveQuestionRequest":
        if bool(self.answers is not None) == bool(self.cancel is True):
            raise ValueError("provide answers or cancel=true")
        return self


class ResolveRuntimeResponse(ContractModel):
    id: str


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


def _uuid_hex_prefix(agent_id: str) -> str:
    return uuid.UUID(agent_id).hex[:8]


def _handle_candidate(name: str, agent_id: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    candidate = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:32]
    return candidate or f"agent-{_uuid_hex_prefix(agent_id)}"


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


_IMAGE_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/gif": ((b"GIF87a", b"GIF89a"), ".gif"),
    "image/webp": (b"RIFF", ".webp"),
}


def validate_image_bytes(content_type: str, data: bytes) -> str:
    expected = _IMAGE_SIGNATURES.get(content_type)
    if not expected or not data or len(data) > IMAGE_MAX_BYTES:
        raise ValueError("image must be PNG, JPEG, WebP, or GIF and at most 5 MiB")
    signature, suffix = expected
    signatures = signature if isinstance(signature, tuple) else (signature,)
    valid = any(data.startswith(value) for value in signatures)
    if content_type == "image/webp":
        valid = valid and len(data) >= 12 and data[8:12] == b"WEBP"
    if not valid:
        raise ValueError("image bytes do not match the declared content type")
    return suffix


def attachment_dir(room_id: str) -> str:
    return f"media/messaging/attachments/{uuid.UUID(room_id)}"


def store_image_attachment(
    owner_id: str, room_id: str, file_name: str, content_type: str, data: bytes
) -> ImageAttachment:
    suffix = validate_image_bytes(content_type, data)
    attachment_id = uuid.uuid4()
    safe_name = "".join(
        char for char in PurePath(file_name).name if ord(char) >= 0x20 and ord(char) != 0x7F and char not in "/\\"
    ).strip()
    safe_name = safe_name.encode("utf-8")[:255].decode("utf-8", "ignore") or f"image{suffix}"
    attachment = ImageAttachment(id=attachment_id, file_name=safe_name, content_type=content_type, size=len(data))
    root = attachment_dir(room_id)
    workspace = get_workspace()
    expected_root = workspace.user_path(owner_id).resolve() / root
    try:
        workspace._resolve_user_file(owner_id, f"{root}/{attachment_id}{suffix}").relative_to(expected_root)
        workspace._resolve_user_file(owner_id, f"{root}/{attachment_id}.json").relative_to(expected_root)
    except (ValueError, WorkspaceError) as exc:
        raise ValueError("attachment path escapes its room") from exc
    workspace.write_bytes(owner_id, f"{root}/{attachment_id}{suffix}", data)
    workspace.write_file(owner_id, f"{root}/{attachment_id}.json", attachment.model_dump_json(by_alias=True))
    return attachment


def load_image_attachment(owner_id: str, room_id: str, attachment_id: str) -> tuple[ImageAttachment, bytes]:
    normalized = str(uuid.UUID(attachment_id))
    root = attachment_dir(room_id)
    workspace = get_workspace()
    try:
        expected_root = workspace.user_path(owner_id).resolve() / root
        metadata_path = workspace._resolve_user_file(owner_id, f"{root}/{normalized}.json")
        metadata_path.relative_to(expected_root)
        metadata = ImageAttachment.model_validate_json(metadata_path.read_text())
        if str(metadata.id) != normalized:
            raise ValueError("attachment metadata mismatch")
        suffix = _IMAGE_SIGNATURES[metadata.content_type][1]
        path = workspace._resolve_user_file(owner_id, f"{root}/{normalized}{suffix}")
        path.relative_to(expected_root)
        with path.open("rb") as handle:
            data = handle.read(IMAGE_MAX_BYTES + 1)
    except (WorkspaceError, OSError, ValueError, KeyError) as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Image attachment not found.") from exc
    try:
        validate_image_bytes(metadata.content_type, data)
        if len(data) != metadata.size:
            raise ValueError("attachment size mismatch")
    except ValueError as exc:
        raise ApiProblem("MESSAGE_NOT_FOUND", "Image attachment is invalid.") from exc
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


async def human_display_name(user_id: str) -> str:
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


async def _validate_human_in_owner(owner_id: str, user_id: str) -> None:
    if owner_id == user_id:
        return
    if not owner_id.startswith("org_"):
        raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")
    from core.services.clerk_admin import list_user_organizations

    if not any(org.get("id") == owner_id for org in await list_user_organizations(user_id, limit=100)):
        raise ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")


async def _resolved_member(
    owner_id: str, member: CreateRoomMember | UpsertRoomMemberRequest, roster: list[dict]
) -> dict:
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
    if meta is None or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if member is None:
        raise ApiProblem(
            "ROOM_FORBIDDEN",
            "You no longer have access to this room.",
            reconcile_from_seq=int(meta.get("lastSeq", 0)),
        )
    return meta, member


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


async def _room_detail(meta: dict, user_id: str, member: dict | None = None) -> RoomDetail:
    member = member or await messaging_repo.get_member(meta["roomId"], "human", user_id)
    if member is None:
        raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.")
    members, last_read = await asyncio.gather(
        messaging_repo.list_members(meta["roomId"]),
        messaging_repo.get_read_cursor(meta["roomId"], user_id),
    )
    floor = max(last_read, int(member.get("joinedSeq", 0)))
    return RoomDetail(
        room_id=meta["roomId"],
        name=meta.get("name"),
        kind=meta["kind"],
        room_revision=int(meta["revision"]),
        last_message_seq=int(meta["lastSeq"]),
        last_message_at=meta.get("lastMessageAt"),
        unread_count=max(0, int(meta["lastSeq"]) - floor),
        updated_at=meta["updatedAt"],
        delivery_key=meta["deliveryKey"],
        members=[_room_member(row) for row in members],
        last_read_seq=last_read,
        created_at=meta["createdAt"],
    )


async def list_rooms(auth: AuthContext) -> RoomListResponse:
    owner_id = resolve_owner_id(auth)
    discovered = await messaging_repo.list_rooms_for_member(owner_id, "human", auth.user_id)
    summaries: list[RoomSummary] = []
    for row in discovered:
        meta = row["meta"]
        member = await messaging_repo.get_member(meta["roomId"], "human", auth.user_id)
        if member is None:
            continue
        last_read = await messaging_repo.get_read_cursor(meta["roomId"], auth.user_id)
        floor = max(last_read, int(member.get("joinedSeq", 0)))
        summaries.append(
            RoomSummary(
                room_id=meta["roomId"],
                name=meta.get("name"),
                kind=meta["kind"],
                room_revision=int(meta["revision"]),
                last_message_seq=int(meta["lastSeq"]),
                last_message_at=meta.get("lastMessageAt"),
                unread_count=max(0, int(meta["lastSeq"]) - floor),
                updated_at=meta["updatedAt"],
            )
        )
    summaries.sort(key=lambda room: room.updated_at, reverse=True)
    return RoomListResponse(rooms=summaries)


async def create_room(auth: AuthContext, request: CreateRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    keys = [(member.kind, member.id) for member in request.members]
    if len(keys) != len(set(keys)) or ("human", auth.user_id) in keys:
        raise ApiProblem("INVALID_REQUEST", "Room members must be unique and omit the creator.")
    if len(request.members) + 1 > messaging_repo.MAX_ROOM_MEMBERS:
        raise ApiProblem("MEMBER_LIMIT", f"Rooms hold at most {messaging_repo.MAX_ROOM_MEMBERS} members.")
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
    return await _room_detail(meta, auth.user_id)


async def get_room(auth: AuthContext, room_id: str) -> RoomDetail:
    meta, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    return await _room_detail(meta, auth.user_id, member)


async def patch_room(auth: AuthContext, room_id: str, request: PatchRoomRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    await require_room_admin(owner_id, auth.user_id, room_id)
    try:
        meta = await messaging_repo.patch_room_name(room_id, request.name)
    except messaging_repo.RoomNotFound as exc:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.") from exc
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)


async def upsert_member(auth: AuthContext, room_id: str, request: UpsertRoomMemberRequest) -> RoomDetail:
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_admin(owner_id, auth.user_id, room_id)
    existing = await messaging_repo.get_member(room_id, request.kind, request.id)
    if existing and existing.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner role cannot be changed.")
    roster = await load_agent_roster(owner_id) if request.kind == "agent" else []
    member = await _resolved_member(owner_id, request, roster)
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
    await _publish_room_metadata(meta)
    return await _room_detail(meta, auth.user_id)


async def remove_member(auth: AuthContext, room_id: str, kind: str, member_id: str) -> None:
    if kind not in {"human", "agent"}:
        raise ApiProblem("INVALID_REQUEST", "Invalid member kind.")
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_admin(owner_id, auth.user_id, room_id)
    target = await messaging_repo.get_member(room_id, kind, member_id)
    if target is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "Room member not found.")
    if target.get("role") == "owner":
        raise ApiProblem("INVALID_REQUEST", "The room owner cannot be removed.")
    await _remove_member_with_notices(meta, kind, member_id)


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
        key = await messaging_repo.ensure_user_stream(meta["ownerId"], human["id"])
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
        notices.append({"userId": human["id"], "channelKey": key, "event": event})
    return await messaging_repo.remove_member(
        meta=meta,
        kind="human",
        member_id=member_id,
        new_delivery_key=new_key,
        live_events=notices,
    )


async def revoke_human_membership(owner_id: str, user_id: str) -> None:
    """Rotate every room key before deactivating this user's private stream."""
    discovered = await messaging_repo.list_rooms_for_member(owner_id, "human", user_id)
    for row in discovered:
        room_id = row["meta"]["roomId"]
        member = await messaging_repo.get_member(room_id, "human", user_id)
        if member is None:
            continue
        meta = await messaging_repo.get_room(room_id)
        if meta is None or meta.get("ownerId") != owner_id:
            continue
        await _remove_member_with_notices(meta, "human", user_id)
    await messaging_repo.deactivate_user_stream(owner_id, user_id)


async def list_messages(auth: AuthContext, room_id: str, after_seq: int, limit: int) -> MessagePage:
    await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    try:
        rows, next_after = await messaging_repo.list_messages(room_id, after_seq, limit)
    except ValueError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    return MessagePage(messages=[canonical_message(row) for row in rows], next_after_seq=next_after)


async def update_message(
    auth: AuthContext, room_id: str, message_id: str, request: UpdateMessageRequest
) -> CanonicalMessage:
    owner_id = resolve_owner_id(auth)
    await require_room_member(owner_id, auth.user_id, room_id)
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

    owner_id = resolve_owner_id(auth)
    await require_room_member(owner_id, auth.user_id, room_id)
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
    event = make_live_event(
        "room.message.reactions.changed", message.model_dump(mode="json", by_alias=True), room_id=room_id
    )
    try:
        await get_live_event_publisher().publish_room(room_id, event)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Could not publish message reactions for room %s", room_id)
    return message


async def _validate_message_links(
    room_id: str,
    reply_to_message_id: str | None,
    thread_root_message_id: str | None,
) -> None:
    for message_id in dict.fromkeys(value for value in (reply_to_message_id, thread_root_message_id) if value):
        if await messaging_repo.get_message_by_id(room_id, message_id) is None:
            raise ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.")


def select_dispatch_targets(
    members: list[dict],
    mentions: list[dict],
    source_agent_id: str | None,
    *,
    room_kind: str | None = None,
) -> list[dict]:
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
    return [{"agentId": agent_id, "eventKind": event_kind} for agent_id, event_kind in sorted(targets.items())]


async def _validate_mentions(room_id: str, mentions: list[MemberRef]) -> list[dict]:
    normalized = sorted({(ref.kind, ref.id) for ref in mentions})
    for kind, member_id in normalized:
        if await messaging_repo.get_member(room_id, kind, member_id) is None:
            raise ApiProblem("MEMBER_NOT_FOUND", "Mentioned member is not in this room.")
    return [{"kind": kind, "id": member_id} for kind, member_id in normalized]


async def create_human_message(auth: AuthContext, room_id: str, request: CreateMessageRequest) -> CanonicalMessage:
    owner_id = resolve_owner_id(auth)
    meta, _ = await require_room_member(owner_id, auth.user_id, room_id)
    mentions = await _validate_mentions(room_id, request.mentions)
    await _validate_message_links(
        room_id,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    members = await messaging_repo.list_members(room_id)
    targets = select_dispatch_targets(members, mentions, None, room_kind=meta.get("kind"))
    attachments = [
        load_image_attachment(owner_id, room_id, str(value))[0].model_dump(mode="json", by_alias=True)
        for value in request.attachment_ids
    ]
    req_hash = messaging_repo.request_hash(
        request.text,
        mentions,
        [str(value) for value in request.attachment_ids],
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    try:
        item = await messaging_repo.create_message(
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
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "clientMessageId was already used with a different request.") from exc
    await container_repo.touch_last_interactive(owner_id)
    return canonical_message(item)


def parse_agent_mentions(text: str, members: list[dict]) -> list[dict]:
    by_handle: dict[str, list[str]] = {}
    for member in members:
        handle = member.get("mentionHandle")
        if member.get("kind") == "agent" and isinstance(handle, str) and handle:
            by_handle.setdefault(handle.lower(), []).append(member["id"])
    found: set[str] = set()
    for handle, agent_ids in by_handle.items():
        if len(agent_ids) != 1:
            continue
        if re.search(rf"(?<![A-Za-z0-9_-])@{re.escape(handle)}(?![A-Za-z0-9_-])", text, re.IGNORECASE):
            found.add(agent_ids[0])
    return [{"kind": "agent", "id": agent_id} for agent_id in sorted(found)]


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
    await _validate_message_links(
        room_id,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    members = await messaging_repo.list_members(room_id)
    mentions = parse_agent_mentions(request.text, members)
    targets = select_dispatch_targets(members, mentions, agent_id, room_kind=meta.get("kind"))
    req_hash = messaging_repo.request_hash(
        room_id,
        agent_id,
        request.text,
        str(request.reply_to_message_id) if request.reply_to_message_id else None,
        str(request.thread_root_message_id) if request.thread_root_message_id else None,
    )
    try:
        item = await messaging_repo.create_message(
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
            run_id=None,
            dispatch_targets=targets,
            source_agent_id=agent_id,
            client_message_id=None,
            attachments=[],
        )
    except messaging_repo.IdempotencyConflict as exc:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "eventId was already used with a different request.") from exc
    return AgentMessageReceiptResponse(message_id=item["messageId"], room_id=room_id, created_at=item["createdAt"])


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


async def mark_room_unread(auth: AuthContext, room_id: str) -> ReadCursorResponse:
    _, member = await require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    value = await messaging_repo.mark_unread(room_id, auth.user_id, joined_seq=int(member.get("joinedSeq", 0)))
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


def agent_message_receipt_json(receipt: AgentMessageReceiptResponse) -> dict[str, Any]:
    """Exact three-key wire body shared by POST and receipt reconciliation."""
    return receipt.model_dump(mode="json", by_alias=True)


def json_body_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
