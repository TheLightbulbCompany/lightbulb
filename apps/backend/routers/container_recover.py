"""Container recovery + gateway-control endpoints.

`/recover` inspects current container + gateway state and dispatches the
appropriate recovery action. `/gateway/restart` forces a config reload on a
running gateway. Both reach the gateway through the shared signed one-shot
helper.

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
from core.containers import get_ecs_manager
from core.gateway.operator_rpc import gateway_rpc_oneshot
from core.repositories import container_repo
from core.services import provision_gate, trial_cap
from core.services.provision_dispatch import dispatch_provision
from core.services.stripe_webhook_handlers import conversion_guard_active
from routers.container import _owner_role

logger = logging.getLogger(__name__)

router = APIRouter()


async def _call_gateway_rpc(
    ip: str,
    owner_id: str,
    token: str,
    method: str,
    params: dict | None = None,
) -> dict:
    """Short-lived signed RPC to the owner's gateway container.

    Thin wrapper over the shared `gateway_rpc_oneshot` helper, keeping
    recovery's tight timeouts: a wedged gateway should fail fast so the
    recovery flow escalates to restart / reprovision rather than hanging the
    request.
    """
    return await gateway_rpc_oneshot(
        ip,
        owner_id=owner_id,
        token=token,
        method=method,
        params=params,
        open_timeout=5,
        recv_timeout=5,
        rpc_timeout=10,
    )


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
    ecs_manager = get_ecs_manager()

    # 1. Try to resolve a running container
    container, ip = await ecs_manager.resolve_running_container(owner_id)

    if not container:
        container = await ecs_manager.get_service_status(owner_id)

    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    status = container.get("status", "unknown")

    # A trial->paid conversion owns this row and is mid-drain. Its reprovision
    # is already in flight; a second one from here would put a task on the
    # EFS-WAL SQLite next to the draining one — the documented corruption mode.
    #
    # Checked BEFORE the status split, not inside the stopped/error branch,
    # because the drain is not instantaneous: ``reprovision_for_user`` scales
    # the service to zero while the row still reads "running", so a poll
    # landing in that window fell through to the gateway branches below —
    # which probe a gateway that is *supposed* to be gone, fail, and write
    # ``status="error"`` + "Gateway restart failed — reprovisioning" over a row
    # the conversion owns. That write is not just noise: it renders the
    # terminal "Setup failed" card to a customer who just paid, and it
    # dispatches a provision competing with the conversion's own. Observed on
    # prod 2026-08-06 (conversion 16:53:46 -> recover-written error 17:09:03 ->
    # self-healed 17:32). The guard is status-agnostic (a timestamp window), so
    # hoisting it costs nothing and closes all three branches at once.
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

    # 3. Container is running -> check gateway health
    token = container.get("gateway_token", "")
    if not ip:
        # Same trial predicate the stopped/error wake enforces above: this
        # branch reprovisions too (a force=True start), and the meter
        # Lambda's cap kill transiently leaves exactly this shape — a
        # "running" row whose task is gone — so without the gate this door
        # is the one way a capped/expired trial owner can wake the box.
        # (Bedrock stays denied for a revoked owner, so no spend — but the
        # container-stays-down invariant must hold at every door.)
        trial_gate = provision_gate.gate_for_trial_reason(
            await trial_cap.trial_block_reason(owner_id), _owner_role(auth)
        )
        if trial_gate is not None:
            raise HTTPException(status_code=402, detail=trial_gate.to_payload())
        logger.warning("Owner %s: running container but no IP, reprovisioning", owner_id)
        # Make the row claimable BEFORE dispatching: the dispatch's claim
        # (try_claim_provision) admits only missing/error/stopped/stale
        # rows — a "running" row would lose the claim and no execution
        # would start, while this endpoint still answered "reprovision".
        # The row IS in an error state (task gone / unreachable), so the
        # honest write is also the enabling one.
        await container_repo.update_status(owner_id, "error")
        await container_repo.update_error(owner_id, "Container running but unreachable")
        await dispatch_provision(owner_id)
        return {
            "action": "reprovision",
            "state": "CONTAINER_DOWN",
            "reason": "Container running but unreachable",
        }

    # Try gateway health check
    try:
        health = await _call_gateway_rpc(ip, owner_id, token, "health")
        if health.get("ok"):
            return {
                "action": "none",
                "state": "HEALTHY",
                "reason": "System is healthy",
            }
    except Exception:
        pass  # Gateway is down, proceed to restart

    # 4. Gateway is down -> try restart via update.run RPC
    logger.info("Recovering owner %s: gateway restart", owner_id)
    try:
        await _call_gateway_rpc(ip, owner_id, token, "update.run")
        return {
            "action": "gateway_restart",
            "state": "GATEWAY_DOWN_DEAD",
            "reason": "Gateway not responding — restarting",
        }
    except Exception:
        logger.warning("Owner %s: gateway restart failed, escalating to reprovision", owner_id)
        # Status flip first — same claimability requirement as the no-IP
        # branch above (update_error writes only last_error fields; a
        # "running" row would make the dispatch's claim stand down).
        await container_repo.update_status(owner_id, "error")
        await container_repo.update_error(owner_id, "Gateway restart failed — reprovisioning")
        await dispatch_provision(owner_id)
        return {
            "action": "reprovision",
            "state": "GATEWAY_DOWN_DEAD",
            "reason": "Gateway restart failed — reprovisioning",
        }


@router.post(
    "/gateway/restart",
    summary="Restart the OpenClaw gateway on the user's container",
    description=(
        "Sends a config.apply RPC to the user's running gateway container, "
        "which triggers a gateway process restart. "
        "Use this when the WebSocket connection is down but the container is running."
    ),
    operation_id="gateway_restart",
    responses={
        404: {"description": "No running container for this user"},
        502: {"description": "Gateway is not responding"},
    },
)
async def gateway_restart(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No running container")
    if not ip:
        raise HTTPException(status_code=502, detail="Container gateway is starting up")

    try:
        # config.apply is an operator.admin-reserved method, so this must use
        # the signed one-shot (a token-only connect would have its scope
        # stripped and the apply rejected). Keep the original generous budget —
        # config.apply re-reads config and restarts the gateway.
        await gateway_rpc_oneshot(
            ip,
            owner_id=owner_id,
            token=container["gateway_token"],
            method="config.apply",
            params={},
            open_timeout=30,
            recv_timeout=10,
            rpc_timeout=30,
        )
    except ConnectionRefusedError:
        raise HTTPException(status_code=502, detail="Gateway is not responding")
    except TimeoutError:
        raise HTTPException(status_code=502, detail="Gateway restart timed out")
    except Exception as e:
        logger.error("Gateway restart failed for user %s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Gateway restart failed")

    return {"ok": True}
