"""Container lifecycle management endpoints.

Provides container status (with auto-retry for failed containers)
and a manual retry-provision endpoint.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.services import browser_sessions, readiness_state
from core.services.onboarding_brief import reconcile_pending_brief
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.provision_gate import (
    evaluate_provision_gate,
    maybe_restore_trial_bedrock,
)
from core.services.stripe_webhook_handlers import maybe_resume_stuck_conversion
from core.services.user_provision import arm_workspace_trial_if_unset, ensure_workspace_trial_default, ensure_user_ready

logger = logging.getLogger(__name__)

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


def _maybe_drain_pending_brief(owner_id: str, onboarding: dict) -> None:
    """Fire the onboarding-brief drain if this owner has an answer waiting.

    The first canonical-room message is persisted before its workspace may be
    ready to fulfil it. This poll retries the durable intake through
    ``core.services.onboarding_brief.reconcile_pending_brief``.

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
    flow: str | None = Query(
        default=None,
        description=(
            "Client-minted signup_flow_id, joining this owner's server-side "
            "trial events back to the anonymous half of their signup journey "
            "(apps/frontend/src/lib/signupFlow.ts). Opaque and untrusted — it "
            "only ever lands in a PostHog property. Deliberately NOT validated "
            "here: a bad value is dropped inside ensure_workspace_trial_default, "
            "never 422'd, because this is the readiness poll and a polluted "
            "localStorage entry must not be able to strand a user on a spinner."
        ),
    ),
):
    owner_id = resolve_owner_id(auth)
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

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        # No container row at all. Evaluate the same provision gate
        # POST /provision uses — if a gate is up, the frontend needs the
        # structured payload (402) so it can render the picker / "top up"
        # CTA instead of trapping the user in a generic "no container"
        # state.
        # Create the workspace's unarmed trial before the gate reads billing.
        await ensure_workspace_trial_default(auth, owner_id, signup_flow_id=flow)
        gate = await evaluate_provision_gate(owner_id=owner_id, is_admin=auth.is_org_admin)
        if gate is not None:
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
    # below falls through, and this endpoint reports a warm workspace — hiding
    # the limit card from exactly the owner most likely to convert. Paid owners
    # also carry bedrock_revoked (Metronome low-balance enforcement) — their trial
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
    # composer of the containers/billing/paperclip-companies inputs — see core/services/readiness_state.py. Passing the
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
    # `onboarding` was fetched once at the top of this handler for the pending
    # brief drain and is threaded into compute_status. It is the same
    # paperclip-companies read compute_status would otherwise do internally (a
    # by-org-id GSI query for an org owner), so without threading it every ~3s
    # ready poll fleet-wide would pay it twice.
    contract = await readiness_state.compute_status(owner_id, container=container, onboarding=onboarding)
    if contract["runtime_phase"] == "ready" and contract["provider"]["subscription_status"] == "trial_local":
        await arm_workspace_trial_if_unset(owner_id)

    # Auto-retry: if the workspace is in a failed/stuck state AND the owner can
    # currently afford to provision, trigger re-provisioning in the
    # background. The same gate as /provision (audit C1) — bare
    # subscription-id existence wasn't enough; we need active/trialing
    # status, and bedrock_claude users need a positive credit balance.
    #
    # Stale-lease recovery: a "provisioning" row whose lease has had no
    # checkpoint refresh for PROVISION_CLAIM_STALE_S means the process running
    # the provision died mid-flight (a raise releases the lease to "error"
    # immediately — only hard process death leaves it to go stale). Fold it
    # into the retryable branch so it inherits every gate below, then let
    # try_claim_provision's stale clause decide ATOMICALLY who re-provisions —
    # two polls can both enter here, exactly one wins the re-claim.
    retryable_states = ("error", "stopped")
    stale_provision = container_repo.is_provision_claim_stale(container)
    provider_update_pending = (
        container.get("status") == "running"
        and bool(container.get("needs_provision"))
        and not container.get("provision_claim_id")
    )
    if (
        container.get("status") in retryable_states or stale_provision or provider_update_pending
    ) and not conversion_active:
        # The FULL trial predicate, not just the cap. An over-cap or expired
        # trial owner gets the limit card rather than a silent re-provision.
        from core.services import provision_gate, trial_cap

        trial_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if trial_gate is not None:
            raise HTTPException(status_code=402, detail=trial_gate.to_payload())
        await ensure_workspace_trial_default(auth, owner_id)
        try:
            await _assert_provision_allowed(
                owner_id,
                is_admin=auth.is_org_admin,
            )
        except HTTPException:
            pass
        else:
            if container.get("status") == "error":
                # Honest error reporting — never mask a failed provision as
                # "provisioning". T3 records the cause on the row:
                #   retryable=True  (transient infra: throttle / 5xx) → still
                #     auto-dispatch the re-provision, but SAY so: status stays
                #     "error", substatus="error_retrying", and last_error is
                #     carried through by _status_response. The client shows
                #     "hit a problem, retrying" — no timer needed.
                #   retryable=False (deterministic: 4xx / config / validation)
                #     → do NOT re-dispatch; the recorded status + last_error
                #     render the error card immediately.
                # Default True keeps a cause-less legacy error row self-healing.
                if container.get("retryable", True):
                    asyncio.create_task(dispatch_provision(owner_id))
                    container["substatus"] = "error_retrying"
                    _sync_contract_with_container(contract, container)
            else:
                # stopped / stale-provisioning lease: the poll-driven self-heal.
                # An idle AgentCore workspace is NOT this case — AWS parks its
                # microVM invisibly and the row stays "running", which is why
                # there is no wake to suppress here any more.
                #
                # No status pre-write: dispatch_provision's atomic lease claim IS
                # the transition to "provisioning". A pre-write would (a) race
                # concurrent polls into double-dispatch and (b) flip the row to
                # "provisioning" with a stale lease, which the claim's stale
                # clause could then mis-time. The local dict mutation below only
                # shapes THIS response.
                asyncio.create_task(dispatch_provision(owner_id))
                container["status"] = "provisioning"
                container["substatus"] = "auto_retry"
                _sync_contract_with_container(contract, container)

    # Re-dispatch ensure_user_ready on EVERY running-container poll. Every caller
    # is an org user (resolve_owner_id 409s anyone else), and ensure_user_ready is
    # the idempotent self-heal for a running box: the paperclip membership-add for
    # a co-admin/member who joins an already-provisioned org, a failed Teams seed
    # (non-fatal to the container, so nothing else retries it), and the Metronome
    # customer for a ledger-eligible owner whose creation blipped. It reads the
    # container row itself and reconciles in-thread instead of dispatching a
    # provision (see _provision_or_reconcile).
    _dispatch_watched(ensure_user_ready(auth))

    return _status_response(contract)


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
        # Always "agentcore". Kept for one release so a client released against the
        # migrated contract still finds the field; it goes with the column.
        "runtime_host": contract.get("runtime_host"),
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
        "Watch-only, and it never STARTS a session."
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
    return await browser_sessions.live_view(resolve_owner_id(auth))


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
    # scripts.provision_runner, a one-shot task the state machine supervises)
    # and never raises: a StartExecution failure releases the row to
    # status="error" instead, picked up by this owner's next status poll.
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
