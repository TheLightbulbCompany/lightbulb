"""WebSocket relay for the container browser live view — CDP screencast path.

Sibling of ``browser_view.py`` (the VNC relay). Same door, same auth fence,
same owner-scoping; the ONLY difference is what it fronts:

    browser_view.py :  bv door -> relay -> container websockify :6080 -> Xvnc :5900
    screencast.py   :  bv door -> relay -> container CDP proxy  :18801 -> Chrome CDP :18800 (loopback)

Instead of the whole X display over RFB, this streams ONE CDP page target via
``Page.startScreencast`` (JPEG frames) and relays human pointer/keyboard back as
CDP ``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent``. Resolution follows
the page viewport, not a fixed 1280x800 X display — the whole point.

Security (see docs/superpowers/spikes/cdp-screencast-reachability-decision.md):
raw CDP (18800) NEVER leaves container loopback. A container-side proxy fronts it
on 18801, reachable ONLY from the backend service SG (existing all-TCP
ContainerFromServiceIngress) with NO ALB path rule — strictly tighter than VNC's
6080. The Clerk-JWT + owner-scoping fence below is the auth, exactly as in
browser_view.py: the bv door (CloudFront -> ALB) is an unauthenticated raw-WS
pipe; the credential is checked HERE.

STOP-FLAG: the container-side proxy that opens 18801 is NOT shipped yet (awaiting
sign-off). On containers without it, ``_discover_page_target`` / the CDP connect
fail and the panel shows its disconnected state — which is exactly the image-pin
rollout gate (old tag = no proxy = inert; new tag = proxy = live).
"""

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, WebSocket
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed
from starlette.websockets import WebSocketDisconnect

from core.auth import AuthContext, _decode_token, _extract_org_claims, resolve_owner_id
from core.containers import get_ecs_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_CLERK_SESSION_COOKIE = "__session"

# The subprotocol we echo back on accept. A browser that offered subprotocols
# fails the WS handshake outright if the server selects none, so — like the VNC
# relay echoing "binary" — we must select a non-credential token. The frontend
# offers ["isol8-screencast", <clerk jwt>]; the jwt is the credential candidate.
_ACCEPT_SUBPROTOCOL = "isol8-screencast"

# CDP frames are JPEG base64 (tens of KB); 10 MB matches the VNC relay ceiling
# and rejects anything absurd before the relay loop.
_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024

# Container-side CDP proxy port (18801) -> loopback Chrome CDP (18800). See the
# reachability decision doc. Raw 18800 is never dialed from here.
_CDP_PROXY_PORT = 18801

# Screencast params. jpeg/quality~60 per the plan; maxWidth/Height cap the frame
# so a huge viewport can't blow the frame budget on a 1 vCPU box. everyNthFrame
# drops frames at the source (cheaper than dropping them after encode).
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1920,
    "maxHeight": 1080,
    "everyNthFrame": 1,
}


async def _resolve_ws_owner(websocket: WebSocket) -> str | None:
    """Validate the Clerk credential on the upgrade and return the owner id.

    Mirrors browser_view.py's fence exactly (JWT via subprotocol for the
    cross-host bv door, cookie as same-host fallback; org claims routed through
    resolve_owner_id so an org member hits the org's container). Closes the
    socket and returns None on any failure.
    """
    offered = websocket.scope.get("subprotocols") or []
    candidates = [p for p in offered if p != _ACCEPT_SUBPROTOCOL]
    cookie_token = websocket.cookies.get(_CLERK_SESSION_COOKIE)
    if cookie_token:
        candidates.append(cookie_token)
    if not candidates:
        await websocket.close(code=4401, reason="Missing session credential")
        return None

    payload = None
    for candidate in candidates:
        try:
            payload = await _decode_token(candidate)
            break
        except Exception:  # noqa: BLE001 - try the next candidate; close below.
            continue
    if payload is None:
        logger.warning("screencast_ws: no offered credential decoded")
        await websocket.close(code=4401, reason="Invalid session credential")
        return None

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4401, reason="Token missing subject")
        return None

    org = _extract_org_claims(payload)
    auth = AuthContext(
        user_id=user_id,
        email=payload.get("email"),
        org_id=org["org_id"],
        org_role=org["org_role"],
        org_slug=org["org_slug"],
        org_permissions=org["org_permissions"],
    )
    return resolve_owner_id(auth)


async def _discover_page_target(ip: str) -> str | None:
    """Return a CDP page target's WS url, rewritten to the proxy host:port.

    CDP's HTTP /json/list advertises webSocketDebuggerUrl as
    ``ws://localhost:18800/devtools/page/<id>``; we keep the path and swap the
    authority to ``{ip}:18801`` so it dials the SG-fenced proxy. Host header is
    the numeric container IP, which Chrome's DNS-rebind check accepts.

    Returns None if the browser isn't up yet (no page target) or CDP is
    unreachable (old image without the proxy) — the caller closes 4503 and the
    panel shows its "asleep/starting" state.
    """
    base = f"http://{ip}:{_CDP_PROXY_PORT}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/json/list")
            resp.raise_for_status()
            targets = resp.json()
    except Exception as e:  # noqa: BLE001 - unreachable / not-yet-started
        logger.info("screencast_ws: CDP discovery failed ip=%s: %s", ip, e)
        return None

    page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
    if page is None:
        return None
    # Keep the path (/devtools/page/<id>), swap authority to the proxy.
    ws_url = str(page["webSocketDebuggerUrl"])
    path = ws_url.split("/", 3)[-1]  # everything after ws://<authority>/
    return f"ws://{ip}:{_CDP_PROXY_PORT}/{path}"


