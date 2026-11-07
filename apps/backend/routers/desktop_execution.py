"""Short hook-authenticated desktop commands; no model-supplied member or peer address."""

import asyncio
import hmac
import json
import re
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.repositories import container_repo
from core.services import desktop_execution as service

router = APIRouter()


@asynccontextmanager
async def _boundary():
    try:
        async with asyncio.timeout(9):
            yield
    except service.DesktopError as exc:
        raise HTTPException(
            exc.status, detail={"code": exc.code} | ({"message": exc.message} if exc.message else {})
        ) from None
    except TimeoutError:
        raise HTTPException(503, detail={"code": "desktop_request_timeout"}) from None


_MAC_ACTIONS = {"mac_acquire", "mac_release"}


async def _auth(request: Request, *, active: bool) -> dict:
    owner = request.headers.get("x-desktop-owner", "")
    if not service._ID.fullmatch(owner):
        raise HTTPException(401, detail="missing owner")
    presented = request.headers.get("authorization", "").removeprefix("Bearer ")
    container = await container_repo.get_by_owner_id(owner, consistent=True)
    expected = (container or {}).get("hook_token") or ""
    if (
        not expected
        or not request.headers.get("authorization", "").startswith("Bearer ")
        or not hmac.compare_digest(presented.encode(), expected.encode())
    ):
        raise HTTPException(401, detail="invalid hook token")
    family = request.headers.get("x-desktop-run-family", "room")
    task = (
        {
            "task_id": request.headers.get("x-desktop-task", ""),
            "run_secret": request.headers.get("x-desktop-run-secret", ""),
        }
        if family == "task"
        else {}
    )
    return await service.authorize_run(
        owner,
        request.headers.get("x-desktop-run", ""),
        request.headers.get("x-desktop-agent", ""),
        family,
        active=active,
        **task,
    )


async def _body(request: Request, limit=service.MAX_REQUEST_BYTES, *, command_body=True) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise service.DesktopError("request_too_large", 413)
        data.extend(chunk)
    try:
        body = json.loads(data)
    except ValueError:
        if len(data) > service.MAX_REQUEST_BYTES:
            raise service.DesktopError("request_too_large", 413) from None
        raise service.DesktopError("invalid_request") from None
    if not isinstance(body, dict):
        raise service.DesktopError("invalid_request")
    candidate = body.get("request", body) if body.get("action") == "submit" else body
    if (
        command_body
        and len(data) > service.MAX_REQUEST_BYTES
        and not (isinstance(candidate, dict) and candidate.get("command") == "file.write")
    ):
        raise service.DesktopError("request_too_large", 413)
    return body


@router.get("/devices")
async def devices(request: Request):
    async with _boundary():
        return await service.discover_devices(await _auth(request, active=True))


@router.post("/operations")
async def submit(request: Request):
    async with _boundary():
        # Existing duplicate receipts remain readable even after the run ends.
        auth = await _auth(request, active=False)
        return await service.submit_desktop(
            auth, request.headers.get("x-desktop-admission", ""), await _body(request, service.MAX_UPLOAD_REQUEST)
        )


@router.post("/admission")
async def admission(request: Request):
    async with _boundary():
        auth = await _auth(request, active=True)
        selection = await _body(request)
        if (
            set(selection) != {"deviceId", "expectedConnectionGeneration", "command"}
            or not isinstance(selection["command"], str)
            or selection["command"] not in service.PUBLIC_COMMANDS
            or any(
                not isinstance(selection[k], str) or not service._ID.fullmatch(selection[k])
                for k in ("deviceId", "expectedConnectionGeneration")
            )
        ):
            raise service.DesktopError("invalid_request")
        return await service.admit_device(auth, selection)


@router.get("/operations/{receipt_id}")
async def status(receipt_id: str, request: Request):
    async with _boundary():
        return await service.get_desktop_receipt(await _auth(request, active=False), receipt_id)


@router.post("/operations/{receipt_id}/cancel")
async def cancel(receipt_id: str, request: Request):
    async with _boundary():
        return await service.cancel_desktop(await _auth(request, active=False), receipt_id)


