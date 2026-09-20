"""Finite, process-local desktop receipts. Losing the process never authorizes replay.

Only understood read-only native probes are enabled here. Shell, files, browser and
MCP need their own policy/approval handlers; paired command grants alone are not policy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken

SUBMISSION_SECONDS = 60
CLOCK_SKEW_SECONDS = 5
APPROVAL_SECONDS = 120
EXECUTION_MS = 180_000
RESULT_GRACE_SECONDS = 10
BODY_SECONDS = 300
MAX_ACTIVE = 8
MAX_IDENTITIES = 128
MAX_RESULT_BYTES = 1024 * 1024
MAX_BODY_BYTES = 8 * MAX_RESULT_BYTES
MAX_REQUEST_BYTES = 16_384
SAFE_COMMANDS = frozenset({"device.info", "device.status", "device.health", "device.permissions"})
ACTIVE = frozenset({"queued", "pending_approval", "running", "cancel_requested"})
AUTH_FIELDS = {"ownerId", "memberId", "runId", "agentId", "family", "roomId"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class DesktopError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def request_digest(validated_request: dict) -> str:
    return hashlib.sha256(_json(validated_request)).hexdigest()


def validate_request(request: dict) -> dict:
    fields = {
        "requestId",
        "runId",
        "agentId",
        "deviceId",
        "expectedConnectionGeneration",
        "command",
        "params",
        "executionDeadlineMs",
    }
    if (
        not isinstance(request, dict)
        or set(request) - fields
        or fields - {"params", "executionDeadlineMs"} - set(request)
    ):
        raise DesktopError("invalid_request")
    request = {"params": {}, "executionDeadlineMs": 30_000, **request}
    for key in ("runId", "agentId", "deviceId", "expectedConnectionGeneration"):
        if not isinstance(request[key], str) or not _ID.fullmatch(request[key]):
            raise DesktopError("invalid_request")
    try:
        request["requestId"] = str(uuid.UUID(request["requestId"]))
        if len(_json(request)) > MAX_REQUEST_BYTES:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise DesktopError("invalid_request") from None
    deadline = request["executionDeadlineMs"]
    if type(deadline) is not int or not 1 <= deadline <= EXECUTION_MS:
        raise DesktopError("invalid_deadline")
    command = request["command"]
    if command == "system.run":
        raise DesktopError("shell_approval_unavailable", 422)
    if not isinstance(command, str) or command not in SAFE_COMMANDS:
        raise DesktopError("unsupported_command", 422)
    if request["params"] != {}:
        raise DesktopError("invalid_command_params")
    return request


class DesktopOperations:
    """One task's finite receipt registry. Callbacks are trusted backend code, never JSON.

    Admission is encrypted with a purpose-derived backend-only key so public tokens
    reveal neither peer addresses nor routing hints. The epoch fences process restart.
    """

    def __init__(
        self, *, secret, address, resolve, dispatch, cancel, authorize, clock=time.time, enabled=lambda owner: True
    ):
        if not secret:
            raise DesktopError("desktop_not_configured", 503)
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(("desktop-admission-v1:" + secret).encode()).digest())
        )
        self.address, self.resolve, self.dispatch = address, resolve, dispatch
        self.cancel_dispatch, self.authorize, self.clock, self.enabled = cancel, authorize, clock, enabled
        self.epoch = str(uuid.uuid4())
        self.records: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def admit(self, auth: dict, target: dict) -> str:
        now = int(self.clock())
        if target["taskAddress"] != self.address:
            raise DesktopError("wrong_desktop_task", 409)
        claims = {
            "purpose": "desktop-admission-v1",
            "auth": auth,
            "deviceId": target["deviceId"],
            "generation": target["generation"],
            "address": self.address,
            "epoch": self.epoch,
            "nonce": str(uuid.uuid4()),
            "iat": now,
            "exp": now + SUBMISSION_SECONDS,
        }
        return self.cipher.encrypt(_json(claims)).decode()

    def claims(self, auth: dict, token: str) -> dict:
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            claims = json.loads(self.cipher.decrypt(token.encode()))
            if set(claims) != {"purpose", "auth", "deviceId", "generation", "address", "epoch", "nonce", "iat", "exp"}:
                raise ValueError()
            if claims["purpose"] != "desktop-admission-v1" or set(claims["auth"]) != AUTH_FIELDS:
                raise ValueError()
            if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in claims["auth"].values()):
                raise ValueError()
            if any(
                not isinstance(claims[k], str) or not _ID.fullmatch(claims[k])
                for k in ("deviceId", "generation", "epoch", "nonce")
            ):
                raise ValueError()
            if not isinstance(claims["address"], str) or len(claims["address"]) > 256:
                raise ValueError()
            if (
                type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or claims["exp"] != claims["iat"] + SUBMISSION_SECONDS
                or claims["iat"] > self.clock() + CLOCK_SKEW_SECONDS
            ):
                raise ValueError()
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
            raise DesktopError("invalid_admission", 403) from None
        if claims["auth"] != auth:
            raise DesktopError("desktop_forbidden", 403)
        return claims

    def receipt_claims(self, auth: dict, receipt_id: str) -> dict:
        try:
            token, request_id = receipt_id.rsplit(".", 1)
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise DesktopError("invalid_receipt", 400) from None
        return self.claims(auth, token)

    def _view(self, row: dict) -> dict:
        return {key: row[key] for key in ("id", "requestId", "state", "result", "error") if key in row}

    def _unknown(self, receipt_id: str, code="operation_unknown") -> dict:
        return {
            "id": receipt_id,
            "requestId": receipt_id.rsplit(".", 1)[-1],
            "state": "unknown",
            "error": {"code": code},
        }

    def _previous(self, auth, request, claims, digest):
        # Rediscovery can mint a new token; it cannot turn the same retained ID
        # into another effect. This scan is bounded to 128 identities per socket.
        for row in self.records.values():
            if (
                row["auth"] == auth
                and row["requestId"] == request["requestId"]
                and row["socket"] == (auth["ownerId"], claims["deviceId"], claims["generation"])
            ):
                if row["digest"] != digest:
                    raise DesktopError("request_conflict", 409)
                row["expiry"] = max(row["expiry"], claims["exp"])
                return self._view(row)
        return None

    def _prune(self):
        now = self.clock()
        for key, row in list(self.records.items()):
            if row["state"] in ACTIVE and now > row["deadline"]:
                row.update(state="unknown", error={"code": "execution_outcome_unknown"}, completed=now)
            if row.get("completed") is not None and now > row["completed"] + BODY_SECONDS:
                if "result" in row:
                    row.pop("result")
                    row.update(state="unknown", error={"code": "result_expired"})
                # Identity outlives ALL possible retries under its admission token.
                if now > row["expiry"] + CLOCK_SKEW_SECONDS and key not in self.tasks:
                    self.records.pop(key)

    async def submit(self, auth: dict, token: str, request: dict) -> dict:
        request = validate_request(request)
        claims = self.claims(auth, token)
        if (
            request["runId"] != auth["runId"]
            or request["agentId"] != auth["agentId"]
            or request["deviceId"] != claims["deviceId"]
            or request["expectedConnectionGeneration"] != claims["generation"]
        ):
            raise DesktopError("desktop_forbidden", 403)
        receipt_id = token + "." + request["requestId"]
        digest = request_digest(request)
        self._prune()
        previous = self._previous(auth, request, claims, digest)
        if previous:
            return previous
        if claims["epoch"] != self.epoch or claims["address"] != self.address:
            return self._unknown(receipt_id, "desktop_generation_lost")
        if self.clock() > claims["exp"] + CLOCK_SKEW_SECONDS:
            return self._unknown(receipt_id, "admission_expired")
        if not self.enabled(auth["ownerId"]):
            raise DesktopError("desktop_disabled", 503)
        await self.authorize(auth)
        target = await self.resolve(auth, request)
        if target["taskAddress"] != self.address or target["generation"] != claims["generation"]:
            return self._unknown(receipt_id, "desktop_generation_lost")
        # No await between duplicate/capacity check and reservation.
        previous = self._previous(auth, request, claims, digest)
        if previous:
            return previous
        socket = (auth["ownerId"], claims["deviceId"], claims["generation"])
        retained = [r for r in self.records.values() if r["socket"] == socket]
        if (
            len(retained) >= MAX_IDENTITIES
            or sum(r["state"] in ACTIVE or r["id"] in self.tasks for r in retained) >= MAX_ACTIVE
        ):
            raise DesktopError("desktop_capacity", 429)
        row = {
            "id": receipt_id,
            "requestId": request["requestId"],
            "state": "queued",
            "digest": digest,
            "socket": socket,
            "expiry": claims["exp"],
            "deadline": self.clock() + request["executionDeadlineMs"] / 1000 + RESULT_GRACE_SECONDS,
            "request": request,
            "target": target,
            "auth": auth,
            "invokeId": str(uuid.uuid4()),
            "dispatched": False,
        }
        self.records[receipt_id] = row
        task = asyncio.create_task(self._execute(row))
        self.tasks[receipt_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(receipt_id, None))
        return self._view(row)

    async def _execute(self, row):
        try:
            await self.authorize(row["auth"])
            target = await self.resolve(row["auth"], row["request"])
            if row["state"] == "cancel_requested":
                row.update(state="rejected", error={"code": "cancelled_before_dispatch"})
                return
            if row["state"] != "queued":
                return
            if target["taskAddress"] != self.address or target["generation"] != row["socket"][2]:
                raise DesktopError("desktop_generation_lost", 409)
            if self.clock() > min(row["expiry"] + CLOCK_SKEW_SECONDS, row["deadline"]) or not self.enabled(
                row["auth"]["ownerId"]
            ):
                raise DesktopError("admission_expired", 409)
            row.update(state="running", dispatched=True)
            # Await directly: wait_for would schedule a second task, leaving a window
            # where cancel sees running but no broker waiter yet, then dispatch starts.
            async with asyncio.timeout(row["request"]["executionDeadlineMs"] / 1000 + RESULT_GRACE_SECONDS):
                result = await self.dispatch(target, row["request"], row["invokeId"])
            current = await self.resolve(row["auth"], row["request"])
            if current["taskAddress"] != self.address or current["generation"] != row["socket"][2]:
                raise DesktopError("desktop_generation_lost", 409)
            if row["state"] == "unknown":
                return  # late output cannot make a lost generation authoritative again
            if len(_json(result)) > MAX_RESULT_BYTES:
                row.update(state="unknown", error={"code": "result_too_large"})
                return
            if result.get("ok"):
                row.update(state="succeeded", result=result.get("payload"))
            else:
                row.update(state="failed", error={"code": "node_failed"}, result=result.get("error"))
            # Evict result bodies, never accepted identities, to meet the socket budget.
            bodies = [r for r in self.records.values() if r["socket"] == row["socket"] and "result" in r]
            total = sum(len(_json(r["result"])) for r in bodies)
            for old in bodies:
                if total <= MAX_BODY_BYTES:
                    break
                total -= len(_json(old.pop("result")))
                old.update(state="unknown", error={"code": "result_expired"})
        except Exception as exc:
            row.update(
                state="unknown" if row["dispatched"] else "rejected",
                error={"code": exc.code if isinstance(exc, DesktopError) else "execution_outcome_unknown"},
            )
        finally:
            row["completed"] = self.clock()

    async def get(self, auth: dict, receipt_id: str) -> dict:
        claims = self.receipt_claims(auth, receipt_id)
        self._prune()
        row = self.records.get(receipt_id)
        if claims["epoch"] != self.epoch or not row:
            return self._unknown(receipt_id)
        if row["state"] in ACTIVE:
            try:
                target = await self.resolve(auth, row["request"])
                if target["taskAddress"] != self.address or target["generation"] != claims["generation"]:
                    raise DesktopError("desktop_generation_lost", 409)
            except Exception:
                row.update(state="unknown", error={"code": "desktop_generation_lost"}, completed=self.clock())
        return self._view(row)

    async def cancel(self, auth: dict, receipt_id: str) -> dict:
        self.receipt_claims(auth, receipt_id)
        self._prune()
        row = self.records.get(receipt_id)
        if not row:
            return self._unknown(receipt_id)
        if row["state"] in ACTIVE:
            row["state"] = "cancel_requested"
            if row["dispatched"]:
                try:
                    await self.cancel_dispatch(row["target"], row["invokeId"])
                except Exception:
                    row.update(state="unknown", error={"code": "cancellation_unconfirmed"}, completed=self.clock())
        return self._view(row)


def submissions_enabled(owner_id: str) -> bool:
    return os.getenv("DESKTOP_EXECUTION_ENABLED", "false").lower() == "true" and owner_id in os.getenv(
        "DESKTOP_EXECUTION_OWNER_ALLOWLIST", ""
    ).split(",")


async def authorize_run(owner_id: str, run_id: str, agent_id: str, family: str, *, active: bool) -> dict:
    """Derive device-member authority from canonical provenance, never caller member IDs."""
    from core.repositories import messaging_repo

    if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (owner_id, run_id, agent_id)):
        raise DesktopError("run_forbidden", 403)
    if family == "paperclip":
        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
        from core.services.paperclip_admin_session import get_admin_session_cookie

        owner = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
        if not owner:
            raise DesktopError("run_forbidden", 403)
        admin = shared_admin_client()
        try:
            cookie = await get_admin_session_cookie(admin._http)
            run = await admin.get_heartbeat_run(session_cookie=cookie, run_id=run_id)
            agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
        except PaperclipApiError:
            raise DesktopError("run_forbidden", 403) from None
        if (
            run.get("id") != run_id
            or run.get("agentId") != agent_id
            or run.get("companyId") != owner.company_id
            or agent.get("companyId") != owner.company_id
        ):
            raise DesktopError("run_forbidden", 403)
        # A company/run/agent is not a human's grant over their paired Mac.
        # Task 4/7 must add explicit trusted local consent, not infer an issue author.
        raise DesktopError("paperclip_member_authority_unavailable", 403)
    if family != "room":
        raise DesktopError("unsupported_run_family", 403)
    run = await messaging_repo.get_run(owner_id, run_id)
    if not run or run.get("ownerId") != owner_id or run.get("runId") != run_id or run.get("agentId") != agent_id:
        raise DesktopError("run_forbidden", 403)
    if active and run.get("status") not in {"dispatched", "started"}:
        raise DesktopError("run_not_active", 403)
    room_id = run.get("roomId")
    if not isinstance(room_id, str) or not _ID.fullmatch(room_id):
        raise DesktopError("run_forbidden", 403)
    room = await messaging_repo.get_room(room_id)
    if (
        not room
        or room.get("ownerId") != owner_id
        or not await messaging_repo.get_member(room_id, "agent", agent_id)
        or await messaging_repo.has_foreign_members(room_id, owner_id)
    ):
        raise DesktopError("run_forbidden", 403)
    message = await messaging_repo.get_message_by_id(room_id, run.get("triggerMessageId", ""))
    author = (message or {}).get("author", {})
    member_id = author.get("id")
    if (
        run.get("eventKind") != "user_request"
        or author.get("kind") != "human"
        or not isinstance(member_id, str)
        or not _ID.fullmatch(member_id)
    ):
        raise DesktopError("run_member_authority_unavailable", 403)
    member = await messaging_repo.get_member(room_id, "human", member_id)
    if not member or messaging_repo.member_home(member, owner_id) != owner_id:
        raise DesktopError("run_forbidden", 403)
    if owner_id != member_id:
        from core.services.clerk_admin import list_organization_members

        if not owner_id.startswith("org_") or not any(
            m.get("user_id") == member_id for m in await list_organization_members(owner_id)
        ):
            raise DesktopError("run_forbidden", 403)
    return {
        "ownerId": owner_id,
        "memberId": member_id,
        "runId": run_id,
        "agentId": agent_id,
        "family": family,
        "roomId": room_id,
    }


async def _reauthorize(auth):
    current = await authorize_run(auth["ownerId"], auth["runId"], auth["agentId"], auth["family"], active=True)
    if current != auth:
        raise DesktopError("run_forbidden", 403)


async def _resolve(auth, request):
    from core.services.connection_service import DesktopTargetError, resolve_desktop_target

    try:
        return await resolve_desktop_target(
            auth["ownerId"],
            auth["memberId"],
            request["deviceId"],
            request["expectedConnectionGeneration"],
            request["command"],
        )
    except DesktopTargetError as exc:
        raise DesktopError(exc.code, 409) from None


def _session(target):
    from routers.gateway_relay import node_sessions

    session = node_sessions.get((target["ownerId"], target["deviceId"]))
    if session is None or session.generation != target["generation"]:
        raise DesktopError("desktop_generation_lost", 409)
    return session


async def _dispatch(target, request, invoke_id):
    from core.services.node_broker import NodeResultTooLarge

    try:
        return await _session(target).invoke(
            request["command"],
            request["params"],
            request["executionDeadlineMs"],
            invoke_id=invoke_id,
            result_grace_ms=RESULT_GRACE_SECONDS * 1000,
        )
    except NodeResultTooLarge:
        raise DesktopError("result_too_large", 413) from None


async def _cancel(target, invoke_id):
    if not _session(target).cancel(invoke_id):
        raise DesktopError("cancellation_unconfirmed", 409)


_operations: DesktopOperations | None = None


def operations() -> DesktopOperations:
    global _operations
    if _operations is None:
        from core.config import settings
        from core.services.task_address import own_address

        _operations = DesktopOperations(
            secret=settings.WS_INTERNAL_SECRET,
            address=own_address(),
            resolve=_resolve,
            dispatch=_dispatch,
            cancel=_cancel,
            authorize=_reauthorize,
            enabled=submissions_enabled,
        )
    return _operations


async def _forward(address: str, body: dict, *, forwarded=False) -> dict:
    """Address is read from a ready row or decrypted admission, never public input."""
    import httpx
    from core.config import settings

    if forwarded:
        raise DesktopError("desktop_forward_loop", 409)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.post(
                address + "/api/v1/desktop-execution/internal",
                json=body,
                headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET},
            )
        if response.status_code >= 400:
            detail = response.json().get("detail", {})
            raise DesktopError(
                detail.get("code", "desktop_peer_unavailable")
                if isinstance(detail, dict)
                else "desktop_peer_unavailable",
                response.status_code,
            )
        return response.json()
    except (httpx.HTTPError, ValueError):
        # A dropped submit response must NEVER be retried by this layer.
        if body["action"] in {"submit", "status", "cancel"}:
            receipt_id = body.get("receiptId") or body["admissionToken"] + "." + body["request"]["requestId"]
            return operations()._unknown(receipt_id, "desktop_peer_unreachable")
        raise DesktopError("desktop_peer_unavailable", 503) from None


async def route_operation(
    action: str, auth: dict, *, admission_token="", request=None, receipt_id="", forwarded=False
) -> dict:
    if action == "submit":
        request = validate_request(request)
    store = operations()
    if action == "submit":
        claims = store.claims(auth, admission_token)
        body = {"action": action, "auth": auth, "admissionToken": admission_token, "request": request}
    else:
        claims = store.receipt_claims(auth, receipt_id)
        body = {"action": action, "auth": auth, "receiptId": receipt_id}
    if claims["address"] != store.address:
        return await _forward(claims["address"], body, forwarded=forwarded)
    if action == "submit":
        return await store.submit(auth, admission_token, request)
    if action == "status":
        return await store.get(auth, receipt_id)
    if action == "cancel":
        return await store.cancel(auth, receipt_id)
    raise DesktopError("invalid_action")


async def submit_desktop(auth: dict, admission_token: str, request: dict) -> dict:
    return await route_operation("submit", auth, admission_token=admission_token, request=request)


async def get_desktop_receipt(auth: dict, receipt_id: str) -> dict:
    return await route_operation("status", auth, receipt_id=receipt_id)


async def cancel_desktop(auth: dict, receipt_id: str) -> dict:
    return await route_operation("cancel", auth, receipt_id=receipt_id)


async def admit_device(auth: dict, request: dict, *, forwarded=False) -> dict:
    target = await _resolve(auth, request)
    store = operations()
    if target["taskAddress"] != store.address:
        return await _forward(
            target["taskAddress"], {"action": "admit", "auth": auth, "request": request}, forwarded=forwarded
        )
    _session(target)
    return {
        "deviceId": target["deviceId"],
        "generation": target["generation"],
        "commands": sorted(set(target["commands"]) & SAFE_COMMANDS),
        "admissionToken": store.admit(auth, target),
        "expiresInSeconds": SUBMISSION_SECONDS,
    }


async def discover_devices(auth: dict) -> dict:
    from core.services.connection_service import ConnectionService

    connections = ConnectionService()
    devices = []
    for connection_id, member, kind in await connections.query_by_owner(auth["ownerId"]):
        if member != auth["memberId"] or kind != "desktop-ready":
            continue
        response = await asyncio.to_thread(
            connections._client.get_item,
            TableName=connections.table_name,
            Key={"connectionId": {"S": connection_id}},
            ConsistentRead=True,
        )
        row = response.get("Item", {})
        commands = {v["S"] for v in row.get("commands", {}).get("L", []) if "S" in v} & SAFE_COMMANDS
        if not commands:
            continue
        request = {
            "deviceId": row.get("deviceId", {}).get("S", ""),
            "expectedConnectionGeneration": row.get("generation", {}).get("S", ""),
            "command": sorted(commands)[0],
        }
        try:
            devices.append(await admit_device(auth, request))
        except DesktopError:
            continue  # stale GSI rows are never advertised as ready
        if len(devices) >= 64:
            break
    return {"devices": devices, "submissionsEnabled": submissions_enabled(auth["ownerId"])}
