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
from pydantic import Field, model_serializer, model_validator

from core.auth import AuthContext, resolve_owner_id
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
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    last_event_at: str | None = None
    ended_at: str | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
    # A Claude Code background job: agent knowledge, never a human listing.
    kind: Literal["interactive", "background"] = "interactive"


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


Lifecycle = Literal["turn_start", "turn_end", "turn_aborted", "compaction", "approval_pending"]


class LocalSessionEventBody(ContractModel):
    """One transcript item as the harness drew it. Every field past `truncated` is optional wire growth."""

    kind: Literal["text", "tool", "thinking", "lifecycle"]
    text: str = Field(max_length=MAX_ENVELOPE_BYTES)
    role: Literal["user", "assistant", "system", "tool"]
    truncated: bool = False
    ts: Annotated[str, Field(max_length=40)] | None = None
    item_id: OpaqueId | None = None
    parent_id: OpaqueId | None = None
    actor: EventActor | None = None
    tool: EventTool | None = None
    lifecycle: Lifecycle | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        # Old-shape events stay byte-compatible (LocalSessionEventPage cannot use
        # response_model_exclude_none -- next_after_seq's null is meaningful).
        return _drop_none(self, handler(self), info, ("ts", "item_id", "parent_id", "actor", "tool", "lifecycle"))


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


class AgentActor(ContractModel):
    kind: Literal["agent"]
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
        return _drop_none(self, handler(self), info, ("actor_user_id", "actor", "invocation_expires_at"))


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
        return AgentInvocation(
            owner, command["roomId"], actor.id, int(command["invocationExpiresAt"]), command["invocationId"]
        )
    user = command["actorUserId"]
    return AuthContext(user_id=user, org_id=owner if owner != user else None)


def _public(model, row: dict):
    """Allowlist projection: never serialize DynamoDB keys or native identity/config."""
    return model.model_validate(
        {field.alias: row[field.alias] for field in model.model_fields.values() if field.alias in row}
    )


def _session_public(row: dict) -> LocalSession:
    """One projection for every listing: ended beats lease state, an unverified
    capability set reads as none."""
    public = dict(row)
    public["latestEventSeq"] = row.get("eventSeq", 0)
    public["lastEventAt"] = iso(row.get("lastEventAt"))
    public["endedAt"] = iso(row.get("endedAt"))
    if row.get("endedAt") is not None:
        public["status"] = "ended"
    elif row["onlineUntil"] <= int(time.time()):
        public["status"] = "offline"
    if not row["capabilitiesVerified"]:
        public["capabilities"] = Capabilities().model_dump(by_alias=True)
    return _public(LocalSession, public)


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
            **{key: previous[key] for key in ("workLink", "workLinkRevision") if previous and key in previous},
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


async def authorize_session(auth: Caller, room_id: str, session_id: str) -> tuple[dict, dict]:
    """One authorization path for directory hydration, events, commands and receipts."""
    owner = caller_owner(auth)
    room_id = validate_uuid(room_id, label="room ID")
    await require_caller(auth, room_id)
    session_id = validate_uuid(session_id, label="local session ID")
    row = await repo.get_session(owner, session_id)
    if row is None or row.get("ownerId") != owner:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if await messaging_repo.get_member(room_id, "human", row["humanUserId"]) is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Session owner is no longer in this room.")
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
                public = _session_public(row)
                if public.kind == "interactive" or isinstance(auth, AgentInvocation):
                    sessions.append(public)
            except ApiProblem as exc:
                if exc.error.code not in {"LOCAL_SESSION_FORBIDDEN", "LOCAL_SESSION_NOT_FOUND"}:
                    raise
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


async def _live_public_sessions(owner: str, humans: list[str], *, include_offline: bool = False) -> list[LocalSession]:
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
            public = _session_public(row)
            if public.status in LIVE_STATUSES or (include_offline and public.status == "offline"):
                sessions.append(public)
    sessions.sort(key=lambda s: s.last_event_at or "", reverse=True)
    return sessions


