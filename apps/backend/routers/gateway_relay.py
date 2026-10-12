"""Raw WebSocket door for a desktop node host.

The BACKEND is the gateway for this workspace (spec §4.4): ``_serve_node_protocol`` terminates
the node protocol in-process -- challenge, signed ``connect``, ``hello-ok``, then frame pumping
for the socket's life. Nothing about the connection lives anywhere else except a TTL presence
row.

There is no second socket and no operator leg. A Paperclip operator used to dial here and be
piped to the container's OpenClaw gateway; its adapter is now Paperclip's native
``hermes_gateway`` over the authenticated HTTP facade, so an operator token arriving here is
refused rather than piped to a runtime that has no WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services import desktop_pair, machine_auth, node_broker, relay_hooks, session_browser
from core.services.connection_service import ConnectionService

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


async def _refresh_presence_loop(
    connections: ConnectionService,
    connection_id: str,
    owner_id: str,
    device_id: str,
    generation: str,
) -> None:
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
        if not await connections.refresh_desktop_ready(owner_id, device_id, generation):
            put_metric("relay.presence", dimensions={"outcome": "superseded"})
            return


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

    if kind != "node":
        # Only a node terminates here. Every other machine client (the Paperclip operator) is
        # on the HTTP facade now; piping it to a runtime with no WebSocket would hang.
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": "unsupported"})
        await websocket.close(code=1008, reason="Unsupported client")
        return

    try:
        container = await container_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("relay workspace read error owner=%s", owner_id, exc_info=True)
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": "error"})
        await websocket.close(code=1011, reason="Internal error")
        return
    if not container:
        # RULING D14: a node's own reconnect (a laptop coming back from sleep) never started a
        # workspace and must not now. There is nothing to wake either -- AgentCore starts the
        # microVM on invoke -- so an unprovisioned owner is simply refused.
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": "no_workspace"})
        await websocket.close(code=1013, reason="Workspace not provisioned")
        return

    connections = ConnectionService()
    connection_id = f"relay#{uuid.uuid4()}"
    try:
        await connections.put_relay_presence(connection_id=connection_id, identity=identity)
    except Exception:  # noqa: BLE001
        # Fail CLOSED, not best-effort: a connection with no presence row is invisible to
        # revoke (nothing to delete, nothing to notice gone). The client's own reconnect loop
        # retries.
        logger.warning("relay presence write failed kind=%s", kind, exc_info=True)
        put_metric("relay.presence", dimensions={"outcome": "error"})
        await websocket.close(code=1011, reason="Internal error")
        return

    close_code = 1000
    try:
        # `_serve_node_protocol` owns the connection end to end -- its own accept(), its
        # presence-refresh leg, and its on_node_ready/on_node_closed calls. Do NOT accept()
        # here too: starlette raises RuntimeError on a second accept() once the socket is
        # CONNECTED, which used to close 1011 before the challenge frame ever went out.
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": "ok"})
        close_code = await _serve_node_protocol(
            websocket, identity, container, connections=connections, connection_id=connection_id
        )
    except Exception:  # noqa: BLE001 — never let a relay error take the worker down
        logger.warning("relay error kind=%s", kind, exc_info=True)
        close_code = 1011
    finally:
        try:
            await asyncio.to_thread(connections.delete_connection, connection_id)
        except Exception:  # noqa: BLE001
            logger.warning("relay presence delete failed kind=%s", kind, exc_info=True)
            put_metric("relay.presence", dimensions={"outcome": "delete_error"})
        put_metric("relay.close", dimensions={"kind": kind, "code": _close_code_bucket(close_code)})
        try:
            await websocket.close(code=close_code)
        except Exception:  # noqa: BLE001 — already closed by the client
            pass


def _reject_node_connect(owner_id: str, reason: str) -> int:
    """Every early-exit 1008 in the node handshake goes through here so the reason is
    visible in CloudWatch (fix round 1, item 7) instead of only in a warning log line."""
    put_metric("relay.node_protocol", dimensions={"outcome": "rejected", "reason": reason})
    logger.info("relay node connect rejected owner=%s reason=%s", owner_id, reason)
    return 1008


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
        return _reject_node_connect(identity.owner_id, "challenge_timeout")
    body = _parse(frame) or {}
    if body.get("method") != "connect" or body.get("type") != "req":
        return _reject_node_connect(identity.owner_id, "bad_connect")
    params = body.get("params") or {}
    min_protocol, max_protocol = params.get("minProtocol"), params.get("maxProtocol")
    if not (
        isinstance(min_protocol, int)
        and isinstance(max_protocol, int)
        and min_protocol <= node_broker.PROTOCOL_VERSION <= max_protocol
    ):
        return _reject_node_connect(identity.owner_id, "protocol_mismatch")
    if not node_broker.signature_matches(params, challenge["payload"]["nonce"]):
        # The GATE is machine_auth at the upgrade; this only catches a client that does not
        # hold the key it claims, which is otherwise a silent mismatch.
        logger.warning("relay node connect signature mismatch owner=%s", identity.owner_id)
        return _reject_node_connect(identity.owner_id, "bad_signature")
    declared = set(params.get("commands") or [])
    # The declared set is intersected with the owner's backend-written paired record: a
    # client that declares more than the owner approved gets the intersection, never the
    # union (node_broker.CommandNotAllowed is the enforcement at invoke time).
    device_id = identity.device_id or ""
    allowed = declared & await desktop_pair.declared_commands_for(identity.owner_id, device_id)
    session = node_broker.NodeSession(device_id, allowed)
    # A shared Claude Code or Codex session on this Mac asks for a browser.
    session.on_offer = functools.partial(session_browser.start, identity.owner_id, identity.user_id, device_id, session)
    key = (identity.owner_id, device_id)
    # Registered only once hello-ok has actually gone out -- a send failure right here must
    # not leave a session live that the client was never told is ready (fix round 1, item 7).
    generation = str(uuid.uuid4())
    await websocket.send_text(json.dumps(node_broker.hello_ok(str(body.get("id")), generation)))
    if connections is not None:
        await connections.publish_desktop_ready(identity=identity, generation=generation, commands=allowed)
    session.generation = generation
    node_sessions[key] = session
    refresher = None
    try:
        try:
            await relay_hooks.on_node_ready(identity.owner_id, identity.user_id)
        except Exception:  # noqa: BLE001 — a hook failure must not kill the leg
            logger.warning("relay on_node_ready hook failed owner=%s", identity.owner_id, exc_info=True)
        refresher = (
            asyncio.create_task(
                _refresh_presence_loop(connections, connection_id, identity.owner_id, device_id, generation)
            )
            if connections is not None and connection_id is not None
            else None
        )
        return await _pump(websocket, session, refresher)
    finally:
        # Defensive belt-and-suspenders: `_pump`'s own finally already cancels-and-awaits
        # the refresher as part of `waitable` on every path (normal return or a cancellation
        # of `_pump` itself), so this should always find it already done. Awaiting after
        # cancel here too (fix round 1, item 7) means that stays true even if a future change
        # to `_pump` ever returns without draining it.
        if refresher is not None and not refresher.done():
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
        # The socket is gone -- nothing will ever answer an invoke still in flight. Fail it
        # NOW rather than letting a caller (Task 28) wait out its own timeout_ms for an
        # answer that can never arrive (fix round 1, item 4).
        session.fail_pending(node_broker.NodeDetached(f"node socket closed for owner={identity.owner_id}"))
        # Only pop THIS socket's own entry -- a second device for the same owner may have
        # registered its own (owner_id, device_id) key since, and must not be evicted here.
        if node_sessions.get(key) is session:
            node_sessions.pop(key, None)
        if connections is not None:
            try:
                await connections.delete_desktop_ready(identity.owner_id, device_id, generation)
            except Exception:  # noqa: BLE001 — TTL still expires a failed cleanup
                logger.warning("relay ready-presence delete failed owner=%s", identity.owner_id, exc_info=True)
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
    the same `_pick_close_code` the shared close-code decision uses."""

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
                await websocket.send_text(json.dumps(await session.next_outbound()))  # one writer, two lanes
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

    # Reuses `_pick_close_code` (revoke wins over any same-batch leg
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
