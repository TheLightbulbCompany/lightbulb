"""API Gateway WebSocket HTTP integration for the local-session machine relay only.

Node hosts and Paperclip operators moved to the raw WebSocket gateway relay
(``routers/gateway_relay.py``, on the ``bv-`` door) — one backend task pipes
both sockets for the connection's life instead of API Gateway's per-frame HTTP
fan-out. The local-session relay stays here: it genuinely needs the
Management API to push work to a device from whichever instance handled the
originating HTTP command.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from core.config import settings
from core.services.connection_service import ConnectionService

logger = logging.getLogger(__name__)

_connection_service: ConnectionService | None = None
_singleton_lock = asyncio.Lock()


async def get_connection_service() -> ConnectionService:
    global _connection_service
    if _connection_service is None:
        async with _singleton_lock:
            if _connection_service is None:
                _connection_service = ConnectionService()
    return _connection_service


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


def _assert_local_session_connection(
    row: dict | None,
    *,
    user_id: str,
    org_id: str | None,
    client_id: str | None,
    auth_kind: str | None,
    verified_device_id: str | None,
) -> str:
    """Raise 403 unless the verified headers describe a paired local-session
    device; otherwise return the owner_id. The only client this door admits
    now — node hosts and Paperclip operators moved to the gateway relay."""
    if (
        row is None
        or row.get("user_id") != user_id
        or row.get("org_id") != org_id
        or auth_kind != "device"
        or client_id != "lightbulb-local-sessions"
        or row.get("connection_type") != "local-session"
        or row.get("auth_kind") != "device"
        or row.get("client_id") != client_id
        or not verified_device_id
        or row.get("device_id") != verified_device_id
    ):
        raise HTTPException(status_code=403, detail="Verified local session identity mismatch")
    return org_id or user_id


@router.post("/connect", status_code=200)
async def ws_connect(
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
    _assert_local_session_connection(
        row,
        user_id=x_user_id,
        org_id=x_org_id or None,
        client_id=x_client_id,
        auth_kind=x_auth_kind,
        verified_device_id=x_verified_device_id,
    )
    from core.services.local_session_relay import get_local_session_relay

    await get_local_session_relay().connect(x_connection_id)
    return Response(status_code=200)


@router.post("/disconnect", status_code=200)
async def ws_disconnect(
    x_connection_id: str | None = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        return Response(status_code=200)
    try:
        (await get_connection_service()).delete_connection(x_connection_id)
    except Exception:
        logger.warning("machine connection row cleanup failed conn=%s", x_connection_id[:12], exc_info=True)
    return Response(status_code=200)


@router.post("/message", status_code=200)
async def ws_message(
    body: dict[str, Any],
    request: Request,
    x_connection_id: str | None = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        raise HTTPException(status_code=400, detail="Missing x-connection-id header")
    row = await asyncio.to_thread((await get_connection_service()).get_connection, x_connection_id)
    if row is None or row.get("connection_type") != "local-session":
        raise HTTPException(status_code=403, detail="Machine connection required")

    from core.services.local_session_relay import get_local_session_relay
    from core.services.local_sessions import MAX_ENVELOPE_BYTES
    from core.services.messaging import ApiProblem

    relay = get_local_session_relay()
    try:
        if len(await request.body()) > MAX_ENVELOPE_BYTES:
            raise ApiProblem("INVALID_REQUEST", "Local session envelope exceeds 16 KiB.")
        response = await relay.handle_message(x_connection_id, body)
    except ApiProblem as exc:
        response = {"type": "local.session.error", "code": exc.error.code}
        raw_sid = body.get("sessionId") if isinstance(body, dict) else None
        sid = raw_sid if isinstance(raw_sid, str) and len(raw_sid) <= 64 else None
        if sid is not None:
            response["sessionId"] = sid
        raw_frame = body.get("type") if isinstance(body, dict) else None
        frame = raw_frame if isinstance(raw_frame, str) and len(raw_frame) <= 64 else None
        logger.warning(
            "local session frame rejected",
            extra={
                "code": exc.error.code,
                "frame": frame,
                "sid": sid,
                "cid": x_connection_id,
            },
        )
    await relay.management.send_message_async(x_connection_id, response)
    return Response(status_code=200)
