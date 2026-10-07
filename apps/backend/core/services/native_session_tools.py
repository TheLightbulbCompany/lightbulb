"""Device-attested, session-bound MCP tools for Claude Code and Codex on a paired Mac.

The model never supplies the source actor. The Mac binds the MCP call to its
verified native mirror; this door checks the current device pairing, session,
turn and target grants again before any durable write.
"""

from __future__ import annotations

import time
import uuid
import json
import logging
from typing import Any, Literal

from botocore.exceptions import ClientError
from pydantic import Field, model_validator

from core.auth import AuthContext
from core.dynamodb import run_in_thread
from core.observability.session_analytics import capture_session_event
from core.repositories import local_sessions_repo as sessions_repo, messaging_repo
from core.services import local_sessions, messaging
from core.services.machine_auth import MachineIdentity
from core.services.messaging import ApiProblem, ContractModel

logger = logging.getLogger(__name__)


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


class NativeDecisionRequest(ContractModel):
    """The owner's own Mac answers from the Claude terminal (spec 2026-10-04-session-sharing-mod §4.3)."""

    session_id: uuid.UUID
    generation: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "decline", "pause", "unpause"]
    user_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def names_one_request(self):
        answers = self.decision in ("allow", "decline")
        if answers != (self.user_id is not None) or answers != (self.request_id is not None):
            raise ValueError("allow and decline name one request; pause and unpause name none")
        return self


