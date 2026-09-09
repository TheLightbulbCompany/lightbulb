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
# Human room brokers never receive the backend operator's administrative scope.
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
    identity: OperatorDeviceIdentity = field(repr=False)
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
    agents: frozenset[str]
    humans: dict[str, QualifiedHumanDevice] = field(repr=False)

    @property
    def trusted_broker_profiles(self) -> dict[str, str]:
        bindings = {human.identity.device_id: human.profile_id for human in self.humans.values()}
        profiles = {human.profile_id for human in self.humans.values()}
        if (
            len(bindings) != len(self.humans)
            or len(profiles) != len(self.humans)
            or any(
                re.fullmatch(r"(?!gateway-owner$)[A-Za-z0-9_-]{1,128}", profile_id) is None for profile_id in profiles
            )
        ):
            raise RoomError("ROOM_UNAVAILABLE", "Qualified human device/profile bindings are ambiguous")
        return bindings

    def require(self, identity: HumanIdentity, container: dict, ip: str):
        _ = self.trusted_broker_profiles
        if not (
            self.owner_id == identity.owner_id
            and self.ip == ip
            and self.release_id
            and self.build_id
            and self.evidence_id
            and self.human_auth_verified is True
            and self.machine_coexistence_verified is True
            and self.legacy_privacy_verified is True
            and identity.alias in self.humans
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


def invalidate_qualification(owner_id: str) -> None:
    """Fail closed for warm native sockets before durable device teardown."""
    _qualified_owners.pop(owner_id, None)


def room_home_id(owner_id: str) -> str:
    """Opaque stable routing identity for the current per-owner room home."""
    return _digest(["founder-container-home-v1", owner_id])


def room_home_generation(qualification: RoomQualification) -> str:
    """Opaque incarnation fence; callers never route by image tag or address."""
    return _digest(
        [
            "founder-container-home-generation-v1",
            qualification.owner_id,
            qualification.ip,
            qualification.release_id,
            qualification.build_id,
            qualification.evidence_id,
        ]
    )


def room_prefix(owner_id: str, agent_id: str) -> str:
    if not _AGENT.fullmatch(agent_id):
        raise RoomError("INVALID_REQUEST", "Invalid room agent")
    return f"agent:{agent_id}:lightbulb-room:{_digest(owner_id)}:"


def owns_room_key(key, identity: HumanIdentity, qualification: RoomQualification) -> bool:
    """Namespace/container fence only; the native ACL decides room membership."""
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
_SESSION_KEY = {"type": "string", "minLength": 1, "maxLength": 4096}
_AGENT_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_PATH = {"type": "string", "maxLength": 4096}
_MEMBER = _object({"type": {"enum": ["profile", "agent"]}, "id": _STRING}, ("type", "id"))
# Browser callers can select only these fields. Actor, profile, auth, agent
# override and server-authored provenance remain properties of the native socket.
_SCHEMAS = {
    "sessions.room.create": _object(
        {
            "agentId": _STRING,
            "idempotencyKey": _STRING,
            "displayName": {"type": "string", "minLength": 1, "maxLength": 500},
            "roomKind": {"enum": ["channel", "dm", "group-dm", "thread"]},
            "members": {"type": "array", "maxItems": 128, "items": _MEMBER},
            "parentSessionKey": _STRING,
            "threadOrigin": _object({"parentRoomKey": _STRING, "originRootMessageId": _STRING}, ("parentRoomKey",)),
        },
        ("agentId", "idempotencyKey", "roomKind", "members"),
    ),
    "sessions.message.append": _object(
        {
            "sessionKey": _STRING,
            "expectedSessionId": _STRING,
            "idempotencyKey": _STRING,
            "text": {"type": "string", "minLength": 1, "maxLength": 100_000},
            "replyToId": _STRING,
            "mentions": {"type": "array", "maxItems": 100, "items": _MEMBER},
        },
        ("sessionKey", "expectedSessionId", "idempotencyKey", "text"),
    ),
    "chat.history": _object(
        {
            "sessionKey": _SESSION_KEY,
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "maxBytes": {"type": "integer", "minimum": 1024, "maximum": 1_000_000},
            "offset": {"type": "integer", "minimum": 0},
            "messageId": _STRING,
            "sessionId": _STRING,
            "maxChars": {"type": "integer", "minimum": 1, "maximum": 500_000},
        },
        ("sessionKey",),
    ),
    "sessions.search": _object(
        {
            "sessionKeys": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "uniqueItems": True,
                "items": _SESSION_KEY,
            },
            "query": {"type": "string", "minLength": 1, "maxLength": 4096},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        ("sessionKeys", "query"),
    ),
    "sessions.list": _object(
        {
            "agentId": _AGENT_ID,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
            "cursor": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,8})$"},
            "search": {"type": "string", "maxLength": 512},
            "archived": {"anyOf": [{"type": "boolean"}, {"const": "all"}]},
            "includeDerivedTitles": {"type": "boolean"},
            "includeLastMessage": {"type": "boolean"},
        }
    ),
    "sessions.messages.subscribe": _object({"key": _SESSION_KEY}, ("key",)),
    "sessions.messages.unsubscribe": _object({"key": _SESSION_KEY}, ("key",)),
    "sessions.viewers.set": _object(
        {
            "agentId": _AGENT_ID,
            "sessionKeys": {
                "type": "array",
                "maxItems": 32,
                "uniqueItems": True,
                "items": _SESSION_KEY,
            },
        },
        ("sessionKeys",),
    ),
    "sessions.presence.heartbeat": _object(
        {
            "sessionKey": _SESSION_KEY,
            "visibility": {"enum": ["visible", "hidden", "unknown"]},
            "recentInput": {"enum": ["recent", "stale", "unknown"]},
        },
        ("sessionKey",),
    ),
    **{
        method: _object({"sessionKey": _SESSION_KEY}, ("sessionKey",))
        for method in (
            "sessions.presence.snapshot",
            "sessions.presence.subscribe",
            "sessions.presence.unsubscribe",
        )
    },
    "session.typing": _object(
        {
            "sessionKey": _SESSION_KEY,
            "sessionId": _STRING,
            "typing": {"type": "boolean"},
        },
        ("sessionKey", "sessionId", "typing"),
    ),
    "users.mentionable": _object(
        {"sessionKey": _SESSION_KEY, "query": {"type": "string", "maxLength": 128}},
        ("sessionKey",),
    ),
    **{
        method: _object({"sessionKey": _SESSION_KEY}, ("sessionKey",))
        for method in ("session.members.list", "session.members.listEvidence")
    },
    "sessions.files.list": _object(
        {"sessionKey": _SESSION_KEY, "path": _PATH, "search": {"type": "string", "maxLength": 4096}},
        ("sessionKey",),
    ),
    "sessions.files.get": _object(
        {"sessionKey": _SESSION_KEY, "path": {**_PATH, "minLength": 1}},
        ("sessionKey", "path"),
    ),
    "sessions.execution.dispatch": _object(
        {
            "sessionKey": _SESSION_KEY,
            "expectedSessionId": _STRING,
            "inputMessageId": _STRING,
            "idempotencyKey": _STRING,
            "hopCount": {"type": "integer", "minimum": 0, "maximum": 3},
        },
        ("sessionKey", "expectedSessionId", "inputMessageId", "idempotencyKey"),
    ),
    **{
        method: _object({"sessionKey": _STRING, "identity": _MEMBER}, ("sessionKey", "identity"))
        for method in ("session.members.add", "session.members.remove")
    },
}
_LOCAL_SCHEMAS = {
    "lightbulb.rooms.readState": _object(
        {"sessionKey": _SESSION_KEY, "expectedSessionId": _STRING},
        ("sessionKey", "expectedSessionId"),
    ),
    "lightbulb.rooms.markRead": _object(
        {
            "sessionKey": _SESSION_KEY,
            "expectedSessionId": _STRING,
            "messageId": _STRING,
            "messageSeq": {"type": "integer", "minimum": 1},
        },
        ("sessionKey", "expectedSessionId", "messageId", "messageSeq"),
    ),
}
_APPEND_RESULT = Draft202012Validator(
    _object(
        {
            "sessionKey": _STRING,
            "sessionId": _STRING,
            "messageId": _STRING,
            "messageSeq": {"type": "integer", "minimum": 1},
            "effectiveParentId": _STRING,
            "appended": {"type": "boolean"},
        },
        ("sessionKey", "sessionId", "messageId", "messageSeq", "appended"),
    ),
)
_VALIDATORS = {
    method: Draft202012Validator(schema)
    for method, schema in {**_SCHEMAS, **_LOCAL_SCHEMAS}.items()
}
ROOM_METHODS = frozenset(_SCHEMAS)
LOCAL_ROOM_METHODS = frozenset(_LOCAL_SCHEMAS)
_INTERNAL_METHODS = frozenset({"agent.wait", "users.self"})
UNCERTAIN_AFTER_SUCCESS = frozenset(
    {
        "sessions.room.create",
        "sessions.message.append",
        "sessions.execution.dispatch",
        "session.members.add",
        "session.members.remove",
    }
)
_ROOM_EVENTS = frozenset(
    {
        "chat",
        "agent",
        "session.message",
        "session.presence",
        "session.typing",
        "session.sharing",
        "session.sharing.evidence",
    }
)


