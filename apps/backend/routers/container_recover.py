"""Container recovery.

`/recover` inspects current container + gateway state and dispatches the
appropriate recovery action, reaching the gateway through ``container_rpc``.

The in-process per-owner recovery lock this router used to hold is REMOVED
(spec §7, PR-10): every reprovision sub-branch below now routes through
``core.services.provision_dispatch.dispatch_provision``, whose crash-safety
IS its idempotency — a StartExecution with the SAME (deterministic) name +
input either starts the execution or is a no-op success against the already-
running one (core.services.provision_dispatch module docstring). Two
concurrent /recover calls now converge on the SAME dispatch outcome instead
of one blocking behind an advisory lock; the only remaining cost of
concurrency is a redundant (harmless, read-only) gateway health probe.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import container_repo
from core.services import container_rpc, provision_gate, trial_cap
from core.services.provision_dispatch import dispatch_provision
from core.services.stripe_webhook_handlers import conversion_guard_active
from routers.container import _owner_role

logger = logging.getLogger(__name__)

router = APIRouter()


async def _call_gateway_rpc(owner_id: str, method: str, params: dict | None = None) -> dict:
    """Recovery keeps a tight budget: a wedged gateway must fail fast so the flow escalates."""
    return await container_rpc.call(owner_id, method, params, timeout_s=10.0)


def _health_verdict(health) -> bool | None:
    """True healthy / False unhealthy / **None: a payload shape this backend does not know**.

    The OpenClaw gateway answers ``{"ok": ...}``; the Hermes bridge answers
    ``{"ready": true, "runtime": …, "uptimeS": …, "busy": …, "agents": …}`` and carries no ``ok``
    at all (``apps/infra/hermes/bridge/server.py``). Reading only ``ok`` therefore made every
    HEALTHY AgentCore workspace look dead: this door fell through to ``update.run`` (also absent
    from the bridge), wrote ``status="error"`` on a perfectly good row and dispatched a full
    reprovision — while answering ``200 {"action":"reprovision"}`` (task-14 row 6).

    An unrecognised payload is NOT "unhealthy": assuming the worst is what made that a
    destructive read. The caller turns ``None`` into a 502 instead of a reprovision.
    """
    if not isinstance(health, dict):
        return None
    for key in ("ok", "ready"):
        if key in health:
            return bool(health[key])
    return None


@router.post(
    "/recover",
    summary="Recover container or gateway",
    description=(
        "Inspects current container and gateway state, then takes the "
        "appropriate recovery action. Idempotent and safe to call repeatedly."
    ),
    operation_id="container_recover",
    responses={
        404: {"description": "No container for this user"},
    },
)
async def container_recover(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)

    # 1. The row is the only state there is: an invoke wakes a parked microVM, so there is no
    #    task IP to discover and no "running but unreachable" shape to detect up front.
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    status = container.get("status", "unknown")

    # A trial->paid conversion owns this row and its reprovision is already in flight; a
    # second one from here would compete with it and can render the terminal "Setup failed"
    # card to a customer who just paid. Observed on prod 2026-08-06. The guard is
    # status-agnostic (a timestamp window), so it sits before the status split and covers
    # every branch below at once.
    if conversion_guard_active(container):
        logger.info("Recover for owner %s stood down: conversion reprovision in flight", owner_id)
        return {
            "action": "already_recovering",
            "state": "RECOVERING",
            "reason": "A subscription upgrade is reprovisioning this container",
        }

    # An ACTIVE provision lease owns this row — stand down on ROW STATE, not
    # a clock. The conversion guard above cannot cover this window: its stamp
    # is cleared the moment the conversion's dispatch is created (so a stale
    # stamp can't block the manual Restart door), which disarms it precisely
    # when the SFN runner's minutes-long provision begins. Observed on prod
    # 2026-08-10: /recover fired four times during a conversion's provision,
    # each probing the mid-boot gateway, failing, writing status="error" over
    # the in-flight run and dispatching a competitor. The lease heartbeat
    # (refreshed by every leased checkpoint write) is the authoritative
    # in-flight signal; a lease gone stale — the runner died — falls through
    # to the ordinary recovery branches below, so a wedged provision is still
    # recoverable from this endpoint. The staleness fence is the lease's own
    # pre-existing crash-recovery TTL, not a guard window of this endpoint's.
    if status == "provisioning" and not container_repo.is_provision_claim_stale(container):
        logger.info("Recover for owner %s stood down: provision lease active", owner_id)
        return {
            "action": "already_recovering",
            "state": "RECOVERING",
            "reason": "A provision is already in flight for this container",
        }

    # 2. Container is stopped or error -> full re-provision
    if status in ("stopped", "error"):
        # This is a WAKE, and it is customer-reachable, so it passes the same
        # two guards ``ecs_manager.start_user_service`` applies to every other
        # start. It cannot inherit them: it wakes the container through the
        # PROVISION executor (dispatch_provision), whose runner starts with
        # force=True (that call IS a provision — the trial->paid conversion's
        # reprovision runs through it and would otherwise deadlock against its
        # own marker). So the third door checks the same predicates here.
        #
        # The FULL trial predicate, not just the cap. An owner whose trial
        # window has expired but who is under today's $3 would otherwise
        # fall through to the executor -- whose runner starts with
        # force=True and therefore skips start_user_service's own check --
        # and wake a container that is out of runway. Same 402 limit card
        # the other doors return, for whichever reason applies.
        trial_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if trial_gate is not None:
            raise HTTPException(status_code=402, detail=trial_gate.to_payload())

        reason = container.get("last_error", f"Container is {status}")
        logger.info("Recovering owner %s: reprovision (status=%s)", owner_id, status)
        # Route through the PROVISION executor (spec §7) — dispatch_provision
        # never raises; a StartExecution failure releases the row to
        # status="error" instead, picked up by the caller's next
        # GET /container/status poll.
        await dispatch_provision(owner_id)
        return {
            "action": "reprovision",
            "state": "CONTAINER_DOWN",
            "reason": reason,
        }

    # 3. Running -> check gateway health.
    # Try gateway health check
    try:
        health = await _call_gateway_rpc(owner_id, "health")
    except Exception:
        health = None  # Gateway is down, proceed to restart
    if health is not None:
        verdict = _health_verdict(health)
        if verdict is None:
            # Fail loudly rather than escalate: the runtime ANSWERED, in a shape we do not
            # recognise, so "restart then reprovision" would be destroying a workspace on a
            # guess. Nothing is written and nothing is dispatched.
            logger.error("Owner %s: unrecognised gateway health payload; refusing to recover", owner_id)
            raise HTTPException(status_code=502, detail="Unrecognised gateway health response")
        if verdict:
            return {
                "action": "none",
                "state": "HEALTHY",
                "reason": "System is healthy",
            }

    # 4. Gateway is down -> reprovision.
    #
    # There is no in-place restart step any more. It was an ``update.run`` RPC into the
    # OpenClaw gateway, and the Hermes bridge serves no such method -- so on every migrated
    # workspace it raised and fell straight through to this same escalation. Calling it was
    # one guaranteed-failing invoke (and, against a parked microVM, one cold start) buying a
    # log line.
    logger.warning("Owner %s: gateway unhealthy, reprovisioning", owner_id)
    # Status flip first: `update_error` writes only the last_error fields, and the dispatch's
    # claim (try_claim_provision) admits only missing/error/stopped/stale rows -- a "running"
    # row would make it stand down while this endpoint answered "reprovision".
    await container_repo.update_status(owner_id, "error")
    await container_repo.update_error(owner_id, "Gateway unhealthy — reprovisioning")
    await dispatch_provision(owner_id)
    return {
        "action": "reprovision",
        "state": "GATEWAY_DOWN_DEAD",
        "reason": "Gateway unhealthy — reprovisioning",
    }