@router.get("/operations/{receipt_id}/artifacts/{index}")
async def artifact(receipt_id: str, index: int, request: Request, offset: int = 0):
    async with _boundary():
        return await service.route_operation(
            "artifact",
            await _auth(request, active=False),
            receipt_id=receipt_id,
            artifact_index=index,
            artifact_offset=offset,
        )


@router.post("/internal")
async def internal(request: Request):
    async with _boundary():
        secret = settings.WS_INTERNAL_SECRET
        presented = request.headers.get("x-lightbulb-ws-internal", "")
        if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
            raise HTTPException(403, detail="forbidden")
        body = await _body(request, service.MAX_UPLOAD_REQUEST + 8192)
        action, auth = body.get("action"), body.get("auth")
        if action not in {"admit", "submit", "status", "cancel", "artifact"} | _MAC_ACTIONS or not service.valid_auth(
            auth
        ):
            raise service.DesktopError("invalid_request")
        allowed = (
            {"action", "auth", "request"}
            if action == "admit" or action in _MAC_ACTIONS
            else (
                {"action", "auth", "request", "admissionToken"}
                if action == "submit"
                else {"action", "auth", "receiptId"}
            )
        )
        if action == "artifact":
            allowed |= {"artifactIndex", "artifactOffset"}
        if set(body) != allowed:
            raise service.DesktopError("invalid_request")
        # A session run is the backend's own (session_browser): only mac_browser's forward to the
        # Mac's owning task carries one here.
        current = await service.authorize_run(
            auth["ownerId"],
            auth["runId"],
            auth["agentId"],
            auth["family"],
            active=action in {"admit"} | _MAC_ACTIONS,
            allow_session=action in _MAC_ACTIONS,
            **({"task_id": auth["roomId"]} if auth["family"] == "task" else {}),
        )
        if current != auth:
            raise service.DesktopError("run_forbidden", 403)
        if action in _MAC_ACTIONS:
            from core.services.mac_browser import ACQUIRE_BUDGET_S as mac_budget

            params = body["request"] if isinstance(body["request"], dict) else {}
            device, budget = params.get("deviceId"), params.get("budgetS", mac_budget)
            if (
                not {"deviceId"}
                <= set(params)
                <= ({"deviceId", "budgetS"} if action == "mac_acquire" else {"deviceId"})
                or not isinstance(device, str)
                or not service._ID.fullmatch(device)
                or type(budget) not in (int, float)
                or not 0 < budget <= mac_budget
            ):
                raise service.DesktopError("invalid_request")
        if action == "admit":
            params = body["request"]
            if (
                not isinstance(params, dict)
                or set(params) != {"deviceId", "expectedConnectionGeneration", "command"}
                or params["command"] not in service.PUBLIC_COMMANDS
            ):
                raise service.DesktopError("invalid_request")
            return await service.admit_device(auth, params, forwarded=True)
        if action not in _MAC_ACTIONS:
            return await service.route_operation(
                action,
                auth,
                admission_token=body.get("admissionToken", ""),
                request=body.get("request"),
                receipt_id=body.get("receiptId", ""),
                forwarded=True,
                artifact_index=body.get("artifactIndex"),
                artifact_offset=body.get("artifactOffset"),
            )
    # The Mac browser lease waits past a Chrome launch: outside _boundary's 9 s.
    from core.services import mac_browser

    try:
        async with asyncio.timeout(mac_browser.FORWARD_TIMEOUT_S):  # < 30 s; acquire answers inside budgetS
            if action == "mac_acquire":
                return await mac_browser.acquire(auth, device, forwarded=True, budget_s=budget)
            return await mac_browser.release(auth, device, "", forwarded=True)
    except service.DesktopError as exc:
        exc = mac_browser._with_message(exc)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from None
    except TimeoutError:
        raise HTTPException(503, detail={"code": "desktop_request_timeout"}) from None