def admit_rpc(method: str, params: dict, identity: HumanIdentity, qualification: RoomQualification) -> dict:
    validator = _VALIDATORS.get(method)
    if validator is None or not validator.is_valid(params):
        raise RoomError("INVALID_REQUEST", "Unsupported room method or arguments")
    admitted = dict(params)
    if method == "sessions.list" and "cursor" in admitted:
        if "offset" in admitted:
            raise RoomError("INVALID_REQUEST", "Directory cursor and offset are mutually exclusive")
        admitted["offset"] = int(admitted.pop("cursor"))
    if "agentId" in admitted and admitted["agentId"] not in qualification.agents:
        raise RoomError("FORBIDDEN", "Room agent not authorized")
    if method == "sessions.room.create":
        agent = params["agentId"]
        admitted["key"] = room_prefix(identity.owner_id, agent) + _digest(
            [identity.issuer, identity.subject, params["idempotencyKey"]]
        )
        admitted["visibility"] = "restricted"
    for key_field in ("sessionKey", "key", "parentSessionKey"):
        if key_field in admitted and not owns_room_key(admitted[key_field], identity, qualification):
            raise RoomError("FORBIDDEN", "Private or foreign session target")
    if any(not owns_room_key(key, identity, qualification) for key in admitted.get("sessionKeys", [])):
        raise RoomError("FORBIDDEN", "Private or foreign session target")
    origin = admitted.get("threadOrigin")
    if origin and not owns_room_key(origin["parentRoomKey"], identity, qualification):
        raise RoomError("FORBIDDEN", "Private or foreign thread origin")
    profiles = {human.profile_id for human in qualification.humans.values()}
    participants = admitted.get("members", []) + admitted.get("mentions", [])
    if "identity" in admitted:
        participants = [*participants, admitted["identity"]]
    for participant in participants:
        if participant["id"] not in (profiles if participant["type"] == "profile" else qualification.agents):
            raise RoomError("FORBIDDEN", "Unqualified room participant")
    if method == "sessions.room.create":
        # Creation dedupe is gateway-wide; append dedupe is native actor/session
        # scoped and its client key must survive unchanged for replay/conflict.
        admitted["idempotencyKey"] = _digest(
            [identity.owner_id, identity.issuer, identity.subject, admitted["key"], admitted["idempotencyKey"]]
        )
    return admitted


