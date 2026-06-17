"""Container recovery + gateway-control endpoints.

`/recover` inspects current container + gateway state and dispatches the
appropriate recovery action. `/gateway/restart` forces a config reload on a
running gateway. Both reach the gateway through the shared signed one-shot
helper.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_ecs_manager
from core.gateway.operator_rpc import gateway_rpc_oneshot
from core.repositories import container_repo
from core.services.container_provision import (
    resolve_provider_choice as _resolve_provider_choice,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# In-memory per-owner lock to prevent concurrent recovery.
_recovery_locks: dict[str, asyncio.Lock] = {}


def _get_lock(owner_id: str) -> asyncio.Lock:
    if owner_id not in _recovery_locks:
        _recovery_locks[owner_id] = asyncio.Lock()
    return _recovery_locks[owner_id]


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
    lock = _get_lock(owner_id)

    if lock.locked():
        return {
            "action": "already_recovering",
            "state": "RECOVERING",
            "reason": "Recovery already in progress",
        }

    async with lock:
        ecs_manager = get_ecs_manager()

        # 1. Try to resolve a running container
        container, ip = await ecs_manager.resolve_running_container(owner_id)

        if not container:
            container = await ecs_manager.get_service_status(owner_id)

        if not container:
            raise HTTPException(status_code=404, detail="No container found")

        status = container.get("status", "unknown")

        # 2. Container is stopped or error -> full re-provision
        if status in ("stopped", "error"):
            reason = container.get("last_error", f"Container is {status}")
            logger.info("Recovering owner %s: reprovision (status=%s)", owner_id, status)
            try:
                # Read the owner's saved provider_choice so recovery rebuilds
                # with the correct LLM path. Keyed on owner_id via billing_repo.
                provider_choice, byo_provider = await _resolve_provider_choice(owner_id)
                await ecs_manager.provision_user_container(
                    owner_id,
                    provider_choice=provider_choice,
                    byo_provider=byo_provider,
                )
            except Exception as e:
                logger.error("Recovery reprovision failed for %s: %s", owner_id, e)
                raise HTTPException(status_code=502, detail="Re-provisioning failed")
            return {
                "action": "reprovision",
                "state": "CONTAINER_DOWN",
                "reason": reason,
            }

        # 3. Container is running -> check gateway health
        token = container.get("gateway_token", "")
        if not ip:
            logger.warning("Owner %s: running container but no IP, reprovisioning", owner_id)
            try:
                # Read the owner's saved provider_choice so recovery rebuilds
                # with the correct LLM path. Keyed on owner_id via billing_repo.
                provider_choice, byo_provider = await _resolve_provider_choice(owner_id)
                await ecs_manager.provision_user_container(
                    owner_id,
                    provider_choice=provider_choice,
                    byo_provider=byo_provider,
                )
            except Exception:
                raise HTTPException(status_code=502, detail="Re-provisioning failed")
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
            await container_repo.update_error(owner_id, "Gateway restart failed — reprovisioning")
            try:
                # Read the owner's saved provider_choice so recovery rebuilds
                # with the correct LLM path. Keyed on owner_id via billing_repo.
                provider_choice, byo_provider = await _resolve_provider_choice(owner_id)
                await ecs_manager.provision_user_container(
                    owner_id,
                    provider_choice=provider_choice,
                    byo_provider=byo_provider,
                )
            except Exception:
                raise HTTPException(status_code=502, detail="Re-provisioning failed")
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
