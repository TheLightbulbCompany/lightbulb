"""Container lifecycle management endpoints.

Provides container status (with auto-retry for failed containers)
and a manual retry-provision endpoint.
"""

import asyncio
import logging
import time

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
from core.services import readiness_state
from core.services.bootstrap_dispatcher import maybe_dispatch_bootstrap
from core.services.onboarding_brief import reconcile_pending_brief
from core.services.management_api_client import ManagementApiClientError
from core.observability.metrics import put_metric, timing
from core.repositories import container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.provision_gate import (
    evaluate_provision_gate,
    maybe_restore_trial_bedrock,
    trial_daily_cap_gate,
    trial_expired_gate,
)
from core.services.stripe_webhook_handlers import CONVERTING_SUBSTATUS, maybe_resume_stuck_conversion
from core.services.user_provision import ensure_personal_trial_default, ensure_user_ready

logger = logging.getLogger(__name__)

# Dispatch guards for the status poll's pool self-heal dial. The pool
# serializes ALL connect I/O behind one process-wide lock — and every chat
# RPC (send_rpc → ensure_connection) queues on that same lock — while the
# absent-create path has no reconnect floor. So the dispatch site must not
# let the ~5s poll cadence pile up dials: the in-flight set is a GLOBAL
# single-flight (one self-heal dial outstanding per process, whoever it is
# for — a wake burst of N owners must not queue N serial handshake holds in
# front of every healthy user's chat RPCs; each blocked owner's next poll
# retries within seconds of the slot freeing), and the per-owner cooldown
# stops a wedged container (running row + unreachable IP) from eating a
# fresh handshake-timeout lock hold per poll. A chat send still dials
# immediately through its own path — these guards only pace the background
# repair.
_SELF_HEAL_DIAL_COOLDOWN_S = 60.0
# A failed dial that returned faster than this held the global connect lock
# only briefly (connection refused during the gateway's boot window) — it is
# exempted from the cooldown so the next poll retries. Anything slower ate a
# real handshake wait and keeps the full cooldown.
_SELF_HEAL_FAST_FAIL_S = 2.0
_self_heal_inflight: set[str] = set()
_self_heal_last_dial: dict[str, float] = {}


def _self_heal_dial_allowed(owner_id: str) -> bool:
    """True when NO self-heal dial is outstanding process-wide and this
    owner's last attempt is at least the cooldown ago (or never happened)."""
    if _self_heal_inflight:
        return False
    last = _self_heal_last_dial.get(owner_id)
    return last is None or (time.monotonic() - last) >= _SELF_HEAL_DIAL_COOLDOWN_S


# A start refusal carries the SAME code as the provision gate's, so each maps
# straight to the gate that renders it. Keeping them in one map means a new
# refusal reason cannot be added without deciding what the owner sees.
_TRIAL_REFUSAL_GATES = {
    START_REFUSED_CAP: trial_daily_cap_gate,
    START_REFUSED_EXPIRED: trial_expired_gate,
}

router = APIRouter()


def _watch_provision_task(task: "asyncio.Task") -> None:
    """Done-callback for the detached ``ensure_user_ready`` dispatches.

    ``ensure_user_ready`` is fired with ``asyncio.create_task`` from the status
    poll and never awaited, so a raised exception would otherwise be a silent
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


def _resolve_cold_start_phase(container: dict, gateway_pool, owner_id: str) -> str:
    """LEGACY pure (status, pool) -> phase map — retained ONLY because
    ``TestColdStartPhaseResolver`` (tests/unit/routers/test_container_status.py)
    still imports and calls it directly with a duck-typed pool. Nothing in
    production calls this anymore: the real mapping now lives in
    ``core.services.readiness_state.resolve_runtime_phase`` (INV-1 — a
    router may not itself compose the containers-status + gateway-liveness
    inputs), fed a plain bool sourced from the connection-service DDB table
    instead of an in-process pool object. Safe to delete alongside that
    test class in a follow-up cleanup.

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


