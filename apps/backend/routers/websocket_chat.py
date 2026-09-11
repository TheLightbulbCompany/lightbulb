"""API Gateway WebSocket HTTP integration for node/operator machine relays only."""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response

from core.config import settings
from core.containers import get_gateway_pool
from core.observability.metrics import put_metric
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.node_proxy import (
    handle_node_connect,
    handle_node_disconnect,
    handle_node_message,
    is_node_connection,
    submit_signed_connect as submit_node_signed_connect,
)
from core.services.operator_proxy import (
    handle_operator_connect,
    handle_operator_disconnect,
    handle_operator_message,
    is_operator_connection,
    submit_operator_signed_connect,
)
from core.services.paperclip_adapter_config import PAPERCLIP_OPERATOR_CLIENT_ID

logger = logging.getLogger(__name__)

NODE_HOST_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})
_WAKE_GATED_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})

_connection_service: ConnectionService | None = None
_management_api_client: ManagementApiClient | None = None
_singleton_lock = asyncio.Lock()


async def get_connection_service() -> ConnectionService:
    global _connection_service
    if _connection_service is None:
        async with _singleton_lock:
            if _connection_service is None:
                _connection_service = ConnectionService()
    return _connection_service


async def get_management_api_client() -> ManagementApiClient:
    global _management_api_client
    if _management_api_client is None:
        async with _singleton_lock:
            if _management_api_client is None:
                _management_api_client = ManagementApiClient()
    return _management_api_client


def require_ws_internal_caller(
    x_lightbulb_ws_internal: str | None = Header(None, alias="x-lightbulb-ws-internal"),
    x_isol8_ws_internal: str | None = Header(None, alias="x-isol8-ws-internal"),
) -> None:
    """Authenticate the trusted WS Lambdas, preserving the deployed header roll."""
    if not settings.WS_ENFORCE_INTERNAL_SECRET:
        return
    secret = settings.WS_INTERNAL_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="WebSocket gateway not configured")
    presented = x_lightbulb_ws_internal or x_isol8_ws_internal
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Forbidden")


router = APIRouter(tags=["websocket"], dependencies=[Depends(require_ws_internal_caller)])


def _assert_machine_connection(
    row: dict | None,
    *,
    user_id: str,
    org_id: str | None,
    client_id: str | None,
    auth_kind: str | None,
    verified_device_id: str | None,
) -> tuple[str, str]:
    if row is None or row.get("user_id") != user_id or row.get("org_id") != org_id:
        raise HTTPException(status_code=403, detail="Verified machine identity mismatch")
    owner_id = org_id or user_id
    if auth_kind == "device":
        if (
            client_id not in NODE_HOST_CLIENT_IDS
            or row.get("connection_type") != "node"
            or row.get("auth_kind") != "device"
            or not verified_device_id
            or row.get("device_id") != verified_device_id
        ):
            raise HTTPException(status_code=403, detail="Verified device/client mismatch")
        return owner_id, "node"
    if auth_kind == "paperclip_service":
        if (
            client_id != PAPERCLIP_OPERATOR_CLIENT_ID
            or row.get("connection_type") != "operator"
            or row.get("auth_kind") != "paperclip_service"
            or verified_device_id
        ):
            raise HTTPException(status_code=403, detail="Verified operator/client mismatch")
        return owner_id, "operator"
    raise HTTPException(status_code=403, detail="Browser WebSocket admission is disabled")


@router.post("/connect", status_code=200)
async def ws_connect(
    background_tasks: BackgroundTasks,
    x_connection_id: str | None = Header(None, alias="x-connection-id"),
    x_user_id: str | None = Header(None, alias="x-user-id"),
    x_org_id: str | None = Header(None, alias="x-org-id"),
    x_client_id: str | None = Header(None, alias="x-client-id"),
    x_auth_kind: str | None = Header(None, alias="x-auth-kind"),
    x_verified_device_id: str | None = Header(None, alias="x-verified-device-id"),
) -> Response:
    if not x_connection_id or not x_user_id:
        raise HTTPException(status_code=400, detail="Missing verified connection headers")
    service = await get_connection_service()
    row = await asyncio.to_thread(service.get_connection, x_connection_id)
    owner_id, kind = _assert_machine_connection(
        row,
        user_id=x_user_id,
        org_id=x_org_id or None,
        client_id=x_client_id,
        auth_kind=x_auth_kind,
        verified_device_id=x_verified_device_id,
    )
    if kind == "node":
        background_tasks.add_task(
            _kick_off_node_connect,
            connection_id=x_connection_id,
            owner_id=owner_id,
            user_id=x_user_id,
            client_id=x_client_id or "node-host",
        )
    else:
        background_tasks.add_task(
            _kick_off_operator_connect,
            connection_id=x_connection_id,
            owner_id=owner_id,
            user_id=x_user_id,
        )
    return Response(status_code=200)


