"""Container recovery endpoint.

Single endpoint that inspects current container + gateway state
and dispatches the appropriate recovery action.
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

import websockets

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT
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
    """Short-lived WebSocket RPC call to a gateway container.

    Uses the same OpenClaw 4.5 signed-device handshake as the long-lived
    pool in `core.gateway.connection_pool`: loads the per-container operator
    device seed from DynamoDB (KMS-decrypted), signs the v2 payload with
    the nonce from connect.challenge, and sends both the token AND the
    signed device in the connect request.

    Falls back cleanly if the container row doesn't have an
    `operator_priv_key_enc` field yet (pre-4.5 row) — propagates a
    RuntimeError the caller already catches and turns into a reprovision.
    """
    from core.crypto import kms_secrets
    from core.crypto.operator_device import (
        BACKEND_CLIENT_ID,
        BACKEND_CLIENT_MODE,
        BACKEND_OPERATOR_SCOPES,
        BACKEND_ROLE,
        load_operator_device_from_seed,
        sign_connect_request,
    )

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise RuntimeError(f"No container row for owner {owner_id}")
    enc_seed = container.get("operator_priv_key_enc")
    if not enc_seed:
        raise RuntimeError(
            f"Container row for owner {owner_id} missing operator_priv_key_enc "
            "(pre-4.5 provision — reprovision required)"
        )
    seed_bytes = kms_secrets.decrypt_bytes(
        enc_seed,
        encryption_context={"owner_id": owner_id, "purpose": "operator-device-seed"},
    )
    identity = load_operator_device_from_seed(seed_bytes)

    uri = f"ws://{ip}:{GATEWAY_PORT}"
    async with websockets.connect(uri, open_timeout=5, close_timeout=2) as ws:
        # Receive challenge + extract nonce
        challenge = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if challenge.get("event") != "connect.challenge":
            raise RuntimeError("Unexpected handshake message")
        nonce = challenge.get("payload", {}).get("nonce") or challenge.get("nonce")
        if not nonce:
            raise RuntimeError("connect.challenge missing nonce")

        # Sign the v2 payload with our operator identity
        device = sign_connect_request(
            identity=identity,
            token=token,
            nonce=nonce,
            scopes=BACKEND_OPERATOR_SCOPES,
        )

        req_id = str(uuid.uuid4())
        await ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": req_id,
                    "method": "connect",
                    "params": {
                        "minProtocol": 4,
                        "maxProtocol": 4,
                        "client": {
                            "id": BACKEND_CLIENT_ID,
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": BACKEND_CLIENT_MODE,
                        },
                        "role": BACKEND_ROLE,
                        "scopes": list(BACKEND_OPERATOR_SCOPES),
                        "auth": {"token": token},
                        "device": device,
                    },
                }
            )
        )
        connect_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if not connect_resp.get("ok"):
            err = connect_resp.get("error", {}).get("message", "unknown")
            raise RuntimeError(f"Handshake rejected: {err}")

        rpc_id = str(uuid.uuid4())
        await ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": rpc_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") == "res" and msg.get("id") == rpc_id:
                return msg.get("payload", {})


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