def _room_route(qualification: RoomQualification) -> dict[str, str]:
    return {
        "home_id": room_home_id(qualification.owner_id),
        "home_generation": room_home_generation(qualification),
    }


def _positive_integer(value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_SAFE_INTEGER
    )


_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_PRESENCE_MAX_CONNECTIONS = 4_096
_PRESENCE_EXPIRES_MS = 90_000
_TYPING_EXPIRES_MS = 2_500
_PRESENCE_FIELDS = frozenset(
    {
        "roomKey",
        "actor",
        "connectionId",
        "connectionStartedAtMs",
        "sequence",
        "state",
        "serverReceivedAtMs",
        "expiresAtMs",
        "visibility",
        "visibilityObservedAtMs",
        "recentInput",
        "recentInputObservedAtMs",
        "viewingIntent",
        "viewingIntentReceivedAtMs",
        "authoritativeLastSeenAtMs",
    }
)


def _epoch_ms(value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1_000_000_000_000 <= value <= _MAX_SAFE_INTEGER
    )


def normalize_presence_event(value, server_now_at_ms: int) -> dict | None:
    """Validate native timing evidence without inventing browser-derived state."""
    if not _epoch_ms(server_now_at_ms) or not isinstance(value, dict):
        return None
    if set(value) - _PRESENCE_FIELDS:
        return None
    event = dict(value)
    actor = event.get("actor")
    if not (
        isinstance(actor, dict)
        and set(actor) == {"type", "id"}
        and actor.get("type") in {"profile", "agent"}
        and isinstance(actor.get("id"), str)
        and 1 <= len(actor["id"]) <= 256
        and isinstance(event.get("roomKey"), str)
        and event["roomKey"]
        and isinstance(event.get("connectionId"), str)
        and 1 <= len(event["connectionId"]) <= 256
        and _positive_integer(event.get("sequence"))
        and event["sequence"] <= _MAX_SAFE_INTEGER
        and event.get("state") in {"online", "away", "offline", "typing", "unknown"}
        and event.get("visibility") in {"visible", "hidden", "unknown"}
        and event.get("recentInput") in {"recent", "stale", "unknown"}
        and event.get("viewingIntent") in {"viewing", "not-viewing", "unknown"}
        and _epoch_ms(event.get("connectionStartedAtMs"))
        and _epoch_ms(event.get("serverReceivedAtMs"))
        and event["connectionStartedAtMs"] <= event["serverReceivedAtMs"] <= server_now_at_ms
        and _epoch_ms(event.get("expiresAtMs"))
    ):
        return None
    duration = _TYPING_EXPIRES_MS if event["state"] == "typing" else _PRESENCE_EXPIRES_MS
    if not (
        event["serverReceivedAtMs"]
        <= event["expiresAtMs"]
        <= event["serverReceivedAtMs"] + duration
    ):
        return None
    for observed_field, projection_field in (
        ("visibilityObservedAtMs", "visibility"),
        ("recentInputObservedAtMs", "recentInput"),
        ("viewingIntentReceivedAtMs", "viewingIntent"),
    ):
        observed_at = event.get(observed_field)
        if not (
            _epoch_ms(observed_at)
            and observed_at <= event["serverReceivedAtMs"]
            and observed_at <= server_now_at_ms
        ):
            event.pop(observed_field, None)
            event[projection_field] = "unknown"
    last_seen = event.get("authoritativeLastSeenAtMs")
    if not (
        _epoch_ms(last_seen)
        and last_seen <= event["serverReceivedAtMs"]
        and last_seen <= server_now_at_ms
    ):
        event.pop("authoritativeLastSeenAtMs", None)
    if event["state"] == "online" and not (
        event["visibility"] == "visible"
        and event["recentInput"] == "recent"
        and event["viewingIntent"] == "viewing"
    ):
        event["state"] = "unknown"
    event["actor"] = dict(actor)
    return event


