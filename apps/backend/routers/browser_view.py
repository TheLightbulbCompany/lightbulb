"""WebSocket relay for the container browser live view.

The per-user container runs its Chromium headful on an Xvfb display, with a
viewing pipeline started at boot (container-stack.ts, Step C4):

    websockify :6080 (container IP)  ->  socat :5900 (loopback)  ->  x11vnc -inetd

This router is the ONLY door to that pipeline. Port 6080 is reachable solely
from the backend security group (service-stack.ts ContainerFromServiceIngress);
it is not in the task's portMappings and has no public route. The frontend's
noVNC client (@novnc/novnc, in the workspace's browser panel) connects here and
we relay RFB frames byte-for-byte to the owner's container.

Auth: browsers cannot attach an Authorization header to a JS-initiated
WebSocket open, and this route is served on its own CloudFront host
(``bv[-env].isol8.co`` — the API host is an API Gateway HTTP API, which cannot
pass WebSocket upgrades at all), where the Clerk session cookie does not
follow. The credential therefore rides the ``Sec-WebSocket-Protocol`` header:
the panel offers ``["binary", <clerk jwt>]``, we validate the JWT entry and
accept ``"binary"`` — a browser fails the connection outright if it offered
subprotocols and the server selects none, which is why ``binary`` must be
echoed rather than the connection accepted bare. The ``__session`` cookie
remains a fallback for same-host callers (tests, future cookie-domain
setups). Owner scoping is the whole fence: the container is resolved from the
authenticated identity via ``resolve_owner_id``, never from anything the
client sends, so there is no cross-tenant parameter to get wrong.

The container IP is resolved per connection through Cloud Map
(``resolve_running_container``) and never cached — the service is long-lived
but the task IP changes on every deploy/OOM/reprovision, and a cached dead IP
has already caused one prod outage.
"""

import asyncio
import logging
import posixpath
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed
from starlette.websockets import WebSocketDisconnect

from core.auth import AuthContext, _decode_token, _extract_org_claims, resolve_owner_id
from core.containers import get_ecs_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_CLERK_SESSION_COOKIE = "__session"
_BROWSER_VIEW_COOKIE = "isol8_browser_view"
_NEKO_COOKIE = "neko"
_COOKIE_PATH = "/api/v1/browser-view"
_NEKO_PATH = "/api/v1/browser-view/webrtc"
_COOKIE_MAX_AGE = 15 * 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_SAFE_REQUEST_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "cache-control", "content-type", "if-none-match", "range"}
)
_SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-encoding", "content-language", "content-type", "etag", "last-modified"}
)

# RFB frames are small (input events up, framebuffer rects down); 10 MB matches
# the paperclip relay's ceiling and is far above any legitimate VNC frame, so
# oversized frames are rejected by the websockets library before the relay loop.
_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024


def _auth_from_payload(payload: dict) -> AuthContext:
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    org = _extract_org_claims(payload)
    return AuthContext(
        user_id=user_id,
        email=payload.get("email"),
        org_id=org["org_id"],
        org_role=org["org_role"],
        org_slug=org["org_slug"],
        org_permissions=org["org_permissions"],
    )


async def _browser_auth(token: str | None) -> AuthContext:
    if not token:
        raise HTTPException(status_code=401, detail="Missing session credential")
    try:
        return _auth_from_payload(await _decode_token(token))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Clerk decoder owns validation detail.
        raise HTTPException(status_code=401, detail="Invalid session credential") from exc


def _frontend_origins() -> frozenset[str]:
    from core.config import settings

    return frozenset(settings.cors_origins_list)


def _bv_origins() -> frozenset[str]:
    origins = set()
    for origin in _frontend_origins():
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        if host in {"localhost", "127.0.0.1"}:
            origins.add("http://localhost:8000")
        elif host == "isol8.co":
            origins.add(f"{parsed.scheme}://bv.isol8.co")
        elif host.endswith(".isol8.co"):
            env = host.removesuffix(".isol8.co")
            origins.add(f"{parsed.scheme}://bv-{env}.isol8.co")
    return frozenset(origins)


def _require_origin(origin: str | None, allowed: frozenset[str]) -> None:
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="Invalid Origin")


async def _resolve_running(auth: AuthContext) -> str:
    _, ip = await get_ecs_manager().resolve_running_container(resolve_owner_id(auth))
    if not ip:
        raise HTTPException(status_code=503, detail="Container not reachable")
    return ip


def _upstream_path(path: str) -> str:
    normalized = posixpath.normpath("/" + unquote(path).replace("\\", "/")).lstrip("/")
    if normalized == "api/room/broadcast" or normalized.startswith("api/room/broadcast/"):
        raise HTTPException(status_code=403, detail="Broadcast route unavailable")
    return f"{_NEKO_PATH}/{normalized}" if normalized else f"{_NEKO_PATH}/"


def _cookie_secure(request: Request) -> bool:
    return request.url.hostname not in {"localhost", "127.0.0.1"}


def _set_cookie(response: Response, key: str, value: str, request: Request) -> None:
    response.set_cookie(
        key=key,
        value=value,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
        path=_COOKIE_PATH,
    )


async def _neko_session(ip: str, cookie: str | None) -> str:
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        if cookie:
            reused = await client.get(
                f"http://{ip}:6081{_NEKO_PATH}/api/whoami",
                headers={"Cookie": f"{_NEKO_COOKIE}={cookie}"},
            )
            if reused.is_success:
                return cookie
        response = await client.post(f"http://{ip}:6081{_NEKO_PATH}/api/login", json={"username": "owner"})
        response.raise_for_status()
        session = response.cookies.get(_NEKO_COOKIE)
    if not session:
        raise HTTPException(status_code=502, detail="Neko login did not return a session")
    return session


