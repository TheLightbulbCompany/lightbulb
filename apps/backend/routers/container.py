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
from core.services import browser_control, browser_extensions, readiness_state
from core.repositories import container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import ensure_workspace_trial_default

logger = logging.getLogger(__name__)

router = APIRouter()


async def _assert_provision_allowed(owner_id: str, *, actor_user_id: str | None = None) -> None:
    """Raise 402 with a structured `blocked` payload if the workspace gate is up.

    Only the browser's start-on-view door uses it now: starting a browser session spends, and
    provisioning does not. ``actor_user_id`` is the caller, so a trial-expired block lands on
    their PostHog person.
    """
    gate = await evaluate_provision_gate(owner_id=owner_id, actor_user_id=actor_user_id)
    if gate is not None:
        raise HTTPException(status_code=402, detail=gate.to_payload())


@router.get(
    "/status",
    summary="Get container metadata for current user",
    description=(
        "A cold read of the workspace's status contract, fetched on mount and after a "
        "live event. It triggers no provisioning: a first provision starts from "
        "POST /container/provision, and a failed one is retried by POST /container/retry. Its one write is creating an admin's "
        "missing trial row (create-if-absent), so a read that beats the webhook sees the trial."
    ),
    operation_id="container_status",
    responses={
        402: {"description": "Chat and orb cannot run now (structured blocked payload)"},
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
        # read can beat the trial row's creation, so it is created here
        # too (create-if-absent -- a write, not a trigger) or the gate would read "no
        # billing" and flash the paywall.
        await ensure_workspace_trial_default(auth, owner_id)
        gate = await evaluate_provision_gate(owner_id=owner_id, actor_user_id=auth.user_id)
        if gate is not None:
            raise HTTPException(status_code=402, detail=gate.to_payload())
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

        trial_gate = provision_gate.gate_for_trial_reason(await trial_cap.trial_block_reason(owner_id))
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
    await _assert_provision_allowed(owner_id, actor_user_id=auth.user_id)
    return await _browser_result(browser_control.watch(owner_id, auth.user_id, frames=bool(body and body.frames)))


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
    from core.services.hook_grants import require_hook_token

    await require_hook_token(owner_id, request)
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
    from core.services.hook_grants import require_hook_token

    await require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    result = await _browser_result(browser_control.acquire_agent(owner_id, body.runId, body.agentId))
    if result == browser_control.STARTING:
        response.status_code = 202  # a cold start runs on in the background; the bridge polls
    return result


@browser_internal_router.post("/{owner_id}/release")
async def browser_agent_release(owner_id: str, body: BrowserRunRelease, request: Request, response: Response):
    from core.services.hook_grants import require_hook_token

    await require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    return await _browser_result(browser_control.release_agent(owner_id, body.runId, body.generation))


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
        503: {"description": "Provisioning failed"},
    },
)
async def container_provision(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await ensure_workspace_trial_default(auth, owner_id)

    # Idempotent: an existing workspace is reported, never re-provisioned. There is no
    # cold-start door here: AgentCore starts the microVM on the next invoke. No billing gate
    # either: a workspace on the shared pool costs nothing until it runs, and chat, the orb
    # and the phone each gate their own spend.
    existing = await container_repo.get_by_owner_id(owner_id)
    if existing:
        return {
            "status": existing.get("status", "unknown"),
            "owner_id": owner_id,
            "already_existed": True,
        }

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
        "Retries the full provisioning flow for a workspace in error state. Not billing-gated: "
        "provisioning creates nothing billable."
    ),
    operation_id="container_retry",
    responses={
        404: {"description": "No container for this user"},
        409: {"description": "Container is not in error state"},
    },
)
async def container_retry(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await ensure_workspace_trial_default(auth, owner_id)

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
