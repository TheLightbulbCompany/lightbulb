"""Device-attested, session-bound MCP tools for Claude Code and Codex on a paired Mac.

The model never supplies the source actor. The Mac binds the MCP call to its
verified native mirror; this door checks the current device pairing, session,
turn and target grants again before any durable write.
"""

from __future__ import annotations

import time
import uuid
import json
import hashlib
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


INTENT_SECONDS = 55 * 60


class NativeSendDecision(ContractModel):
    request_id: uuid.UUID
    approval_id: str = Field(pattern=r"^appr-[a-f0-9]{32}$")
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["allow", "deny", "cancel"]
    reason: str | None = Field(default=None, max_length=500)


def _intent_digest(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _intent_key(request_id: str) -> str:
    return f"NATIVE_INTENT#{request_id}"


def _tool_key(request_id: str) -> str:
    return f"NATIVE_TOOL#{request_id}"


async def _publish_session(owner: str, human: str) -> None:
    from core.services.local_session_relay import publish_resources_changed

    await publish_resources_changed(owner, human)


async def prepare_codex_send(device: MachineIdentity, request: NativeToolRequest) -> dict:
    """Park one exact, device-attested native send without touching its destination."""
    if request.tool != "send":
        raise ApiProblem("INVALID_REQUEST", "Only sends use this approval door.")
    destination, text, request_id = _send_args(request.arguments)
    source_id = str(request.source_session_id)
    pk = sessions_repo.session_pk(device.owner_id, source_id)
    request_hash = messaging_repo.request_hash(source_id, destination, text)
    digest = _intent_digest({"destination": destination, "text": text, "requestId": request_id})
    for _ in range(6):
        source = await _source(device, request)
        prior = await messaging_repo._get(pk, _tool_key(request_id))
        if prior:
            if prior.get("requestHash") != request_hash or prior.get("inputDigest") != digest:
                raise ApiProblem("IDEMPOTENCY_CONFLICT", "requestId was used for another send.")
            if prior.get("result"):
                await _clear_codex_intent(device.owner_id, source_id, prior["approvalId"])
                return prior["result"]
            pending = source.get("nativeSendIntent") or {}
            if pending.get("requestId") == request_id and pending.get("approvalId") == prior.get("approvalId"):
                if pending.get("expiresAt", 0) <= int(time.time()):
                    return {"state": "expired", "requestId": request_id}
                if pending.get("connectionId") != source.get("connectionId") or pending.get(
                    "sourceGeneration"
                ) != source.get("generation"):
                    return {"state": "offline", "requestId": request_id}
                return {
                    "state": pending["state"],
                    "requestId": request_id,
                    "approvalId": pending["approvalId"],
                    "expiresAt": pending["expiresAt"],
                }
            return {
                "state": "expired" if prior["approvalExpiresAt"] <= int(time.time()) else "cancelled",
                "requestId": request_id,
            }
        now = int(time.time())
        pending = source.get("nativeSendIntent") or {}
        if (
            pending.get("expiresAt", 0) > now
            and pending.get("connectionId") == source.get("connectionId")
            and pending.get("sourceGeneration") == source.get("generation")
        ):
            older = await messaging_repo._get(pk, _tool_key(pending["requestId"]))
            if older is None or not older.get("result"):
                raise ApiProblem("LOCAL_SESSION_CONFLICT", "Answer or cancel the pending native send first.")
        enrollment = await sessions_repo.get_enrollment(device.owner_id, device.user_id, device.device_id)
        if enrollment is None:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "The Mac is no longer paired.")
        approval_id = f"appr-{uuid.uuid4().hex}"
        expires = now + INTENT_SECONDS
        pointer = {
            "requestId": request_id,
            "approvalId": approval_id,
            "inputDigest": digest,
            "expiresAt": expires,
            "state": "pending_approval",
            "connectionId": source["connectionId"],
            "sourceGeneration": source["generation"],
            "sourceTurnId": request.source_turn_id,
        }
        updated = {
            **source,
            "revision": str(uuid.uuid4()),
            "nativeSendIntent": pointer,
        }
        claim = {
            "pk": pk,
            "sk": _tool_key(request_id),
            "requestHash": request_hash,
            "destination": destination,
            "sourceTurnId": request.source_turn_id,
            "originItemId": request.origin_item_id,
            "state": "pending_approval",
            "approvalId": approval_id,
            "inputDigest": digest,
            "approvalExpiresAt": expires,
            "createdAt": now,
        }
        intent = {
            "pk": pk,
            "sk": _intent_key(request_id),
            "approvalId": approval_id,
            "inputDigest": digest,
            "requestId": request_id,
            "destination": destination,
            "text": text,
            "sourceGeneration": source["generation"],
            "sourceConversationId": source["nativeConversationId"],
            "deviceId": source["deviceId"],
            "humanUserId": source["humanUserId"],
            "sourceTurnId": request.source_turn_id,
            "originItemId": request.origin_item_id,
            "connectionId": source["connectionId"],
            "state": "pending_approval",
            "createdAt": now,
            "expiresAt": expires,
        }
        audit = local_sessions.audit_row(device.owner_id, source_id, None, "native_send_requested")
        try:
            await sessions_repo.put_session(
                updated,
                source,
                enrollment,
                extra=[
                    {
                        "Put": {
                            "TableName": messaging_repo._table().name,
                            "Item": claim,
                            "ConditionExpression": "attribute_not_exists(sk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": messaging_repo._table().name,
                            "Item": intent,
                            "ConditionExpression": "attribute_not_exists(sk)",
                        }
                    },
                    {"Put": {"TableName": messaging_repo._table().name, "Item": audit}},
                ],
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await _publish_session(device.owner_id, device.user_id)
        return {"state": "pending_approval", "requestId": request_id, "approvalId": approval_id, "expiresAt": expires}
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry the same requestId.")


async def _clear_codex_intent(owner: str, source_id: str, approval_id: str) -> None:
    """Drop the private card after a settled receipt; concurrent relay writes use CAS."""
    for _ in range(6):
        row = await sessions_repo.get_session(owner, source_id)
        if row is None or (row.get("nativeSendIntent") or {}).get("approvalId") != approval_id:
            return
        enrollment = await sessions_repo.get_enrollment(owner, row["humanUserId"], row["deviceId"])
        if enrollment is None:
            return
        updated = {**row, "revision": str(uuid.uuid4())}
        updated.pop("nativeSendIntent", None)
        if (updated.get("pendingApproval") or {}).get("approvalId") == approval_id:
            updated.pop("pendingApproval", None)
        try:
            await sessions_repo.put_session(updated, row, enrollment)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await _publish_session(owner, row["humanUserId"])
        return


async def _settle_codex_result(owner: str, source_id: str, request_id: str, claim: dict, result: dict) -> dict:
    """Persist a recovered downstream commit before releasing the next send slot."""
    persisted = await _finish(owner, source_id, request_id, claim["destination"], result)
    await _clear_codex_intent(owner, source_id, claim["approvalId"])
    try:
        await run_in_thread(
            messaging_repo._table().delete_item,
            Key={"pk": sessions_repo.session_pk(owner, source_id), "sk": _intent_key(request_id)},
        )
    except Exception:
        pass  # The private row has a 55-minute TTL; the durable receipt is authoritative.
    return persisted


async def get_codex_send_receipt(auth: AuthContext, session_id: str, request_id: str) -> dict:
    row, _ = await local_sessions.authorize_session(auth, None, session_id)
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only this session's owner can read its send receipt.")
    try:
        request_id = str(uuid.UUID(request_id))
    except ValueError:
        raise ApiProblem("INVALID_REQUEST", "Invalid requestId.") from None
    source_id = row["sessionId"]
    claim = await messaging_repo._get(sessions_repo.session_pk(row["ownerId"], source_id), _tool_key(request_id))
    if claim is None or not claim.get("approvalId"):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Send receipt not found.")
    if claim.get("result"):
        result = claim["result"]
        if result.get("state") == "rejected" and result.get("error", {}).get("code") not in {
            "OWNER_DENIED",
            "CANCELLED",
        }:
            committed = await _committed_result(
                row["ownerId"], source_id, result.get("resolvedDestination") or claim["destination"], request_id
            )
            if committed:
                result = committed
        result = await _settle_codex_result(row["ownerId"], source_id, request_id, claim, result)
        if result.get("targetRequestId"):
            command = await sessions_repo.get_command(
                row["ownerId"], result["destination"].removeprefix("session:"), result["targetRequestId"]
            )
            if command:
                result = {**result, "state": command["state"]}
        return {"requestId": request_id, **result}
    intent = await messaging_repo._get(sessions_repo.session_pk(row["ownerId"], source_id), _intent_key(request_id))
    resolved = (intent or {}).get("resolvedDestination") or claim["destination"]
    committed = await _committed_result(row["ownerId"], source_id, resolved, request_id)
    if committed:
        result = await _settle_codex_result(row["ownerId"], source_id, request_id, claim, committed)
        return {"requestId": request_id, **result}
    pointer = row.get("nativeSendIntent") or {}
    if claim["approvalExpiresAt"] <= int(time.time()):
        state = "expired"
    elif (
        pointer.get("requestId") != request_id
        or pointer.get("sourceGeneration") != row.get("generation")
        or row.get("endedAt") is not None
        or row.get("unshared")
    ):
        state = "cancelled"
    elif pointer.get("connectionId") != row.get("connectionId") or row.get("onlineUntil", 0) <= int(time.time()):
        state = "offline"
    else:
        state = pointer.get("state", "pending_approval")
    return {"requestId": request_id, "state": state}


async def answer_codex_send(auth: AuthContext, session_id: str, decision: NativeSendDecision) -> dict:
    """Consume an owner's exact approval before any native message is committed."""
    request_id = str(decision.request_id)
    for _ in range(6):
        source, enrollment = await local_sessions.authorize_session(auth, None, session_id)
        if auth.user_id != source["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only this session's owner can answer its send.")
        owner = source["ownerId"]
        source_id = source["sessionId"]
        pk = sessions_repo.session_pk(owner, source_id)
        claim = await messaging_repo._get(pk, _tool_key(request_id))
        if (
            claim is None
            or claim.get("approvalId") != decision.approval_id
            or claim.get("inputDigest") != decision.input_digest
        ):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That native send has changed.")
        if claim.get("result"):
            await _clear_codex_intent(owner, source_id, decision.approval_id)
            return {"requestId": request_id, **claim["result"]}
        pointer = source.get("nativeSendIntent") or {}
        intent = await messaging_repo._get(pk, _intent_key(request_id))
        if (
            pointer.get("requestId") != request_id
            or pointer.get("approvalId") != decision.approval_id
            or pointer.get("inputDigest") != decision.input_digest
            or intent is None
            or intent.get("approvalId") != decision.approval_id
            or intent.get("inputDigest") != decision.input_digest
        ):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That native send is no longer pending.")
        if (
            intent.get("requestId") != request_id
            or intent.get("connectionId") != pointer.get("connectionId")
            or intent.get("sourceGeneration") != pointer.get("sourceGeneration")
            or intent.get("sourceTurnId") != pointer.get("sourceTurnId")
            or intent.get("sourceTurnId") != claim.get("sourceTurnId")
            or intent.get("originItemId") != claim.get("originItemId")
            or intent.get("deviceId") != source["deviceId"]
            or intent.get("humanUserId") != source["humanUserId"]
        ):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The native source changed.")
        if (
            _intent_digest({"destination": intent["destination"], "text": intent["text"], "requestId": request_id})
            != decision.input_digest
            or messaging_repo.request_hash(source_id, intent["destination"], intent["text"]) != claim["requestHash"]
        ):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The exact native send changed.")
        now = int(time.time())
        if min(pointer["expiresAt"], intent["expiresAt"]) <= now:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That native send expired.")
        if decision.decision != "allow":
            if pointer["state"] != "pending_approval":
                raise ApiProblem("LOCAL_SESSION_CONFLICT", "That native send is already dispatching.")
            result = {
                "state": "cancelled" if decision.decision == "cancel" else "rejected",
                "destination": intent["destination"],
                "error": {
                    "code": "CANCELLED" if decision.decision == "cancel" else "OWNER_DENIED",
                    "message": decision.reason
                    or (
                        "The owner cancelled this send."
                        if decision.decision == "cancel"
                        else "The owner denied this send."
                    ),
                },
            }
            updated = {**source, "revision": str(uuid.uuid4())}
            updated.pop("nativeSendIntent", None)
            if (updated.get("pendingApproval") or {}).get("approvalId") == decision.approval_id:
                updated.pop("pendingApproval", None)
            audit = local_sessions.audit_row(
                owner,
                source_id,
                auth,
                "native_send_cancelled" if decision.decision == "cancel" else "native_send_denied",
            )
            try:
                await sessions_repo.put_session(
                    updated,
                    source,
                    enrollment,
                    extra=[
                        {
                            "Update": {
                                "TableName": messaging_repo._table().name,
                                "Key": {"pk": pk, "sk": _tool_key(request_id)},
                                "UpdateExpression": "SET #state = :state, result = :result",
                                "ConditionExpression": "approvalId = :approval AND inputDigest = :digest AND attribute_not_exists(result)",
                                "ExpressionAttributeNames": {"#state": "state"},
                                "ExpressionAttributeValues": {
                                    ":state": result["state"],
                                    ":result": result,
                                    ":approval": decision.approval_id,
                                    ":digest": decision.input_digest,
                                },
                            }
                        },
                        {
                            "Delete": {
                                "TableName": messaging_repo._table().name,
                                "Key": {"pk": pk, "sk": _intent_key(request_id)},
                                "ConditionExpression": "approvalId = :approval",
                                "ExpressionAttributeValues": {":approval": decision.approval_id},
                            }
                        },
                        {"Put": {"TableName": messaging_repo._table().name, "Item": audit}},
                    ],
                )
            except ClientError as exc:
                if not messaging_repo._is_cancelled(exc):
                    raise
                continue
            await _publish_session(owner, source["humanUserId"])
            return {"requestId": request_id, **result}
        if (
            source.get("endedAt") is not None
            or source.get("unshared")
            or source.get("onlineUntil", 0) <= now
            or source.get("connectionId") != pointer["connectionId"]
            or source["generation"] != intent["sourceGeneration"]
            or source["nativeConversationId"] != intent["sourceConversationId"]
        ):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The Mac or shared session changed; no message was sent.")
        from core.services.local_session_relay import get_local_session_relay

        relay = get_local_session_relay()
        try:
            connection, _ = await relay.connect(pointer["connectionId"])
        except Exception:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The Mac is offline; no message was sent.") from None
        if connection.get("device_id") != source["deviceId"] or connection.get("user_id") != source["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "The Mac connection changed; no message was sent.")
        if pointer["state"] == "pending_approval":
            updated = {**source, "revision": str(uuid.uuid4()), "nativeSendIntent": {**pointer, "state": "dispatching"}}
            audit = local_sessions.audit_row(owner, source_id, auth, "native_send_approved")
            try:
                await sessions_repo.put_session(
                    updated,
                    source,
                    enrollment,
                    extra=[
                        relay._connection_check(pointer["connectionId"], connection),
                        {
                            "Update": {
                                "TableName": messaging_repo._table().name,
                                "Key": {"pk": pk, "sk": _tool_key(request_id)},
                                "UpdateExpression": "SET #state = :dispatching",
                                "ConditionExpression": "#state = :pending AND approvalId = :approval AND inputDigest = :digest AND approvalExpiresAt > :now",
                                "ExpressionAttributeNames": {"#state": "state"},
                                "ExpressionAttributeValues": {
                                    ":dispatching": "dispatching",
                                    ":pending": "pending_approval",
                                    ":approval": decision.approval_id,
                                    ":digest": decision.input_digest,
                                    ":now": now,
                                },
                            }
                        },
                        {
                            "Update": {
                                "TableName": messaging_repo._table().name,
                                "Key": {"pk": pk, "sk": _intent_key(request_id)},
                                "UpdateExpression": "SET #state = :dispatching",
                                "ConditionExpression": "#state = :pending AND approvalId = :approval AND inputDigest = :digest AND expiresAt > :now",
                                "ExpressionAttributeNames": {"#state": "state"},
                                "ExpressionAttributeValues": {
                                    ":dispatching": "dispatching",
                                    ":pending": "pending_approval",
                                    ":approval": decision.approval_id,
                                    ":digest": decision.input_digest,
                                    ":now": now,
                                },
                            }
                        },
                        {"Put": {"TableName": messaging_repo._table().name, "Item": audit}},
                    ],
                )
            except ClientError as exc:
                if not messaging_repo._is_cancelled(exc):
                    raise
                continue
            source = updated
        elif pointer["state"] != "dispatching" or decision.decision != "allow":
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "That native send is unavailable.")
        resolved = intent.get("resolvedDestination") or intent["destination"]
        committed = await _committed_result(owner, source_id, resolved, request_id)
        if committed:
            result = committed
        else:
            destination = resolved
            try:
                if destination.startswith("person:"):
                    destination = await _resolve_destination(auth, destination)
                    await run_in_thread(
                        messaging_repo._table().update_item,
                        Key={"pk": pk, "sk": _intent_key(request_id)},
                        UpdateExpression="SET resolvedDestination = :destination",
                        ConditionExpression="approvalId = :approval AND #state = :dispatching AND expiresAt > :now",
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues={
                            ":destination": destination,
                            ":approval": decision.approval_id,
                            ":dispatching": "dispatching",
                            ":now": int(time.time()),
                        },
                    )
                committed = await _committed_result(owner, source_id, destination, request_id)
                if committed:
                    result = committed
                else:
                    send_source = {**source, "activeTurnId": intent["sourceTurnId"]}
                    result = await _dispatch_native(
                        auth, send_source, destination, intent["text"], request_id, approval_id=decision.approval_id
                    )
            except ApiProblem as exc:
                committed = await _committed_result(owner, source_id, destination, request_id)
                if committed:
                    result = committed
                elif exc.error.retryable:
                    raise
                else:
                    result = {
                        "state": "rejected",
                        "destination": intent["destination"],
                        "resolvedDestination": destination,
                        "error": exc.body(),
                    }
        result = await _settle_codex_result(owner, source_id, request_id, claim, result)
        return {"requestId": request_id, **result}
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Retry this decision.")


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
        if str(s.session_id) != source_id
        and s.status in local_sessions.LIVE_STATUSES
        and s.capabilities_verified
        and s.capabilities.send_mode != "none"
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
        if result["state"] != "rejected" and prior["result"]["state"] == "rejected":
            # An overlapping attempt may have failed after the other committed
            # downstream. A confirmed send wins over its earlier error receipt.
            try:
                await run_in_thread(
                    messaging_repo._table().update_item,
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET #state = :state, result = :result",
                    ConditionExpression="destination = :destination AND #state = :rejected",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":state": result["state"],
                        ":result": result,
                        ":destination": destination,
                        ":rejected": "rejected",
                    },
                )
                return result
            except ClientError as changed:
                if changed.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                latest = await messaging_repo._get(pk, sk)
                if latest and latest.get("result"):
                    return latest["result"]
                raise
        return prior["result"]
    return result


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