def _upstream_headers(request: Request, neko_cookie: str) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _SAFE_REQUEST_HEADERS and name.lower() not in _HOP_BY_HOP
    } | {"cookie": f"{_NEKO_COOKIE}={neko_cookie}"}


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() in _SAFE_RESPONSE_HEADERS}


async def _close_http(client: httpx.AsyncClient, response: httpx.Response) -> None:
    await response.aclose()
    await client.aclose()


@router.post("/session")
async def browser_view_session(request: Request) -> Response:
    """Mint the short-lived browser-view cookies for the verified owner."""
    _require_origin(request.headers.get("origin"), _frontend_origins())
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing session credential")
    auth = await _browser_auth(token)
    ip = await _resolve_running(auth)
    neko = await _neko_session(ip, request.cookies.get(_NEKO_COOKIE))
    response = Response(
        content='{"iframe_url":"/api/v1/browser-view/webrtc/?cast=1","state":"ready"}',
        media_type="application/json",
    )
    _set_cookie(response, _BROWSER_VIEW_COOKIE, token, request)
    _set_cookie(response, _NEKO_COOKIE, neko, request)
    return response


async def _webrtc_auth(request: Request) -> tuple[AuthContext, str]:
    if request.method not in _SAFE_METHODS:
        _require_origin(request.headers.get("origin"), _frontend_origins() | _bv_origins())
    auth = await _browser_auth(request.cookies.get(_BROWSER_VIEW_COOKIE))
    neko = request.cookies.get(_NEKO_COOKIE)
    if not neko:
        raise HTTPException(status_code=401, detail="Missing Neko session")
    return auth, neko


async def _webrtc_proxy(request: Request, path: str) -> Response:
    upstream_path = _upstream_path(path)
    auth, neko = await _webrtc_auth(request)
    ip = await _resolve_running(auth)
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        upstream_request = client.build_request(
            request.method,
            f"http://{ip}:6081{upstream_path}",
            headers=_upstream_headers(request, neko),
            content=request.stream(),
        )
        upstream = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Neko unavailable") from None
    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        background=BackgroundTask(_close_http, client, upstream),
    )
    if session := upstream.cookies.get(_NEKO_COOKIE):
        _set_cookie(response, _NEKO_COOKIE, session, request)
    return response


@router.api_route("/webrtc/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def browser_view_webrtc_root(request: Request) -> Response:
    return await _webrtc_proxy(request, "")


@router.api_route("/webrtc/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def browser_view_webrtc_http(request: Request, path: str) -> Response:
    return await _webrtc_proxy(request, path)


@router.websocket("/webrtc/ws")
async def browser_view_webrtc_ws(websocket: WebSocket) -> None:
    """Relay Neko signaling; either closed leg immediately tears down both."""
    try:
        _require_origin(websocket.headers.get("origin"), _frontend_origins() | _bv_origins())
        auth = await _browser_auth(websocket.cookies.get(_BROWSER_VIEW_COOKIE))
        neko = websocket.cookies.get(_NEKO_COOKIE)
        if not neko:
            raise HTTPException(status_code=401, detail="Missing Neko session")
        ip = await _resolve_running(auth)
    except HTTPException as exc:
        await websocket.close(code=4403 if exc.status_code == 403 else 4401)
        return

    try:
        async with ws_connect(
            f"ws://{ip}:6081{_NEKO_PATH}/ws",
            additional_headers={"Cookie": f"{_NEKO_COOKIE}={neko}"},
            max_size=_WS_MAX_FRAME_SIZE,
            open_timeout=5,
            close_timeout=5,
        ) as upstream:
            await websocket.accept()

            async def client_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            legs = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
            _, pending = await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
            for leg in pending:
                leg.cancel()
            await asyncio.gather(*legs, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 - upstream is an auxiliary process.
        logger.warning("browser_view_webrtc_ws: connection failed: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            pass


@router.websocket("/ws")
async def browser_view_ws(websocket: WebSocket) -> None:
    """Bidirectional relay: owner's noVNC client <-> their container's websockify."""
    # ---- Auth: JWT via subprotocol (cross-host), cookie as fallback ----
    # A JWT is a syntactically valid RFC 6455 subprotocol token (base64url
    # alphabet + dots, no commas), so it travels intact in the offer list.
    # Anything that isn't "binary" is treated as a candidate credential; the
    # first one that decodes wins. The offer list is attacker-controlled by
    # construction — a candidate that fails to decode is simply skipped, and
    # decoding is the same Clerk JWKS validation the cookie path uses.
    offered = websocket.scope.get("subprotocols") or []
    candidates = [p for p in offered if p != "binary"]
    cookie_token = websocket.cookies.get(_CLERK_SESSION_COOKIE)
    if cookie_token:
        candidates.append(cookie_token)
    if not candidates:
        await websocket.close(code=4401, reason="Missing session credential")
        return

    payload = None
    for candidate in candidates:
        try:
            payload = await _decode_token(candidate)
            break
        except Exception:  # noqa: BLE001 - try the next candidate; we close below.
            continue
    if payload is None:
        logger.warning("browser_view_ws: no offered credential decoded")
        await websocket.close(code=4401, reason="Invalid session credential")
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
        # Upstream gets ONLY "binary" (when offered) — never the credential
        # subprotocol, which must not leave this process. Client is accepted
        # with "binary" for the same reason it was offered: a browser that
        # offered subprotocols fails the connection if the server selects none.
        binary_offered = "binary" in offered
        async with ws_connect(
            upstream_url,
            subprotocols=["binary"] if binary_offered else None,
            max_size=_WS_MAX_FRAME_SIZE,
            open_timeout=10,
            close_timeout=5,
        ) as upstream:
            await websocket.accept(subprotocol="binary" if binary_offered else upstream.subprotocol)

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
