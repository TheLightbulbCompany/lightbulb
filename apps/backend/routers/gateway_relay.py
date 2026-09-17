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
from core.services import desktop_pair, machine_auth, node_broker, relay_hooks, runtime_transport
from core.services.connection_service import ConnectionService
from core.services.container_rpc import GATEWAY_PORT
from core.services.container_wake import request_wake_silent
from core.services.desktop_pair_live import heal_desktop_device_not_paired

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024
_PARSE_LIMIT = 64 * 1024
_REFRESH_SECONDS = 60

# Live AgentCore node sockets, keyed by (owner_id, device_id) -- a paired Mac holds TWO at
# once (the Tauri device client `openclaw-macos` and the CLI `node-host`), each declaring a
# disjoint command set (desktop_pair.DESKTOP_MAC_COMMANDS / DESKTOP_CLI_COMMANDS never
# overlap), so keying by owner_id alone would let the second connect evict the first's
# session (fix round 1, item 2). Task 28's route (the in-process invoke door) reaches a
# session through `find_node_session` below, never this dict directly. Populated/cleared
# around each socket's life in `_serve_node_protocol`.
node_sessions: dict[tuple[str, str], node_broker.NodeSession] = {}


def find_node_session(owner_id: str, *, command: str | None = None) -> node_broker.NodeSession | None:
    """The live session for this owner that declares `command` (or, with no `command`,
    any live session for the owner). Returns None when no such session is attached."""
    for (session_owner_id, _device_id), session in node_sessions.items():
        if session_owner_id != owner_id:
            continue
        if command is None or command in session.commands:
            return session
    return None


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


def _pick_close_code(done: set[asyncio.Task], refresher: asyncio.Task | None) -> int | None:
    """Decide the close-code override from one asyncio.wait(FIRST_COMPLETED)
    batch. Revoke (1008) wins over a leg failure (1011) when both land in the
    SAME batch — presence loss is the stronger, more specific signal, and a
    revoked client should never see "internal error" instead of "revoked".
    Returns None when nothing in `done` calls for an override.
    """
    if refresher is not None and refresher in done:
        return 1008
    for task in done:
        if task is not refresher and not task.cancelled() and task.exception() is not None:
            return 1011
    return None


async def _refresh_presence_loop(connections: ConnectionService, connection_id: str) -> None:
    """60s presence-refresh tick, shared by both transports' relay loop (M2-R1: the
    AgentCore branch keeps this running exactly as this loop does today). Ending this task
    (the row was revoked) is the signal that closes the socket 1008."""
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


