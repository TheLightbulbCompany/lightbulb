"""WebSocket relay for the container browser live view — CDP screencast path.

Sibling of ``browser_view.py`` (the VNC relay). Same door, same auth fence,
same owner-scoping; the ONLY difference is what it fronts:

    browser_view.py :  bv door -> relay -> container websockify :6080 -> Xvnc :5900
    screencast.py   :  bv door -> relay -> container CDP proxy  :18801 -> Chrome CDP :18800 (loopback)

Instead of the whole X display over RFB, this streams ONE CDP page target via
``Page.startScreencast`` (JPEG frames) and relays human pointer/keyboard back as
CDP ``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent``. Resolution follows
the page viewport, not a fixed 1280x800 X display — the whole point. The viewport
itself follows the viewer: the panel reports its CSS-pixel box as
``{type:"resize"}`` messages, applied here via ``Emulation.setDeviceMetricsOverride``
so the page REFLOWS to the panel size instead of the frame being stretched.

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
import time

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
# drops frames at the source (cheaper than dropping them after encode). The cap
# already exceeds the viewport ceiling below (1600x1000 @ dsf 1), so a viewer
# resize never needs a screencast restart.
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1920,
    "maxHeight": 1080,
    "everyNthFrame": 1,
}

# Viewport ceiling for viewer-driven resizes. Frame production is CPU-BOUND
# under SwiftShader software rendering (measured ~180-250ms/frame at 1280x800,
# cost scales with PIXEL COUNT), so matching a big retina panel 1:1 would tank
# the real framerate. The cap is aspect-preserving: a larger panel gets the same
# SHAPE at a lower resolution, and the panel's objectFit scales it back up —
# the reflow (aspect) is the correctness fix, resolution is just crispness.
_MAX_VIEWPORT_WIDTH = 1600
_MAX_VIEWPORT_HEIGHT = 1000

# Floor between Emulation.setDeviceMetricsOverride applies. The panel already
# trailing-debounces its ResizeObserver; this is the server-side thrash fence
# for clients that don't.
_RESIZE_MIN_INTERVAL = 0.1

# Deliver at most 30fps — the standard interactive-remote-view ceiling. CDP emits
# the NEXT screencastFrame only after we ack the last one, so spacing our acks by
# one frame-interval caps BOTH the delivered framerate AND the container's
# screencast-capture CPU (no wasted encode/send on a busy page) without dropping
# frames (a dropped-but-unacked frame would stall CDP, which waits for the ack).
SCREENCAST_MAX_FPS = 30
_MIN_FRAME_INTERVAL = 1.0 / SCREENCAST_MAX_FPS


def _ack_delay(last_ack: float | None, now: float) -> float:
    """Seconds to wait before the next screencastFrameAck to hold delivery at
    ≤ SCREENCAST_MAX_FPS. ``last_ack``/``now`` are ``time.monotonic()`` seconds.

    Returns 0 when the previous ack is already ≥ one frame-interval old — a
    stream slower than 30fps is never throttled, so it adds no latency.
    """
    if last_ack is None:
        return 0.0
    return max(0.0, _MIN_FRAME_INTERVAL - (now - last_ack))


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


def _select_page_target(targets: list, desired_target_id: str | None) -> dict | None:
    """Pick the CDP page target to screencast from a /json/list payload.

    When the client asked for a specific tab (``?target=<raw cdp id>``, sent by
    the chrome tab strip on switch) and it's still an open page target, stream
    it; otherwise fall back to the first page target (fresh connect, or the
    requested tab was closed / its raw id rotated on a redirect). Only real
    ``page`` targets with a debugger url are eligible — never a background /
    service-worker target.
    """
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if desired_target_id:
        wanted = next((t for t in pages if str(t.get("id") or "") == desired_target_id), None)
        if wanted is not None:
            return wanted
    return pages[0] if pages else None


async def _discover_page_target(ip: str, desired_target_id: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(proxy ws url, raw target id)`` for the page target to stream.

    CDP's HTTP /json/list advertises webSocketDebuggerUrl as
    ``ws://localhost:18800/devtools/page/<id>``; we keep the path and swap the
    authority to ``{ip}:18801`` so it dials the SG-fenced proxy. Host header is
    the numeric container IP, which Chrome's DNS-rebind check accepts. The raw
    target id is returned so the relay can echo it to the client ({type:target})
    for the active-tab highlight + URL bar.

    Returns ``(None, None)`` if the browser isn't up yet (no page target) or CDP
    is unreachable (old image without the proxy) — the caller closes 4503 and
    the panel shows its "asleep/starting" state.
    """
    base = f"http://{ip}:{_CDP_PROXY_PORT}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/json/list")
            resp.raise_for_status()
            targets = resp.json()
    except Exception as e:  # noqa: BLE001 - unreachable / not-yet-started
        logger.info("screencast_ws: CDP discovery failed ip=%s: %s", ip, e)
        return None, None

    page = _select_page_target(targets, desired_target_id)
    if page is None:
        return None, None
    # Keep the path (/devtools/page/<id>), swap authority to the proxy.
    ws_url = str(page["webSocketDebuggerUrl"])
    path = ws_url.split("/", 3)[-1]  # everything after ws://<authority>/
    return f"ws://{ip}:{_CDP_PROXY_PORT}/{path}", str(page.get("id") or "")