def _cdp_input_method(msg_type: str) -> str | None:
    """Map a frontend input message type to its CDP Input.* method.

    The frontend owns coord mapping and shapes the CDP params; this only routes.
    Unknown types return None and are dropped (never forwarded blindly to CDP).
    """
    return {
        "mouse": "Input.dispatchMouseEvent",
        "key": "Input.dispatchKeyEvent",
    }.get(msg_type)


@router.websocket("/ws")
async def screencast_ws(websocket: WebSocket) -> None:
    """Bidirectional relay: owner's canvas panel <-> their container's CDP."""
    owner_id = await _resolve_ws_owner(websocket)
    if owner_id is None:
        return  # _resolve_ws_owner already closed with the right code.

    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if container is None or ip is None:
        await websocket.close(code=4503, reason="Container not reachable")
        return

    cdp_url = await _discover_page_target(ip)
    if cdp_url is None:
        # No proxy (old image), CDP down, or no page target yet.
        await websocket.close(code=4503, reason="Browser not reachable")
        return

    # Monotonic CDP command id. We fire commands and don't await their replies
    # (a relay has nothing to do with them); we only act on screencastFrame
    # events. Errors surface as CDP error frames, ignored.
    cmd_id = 0

    def _next_id() -> int:
        nonlocal cmd_id
        cmd_id += 1
        return cmd_id

    try:
        async with ws_connect(
            cdp_url,
            max_size=_WS_MAX_FRAME_SIZE,
            open_timeout=10,
            close_timeout=5,
        ) as cdp:
            await websocket.accept(subprotocol=_ACCEPT_SUBPROTOCOL)
            await cdp.send(json.dumps({"id": _next_id(), "method": "Page.enable"}))
            await cdp.send(
                json.dumps({"id": _next_id(), "method": "Page.startScreencast", "params": _SCREENCAST_PARAMS})
            )

            async def cdp_to_client() -> None:
                try:
                    async for raw in cdp:
                        try:
                            evt = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if evt.get("method") != "Page.screencastFrame":
                            continue  # ignore command replies + other events
                        params = evt.get("params") or {}
                        # Forward CDP's base64 data verbatim (no re-encode), then
                        # ack. ACK-AFTER-SEND is the backpressure: Chrome emits the
                        # next frame only after ack, and we await the client send
                        # first, so a slow viewer throttles the stream to ~1
                        # in-flight frame. ponytail: ack-after-send bounds
                        # in-flight to 1; add a frame-drop/window if a fast box
                        # ever needs a deeper pipeline.
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "frame",
                                    "data": params.get("data", ""),
                                    "metadata": params.get("metadata") or {},
                                }
                            )
                        )
                        session_id = params.get("sessionId")
                        if session_id is not None:
                            await cdp.send(
                                json.dumps(
                                    {
                                        "id": _next_id(),
                                        "method": "Page.screencastFrameAck",
                                        "params": {"sessionId": session_id},
                                    }
                                )
                            )
                except (WebSocketDisconnect, ConnectionClosed):
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning("screencast_ws: cdp→client relay error owner=%s: %s", owner_id, e)

            async def client_to_cdp() -> None:
                try:
                    while True:
                        raw = await websocket.receive_text()
                        try:
                            msg = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        method = _cdp_input_method(msg.get("type"))
                        if method is None:
                            continue
                        event = msg.get("event")
                        if not isinstance(event, dict):
                            continue
                        await cdp.send(json.dumps({"id": _next_id(), "method": method, "params": event}))
                except (WebSocketDisconnect, ConnectionClosed):
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning("screencast_ws: client→cdp relay error owner=%s: %s", owner_id, e)

            # FIRST_COMPLETED teardown, same rationale as browser_view.py: either
            # leg ending means the session is dead, and leaving the CDP socket
            # half-open would keep Page.startScreencast running in the owner's
            # container (CPU cost on 1 vCPU) after the viewer closed the panel.
            legs = {
                asyncio.create_task(cdp_to_client()),
                asyncio.create_task(client_to_cdp()),
            }
            _, pending = await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
            for leg in pending:
                leg.cancel()
            await asyncio.gather(*legs, return_exceptions=True)
            # Falling out of the `async with` closes the CDP socket; Chrome stops
            # the screencast for a dropped client.
    except Exception as e:  # noqa: BLE001 - top-level guard
        logger.warning("screencast_ws: connection failed owner=%s ip=%s: %s", owner_id, ip, e)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
