"""Container recovery.

`/recover` inspects current container + gateway state and dispatches the
appropriate recovery action, reaching the gateway through ``container_rpc``.

Every reprovision sub-branch below routes through
``core.services.provision_dispatch.dispatch_provision``, which no-ops while a
provision execution is RUNNING for the owner, and every provision step is
idempotent — so concurrent /recover calls need no lock.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import container_repo
from core.services import container_rpc, provision_gate
from core.services.provision_dispatch import dispatch_provision
from core.services.user_provision import ensure_workspace_trial_default

logger = logging.getLogger(__name__)

router = APIRouter()


async def _call_gateway_rpc(owner_id: str, method: str, params: dict | None = None) -> dict:
    """Recovery keeps a tight budget: a wedged gateway must fail fast so the flow escalates."""
    return await container_rpc.call(owner_id, method, params, timeout_s=10.0)


async def _assert_gate_clear(auth: AuthContext, owner_id: str) -> None:
    """402 with the structured card when the provision gate is up (the one /provision uses)."""
    gate = await provision_gate.evaluate_provision_gate(
        owner_id=owner_id, is_admin=auth.is_org_admin, actor_user_id=auth.user_id
    )
    if gate is not None:
        raise HTTPException(status_code=402, detail=gate.to_payload())


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
        404: {"description": "No container for this user (org member)"},
        402: {"description": "Provision gate is up (structured blocked payload)"},
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
        # No row at all (its organization.created provision never started). Nothing re-drives
        # that any more, so an admin's Retry starts it -- behind the same trial row + gate
        # POST /container/provision applies. Members ride the admin's workspace.
        if not auth.is_org_admin:
            raise HTTPException(status_code=404, detail="No container found")
        await ensure_workspace_trial_default(auth, owner_id)
        await _assert_gate_clear(auth, owner_id)
        await dispatch_provision(owner_id)
        return {"action": "reprovision", "state": "CONTAINER_DOWN", "reason": "Workspace not provisioned yet"}

    status = container.get("status", "unknown")

    # A provisioning row must never be probed: /recover used to fire during a
    # conversion's provision, probe the mid-boot gateway, write status="error"
    # over the in-flight run (prod 2026-08-10). Stand down: the execution ends in
    # MarkRunning or a MarkFailed* error row, and a dispatch that never started
    # releases the row to error itself (provision_dispatch).
    if status == "provisioning":
        return {
            "action": "already_recovering",
            "state": "RECOVERING",
            "reason": "A provision is already in flight for this container",
        }

    # 2. Container is stopped or error -> full re-provision
    if status in ("stopped", "error"):
        # This is a WAKE, and it is customer-reachable, so it passes the same two guards every
        # other start path applies. It cannot inherit them: it wakes the workspace through the
        # PROVISION executor (dispatch_provision), whose runner starts with
        # force=True (that call IS a provision — the trial->paid conversion's
        # reprovision runs through it and would otherwise deadlock against its
        # own marker). So the third door checks the same predicates here.
        #
        # The FULL provision gate /provision and /retry apply, not just the trial
        # predicate: a canceled or past-due workspace, or one with no provider or
        # credits, must not reprovision its way back through this door either.
        await _assert_gate_clear(auth, owner_id)

        reason = container.get("last_error", f"Container is {status}")
        logger.info("Recovering owner %s: reprovision (status=%s)", owner_id, status)
        # Route through the PROVISION executor (spec §7) — dispatch_provision
        # never raises; a StartExecution failure releases the row to a
        # retryable status="error" instead.
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
    # The SAME provision gate the stopped/error branch applies. A reprovision re-mints the
    # owner's gate bearer, which is exactly what the meter's backstop kill DENIED to stop a
    # capped trial -- so without this gate an unhealthy running row is the one door a capped owner
    # could reprovision their way back through. (It used to be covered by the running/no-IP branch
    # that resolved a live task IP; there is no task IP to miss now, so the check moves here.)
    await _assert_gate_clear(auth, owner_id)

    logger.warning("Owner %s: gateway unhealthy, reprovisioning", owner_id)
    # Status flip first: provision_hermes_runtime returns early for a "running" row, so without
    # it the dispatched run would do nothing while this endpoint answered "reprovision".
    await container_repo.update_status(owner_id, "error")
    await container_repo.update_error(owner_id, "Gateway unhealthy — reprovisioning")
    await dispatch_provision(owner_id)
    return {
        "action": "reprovision",
        "state": "GATEWAY_DOWN_DEAD",
        "reason": "Gateway unhealthy — reprovisioning",
    }