async def _room_send(
    auth: AuthContext, source: dict, room_id: str, text: str, request_id: str, *, approval_id: str
) -> dict:
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
        sessions_repo.native_source_condition(source, approval_id=approval_id),
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


async def _session_send(
    auth: AuthContext, source: dict, session_id: str, text: str, request_id: str, *, approval_id: str
) -> dict:
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
        sessions_repo.native_source_condition(source, approval_id=approval_id),
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
        source_turn_id=source.get("activeTurnId"),
        native_hop=await _outgoing_hop(source),
        source_conditions=source_conditions,
        source_pairing=(source["humanUserId"], source["deviceId"]),
    )
    try:
        await relay.dispatch_session(source["ownerId"], session_id)
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


async def _resolve_destination(auth: AuthContext, destination: str) -> str:
    if not destination.startswith("person:"):
        return destination
    member_id = destination.removeprefix("person:")
    if member_id == auth.user_id:
        raise ApiProblem("INVALID_REQUEST", "A native session cannot message its own person destination.")
    rooms = (await messaging.list_rooms(auth)).rooms
    match = next((r for r in rooms if r.kind == "dm" and any(m.id == member_id for m in r.members)), None)
    if match is None:
        raise ApiProblem("ROOM_NOT_FOUND", "Open a direct message with this person first.")
    return f"room:{match.room_id}"