def _parse(frame: str, *, limit: int = _PARSE_LIMIT) -> dict | None:
    # Cheap char-count pre-check before the (allocating) UTF-8 encode: a
    # frame already over the limit in characters is over it in bytes too
    # (encode() only ever grows byte count relative to char count), so this
    # short-circuits the copy for the common case of a wildly oversized frame.
    #
    # `limit` defaults to the 64 KiB ECS inspection-peek cap -- the node reader below passes
    # node_broker.MAX_PAYLOAD instead (fix round 1, item 3): a node.invoke.result can legally
    # carry a large tool result, and the 64 KiB cap silently dropped anything bigger (no ack,
    # the caller's future never resolves, and an unanswered req suppresses the CLI's own tick
    # watchdog).
    if len(frame) > limit or len(frame.encode()) > limit:
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
    except Exception:  # noqa: BLE001
        # Fail CLOSED, not best-effort: a connection with no presence row is
        # invisible to count_relay_nodes() (the first-node deny-list patch
        # never fires) and to revoke (nothing to delete, nothing to notice
        # gone). The client's own reconnect loop retries.
        logger.warning("relay presence write failed kind=%s", kind, exc_info=True)
        put_metric("relay.presence", dimensions={"outcome": "error"})
        await websocket.close(code=1011, reason="Internal error")
        return

    close_code = 1000
    node_ready = False
    not_paired_healed = False
    legs: set[asyncio.Task] = set()
    refresher: asyncio.Task | None = None
    try:
        if kind == "node" and runtime_transport.host_for(container) == "agentcore":
            # No second socket: the BACKEND is the gateway for this workspace, terminating
            # the node protocol itself (spec §4.4) instead of dialling a container that does
            # not exist. `_serve_node_protocol` owns the connection end to end (including its
            # own accept(), presence-refresh leg, and on_node_ready/on_node_closed calls), so
            # none of `node_ready`/`legs`/`refresher` below ever get set on this path — the
            # shared `finally` skips the ECS-shaped on_node_closed dispatch as a result.
            #
            # Fix round 1, item 1 (critical): do NOT `await websocket.accept()` here too --
            # starlette raises RuntimeError on a second accept() once the socket is already
            # CONNECTED, which used to close 1011 before the challenge frame ever went out.
            put_metric("relay.connect", dimensions={"kind": kind, "outcome": "ok"})
            close_code = await _serve_node_protocol(
                websocket, identity, container, connections=connections, connection_id=connection_id
            )
        else:
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
                                    await relay_hooks.on_node_ready(
                                        owner_id, identity.user_id, identity.device_id or ""
                                    )
                                except Exception:  # noqa: BLE001 — a hook failure must not kill the leg
                                    logger.warning("relay on_node_ready hook failed owner=%s", owner_id, exc_info=True)
                    except (WebSocketDisconnect, ConnectionClosed):
                        return

                legs = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
                refresher = asyncio.create_task(_refresh_presence_loop(connections, connection_id))
                done, pending = await asyncio.wait(legs | {refresher}, return_when=asyncio.FIRST_COMPLETED)
                decided = _pick_close_code(done, refresher)
                if decided is not None:
                    close_code = decided
                for task in done:
                    if task is refresher or task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        logger.warning("relay leg failed kind=%s", kind, exc_info=exc)
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
        delete_failed = False
        try:
            await asyncio.to_thread(connections.delete_connection, connection_id)
        except Exception:  # noqa: BLE001
            delete_failed = True
            logger.warning("relay presence delete failed kind=%s", kind, exc_info=True)
            put_metric("relay.presence", dimensions={"outcome": "delete_error"})
        if kind == "node" and node_ready:
            try:
                # This row may still be sitting in DDB if the delete above
                # failed — tell the hook so its last-node count doesn't count
                # the connection that is, in fact, closing right now.
                await relay_hooks.on_node_closed(owner_id, identity.user_id, stale_row_undeleted=delete_failed)
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


async def _serve_node_protocol(
    websocket: WebSocket,
    identity: machine_auth.MachineIdentity,
    container: dict,
    *,
    connections: ConnectionService | None = None,
    connection_id: str | None = None,
) -> int:
    """Server-first node protocol, terminated in the backend for an AgentCore workspace:
    send the challenge, validate the client's `connect`, answer `hello-ok`, register the
    session, then pump frames for the socket's life. No second socket — the backend IS the
    gateway for this workspace (spec §4.4). Returns the close code.

    ``connections``/``connection_id`` are optional so a unit test can drive this function
    directly without a real presence row; the router always passes both.
    """
    await websocket.accept()
    challenge = node_broker.challenge_frame()
    await websocket.send_text(json.dumps(challenge))
    try:
        frame = await asyncio.wait_for(websocket.receive_text(), timeout=node_broker.CHALLENGE_DEADLINE_S)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return 1008
    body = _parse(frame) or {}
    if body.get("method") != "connect" or body.get("type") != "req":
        return 1008
    params = body.get("params") or {}
    if not node_broker.signature_matches(params, challenge["payload"]["nonce"]):
        # The GATE is machine_auth at the upgrade; this only catches a client that does not
        # hold the key it claims, which is otherwise a silent mismatch.
        logger.warning("relay node connect signature mismatch owner=%s", identity.owner_id)
        return 1008
    declared = set(params.get("commands") or [])
    # The declared set is intersected with the owner's backend-written paired record: a
    # client that declares more than the owner approved gets the intersection, never the
    # union (node_broker.CommandNotAllowed is the enforcement at invoke time).
    device_id = identity.device_id or ""
    allowed = declared & await desktop_pair.declared_commands_for(identity.owner_id, device_id)
    session = node_broker.NodeSession(device_id, allowed)
    key = (identity.owner_id, device_id)
    node_sessions[key] = session
    await websocket.send_text(json.dumps(node_broker.hello_ok(str(body.get("id")), str(uuid.uuid4()))))
    try:
        await relay_hooks.on_node_ready(identity.owner_id, identity.user_id, device_id)
    except Exception:  # noqa: BLE001 — a hook failure must not kill the leg
        logger.warning("relay on_node_ready hook failed owner=%s", identity.owner_id, exc_info=True)
    refresher = (
        asyncio.create_task(_refresh_presence_loop(connections, connection_id))
        if connections is not None and connection_id is not None
        else None
    )
    try:
        return await _pump(websocket, session, refresher)
    finally:
        if refresher is not None and not refresher.done():
            refresher.cancel()
        # The socket is gone -- nothing will ever answer an invoke still in flight. Fail it
        # NOW rather than letting a caller (Task 28) wait out its own timeout_ms for an
        # answer that can never arrive (fix round 1, item 4).
        session.fail_pending(node_broker.NodeDetached(f"node socket closed for owner={identity.owner_id}"))
        # Only pop THIS socket's own entry -- a second device for the same owner may have
        # registered its own (owner_id, device_id) key since, and must not be evicted here.
        if node_sessions.get(key) is session:
            node_sessions.pop(key, None)
        try:
            await relay_hooks.on_node_closed(identity.owner_id, identity.user_id)
        except Exception:  # noqa: BLE001
            logger.warning("relay node-closed hook failed owner=%s", identity.owner_id, exc_info=True)