def _resize_override_params(msg: dict) -> dict | None:
    """Validate a client ``{type:"resize"}`` into Emulation.setDeviceMetricsOverride
    params. Trust boundary: degenerate sizes are dropped (None), oversized ones
    are scaled down aspect-preserving to the viewport ceiling.
    """
    try:
        width = int(msg.get("width"))
        height = int(msg.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    scale = min(1.0, _MAX_VIEWPORT_WIDTH / width, _MAX_VIEWPORT_HEIGHT / height)
    return {
        "width": max(1, round(width * scale)),
        "height": max(1, round(height * scale)),
        # Forced to 1 regardless of the client's devicePixelRatio: SwiftShader
        # compositing is CPU-bound per pixel, so dsf 2 would quadruple frame
        # cost (~5fps -> ~2fps) for crispness a JPEG-over-WS view doesn't repay.
        # ponytail: honor client dsf (<=2) if containers ever get a real GPU.
        "deviceScaleFactor": 1,
        "mobile": False,
    }


class _ResizeCoalescer:
    """Latest-wins applier for viewport resizes.

    Bursts overwrite the pending value instead of queueing a CDP command each;
    at most one apply is in flight, spaced >= _RESIZE_MIN_INTERVAL apart. An
    apply failure is swallowed — a resize must never crash the session.
    """

    def __init__(self, apply) -> None:
        self._apply = apply
        self._pending: dict | None = None
        self._task: asyncio.Task | None = None

    def submit(self, params: dict) -> None:
        self._pending = params
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending is not None:
            params, self._pending = self._pending, None
            try:
                await self._apply(params)
            except Exception as e:  # noqa: BLE001 - session may be closing
                logger.info("screencast_ws: resize apply failed: %s", e)
                return
            await asyncio.sleep(_RESIZE_MIN_INTERVAL)


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

    # ?target=<raw cdp target id> lets the chrome tab strip point the stream at
    # the tab the user switched to (the screencast is per-page; focusing a tab
    # doesn't move a stream already bound to another target). Absent/stale → the
    # first page target, unchanged from before.
    desired_target_id = websocket.query_params.get("target") or None
    cdp_url, target_id = await _discover_page_target(ip, desired_target_id)
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
            # Tell the client which raw target it's now streaming so the tab
            # strip highlights the right tab + the URL bar shows its address.
            await websocket.send_text(json.dumps({"type": "target", "targetId": target_id}))
            await cdp.send(json.dumps({"id": _next_id(), "method": "Page.enable"}))
            await cdp.send(
                json.dumps({"id": _next_id(), "method": "Page.startScreencast", "params": _SCREENCAST_PARAMS})
            )

            async def _apply_viewport(params: dict) -> None:
                await cdp.send(
                    json.dumps({"id": _next_id(), "method": "Emulation.setDeviceMetricsOverride", "params": params})
                )

            # ponytail: the viewport override outlives the viewer — the shared
            # browser stays at the last panel size after the panel closes.
            # Clearing on last-viewer disconnect needs cross-session refcounting
            # (and miscounts during the maxHealthyPercent:200 deploy transient
            # when two backends run); add Emulation.clearDeviceMetricsOverride
            # behind a per-owner viewer count if agents mind the leftover size.
            resizer = _ResizeCoalescer(_apply_viewport)

            async def cdp_to_client() -> None:
                last_ack: float | None = None
                try:
                    async for raw in cdp:
                        try:
                            evt = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if evt.get("method") != "Page.screencastFrame":
                            continue  # ignore command replies + other events
                        params = evt.get("params") or {}
                        # Forward CDP's base64 data verbatim (no re-encode) — the
                        # frame we already have goes out immediately, so pacing
                        # adds no latency to a displayed frame. ACK-AFTER-SEND is
                        # still the backpressure: Chrome emits the next frame only
                        # after ack, bounding in-flight to 1. On top of that we
                        # PACE the ack to hold delivery ≤ SCREENCAST_MAX_FPS —
                        # spacing acks throttles the SOURCE (capture + encode CPU),
                        # not just the wire. A slower stream is acked at once.
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
                            delay = _ack_delay(last_ack, time.monotonic())
                            if delay > 0:
                                await asyncio.sleep(delay)
                            await cdp.send(
                                json.dumps(
                                    {
                                        "id": _next_id(),
                                        "method": "Page.screencastFrameAck",
                                        "params": {"sessionId": session_id},
                                    }
                                )
                            )
                            last_ack = time.monotonic()
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
                        if msg.get("type") == "resize":
                            params = _resize_override_params(msg)
                            if params is not None:
                                resizer.submit(params)
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