async def decide(device: MachineIdentity, body: NativeDecisionRequest) -> local_sessions.LocalSession:
    """The one write a device token reaches (owner approval 2026-10-04): only for the owner's own
    session on this Mac, at its current generation, while this Mac holds its relay connection.
    The Mac freezes remote typing while a request is pending, so only the owner can press it."""
    sid = str(body.session_id)
    row = await sessions_repo.get_session(device.owner_id, sid)
    if (
        not row
        or row.get("humanUserId") != device.user_id
        or row.get("deviceId") != device.device_id
        or row.get("generation") != body.generation
        or row.get("endedAt") is not None
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not this Mac's current session.")
    from core.services.local_session_relay import get_local_session_relay

    connection, _ = await get_local_session_relay().connect(row.get("connectionId") or "")
    if connection.get("device_id") != device.device_id or connection.get("user_id") != device.user_id:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This Mac is not connected.")
    auth = _human(device)
    if body.decision in ("pause", "unpause"):
        session = await local_sessions.set_sharing(auth, sid, body.decision == "unpause", generation=body.generation)
        from core.services.local_session_relay import publish_resources_changed

        # Same notice the web sharing route sends, so other clients' session views refresh.
        await publish_resources_changed(device.owner_id, device.user_id)
        return session
    # Join and pick-up requests share one presence list; the request id says which one this answers.
    if local_sessions.pending_pickups(row).get(body.user_id) == body.request_id:
        return await local_sessions.decide_pickup_request(
            auth, sid, body.user_id, body.decision, request_id=body.request_id
        )
    return await local_sessions.decide_join(auth, sid, body.user_id, body.decision, request_id=body.request_id)


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


def sendable(session: local_sessions.LocalSession, user_id: str) -> bool:
    """A live, verified session this person may send a follow-up into (both MCP servers list this)."""
    return (
        session.status in local_sessions.LIVE_STATUSES
        and session.capabilities_verified
        and session.control_unavailable_reason is None
        and session.capabilities.send_mode != "none"
        and (session.human_user_id == user_id or session.team_control or user_id in session.allowed_senders)
    )


async def _targets(auth: AuthContext, source_id: str) -> dict:
    rooms = (await messaging.list_rooms(auth)).rooms
    sessions = (await local_sessions.list_workspace_sessions(auth)).sessions
    people: dict[tuple[str, str], dict] = {}
    active_rooms = [room for room in rooms if room.archived_at is None]
    room_rows = [
        {"destination": f"room:{room.room_id}", "name": room.name or "Direct message", "kind": room.kind}
        for room in active_rooms[:20]
    ]
    for room in active_rooms:
        if room.kind != "dm":
            continue
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
        if len(people) >= 20:
            break
    session_rows = [
        {"destination": f"session:{s.session_id}", "provider": s.provider, "title": s.title, "status": s.status}
        for s in sessions
        if str(s.session_id) != source_id and sendable(s, auth.user_id)
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
    owner: str, source: str, request_id: str, destination: str, text: str, turn_id: str | None, item_id: str | None
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
            UpdateExpression="SET #state = :state, #result = :result",
            ConditionExpression="destination = :destination AND attribute_not_exists(#result)",
            ExpressionAttributeNames={"#state": "state", "#result": "result"},
            ExpressionAttributeValues={":state": result["state"], ":result": result, ":destination": destination},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        prior = await messaging_repo._get(pk, sk)
        if prior is None or prior.get("destination") != destination or not prior.get("result"):
            raise ApiProblem("IDEMPOTENCY_CONFLICT", "Native tool receipt changed.") from exc
        if result["state"] != "rejected" and prior["result"]["state"] == "rejected":
            # An overlapping attempt may have failed after the other committed
            # downstream. A confirmed send wins over its earlier error receipt.
            try:
                await run_in_thread(
                    messaging_repo._table().update_item,
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET #state = :state, #result = :result",
                    ConditionExpression="destination = :destination AND #state = :rejected",
                    ExpressionAttributeNames={"#state": "state", "#result": "result"},
                    ExpressionAttributeValues={
                        ":state": result["state"],
                        ":result": result,
                        ":destination": destination,
                        ":rejected": "rejected",
                    },
                )
                await _capture_send_result(owner, source, request_id, destination, result)
                return result
            except ClientError as changed:
                if changed.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                latest = await messaging_repo._get(pk, sk)
                if latest and latest.get("result"):
                    return latest["result"]
                raise
        return prior["result"]
    await _capture_send_result(owner, source, request_id, destination, result)
    return result


async def _capture_send_result(owner: str, source: str, request_id: str, destination: str, result: dict) -> None:
    try:
        row = await sessions_repo.get_session(owner, source)
        if row:
            capture_session_event(
                row,
                "native_send_outcome",
                f"{request_id}:{result['state']}",
                request_id=request_id,
                origin="native",
                state=result["state"],
                destination_kind=destination.split(":", 1)[0],
            )
    except Exception:
        logger.warning("native send analytics capture failed", exc_info=True)


async def _committed_result(owner: str, source: str, destination: str, request_id: str) -> dict | None:
    """Strong-read the downstream idempotency row before settling an error."""
    if destination.startswith("session:"):
        target = destination.removeprefix("session:")
        command = await sessions_repo.get_command(owner, target, request_id)
        if command and command.get("actor") == {"kind": "session", "id": source}:
            return {"state": command["state"], "destination": destination, "targetRequestId": request_id}
    elif destination.startswith("room:"):
        room_id = destination.removeprefix("room:")
        receipt = await messaging_repo._get(
            messaging_repo.room_pk(room_id), messaging_repo.agent_idemp_sk(f"native:{source}:{request_id}")
        )
        if receipt:
            return {"state": "applied", "destination": destination, "messageId": receipt["messageId"]}
    return None


async def _committed_legacy(
    owner: str, source: str, destination: str, request_id: str, auth: AuthContext
) -> dict | None:
    if destination.startswith("person:"):
        try:
            destination = await _resolve_destination(auth, destination)
        except ApiProblem:
            return None
    return await _committed_result(owner, source, destination, request_id)


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
        sessions_repo.native_source_condition(source),
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
    from core.services.local_session_relay import get_local_session_relay

    relay = get_local_session_relay()
    connection_id = source.get("connectionId") or ""
    connection, enrollment = await relay.connect(connection_id)
    if connection.get("device_id") != source["deviceId"] or connection.get("user_id") != source["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Native session source is not connected.")
    target, _ = await local_sessions.authorize_session(auth, None, session_id, owner_only=True)
    source_conditions = [
        sessions_repo.native_source_condition(source),
        relay._connection_check(connection_id, connection),
    ]
    # admit_command already checks the target's pairing and enrollment. A
    # DynamoDB transaction cannot check that same item twice on one Mac.
    if (target["humanUserId"], target["deviceId"]) != (source["humanUserId"], source["deviceId"]):
        source_conditions.extend(
            [
                sessions_repo.enrollment_condition(enrollment),
            ]
        )
    return await session_send(
        auth,
        target,
        text,
        request_id,
        source_session_id=source["sessionId"],
        source_generation=source["generation"],
        source_turn_id=source.get("activeTurnId"),
        native_hop=await _outgoing_hop(source),
        source_conditions=source_conditions,
        source_pairing=(source["humanUserId"], source["deviceId"]),
    )


async def session_send(auth: AuthContext, target: dict, text: str, request_id: str, **source: Any) -> dict:
    """One follow-up into an authorized target session; the native and hosted MCP sends both land here."""
    from core.services.local_session_relay import get_local_session_relay

    session_id = target["sessionId"]
    if target.get("controlUnavailableReason"):
        # The Mac failed control closed; find_sessions says canSend false, and admission agrees.
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Sending is unavailable for this session.")
    command = local_sessions.LocalSessionCommand(
        request_id=request_id, expected_generation=target["generation"], kind="send", text=text
    )
    receipt = await local_sessions.submit_command(auth, None, session_id, command, **source)
    try:
        await get_local_session_relay().dispatch_session(target["ownerId"], session_id)
    except Exception:
        # Durable admission stays queued; the device heartbeat retries delivery.
        logger.warning("session send dispatch failed session=%s", session_id, exc_info=True)
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


async def _resolve_destination(auth: AuthContext, destination: str) -> str:
    if not destination.startswith("person:"):
        return destination
    return f"room:{await person_dm(auth, destination.removeprefix('person:'))}"


async def person_dm(auth: AuthContext, member_id: str, *, create: bool = False) -> str:
    """The caller's DM room with this member. A native session needs it open already; the
    hosted MCP opens one with a workspace person (``create``), the same find-or-create the web uses."""
    if member_id == auth.user_id:
        raise ApiProblem("INVALID_REQUEST", "Cannot message your own person destination.")
    rooms = (await messaging.list_rooms(auth)).rooms
    match = next((r for r in rooms if r.kind == "dm" and any(m.id == member_id for m in r.members)), None)
    if match is not None:
        return str(match.room_id)
    if not create:
        raise ApiProblem("ROOM_NOT_FOUND", "Open a direct message with this person first.")
    # People are Clerk ids; anything else is an agent, which create_room checks against the roster.
    counterpart = (
        messaging.CreateRoomMember(kind="human", id=member_id, role="member", listen_mode=None)
        if member_id.startswith("user_")
        else messaging.CreateRoomMember(kind="agent", id=member_id, role="member", listen_mode="active")
    )
    room = await messaging.create_room(
        auth,
        messaging.CreateRoomRequest(
            # Deterministic, so a retried send cannot open a second DM.
            client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-dm:{auth.user_id}:{member_id}"),
            name=None,
            kind="dm",
            members=[counterpart],
        ),
    )
    return str(room.room_id)


async def _dispatch_native(
    auth: AuthContext,
    source: dict,
    destination: str,
    text: str,
    request_id: str,
) -> dict:
    if destination.startswith("session:"):
        return await _session_send(auth, source, destination.removeprefix("session:"), text, request_id)
    return await _room_send(auth, source, destination.removeprefix("room:"), text, request_id)


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
        result = row.get("result") or {"state": row.get("state", "pending")}
        if result["state"] == "pending" or (result["state"] == "rejected" and result.get("error")):
            resolved = result.get("resolvedDestination") or row.get("resolvedDestination") or row["destination"]
            committed = await _committed_legacy(device.owner_id, source_id, resolved, request_id, auth)
            if committed:
                result = await _finish(device.owner_id, source_id, request_id, row["destination"], committed)
        if result.get("targetRequestId"):
            target = result["destination"].removeprefix("session:")
            command = await sessions_repo.get_command(device.owner_id, target, result["targetRequestId"])
            if command:
                result = {**result, "state": command["state"]}
        return result
    # Sharing the session is the authorization; the provider's own tool prompt
    # gates each call (owner ruling 2026-10-02). A send posts immediately.
    destination, text, request_id = _send_args(args)
    row = await _claim(
        device.owner_id, source_id, request_id, destination, text, request.source_turn_id, request.origin_item_id
    )
    destination = row.get("resolvedDestination") or destination
    if prior := row.get("result"):
        if prior.get("state") == "rejected" and prior.get("error"):
            committed = await _committed_legacy(
                device.owner_id, source_id, prior.get("resolvedDestination") or destination, request_id, auth
            )
            if committed:
                return await _finish(device.owner_id, source_id, request_id, row["destination"], committed)
        return prior  # a replay reports its stored receipt, refusals included
    try:
        if destination.startswith("person:"):
            # Pin the DM before posting: a retry must not re-resolve to a different room.
            resolved = await _resolve_destination(auth, destination)
            pinned = await run_in_thread(
                messaging_repo._table().update_item,
                Key={"pk": row["pk"], "sk": row["sk"]},
                UpdateExpression="SET resolvedDestination = if_not_exists(resolvedDestination, :destination)",
                ExpressionAttributeValues={":destination": resolved},
                ReturnValues="ALL_NEW",
            )
            destination = pinned["Attributes"]["resolvedDestination"]
        result = await _dispatch_native(auth, source, destination, text, request_id)
    except ApiProblem as exc:
        if not exc.error.retryable:
            committed = await _committed_result(device.owner_id, source_id, destination, request_id)
            if committed:
                return await _finish(device.owner_id, source_id, request_id, row["destination"], committed)
            persisted = await _finish(
                device.owner_id,
                source_id,
                request_id,
                row["destination"],
                {
                    "state": "rejected",
                    "destination": row["destination"],
                    "resolvedDestination": destination,
                    "error": exc.body(),
                },
            )
            if persisted["state"] != "rejected":
                return persisted
        raise
    return await _finish(device.owner_id, source_id, request_id, row["destination"], result)
