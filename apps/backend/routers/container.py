"""Container lifecycle management endpoints.

Provides container status (with auto-retry for failed containers)
and a manual retry-provision endpoint.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.containers.ecs_manager import EcsManagerError
from core.services.management_api_client import ManagementApiClientError
from core.observability.metrics import put_metric, timing
from core.repositories import container_repo
from core.services.container_provision import (
    resolve_provider_choice as _resolve_provider_choice,
    background_provision as _background_provision,
)
from core.services.provision_gate import evaluate_provision_gate
from core.services.user_provision import ensure_user_ready

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_cold_start_phase(container: dict, gateway_pool, owner_id: str) -> str:
    """Map (DDB container.status, gateway pool connection state) -> phase.

    Phases the frontend renders as a stepper. The gap between
    `provisioning` and `ready` is normally ~12 min today (5+ min wedge in
    upstream's sidecars.channels phase), so showing the user *something*
    during that window is the whole point of this surface.

    Returns:
      - "provisioning"  ECS task PROVISIONING / PENDING / STOPPED / error
      - "starting"      ECS task RUNNING but our pool hasn't completed
                        the OpenClaw handshake + health verification yet.
                        Covers gateway boot + sidecars.channels + qmd init.
      - "ready"         pool.is_user_connected() — openclaw is responsive
                        to RPCs, chat is usable.
    """
    status = (container or {}).get("status")
    if status != "running":
        return "provisioning"
    if gateway_pool.is_user_connected(owner_id):
        return "ready"
    return "starting"


async def _assert_provision_allowed(
    owner_id: str,
    clerk_user_id: str,
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
        clerk_user_id=clerk_user_id,
        is_admin=is_admin,
    )
    if gate is not None:
        raise HTTPException(status_code=402, detail=gate.to_payload())


@router.get(
    "/status",
    summary="Get container metadata for current user",
    description=(
        "Returns the user's container status and metadata. "
        "If no container row exists yet and the provision gate is clear, "
        "dispatches ensure_user_ready in the background and returns a "
        "synthetic provisioning response (owners) or 202 'workspace pending' "
        "(org members). If the container is in error state and the user "
        "has an active subscription, auto-triggers re-provisioning in the "
        "background."
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
    ecs_manager = get_ecs_manager()
    # Use resolve_running_container so polling triggers the
    # provisioning -> running health-check transition.
    container, _ip = await ecs_manager.resolve_running_container(owner_id)
    if not container:
        # Fall back to get_service_status for error/stopped containers
        container = await ecs_manager.get_service_status(owner_id)
    if not container:
        # No container row at all. Evaluate the same provision gate
        # POST /provision uses — if a gate is up, the frontend needs the
        # structured payload (402) so it can render the picker / "top up"
        # CTA instead of trapping the user in a generic "no container"
        # state.
        is_admin = auth.is_org_admin if auth.is_org_context else True
        gate = await evaluate_provision_gate(
            owner_id=owner_id,
            clerk_user_id=auth.user_id,
            is_admin=is_admin,
        )
        if gate is not None:
            # signup_required + admin == "owner has never started
            # signup" (no billing row at all). The ProviderPicker on
            # /chat is the only checkout entry point
            # (POST /billing/trial-checkout); surfacing 402 here traps
            # the admin in a Subscribe button that loops
            # /onboarding → /chat with no way to start checkout.
            # Return 404 so useProvisioningState maps it to
            # phase="needs-signup" and renders the picker. Inactive-row
            # gates (subscription_required for canceled/unpaid/paused,
            # or payment_past_due) keep their 402 + structured action
            # so the user gets routed to /settings/billing instead —
            # those owners already have a subscription and
            # trial-checkout would 409 already_subscribed on click.
            # Members always see the gate so they get "ask your org
            # admin" instead of a picker that would 403 on click.
            if gate.code == "signup_required" and is_admin:
                raise HTTPException(status_code=404, detail="No container found")
            raise HTTPException(status_code=402, detail=gate.to_payload())

        # Gate clear, no container row. After Tasks 6 + 7 stripped the
        # eager Clerk-webhook provisioning, this endpoint is the canonical
        # poll-driven trigger that drives every authenticated user from
        # "Clerk-authed" to "container provisioning." Fire ensure_user_ready
        # in the background; it's idempotent and gate-checks again itself.
        #
        # Org members can't drive container provisioning themselves —
        # they ride the owner's container. But ensure_user_ready (member
        # role) handles paperclip-membership-add against the owner's
        # company; if the owner's company isn't active yet, it returns
        # gracefully. Surface 202 so the frontend renders "workspace
        # pending" rather than a generic loading state.
        if auth.is_org_context and not auth.is_org_admin:
            asyncio.create_task(ensure_user_ready(auth))
            raise HTTPException(
                status_code=202,
                detail="team workspace pending",
            )

        # Owner role: dispatch ensure_user_ready and return a synthetic
        # provisioning response. Shape mirrors the existing 200-OK return
        # below so the frontend's phase=="provisioning" UI handles this
        # without schema drift.
        asyncio.create_task(ensure_user_ready(auth))
        return {
            "service_name": None,
            "status": "provisioning",
            "substatus": "ensure_user_ready",
            "created_at": None,
            "updated_at": None,
            "region": settings.AWS_REGION,
            "last_error": None,
            "last_error_at": None,
            "phase": "provisioning",
        }

    # Auto-retry: if container is in a failed/stuck state AND the owner can
    # currently afford to provision, trigger re-provisioning in the
    # background. The same gate as /provision (audit C1) — bare
    # subscription-id existence wasn't enough; we need active/trialing
    # status, and bedrock_claude users need a positive credit balance.
    retryable_states = ("error", "stopped")
    if container.get("status") in retryable_states:
        try:
            await _assert_provision_allowed(
                owner_id,
                auth.user_id,
                is_admin=auth.is_org_admin if auth.is_org_context else True,
            )
        except HTTPException:
            pass
        else:
            await container_repo.update_status(owner_id, "provisioning", "auto_retry")
            asyncio.create_task(_background_provision(owner_id, auth.user_id))
            container["status"] = "provisioning"
            container["substatus"] = "auto_retry"

    # Org members: their paperclip-membership-add doesn't fall out of the
    # missing-container branch above when the org owner's container already
    # exists (e.g., member joins an org that's already provisioned). The
    # "missing container" branch is the canonical paperclip-add trigger, but
    # an org member sharing the owner's container resolves to an existing
    # row here and skips it. Fire ensure_user_ready idempotently —
    # _ensure_paperclip_for_member treats provision_member's duplicate-409
    # as success, so this is safe to invoke on every member status poll.
    if auth.is_org_context and not auth.is_org_admin:
        asyncio.create_task(ensure_user_ready(auth))

    return {
        "service_name": container.get("service_name"),
        "status": container.get("status"),
        "substatus": container.get("substatus"),
        "created_at": container.get("created_at"),
        "updated_at": container.get("updated_at"),
        "region": settings.AWS_REGION,
        "last_error": container.get("last_error"),
        "last_error_at": container.get("last_error_at"),
        # Cold-start phase for the frontend stepper. See
        # _resolve_cold_start_phase for the (status, pool) -> phase map.
        "phase": _safe_phase(container, owner_id),
    }


def _safe_phase(container: dict, owner_id: str) -> str:
    """Wrap _resolve_cold_start_phase so a not-yet-initialized gateway
    pool doesn't 500 the status endpoint. Status must always answer.

    On pool init failure (ManagementApiClientError when
    WS_MANAGEMENT_API_URL is unset — typically test envs that skip the
    lifespan handler) we fall back to a *container-only* phase mapping
    instead of forcing "starting":

      - ECS RUNNING  -> "ready"        (best guess; chat may or may
                                        not work — but the frontend will
                                        surface the real chat error
                                        instead of trapping the user
                                        in a never-ending stepper)
      - anything else -> "provisioning"

    Returning "starting" here keeps users stuck in the cold-start UI
    indefinitely even when the container itself is healthy and usable.

    Anything other than ManagementApiClientError bubbles up as a real
    bug. Logs once per process so a production misconfiguration shows
    up in CloudWatch.
    """
    try:
        pool = get_gateway_pool()
    except ManagementApiClientError as exc:
        if not getattr(_safe_phase, "_pool_warning_logged", False):
            logger.warning(
                "Gateway pool not initialized; status phase falls back to container-only mapping. Real pool error: %s",
                exc,
            )
            _safe_phase._pool_warning_logged = True  # type: ignore[attr-defined]
        return "ready" if (container or {}).get("status") == "running" else "provisioning"
    return _resolve_cold_start_phase(container, pool, owner_id)


@router.post(
    "/provision",
    summary="Provision a container for the current owner",
    description=(
        "Creates a new container for the authenticated owner (user or org). "
        "Idempotent — returns existing container if one already exists. "
        "Only provisions for the resolved owner_id (org in org context, user in personal)."
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

    # Check if container already exists (idempotent). The payment gate
    # below is intentionally NOT applied to existing containers — billing
    # state can churn (cancel + resubscribe) and we don't want a stale
    # 402 to mask the actual container the caller is asking about.
    existing = await container_repo.get_by_owner_id(owner_id)
    if existing:
        if existing.get("status") == "stopped":
            # Container was scaled to zero -- restart it. This is the
            # cold-start path; emit a metric so we can answer "how often do
            # users hit a cold start" and "how long does the restart take".
            try:
                with timing("gateway.cold_start.latency"):
                    await get_ecs_manager().start_user_service(owner_id)
                put_metric("gateway.cold_start.count", dimensions={"outcome": "ok"})
                logger.info("Restarted stopped container for owner %s", owner_id)
                return {
                    "status": "provisioning",
                    "service_name": existing.get("service_name"),
                    "owner_id": owner_id,
                    "already_existed": True,
                }
            except EcsManagerError as e:
                put_metric("gateway.cold_start.count", dimensions={"outcome": "error"})
                logger.error("Restart failed for stopped container, owner %s: %s", owner_id, e)
                raise HTTPException(status_code=503, detail=str(e))
        return {
            "status": existing.get("status", "unknown"),
            "service_name": existing.get("service_name"),
            "owner_id": owner_id,
            "already_existed": True,
        }

    # Payment gate (audit C1). New-container path only — the existing-
    # container branch above intentionally bypasses this so we don't 402
    # callers asking about a container they already have.
    await _assert_provision_allowed(
        owner_id,
        auth.user_id,
        is_admin=auth.is_org_admin if auth.is_org_context else True,
    )

    # Provision new container — read the owner's saved provider_choice so the
    # container is configured for the right LLM path (OAuth / BYO key /
    # bedrock_claude). Keyed on owner_id via billing_repo.
    try:
        provider_choice, byo_provider = await _resolve_provider_choice(owner_id)
        service_name = await get_ecs_manager().provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
        )
        logger.info("Provisioned container %s for owner %s", service_name, owner_id)
        return {
            "status": "provisioning",
            "service_name": service_name,
            "owner_id": owner_id,
            "already_existed": False,
        }
    except EcsManagerError as e:
        logger.error("Provisioning failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=503, detail=str(e))


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
    # Audit C1: same hardened gate as /provision — ID existence isn't
    # enough; status must be active/trialing AND bedrock_claude needs
    # a positive credit balance.
    await _assert_provision_allowed(
        owner_id,
        auth.user_id,
        is_admin=auth.is_org_admin if auth.is_org_context else True,
    )

    ecs_manager = get_ecs_manager()
    container = await ecs_manager.get_service_status(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")
    if container.get("status") not in ("error", "stopped"):
        raise HTTPException(
            status_code=409,
            detail=f"Container is in '{container.get('status')}' state, not retryable",
        )

    try:
        provider_choice, byo_provider = await _resolve_provider_choice(owner_id)
        service_name = await ecs_manager.provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
        )
    except EcsManagerError as e:
        logger.error("Retry provisioning failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Provisioning failed")

    return {"ok": True, "service_name": service_name}
