"""WebSocket relay for the container browser live view.

The per-user container runs its Chromium headful on an Xvfb display, with a
viewing pipeline started at boot (container-stack.ts, Step C4):

    websockify :6080 (container IP)  ->  socat :5900 (loopback)  ->  x11vnc -inetd

This router is the ONLY door to that pipeline. Port 6080 is reachable solely
from the backend security group (service-stack.ts ContainerFromServiceIngress);
it is not in the task's portMappings and has no public route. The frontend's
noVNC client (@novnc/novnc, in the workspace's browser panel) connects here and
we relay RFB frames byte-for-byte to the owner's container.

Auth is the Clerk ``__session`` cookie, validated per upgrade — browsers cannot
attach an Authorization header to a JS-initiated WebSocket open, so the cookie
is the only credential channel (same constraint, and same solution, as
``paperclip_proxy.proxy_ws``). Owner scoping is the whole fence: the container
is resolved from the authenticated identity via ``resolve_owner_id``, never
from anything the client sends, so there is no cross-tenant parameter to get
wrong.

The container IP is resolved per connection through Cloud Map
(``resolve_running_container``) and never cached — the service is long-lived
but the task IP changes on every deploy/OOM/reprovision, and a cached dead IP
has already caused one prod outage.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed
from starlette.websockets import WebSocketDisconnect

from core.auth import AuthContext, _decode_token, _extract_org_claims, resolve_owner_id
from core.containers import get_ecs_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_CLERK_SESSION_COOKIE = "__session"

# RFB frames are small (input events up, framebuffer rects down); 10 MB matches
# the paperclip relay's ceiling and is far above any legitimate VNC frame, so
# oversized frames are rejected by the websockets library before the relay loop.
_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024


@router.websocket("/ws")
async def browser_view_ws(websocket: WebSocket) -> None:
    """Bidirectional relay: owner's noVNC client <-> their container's websockify."""
    # ---- Auth: Clerk session cookie on the upgrade frame ----
    clerk_token = websocket.cookies.get(_CLERK_SESSION_COOKIE)
    if not clerk_token:
        await websocket.close(code=4401, reason="Missing session credential")
        return
    try:
        payload = await _decode_token(clerk_token)
    except Exception as e:  # noqa: BLE001 - opaque to caller; we close.
        logger.warning("browser_view_ws: invalid Clerk token: %s", e)
        await websocket.close(code=4401, reason="Invalid Clerk token")
        return

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4401, reason="Token missing subject")
        return

    # Org members share the org's container, so the lookup key must go through
    # resolve_owner_id with the org claims — a bare user_id would miss every
    # org-owned container.
    org = _extract_org_claims(payload)
    auth = AuthContext(
        user_id=user_id,
        email=payload.get("email"),
        org_id=org["org_id"],
        org_role=org["org_role"],
        org_slug=org["org_slug"],
        org_permissions=org["org_permissions"],
    )
    owner_id = resolve_owner_id(auth)

    # ---- Resolve the container, IP per-connection via Cloud Map ----
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if container is None or ip is None:
        # Covers: never provisioned, provisioning (no service yet), scaled to
        # zero (trial idle), or task mid-replacement. The panel renders its
        # "agents asleep / starting" state off this close code.
        await websocket.close(code=4503, reason="Container not reachable")
        return

    upstream_url = f"ws://{ip}:6080/"

    try:
        # Connect upstream FIRST, echoing the client's offered subprotocols
        # (noVNC offers none by default; older clients offer "binary"), then
        # accept the client with whatever websockify actually selected — so
        # both legs agree without this relay hardcoding a protocol list.
        offered = websocket.scope.get("subprotocols") or []
        async with ws_connect(
            upstream_url,
            subprotocols=offered or None,
            max_size=_WS_MAX_FRAME_SIZE,
            open_timeout=10,
            close_timeout=5,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await upstream.send(msg["text"])
                except (WebSocketDisconnect, ConnectionClosed):
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning("browser_view_ws: client→container relay error owner=%s: %s", owner_id, e)

            async def upstream_to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except (WebSocketDisconnect, ConnectionClosed):
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning("browser_view_ws: container→client relay error owner=%s: %s", owner_id, e)

            # FIRST_COMPLETED, deliberately unlike the paperclip relay's
            # gather-both idiom: either leg ending means the session is dead,
            # and a byte relay has no mid-frame worth preserving. Waiting for
            # BOTH legs would leave the relay half-open when the viewer closes
            # the panel (upstream_to_client blocks in `async for` until the
            # container side speaks) — and a half-open relay here pins a live
            # x11vnc polling the framebuffer in the owner's container, the
            # exact CPU cost the per-connection spawn design exists to bound.
            legs = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            _, pending = await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
            for leg in pending:
                leg.cancel()
            await asyncio.gather(*legs, return_exceptions=True)
            # Falling out of the `async with` closes the upstream socket, which
            # ends the container-side x11vnc; the finally closes the viewer.
    except Exception as e:  # noqa: BLE001 - top-level guard
        logger.warning("browser_view_ws: connection failed owner=%s ip=%s: %s", owner_id, ip, e)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