@router.post("/internal/mac-wipe")
async def mac_wipe_peer(request: Request):
    """A peer task forwards a Mac browser wipe to the task holding that Mac's socket."""
    from core.services import mac_browser

    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    body = await _body(request, command_body=False)
    if set(body) != {"ownerId", "deviceId"} or not all(
        isinstance(body[k], str) and service._ID.fullmatch(body[k]) for k in body
    ):
        raise HTTPException(400, detail={"code": "invalid_request"})
    try:
        async with asyncio.timeout(mac_browser.CLEAR_SECONDS):
            wiped = await mac_browser.wipe(
                body["ownerId"], body["deviceId"], mac_browser.CLEAR_SECONDS - 3, forwarded=True
            )
    except (service.DesktopError, TimeoutError):
        wiped = False
    if not wiped:
        raise HTTPException(409, detail={"code": "browser_wipe_failed"})
    return {"ok": True}


#: 32 events of 1,000 characters, each at most 12 JSON bytes (a \u-escaped surrogate pair).
MAC_INPUT_MAX_BYTES = 400_000


@router.post("/internal/mac-frames")
async def mac_frames_peer(request: Request):
    """Forward one viewer heartbeat to the task holding that run's live Mac channel."""
    from core.services import mac_live
    from routers.container import deliver_mac_frames

    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    try:
        body = await _body(request, command_body=False)
    except service.DesktopError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    if (
        set(body)
        not in ({"ownerId", "userId", "runId", "tier"}, {"ownerId", "userId", "runId", "tier", "viewerOwnerId"})
        or not all(isinstance(body[k], str) and service._ID.fullmatch(body[k]) for k in ("ownerId", "userId"))
        or (
            "viewerOwnerId" in body
            and not (isinstance(body["viewerOwnerId"], str) and service._ID.fullmatch(body["viewerOwnerId"]))
        )
        or not (isinstance(body["runId"], str) and re.fullmatch(mac_live.RUN_ID_PATTERN, body["runId"]))
        or body["tier"] not in ("view", "thumb")
    ):
        raise HTTPException(400, detail={"code": "invalid_request"})
    await deliver_mac_frames(
        body["ownerId"], body["userId"], body["runId"], body["tier"], viewer_owner=body.get("viewerOwnerId")
    )
    return {"ok": True}


@router.post("/internal/mac-input")
async def mac_input_peer(request: Request):
    """A peer task forwards a viewer's Mac input to the task holding that Mac's socket."""
    from pydantic import ValidationError

    from core.services import mac_live
    from routers.container import deliver_mac_input

    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    try:
        body = await _body(request, MAC_INPUT_MAX_BYTES, command_body=False)
    except service.DesktopError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    if (
        set(body)
        not in ({"ownerId", "userId", "runId", "events"}, {"ownerId", "userId", "runId", "events", "viewerOwnerId"})
        or not all(isinstance(body[k], str) and service._ID.fullmatch(body[k]) for k in ("ownerId", "userId"))
        or (
            "viewerOwnerId" in body
            and not (isinstance(body["viewerOwnerId"], str) and service._ID.fullmatch(body["viewerOwnerId"]))
        )
        or not (isinstance(body["runId"], str) and re.fullmatch(mac_live.RUN_ID_PATTERN, body["runId"]))
    ):
        raise HTTPException(400, detail={"code": "invalid_request"})
    try:
        events = mac_live.parse_events(body["events"])
    except ValidationError:
        raise HTTPException(400, detail={"code": "invalid_request"}) from None
    await deliver_mac_input(
        body["ownerId"], body["userId"], body["runId"], events, viewer_owner=body.get("viewerOwnerId")
    )
    return {"ok": True}


@router.post("/internal/approval")
async def approval_peer(request: Request):
    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    try:
        async with asyncio.timeout(service.APPROVAL_SECONDS + 5):
            return await service.handle_approval_peer(await _body(request, 32768, command_body=False))
    except service.DesktopError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except TimeoutError:
        raise HTTPException(409, detail={"code": "shell_approval_expired"}) from None


@router.post("/internal/file-policy")
async def file_policy_peer(request: Request):
    async with _boundary():
        secret = settings.WS_INTERNAL_SECRET
        presented = request.headers.get("x-lightbulb-ws-internal", "")
        if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
            raise HTTPException(403, detail="forbidden")
        return await service.handle_file_policy_peer(await _body(request))