class _FrameTooLarge(Exception):
    """Raised by the node reader leg when a client frame exceeds node_broker.MAX_PAYLOAD --
    `_pump` maps this to close code 1009 (message too big) rather than the generic 1011."""


async def _pump(websocket: WebSocket, session: node_broker.NodeSession, refresher: asyncio.Task | None) -> int:
    """Three legs plus the presence refresher when one is wired: read+ack, drain outbound
    invokes, and tick. The tick is not optional — the CLI closes 4000 at twice the interval
    we promised in hello-ok. A refresher ending (presence row revoked) closes 1008, same as
    the ECS relay's own `_pick_close_code`."""

    async def reader() -> None:
        try:
            while True:
                frame = await websocket.receive_text()
                # The node protocol's own ceiling (25 MiB -- the real gateway's value;
                # screen snapshots and large tool results need it), NOT the 64 KiB ECS
                # inspection-peek cap: a node.invoke.result over 64 KiB used to be silently
                # dropped by `_parse`'s default limit (no ack, the caller's future never
                # resolved, and an unanswered req suppresses the CLI's own tick watchdog --
                # fix round 1, item 3).
                if len(frame) > node_broker.MAX_PAYLOAD or len(frame.encode()) > node_broker.MAX_PAYLOAD:
                    raise _FrameTooLarge(len(frame))
                answer = session.resolve(_parse(frame, limit=node_broker.MAX_PAYLOAD) or {})
                if answer is not None:
                    await websocket.send_text(json.dumps(answer))
        except (WebSocketDisconnect, ConnectionClosed):
            return

    async def writer() -> None:
        try:
            while True:
                await websocket.send_text(json.dumps(await session.send.get()))
        except (WebSocketDisconnect, ConnectionClosed):
            return

    async def ticker() -> None:
        try:
            while True:
                await asyncio.sleep(node_broker.TICK_INTERVAL_MS / 1000)
                await websocket.send_text(json.dumps(node_broker.tick_frame()))
        except (WebSocketDisconnect, ConnectionClosed):
            return

    legs = {asyncio.create_task(leg()) for leg in (reader, writer, ticker)}
    waitable = legs | ({refresher} if refresher is not None else set())
    done: set[asyncio.Task] = set()
    pending: set[asyncio.Task] = set(waitable)
    try:
        done, pending = await asyncio.wait(waitable, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # asyncio.wait does NOT cancel its own awaitables -- if THIS coroutine is itself
        # cancelled while blocked on the wait above (a shutdown/drain interrupting the
        # connection, not a leg finishing on its own), the wait call never returns and the
        # cancel+await below would never run if it lived after it instead of in this
        # `finally`. `pending` still holds every leg (none observed as done yet) in that
        # case, so this unconditionally cancels and awaits all of them either way
        # (fix round 1, item 6).
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waitable, return_exceptions=True)

    # Reuses the ECS relay's own `_pick_close_code` (revoke wins over any same-batch leg
    # failure) and its "relay leg failed" logging pattern; a `_FrameTooLarge` refines the
    # generic 1011 down to the more specific 1009, but never overrides a 1008 revoke.
    close_code = _pick_close_code(done, refresher) or 1000
    for task in done:
        if task is refresher or task.cancelled():
            continue
        exc = task.exception()
        if exc is None:
            continue
        if isinstance(exc, _FrameTooLarge) and close_code != 1008:
            close_code = 1009
        elif close_code == 1011:
            logger.warning("relay leg failed kind=node", exc_info=exc)
    return close_code