class NativePresenceProjection:
    """Connection-local at-least-once reconciliation; never a presence authority."""

    def __init__(self):
        self._connections: dict[str, dict[str, dict]] = {}
        self._server_now: dict[str, int] = {}
        self._snapshots: dict[str, dict] = {}

    def reconcile_event(self, value, server_now_at_ms: int) -> dict | None:
        event = normalize_presence_event(value, server_now_at_ms)
        if event is None:
            return None
        room_connections = self._connections.setdefault(event["roomKey"], {})
        prior = room_connections.get(event["connectionId"])
        if prior and (
            prior["connectionStartedAtMs"] != event["connectionStartedAtMs"]
            or prior["actor"] != event["actor"]
        ):
            return None
        if prior and (
            event["sequence"] <= prior["sequence"]
            or event["serverReceivedAtMs"] < prior["serverReceivedAtMs"]
        ):
            return None
        room_connections[event["connectionId"]] = event
        self._server_now[event["roomKey"]] = max(
            server_now_at_ms, self._server_now.get(event["roomKey"], server_now_at_ms)
        )
        return dict(event)

    def reconcile_snapshot(self, value) -> dict:
        if not isinstance(value, dict):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native presence inventory")
        if set(value) - {"roomKey", "inventoryStatus", "serverNowAtMs", "connections", "failureCode"}:
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native presence inventory")
        room_key = value.get("roomKey")
        status = value.get("inventoryStatus")
        server_now = value.get("serverNowAtMs")
        connections = value.get("connections")
        if not (
            isinstance(room_key, str)
            and room_key
            and status in {"complete", "incomplete", "failed"}
            and _epoch_ms(server_now)
            and isinstance(connections, list)
            and len(connections) <= _PRESENCE_MAX_CONNECTIONS
            and (
                value.get("failureCode") is None
                or isinstance(value.get("failureCode"), str)
                and 1 <= len(value["failureCode"]) <= 128
            )
        ):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native presence inventory")
        prior_snapshot = self._snapshots.get(room_key)
        if server_now < self._server_now.get(room_key, 0):
            return dict(prior_snapshot) if prior_snapshot else {
                **value,
                "connections": [],
                "evidenceStatus": "invalid",
                "validationFailureCode": "PRESENCE_STALE_EVIDENCE",
            }
        previous_connections = dict(self._connections.get(room_key, {}))
        retained = dict(previous_connections)
        if status == "failed":
            snapshot = {
                **value,
                "connections": list(retained.values()),
                **(
                    {"evidenceStatus": prior_snapshot["evidenceStatus"]}
                    if prior_snapshot and prior_snapshot.get("evidenceStatus") is not None
                    else {}
                ),
                **(
                    {"validationFailureCode": prior_snapshot["validationFailureCode"]}
                    if prior_snapshot and prior_snapshot.get("validationFailureCode") is not None
                    else {}
                ),
            }
            self._server_now[room_key] = server_now
            self._snapshots[room_key] = snapshot
            return dict(snapshot)
        included = set()
        invalid = False
        for candidate in connections:
            event = normalize_presence_event(candidate, server_now)
            invalid_observation = (
                bool(
                    event
                    and any(
                        field in candidate and event.get(field) is None
                        for field in (
                            "visibilityObservedAtMs",
                            "recentInputObservedAtMs",
                            "viewingIntentReceivedAtMs",
                            "authoritativeLastSeenAtMs",
                        )
                    )
                )
                if isinstance(candidate, dict)
                else False
            )
            if event is None or event["roomKey"] != room_key or invalid_observation:
                invalid = True
                prior = (
                    retained.get(candidate.get("connectionId"))
                    if isinstance(candidate, dict)
                    else None
                )
                if prior:
                    retained[prior["connectionId"]] = {
                        **{
                            key: value
                            for key, value in prior.items()
                            if key
                            not in {
                                "visibilityObservedAtMs",
                                "recentInputObservedAtMs",
                                "viewingIntentReceivedAtMs",
                                "authoritativeLastSeenAtMs",
                            }
                        },
                        "state": "unknown",
                        "visibility": "unknown",
                        "recentInput": "unknown",
                        "viewingIntent": "unknown",
                    }
                continue
            prior = retained.get(event["connectionId"])
            if prior and (
                prior["connectionStartedAtMs"] != event["connectionStartedAtMs"]
                or prior["actor"] != event["actor"]
            ):
                invalid = True
                retained[event["connectionId"]] = {**prior, "state": "unknown"}
                continue
            included.add(event["connectionId"])
            if prior and (
                event["sequence"] <= prior["sequence"]
                or event["serverReceivedAtMs"] < prior["serverReceivedAtMs"]
            ):
                continue
            retained[event["connectionId"]] = event
        if status == "complete" and not invalid:
            retained = {key: event for key, event in retained.items() if key in included}
        if len(retained) > _PRESENCE_MAX_CONNECTIONS:
            snapshot = {
                **value,
                "connections": list(previous_connections.values()),
                "evidenceStatus": "invalid",
                "validationFailureCode": "PRESENCE_CAPACITY_EXCEEDED",
            }
            self._server_now[room_key] = server_now
            self._snapshots[room_key] = snapshot
            return dict(snapshot)
        self._connections[room_key] = retained
        self._server_now[room_key] = server_now
        snapshot = {
            **value,
            "connections": list(retained.values()),
            "evidenceStatus": "invalid" if invalid else "valid",
            **({"validationFailureCode": "PRESENCE_INVALID_EVIDENCE"} if invalid else {}),
        }
        self._snapshots[room_key] = snapshot
        return dict(snapshot)


