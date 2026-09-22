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

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem, ContractModel, make_live_event, require_room_member

CLIENT_ID = "lightbulb-local-sessions"
LIVENESS_SECONDS = 30
CLAIM_SECONDS = 5
logger = logging.getLogger(__name__)


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


class Receipt(ContractModel):
    type: Literal["local.session.receipt"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    request_id: sessions.OpaqueId
    outcome: Literal["applied", "rejected", "unknown"]
    provider_message_id: sessions.OpaqueId | None = None
    held: bool = False


_WIRE = TypeAdapter(Register | DeviceHeartbeat | Events | Receipt | Ended)


def check_envelope(body: dict) -> None:
    # Match ManagementApiClient's *actual* encoding, including non-ASCII expansion.
    if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > sessions.MAX_ENVELOPE_BYTES:
        raise ApiProblem("INVALID_REQUEST", "Local session envelope exceeds 16 KiB.")


def _revised(row: dict, **changes) -> dict:
    return {**row, **changes, "revision": str(uuid.uuid4())}


def _conflict(reason: str, **fields: object) -> ApiProblem:
    # Ids, seqs and reasons only — never frame content.
    logger.warning("local session conflict: %s", reason, extra={"reason": reason, **fields})
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed; reconcile before retrying.")


async def _actor_label(command: dict) -> dict | None:
    """Room-derived handle and name of the sender; the desktop shows it in the owner's terminal."""
    from core.services.messaging import human_emails, with_human_handles

    kind, member_id = ("agent", command["actor"]["id"]) if "actor" in command else ("human", command["actorUserId"])
    room_members = await store.list_members(command["roomId"])
    members = with_human_handles(room_members, await human_emails(room_members))
    member = next((m for m in members if m.get("kind") == kind and str(m.get("id")) == member_id), None)
    if member is None:
        return None
    return {
        "kind": kind,
        "handle": member.get("mentionHandle") or None,
        "displayName": member.get("displayName") or None,
    }


async def publish_resources_changed(owner: str, human: str, *, workspace: bool = True) -> None:
    """``workspace=False`` for transcript batches: the sidebar lists liveness, not
    content, so only register/heartbeat/ended make every member refetch it."""
    from core.services.live_events import get_live_event_publisher, publish_owner_users

    # GSI is discovery only; current membership/scope is checked before publishing.
    try:
        for candidate in await store.list_rooms_for_member(owner, "human", human):
            room = candidate["meta"]["roomId"]
            try:
                await require_room_member(owner, human, room)
            except ApiProblem:
                continue
            await get_live_event_publisher().publish_room(
                room, make_live_event("room.resources.changed", {"roomId": room}, room_id=room)
            )
    except Exception:
        # Hints are expendable; authorized HTTP polling repairs lost hints.
        logger.warning("Local session resource hint failed")
    if not workspace:
        return
    # Workspace-wide: the sidebar list is not room-scoped (spec §5). A failing room
    # publish above must not suppress this -- each hint fails independently.
    try:
        await publish_owner_users(owner, make_live_event("workspace.sessions.changed", {"humanUserId": human}))
    except Exception:
        logger.warning("Workspace session hint failed")


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
                        lastEventAt=int(time.time()),
                    )
                    # A resumed conversation re-registers under the same id: registering un-ends it.
                    row.pop("endedAt", None)
                    row.pop("endedReason", None)
                    await repo.put_session(row, previous, enrollment, extra=[self._connection_check(cid, connection)])
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
                elif isinstance(message, DeviceHeartbeat):
                    response = await self._device_heartbeat(cid, connection, enrollment, message)
                    notify_resources = False
                elif isinstance(message, Ended):
                    response = await self._ended(cid, connection, enrollment, message)
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
            c for c in await repo.list_commands(owner, sid) if c["state"] in {"dispatched", "unknown"}
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
        }

    async def _device_heartbeat(self, cid: str, connection: dict, enrollment: dict, message: DeviceHeartbeat) -> dict:
        """One frame per device renews every mirror it still owns; the rest come back rejected."""
        # ponytail: a read, a lease write and a delivery pass per listed session, capped at 24;
        # batch the reads if a device ever carries enough live mirrors for it to show.
        owner, now = connection.get("org_id") or connection["user_id"], int(time.time())
        rejected, reconcile = [], []
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
            public = ("status", "activeTurnId", "title", "cwdName", "lastAssistantMessage", "controlUnavailableReason")
            updated = _revised(
                row,
                status=item.status,
                activeTurnId=item.active_turn_id,
                controlUnavailableReason=item.control_unavailable_reason,
                onlineUntil=now + LIVENESS_SECONDS,
                workspace=item.workspace.model_dump(by_alias=True),
                **labels,
            )
            # Heartbeats may withdraw authority. A fresh registration is
            # required to restore it after runtime revalidation.
            if item.control_unavailable_reason is not None:
                updated.update(
                    capabilities=sessions.Capabilities().model_dump(by_alias=True), capabilitiesVerified=False
                )
            moved = any(row.get(field) != updated.get(field) for field in public)
            scanned = row.get("workspace") != updated.get("workspace")
            if moved:
                updated["lastEventAt"] = now
            await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection)])
            # Hint per session, never deferred to the end of the frame: a failure
            # part-way retries the whole frame, and by then the rows already written
            # match what the device sent, so a deferred hint would be dropped.
            # A workspace scan is worth a refetch but is not the user doing something,
            # so it hints without stamping lastEventAt. A lapsed lease reads as
            # offline, so recovery must reach the room even when nothing else moved;
            # renewal makes that fire once.
            if moved or scanned or row["onlineUntil"] <= now:
                await publish_resources_changed(owner, row["humanUserId"])
            # Reconcile after delivery: this heartbeat's own dispatch is part of what the journal must settle.
            await self.dispatch_session(owner, item.session_id)
            pending = await self._reconcile_for(owner, item.session_id, row["generation"])
            if pending is not None:
                reconcile.append({"sessionId": item.session_id, **pending})
        return {"type": "local.device.heartbeat.ack", "rejected": rejected, "reconcile": reconcile, "serverTime": now}

    async def _ended(self, cid: str, connection: dict, enrollment: dict, message: Ended) -> dict:
        owner = connection.get("org_id") or connection["user_id"]
        row = await repo.get_session(owner, message.session_id)
        ack = {"type": "local.session.ended.ack", "sessionId": message.session_id}
        if (
            row is None
            or row["deviceId"] != connection["device_id"]
            or row["generation"] != message.generation
            or row.get("endedAt") is not None
        ):
            return ack
        # A device clock ahead of the server cannot park a session in the future,
        # where it would outrank every live row in a lastEventAt ordering.
        ended_at = min(message.ended_at, int(time.time()))
        updated = _revised(row, status="ended", endedAt=ended_at, lastEventAt=ended_at, endedReason=message.reason)
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection)])
        await publish_resources_changed(owner, row["humanUserId"])
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
            }
        if message.after_seq != row.get("eventSeq", 0):
            raise _conflict("cursor", sid=sid, expected=row.get("eventSeq", 0), got=message.after_seq)
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
        updated = _revised(
            row,
            eventSeq=end,
            lastEventAt=now // 1000,
            nextTextAt=now + 500 if has_text else row.get("nextTextAt", 0),
            **({"nextHintAt": now + 1000} if hint else {}),
        )
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection), *updates])
        if hint:
            await publish_resources_changed(row["ownerId"], row["humanUserId"], workspace=False)
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
        return {
            "type": "local.session.receipt.ack",
            "sessionId": row["sessionId"],
            "requestId": message.request_id,
            "state": message.outcome,
        }

    async def _dispatch_checks(self, row: dict, command: dict, claim: bool) -> list[dict] | None:
        owner, sid = row["ownerId"], row["sessionId"]
        auth = sessions.command_caller(owner, command)
        current, enrollment = await sessions.authorize_session(auth, command["roomId"], sid)
        sessions._require_control(current, sessions._public(sessions.LocalSessionCommand, command))
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
                        and not (c["state"] == "unknown" and c["expectedGeneration"] != row["generation"])
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
                    envelope = {"type": "local.session.command", "command": public, "actor": actor}
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
