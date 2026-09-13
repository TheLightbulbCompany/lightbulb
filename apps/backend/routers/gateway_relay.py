"""Raw WebSocket relay for machine clients (desktop node hosts, Paperclip operators) -> the owner's OpenClaw gateway.

One backend task owns both sockets for the connection's life; nothing about the connection
lives anywhere else except a TTL presence row. The OpenClaw handshake is the client's and
the container's business - frames are forwarded verbatim except the two hooks below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from core.containers import get_ecs_manager
from core.observability.metrics import put_metric
from core.services import machine_auth, relay_hooks
from core.services.connection_service import ConnectionService
from core.services.container_rpc import GATEWAY_PORT
from core.services.container_wake import request_wake_silent
from core.services.desktop_pair_live import heal_desktop_device_not_paired

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024
_PARSE_LIMIT = 64 * 1024
_REFRESH_SECONDS = 60
# relay.close's `code` dimension: bucket to the codes this relay actually
# distinguishes plus one 4xxx catch-all. A raw code would blow up dimension
# cardinality — upstream can close with any of ~70 defined codes plus
# arbitrary app-specific ones in 4000-4999.
_KNOWN_CLOSE_CODES = frozenset({1000, 1001, 1008, 1011, 1013})


def _close_code_bucket(code: int) -> str:
    if code in _KNOWN_CLOSE_CODES:
        return str(code)
    if 4000 <= code < 5000:
        return "4xxx"
    return "other"


def _parse(frame: str) -> dict | None:
    # Cheap char-count pre-check before the (allocating) UTF-8 encode: a
    # frame already over the limit in characters is over it in bytes too
    # (encode() only ever grows byte count relative to char count), so this
    # short-circuits the copy for the common case of a wildly oversized frame.
    if len(frame) > _PARSE_LIMIT or len(frame.encode()) > _PARSE_LIMIT:
        return None
    try:
        body = json.loads(frame)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@router.websocket("/ws")
@router.websocket("/ws/")
async def gateway_relay_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    client_id = websocket.query_params.get("clientId", "")
    try:
        identity = await machine_auth.verify_upgrade(token, client_id)
    except Exception:  # noqa: BLE001 — verify_upgrade documents "never raises"; defend anyway
        logger.warning("relay upgrade auth error", exc_info=True)
        put_metric("relay.connect", dimensions={"kind": "unknown", "outcome": "error"})
        await websocket.close(code=1011, reason="Internal error")
        return
    if identity is None:
        put_metric("relay.connect", dimensions={"kind": "unknown", "outcome": "unauthorized"})
        await websocket.close(code=1008, reason="Unauthorized")
        return
    owner_id, kind = identity.owner_id, identity.kind

    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("relay container resolve error owner=%s", owner_id, exc_info=True)
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": "error"})
        await websocket.close(code=1011, reason="Internal error")
        return
    if container is None or ip is None:
        # RULING D14: only an operator connect wakes a stopped container. A
        # node's own reconnect (a laptop coming back from sleep) never did
        # before this relay and must not start now — it would re-provision a
        # parked trial container forever.
        outcome = await request_wake_silent(owner_id, door="relay") if kind == "operator" else "skipped_node"
        if kind == "operator":
            await relay_hooks.maybe_disarm_expired_trial(owner_id)
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": f"wake_{outcome}"})
        await websocket.close(code=1013, reason="Container not running")
        return

    connections = ConnectionService()
    connection_id = f"relay#{uuid.uuid4()}"
    try:
        await connections.put_relay_presence(connection_id=connection_id, identity=identity)
    except Exception:  # noqa: BLE001 — presence is best-effort; the relay still serves
        logger.warning("relay presence write failed kind=%s", kind, exc_info=True)
        put_metric("relay.presence", dimensions={"outcome": "put_error"})

    close_code = 1000
    node_ready = False
    not_paired_healed = False
    legs: set[asyncio.Task] = set()
    refresher: asyncio.Task | None = None
    try:
        async with ws_connect(
            f"ws://{ip}:{GATEWAY_PORT}", max_size=_WS_MAX_FRAME_SIZE, open_timeout=10, close_timeout=5
        ) as upstream:
            await websocket.accept()
            put_metric("relay.connect", dimensions={"kind": kind, "outcome": "ok"})

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                            continue
                        text = msg.get("text")
                        if text is None:
                            continue
                        if kind == "operator":
                            body = _parse(text)
                            if (
                                body
                                and body.get("type") == "req"
                                and body.get("method") in relay_hooks.WAKE_GATED_METHODS
                            ):
                                refusal = await relay_hooks.admit_operator_wake(
                                    user_id=identity.user_id, owner_id=owner_id, body=body
                                )
                                if refusal is not None:
                                    await websocket.send_text(json.dumps(refusal))
                                    continue
                        await upstream.send(text)
                except (WebSocketDisconnect, ConnectionClosed):
                    return

            async def upstream_to_client() -> None:
                nonlocal node_ready, not_paired_healed
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                            continue
                        await websocket.send_text(frame)
                        body = _parse(frame)
                        if not body or body.get("type") != "res":
                            continue
                        error = body.get("error") if isinstance(body.get("error"), dict) else None
                        if error and error.get("code") == "NOT_PAIRED":
                            # Once per connection (spec §4.5): a looping client
                            # that keeps getting rejected must not hammer the
                            # self-heal RPCs on every retry.
                            if not not_paired_healed:
                                not_paired_healed = True
                                details = error.get("details") if isinstance(error.get("details"), dict) else {}
                                request_id = (
                                    details.get("requestId") if isinstance(details.get("requestId"), str) else None
                                )
                                device_id = identity.device_id or (
                                    details.get("deviceId") if isinstance(details.get("deviceId"), str) else None
                                )
                                if device_id:
                                    await _self_heal(kind, owner_id, device_id, request_id)
                        elif (
                            kind == "node"
                            and not node_ready
                            and body.get("ok") is True
                            and isinstance(body.get("payload"), dict)
                            and body["payload"].get("type") == "hello-ok"
                        ):
                            node_ready = True
                            try:
                                await relay_hooks.on_node_ready(owner_id, identity.user_id, identity.device_id or "")
                            except Exception:  # noqa: BLE001 — a hook failure must not kill the leg
                                logger.warning("relay on_node_ready hook failed owner=%s", owner_id, exc_info=True)
                except (WebSocketDisconnect, ConnectionClosed):
                    return

            async def refresh_presence() -> None:
                while True:
                    await asyncio.sleep(_REFRESH_SECONDS)
                    try:
                        alive = await connections.refresh_relay_presence(connection_id)
                    except Exception:  # noqa: BLE001
                        put_metric("relay.presence", dimensions={"outcome": "refresh_error"})
                        continue
                    if not alive:
                        put_metric("relay.presence", dimensions={"outcome": "revoked"})
                        return  # ends the wait below → both legs cancelled → close 1008

            legs = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
            refresher = asyncio.create_task(refresh_presence())
            done, pending = await asyncio.wait(legs | {refresher}, return_when=asyncio.FIRST_COMPLETED)
            if refresher in done:
                close_code = 1008
            for task in done:
                if task is refresher or task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.warning("relay leg failed kind=%s", kind, exc_info=exc)
                    close_code = 1011
            for task in pending:
                task.cancel()
            await asyncio.gather(*legs, refresher, return_exceptions=True)

            # Propagate the container's own close code (e.g. a deploy/restart
            # closing 4401/1001) to the client instead of always reporting our
            # generic 1000 — spec §4.4/§6 "same code". 1005/1006 are the
            # websockets library's synthetic "no close frame at all" markers
            # and must never be forwarded as if the peer sent them. Only
            # overwrite the DEFAULT 1000 — a 1011 (leg failure) or 1008
            # (presence revoked) already decided above is a stronger signal
            # than whatever the upstream container happened to close with.
            code = upstream.close_code
            if close_code == 1000 and code and 1000 <= code < 5000 and code not in (1005, 1006):
                close_code = code
    except Exception:  # noqa: BLE001 — never let a relay error take the worker down
        logger.warning("relay error kind=%s", kind, exc_info=True)
        close_code = 1011
    finally:
        # A drain/shutdown cancellation can interrupt this coroutine at any
        # await above, before the normal pending-task cancel+gather runs —
        # leaving the legs/refresher orphaned. Cancel them unconditionally
        # here first so the presence delete + relay.close below still run on
        # a bounded, predictable path regardless of how we got here.
        for task in (*legs, *((refresher,) if refresher is not None else ())):
            if not task.done():
                task.cancel()
        try:
            await asyncio.to_thread(connections.delete_connection, connection_id)
        except Exception:  # noqa: BLE001
            put_metric("relay.presence", dimensions={"outcome": "delete_error"})
        if kind == "node" and node_ready:
            try:
                await relay_hooks.on_node_closed(owner_id, identity.user_id)
            except Exception:  # noqa: BLE001
                logger.warning("relay node-closed hook failed", exc_info=True)
        put_metric("relay.close", dimensions={"kind": kind, "code": _close_code_bucket(close_code)})
        try:
            await websocket.close(code=close_code)
        except Exception:  # noqa: BLE001 — already closed by the client
            pass


async def _self_heal(kind: str, owner_id: str, device_id: str, request_id: str | None) -> None:
    # Both kinds share the same gate and the same approve call —
    # heal_desktop_device_not_paired requires the deviceId to already be in
    # the owner's backend-written devices/paired.json (the artifact of the
    # Clerk-authed /desktop/pair flow) before approving anything. That gate
    # matters just as much for an operator: the operator's MachineIdentity
    # carries no device_id of its own (machine_auth._operator always sets
    # device_id=None), so this deviceId comes entirely from the container's
    # own NOT_PAIRED frame — untrusted input a compromised or buggy container
    # could use to mint trust for an arbitrary device id if it weren't gated.
    try:
        healed = await heal_desktop_device_not_paired(owner_id=owner_id, device_id=device_id, request_id=request_id)
        put_metric("relay.not_paired_selfheal", dimensions={"kind": kind, "outcome": "healed" if healed else "failed"})
    except Exception:  # noqa: BLE001
        put_metric("relay.not_paired_selfheal", dimensions={"kind": kind, "outcome": "error"})
        logger.warning("relay NOT_PAIRED self-heal failed kind=%s", kind, exc_info=True)