class NativeMessageProjection:
    """Ephemeral canonical sequence watermarks for dedupe and gap recovery."""

    def __init__(self):
        self._rooms: dict[str, tuple[str, int]] = {}

    def observe_history(self, result, expected_room_key: str) -> None:
        if not (
            isinstance(result, dict)
            and result.get("sessionKey") == expected_room_key
            and isinstance(result.get("sessionId"), str)
            and result["sessionId"]
            and isinstance(result.get("messages"), list)
        ):
            return
        identities = [_history_message_identity(message) for message in result["messages"]]
        if any(identity is None for identity in identities):
            return
        observed = max((identity[1] for identity in identities if identity), default=0)
        prior = self._rooms.get(expected_room_key)
        if prior and prior[0] == result["sessionId"]:
            observed = max(observed, prior[1])
        self._rooms[expected_room_key] = (result["sessionId"], observed)

    def reconcile_event(self, payload) -> str:
        if not isinstance(payload, dict):
            return "invalid"
        room_key = payload.get("sessionKey")
        session_id = payload.get("sessionId")
        message_id = payload.get("messageId")
        message_seq = payload.get("messageSeq")
        if not (
            isinstance(room_key, str)
            and room_key
            and isinstance(session_id, str)
            and session_id
            and isinstance(message_id, str)
            and message_id
            and _positive_integer(message_seq)
        ):
            return "invalid"
        prior = self._rooms.get(room_key)
        if prior is None or prior[0] != session_id or message_seq > prior[1] + 1:
            return "gap"
        if message_seq <= prior[1]:
            return "duplicate"
        self._rooms[room_key] = (session_id, message_seq)
        return "forward"


