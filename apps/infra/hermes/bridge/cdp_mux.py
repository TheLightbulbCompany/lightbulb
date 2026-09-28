"""One AWS automation connection, shared by every run in this microVM.

AWS admits ONE CDP connection per browser session (a second connect gets HTTP 429), and each run
brings its own clients: agent-browser's daemon, Hermes's CDP supervisor (dialogs, frames, the
vault fill) and browser_cdp. This loopback server gives each run its own endpoint
(``url_for(run_id)``) and multiplexes them all onto one upstream WebSocket:

* every command gets a fresh upstream id, and its response goes back only to the client that sent it;
* ``Target.attachedToTarget`` goes only to the client that attached (else the owners of the session
  it arrived on), and session-scoped events only to the clients using that session;
* each run gets its own tab, and ``Target.getTargets`` lists the run's tabs first, so a client that
  picks "the first page" (agent-browser, the supervisor) lands on its own run's tab;
* Hermes's XHR dialog bridge (``http://hermes-dialog-bridge.invalid``) is answered here and never
  installed: it is mixed-content blocked on https pages, so the native
  ``Page.javascriptDialogOpening`` + ``Page.handleJavaScriptDialog`` path is what works.

Nothing else is filtered: agents may send any method to any tab (owner ruling 2026-09-27).
Everything below runs on this module's own event loop thread; the public functions are the only
entry points from other threads.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import threading

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

log = logging.getLogger("bridge.cdp_mux")

PORT = 9222
PREFIX = "/devtools/browser/"
DIALOG_BRIDGE = "hermes-dialog-bridge.invalid"
CALL_TIMEOUT_S = 15.0

_loop: asyncio.AbstractEventLoop | None = None
_start_lock = threading.Lock()


class _State:
    def __init__(self) -> None:
        self.url = ""                 # latest signed automation URL (reconnects use it)
        self.generation = None
        self.up = None                # the one upstream WebSocket
        self.up_lock = asyncio.Lock()
        self.ids = itertools.count(1)
        self.pending: dict = {}       # upstream id -> (client, client id, method) | Future
        self.clients: dict = {}       # client -> run key
        self.sessions: dict = {}      # CDP sessionId -> set(client)
        self.attaching: dict = {}     # targetId -> [client, ...] awaiting attachedToTarget
        self.auto: set = set()        # clients that asked for browser-level auto-attach
        self.tabs: dict = {}          # run key -> [targetId, ...]
        self.runs: set = set()        # run keys whose endpoint is live


S = _State()


def key_for(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:24]


# ---- thread-safe entry points --------------------------------------------------------------


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _start_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            async def main() -> None:
                async with serve(_serve_client, "127.0.0.1", PORT, max_size=None):
                    ready.set()
                    await asyncio.Future()

            threading.Thread(target=loop.run_until_complete, args=(main(),), name="cdp-mux", daemon=True).start()
            if not ready.wait(10):
                raise RuntimeError("cdp mux did not start")
            _loop = loop
    return _loop


def _run(coro, timeout: float = CALL_TIMEOUT_S):
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result(timeout)


def url_for(run_id: str) -> str:
    """This run's loopback CDP endpoint (registers the run)."""
    key = key_for(run_id)
    _run(_register(key))
    return f"ws://127.0.0.1:{PORT}{PREFIX}{key}"


def set_upstream(url: str, generation) -> None:
    """The backend's latest signed URL. A new generation is a new session (or a revoked
    connection): drop the old upstream and every client on it."""
    _run(_set_upstream(url, generation))


def close_run(run_id: str) -> None:
    """The run ended: close its tabs and its clients (agent-browser's `close` leaves tabs open)."""
    if _loop is not None:
        _run(_close_run(key_for(run_id)))


def disconnect() -> None:
    """Give the automation connection back (the backend's save needs it)."""
    if _loop is not None:
        _run(_drop_upstream())


# ---- loop-side ---------------------------------------------------------------------------------


async def _register(key: str) -> None:
    S.runs.add(key)


async def _set_upstream(url: str, generation) -> None:
    changed = S.generation is not None and generation != S.generation
    S.url, S.generation = url, generation
    if changed:
        await _drop_upstream()


async def _drop_upstream() -> None:
    up, S.up = S.up, None
    if up is not None:
        with contextlib.suppress(Exception):
            await up.close()
    _reset()


def _reset() -> None:
    for entry in S.pending.values():
        if isinstance(entry, asyncio.Future) and not entry.done():
            entry.set_exception(ConnectionError("browser connection closed"))
    S.pending.clear()
    S.sessions.clear()
    S.attaching.clear()
    S.auto.clear()
    S.tabs.clear()  # a new upstream is a new session: those tabs are gone with it
    for client in list(S.clients):
        asyncio.ensure_future(client.close())
    S.clients.clear()


async def _ensure_up() -> None:
    async with S.up_lock:
        if S.up is not None:
            return
        if not S.url:
            raise ConnectionError("no browser session")
        S.up = await connect(S.url, max_size=None, open_timeout=10)
        asyncio.ensure_future(_pump(S.up))