async def list_workspace_sessions(auth: Caller) -> WorkspaceSessions:
    """Every live session in the workspace, plus lapsed-lease (`offline`) ones
    so a sleeping Mac reads as Offline rather than vanishing. Visibility is
    workspace-wide by owner ruling (spec §2.3); membership is not consulted."""
    owner = caller_owner(auth)
    humans = [row["userId"] for row in await messaging_repo.list_active_user_streams(owner)]
    # ponytail: one query per human per call; add an owner-keyed directory row when workspaces pass ~50 humans.
    sessions = [s for s in await _live_public_sessions(owner, humans, include_offline=True) if s.kind == "interactive"]
    return WorkspaceSessions(sessions=sessions[:WORKSPACE_SESSION_CAP])


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
        sessions = await _live_public_sessions(owner, list(names))
        # Background jobs are listed too. No `kind` key yet: the baked plugin 400s an unknown
        # session field, so it lands here only after an image whose parser accepts it is rolled.
        include = {"session_id", "provider", "cwd_name", "title", "status", "latest_event_seq", "last_event_at"}
        out = [
            {
                **s.model_dump(by_alias=True, include=include),
                "humanUserId": s.human_user_id,
                "displayName": names.get(s.human_user_id) or "Workspace member",
            }
            for s in sessions
        ]
        return out[:SESSION_SUMMARY_CAP]
    except Exception:
        logger.warning("Live session summary unavailable for room %s", room_id, exc_info=True)
        return []


async def list_events(
    auth: Caller, room_id: str, session_id: str, after_seq: int = 0, limit: int = 50
) -> LocalSessionEventPage:
    session, _ = await authorize_session(auth, room_id, session_id)
    if after_seq < 0 or after_seq >= 10**20 - 1 or not 1 <= limit <= 100:
        raise ApiProblem("INVALID_REQUEST", "Invalid event pagination.")
    rows, next_seq = await repo.list_events(session["ownerId"], session["sessionId"], after_seq, limit)
    await require_caller(auth, room_id)
    return LocalSessionEventPage(events=[_public(LocalSessionEvent, row) for row in rows], next_after_seq=next_seq)


def _require_control(row: dict, request: LocalSessionCommand) -> None:
    if row["generation"] != request.expected_generation:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session generation changed.")
    if row.get("endedAt") is not None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
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


async def submit_command(auth: Caller, room_id: str, session_id: str, request: LocalSessionCommand) -> CommandReceipt:
    owner = caller_owner(auth)
    room_id = validate_uuid(room_id, label="room ID")
    session_id = validate_uuid(session_id, label="local session ID")
    payload = request.model_dump(by_alias=True)
    author = (
        {
            "actor": {"kind": "agent", "id": auth.agent_id},
            "invocationExpiresAt": auth.expires_at,
            "invocationId": auth.invocation_id,
        }
        if isinstance(auth, AgentInvocation)
        else {"actorUserId": auth.user_id}
    )
    req_hash = messaging_repo.request_hash(
        owner,
        session_id,
        room_id,
        {"kind": "agent", "id": auth.agent_id} if isinstance(auth, AgentInvocation) else auth.user_id,
        payload,
    )
    for _ in range(6):
        row, enrollment = await authorize_session(auth, room_id, session_id)
        existing = await repo.get_command(owner, session_id, request.request_id)
        if existing:
            if existing["requestHash"] != req_hash:
                raise ApiProblem("IDEMPOTENCY_CONFLICT", "requestId was already used for a different request.")
            return _public(CommandReceipt, existing)
        _require_control(row, request)
        now = int(time.time())
        if isinstance(auth, AgentInvocation) and now >= auth.expires_at:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.")
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
            "deliverBy": min(now + 30, auth.expires_at) if isinstance(auth, AgentInvocation) else now + 30,
            "requestHash": req_hash,
        }
        try:
            await repo.admit_command(row, enrollment, receipt)
            return _public(CommandReceipt, receipt)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry the same requestId.")


async def get_command(auth: Caller, room_id: str, session_id: str, request_id: str) -> CommandReceipt:
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
    await require_caller(auth, room_id)
    return _public(CommandReceipt, row)