def filter_rpc_result(
    method: str,
    result,
    identity: HumanIdentity,
    qualification: RoomQualification,
    params: dict | None = None,
):
    """Apply depth filters to unscoped native discovery results."""
    if method == "sessions.list":
        if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room inventory")
        status = result.get("inventoryStatus", "complete")
        if status not in {"complete", "incomplete", "failed"}:
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room inventory status")
        evidence_at = result.get("serverEvidenceAtMs", result.get("ts"))
        if not _epoch_ms(evidence_at):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room inventory evidence time")
        projected = []
        # Failed inventory carries no usable fresh rows. The caller retains
        # its visibly stale last-known directory under the unchanged status.
        for item in [] if status == "failed" else result["sessions"]:
            # Native list authorization projects the exact caller's role. An
            # owner-shaped key is only a routing fence and never membership.
            visibility = item.get("visibility") if isinstance(item, dict) else None
            sharing_role = item.get("sharingRole") if isinstance(item, dict) else None
            authorized_projection = (
                visibility == "restricted" and sharing_role in {"owner", "member"}
            ) or (
                visibility == "shared"
                and sharing_role in {"admin", "owner", "member", "viewer"}
            )
            if not (
                isinstance(item, dict)
                and owns_room_key(item.get("key"), identity, qualification)
                and authorized_projection
                and isinstance(item.get("sessionId"), str)
                and item["sessionId"]
                and item.get("roomKind") in {"channel", "dm", "group-dm", "thread"}
            ):
                continue
            last_seq = item.get("lastMessageSeq")
            if last_seq is not None and not _positive_integer(last_seq):
                raise RoomError("ROOM_UNAVAILABLE", "Invalid native room sequence")
            projected.append(
                {
                    "roomKey": item["key"],
                    "sessionId": item["sessionId"],
                    "roomKind": item["roomKind"],
                    **({"lastMessageSeq": last_seq} if last_seq is not None else {}),
                    **_room_route(qualification),
                }
            )
        response = {
            "inventoryStatus": status,
            "serverEvidenceAtMs": evidence_at,
            "rooms": projected,
        }
        next_cursor = result.get("nextCursor")
        if next_cursor is None and _positive_integer(result.get("nextOffset")):
            next_cursor = str(result["nextOffset"])
        if status != "failed" and isinstance(next_cursor, str) and next_cursor:
            response["nextCursor"] = next_cursor
        if isinstance(result.get("failureCode"), str) and result["failureCode"]:
            response["failureCode"] = result["failureCode"]
        return response
    if method == "sessions.search":
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room search result")
        requested = set((params or {}).get("sessionKeys", []))
        return {
            **result,
            "results": [
                {**item, **_room_route(qualification)}
                for item in result["results"]
                if isinstance(item, dict)
                and item.get("sessionKey") in requested
                and owns_room_key(item.get("sessionKey"), identity, qualification)
            ],
        }
    if method == "sessions.viewers.set":
        if not isinstance(result, dict) or not isinstance(result.get("sessionKeys"), list):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room viewing declaration")
        requested = set((params or {}).get("sessionKeys", []))
        returned = result["sessionKeys"]
        if len(returned) > 32 or not all(isinstance(key, str) for key in returned):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native room viewing declaration")
        returned_set = set(returned)
        if not (
            len(returned) == len(returned_set)
            and returned_set == requested
            and all(owns_room_key(key, identity, qualification) for key in returned)
        ):
            raise RoomError("FORBIDDEN", "Native viewing declaration changed scope")
        return {"sessionKeys": returned}
    if method == "users.mentionable":
        if not isinstance(result, dict) or not isinstance(result.get("users"), list):
            raise RoomError("ROOM_UNAVAILABLE", "Invalid native mentionable result")
        profiles = {human.profile_id for human in qualification.humans.values()}
        return {
            **result,
            "users": [item for item in result["users"] if isinstance(item, dict) and item.get("profileId") in profiles],
        }
    if isinstance(result, dict) and (params or {}).get("sessionKey"):
        return {**result, **_room_route(qualification)}
    if isinstance(result, dict) and (params or {}).get("key"):
        return {**result, **_room_route(qualification)}
    return result


def native_execution_run_id(profile_id: str, params: dict) -> str:
    """Mirror the fork's private-room root run identity exactly."""
    return _digest(
        [
            "room-execution-v1",
            params["expectedSessionId"],
            {"type": "profile", "id": profile_id},
            params["idempotencyKey"],
            params["inputMessageId"],
        ]
    )


def is_terminal_agent_wait_result(result, run_id: str, session_id: str) -> bool:
    """Accept only canonical, exact-run terminal proof from internal agent.wait."""
    if not isinstance(result, dict) or result.get("runId") != run_id:
        return False
    if result.get("retryableTransportError") is True:
        return False
    status = result.get("status")
    if status in {"ok", "error"}:
        return True
    if status != "timeout" or result.get("pendingError") is True:
        return False
    ended_at = result.get("endedAt")
    if isinstance(ended_at, int) and not isinstance(ended_at, bool) and ended_at >= 0:
        return True
    receipt = result.get("terminalReceipt")
    requested = receipt.get("requested") if isinstance(receipt, dict) else None
    effective = receipt.get("effective") if isinstance(receipt, dict) else None
    return bool(
        isinstance(receipt, dict)
        and receipt.get("runId") == run_id
        and receipt.get("sessionId") == session_id
        and isinstance(receipt.get("turnId"), str)
        and receipt["turnId"]
        and isinstance(requested, dict)
        and all(isinstance(requested.get(field), str) and requested[field] for field in ("provider", "model"))
        and isinstance(effective, dict)
        and all(
            isinstance(effective.get(field), str) and effective[field]
            for field in ("provider", "model", "responseModel")
        )
        and isinstance(receipt.get("successfulToolNames"), list)
        and all(isinstance(name, str) for name in receipt["successfulToolNames"])
        and isinstance(receipt.get("rerouted"), bool)
        and receipt.get("terminalDisposition") in {"visible", "not-visible"}
    )


