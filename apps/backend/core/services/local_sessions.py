"""Human-authorized local harness directory and durable command admission.

No native transport lives here. register_session is for a future verified machine
connection; its identity arguments must come from that connection, never its payload.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from pydantic import Field, model_validator

from core.auth import AuthContext, resolve_owner_id
from core.repositories import container_repo, local_sessions_repo as repo, messaging_repo
from core.services.messaging import ApiProblem, ContractModel, require_room_member, validate_uuid

OpaqueId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
DeviceId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MAX_ENVELOPE_BYTES = 16 * 1024
# Leave room for server-authored provenance inside the transport envelope.
MAX_COMMAND_BYTES = 8 * 1024


class EnrollmentRequest(ContractModel):
    device_id: DeviceId
    cli_device_id: DeviceId | None = None
    enabled: bool


class LocalDevice(ContractModel):
    device_id: str
    human_user_id: str
    cli_device_id: str | None = None


class EnrollmentResponse(LocalDevice):
    enabled: bool


class Capabilities(ContractModel):
    send_mode: Literal["none", "queue", "immediate"] = "none"
    steer: bool = False
    interrupt: bool = False


class SessionRegistration(ContractModel):
    provider: Literal["claude", "codex"]
    profile_fingerprint: DeviceId
    native_conversation_id: OpaqueId
    generation: OpaqueId
    provider_version: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["idle", "busy", "waiting-approval", "offline"] = "offline"
    active_turn_id: OpaqueId | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)
    # The adapter must verify THIS provider version; omitted never inherits trust.
    capabilities_verified: bool = False
    online_until: int = Field(default=0, ge=0)


class LocalSession(ContractModel):
    session_id: str
    device_id: str
    human_user_id: str
    provider: Literal["claude", "codex"]
    generation: str
    status: Literal["idle", "busy", "waiting-approval", "offline"]
    active_turn_id: str | None
    capabilities: Capabilities
    capabilities_verified: bool


class RoomResources(ContractModel):
    devices: list[LocalDevice]
    sessions: list[LocalSession]


class LocalSessionEvent(ContractModel):
    seq: int = Field(ge=1)
    kind: Literal["text", "tool", "lifecycle"]
    text: str = Field(max_length=MAX_ENVELOPE_BYTES)
    role: Literal["user", "assistant", "system", "tool"]
    truncated: bool = False

    @model_validator(mode="after")
    def validate_envelope(self):
        if len(self.model_dump_json(by_alias=True).encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError("event exceeds 16 KiB; split text on UTF-8 boundaries")
        return self


class LocalSessionEventPage(ContractModel):
    events: list[LocalSessionEvent]
    next_after_seq: int | None


class LocalSessionCommand(ContractModel):
    request_id: OpaqueId
    expected_generation: OpaqueId
    kind: Literal["send", "interrupt"]
    text: str | None = Field(default=None, max_length=MAX_COMMAND_BYTES)
    expected_turn_id: OpaqueId | None = None

    @model_validator(mode="after")
    def validate_command(self):
        if self.kind == "send" and (self.text is None or not self.text.strip()):
            raise ValueError("send requires text")
        if self.kind == "interrupt" and self.text is not None:
            raise ValueError("interrupt does not accept text")
        if (
            len(self.model_dump_json(by_alias=True, include=set(LocalSessionCommand.model_fields)).encode("utf-8"))
            > MAX_COMMAND_BYTES
        ):
            raise ValueError("command exceeds 8 KiB")
        return self


class CommandReceipt(LocalSessionCommand):
    session_id: str
    room_id: str
    actor_user_id: str
    command_seq: int
    state: Literal["pending", "dispatched", "applied", "rejected", "unknown"]
    created_at: int
    deliver_by: int
    held: bool = False
    provider_message_id: str | None = None


def _public(model, row: dict):
    """Allowlist projection: never serialize DynamoDB keys or native identity/config."""
    return model.model_validate(
        {field.alias: row[field.alias] for field in model.model_fields.values() if field.alias in row}
    )


async def _paired(owner_id: str, user_id: str, device_id: str, cli_device_id: str | None) -> None:
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    pairs = (container or {}).get("paired_devices", {})
    for key, client in ((device_id, "openclaw-macos"), (cli_device_id, "node-host")):
        if key is None:
            continue
        pair = pairs.get(key, {})
        if pair.get("userId") != user_id or pair.get("clientId") != client:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Paired desktop ownership is required.")


async def set_enrollment(auth: AuthContext, request: EnrollmentRequest) -> EnrollmentResponse:
    owner = resolve_owner_id(auth)
    await _paired(owner, auth.user_id, request.device_id, request.cli_device_id)
    try:
        row = await repo.put_enrollment(owner, auth.user_id, request.device_id, request.cli_device_id, request.enabled)
    except ClientError as exc:
        if not messaging_repo._is_cancelled(exc):
            raise
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Device pairing changed. Try again.") from exc
    return _public(EnrollmentResponse, row)


async def _enrolled(owner: str, human: str, device: str) -> dict:
    enrollment = await repo.get_enrollment(owner, human, device)
    if not enrollment or not enrollment.get("enabled"):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")
    await _paired(owner, human, device, enrollment.get("cliDeviceId"))
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
            **registration.model_dump(by_alias=True),
            "pk": repo.session_pk(owner_id, sid),
            "sk": "META",
            "ownerId": owner_id,
            "humanUserId": user_id,
            "deviceId": device_id,
            "sessionId": sid,
            "revision": str(uuid.uuid4()),
            "commandSeq": (previous or {}).get("commandSeq", 0),
        }
        try:
            await repo.put_session(row, previous, enrollment)
            return row
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")


async def authorize_session(auth: AuthContext, room_id: str, session_id: str) -> tuple[dict, dict]:
    """One authorization path for directory hydration, events, commands and receipts."""
    owner = resolve_owner_id(auth)
    room_id = validate_uuid(room_id, label="room ID")
    await require_room_member(owner, auth.user_id, room_id)
    session_id = validate_uuid(session_id, label="local session ID")
    row = await repo.get_session(owner, session_id)
    if row is None or row.get("ownerId") != owner:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if await messaging_repo.get_member(room_id, "human", row["humanUserId"]) is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Session owner is no longer in this room.")
    return row, await _enrolled(owner, row["humanUserId"], row["deviceId"])


async def list_resources(auth: AuthContext, room_id: str) -> RoomResources:
    owner = resolve_owner_id(auth)
    room_id = validate_uuid(room_id, label="room ID")
    await require_room_member(owner, auth.user_id, room_id)
    devices, sessions = [], []
    # ponytail: bounded room membership, per-human directory reads; batch only if measured slow.
    for member in await messaging_repo.list_members(room_id):
        if member.get("kind") != "human":
            continue
        human = member["id"]
        for enrollment in await repo.list_enrollments(owner, human):
            try:
                await require_room_member(owner, auth.user_id, room_id)
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
                public = dict(row)
                if row["onlineUntil"] <= int(time.time()):
                    public["status"] = "offline"
                if not row["capabilitiesVerified"]:
                    public["capabilities"] = Capabilities().model_dump(by_alias=True)
                sessions.append(_public(LocalSession, public))
            except ApiProblem as exc:
                if exc.error.code not in {"LOCAL_SESSION_FORBIDDEN", "LOCAL_SESSION_NOT_FOUND"}:
                    raise
    return RoomResources(devices=devices, sessions=sessions)


async def list_events(
    auth: AuthContext, room_id: str, session_id: str, after_seq: int = 0, limit: int = 50
) -> LocalSessionEventPage:
    session, _ = await authorize_session(auth, room_id, session_id)
    if after_seq < 0 or after_seq >= 10**20 - 1 or not 1 <= limit <= 100:
        raise ApiProblem("INVALID_REQUEST", "Invalid event pagination.")
    rows, next_seq = await repo.list_events(session["ownerId"], session["sessionId"], after_seq, limit)
    return LocalSessionEventPage(events=[_public(LocalSessionEvent, row) for row in rows], next_after_seq=next_seq)


def _require_control(row: dict, request: LocalSessionCommand) -> None:
    if row["generation"] != request.expected_generation:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session generation changed.")
    if row["status"] == "offline" or row["onlineUntil"] <= int(time.time()):
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session is offline.")
    caps = row["capabilities"]
    if not row["capabilitiesVerified"]:
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Provider compatibility has not been verified.")
    if request.kind == "interrupt" and not caps["interrupt"]:
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider cannot interrupt.")
    if request.kind == "send" and caps["sendMode"] == "none":
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider cannot accept input.")
    if request.expected_turn_id is not None:
        if request.expected_turn_id != row.get("activeTurnId"):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The active turn changed.")
        if request.kind == "send" and not caps["steer"]:
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This provider supports queued input only.")
    elif request.kind == "interrupt" or (
        request.kind == "send" and row["status"] == "busy" and caps["sendMode"] == "immediate"
    ):
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "An active turn ID is required.")


async def submit_command(
    auth: AuthContext, room_id: str, session_id: str, request: LocalSessionCommand
) -> CommandReceipt:
    owner = resolve_owner_id(auth)
    room_id = validate_uuid(room_id, label="room ID")
    session_id = validate_uuid(session_id, label="local session ID")
    payload = request.model_dump(by_alias=True)
    req_hash = messaging_repo.request_hash(owner, session_id, room_id, auth.user_id, payload)
    for _ in range(6):
        row, enrollment = await authorize_session(auth, room_id, session_id)
        existing = await repo.get_command(owner, session_id, request.request_id)
        if existing:
            if existing["requestHash"] != req_hash:
                raise ApiProblem("IDEMPOTENCY_CONFLICT", "requestId was already used for a different request.")
            return _public(CommandReceipt, existing)
        _require_control(row, request)
        now = int(time.time())
        receipt = {
            **payload,
            "pk": repo.session_pk(owner, session_id),
            "sk": f"COMMAND#{request.request_id}",
            "sessionId": session_id,
            "roomId": room_id,
            "actorUserId": auth.user_id,
            "commandSeq": row["commandSeq"] + 1,
            "state": "pending",
            "createdAt": now,
            "deliverBy": now + 30,
            "requestHash": req_hash,
        }
        try:
            await repo.admit_command(row, enrollment, receipt)
            return _public(CommandReceipt, receipt)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry the same requestId.")


async def get_command(auth: AuthContext, room_id: str, session_id: str, request_id: str) -> CommandReceipt:
    room_id = validate_uuid(room_id, label="room ID")
    session, _ = await authorize_session(auth, room_id, session_id)
    # Reuse the command identifier contract without accepting payload-owned identity.
    from pydantic import TypeAdapter, ValidationError

    try:
        TypeAdapter(OpaqueId).validate_python(request_id)
    except ValidationError as exc:
        raise ApiProblem("INVALID_REQUEST", "Invalid request ID.") from exc
    row = await repo.get_command(session["ownerId"], session["sessionId"], request_id)
    if row is None or row["roomId"] != room_id:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Command not found in this room.")
    return _public(CommandReceipt, row)