def _maybe_drain_pending_brief(owner_id: str, onboarding: dict) -> None:
    """Fire the onboarding-brief drain if this owner has an answer waiting.

    The funnel renders DURING provisioning, so a first-time user's brief
    almost always lands before there is an owner row to claim it against;
    ``POST /onboarding/brief`` records it and returns, and THIS poll is what
    fulfils it (see routers/onboarding.py and
    core.services.onboarding_brief.reconcile_pending_brief).

    ``pending_brief`` rides the SAME onboarding dict compute_status consumes --
    one read for both -- and is False for every owner whose funnel has
    completed, so the steady-state fleet never schedules this. The reconciler
    re-reads consistently, claims atomically and dedupes concurrent drains for
    the same owner; this pre-check exists purely to keep the common poll from
    scheduling a no-op task.
    """
    if onboarding.get("pending_brief"):
        asyncio.create_task(reconcile_pending_brief(owner_id))


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

    # Drain a funnel answer submitted before the workspace could take it, BEFORE
    # any of this endpoint's early returns. It used to sit at the bottom, which
    # quietly excluded whole cohorts from ever fulfilling: a parked trial (returns
    # at the wake gate), a bedrock_revoked or trial-cap 402, an org member on the
    # no-container path (202). None of those block the drain itself -- it needs
    # Paperclip, not a running container -- so they were dead ends for no reason.
    #
    # Costs one paperclip-companies read on every poll, which is why it is also
    # threaded into compute_status below rather than read twice.
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    _maybe_drain_pending_brief(owner_id, onboarding)

    ecs_manager = get_ecs_manager()
    # Use resolve_running_container so polling triggers the
    # provisioning -> running health-check transition.
    container, container_ip = await ecs_manager.resolve_running_container(owner_id)
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
            _dispatch_watched(ensure_user_ready(auth))
            raise HTTPException(
                status_code=202,
                detail="team workspace pending",
            )

        # Owner role: dispatch ensure_user_ready and return a provisioning
        # response composed through compute_status (which handles
        # container=None -> runtime_phase="provisioning") rather than a
        # hand-built stub. Two reasons this must NOT hardcode onboarded=False:
        #  1. Container-only recreation (G1 bumped, the company + G2 onboarding
        #     preserved) transiently has no container row; hardcoding False
        #     here would re-show a COMPLETED funnel. compute_status reads the
        #     preserved onboarding state (funnel_state=="complete") independent
        #     of the container, so onboarded rides correctly.
        #  2. Wire-shape parity: the has-container return below emits the full
        #     StatusContract; this path must too, or a no-container poll gives
        #     deriveUIPhase a different (legacy-only) shape.
        # We reflect the just-issued dispatch on the (still-absent) row's
        # status fields; lifecycle/runtime_phase/onboarded stay as composed
        # (e.g. an org owner with no provider correctly surfaces
        # provider_selection_required rather than a false "provisioning").
        _dispatch_watched(ensure_user_ready(auth))
        contract = await readiness_state.compute_status(owner_id, container=None, onboarding=onboarding)
        contract["container_status"] = "provisioning"
        contract["container_substatus"] = "ensure_user_ready"
        return _status_response(contract)

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

    # Single readiness+onboarding read (INV-1): compute_status is the SOLE
    # composer of the containers/billing/gateway-liveness/paperclip-companies
    # inputs — see core/services/readiness_state.py. Passing the
    # already-fetched `container` skips a redundant containers-row read and
    # lets the contract reflect the LOCAL (pre-commit) mutations the
    # auto-retry branch below makes to `container["status"]` — see
    # `_sync_contract_with_container`, called after each such mutation.
    #
    # Includes the degraded Teams signal (non-blocking): if this owner's
    # Paperclip company seed failed, a running container still serves chat —
    # `contract["teams_state"] == "failed"` surfaces alongside a normal
    # phase so the client can render a "Teams needs attention / retry"
    # affordance instead of a hard block. Never gates.
    #
    # `onboarding` was fetched once at the top of this handler (for the drain
    # hook) and is threaded into BOTH compute_status and the bootstrap-dispatch
    # hook below — it is the same paperclip-companies read compute_status would
    # otherwise do internally (a by-org-id GSI query for an org owner), so
    # without threading it every ~3s ready poll fleet-wide would pay it twice.
    contract = await readiness_state.compute_status(owner_id, container=container, onboarding=onboarding)

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
            if container.get("status") == "error":
                # Honest error reporting — never mask a failed provision as
                # "provisioning". T3 records the cause on the row:
                #   retryable=True  (transient infra: Spot reclaim / throttle /
                #     5xx) → still auto-dispatch the re-provision, but SAY so:
                #     status stays "error", substatus="error_retrying", and
                #     last_error is carried through by _status_response. The
                #     client shows "hit a problem, retrying" — no timer needed.
                #   retryable=False (deterministic: 4xx / config / validation)
                #     → do NOT re-dispatch; the recorded status + last_error
                #     render the error card immediately.
                # Default True keeps a cause-less legacy error row self-healing
                # (the pre-T3 auto-retry behaviour).
                if container.get("retryable", True):
                    asyncio.create_task(dispatch_provision(owner_id))
                    container["substatus"] = "error_retrying"
                    _sync_contract_with_container(contract, container)
            else:
                # stopped / stale-provisioning lease: UNCHANGED silent auto-wake.
                # Wake-on-intent gate — applies ONLY to a healthy parked trial
                # box (status=stopped + stop_reason=trial_idle + trial_local +
                # !conversion_pending). Stale leases, killswitch and admin stops
                # all fail the predicate and keep the unconditional poll-driven
                # self-heal below; gates above already 402'd capped or expired
                # owners, so parked can never mask a limit card.
                parked = await wake_policy.is_parked_trial(owner_id, container)
                if parked and wake == "0":
                    # Read-only poll from a presence-aware client: report the
                    # parked state, start nothing.
                    wake_policy.note_wake_suppressed(owner_id, container, door="status_poll", reason="gated")
                    return _status_response(contract, parked=True)
                if parked and wake is None:
                    # Legacy client (pre-gate bundle, never reloaded): preserve
                    # wake-on-read but bound it to the daily budget so a zombie
                    # tab can't run the container around the clock.
                    if not await wake_policy.legacy_wake_allowed(owner_id):
                        wake_policy.note_wake_suppressed(owner_id, container, door="status_legacy", reason="budget")
                        # No explicit-wake client is polling here (this is the
                        # `wake is None` branch), so report the contract as-is
                        # -- no `wake_retry_after_s` override -- rather than
                        # forcing the explicit-wake shape via `parked=True`.
                        return _status_response(contract)
                    wake_policy.note_wake_dispatched(door="status_legacy", intent="poll")
                elif parked:
                    wake_policy.note_wake_dispatched(door="status_poll", intent="explicit")
                else:
                    wake_policy.note_wake_dispatched(door="status_poll", intent="self_heal")
                # No status pre-write here: dispatch_provision's atomic lease
                # claim IS the transition to "provisioning". A pre-write would (a)
                # race concurrent polls into double-dispatch (the exact incident
                # this closes) and (b) flip the row to "provisioning" with a stale
                # lease, which the claim's stale-clause could then mis-time. The
                # local dict mutation below only shapes THIS response; the row
                # flips ~ms later when the dispatched task wins its claim.
                asyncio.create_task(dispatch_provision(owner_id))
                container["status"] = "provisioning"
                container["substatus"] = "auto_retry"
                _sync_contract_with_container(contract, container)

    # Re-dispatch ensure_user_ready on the running-container poll for: (a) EVERY
    # org-context user — idempotent paperclip-membership-add (a co-admin/member
    # joining an already-provisioned org resolves to a container row here and would
    # otherwise skip the add); and (b) a PERSONAL owner whose Teams seed is currently
    # failed — T1 made a seed failure non-fatal to the container, so without this a
    # transient personal seed blip stays teams_status="failed" forever with no
    # self-heal. Gating personal on teams_status=="failed" keeps the happy path free
    # of a per-poll re-seed. Safe on a running box with no flag: ensure_user_ready
    # reads the container row itself and reconciles the seed in-thread instead of
    # dispatching a provision (see _provision_or_reconcile).
    if auth.is_org_context or contract["teams_state"] == "failed":
        _dispatch_watched(ensure_user_ready(auth))

    # Server-owned bootstrap dispatch (INV-5, spec PR-6). Gate on the container
    # being RUNNING ("starting" OR "ready"), NOT on "ready" alone: "ready"
    # requires a live gateway connection, but during onboarding the funnel hides
    # the chat surface so no chat RPC ever opens one -- gating on "ready" would
    # DEADLOCK (funnel_state=submitted, bootstrap_state=not_started, forever,
    # because the only thing that would open the connection is this dispatch).
    # maybe_dispatch_bootstrap OPENS its own connection (resolve_running_container
    # + ensure_connection) to send Friday's kickoff, so it only needs a running
    # container; a "starting" attempt before OpenClaw is listening fails
    # gracefully and retries on the next poll, and the connection it establishes
    # is what flips the next poll to "ready". The funnel_state/bootstrap_state
    # pre-check (mirroring the dispatcher's own guards, off the SAME `onboarding`
    # dict compute_status already consumed -- zero extra reads) keeps the
    # overwhelming majority of polls from even scheduling a no-op task; the
    # dispatcher's atomic claim remains the real single-fire fence (INV-5).
    if (
        contract["runtime_phase"] in ("starting", "ready")
        and onboarding.get("funnel_state") == "submitted"
        and (onboarding.get("bootstrap_state") or "not_started") == "not_started"
    ):
        asyncio.create_task(maybe_dispatch_bootstrap(owner_id, onboarding=onboarding))

    # runtime_phase "ready" requires TWO truths: DDB status "running" AND a
    # gateway-liveness row — and that row is written ONLY by a successful
    # pool connect (connection_pool.connect → mark_gateway_connected). Every
    # pool dial is demand-driven (an RPC arriving), so a scale-from-zero
    # wake deadlocks by construction: the cold-start UI polls this endpoint
    # and sends no RPCs, this endpoint reports "starting" because no
    # connection exists, and no connection will ever exist because nothing
    # dials. The durable DDB liveness row fixed the READ half of the old
    # per-process-pool blind spot (another instance's connection is now
    # visible); this dial is the WRITE half — when no instance holds a
    # connection at all, the poll itself must create the demand. Dial in
    # the background so the NEXT poll reports the truth. wake=0 (read-only
    # poll) never dials; failures are swallowed — the next poll simply
    # reports whatever is true.
    if (
        wake != "0"
        and container_ip
        and container is not None
        and container.get("gateway_token")
        and contract["runtime_phase"] == "starting"
        and _self_heal_dial_allowed(owner_id)
    ):
        _self_heal_inflight.add(owner_id)
        _self_heal_last_dial[owner_id] = time.monotonic()
        asyncio.create_task(_pool_self_heal_connect(owner_id, container_ip, container["gateway_token"]))

    return _status_response(contract)


