"""Container lifecycle management endpoints.

Provides the container status read and the manual provision/retry endpoints.
"""

import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.services import browser_control, browser_extensions, mac_live, readiness_state
from core.repositories import container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import ensure_workspace_trial_default

logger = logging.getLogger(__name__)

router = APIRouter()


def _owner_role(auth: AuthContext) -> str:
    """ "admin" | "member" — the role a Gate payload is rendered for."""
    is_admin = auth.is_org_admin
    return "admin" if is_admin else "member"


async def _assert_provision_allowed(
    owner_id: str,
    *,
    is_admin: bool = True,
    actor_user_id: str | None = None,
) -> None:
    """Raise 402 with a structured `blocked` payload if a provision gate fires.

    Delegates to ``core.services.provision_gate.evaluate_provision_gate`` so
    /container/provision and /container/status share the same logic and can
    never disagree about whether a gate is up. ``actor_user_id`` is the caller,
    so a trial-expired block lands on their PostHog person.
    """
    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        is_admin=is_admin,
        actor_user_id=actor_user_id,
    )
    if gate is not None:
        raise HTTPException(status_code=402, detail=gate.to_payload())


@router.get(
    "/status",
    summary="Get container metadata for current user",
    description=(
        "A cold read of the workspace's status contract, fetched on mount and after a "
        "live event. It triggers no provisioning: a new workspace is provisioned by the "
        "Clerk organization.created webhook, and a failed or stopped one waits for the "
        "user's Retry (POST /container/recover). Its one write is creating an admin's "
        "missing trial row (create-if-absent), so a read that beats the webhook sees the trial."
    ),
    operation_id="container_status",
    responses={
        202: {"description": "Org-member: owner's container not ready yet"},
        402: {"description": "Provision gate is up (structured blocked payload)"},
    },
)
async def container_status(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    # One paperclip-companies read, threaded into both compute_status calls below.
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        # No row: surface a gate as its structured 402 payload so the frontend renders the
        # picker / "top up" CTA instead of a generic "no container" state. An admin's first
        # read can beat the organization.created webhook, so the trial row is created here
        # too (create-if-absent -- a write, not a trigger) or the gate would read "no
        # billing" and flash the paywall.
        if auth.is_org_admin:
            await ensure_workspace_trial_default(auth, owner_id)
        gate = await evaluate_provision_gate(owner_id=owner_id, is_admin=auth.is_org_admin, actor_user_id=auth.user_id)
        if gate is not None:
            raise HTTPException(status_code=402, detail=gate.to_payload())
        # Org members ride the owner's container: 202 renders "workspace pending".
        if auth.is_org_context and not auth.is_org_admin:
            raise HTTPException(status_code=202, detail="team workspace pending")
        # compute_status handles container=None (runtime_phase="provisioning") and keeps a
        # preserved onboarding state (funnel_state=="complete") across a container-only
        # recreation, so onboarded is never hardcoded False here.
        contract = await readiness_state.compute_status(owner_id, container=None, onboarding=onboarding)
        return _status_response(contract)

    # Single readiness+onboarding read (INV-1): compute_status is the SOLE composer of the
    # containers/billing/paperclip-companies inputs — see core/services/readiness_state.py.
    # A failed Teams seed surfaces as `teams_state == "failed"` alongside a normal phase
    # (non-blocking; never gates).
    contract = await readiness_state.compute_status(owner_id, container=container, onboarding=onboarding)

    # A failed/stopped row is reported as it is: nothing re-drives it from here. An over-cap or
    # expired trial gets the limit card instead of the error card (the FULL trial predicate).
    if container.get("status") in ("error", "stopped"):
        from core.services import provision_gate, trial_cap

        trial_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if trial_gate is not None:
            raise HTTPException(status_code=402, detail=trial_gate.to_payload())

    return _status_response(contract)


def _status_response(contract: dict) -> dict:
    """The ONE 200 shape for /container/status: the composed
    ``StatusContract`` (see core/services/readiness_state.compute_status),
    verbatim -- no legacy flat duplicates (spec PR-9, CLIENT-HOOK). Prior
    revisions of this function emitted BOTH the clean contract fields AND
    duplicate flat ones (``phase``=runtime_phase, ``status``/``substatus``
    =container_status/container_substatus, ``teams_status``=teams_state==
    "failed"); those are deleted here -- there are no users and no
    backward-compat requirement, and every reader (web + mobile) now reads
    the contract fields directly via ``deriveUIPhase``
    (packages/shared/src/hooks/statusContract.ts).

    A deliberately-parked trial workspace is reported via ``lifecycle="parked"``
    + ``parked=True``, both composed by ``compute_status`` off
    ``wake_policy.is_parked_trial`` -- NOT the old ``phase="parked"`` string
    (``phase`` no longer exists on the wire; ``runtime_phase`` stays
    "provisioning", per INV-6). There is no ``wake_retry_after_s`` hint any
    more: AgentCore wakes the microVM on the next invoke, so a client has
    nothing to wait out.
    """
    return {
        "created_at": contract.get("created_at"),
        "updated_at": contract.get("updated_at"),
        "region": settings.AWS_REGION,
        # Only an errored row explains itself: a live one may carry a stale earlier failure.
        "last_error": contract.get("last_error") if contract.get("container_status") == "error" else None,
        "last_error_at": contract.get("last_error_at") if contract.get("container_status") == "error" else None,
        # Per-CONTAINER onboarding fact. RECONCILE-WITH-ONBOARD: this was
        # `container.get("onboarded_at") is not None`; the
        # composer now derives it as `funnel_state == "complete"` once
        # ONBOARD's writers exist, falling back to the legacy onboarded_at
        # stamp until then -- see readiness_state.compute_status.
        "onboarded": contract.get("onboarded", False),
        "lifecycle": contract.get("lifecycle"),
        "runtime_phase": contract.get("runtime_phase"),
        "chat_available": contract.get("chat_available"),
        "container_status": contract.get("container_status"),
        "container_substatus": contract.get("container_substatus"),
        # Constant, and read by nothing current. Shipped mobile bundles (OTA from main) hold
        # their runtime RPCs until this key is truthy, so it stays until those bundles age out.
        "runtime_host": "agentcore",
        "parked": bool(contract.get("parked", False)),
        "generations": contract.get("generations"),
        "funnel": contract.get("funnel"),
        "bootstrap": contract.get("bootstrap"),
        "provider": contract.get("provider"),
        "teams_state": contract.get("teams_state"),
    }


#: Mounted at ``/api/v1/browser``, not under this file's ``/api/v1/container`` prefix — the
#: panel's URLs are ``/api/v1/browser/*``. Same two-routers-in-one-module shape
#: ``connections.oauth_router`` already uses.
browser_router = APIRouter()


class BrowserGeneration(BaseModel):
    generation: str = Field(min_length=1, max_length=128)


class BrowserRun(BaseModel):
    runId: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")
    #: Which agent the run belongs to, so the panel can say "Friday is browsing".
    agentId: str | None = Field(default=None, max_length=256)


class BrowserRunRelease(BrowserRun, BrowserGeneration):
    pass


async def _browser_result(operation):
    try:
        return await operation
    except browser_control.BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        # A failed signed URL/RPC must never leak its credential-bearing exception: log the
        # class only, so a 503 is at least attributable.
        logger.warning("browser operation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Browser is temporarily unavailable. Try again.") from None


class BrowserViewing(BaseModel):
    #: This viewer has no live view (another screen holds its one slot): send live frames.
    frames: bool = False


@browser_router.post("/viewing")
async def browser_viewing(
    response: Response, body: BrowserViewing | None = None, auth: AuthContext = Depends(get_current_user)
):
    """The open, visible panel's ~20 s heartbeat: starts the browser if none is running (like
    Start) and keeps it from idle-stopping while someone is watching."""
    response.headers["Cache-Control"] = "no-store"
    owner_id = resolve_owner_id(auth)
    await _assert_provision_allowed(owner_id, is_admin=auth.is_org_admin, actor_user_id=auth.user_id)
    return await _browser_result(browser_control.watch(owner_id, auth.user_id, frames=bool(body and body.frames)))


class BrowserFrames(BaseModel):
    runId: str = Field(pattern=mac_live.RUN_ID_PATTERN)
    deviceId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    tier: Literal["view", "thumb"]
    roomId: str | None = None


MAC_FRAMES_PATH = "/api/v1/desktop-execution/internal/mac-frames"


async def deliver_mac_frames(
    owner_id: str, user_id: str, run_id: str, tier: Literal["view", "thumb"], *, viewer_owner: str | None = None
) -> None:
    """Stamp demand only on the task holding this live run's channel."""
    try:
        await mac_live.heartbeat(owner_id, user_id, run_id, tier, viewer_owner=viewer_owner)
    except LookupError:
        raise HTTPException(409, detail=_MAC_NOT_LIVE) from None
    except PermissionError:
        raise HTTPException(403, detail=_MAC_ROOM_FORBIDDEN) from None


@browser_router.post("/frames")
async def browser_frames(response: Response, body: BrowserFrames, auth: AuthContext = Depends(get_current_user)):
    """A current room member's ~20 s heartbeat for one live Mac run and one cast tier."""
    from core.services import mac_browser, task_address
    from core.services.connection_service import ConnectionService
    from core.services.desktop_execution import DesktopError

    response.headers["Cache-Control"] = "no-store"
    owner_id, viewer_owner = await mac_run_owner(auth, body.roomId)
    address = await ConnectionService().desktop_task_address(owner_id, body.deviceId)
    if address is None:
        raise HTTPException(409, detail=_MAC_NOT_LIVE)
    if address == task_address.own_address():
        await deliver_mac_frames(owner_id, auth.user_id, body.runId, body.tier, viewer_owner=viewer_owner)
        return {"ok": True}
    try:
        payload = {"ownerId": owner_id, "userId": auth.user_id, "runId": body.runId, "tier": body.tier}
        if viewer_owner != owner_id:
            payload["viewerOwnerId"] = viewer_owner
        await mac_browser._forward_mac(address, payload, MAC_FRAMES_PATH, timeout=5)
    except DesktopError as exc:
        if exc.status == 409 and exc.code == "mac_view_not_live":
            raise HTTPException(409, detail=_MAC_NOT_LIVE) from None
        if exc.status == 403:
            raise HTTPException(403, detail=_MAC_ROOM_FORBIDDEN) from None
        logger.warning("mac frames forward failed owner=%s code=%s", owner_id, exc.code)
        raise HTTPException(502, detail={"code": exc.code, "message": exc.message}) from None
    return {"ok": True}


class BrowserMacInput(BaseModel):
    runId: str = Field(pattern=mac_live.RUN_ID_PATTERN)
    deviceId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    roomId: str | None = None  # the room routes a shared guest to its Mac-owning workspace
    events: mac_live.MacInputEvents


_MAC_NOT_LIVE = {"code": "mac_view_not_live", "message": "That page isn't live on the Mac any more."}
# Messaging's ROOM_FORBIDDEN, lowercased like this route's other codes.
_MAC_ROOM_FORBIDDEN = {"code": "room_forbidden", "message": "Only people in this room can use its browser."}
_SESSION_FORBIDDEN = {
    "code": "session_forbidden",
    "message": "Only people who may type into this session can use its browser.",
}
MAC_INPUT_PATH = "/api/v1/desktop-execution/internal/mac-input"


async def mac_run_owner(auth: AuthContext, room_id: str | None) -> tuple[str, str]:
    """The Mac belongs to the room's owner; membership follows the viewer's home workspace."""
    from core.services.messaging import ApiProblem, require_room_member

    viewer_owner = resolve_owner_id(auth)
    if room_id is None:  # an older client can still reach its own workspace's Mac
        return viewer_owner, viewer_owner
    try:
        room, _ = await require_room_member(viewer_owner, auth.user_id, room_id)
    except ApiProblem:
        raise HTTPException(403, detail=_MAC_ROOM_FORBIDDEN) from None
    return room["ownerId"], viewer_owner


async def deliver_mac_input(
    owner_id: str, user_id: str, run_id: str, events: list[dict], *, viewer_owner: str | None = None
) -> None:
    """Hand input to the run's live channel on THIS task (also the peer route's delivery)."""
    from core.services import mac_browser, session_browser
    from core.services.node_broker import CdpClosed, CommandNotAllowed, NodeCapacity

    try:
        await mac_live.input(owner_id, user_id, run_id, events, viewer_owner=viewer_owner)
    except (LookupError, CdpClosed):
        raise HTTPException(409, detail=_MAC_NOT_LIVE) from None
    except PermissionError:  # not a current member of the run's room, or may not type into the session
        raise HTTPException(
            403, detail=_SESSION_FORBIDDEN if session_browser.session_of(run_id) else _MAC_ROOM_FORBIDDEN
        ) from None
    except CommandNotAllowed:
        code = "desktop_update_required"
        raise HTTPException(409, detail={"code": code, "message": mac_browser.MESSAGES[code]}) from None
    except NodeCapacity:  # the Mac's socket is backed up, as desktop_session_start answers it
        code = "mac_browser_busy"
        raise HTTPException(429, detail={"code": code, "message": mac_browser.MESSAGES[code]}) from None


async def route_mac_input(
    owner_id: str, user_id: str, device_id: str, run_id: str, events: list[dict], *, viewer_owner: str | None = None
) -> None:
    """Input for a Mac run, delivered on the task holding that Mac's socket: here, or forwarded to
    the peer that holds it. The deviceId only routes; the live channel re-checks who may drive it."""
    from core.services import mac_browser, task_address
    from core.services.connection_service import ConnectionService
    from core.services.desktop_execution import DesktopError

    address = await ConnectionService().desktop_task_address(owner_id, device_id)
    if address is None:
        raise HTTPException(409, detail=_MAC_NOT_LIVE)
    if address == task_address.own_address():
        await deliver_mac_input(owner_id, user_id, run_id, events, viewer_owner=viewer_owner)
        return
    try:
        payload = {"ownerId": owner_id, "userId": user_id, "runId": run_id, "events": events}
        if viewer_owner is not None and viewer_owner != owner_id:
            payload["viewerOwnerId"] = viewer_owner
        await mac_browser._forward_mac(address, payload, MAC_INPUT_PATH, timeout=5)
    except DesktopError as exc:
        if exc.status == 409 and exc.code == "mac_view_not_live":
            raise HTTPException(409, detail=_MAC_NOT_LIVE) from None
        if exc.status in (403, 409, 429):  # the peer's own answer (may not drive it, update required, busy)
            raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from None
        logger.warning("mac input forward failed owner=%s code=%s", owner_id, exc.code)
        raise HTTPException(502, detail={"code": exc.code, "message": exc.message}) from None


@browser_router.post("/mac/input")
async def browser_mac_input(body: BrowserMacInput, response: Response, auth: AuthContext = Depends(get_current_user)):
    """A viewer's clicks and keys on an agent's Mac tab, delivered to the task holding that
    Mac's socket. The room resolves the Mac owner's workspace; the live channel checks the
    viewer's current membership using their own workspace. The deviceId only routes."""
    response.headers["Cache-Control"] = "no-store"
    owner_id, viewer_owner = await mac_run_owner(auth, body.roomId)
    events = mac_live.dump_events(body.events)
    await route_mac_input(owner_id, auth.user_id, body.deviceId, body.runId, events, viewer_owner=viewer_owner)
    return {"ok": True}


class BrowserInput(BaseModel):
    """One click, scroll, key or text on an agent's live frames. No `type`: the plain "a person
    is using the live view" signal."""

    runId: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")
    type: Literal["click", "wheel", "key", "text"] | None = None
    #: In the frame's own CSS pixels (its `w` x `h`); the bridge clamps to the page.
    x: float = Field(default=0, ge=0, le=10000)
    y: float = Field(default=0, ge=0, le=10000)
    button: Literal["left", "middle", "right"] = "left"
    key: str | None = Field(default=None, max_length=32)
    text: str | None = Field(default=None, max_length=1000)
    deltaY: float = Field(default=0, ge=-5000, le=5000)


@browser_router.post("/input")
async def browser_input(
    response: Response, body: BrowserInput | None = None, auth: AuthContext = Depends(get_current_user)
):
    """A person is using the browser: through the live view (the panel throttles this to one
    every few seconds; it grants nothing) or, with a `type`, on an agent's live frames, which
    the bridge types into that run's tab. Agents yield to both; the autosave waits for quiet."""
    response.headers["Cache-Control"] = "no-store"
    owner_id = resolve_owner_id(auth)
    if body is None or body.type is None:
        return await _browser_result(browser_control.record_input(owner_id))
    return await _browser_result(browser_control.send_input(owner_id, body.model_dump(exclude_none=True)))


async def _extension_result(operation):
    try:
        return await operation
    except browser_extensions.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def _restart(owner_id: str) -> bool:
    """Extensions load only at session start: restart a running, idle browser in the background."""
    try:
        return await browser_control.restart_soon(owner_id)
    except browser_control.BrowserError:
        return False


async def _extension_change(owner_id: str, operation) -> dict:
    added = await _extension_result(operation)
    return {**added, "restarting": await _restart(owner_id)}


class WebStoreInstall(BaseModel):
    idOrUrl: str = Field(min_length=1, max_length=2048)


class ExtensionAsk(WebStoreInstall):
    agentId: str | None = Field(default=None, max_length=256)


@browser_router.post("/extensions/u/{owner_id}/requests", status_code=201)
async def browser_extensions_agent_request(owner_id: str, body: ExtensionAsk, request: Request):
    """An agent (hook token, via lightbulb-api.sh) asks to add a Web Store extension. It never
    installs: every workspace member gets a Needs-you Activity item, and one click adds it."""
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    return await _extension_result(browser_extensions.request_install(owner_id, body.idOrUrl, agent_id=body.agentId))


# ---- the helper extension inside the shared browser (its per-session token) ----------------


#: A helper click installs directly only this soon after a person's own input in the panel.
HELPER_HUMAN_WINDOW_S = 5.0


async def _helper_owner(request: Request) -> tuple[str, dict]:
    """The workspace a helper token is for, and its browser state. The token lives inside the
    browser, where an agent run can read it over CDP: it is honoured only while that
    workspace has a live session, and it never resolves a password."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    owner_id = await browser_extensions.helper_owner(token) if token else None
    state = await container_repo.get_browser_state(owner_id) if owner_id else None
    if not owner_id or not (state or {}).get("session_id"):
        raise HTTPException(status_code=401, detail="invalid browser helper token")
    return owner_id, state


async def _person_just_clicked(owner_id: str) -> bool:
    touched = await container_repo.get_browser_input_at(owner_id) or 0
    return time.time() - float(touched) <= HELPER_HUMAN_WINDOW_S


@browser_router.post("/helper/extensions", status_code=201)
async def browser_helper_install(body: WebStoreInstall, request: Request):
    """The helper popup's "Add to the shared browser" on a Web Store page: installs when a
    person just clicked in the panel, files the Needs-you ask otherwise."""
    owner_id, state = await _helper_owner(request)
    # Anything inside the browser can press the helper's button (an agent, or whatever read
    # its token). Only a click that lands right after a PERSON's input in the panel -- which
    # the member's own Clerk session reports -- installs; every other one asks.
    if browser_control._mode(state) == "agent" or not await _person_just_clicked(owner_id):
        agent_id = state.get("agent_id") if browser_control._mode(state) == "agent" else None
        return await _extension_result(browser_extensions.request_install(owner_id, body.idOrUrl, agent_id=agent_id))
    added = await _extension_change(owner_id, browser_extensions.add_from_webstore(owner_id, body.idOrUrl, "browser"))
    return {**added, "status": "installed"}


class HelperSave(BaseModel):
    origin: str = Field(max_length=2048)
    username: str = Field(max_length=512)
    password: str = Field(max_length=4096)


def _origin(value: str) -> str:
    from core.services import website_logins

    try:
        return website_logins.normalize_origin(value)
    except website_logins.LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@browser_router.get("/helper/logins")
async def browser_helper_logins(origin: str, request: Request):
    """The workspace's saved logins for the page's own origin (the helper derives it from the
    tab, never from the page): usernames only."""
    from core.services import website_logins

    owner_id, _ = await _helper_owner(request)
    origin = _origin(origin)
    logins = await website_logins.list_logins(owner_id)
    return {"logins": [{"id": x["id"], "username": x["username"]} for x in logins if x["origin"] == origin]}


@browser_router.post("/helper/logins", status_code=201)
async def browser_helper_save(body: HelperSave, request: Request):
    """The helper's "Save this login for the workspace?" -- into the same store Connections ->
    Website sign-ins lists and the agents' vault fills from. A save replaces the password of an
    existing origin + username, and the helper's token can be read by a page or an agent, so it
    takes the same two checks as Fill: no run holding the browser, and a person's fresh click."""
    from core.services import website_logins

    owner_id, state = await _helper_owner(request)
    if browser_control._mode(state) == "agent":
        raise HTTPException(status_code=409, detail=browser_control.AGENT_BUSY)
    if not await _person_just_clicked(owner_id):
        raise HTTPException(status_code=403, detail="Click Save yourself in the browser panel.")
    try:
        return await website_logins.add_login(
            owner_id, body.origin, body.username, body.password, added_by="browser", added_by_name="Shared browser"
        )
    except website_logins.LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


class HelperFill(BaseModel):
    id: str = Field(min_length=1, max_length=64)


@browser_router.post("/helper/logins/fill-request")
async def browser_helper_fill(body: HelperFill, request: Request, response: Response):
    """The helper's "Fill <username>": the backend types the login into the active tab itself
    (browser_control.fill_login). The helper's token can be read by an agent over CDP, so this
    never answers a password -- and fills only on a person's fresh click, with no run holding
    the browser, on a tab whose real address is the login's origin."""
    owner_id, state = await _helper_owner(request)
    response.headers["Cache-Control"] = "no-store"
    if browser_control._mode(state) == "agent":
        raise HTTPException(status_code=409, detail=browser_control.AGENT_BUSY)
    if not await _person_just_clicked(owner_id):
        raise HTTPException(status_code=403, detail="Click Fill yourself in the browser panel.")
    return await _browser_result(browser_control.fill_login(owner_id, body.id))


browser_internal_router = APIRouter()


@browser_internal_router.post("/{owner_id}/acquire")
async def browser_agent_acquire(owner_id: str, body: BrowserRun, request: Request, response: Response):
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    result = await _browser_result(browser_control.acquire_agent(owner_id, body.runId, body.agentId))
    if result == browser_control.STARTING:
        response.status_code = 202  # a cold start runs on in the background; the bridge polls
    return result


@browser_internal_router.post("/{owner_id}/release")
async def browser_agent_release(owner_id: str, body: BrowserRunRelease, request: Request, response: Response):
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    return await _browser_result(browser_control.release_agent(owner_id, body.runId, body.generation))


_MAC_ID = r"^[A-Za-z0-9_-]{1,128}$"


class MacRun(BaseModel):
    runId: str = Field(pattern=_MAC_ID)
    agentId: str = Field(pattern=_MAC_ID)
    family: Literal["room", "call"] = "room"


class MacAcquire(MacRun):
    deviceId: str | None = Field(default=None, pattern=_MAC_ID)
    budgetS: float = Field(default=25.0, gt=0, le=25)  # the bridge's remaining time: answer inside it


class MacRelease(MacRun):
    deviceId: str = Field(pattern=_MAC_ID)
    generation: str = Field(default="", max_length=128)


class MacSynced(MacRun):
    deviceId: str = Field(pattern=_MAC_ID)
    syncId: str = Field(pattern=_MAC_ID)
    ok: bool


async def _mac_call(
    owner_id: str, body: MacRun, request: Request, response: Response, operation, ended, *, active: bool = True
):
    """Hook token, then run authority (active=True: the bridge calls before run.ended), then the
    operation. `ended` answers a run that is no longer active (None: refuse it)."""
    from core.services import desktop_execution, mac_browser
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    try:
        auth = await desktop_execution.authorize_run(owner_id, body.runId, body.agentId, body.family, active=active)
        return await operation(auth)
    except desktop_execution.DesktopError as exc:
        if exc.code == "run_not_active" and ended is not None:
            return ended
        exc = mac_browser._with_message(exc)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from None
    except HTTPException:
        raise
    except Exception as exc:  # a dependency failed: the class only, never a URL or value
        logger.warning("mac browser operation failed: %s", type(exc).__name__)
        raise HTTPException(
            503, detail={"code": "mac_browser_unavailable", "message": mac_browser.GENERIC_MESSAGE}
        ) from None


@browser_internal_router.post("/{owner_id}/mac/acquire")
async def mac_browser_acquire(owner_id: str, body: MacAcquire, request: Request, response: Response):
    from core.services import mac_browser

    result = await _mac_call(
        owner_id,
        body,
        request,
        response,
        lambda auth: mac_browser.acquire(auth, body.deviceId, budget_s=body.budgetS),
        None,
    )
    if result == mac_browser.STARTING:
        response.status_code = 202  # a sync is running in some bridge; retry shortly
    return result


@browser_internal_router.post("/{owner_id}/mac/release")
async def mac_browser_release(owner_id: str, body: MacRelease, request: Request, response: Response):
    from core.services import mac_browser

    return await _mac_call(
        owner_id,
        body,
        request,
        response,
        lambda auth: mac_browser.release(auth, body.deviceId, body.generation),
        {"sync": None},  # the Mac keeps its changes; the next release saves them
    )


@browser_internal_router.post("/{owner_id}/mac/synced")
async def mac_browser_synced(owner_id: str, body: MacSynced, request: Request, response: Response):
    from core.services import mac_browser

    return await _mac_call(
        owner_id,
        body,
        request,
        response,
        lambda auth: mac_browser.synced(auth, body.deviceId, body.syncId, body.ok),
        {"ok": False},
        # A sync may finish after run.ended (Stop, a slow save): the syncId fence is the guard, and
        # refusing it would leave the claim live for CLAIM_SECONDS.
        active=False,
    )


provision_events_router = APIRouter()


@provision_events_router.post("/execution-events", status_code=204)
async def provision_execution_event(request: Request) -> Response:
    """EventBridge API destination: a provision state-machine execution changed status.

    The machine's own row writes (MarkFailed*) and the provisioner Lambda's never reach
    the backend's write-site publisher, so this is how their outcome reaches the owner's
    open clients -- a refetch nudge, never state. Authenticated by the WS internal shared
    secret, which the EventBridge connection sends as ``x-lightbulb-ws-internal``; checked
    unconditionally (not behind WS_ENFORCE_INTERNAL_SECRET)."""
    import hmac
    import json

    from core.services.status_events import publish_status_changed_now

    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        detail = (await request.json()).get("detail") or {}
        owner_id = json.loads(detail.get("input") or "{}").get("owner_id")
    except (ValueError, AttributeError):
        owner_id = None
    if isinstance(owner_id, str) and owner_id:
        await publish_status_changed_now(owner_id)
    return Response(status_code=204)


@router.post(
    "/provision",
    summary="Provision a container for the current owner",
    description=(
        "Creates a new container for the authenticated owner (user or org). "
        "Idempotent — returns the existing workspace if one already exists. "
        "Only provisions for the resolved owner_id."
    ),
    operation_id="container_provision",
    responses={
        200: {"description": "Container provisioned or already exists"},
        402: {"description": "Provision/start gate is up (structured blocked payload)"},
        503: {"description": "Provisioning failed"},
    },
)
async def container_provision(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await ensure_workspace_trial_default(auth, owner_id)

    # Idempotent: an existing workspace is reported, never re-provisioned. The
    # payment gate below is intentionally NOT applied to it — billing state can
    # churn (cancel + resubscribe) and we don't want a stale 402 to mask the
    # actual workspace the caller is asking about. There is no cold-start door
    # here any more either: AgentCore starts the microVM on the next invoke, so
    # a caller with a row has nothing to ask us to start.
    existing = await container_repo.get_by_owner_id(owner_id)
    if existing:
        return {
            "status": existing.get("status", "unknown"),
            "owner_id": owner_id,
            "already_existed": True,
        }

    # Payment gate (audit C1). New-container path only — the existing-
    # container branch above intentionally bypasses this so we don't 402
    # callers asking about a container they already have.
    await _assert_provision_allowed(
        owner_id,
        is_admin=auth.is_org_admin,
        actor_user_id=auth.user_id,
    )

    # Provision via the PROVISION Step Functions executor (spec §7 / PR-10) —
    # the SOLE provisioning executor, no flag. dispatch_provision is a fast
    # StartExecution call (the real work happens inside
    # scripts.provision_runner, run by the state machine's provisioner Lambda)
    # and never raises: a StartExecution failure releases the row to a
    # retryable status="error", which the stepper shows with its Retry.
    await dispatch_provision(owner_id)
    row = await container_repo.get_by_owner_id(owner_id)
    logger.info("Dispatched provision for owner %s", owner_id)
    return {
        "status": (row or {}).get("status", "provisioning"),
        "owner_id": owner_id,
        "already_existed": False,
    }


@router.post(
    "/retry",
    summary="Retry provisioning for a failed container",
    description=(
        "Retries the full provisioning flow for a container that is in error state. Requires an active subscription."
    ),
    operation_id="container_retry",
    responses={
        404: {"description": "No container for this user"},
        409: {"description": "Container is not in error state"},
        402: {"description": "No active subscription"},
    },
)
async def container_retry(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await ensure_workspace_trial_default(auth, owner_id)
    # Audit C1: same hardened gate as /provision — ID existence isn't
    # enough; status must be active/trialing AND bedrock_claude needs
    # a positive credit balance.
    await _assert_provision_allowed(
        owner_id,
        is_admin=auth.is_org_admin,
        actor_user_id=auth.user_id,
    )

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")
    if container.get("status") not in ("error", "stopped"):
        raise HTTPException(
            status_code=409,
            detail=f"Container is in '{container.get('status')}' state, not retryable",
        )

    # Route through the PROVISION executor (spec §7). dispatch_provision never raises.
    await dispatch_provision(owner_id)
    return {"ok": True}