async def _call(method: str, params: dict | None = None) -> dict:
    """A command of the mux's own, answered to nobody else."""
    uid = next(S.ids)
    future = asyncio.get_running_loop().create_future()
    S.pending[uid] = future
    await S.up.send(json.dumps({"id": uid, "method": method, "params": params or {}}))
    reply = await asyncio.wait_for(future, CALL_TIMEOUT_S)
    if "error" in reply:
        raise RuntimeError(reply["error"].get("message", "CDP error"))
    return reply.get("result") or {}


async def _close_run(key: str) -> None:
    S.runs.discard(key)
    for target in S.tabs.pop(key, []):
        if S.up is not None:
            uid = next(S.ids)  # fire and forget: nobody waits on a closing tab
            with contextlib.suppress(Exception):
                await S.up.send(json.dumps({"id": uid, "method": "Target.closeTarget", "params": {"targetId": target}}))
    for client, owner in list(S.clients.items()):
        if owner == key:
            _forget(client)
            with contextlib.suppress(Exception):
                await client.close()


def _forget(client) -> None:
    S.clients.pop(client, None)
    S.auto.discard(client)
    for owners in S.sessions.values():
        owners.discard(client)
    for waiting in S.attaching.values():
        while client in waiting:
            waiting.remove(client)


async def _serve_client(client) -> None:
    path = client.request.path
    key = path[len(PREFIX):] if path.startswith(PREFIX) else ""
    if key not in S.runs:
        await client.close(4404, "unknown run")
        return
    try:
        await _ensure_up()
        async with S.up_lock:
            if not S.tabs.get(key):  # the run's own tab: what "the first page" means for its clients
                S.tabs[key] = [(await _call("Target.createTarget", {"url": "about:blank"}))["targetId"]]
    except Exception as exc:  # noqa: BLE001 -- no URL in the log: it is a signed capability
        log.warning("cdp mux: upstream unavailable (%s)", type(exc).__name__)
        await client.close(1011, "browser unavailable")
        return
    S.clients[client] = key
    try:
        async for raw in client:
            await _from_client(client, raw)
    except Exception:  # noqa: BLE001 -- a client dropping mid-send is routine
        pass
    finally:
        _forget(client)


async def _from_client(client, raw) -> None:
    message = json.loads(raw)
    session = message.get("sessionId")
    if DIALOG_BRIDGE in raw:
        await client.send(json.dumps({"id": message["id"], "result": {"identifier": "0"},
                                      **({"sessionId": session} if session else {})}))
        return
    method, params = message.get("method"), message.get("params") or {}
    if session:
        S.sessions.setdefault(session, set()).add(client)
    if method == "Target.attachToTarget":
        S.attaching.setdefault(params.get("targetId"), []).append(client)
    if method == "Target.setAutoAttach" and not session:
        S.auto.add(client)
    if S.up is None:
        await client.close(1011, "browser connection closed")
        return
    uid = next(S.ids)
    S.pending[uid] = (client, message["id"], method)
    message["id"] = uid
    await S.up.send(json.dumps(message))


def _first_mine(key: str, infos: list) -> list:
    mine = S.tabs.get(key, [])
    return sorted(infos, key=lambda t: mine.index(t.get("targetId")) if t.get("targetId") in mine else len(mine))


async def _pump(up) -> None:
    try:
        async for raw in up:
            message = json.loads(raw)
            if "id" in message:
                entry = S.pending.pop(message["id"], None)
                if isinstance(entry, asyncio.Future):
                    if not entry.done():
                        entry.set_result(message)
                    continue
                if entry is None:
                    continue
                client, client_id, method = entry
                key, result = S.clients.get(client), message.get("result") or {}
                if key and method == "Target.createTarget" and result.get("targetId"):
                    S.tabs.setdefault(key, []).append(result["targetId"])
                if key and method == "Target.getTargets" and isinstance(result.get("targetInfos"), list):
                    result["targetInfos"] = _first_mine(key, result["targetInfos"])
                if result.get("sessionId"):
                    S.sessions.setdefault(result["sessionId"], set()).add(client)
                message["id"] = client_id
                targets = {client}
            else:
                method, params, outer = message.get("method"), message.get("params") or {}, message.get("sessionId")
                if method == "Target.attachedToTarget":
                    waiting = S.attaching.get((params.get("targetInfo") or {}).get("targetId")) or []
                    if waiting:
                        owners = {waiting.pop(0)}
                    elif outer:
                        owners = set(S.sessions.get(outer, ()))
                    else:
                        owners = set(S.auto or S.clients)
                    S.sessions[params.get("sessionId")] = owners
                    targets = owners
                elif outer:
                    targets = S.sessions.get(outer) or set(S.clients)
                else:
                    targets = set(S.clients)
                if method == "Target.targetDestroyed":
                    for tabs in S.tabs.values():
                        with contextlib.suppress(ValueError):
                            tabs.remove(params.get("targetId"))
                if method == "Target.detachedFromTarget":
                    S.sessions.pop(params.get("sessionId"), None)
            text = json.dumps(message)
            for client in list(targets):
                if client in S.clients:
                    with contextlib.suppress(Exception):
                        await client.send(text)
    except Exception:  # noqa: BLE001 -- AWS closing or revoking the stream
        pass
    finally:
        if S.up is up:
            S.up = None
            _reset()