async def _dispatch_native(
    auth: AuthContext,
    source: dict,
    destination: str,
    text: str,
    request_id: str,
    *,
    approval_id: str,
) -> dict:
    if destination.startswith("session:"):
        return await _session_send(
            auth, source, destination.removeprefix("session:"), text, request_id, approval_id=approval_id
        )
    return await _room_send(auth, source, destination.removeprefix("room:"), text, request_id, approval_id=approval_id)


async def execute(device: MachineIdentity, request: NativeToolRequest) -> dict:
    await _source(device, request)
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
        if row.get("approvalId"):
            return await get_codex_send_receipt(auth, source_id, request_id)
        result = row.get("result") or {"state": row.get("state", "pending")}
        if result["state"] == "pending" or (result["state"] == "rejected" and result.get("error")):
            committed = await _committed_result(
                device.owner_id, source_id, result.get("resolvedDestination") or row["destination"], request_id
            )
            if committed:
                result = await _finish(device.owner_id, source_id, request_id, row["destination"], committed)
        if result.get("targetRequestId"):
            target = result["destination"].removeprefix("session:")
            command = await sessions_repo.get_command(device.owner_id, target, result["targetRequestId"])
            if command:
                result = {**result, "state": command["state"]}
        return result
    # Both providers may run an MCP tool without a permission hook (Claude's
    # auto mode does this). Only the owner decision route may dispatch a send.
    raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Native sends require owner approval.")
