"""Container lifecycle management endpoints.

Provides container status (with auto-retry for failed containers)
and a manual retry-provision endpoint.
"""

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from core.auth import AuthContext, get_current_user, is_org_owner_id, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.containers.ecs_manager import (
    START_REFUSED_CAP,
    START_REFUSED_EXPIRED,
    ContainerStartRefused,
    EcsManagerError,
)
from core.services import container_wake, wake_policy
from core.services.management_api_client import ManagementApiClientError
from core.observability.metrics import put_metric, timing
from core.repositories import container_repo
from core.services.container_provision import (
    resolve_provider_choice as _resolve_provider_choice,
    background_provision as _background_provision,
)
from core.services.provision_gate import (
    evaluate_provision_gate,
    maybe_restore_trial_bedrock,
    trial_daily_cap_gate,
    trial_expired_gate,
)
from core.services.stripe_webhook_handlers import CONVERTING_SUBSTATUS, maybe_resume_stuck_conversion
from core.services.user_provision import ensure_personal_trial_default, ensure_user_ready

logger = logging.getLogger(__name__)


# A start refusal carries the SAME code as the provision gate's, so each maps
# straight to the gate that renders it. Keeping them in one map means a new
# refusal reason cannot be added without deciding what the owner sees.
_TRIAL_REFUSAL_GATES = {
    START_REFUSED_CAP: trial_daily_cap_gate,
    START_REFUSED_EXPIRED: trial_expired_gate,
}

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