async def _pool_self_heal_connect(owner_id: str, ip: str, token: str) -> None:
    """Background dial for the status poll's pool self-heal.

    Same (owner, ip, token) triple and ensure_connection primitive the chat
    send path uses — idempotent, fast-paths when a racing dial already won.
    The dial races real container state (mid-boot gateway, task just died),
    so failures are logged and swallowed: the poll it came from has already
    answered, and the next poll simply reports whatever is true. Always
    clears the per-owner in-flight marker the dispatch site set — the
    cooldown (stamped at dispatch) paces the next attempt.
    """
    started = time.monotonic()
    try:
        pool = get_gateway_pool()
        if pool.is_user_connected(owner_id):
            return
        await pool.ensure_connection(owner_id, ip, token)
        logger.info("status-poll pool self-heal dial connected owner=%s", owner_id)
        # This dial may have created a connection while NO frontend WS is
        # registered on this task (status-only client, or the browser's WS
        # landed on a different backend task). remove_frontend_connection —
        # the only other armer of the idle grace-close — will never fire for
        # it, so without this arm the connection would refresh gateway
        # liveness forever and keep a trial container running with nobody
        # attached. Over-preserve-safe: the grace path re-checks frontends
        # at fire time and the trial stop re-checks cross-task DDB truth.
        pool.arm_idle_teardown_if_unattended(owner_id)
    except Exception as exc:
        logger.info("status-poll pool self-heal dial failed owner=%s: %s", owner_id, exc)
        # The cooldown exists to pace EXPENSIVE failures — a blackholed IP
        # holding the pool's global connect lock through a full handshake
        # timeout. A dial that failed in milliseconds (connection refused
        # while the gateway is still booting — the normal race this helper
        # documents above) held the lock for effectively nothing, and
        # leaving the 60s stamp would pin the user on "starting" for a
        # minute when the next ~5s poll's dial would succeed. Clear the
        # stamp so that poll retries; the global single-flight still bounds
        # concurrency, and a slow failure keeps the full cooldown.
        if time.monotonic() - started < _SELF_HEAL_FAST_FAIL_S:
            _self_heal_last_dial.pop(owner_id, None)
    finally:
        _self_heal_inflight.discard(owner_id)


