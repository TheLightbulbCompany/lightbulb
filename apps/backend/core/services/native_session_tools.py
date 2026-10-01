"""Device-attested, session-bound MCP tools for Claude Code and Codex on a paired Mac.

The model never supplies the source actor. The Mac binds the MCP call to its
verified native mirror; this door checks the current device pairing, session,
turn and target grants again before any durable write.
"""

from __future__ import annotations

import time
import uuid
import json
from typing import Any, Literal

from botocore.exceptions import ClientError
from pydantic import Field, model_validator

from core.auth import AuthContext
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo as sessions_repo, messaging_repo
from core.services import local_sessions, messaging
from core.services.machine_auth import MachineIdentity
from core.services.messaging import ApiProblem, ContractModel


class NativeToolRequest(ContractModel):
    source_session_id: uuid.UUID
    source_generation: str = Field(min_length=1, max_length=128)
    source_provider: Literal["claude", "codex"]
    source_conversation_id: str = Field(min_length=1, max_length=128)
    source_turn_id: str | None = Field(max_length=128)
    origin_item_id: str | None = Field(default=None, max_length=128)
    tool: Literal["list_targets", "send", "receipt"]
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.arguments, ensure_ascii=True).encode("utf-8")) > 8192:
            raise ValueError("Native tool arguments too large")
        return self


def _human(device: MachineIdentity) -> AuthContext:
    return AuthContext(user_id=device.user_id, org_id=device.org_id or None)


