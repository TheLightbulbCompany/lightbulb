"""Stateless paired-desktop transport; DynamoDB fences every worker and reconnect.

No provider RPC or process registry. Dispatched means transport attempted; only a
desktop journal receipt can establish native admission. Unknown blocks the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from functools import lru_cache
from typing import Literal

from botocore.exceptions import ClientError
from pydantic import Field, TypeAdapter, ValidationError

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem, ContractModel, make_live_event, require_room_member

CLIENT_ID = "lightbulb-local-sessions"
LIVENESS_SECONDS = 30
CLAIM_SECONDS = 5
_COMPATIBLE = {("claude", "2.1.268"), ("codex", "0.149.0")}
logger = logging.getLogger(__name__)


class Descriptor(ContractModel):
    provider: Literal["claude", "codex"]
    profile_fingerprint: sessions.DeviceId
    native_conversation_id: sessions.OpaqueId
    generation: sessions.OpaqueId
    provider_version: str = Field(min_length=1, max_length=128)
    status: Literal["idle", "busy", "waiting-approval", "offline"]
    active_turn_id: sessions.OpaqueId | None = None


class Register(ContractModel):
    type: Literal["local.session.register"]
    session: Descriptor
    previous_generation: sessions.OpaqueId | None = None


class Heartbeat(ContractModel):
    type: Literal["local.session.heartbeat"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    status: Literal["idle", "busy", "waiting-approval", "offline"]
    active_turn_id: sessions.OpaqueId | None = None


class Event(ContractModel):
    kind: Literal["text", "tool", "lifecycle"]
    role: Literal["user", "assistant", "system", "tool"]
    text: str = Field(max_length=sessions.MAX_ENVELOPE_BYTES)
    truncated: bool = False


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


_WIRE = TypeAdapter(Register | Heartbeat | Events | Receipt)


def check_envelope(body: dict) -> None:
    # Match ManagementApiClient's *actual* encoding, including non-ASCII expansion.
    if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > sessions.MAX_ENVELOPE_BYTES:
        raise ApiProblem("INVALID_REQUEST", "Local session envelope exceeds 16 KiB.")


def _revised(row: dict, **changes) -> dict:
    return {**row, **changes, "revision": str(uuid.uuid4())}


def _conflict() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed; reconcile before retrying.")


async def publish_resources_changed(owner: str, human: str) -> None:
    from core.services.live_events import get_live_event_publisher

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
        if (
            not row
            or row.get("humanUserId") != connection["user_id"]
            or row.get("deviceId") != connection["device_id"]
            or row.get("connectionId") != cid
            or (generation is not None and row.get("generation") != generation)
        ):
            raise _conflict()
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
                            raise _conflict()
                        if (
                            previous["generation"] != descriptor.generation
                            and message.previous_generation != previous["generation"]
                        ):
                            raise _conflict()
                    compatible = (descriptor.provider, descriptor.provider_version) in _COMPATIBLE
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
                    )
                    await repo.put_session(row, previous, enrollment, extra=[self._connection_check(cid, connection)])
                    await self.dispatch_session(owner, sid)
                    response = {
                        "type": "local.session.registered",
                        "sessionId": sid,
                        "generation": row["generation"],
                        "afterSeq": row["eventSeq"],
                    }
                else:
                    sid = str(message.session_id)
                    # A current connection may reconcile its journal from an older
                    # execution; events/heartbeats must match the current generation.
                    row = await self._current(
                        cid, connection, sid, None if isinstance(message, Receipt) else message.generation
                    )
                    if isinstance(message, Events):
                        response = await self._events(cid, connection, enrollment, row, message)
                    elif isinstance(message, Heartbeat):
                        updated = _revised(
                            row,
                            status=message.status,
                            activeTurnId=message.active_turn_id,
                            onlineUntil=int(time.time()) + LIVENESS_SECONDS,
                        )
                        await repo.put_session(
                            updated, row, enrollment, extra=[self._connection_check(cid, connection)]
                        )
                        await self.dispatch_session(owner, sid)
                        response = {
                            "type": "local.session.heartbeat.ack",
                            "sessionId": sid,
                            "generation": row["generation"],
                            "afterSeq": row.get("eventSeq", 0),
                        }
                    else:
                        response = await self._receipt(cid, connection, enrollment, row, message)
                        await self.dispatch_session(owner, sid)
                await publish_resources_changed(owner, human)
                if isinstance(message, (Register, Heartbeat)):
                    unresolved = next(
                        (c for c in await repo.list_commands(owner, sid) if c["state"] in {"dispatched", "unknown"}),
                        None,
                    )
                    response["reconcile"] = (
                        {
                            "requestId": unresolved["requestId"],
                            "generation": unresolved["expectedGeneration"],
                            "state": unresolved["state"],
                        }
                        if unresolved
                        else None
                    )
                check_envelope(response)
                return response
            except ClientError as exc:
                if not store._is_cancelled(exc):
                    raise
                connection, enrollment = await self.connect(cid)
        raise _conflict()

    async def _events(self, cid: str, connection: dict, enrollment: dict, row: dict, message: Events) -> dict:
        first_seq = message.after_seq + 1
        previous = await store._get(row["pk"], f"EVENT#{first_seq:020d}")
        digest = store.request_hash(message.model_dump(mode="json", by_alias=True))
        if previous:
            if previous.get("batchHash") != digest:
                raise _conflict()
            return {
                "type": "local.session.events.ack",
                "sessionId": row["sessionId"],
                "batchId": message.batch_id,
                "afterSeq": previous["batchEndSeq"],
            }
        if message.after_seq != row.get("eventSeq", 0):
            raise _conflict()
        now = int(time.time() * 1000)
        has_text = any(event.kind == "text" for event in message.events)
        if has_text and now < row.get("nextTextAt", 0):
            raise ApiProblem("RATE_LIMITED", "Text batches are limited to two per second.", retry_after=0.5)
        end = message.after_seq + len(message.events)
        updates = []
        for seq, event in enumerate(message.events, first_seq):
            value = sessions.LocalSessionEvent(seq=seq, **event.model_dump()).model_dump(by_alias=True)
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
        updated = _revised(row, eventSeq=end, nextTextAt=now + 500 if has_text else row.get("nextTextAt", 0))
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection), *updates])
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
        command = await repo.get_command(row["ownerId"], row["sessionId"], message.request_id)
        if not command or command["expectedGeneration"] != message.generation or command["state"] == "pending":
            raise _conflict()
        outcome = dict(state=message.outcome, held=message.held, providerMessageId=message.provider_message_id)
        if command["state"] in {"applied", "rejected"}:
            if any(command.get(key) != value for key, value in outcome.items()):
                raise _conflict()
        else:
            await repo.replace_command(
                command,
                _revised(command, **outcome, updatedAt=int(time.time())),
                checks=[
                    repo.pairing_condition(
                        row["ownerId"], row["humanUserId"], row["deviceId"], enrollment.get("cliDeviceId")
                    ),
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

    async def _dispatch_checks(self, row: dict, command: dict, claim: bool) -> list[dict]:
        owner, sid = row["ownerId"], row["sessionId"]
        auth = AuthContext(user_id=command["actorUserId"], org_id=owner if owner != command["actorUserId"] else None)
        current, enrollment = await sessions.authorize_session(auth, command["roomId"], sid)
        sessions._require_control(current, sessions._public(sessions.LocalSessionCommand, command))
        connection, _ = await self.connect(current["connectionId"])
        if (
            current["revision"] != row["revision"]
            or connection["device_id"] != current["deviceId"]
            or connection["user_id"] != current["humanUserId"]
        ):
            raise _conflict()
        now = int(time.time())
        if command["deliverBy"] <= now or (not claim and command.get("claimUntil", 0) <= now):
            raise _conflict()
        return [
            *repo.room_conditions(owner, row["humanUserId"], command),
            repo.pairing_condition(owner, row["humanUserId"], row["deviceId"], enrollment.get("cliDeviceId")),
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
                command = next((c for c in commands if c["state"] not in {"applied", "rejected"}), None)
                if not command:
                    return
                if command["state"] == "dispatched":
                    if command.get("connectionId") != row.get("connectionId") or command["deliverBy"] <= int(
                        time.time()
                    ):
                        await repo.replace_command(
                            command, _revised(command, state="unknown", updatedAt=int(time.time()))
                        )
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
                except ApiProblem:
                    await repo.replace_command(command, _revised(command, state="rejected", updatedAt=int(time.time())))
                    continue
                claim = _revised(command, claimUntil=min(int(time.time()) + CLAIM_SECONDS, command["deliverBy"]))
                await repo.replace_command(command, claim, checks=checks)
                row = await repo.get_session(owner, sid)
                try:
                    checks = await self._dispatch_checks(row, claim, False)
                    public = sessions._public(sessions.CommandReceipt, claim).model_dump(mode="json", by_alias=True)
                    public["state"] = "dispatched"
                    envelope = {"type": "local.session.command", "command": public}
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