async def _finish_native_run(conn_svc, run_holds: set, run: tuple[str, str, str]) -> None:
    await conn_svc.finish_native_run(*run)
    run_holds.discard(run)


def require_append_result(result, params: dict) -> None:
    if not (
        _APPEND_RESULT.is_valid(result)
        and result["sessionKey"] == params["sessionKey"]
        and result["sessionId"] == params["expectedSessionId"]
    ):
        raise RoomError("ROOM_UNAVAILABLE", "Native append receipt does not match the requested session")


def _history_message_identity(message) -> tuple[str, int] | None:
    if not isinstance(message, dict) or not isinstance(message.get("__openclaw"), dict):
        return None
    metadata = message["__openclaw"]
    message_id = metadata.get("id")
    message_seq = metadata.get("seq")
    if not isinstance(message_id, str) or not message_id or not _positive_integer(message_seq):
        return None
    return message_id, message_seq


def _require_history(result, session_key: str, expected_session_id: str) -> list[tuple[str, int]]:
    if not (
        isinstance(result, dict)
        and result.get("sessionKey") == session_key
        and result.get("sessionId") == expected_session_id
        and isinstance(result.get("messages"), list)
    ):
        raise RoomError("ROOM_UNAVAILABLE", "Native room history identity changed")
    identities = []
    for message in result["messages"]:
        identity = _history_message_identity(message)
        if identity is None:
            raise RoomError("ROOM_UNAVAILABLE", "Native room history sequence unavailable")
        identities.append(identity)
    return identities


def _read_state_payload(
    qualification: RoomQualification,
    params: dict,
    cursor: dict | None,
    last_message_seq: int,
) -> dict:
    last_read = cursor["last_read_message_seq"] if cursor else 0
    return {
        "roomKey": params["sessionKey"],
        "sessionId": params["expectedSessionId"],
        "lastReadMessageSeq": last_read,
        "lastMessageSeq": last_message_seq,
        "unreadCount": max(0, last_message_seq - last_read),
        **({"cursorUpdatedAt": cursor["updated_at"]} if cursor else {}),
        **_room_route(qualification),
    }