async def _source(device: MachineIdentity, request: NativeToolRequest) -> dict:
    sid = str(request.source_session_id)
    row = await sessions_repo.get_session(device.owner_id, sid)
    # Codex exposes an active turn in the mirror. Claude currently exposes only
    # the exact process/conversation binding verified by the Mac; its turn is
    # always null, so requiring one would make every Claude tool call fail.
    turn_current = (
        request.source_turn_id is not None and row.get("activeTurnId") == request.source_turn_id
        if row and request.source_provider == "codex"
        else request.source_turn_id is None and (row or {}).get("activeTurnId") is None
    )
    if (
        not row
        or row.get("humanUserId") != device.user_id
        or row.get("deviceId") != device.device_id
        or row.get("generation") != request.source_generation
        or row.get("provider") != request.source_provider
        or row.get("nativeConversationId") != request.source_conversation_id
        or row.get("unshared")
        or row.get("endedAt") is not None
        or int(row.get("onlineUntil") or 0) < int(time.time())
        or not turn_current
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native session source is not current.")
    # A stale or superseded relay connection cannot attest a new tool call.
    from core.services.local_session_relay import get_local_session_relay

    connection, _ = await get_local_session_relay().connect(row.get("connectionId") or "")
    if connection.get("device_id") != device.device_id or connection.get("user_id") != device.user_id:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native session source is not connected.")
    return row


async def _targets(auth: AuthContext, source_id: str) -> dict:
    rooms = (await messaging.list_rooms(auth)).rooms
    sessions = (await local_sessions.list_workspace_sessions(auth)).sessions
    people: dict[tuple[str, str], dict] = {}
    room_rows = []
    for room in [room for room in rooms if room.archived_at is None][:20]:
        room_rows.append(
            {"destination": f"room:{room.room_id}", "name": room.name or "Direct message", "kind": room.kind}
        )
        for member in room.members:
            if member.id == auth.user_id and member.kind == "human":
                continue
            people[(member.kind, member.id)] = {
                "kind": member.kind,
                "id": member.id,
                "name": member.display_name,
                "handle": member.mention_handle,
                "roomId": str(room.room_id),
                "destination": f"person:{member.id}",
            }
    session_rows = [
        {"destination": f"session:{s.session_id}", "provider": s.provider, "title": s.title, "status": s.status}
        for s in sessions
        if str(s.session_id) != source_id
        and (s.human_user_id == auth.user_id or s.team_control or auth.user_id in s.allowed_senders)
    ][:20]
    return {"rooms": room_rows, "people": list(people.values())[:20], "sessions": session_rows}


def _send_args(args: dict[str, Any]) -> tuple[str, str, str]:
    if set(args) != {"destination", "text", "requestId"}:
        raise ApiProblem("INVALID_REQUEST", "Send needs destination, text and requestId.")
    destination, text, request_id = (args[k] for k in ("destination", "text", "requestId"))
    if (
        not isinstance(destination, str)
        or len(destination) > 256
        or not destination.startswith(("room:", "person:", "session:"))
    ):
        raise ApiProblem("INVALID_REQUEST", "Invalid destination.")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 4096:
        raise ApiProblem("INVALID_REQUEST", "Message must be 1–4096 bytes.")
    try:
        request_id = str(uuid.UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ApiProblem("INVALID_REQUEST", "requestId must be a UUID.") from None
    return destination, text, request_id


async def _claim(
    owner: str, source: str, request_id: str, destination: str, text: str, turn_id: str, item_id: str | None
) -> dict:
    pk, sk = sessions_repo.session_pk(owner, source), f"NATIVE_TOOL#{request_id}"
    hashed = messaging_repo.request_hash(source, destination, text)
    item = {
        "pk": pk,
        "sk": sk,
        "requestHash": hashed,
        "destination": destination,
        "sourceTurnId": turn_id,
        "originItemId": item_id,
        "state": "pending",
        "createdAt": int(time.time()),
    }
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return item
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    prior = await messaging_repo._get(pk, sk)
    if prior is None or prior.get("requestHash") != hashed:
        raise ApiProblem("IDEMPOTENCY_CONFLICT", "requestId was used for another send.")
    return prior


async def _finish(owner: str, source: str, request_id: str, destination: str, result: dict) -> dict:
    pk, sk = sessions_repo.session_pk(owner, source), f"NATIVE_TOOL#{request_id}"
    try:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET #state = :state, result = :result",
            ConditionExpression="destination = :destination AND attribute_not_exists(result)",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":state": result["state"], ":result": result, ":destination": destination},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        prior = await messaging_repo._get(pk, sk)
        if prior is None or prior.get("destination") != destination or not prior.get("result"):
            raise ApiProblem("IDEMPOTENCY_CONFLICT", "Native tool receipt changed.") from exc
        return prior["result"]
    return result


async def _room_send(auth: AuthContext, source: dict, room_id: str, text: str, request_id: str) -> dict:
    from core.services.local_session_relay import get_local_session_relay

    try:
        room_id = str(uuid.UUID(room_id))
    except ValueError:
        raise ApiProblem("INVALID_REQUEST", "Invalid room destination.") from None
    members = await messaging_repo.list_members(room_id)
    decorated = messaging.with_human_handles(members, await messaging.human_emails(members))
    mentions, unresolved = messaging.parse_agent_mentions(text, decorated)
    if unresolved:
        raise ApiProblem("INVALID_REQUEST", "A mentioned person or agent is not uniquely in this room.")
    relay = get_local_session_relay()
    connection_id = source.get("connectionId") or ""
    connection, enrollment = await relay.connect(connection_id)
    if connection.get("device_id") != source["deviceId"] or connection.get("user_id") != source["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native session source is not connected.")
    conditions = [
        sessions_repo.pairing_condition(source["ownerId"], source["humanUserId"], source["deviceId"], None),
        sessions_repo.enrollment_condition(enrollment),
        relay._session_check(source),
        relay._connection_check(connection_id, connection),
    ]
    message = await messaging.create_human_message(
        auth,
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.UUID(request_id),
            text=text,
            mentions=[messaging.MemberRef(**m) for m in mentions],
            session_mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        native_source_id=source["sessionId"],
        native_provider=source["provider"],
        native_hop=await _outgoing_hop(source),
        native_conditions=conditions,
    )
    return {"state": "applied", "destination": f"room:{room_id}", "messageId": str(message.message_id)}


async def _session_send(auth: AuthContext, source: dict, session_id: str, text: str, request_id: str) -> dict:
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        raise ApiProblem("INVALID_REQUEST", "Invalid session destination.") from None
    if session_id == source["sessionId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "A native session cannot message itself.")
    target, _ = await local_sessions.authorize_session(auth, None, session_id, owner_only=True)
    command = local_sessions.LocalSessionCommand(
        request_id=request_id, expected_generation=target["generation"], kind="send", text=text
    )
    receipt = await local_sessions.submit_command(
        auth,
        None,
        session_id,
        command,
        source_session_id=source["sessionId"],
        source_generation=source["generation"],
        native_hop=await _outgoing_hop(source),
    )
    from core.services.local_session_relay import get_local_session_relay

    try:
        await get_local_session_relay().dispatch_session(source["ownerId"], session_id)
    except Exception:
        pass  # Durable admission stays queued; the device heartbeat retries delivery.
    return {"state": receipt.state, "destination": f"session:{session_id}", "targetRequestId": request_id}


async def _outgoing_hop(source: dict) -> int:
    commands = await sessions_repo.list_commands(source["ownerId"], source["sessionId"])
    last_human = max((int(c["commandSeq"]) for c in commands if c.get("actorUserId")), default=0)
    # ponytail: direct terminal prompts do not yet reset this cap. A human's
    # Lightbulb follow-up does; refine the native turn lineage if it blocks use.
    incoming = max(
        (
            int(c.get("nativeHop") or 1)
            for c in commands
            if (c.get("actor") or {}).get("kind") == "session"
            and c.get("state") in {"dispatched", "applied", "unknown"}
            and int(c["commandSeq"]) > last_human
        ),
        default=0,
    )
    if incoming >= 2:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native session reply loop limit reached.")
    return incoming + 1


async def execute(device: MachineIdentity, request: NativeToolRequest) -> dict:
    source = await _source(device, request)
    source_id = str(request.source_session_id)
    auth = _human(device)
    args = request.arguments
    if request.tool == "list_targets":
        if args:
            raise ApiProblem("INVALID_REQUEST", "list_targets takes no arguments.")
        return await _targets(auth, source_id)
    if request.tool == "receipt":
        if set(args) != {"requestId"}:
            raise ApiProblem("INVALID_REQUEST", "receipt needs requestId.")
        try:
            request_id = str(uuid.UUID(args["requestId"]))
        except (ValueError, TypeError, AttributeError):
            raise ApiProblem("INVALID_REQUEST", "Invalid requestId.") from None
        row = await messaging_repo._get(
            sessions_repo.session_pk(device.owner_id, source_id), f"NATIVE_TOOL#{request_id}"
        )
        if row is None:
            return {"state": "unknown", "retry": "Use the same requestId, destination and text."}
        result = row.get("result") or {"state": "pending"}
        if result.get("targetRequestId"):
            target = result["destination"].removeprefix("session:")
            command = await sessions_repo.get_command(device.owner_id, target, result["targetRequestId"])
            if command:
                result = {**result, "state": command["state"]}
        return result
    destination, text, request_id = _send_args(args)
    row = await _claim(
        device.owner_id, source_id, request_id, destination, text, request.source_turn_id, request.origin_item_id
    )
    if prior := row.get("result"):
        if prior.get("state") == "rejected":
            error = prior["error"]
            raise ApiProblem(error["code"], error["message"])
        return prior
    try:
        if destination.startswith("session:"):
            result = await _session_send(auth, source, destination.removeprefix("session:"), text, request_id)
        else:
            if destination.startswith("person:"):
                member_id = destination.removeprefix("person:")
                rooms = (await messaging.list_rooms(auth)).rooms
                match = next((r for r in rooms if r.kind == "dm" and any(m.id == member_id for m in r.members)), None)
                if match is None:
                    raise ApiProblem("ROOM_NOT_FOUND", "Open a direct message with this person first.")
                destination = f"room:{match.room_id}"
            result = await _room_send(auth, source, destination.removeprefix("room:"), text, request_id)
    except ApiProblem as exc:
        if not exc.error.retryable:
            persisted = await _finish(
                device.owner_id,
                source_id,
                request_id,
                row["destination"],
                {"state": "rejected", "destination": row["destination"], "error": exc.body()},
            )
            if persisted["state"] != "rejected":
                return persisted
        raise
    return await _finish(device.owner_id, source_id, request_id, row["destination"], result)