def _owner_role(auth: AuthContext) -> str:
    """ "admin" | "member" — the role a Gate payload is rendered for.

    Personal owners are always admin of themselves.
    """
    is_admin = auth.is_org_admin if auth.is_org_context else True
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
    wake: str | None = Query(
        default=None,
        description=(
            "Wake marker. '0' = read-only status: never start a parked "
            "(trial_idle) container. '1' = explicit presence-backed wake. "
            "Absent = legacy client; wake-on-read preserved but budgeted "
            "for parked rows. Error/stale-lease self-heal ignores this "
            "param entirely."
        ),
    ),
):
    owner_id = resolve_owner_id(auth)
    # Opportunistic seat reconcile for org owners: converges the Stripe seat
    # quantity to the live member count without a cron, closing any missed
    # membership-webhook gap. Fire-and-forget — the reconciler self-guards to
    # a single DDB read for non-team orgs, and must never add latency or 500
    # the status poll.
    if is_org_owner_id(owner_id):
        from core.services.billing_service import sync_team_seat_quantity

        asyncio.create_task(sync_team_seat_quantity(owner_id))
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
        # Auto-default a brand-new PERSONAL owner into trial_local BEFORE the
        # gate. Otherwise the gate sees no billing row, returns signup_required,
        # and this endpoint 404s below — before ensure_user_ready (the
        # background trial-creator) is ever dispatched — trapping a fresh
        # personal signup on the ProviderPicker instead of provisioning the trial container.
        # Idempotent conditional put; no-op for orgs and for owners who already
        # have a row.
        await ensure_personal_trial_default(auth, owner_id)
        gate = await evaluate_provision_gate(
            owner_id=owner_id,
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

    # Daily Bedrock restore: a capped-out (or previously-expired-and-now-
    # renewed) trial owner's container row may still carry bedrock_revoked
    # from the meter Lambda's kill. The owner is polling US right now, so this
    # is the trigger — no worker. No-op (zero extra reads) unless the flag is
    # actually set. See provision_gate.maybe_restore_trial_bedrock docstring.
    await maybe_restore_trial_bedrock(owner_id=owner_id, container=container)

    # If the row STILL carries the revoke after the restore pass (it declined:
    # capped or expired), gate NOW — whatever the row's status says. The meter
    # Lambda's kill stamps status="stopped", but that write is one non-atomic
    # step of a best-effort kill: if it fails, or a racing backend writer
    # clobbers it, the row stays "running" with no live task, every branch
    # below falls through, and this endpoint returns phase="starting" — an
    # indefinite cold-start stepper with no Upgrade path, hiding the limit
    # card from exactly the owner most likely to convert. Paid owners also
    # carry bedrock_revoked (Metronome low-balance enforcement) — their trial
    # reason is None here, so they fall through unchanged.
    if container.get("bedrock_revoked"):
        from core.services import provision_gate, trial_cap

        revoked_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if revoked_gate is not None:
            raise HTTPException(status_code=402, detail=revoked_gate.to_payload())

    # Trial→paid conversion owns the container row while it stops + drains the
    # trial box (see core/services/stripe_webhook_handlers.py). Two things
    # happen here:
    #
    #   - While a conversion is live, the auto-retry below MUST stand down. The
    #     conversion's reprovision blocks in a ~90s drain wait to guarantee a
    #     single EFS-WAL SQLite writer; the just-paid owner polls this endpoint
    #     every ~3s and sees "stopped", and — being paid — passes the gate. An
    #     auto-retry there dispatches a second provision with NO drain wait and
    #     lands a new task on top of the draining one. That is the documented
    #     corruption / crash-loop mode.
    #
    #   - If the conversion's fire-and-forget task died, nothing else will ever
    #     re-fire it (Stripe's retry sees old_status != "trial_local"), so a
    #     PAYING owner keeps the trial container forever. This poll is the only
    #     thing that runs again, so it re-dispatches — idempotently and
    #     rate-limited to at most once per guard window.
    conversion_active = await maybe_resume_stuck_conversion(owner_id, container)

    # Auto-retry: if container is in a failed/stuck state AND the owner can
    # currently afford to provision, trigger re-provisioning in the
    # background. The same gate as /provision (audit C1) — bare
    # subscription-id existence wasn't enough; we need active/trialing
    # status, and bedrock_claude users need a positive credit balance.
    retryable_states = ("error", "stopped")
    # Stale-lease recovery: a "provisioning" row whose lease has had no
    # checkpoint refresh for PROVISION_CLAIM_STALE_S means the process running
    # the provision died mid-flight (a raise releases the lease to "error"
    # immediately — only hard process death leaves it to go stale). Fold it
    # into the retryable branch so it inherits every gate below, then let
    # try_claim_provision's stale clause decide ATOMICALLY who re-provisions —
    # two polls can both enter here, exactly one wins the re-claim. The ECS
    # liveness guard keeps this from firing during the legitimate post-start
    # boot window (the provision has returned, the lease is no longer being
    # refreshed, but the container is healthy and starting — up to ~12 min).
    stale_provision = container_repo.is_provision_claim_stale(
        container
    ) and not await get_ecs_manager().has_live_service(owner_id)
    if (container.get("status") in retryable_states or stale_provision) and not conversion_active:
        # A container the free tier scaled to zero must NOT auto-retry/wake.
        # Surface the limit card so the frontend shows the block (not a spinner)
        # and the container stays down until the counter resets (UTC midnight)
        # or the owner upgrades.
        #
        # The FULL trial predicate, not just the cap. An expired trial was
        # already denied the wake — _assert_provision_allowed below raises, and
        # its 402 is deliberately swallowed as a may-I-auto-retry predicate — but
        # the owner then got a bare "stopped" container with no explanation
        # instead of the trial_expired card every other door renders.
        from core.services import provision_gate, trial_cap

        trial_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if trial_gate is not None:
            raise HTTPException(status_code=402, detail=trial_gate.to_payload())
        try:
            await _assert_provision_allowed(
                owner_id,
                is_admin=auth.is_org_admin if auth.is_org_context else True,
            )
        except HTTPException:
            pass
        else:
            # Wake-on-intent gate — applies ONLY to a healthy parked trial box
            # (status=stopped + stop_reason=trial_idle + trial_local +
            # !conversion_pending). Error rows, stale leases, killswitch and
            # admin stops all fail the predicate and keep the unconditional
            # poll-driven self-heal below; gates above already 402'd capped or
            # expired owners, so parked can never mask a limit card.
            parked = await wake_policy.is_parked_trial(owner_id, container)
            if parked and wake == "0":
                # Read-only poll from a presence-aware client: report the
                # parked state, start nothing.
                wake_policy.note_wake_suppressed(owner_id, container, door="status_poll", reason="gated")
                return _status_response(container, owner_id, parked=True)
            if parked and wake is None:
                # Legacy client (pre-gate bundle, never reloaded): preserve
                # wake-on-read but bound it to the daily budget so a zombie
                # tab can't run the container around the clock.
                if not await wake_policy.legacy_wake_allowed(owner_id):
                    wake_policy.note_wake_suppressed(owner_id, container, door="status_legacy", reason="budget")
                    # Legacy-shaped response: old bundles get today's field
                    # set (plain stopped), never the new parked fields.
                    return _status_response(container, owner_id)
                wake_policy.note_wake_dispatched(door="status_legacy", intent="poll")
            elif parked:
                wake_policy.note_wake_dispatched(door="status_poll", intent="explicit")
            else:
                wake_policy.note_wake_dispatched(door="status_poll", intent="self_heal")
            # No status pre-write here: background_provision's atomic lease
            # claim IS the transition to "provisioning". A pre-write would (a)
            # race concurrent polls into double-dispatch (the exact incident
            # this closes) and (b) flip the row to "provisioning" with a stale
            # lease, which the claim's stale-clause could then mis-time. The
            # local dict mutation below only shapes THIS response; the row
            # flips ~ms later when the dispatched task wins its claim.
            asyncio.create_task(_background_provision(owner_id, auth.user_id))
            container["status"] = "provisioning"
            container["substatus"] = "auto_retry"

    # Org members (and admins): their paperclip-membership-add doesn't fall
    # out of the missing-container branch above when the org's container
    # already exists (e.g., a co-admin or member joins an org that's already
    # provisioned). The "missing container" branch is the canonical
    # paperclip-add trigger, but anyone sharing the existing container
    # resolves to a container row here and skips it. Fire ensure_user_ready
    # idempotently for EVERY org-context user — excluding admins here was the
    # /teams "team workspace not provisioned" bug for multi-admin orgs.
    # provision_container=False because the container is already running:
    # ensure_user_ready must NOT trigger provision_user_container, which
    # force-restarts a running service (ecs_manager docstring scenario 3).
    if auth.is_org_context:
        asyncio.create_task(ensure_user_ready(auth, provision_container=False))

    return _status_response(container, owner_id)


def _status_response(container: dict, owner_id: str, *, parked: bool = False) -> dict:
    """The one 200 shape for /container/status.

    ``parked=True`` is additive: same base fields (so no consumer schema
    drift) plus ``parked``/``wake_retry_after_s`` and ``phase="parked"`` —
    the phase every stepper consumer must treat as ready-equivalent (a parked
    box is healthy and one intent away from running, not mid-provision).
    """
    body = {
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
    if parked:
        body["phase"] = "parked"
        body["parked"] = True
        body["wake_retry_after_s"] = 30
    return body


@router.post(
    "/wake",
    summary="Explicitly wake the owner's container",
    operation_id="container_wake",
    responses={402: {"description": "Provision/trial gate is up (structured blocked payload)"}},
)
async def container_wake_endpoint(
    auth: AuthContext = Depends(get_current_user),
    reason: str = Body(default="interaction", embed=True),
):
    """The explicit intent verb. Never budgeted, never suppressed — gates
    are the only refusal, and they return the same structured 402 payload as
    every other door. Old backends 404 this route; clients must treat that as
    a per-request soft-fail (never cache it)."""
    owner_id = resolve_owner_id(auth)
    if reason not in container_wake.WAKE_REASONS:
        reason = "interaction"
    result = await container_wake.request_wake(
        owner_id,
        reason=reason,
        is_admin=auth.is_org_admin if auth.is_org_context else True,
        user_id=auth.user_id,
    )
    if result.outcome == "blocked":
        raise HTTPException(status_code=402, detail=result.gate_payload)
    return {"outcome": result.outcome}


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
        "Only provisions for the resolved owner_id (org in org context, user in personal). "
        "Restarting a stopped container passes the start guards: an over-cap "
        "free-tier owner gets the 402 daily-limit gate, and a container a "
        "trial→paid conversion is reprovisioning is left alone (200, "
        "status=provisioning)."
    ),
    operation_id="container_provision",
    responses={
        200: {"description": "Container provisioned, already exists, or is being reprovisioned"},
        402: {"description": "Provision/start gate is up (structured blocked payload)"},
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
            #
            # The payment gate is deliberately NOT applied here (see the comment
            # above), which is precisely why the two *start* guards — the
            # free-tier daily cap and the trial->paid conversion — live inside
            # start_user_service instead of being duplicated into this branch.
            # This endpoint is the second door onto the same stopped container
            # that GET /container/status auto-retries; before the guards moved
            # down, a capped-out trial owner could simply POST here to wake the
            # container the cap had just put to sleep, and a POST landing during
            # a conversion's ~90s drain re-opened the EFS-WAL double-writer race.
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
            except ContainerStartRefused as refusal:
                # A guard is up. NOT a failure (so no cold_start error metric,
                # no 503): the container is healthy, we are declining to wake it.
                put_metric(
                    "gateway.cold_start.count",
                    dimensions={"outcome": "refused"},
                )
                if refusal.reason in _TRIAL_REFUSAL_GATES:
                    # The same 402 card GET /container/status returns — same gate,
                    # same payload, whichever door the owner knocks on.
                    raise HTTPException(
                        status_code=402,
                        detail=_TRIAL_REFUSAL_GATES[refusal.reason](_owner_role(auth)).to_payload(),
                    )
                # Conversion in flight. The container IS coming back — the
                # conversion's own reprovision will start it once the old task has
                # drained — so this is not an error to surface. Answer with the
                # pollable provisioning shape this endpoint already returns (it is
                # documented as idempotent) and let the caller poll
                # /container/status, which reports the same converting state.
                logger.info(
                    "Refused to wake container for owner %s: trial-conversion reprovision in flight",
                    owner_id,
                )
                return {
                    "status": "provisioning",
                    "substatus": CONVERTING_SUBSTATUS,
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
