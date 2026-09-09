"""Company-room admission over distinct native human sockets.

No owner-admin fallback. There is deliberately no runtime qualification loader:
the controller must prove native human/machine coexistence and legacy privacy
before wiring a reviewed qualification source. Test registrations are not rollout.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

import websockets
from jsonschema import Draft202012Validator

from core.crypto.operator_device import OperatorDeviceIdentity, sign_connect_request
from core.gateway.operator_rpc import GATEWAY_PORT

CLIENT_ID = "lightbulb-company-room"
HUMAN_SCOPES = ("operator.read", "operator.write")
_AGENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")


class RoomError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.error = {"code": code, "message": message}
        super().__init__(message)


def _digest(value) -> str:
    # Matches UTF8(JSON.stringify(...)); preserve issuer/subject case exactly.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class HumanIdentity:
    owner_id: str
    issuer: str
    subject: str
    connection_id: str
    source_ip: str

    @property
    def alias(self) -> str:
        # Native state/user-profiles.ts normalizeEmail trims and lowercases.
        return f"clerk-{_digest([self.issuer, self.subject])}@identity.lightbulb.invalid"

    @classmethod
    def from_connection(cls, connection_id: str, row: dict):
        from core.config import settings

        if not (
            row.get("connection_type") == "native_human"
            and row.get("auth_kind") == "clerk_user"
            and row.get("verified_issuer") == settings.CLERK_ISSUER
            and row.get("verified_subject") == row.get("user_id")
            and isinstance(row.get("user_id"), str)
            and row["user_id"]
            and isinstance(row.get("org_id"), str)
            and row["org_id"].startswith("org_")
        ):
            raise RoomError("FORBIDDEN", "Verified organization human identity required")
        try:
            source = ipaddress.ip_address(row.get("source_ip", ""))
            if source.is_loopback or source.is_unspecified:
                raise ValueError("unattributable source")
        except ValueError as exc:
            raise RoomError("FORBIDDEN", "Verified client network attribution required") from exc
        return cls(row["org_id"], row["verified_issuer"], row["verified_subject"], connection_id, str(source))


@dataclass(frozen=True)
class QualifiedHumanDevice:
    identity: OperatorDeviceIdentity
    profile_id: str


@dataclass(frozen=True)
class RoomQualification:
    """Server-owned, exact runtime proof, never accepted from request/container tags.

    Device keys must be distinct from backend/Paperclip devices and bound to the
    hashed verified alias. Provisioning/loading these records remains a gate.
    """

    owner_id: str
    ip: str
    release_id: str
    build_id: str
    evidence_id: str
    human_auth_verified: bool
    machine_coexistence_verified: bool
    legacy_privacy_verified: bool
    identity_header: str
    agents: frozenset[str]
    humans: dict[str, QualifiedHumanDevice] = field(repr=False)

    def require(self, identity: HumanIdentity, container: dict, ip: str):
        if not (
            self.owner_id == identity.owner_id
            and self.ip == ip
            and self.release_id
            and self.release_id == container.get("release_update_id")
            and self.build_id
            and self.evidence_id
            and self.human_auth_verified is True
            and self.machine_coexistence_verified is True
            and self.legacy_privacy_verified is True
            and identity.alias in self.humans
            and re.fullmatch(r"x-[a-z0-9-]+", self.identity_header)
            and self.identity_header not in {"x-forwarded-for", "x-openclaw-scopes"}
        ):
            raise RoomError("ROOM_UNAVAILABLE", "Native room runtime/auth qualification unavailable")
        human = self.humans[identity.alias]
        if not human.profile_id or human.profile_id == "gateway-owner":
            raise RoomError("ROOM_UNAVAILABLE", "Qualified human profile required")
        return human


# No API, environment switch, image-tag inference, or production registration.
_qualified_owners: dict[str, RoomQualification] = {}


def qualification_for(owner_id: str) -> RoomQualification:
    qualification = _qualified_owners.get(owner_id)
    if qualification is None:
        raise RoomError("ROOM_UNAVAILABLE", "Native human and machine authentication not qualified")
    return qualification


def room_prefix(owner_id: str, agent_id: str) -> str:
    if not _AGENT.fullmatch(agent_id):
        raise RoomError("INVALID_REQUEST", "Invalid room agent")
    return f"agent:{agent_id}:lightbulb-room:{_digest(owner_id)}:"


def owns_room_key(key, identity: HumanIdentity, qualification: RoomQualification) -> bool:
    if not isinstance(key, str):
        return False
    parts = key.split(":")
    return (
        len(parts) == 5
        and parts[1] in qualification.agents
        and key.startswith(room_prefix(identity.owner_id, parts[1]))
        and re.fullmatch(r"[0-9a-f]{64}", parts[4]) is not None
    )


def _object(properties: dict, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STRING = {"type": "string", "minLength": 1, "maxLength": 512}
_MENTION_REFERENCE = {"type": "string", "minLength": 1, "maxLength": 256}
_TARGET = {"sessionKey": _STRING, "agentId": _STRING}
_KEY = {"key": _STRING, "agentId": _STRING}
_MENTIONS = {
    "type": "array",
    "maxItems": 10,
    "items": _object(
        {
            "profileId": _MENTION_REFERENCE,
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 1},
        },
        ("profileId", "start", "end"),
    ),
}
# Deliberately a subset of frozen 9.3 closed schemas. No run dispatch in create,
# public share, attachments, arbitrary sessions.patch, tools or account RPCs.
_SCHEMAS = {
    "sessions.create": _object(
        {
            "agentId": _STRING,
            "idempotencyKey": _STRING,
            "displayName": {"type": "string", "minLength": 1, "maxLength": 500},
            "parentSessionKey": _STRING,
        },
        ("agentId", "idempotencyKey"),
    ),
    "chat.send": _object(
        {
            **_TARGET,
            "message": {"type": "string", "maxLength": 100_000},
            "idempotencyKey": _STRING,
            "sessionId": _STRING,
            "mentions": _MENTIONS,
            "expectedLeafEntryId": {"type": ["string", "null"], "minLength": 1},
            "expectedSessionRoutingContract": _STRING,
            "queueMode": {"enum": ["steer", "followup", "collect", "interrupt"]},
            "replyToId": _STRING,
        },
        ("sessionKey", "message", "idempotencyKey", "expectedLeafEntryId"),
    ),
    "chat.history": _object(
        {
            **_TARGET,
            "cursor": {"type": "string"},
            "sessionId": _STRING,
            "messageId": _STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
        },
        ("sessionKey",),
    ),
    "chat.abort": _object({**_TARGET, "runId": _STRING}, ("sessionKey",)),
    "sessions.messages.subscribe": _object(_KEY, ("key",)),
    "sessions.messages.unsubscribe": _object(_KEY, ("key",)),
    "session.members.list": _object(_TARGET, ("sessionKey",)),
    "session.members.listEvidence": _object(_TARGET, ("sessionKey",)),
    "session.members.add": _object({**_TARGET, "identityId": _STRING}, ("sessionKey", "identityId")),
    "session.members.remove": _object({**_TARGET, "identityId": _STRING}, ("sessionKey", "identityId")),
    "session.visibility.set": _object(
        {**_TARGET, "visibility": {"enum": ["shared", "read-only", "suggest"]}}, ("sessionKey", "visibility")
    ),
    "sessions.assignOwner": _object(
        {**_KEY, "owner": _object({"type": {"enum": ["agent", "human"]}, "id": _STRING}, ("type", "id"))},
        ("key", "owner"),
    ),
    "session.typing": _object(
        {
            **_TARGET,
            "sessionId": _STRING,
            "typing": {"type": "boolean"},
            "preview": {"type": "string", "maxLength": 400},
        },
        ("sessionKey", "sessionId", "typing"),
    ),
    "users.mentionable": _object(
        {**_TARGET, "agentId": _MENTION_REFERENCE, "query": {"type": "string", "maxLength": 128}}, ("sessionKey",)
    ),
    "mentions.list": _object({}),
    "mentions.dismiss": _object(
        {"ids": {"type": "array", "maxItems": 100, "uniqueItems": True, "items": _MENTION_REFERENCE}}, ("ids",)
    ),
}
_VALIDATORS = {method: Draft202012Validator(schema) for method, schema in _SCHEMAS.items()}
ROOM_METHODS = frozenset(_SCHEMAS)
_ROOM_EVENTS = frozenset(
    {"chat", "agent", "session.message", "session.typing", "session.sharing", "session.sharing.evidence"}
)


def admit_rpc(method: str, params: dict, identity: HumanIdentity, qualification: RoomQualification) -> dict:
    validator = _VALIDATORS.get(method)
    if validator is None or not validator.is_valid(params):
        raise RoomError("INVALID_REQUEST", "Unsupported room method or arguments")
    admitted = dict(params)
    if method == "chat.history":
        # Frozen chat-history-handler.ts rejects these selector combinations
        # before lookup; native transcript-to-canonical-key ownership stays upstream.
        if "sessionId" in params and "messageId" not in params:
            raise RoomError("INVALID_REQUEST", "sessionId requires messageId")
        if "offset" in params and "messageId" in params:
            raise RoomError("INVALID_REQUEST", "offset and messageId cannot be used together")
        if "cursor" in params and ("offset" in params or "messageId" in params):
            raise RoomError("INVALID_REQUEST", "cursor cannot be used with offset or messageId")
    if method == "sessions.create":
        agent = params["agentId"]
        if agent not in qualification.agents:
            raise RoomError("FORBIDDEN", "Room agent not authorized")
        admitted["key"] = room_prefix(identity.owner_id, agent) + _digest(
            [identity.issuer, identity.subject, params["idempotencyKey"]]
        )
        admitted["visibility"] = "shared"
    for key_field in ("sessionKey", "key", "parentSessionKey"):
        if key_field in admitted and not owns_room_key(admitted[key_field], identity, qualification):
            raise RoomError("FORBIDDEN", "Private or foreign session target")
    target = admitted.get("sessionKey") or admitted.get("key")
    if target and "agentId" in admitted and admitted["agentId"] != target.split(":")[1]:
        raise RoomError("FORBIDDEN", "Room agent/key mismatch")
    profiles = {human.profile_id for human in qualification.humans.values()}
    if "identityId" in admitted and admitted["identityId"] not in profiles:
        raise RoomError("FORBIDDEN", "Unqualified room participant")
    if method == "sessions.assignOwner":
        owner = admitted["owner"]
        if owner["id"] not in (profiles if owner["type"] == "human" else qualification.agents):
            raise RoomError("FORBIDDEN", "Unqualified room assignee")
    if "idempotencyKey" in admitted:
        # Native run dedupe is gateway-wide. Namespace by actor AND room so a
        # different human cannot consume another human's retry/run identifier.
        admitted["idempotencyKey"] = _digest(
            [identity.owner_id, identity.issuer, identity.subject, target, admitted["idempotencyKey"]]
        )
    if method == "chat.send":
        admitted["deliver"] = False
        length = len(params["message"].encode("utf-16-le")) // 2
        for mention in params.get("mentions", []):
            if mention["profileId"] not in profiles or not 0 <= mention["start"] < mention["end"] <= length:
                raise RoomError("INVALID_REQUEST", "Invalid mention offsets")
    return admitted


async def require_current_member(identity: HumanIdentity, conn_svc) -> None:
    from core.services.clerk_admin import list_user_organizations

    row = await asyncio.to_thread(conn_svc.get_connection, identity.connection_id)
    if not row or HumanIdentity.from_connection(identity.connection_id, row) != identity:
        raise RoomError("FORBIDDEN", "Human connection revoked")
    # No positive membership cache: a warm task must not authorize removed users.
    # ponytail: Clerk read per action/event; qualify stream-rate limits before
    # rollout, then use a durable revocation epoch if this becomes a bottleneck.
    if not any(org["id"] == identity.owner_id for org in await list_user_organizations(identity.subject, limit=100)):
        raise RoomError("FORBIDDEN", "Organization membership revoked or unavailable")


class NativeHumanConnection:
    def __init__(self, identity, qualification, device, management_api, conn_svc):
        self.identity = identity
        self.qualification = qualification
        self.device = device
        self.management_api = management_api
        self.conn_svc = conn_svc
        self.ws = None
        self.reader = None
        self.pending = {}
        self.subscriptions = set()
        self.methods = set()
        self.profile = None
        self.closed = False
        self.run_holds = set()

    async def connect(self):
        try:
            self.ws = await websockets.connect(
                f"ws://{self.qualification.ip}:{GATEWAY_PORT}",
                additional_headers={
                    self.qualification.identity_header: self.identity.alias,
                    "x-forwarded-for": self.identity.source_ip,
                    "x-openclaw-scopes": ",".join(HUMAN_SCOPES),
                },
                open_timeout=10,
                close_timeout=5,
                max_size=2**20,
            )
            challenge = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
            nonce = challenge.get("payload", {}).get("nonce")
            if challenge.get("event") != "connect.challenge" or not isinstance(nonce, str) or not nonce:
                raise RoomError("ROOM_UNAVAILABLE", "Invalid native challenge")
            req_id = str(uuid4())
            await self.ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": req_id,
                        "method": "connect",
                        "params": {
                            "minProtocol": 4,
                            "maxProtocol": 4,
                            "role": "operator",
                            "scopes": list(HUMAN_SCOPES),
                            "client": {
                                "id": "gateway-client",
                                "mode": "backend",
                                "version": "1.0.0",
                                "platform": "linux",
                                "instanceId": self.identity.connection_id,
                            },
                            "device": sign_connect_request(
                                identity=self.device.identity, token="", nonce=nonce, scopes=HUMAN_SCOPES
                            ),
                        },
                    }
                )
            )
            response = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
            hello = response.get("payload", {})
            auth = hello.get("auth", {})
            if not (
                response.get("id") == req_id
                and response.get("ok") is True
                and hello.get("type") == "hello-ok"
                and hello.get("protocol") == 4
                and hello.get("server", {}).get("buildId") == self.qualification.build_id
                and auth.get("role") == "operator"
                and set(auth.get("scopes", [])) == set(HUMAN_SCOPES)
                and not auth.get("deviceToken")
            ):
                raise RoomError("ROOM_UNAVAILABLE", "Native human handshake is not qualified")
            self.methods = set(hello.get("features", {}).get("methods", []))
            if (
                not {"users.self", "sessions.create", "chat.send", "chat.history", "sessions.messages.subscribe"}
                <= self.methods
            ):
                raise RoomError("ROOM_UNAVAILABLE", "Native room methods unavailable")
            self.reader = asyncio.create_task(self._read())
            result = await self.rpc("users.self", {})
            self.profile = result.get("profile", {})
            if self.profile.get("id") != self.device.profile_id:
                raise RoomError("ROOM_UNAVAILABLE", "Native profile does not match verified human")
        except BaseException:
            await self.close()
            raise

    async def rpc(self, method, params):
        if self.closed or method not in self.methods:
            raise RoomError("ROOM_UNAVAILABLE", "Native human connection/method unavailable; reconnect")
        req_id = str(uuid4())
        run = None
        new_hold = False
        if method == "chat.send":
            run = (self.identity.owner_id, params["sessionKey"], params["idempotencyKey"])
            new_hold = await self.conn_svc.begin_native_run(*run)
            self.run_holds.add(run)
        future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        try:
            await self.ws.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, 30)
        except RoomError as exc:
            # Frozen pre-admission rejection; generic native/transport errors
            # may follow acceptance and MUST retain the hold. An existing hold
            # belongs to an earlier attempt and cannot be cleared by this retry.
            if (
                run
                and new_hold
                and getattr(exc, "native_rejection", False)
                and exc.error.get("code") == "INVALID_REQUEST"
                and exc.error.get("details", {}).get("reason") == "active-leaf-changed"
            ):
                await self.conn_svc.finish_native_run(*run)
                self.run_holds.discard(run)
            raise
        finally:
            self.pending.pop(req_id, None)

    async def _read(self):
        next_admission_check = time.monotonic() + 5
        try:
            while not self.closed:
                if time.monotonic() >= next_admission_check:
                    await self.require_current()
                    next_admission_check = time.monotonic() + 5
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), 5)
                except asyncio.TimeoutError:
                    await self.require_current()
                    continue
                frame = json.loads(raw)
                if frame.get("type") == "res":
                    future = self.pending.get(frame.get("id"))
                    if future and not future.done():
                        if frame.get("ok") is True:
                            future.set_result(frame.get("payload"))
                        else:
                            error = RoomError("NATIVE_ERROR", "Native room request rejected")
                            error.error = frame.get("error") or error.error
                            error.native_rejection = True
                            future.set_exception(error)
                    continue
                await self.forward_event(frame)
        except Exception:
            # No automatic replay. Reconnect must restore canonical history and
            # explicitly resubscribe; a send's outcome can be uncertain.
            try:
                await self.management_api.send_message_async(
                    self.identity.connection_id,
                    {
                        "type": "event",
                        "event": "native.room.reconnectRequired",
                        "payload": {},
                    },
                )
            except Exception:
                pass
            # Force a new edge admission after transport failure or revocation;
            # old subscriptions and an old JWT cannot keep this socket alive.
            try:
                await asyncio.to_thread(self.conn_svc.delete_connection, self.identity.connection_id)
            finally:
                await asyncio.to_thread(self.management_api.close_connection, self.identity.connection_id)
        finally:
            await self.close()

    async def forward_event(self, frame):
        if frame.get("type") != "event" or frame.get("event") not in _ROOM_EVENTS:
            return
        payload = frame.get("payload") or {}
        key = payload.get("sessionKey") or payload.get("key")
        if not owns_room_key(key, self.identity, self.qualification):
            return
        await self.require_current()
        # Observe matching terminal evidence even if this tab unsubscribed.
        # Do not use display messages, another room/run, or disconnect as proof.
        run_id = payload.get("runId")
        if (
            frame.get("event") == "chat"
            and payload.get("state") in {"final", "error", "aborted"}
            and isinstance(run_id, str)
            and run_id
        ):
            run = (self.identity.owner_id, key, run_id)
            await self.conn_svc.finish_native_run(*run)
            self.run_holds.discard(run)
        if key not in self.subscriptions:
            return
        # Native sender/creator/entryId/parentId/runId evidence stays intact.
        if len(json.dumps(frame, separators=(",", ":")).encode()) > 110_000:
            frame = {"type": "event", "event": "native.room.historyRequired", "payload": {"sessionKey": key}}
        if not await self.management_api.send_message_async(self.identity.connection_id, frame):
            raise RoomError("ROOM_UNAVAILABLE", "Native room frontend disconnected")

    async def require_current(self):
        if qualification_for(self.identity.owner_id) != self.qualification:
            raise RoomError("ROOM_UNAVAILABLE", "Room qualification was revoked")
        await require_current_member(self.identity, self.conn_svc)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.subscriptions.clear()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(
                    RoomError("ROOM_UNAVAILABLE", "Native connection closed; restore canonical history")
                )
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
        if self.ws:
            await self.ws.close()


class NativeRoomPool:
    def __init__(self, management_api, conn_svc):
        self.management_api = management_api
        self.conn_svc = conn_svc
        self.connections = {}
        self.run_holds = set()
        # ponytail: one dial lock for the pilot; per-client locks if dial latency matters.
        self.lock = asyncio.Lock()
        self.closed = False

    async def ensure(self, identity, container, ip):
        qualification = qualification_for(identity.owner_id)
        device = qualification.require(identity, container, ip)
        await require_current_member(identity, self.conn_svc)
        async with self.lock:
            if self.closed:
                raise RoomError("ROOM_UNAVAILABLE", "Room pool is shutting down")
            existing = self.connections.get(identity)
            if existing and not existing.closed and existing.qualification == qualification:
                return existing
            if existing:
                await existing.close()
            conn = NativeHumanConnection(identity, qualification, device, self.management_api, self.conn_svc)
            conn.run_holds = self.run_holds
            try:
                await conn.connect()
                await require_current_member(identity, self.conn_svc)
            except BaseException:
                await conn.close()
                raise
            self.connections[identity] = conn
            return conn

    def has_activity(self, owner_id):
        return any(run[0] == owner_id for run in self.run_holds) or any(
            identity.owner_id == owner_id and not conn.closed for identity, conn in self.connections.items()
        )

    async def close_connection(self, connection_id):
        for identity, conn in list(self.connections.items()):
            if identity.connection_id == connection_id:
                self.connections.pop(identity, None)
                await conn.close()

    async def close_all(self):
        self.closed = True
        async with self.lock:
            for identity in list(self.connections):
                await self.close_connection(identity.connection_id)


async def revoke_member_connections(owner_id: str, user_id: str) -> None:
    """Drop durable edge admission before closing warm upstream/subscription state."""
    from core.containers import get_gateway_pool
    from core.services import teams_event_broker_singleton
    from core.services.node_proxy import handle_node_disconnect
    from core.services.operator_proxy import handle_operator_disconnect

    pool = get_gateway_pool()
    errors = []
    connection_ids = set()
    try:
        rows = await pool._conn_svc.query_by_owner(owner_id)
        connection_ids.update(conn_id for conn_id, member_id, _ in rows if member_id == user_id)
    except Exception as exc:
        errors.append(exc)
    try:
        connection_ids.update(await pool._conn_svc.pending_member_revocations(owner_id, user_id))
    except Exception as exc:
        errors.append(exc)
    # Include locally admitted humans while the by-owner GSI catches up.
    connection_ids.update(
        identity.connection_id
        for identity in pool.native_rooms.connections
        if identity.owner_id == owner_id and identity.subject == user_id
    )
    for conn_id in connection_ids:
        connection_errors = []
        try:
            await pool._conn_svc.begin_member_revocation(owner_id, user_id, conn_id)
        except Exception as exc:
            connection_errors.append(exc)
        # Independent teardown must continue even after a storage/transport
        # failure. The durable retry ID is removed only when ALL steps succeed.
        try:
            pool.remove_frontend_connection(owner_id, conn_id)  # invalidates warm recipient cache
        except Exception as exc:
            connection_errors.append(exc)
        operations = [
            pool.native_rooms.close_connection(conn_id),
            handle_node_disconnect(conn_id, owner_id, user_id),
            handle_operator_disconnect(conn_id),
            asyncio.to_thread(pool._management_api.close_connection, conn_id),
        ]
        try:
            broker = teams_event_broker_singleton.get_broker()
            if broker is not None:
                operations.append(broker.unsubscribe(user_id, conn_id))
        except Exception as exc:
            connection_errors.append(exc)
        results = await asyncio.gather(*operations, return_exceptions=True)
        connection_errors.extend(result for result in results if isinstance(result, BaseException))
        if not connection_errors:
            try:
                await pool._conn_svc.finish_member_revocation(owner_id, user_id, conn_id)
            except Exception as exc:
                connection_errors.append(exc)
        errors.extend(connection_errors)
    if errors:
        raise RuntimeError("Member connection revocation incomplete; retry webhook") from errors[0]
