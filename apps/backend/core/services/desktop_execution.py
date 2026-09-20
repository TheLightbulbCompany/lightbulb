"""Finite, process-local desktop receipts. Losing the process never authorizes replay.

Native probes, notifications, bounded snapshots, explicitly approved shell commands
and scoped browser actions. General files and MCP still require their own policy.
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
APPROVAL_COMMAND = "system.run.approve.v1"
PUBLIC_COMMANDS = SAFE_COMMANDS | {"system.run", "system.notify", "screen.snapshot", "browser.proxy"}
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
        from core.services.desktop_browser import MAX_UPLOAD_REQUEST

        upload = (
            request.get("command") == "browser.proxy"
            and isinstance(request.get("params"), dict)
            and request["params"].get("action") == "upload"
        )
        if len(_json(request)) > (MAX_UPLOAD_REQUEST if upload else MAX_REQUEST_BYTES):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise DesktopError("invalid_request") from None
    deadline = request["executionDeadlineMs"]
    if type(deadline) is not int or not 1 <= deadline <= EXECUTION_MS:
        raise DesktopError("invalid_deadline")
    command = request["command"]
    if not isinstance(command, str):
        raise DesktopError("unsupported_command", 422)
    if command == "browser.proxy":
        from core.services.desktop_browser import validate

        return json.loads(_json(request | {"params": validate(request["params"])}))
    if command in {"system.notify", "screen.snapshot"}:
        params = request["params"]
        if not isinstance(params, dict):
            raise DesktopError("invalid_command_params")
        if command == "screen.snapshot":
            if set(params) - {"format"} or params.get("format", "png") not in ("png", "jpg"):
                raise DesktopError("invalid_command_params")
        elif (
            set(params) - {"title", "body"}
            or any(
                not isinstance(params.get(k, ""), str)
                or len(params.get(k, "").encode("utf-8", "surrogatepass")) > limit
                for k, limit in (("title", 256), ("body", 4096))
            )
            or not (params.get("title", "").strip() or params.get("body", "").strip())
        ):
            raise DesktopError("invalid_command_params")
        return json.loads(_json(request))
    if command == "system.run":
        params = request["params"]
        if not isinstance(params, dict) or set(params) - {"argv", "cwd", "env"}:
            raise DesktopError("invalid_command_params")
        argv, cwd, env = params.get("argv"), params.get("cwd"), params.get("env", {})
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(a, str) or len(a) > 4096 or "\x00" in a for a in argv)
            or not argv[0]
            or not isinstance(cwd, str)
            or not cwd.startswith("/")
            or "\x00" in cwd
            or not isinstance(env, dict)
            or len(env) > 16
            or any(
                not isinstance(k, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k)
                or not isinstance(v, str)
                or len(v) > 1024
                or "\x00" in v
                for k, v in env.items()
            )
        ):
            raise DesktopError("invalid_command_params")
        return json.loads(_json(request | {"params": {"argv": argv, "cwd": cwd, "env": env}}))
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
        self,
        *,
        secret,
        address,
        resolve,
        dispatch,
        cancel,
        authorize,
        clock=time.time,
        enabled=lambda owner: True,
        companion=None,
    ):
        if not secret:
            raise DesktopError("desktop_not_configured", 503)
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(("desktop-admission-v1:" + secret).encode()).digest())
        )
        self.address, self.resolve, self.dispatch = address, resolve, dispatch
        self.cancel_dispatch, self.authorize, self.clock, self.enabled = cancel, authorize, clock, enabled
        self.epoch = str(uuid.uuid4())
        self.companion = companion
        from core.services.desktop_browser import BrowserScopes

        self.browser = BrowserScopes()
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
            if now > row["deadline"]:
                task = self.tasks.get(key)
                if task is not None and not task.done():
                    task.cancel()
                if row["state"] in ACTIVE:
                    row.update(
                        state="unknown" if row["dispatched"] else "rejected",
                        error={
                            "code": "execution_outcome_unknown" if row["dispatched"] else "deadline_before_dispatch"
                        },
                        completed=now,
                    )
            if row.get("completed") is not None and now > row["completed"] + BODY_SECONDS:
                row.pop("_artifacts", None)
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
            or sum(self._retained_bytes(r) for r in retained) + len(_json(request)) > MAX_BODY_BYTES
        ):
            raise DesktopError("desktop_capacity", 429)
        row = {
            "id": receipt_id,
            "requestId": request["requestId"],
            "state": "queued",
            "digest": digest,
            "socket": socket,
            "expiry": claims["exp"],
            "deadline": self.clock()
            + request["executionDeadlineMs"] / 1000
            + RESULT_GRACE_SECONDS
            + (APPROVAL_SECONDS if request["command"] == "system.run" else 0),
            "request": request,
            "target": target,
            "auth": auth,
            "invokeId": str(uuid.uuid4()),
            "dispatched": False,
            "pendingInvoke": None,
        }
        self.records[receipt_id] = row
        task = asyncio.create_task(self._execute(row))
        self.tasks[receipt_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(receipt_id, None))
        return self._view(row)

    async def _execute(self, row):
        remaining = max(0, row["deadline"] - self.clock())
        cleanup_deadline = asyncio.get_running_loop().time() + remaining
        try:
            # One admission deadline covers authorization, dispatch AND completion
            # validation; a stalled directory/DynamoDB read cannot hold a slot forever.
            async with asyncio.timeout(remaining):
                await self._execute_before_deadline(row)
        except (Exception, asyncio.CancelledError) as exc:
            row.update(
                state="unknown" if row["dispatched"] else "rejected",
                error={
                    "code": exc.code
                    if isinstance(exc, DesktopError)
                    else ("execution_outcome_unknown" if row["dispatched"] else "deadline_before_dispatch")
                },
            )
        finally:
            if row["request"]["command"] == "browser.proxy":
                for file in row["request"]["params"].get("args", {}).get("files", []):
                    file.pop("contentBase64", None)
            if row["state"] != "succeeded":
                row.pop("_artifacts", None)
            if row.get("pendingInvoke"):
                target, invoke_id = row["pendingInvoke"]
                try:
                    async with asyncio.timeout(max(0, min(2, cleanup_deadline - asyncio.get_running_loop().time()))):
                        await self.cancel_dispatch(target, invoke_id)
                except Exception:
                    pass  # send success would not prove process termination either
            row["completed"] = self.clock()

    async def _execute_before_deadline(self, row):
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
        if row["request"]["command"] == "system.run":
            result = await self._shell(row, target)
        elif row["request"]["command"] == "browser.proxy":
            result = await self.browser.execute(self, row, target)
        else:
            row.update(state="running", dispatched=True)
            result = await self._phase(row, target, row["request"], row["invokeId"])
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
        self.trim_bodies(row["socket"])

    def trim_bodies(self, socket):
        retained = [r for r in self.records.values() if r["socket"] == socket]
        bodies = [r for r in retained if "result" in r]
        total = sum(self._retained_bytes(r) for r in retained)
        for old in bodies:
            if total <= MAX_BODY_BYTES:
                break
            total -= len(_json(old.pop("result"))) + sum(len(v) for v in old.pop("_artifacts", []))
            old.update(state="unknown", error={"code": "result_expired"})
        return total

    @staticmethod
    def _retained_bytes(row):
        return (
            len(_json(row["request"])) + len(_json(row.get("result"))) + sum(len(v) for v in row.get("_artifacts", []))
        )

    async def artifact(self, auth, receipt_id, index, offset):
        from core.services.desktop_browser import CHUNK_BYTES

        self.receipt_claims(auth, receipt_id)
        self._prune()
        row = self.records.get(receipt_id)
        if not row or row["state"] != "succeeded" or "_artifacts" not in row:
            raise DesktopError("artifact_expired", 410)
        if type(index) is not int or not 0 <= index < len(row["_artifacts"]) or type(offset) is not int:
            raise DesktopError("invalid_artifact_request")
        binary = row["_artifacts"][index]
        if not 0 <= offset <= len(binary) or offset % CHUNK_BYTES:
            raise DesktopError("invalid_artifact_request")
        chunk = binary[offset : offset + CHUNK_BYTES]
        return {"id": index, "offset": offset, "bytes": len(binary), "base64": base64.b64encode(chunk).decode()}

    async def _phase(self, row, target, request, invoke_id):
        if row["state"] in {"cancel_requested", "unknown"}:
            raise DesktopError("cancelled_before_dispatch", 409)
        row["pendingInvoke"] = (target, invoke_id)
        # No task hop: waiter registration must precede interleaved cancellation.
        result = await self.dispatch(target, request, invoke_id)
        row["pendingInvoke"] = None
        return result

    async def _shell(self, row, target):
        if self.companion is None:
            raise DesktopError("shell_approval_unavailable", 422)
        native = await self.companion(row["auth"], target)
        request, auth = row["request"], row["auth"]
        params = request["params"]
        expires = min(row["deadline"], self.clock() + APPROVAL_SECONDS)
        prepare_params = {
            "command": params["argv"],
            "cwd": params["cwd"],
            "env": params["env"],
            "agentId": auth["agentId"],
            "sessionKey": auth["runId"],
            "runId": auth["runId"],
            "timeoutMs": request["executionDeadlineMs"],
        }
        prepared = await self._phase(
            row,
            target,
            request | {"command": "system.run.prepare", "params": prepare_params, "executionDeadlineMs": 15_000},
            row["invokeId"] + "-prepare",
        )
        payload = prepared.get("payload") or {}
        plan = payload.get("plan")
        if (
            not prepared.get("ok")
            or not isinstance(plan, dict)
            or payload.get("execPolicy") != {"security": "allowlist", "ask": "always"}
        ):
            raise DesktopError("shell_prepare_denied", 422)
        if (
            plan.get("argv") != params["argv"]
            or not isinstance(plan.get("cwd"), str)
            or not plan["cwd"].startswith("/")
            or plan.get("agentId") != auth["agentId"]
            or plan.get("sessionKey") != auth["runId"]
            or len(_json(plan)) > MAX_REQUEST_BYTES
        ):
            raise DesktopError("shell_prepare_mismatch", 422)
        # Digest includes env/deadline (not bound by upstream), both generations, run,
        # exact prepared script hash, and expiry. Only this trusted flow sets allow-once.
        binding = request_digest(
            {
                "request": request,
                "plan": plan,
                "native": native["generation"],
                "nativeId": native["deviceId"],
                "expires": expires,
            }
        )
        approval = {
            "binding": binding,
            "workspace": auth["ownerId"],
            "agent": auth["agentId"],
            "device": target["deviceId"],
            "run": auth["runId"],
            "argv": plan["argv"],
            "cwd": plan["cwd"],
            "env": params["env"],
            "expiresAtMs": int(expires * 1000),
        }
        row["state"] = "pending_approval" if row["state"] == "queued" else row["state"]
        answer = await self._phase(
            row,
            native,
            request
            | {
                "command": APPROVAL_COMMAND,
                "params": approval,
                "executionDeadlineMs": max(1, int((expires - self.clock()) * 1000)),
            },
            row["invokeId"] + "-approval",
        )
        if not answer.get("ok") or answer.get("payload") != {"approved": True, "binding": binding}:
            raise DesktopError("shell_approval_denied", 403)
        await self.authorize(auth)
        current = await self.resolve(auth, request)
        current_native = await self.companion(auth, current)
        if current != target or current_native != native:
            raise DesktopError("desktop_generation_lost", 409)
        if self.clock() >= expires or not self.enabled(auth["ownerId"]):
            raise DesktopError("shell_approval_expired", 409)
        if row["state"] != "pending_approval":
            raise DesktopError("cancelled_before_dispatch", 409)
        run_params = prepare_params | {
            "command": plan["argv"],
            "cwd": plan["cwd"],
            "systemRunPlan": plan,
            "approvalDecision": "allow-once",
        }
        row.update(state="running", dispatched=True)
        return await self._phase(row, target, request | {"params": run_params}, row["invokeId"])

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
            if row.get("pendingInvoke"):
                try:
                    await self.cancel_dispatch(*row["pendingInvoke"])
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


async def _resolve_companion(auth, target):
    from core.repositories import container_repo
    from core.services.connection_service import ConnectionService, DesktopTargetError
    from core.services.desktop_pair import paired_physical_mac

    row = await container_repo.get_by_owner_id(auth["ownerId"], consistent=True) or {}
    pair = paired_physical_mac(row, auth["memberId"], target["deviceId"])
    if (
        pair is None
        or pair[0].get("clientId") != "node-host"
        or pair[0].get("pairingGroupId") != target["pairingGroupId"]
    ):
        raise DesktopError("shell_approval_unavailable", 422)
    native_id = next((key for key, entry in row["paired_devices"].items() if entry == pair[1]), "")
    connections = ConnectionService()
    ready = await asyncio.to_thread(
        connections._client.get_item,
        TableName=connections.table_name,
        Key={"connectionId": {"S": connections.desktop_ready_id(auth["ownerId"], native_id)}},
        ConsistentRead=True,
    )
    generation = ready.get("Item", {}).get("generation", {}).get("S", "")
    try:
        native = await connections._resolve_desktop_target(
            auth["ownerId"], auth["memberId"], native_id, generation, APPROVAL_COMMAND
        )
    except DesktopTargetError:
        raise DesktopError("shell_approval_unavailable", 422) from None
    if native["pairingGroupId"] != target["pairingGroupId"]:
        raise DesktopError("shell_approval_unavailable", 422)
    return native | {"approvalAuth": auth, "cliDeviceId": target["deviceId"], "cliGeneration": target["generation"]}


def _session(target):
    from routers.gateway_relay import node_sessions

    session = node_sessions.get((target["ownerId"], target["deviceId"]))
    if session is None or session.generation != target["generation"]:
        raise DesktopError("desktop_generation_lost", 409)
    return session


async def _dispatch(target, request, invoke_id):
    from core.services.node_broker import NodeResultTooLarge

    if request["command"] == APPROVAL_COMMAND and target["taskAddress"] != operations().address:
        return await _approval_rpc(target, "invoke", invoke_id, request["params"])
    try:
        return await _session(target).invoke(
            request["command"],
            request["params"],
            request["executionDeadlineMs"],
            invoke_id=invoke_id,
            result_grace_ms=RESULT_GRACE_SECONDS * 1000,
            **(
                {"browser_media": True}
                if request["command"] == "browser.proxy"
                and request["params"].get("path") in {"/screenshot", "/pdf", "/download"}
                else {}
            ),
        )
    except NodeResultTooLarge:
        raise DesktopError("result_too_large", 413) from None


async def _cancel(target, invoke_id):
    if "approvalAuth" in target and target["taskAddress"] != operations().address:
        await _approval_rpc(target, "cancel", invoke_id)
        return
    session = _session(target)
    if not session.cancel(invoke_id):
        if "approvalAuth" not in target:
            raise DesktopError("cancellation_unconfirmed", 409)
        # Native v1 retains cancel-before-request tombstones. A cross-worker cancel
        # can overtake the prompt HTTP request before its waiter exists.
        session.send.put_nowait(
            {
                "type": "event",
                "event": "node.invoke.cancel",
                "payload": {"invokeId": invoke_id, "nodeId": target["deviceId"]},
            }
        )


async def _approval_rpc(target, action, invoke_id, params=None):
    import httpx
    from core.config import settings

    body = {
        "action": action,
        "auth": target["approvalAuth"],
        "deviceId": target["deviceId"],
        "generation": target["generation"],
        "cliDeviceId": target["cliDeviceId"],
        "cliGeneration": target["cliGeneration"],
        "invokeId": invoke_id,
        "params": params,
    }
    # Background-only bounded wait; public submit/status remain short. No retries.
    async with httpx.AsyncClient(
        timeout=APPROVAL_SECONDS + 5 if action == "invoke" else 8, follow_redirects=False
    ) as client:
        response = await client.post(
            target["taskAddress"] + "/api/v1/desktop-execution/internal/approval",
            json=body,
            headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET},
        )
        if response.status_code != 200:
            raise DesktopError("shell_approval_unavailable", 409)
        return response.json()


async def handle_approval_peer(body):
    """Backend-only child phase, never an API accepting model approval decisions."""
    if set(body) != {"action", "auth", "deviceId", "generation", "cliDeviceId", "cliGeneration", "invokeId", "params"}:
        raise DesktopError("invalid_request")
    auth = body["auth"]
    if not isinstance(auth, dict) or set(auth) != AUTH_FIELDS or body["action"] not in {"invoke", "cancel"}:
        raise DesktopError("invalid_request")
    if not isinstance(body["invokeId"], str) or not re.fullmatch(r"[a-f0-9-]{36}-approval", body["invokeId"]):
        raise DesktopError("invalid_request")
    if body["action"] == "cancel":
        # A revocation must not prevent cancellation of an already authenticated phase.
        target = {
            "ownerId": auth["ownerId"],
            "deviceId": body["deviceId"],
            "generation": body["generation"],
            "approvalAuth": auth,
            "taskAddress": operations().address,
        }
        await _cancel(target, body["invokeId"])
        return {"cancelRequested": True}
    await _reauthorize(auth)
    target = await _resolve(
        auth,
        {
            "deviceId": body["cliDeviceId"],
            "expectedConnectionGeneration": body["cliGeneration"],
            "command": "system.run",
        },
    )
    native = await _resolve_companion(auth, target)
    if (
        native["deviceId"] != body["deviceId"]
        or native["generation"] != body["generation"]
        or native["taskAddress"] != operations().address
    ):
        raise DesktopError("desktop_generation_lost", 409)
    p = body["params"]
    if (
        not isinstance(p, dict)
        or set(p) != {"binding", "workspace", "agent", "device", "run", "argv", "cwd", "env", "expiresAtMs"}
        or p["workspace"] != auth["ownerId"]
        or p["agent"] != auth["agentId"]
        or p["run"] != auth["runId"]
        or p["device"] != target["deviceId"]
        or not isinstance(p["binding"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", p["binding"])
        or type(p["expiresAtMs"]) is not int
        or not 0 < p["expiresAtMs"] - time.time() * 1000 <= APPROVAL_SECONDS * 1000
    ):
        raise DesktopError("invalid_request")
    return await _dispatch(
        native,
        {
            "command": APPROVAL_COMMAND,
            "params": p,
            "executionDeadlineMs": max(1, int(p["expiresAtMs"] - time.time() * 1000)),
        },
        body["invokeId"],
    )


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
            companion=_resolve_companion,
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
    action: str,
    auth: dict,
    *,
    admission_token="",
    request=None,
    receipt_id="",
    forwarded=False,
    artifact_index=None,
    artifact_offset=None,
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
        if action == "artifact":
            body |= {"artifactIndex": artifact_index, "artifactOffset": artifact_offset}
    if claims["address"] != store.address:
        return await _forward(claims["address"], body, forwarded=forwarded)
    if action == "submit":
        return await store.submit(auth, admission_token, request)
    if action == "status":
        return await store.get(auth, receipt_id)
    if action == "cancel":
        return await store.cancel(auth, receipt_id)
    if action == "artifact":
        return await store.artifact(auth, receipt_id, artifact_index, artifact_offset)
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
        "commands": sorted(set(target["commands"]) & PUBLIC_COMMANDS),
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
        commands = {v["S"] for v in row.get("commands", {}).get("L", []) if "S" in v} & PUBLIC_COMMANDS
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
