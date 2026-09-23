"""Container lifecycle management endpoints.

Provides the container status read and the manual provision/retry endpoints.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, HttpUrl
from typing import Literal

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.services import browser_control, readiness_state
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import ensure_workspace_trial_default

logger = logging.getLogger(__name__)

router = APIRouter()


def _watch_provision_task(task: "asyncio.Task") -> None:
    """Done-callback for the detached ``ensure_user_ready`` dispatches.

    ``ensure_user_ready`` is fired with ``asyncio.create_task`` from the Paperclip-proxy
    handshake and never awaited, so a raised exception would otherwise be a silent
    "Task exception was never retrieved" — invisible to the W1
    provision-error alarm. Surface it: log the task's traceback and emit the
    SAME ``container.provision`` ``{status: error}`` metric the synchronous
    provision path emits (see ``container_provision.emit_provision_metric``),
    so a dispatch-path failure lands on the exact CloudWatch stream that alarm
    already watches. A cancelled task carries no failure. Best-effort
    throughout — a done-callback must never raise.
    """
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is None:
        return
    logger.error("Background ensure_user_ready task failed", exc_info=exc)
    try:
        put_metric("container.provision", dimensions={"status": "error"})
    except Exception:  # noqa: BLE001 — telemetry must never raise in a callback
        logger.debug("container.provision metric emission failed", exc_info=True)


def _dispatch_watched(coro) -> None:
    """Fire-and-forget a provisioning coroutine with the exception-surfacing
    done-callback attached (Part A observability for the ``ensure_user_ready``
    dispatches).

    ``add_done_callback`` is a real-``asyncio.Task`` method; unit tests patch
    ``asyncio.create_task`` and may hand back a non-Task (``None``, a bare
    coroutine, or a ``MagicMock``), so attach the callback only when a genuine
    Task came back. In production ``create_task`` always returns a Task.
    """
    task = asyncio.create_task(coro)
    if isinstance(task, asyncio.Task):
        task.add_done_callback(_watch_provision_task)


def _owner_role(auth: AuthContext) -> str:
    """ "admin" | "member" — the role a Gate payload is rendered for."""
    is_admin = auth.is_org_admin
    return "admin" if is_admin else "member"


async def _assert_provision_allowed(
    owner_id: str,
    *,
    is_admin: bool = True,
) -> None:
    """Raise 402 with a structured `blocked` payload if a provision gate fires.

    Delegates to ``core.services.provision_gate.evaluate_provision_gate`` so
    /container/provision and /container/status share the same logic and can
    never disagree about whether a gate is up.
    """
    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        is_admin=is_admin,
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
        gate = await evaluate_provision_gate(owner_id=owner_id, is_admin=auth.is_org_admin)
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
#: panel's URL is ``/api/v1/browser/live-view``. Same two-routers-in-one-module shape
#: ``connections.oauth_router`` already uses.
browser_router = APIRouter()


@browser_router.get(
    "/live-view",
    summary="Presigned live view of this workspace's running browser session",
    description=(
        "Returns a 300 s presigned DCV URL for the AgentCore Browser session this "
        "workspace's agents are driving, or `active: false` when none is running. "
        "Polling never starts a session; controller metadata gates the workspace UI."
    ),
    operation_id="browser_live_view",
)
async def browser_live_view(
    response: Response,
    auth: AuthContext = Depends(get_current_user),
):
    # The URL is a short-lived bearer credential for a live browser: no CDN, no proxy, no
    # disk cache. The owner comes from the Clerk claim and never from the request.
    response.headers["Cache-Control"] = "no-store"
    return await _browser_result(browser_control.view(resolve_owner_id(auth), auth.user_id))


class BrowserGeneration(BaseModel):
    generation: str = Field(min_length=1, max_length=128)


class BrowserControlRequest(BrowserGeneration):
    action: Literal["take", "release"]


class BrowserNavigateRequest(BrowserGeneration):
    url: HttpUrl


class BrowserRun(BaseModel):
    runId: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")


class BrowserRunRelease(BrowserRun, BrowserGeneration):
    pass


async def _browser_result(operation):
    try:
        return await operation
    except browser_control.BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        # A failed signed URL/RPC must never leak its credential-bearing exception.
        raise HTTPException(status_code=503, detail="Browser is temporarily unavailable. Try again.") from None


@browser_router.post("/start")
async def browser_start(response: Response, auth: AuthContext = Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    owner_id = resolve_owner_id(auth)
    await _assert_provision_allowed(owner_id, is_admin=auth.is_org_admin)
    return await _browser_result(browser_control.start(owner_id, auth.user_id))


@browser_router.post("/control")
async def browser_change_control(
    body: BrowserControlRequest, response: Response, auth: AuthContext = Depends(get_current_user)
):
    response.headers["Cache-Control"] = "no-store"
    owner_id = resolve_owner_id(auth)
    if body.action == "take":
        await _assert_provision_allowed(owner_id, is_admin=auth.is_org_admin)
    action = browser_control.take if body.action == "take" else browser_control.release_human
    return await _browser_result(action(owner_id, auth.user_id, body.generation))


@browser_router.post("/stop")
async def browser_stop(body: BrowserGeneration, response: Response, auth: AuthContext = Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    return await _browser_result(browser_control.stop(resolve_owner_id(auth), auth.user_id, body.generation))


@browser_router.post("/navigate")
async def browser_navigate(
    body: BrowserNavigateRequest, response: Response, auth: AuthContext = Depends(get_current_user)
):
    response.headers["Cache-Control"] = "no-store"
    if body.url.username or body.url.password:
        raise HTTPException(status_code=422, detail="Website addresses cannot include credentials.")
    return await _browser_result(
        browser_control.navigate(resolve_owner_id(auth), auth.user_id, body.generation, str(body.url))
    )


browser_internal_router = APIRouter()


@browser_internal_router.post("/{owner_id}/acquire")
async def browser_agent_acquire(owner_id: str, body: BrowserRun, request: Request, response: Response):
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    response.headers["Cache-Control"] = "no-store"
    return await _browser_result(browser_control.acquire_agent(owner_id, body.runId))


@browser_internal_router.post("/{owner_id}/release")
async def browser_agent_release(owner_id: str, body: BrowserRunRelease, request: Request, response: Response):
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
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