def _sync_contract_with_container(contract: dict, container: dict) -> dict:
    """Refresh the container-derived fields on an already-computed
    ``contract`` after a LOCAL (pre-commit) mutation to ``container`` --
    the auto-retry branch above optimistically flips
    ``container["status"]``/``["substatus"]`` before the row itself
    catches up, so THIS response should reflect that mutation without a
    second ``readiness_state.compute_status`` call (which would re-read
    billing/gateway-liveness/paperclip-companies for no reason -- none of
    those changed).

    Both mutation sites this is called from only ever set
    ``status`` to ``"provisioning"`` or leave it ``"error"`` — neither is
    ``"running"``, so ``runtime_phase`` is unconditionally
    ``"provisioning"`` here; this intentionally does NOT re-derive that
    from ``readiness_state.resolve_runtime_phase`` to avoid pretending a
    gateway-liveness read happened when it didn't.
    """
    status = container.get("status")
    contract["container_status"] = status
    contract["container_substatus"] = container.get("substatus")
    contract["lifecycle"] = "error" if status == "error" else "provisioning"
    contract["runtime_phase"] = "provisioning"
    contract["chat_available"] = False
    return contract


def _status_response(contract: dict, *, parked: bool = False) -> dict:
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

    ``parked=True`` represents the deliberately-asleep, wake-gated trial box
    via ``lifecycle="parked"`` + ``parked=True`` + ``wake_retry_after_s`` --
    NOT the old ``phase="parked"`` string (``phase`` no longer exists on the
    wire; ``runtime_phase`` stays "provisioning", per INV-6, since a parked
    box has no live runtime). Explicit caller-supplied flag (not read off
    ``contract["parked"]``) because the wake-gated-over-budget caller
    (``routers/container.py``'s legacy-wake branch) wants the box reported
    as parked without the ``wake_retry_after_s`` hint (no explicit-wake
    client is polling); the composed contract's own ``parked``/``lifecycle``
    already agree in every real case (both derive from the same
    ``wake_policy.is_parked_trial`` check) -- this override exists for the
    one call site that wants ``wake_retry_after_s`` added on top.
    """
    body = {
        "service_name": contract.get("service_name"),
        "created_at": contract.get("created_at"),
        "updated_at": contract.get("updated_at"),
        "region": settings.AWS_REGION,
        "last_error": contract.get("last_error"),
        "last_error_at": contract.get("last_error_at"),
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
        "parked": bool(contract.get("parked", False)),
        "generations": contract.get("generations"),
        "funnel": contract.get("funnel"),
        "bootstrap": contract.get("bootstrap"),
        "provider": contract.get("provider"),
        "teams_state": contract.get("teams_state"),
    }
    if parked:
        body["lifecycle"] = "parked"
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
    )
    if result.outcome == "blocked":
        raise HTTPException(status_code=402, detail=result.gate_payload)
    return {"outcome": result.outcome}


def _safe_phase(container: dict, owner_id: str) -> str:
    """LEGACY — retained ONLY as a patch target for existing test doubles
    (``patch("routers.container._safe_phase", ...)`` / ``monkeypatch.
    setattr(container, "_safe_phase", ...)`` across
    tests/unit/routers/test_container_status*.py) that stub this out to
    isolate themselves from gateway-pool internals. Nothing in production
    calls this anymore — the real phase now comes from
    ``readiness_state.compute_status``'s ``runtime_phase`` (gateway
    liveness sourced from the connection-service DDB table, not the
    in-process pool; see ``_pool_self_heal_connect``'s deletion note).
    Safe to delete alongside those patches once nothing references it.

    On pool init failure (ManagementApiClientError when
    WS_MANAGEMENT_API_URL is unset — typically test envs that skip the
    lifespan handler) falls back to a container-only phase mapping instead
    of forcing "starting".
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

    # Provision new container via the PROVISION Step Functions executor (spec
    # §7 / PR-10) — the SOLE provisioning executor, no flag. dispatch_provision
    # is a fast StartExecution call (the actual provider_choice read + ECS
    # work happens inside scripts.provision_runner, a separate one-shot ECS
    # task the state machine supervises) and never raises: a StartExecution
    # failure releases the row to status="error" instead, picked up by this
    # owner's next GET /container/status poll. Two provisioning systems must
    # never be live at once, so this door no longer calls
    # ecs_manager.provision_user_container directly.
    await dispatch_provision(owner_id)
    row = await container_repo.get_by_owner_id(owner_id)
    logger.info("Dispatched provision for owner %s", owner_id)
    return {
        "status": (row or {}).get("status", "provisioning"),
        "service_name": (row or {}).get("service_name"),
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

    # Route through the PROVISION executor (spec §7) — see /provision above
    # for why this no longer calls ecs_manager.provision_user_container
    # directly. dispatch_provision never raises.
    await dispatch_provision(owner_id)
    row = await container_repo.get_by_owner_id(owner_id)
    return {"ok": True, "service_name": (row or {}).get("service_name")}