async def handle_local_room_rpc(
    method: str,
    params: dict,
    identity: HumanIdentity,
    qualification: RoomQualification,
    conn,
) -> dict:
    """Read/write product cursor state only after an authoritative native read."""
    from core.repositories import room_read_cursor_repo

    session_key = params["sessionKey"]
    session_id = params["expectedSessionId"]
    if method == "lightbulb.rooms.markRead":
        anchored = await conn.rpc(
            "chat.history",
            {
                "sessionKey": session_key,
                "sessionId": session_id,
                "messageId": params["messageId"],
                "limit": 1,
                "maxChars": 1,
            },
        )
        identities = _require_history(anchored, session_key, session_id)
        conn.message_projection.observe_history(anchored, session_key)
        if (params["messageId"], params["messageSeq"]) not in identities:
            raise RoomError("INVALID_REQUEST", "Read cursor must reference a canonical visible message")
        # Recheck controller admission after the native ACL/history await. This
        # row is never consulted for authorization even if revocation races it.
        await conn.require_current()
        await room_read_cursor_repo.advance(
            identity.owner_id,
            conn.device.profile_id,
            session_key,
            params["messageSeq"],
        )
    history = await conn.rpc("chat.history", {"sessionKey": session_key, "limit": 1, "maxChars": 1})
    identities = _require_history(history, session_key, session_id)
    conn.message_projection.observe_history(history, session_key)
    await conn.require_current()
    cursor = await room_read_cursor_repo.get(identity.owner_id, conn.device.profile_id, session_key)
    return _read_state_payload(
        qualification,
        params,
        cursor,
        max((sequence for _, sequence in identities), default=0),
    )


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
    def __init__(self, identity, qualification, device, token, management_api, conn_svc):
        self.identity = identity
        self.qualification = qualification
        self.device = device
        self.token = token
        self.management_api = management_api
        self.conn_svc = conn_svc
        self.ws = None
        self.reader = None
        self.pending = {}
        self.subscriptions = set()
        self.presence_subscriptions = set()
        self.presence_projection = NativePresenceProjection()
        self.message_projection = NativeMessageProjection()
        self.methods = set()
        self.profile = None
        self.closed = False
        self.run_holds = set()

    async def connect(self):
        try:
            self.ws = await websockets.connect(
                f"ws://{self.qualification.ip}:{GATEWAY_PORT}",
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
                            "auth": {
                                "token": self.token,
                                "trustedBrokerProfileId": self.device.profile_id,
                            },
                            "client": {
                                "id": "gateway-client",
                                "mode": "backend",
                                "version": "1.0.0",
                                "platform": "linux",
                                "instanceId": self.identity.connection_id,
                            },
                            "device": sign_connect_request(
                                identity=self.device.identity,
                                token=self.token,
                                nonce=nonce,
                                scopes=HUMAN_SCOPES,
                                platform="linux",
                                trusted_broker_profile_id=self.device.profile_id,
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
                and not {"deviceToken", "deviceTokens", "bootstrapToken"} & auth.keys()
                and not {"deviceToken", "deviceTokens", "bootstrapToken"} & hello.keys()
            ):
                raise RoomError("ROOM_UNAVAILABLE", "Native human handshake is not qualified")
            self.methods = set(hello.get("features", {}).get("methods", []))
            if not ROOM_METHODS | _INTERNAL_METHODS <= self.methods:
                raise RoomError("ROOM_UNAVAILABLE", "Native room methods unavailable")
            self.reader = asyncio.create_task(self._read())
            result = await self.rpc("users.self", {})
            self.profile = result.get("profile", {})
            if self.profile.get("id") != self.device.profile_id:
                raise RoomError("ROOM_UNAVAILABLE", "Native profile does not match verified human")
            await self.require_current()
        except BaseException:
            await self.close()
            raise

    async def rpc(self, method, params):
        if self.closed or method not in self.methods or method not in ROOM_METHODS | _INTERNAL_METHODS:
            raise RoomError("ROOM_UNAVAILABLE", "Native human connection/method unavailable; reconnect")
        req_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        try:
            await self.ws.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, 30)
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
        key = payload.get("sessionKey") or payload.get("key") or payload.get("roomKey")
        if not owns_room_key(key, self.identity, self.qualification):
            return
        await self.require_current()
        if frame.get("event") == "session.presence":
            normalized = self.presence_projection.reconcile_event(
                payload, int(time.time() * 1000)
            )
            if normalized is None:
                return
            frame = {**frame, "payload": normalized}
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
            await _finish_native_run(self.conn_svc, self.run_holds, run)
        subscribed = self.presence_subscriptions if frame.get("event") == "session.presence" else self.subscriptions
        if key not in subscribed:
            return
        if frame.get("event") == "session.message":
            outcome = self.message_projection.reconcile_event(payload)
            if outcome in {"invalid", "duplicate"}:
                return
            if outcome == "gap":
                frame = {
                    "type": "event",
                    "event": "native.room.historyRequired",
                    "payload": {
                        "sessionKey": key,
                        "sessionId": payload["sessionId"],
                        "receivedMessageSeq": payload["messageSeq"],
                        **_room_route(self.qualification),
                    },
                }
        if isinstance(frame.get("payload"), dict):
            frame = {
                **frame,
                "payload": {**frame["payload"], **_room_route(self.qualification)},
            }
        # Native sender/creator/entryId/parentId/runId evidence stays intact.
        if len(json.dumps(frame, separators=(",", ":")).encode()) > 110_000:
            frame = {
                "type": "event",
                "event": "native.room.historyRequired",
                "payload": {"sessionKey": key, **_room_route(self.qualification)},
            }
        if not await self.management_api.send_message_async(self.identity.connection_id, frame):
            raise RoomError("ROOM_UNAVAILABLE", "Native room frontend disconnected")

    async def require_current(self):
        if qualification_for(self.identity.owner_id) != self.qualification:
            raise RoomError("ROOM_UNAVAILABLE", "Room qualification was revoked")
        from core.services.human_broker_provisioning import require_active_human_device

        try:
            await require_active_human_device(
                self.identity.owner_id,
                self.identity.alias,
                self.device.identity.device_id,
                self.device.profile_id,
            )
        except Exception as exc:
            raise RoomError("ROOM_UNAVAILABLE", "Human credential was revoked or is unavailable") from exc
        await require_current_member(self.identity, self.conn_svc)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.subscriptions.clear()
        self.presence_subscriptions.clear()
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
        token = container.get("gateway_token")
        if not isinstance(token, str) or not token:
            raise RoomError("ROOM_UNAVAILABLE", "Qualified room runtime authentication unavailable")
        await require_current_member(identity, self.conn_svc)
        async with self.lock:
            if self.closed:
                raise RoomError("ROOM_UNAVAILABLE", "Room pool is shutting down")
            existing = self.connections.get(identity)
            if existing and not existing.closed and existing.qualification == qualification and existing.token == token:
                return existing
            if existing:
                await existing.close()
            conn = NativeHumanConnection(identity, qualification, device, token, self.management_api, self.conn_svc)
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

    async def begin_run(self, owner_id: str, session_key: str, run_id: str) -> tuple[tuple[str, str, str], bool]:
        run = (owner_id, session_key, run_id)
        created = await self.conn_svc.begin_native_run(*run)
        self.run_holds.add(run)
        return run, created

    async def finish_run(self, owner_id: str, session_key: str, run_id: str) -> None:
        """Release only canonical terminal or explicit reconciliation evidence."""
        await _finish_native_run(self.conn_svc, self.run_holds, (owner_id, session_key, run_id))

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