async def _kick_off_node_connect(*, connection_id: str, owner_id: str, user_id: str, client_id: str) -> None:
    try:
        await handle_node_connect(
            owner_id=owner_id,
            user_id=user_id,
            connection_id=connection_id,
            connect_params={"role": "node", "client": {"id": client_id, "mode": "node"}},
            management_api=await get_management_api_client(),
        )
    except Exception:
        logger.warning("node relay connect failed conn=%s", connection_id[:12], exc_info=True)


async def _kick_off_operator_connect(*, connection_id: str, owner_id: str, user_id: str) -> None:
    try:
        await handle_operator_connect(
            owner_id=owner_id,
            user_id=user_id,
            connection_id=connection_id,
            connect_params={
                "role": "operator",
                "client": {"id": PAPERCLIP_OPERATOR_CLIENT_ID, "mode": "backend"},
            },
            management_api=await get_management_api_client(),
        )
    except Exception:
        logger.warning("operator relay connect failed conn=%s", connection_id[:12], exc_info=True)


@router.post("/disconnect", status_code=200)
async def ws_disconnect(
    x_connection_id: str | None = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        return Response(status_code=200)
    await asyncio.gather(
        handle_operator_disconnect(x_connection_id),
        handle_node_disconnect(x_connection_id),
        return_exceptions=True,
    )
    try:
        (await get_connection_service()).delete_connection(x_connection_id)
    except Exception:
        logger.warning("machine connection row cleanup failed conn=%s", x_connection_id[:12], exc_info=True)
    return Response(status_code=200)


def _wake_refusal_payload(gate: dict, *, req_id: Any) -> dict:
    code = gate.get("code", "blocked")
    return {
        "type": "res",
        "id": req_id,
        "ok": False,
        "error": {
            "code": code,
            "message": gate.get("message", "Agent wake blocked"),
            "details": {"reason": "wake_admission_gate", "code": code},
        },
    }


async def _admit_operator_wake(*, user_id: str, owner_id: str, body: dict[str, Any]) -> dict | None:
    if body.get("method") not in _WAKE_GATED_METHODS:
        return None
    params = body.get("params")
    session_key = params.get("sessionKey") if isinstance(params, dict) else ""
    try:
        gate = await get_gateway_pool().gate_chat(user_id=user_id, owner_id=owner_id, session_key=session_key or "")
    except Exception:
        # Paperclip work must not be lost on a transient UX-gate read; the
        # Bedrock gate remains the inference fence.
        put_metric("wake.admission", dimensions={"outcome": "gate_error"})
        return None
    if not gate.get("blocked"):
        return None
    put_metric("wake.admission", dimensions={"outcome": "blocked", "reason": gate.get("code", "blocked")})
    return _wake_refusal_payload(gate, req_id=body.get("id"))


@router.post("/message", status_code=200)
async def ws_message(
    body: dict[str, Any],
    x_connection_id: str | None = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        raise HTTPException(status_code=400, detail="Missing x-connection-id header")
    row = await asyncio.to_thread((await get_connection_service()).get_connection, x_connection_id)
    if row is None or row.get("connection_type") not in {"node", "operator"}:
        raise HTTPException(status_code=403, detail="Machine connection required")
    owner_id = row.get("org_id") or row["user_id"]
    user_id = row["user_id"]

    if row["connection_type"] == "operator":
        if row.get("auth_kind") != "paperclip_service":
            raise HTTPException(status_code=403, detail="Verified operator required")
        if not is_operator_connection(x_connection_id):
            await _send_relay_unavailable(x_connection_id, body, "operator")
            return Response(status_code=200)
        if body.get("type") == "req" and body.get("method") == "connect":
            submit_operator_signed_connect(x_connection_id, body)
        else:
            refusal = (
                await _admit_operator_wake(user_id=user_id, owner_id=owner_id, body=body)
                if body.get("type") == "req"
                else None
            )
            if refusal is not None:
                await (await get_management_api_client()).send_message_async(x_connection_id, refusal)
            else:
                await handle_operator_message(x_connection_id, body)
        return Response(status_code=200)

    if row.get("auth_kind") != "device" or not row.get("device_id"):
        raise HTTPException(status_code=403, detail="Verified device required")
    if body.get("type") == "req" and body.get("method") == "connect":
        params = body.get("params")
        device = params.get("device") if isinstance(params, dict) else None
        if (
            isinstance(device, dict)
            and device.get("id") == row.get("device_id")
            and is_node_connection(x_connection_id)
        ):
            submit_node_signed_connect(x_connection_id, body)
        elif isinstance(device, dict) and device.get("id"):
            await _send_relay_unavailable(x_connection_id, body, "node")
        return Response(status_code=200)
    if is_node_connection(x_connection_id):
        await handle_node_message(x_connection_id, body)
    else:
        await _send_relay_unavailable(x_connection_id, body, "node")
    return Response(status_code=200)


async def _send_relay_unavailable(connection_id: str, body: dict[str, Any], kind: str) -> None:
    logger.warning("%s relay unavailable conn=%s", kind, connection_id[:12])
    await (await get_management_api_client()).send_message_async(
        connection_id,
        {
            "type": "res",
            "id": body.get("id"),
            "ok": False,
            "error": {"message": f"{kind} relay unavailable on this node — reconnect"},
        },
    )
