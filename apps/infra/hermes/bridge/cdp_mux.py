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

Live frames (``cast``): while people who are not holding the one live-view (DCV) slot watch, a
run's tab is screencast at 480 px, at most FRAME_INTERVAL_S apart (latest frame wins, never a
queue), and handed to the run's sink, which publishes it to the room. The mux's own session on
that tab is invisible to the run's clients. ``dispatch`` sends a watcher's click or keys to it.
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
#: Frames: 480 px JPEG q50 measured ~10 KB and ~19% of the browser's one vCPU per cast (dev
#: 2026-09-27), so at most MAX_CASTS at once, and no faster than 5 a second.
CAST_PARAMS = {"format": "jpeg", "quality": 50, "maxWidth": 480, "maxHeight": 480}
MAX_CASTS = 2
FRAME_INTERVAL_S = 0.2
#: AppSync silently drops an event over ~240 KB (a 250 KB one returned 200 and never arrived).
MAX_FRAME_B64 = 150_000
#: Keys a watcher can press (windowsVirtualKeyCode, text); printable text goes as insertText.
KEYS = {"Enter": (13, "\r"), "Backspace": (8, ""), "Tab": (9, ""), "Escape": (27, ""), "Delete": (46, ""),
        "ArrowLeft": (37, ""), "ArrowUp": (38, ""), "ArrowRight": (39, ""), "ArrowDown": (40, ""),
        "Home": (36, ""), "End": (35, ""), "PageUp": (33, ""), "PageDown": (34, "")}

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
        self.want: dict = {}          # run key -> (sink, monotonic deadline): someone watches its frames
        self.casts: dict = {}         # run key -> _Cast, live screencasts
        self.cast_sessions: dict = {} # CDP sessionId -> _Cast (kept until detached, so nothing leaks)
        self.cast_attaching: dict = {}  # targetId -> _Cast awaiting attachedToTarget


class _Cast:
    def __init__(self, key: str, target: str) -> None:
        self.key, self.target, self.session, self.task = key, target, None, None
        self.latest = self.sent = None   # (jpeg base64, css width, css height)
        self.w = self.h = 0
        self.seq = 0
        self.resend = False
        self.changed = asyncio.Event()


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


def cast(run_id: str, sink, ttl: float, resend: bool = False) -> None:
    """Someone is watching this run's frames for ``ttl`` more seconds: screencast its tab to
    ``sink(frame)`` (a blocking publish, run off the loop). Fire and forget: never waits on CDP.
    ``resend`` republishes the latest frame, for a viewer who just arrived at a still page."""
    asyncio.run_coroutine_threadsafe(_cast(key_for(run_id), sink, ttl, resend), _ensure_loop())


def dispatch(run_id: str | None, event: dict) -> None:
    """A watcher's click/wheel/key/text on a cast tab (``run_id`` None: the only/first cast).
    Raises LookupError when that tab is not being cast."""
    _run(_dispatch(key_for(run_id) if run_id else None, event))


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
    for cast in S.casts.values():  # S.want stays: a run's next client re-creates its tab and cast
        if cast.task is not None:
            cast.task.cancel()
    S.casts.clear()
    S.cast_sessions.clear()
    S.cast_attaching.clear()
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


def _command(uid: int, method: str, params: dict | None, session: str | None) -> str:
    return json.dumps({"id": uid, "method": method, "params": params or {}, **({"sessionId": session} if session else {})})


async def _call(method: str, params: dict | None = None, session: str | None = None) -> dict:
    """A command of the mux's own, answered to nobody else."""
    uid = next(S.ids)
    future = asyncio.get_running_loop().create_future()
    S.pending[uid] = future
    await S.up.send(_command(uid, method, params, session))
    reply = await asyncio.wait_for(future, CALL_TIMEOUT_S)
    if "error" in reply:
        raise RuntimeError(reply["error"].get("message", "CDP error"))
    return reply.get("result") or {}


async def _send(method: str, params: dict | None = None, session: str | None = None) -> None:
    """Fire and forget: its reply matches no pending entry and is dropped."""
    if S.up is not None:
        with contextlib.suppress(Exception):
            await S.up.send(_command(next(S.ids), method, params, session))


# ---- live frames -------------------------------------------------------------------------------


async def _cast(key: str, sink, ttl: float, resend: bool) -> None:
    S.want[key] = (sink, asyncio.get_running_loop().time() + ttl)
    live = S.casts.get(key)
    if live is not None:
        live.resend = live.resend or resend
        live.changed.set()  # wakes the loop to read the new deadline
    else:
        await _start_cast(key)


async def _start_cast(key: str) -> None:
    tabs = S.tabs.get(key)
    if key in S.casts or key not in S.want or not tabs or S.up is None or len(S.casts) >= MAX_CASTS:
        return
    cast = S.casts[key] = _Cast(key, tabs[0])  # ponytail: the run's first tab, not whichever it last used
    S.cast_attaching[cast.target] = cast
    try:
        cast.session = (await _call("Target.attachToTarget", {"targetId": cast.target, "flatten": True}))["sessionId"]
        S.cast_sessions[cast.session] = cast
        # A tab that is not the window's front tab paints nothing without this (dev 2026-09-27).
        await _call("Emulation.setFocusEmulationEnabled", {"enabled": True}, cast.session)
        await _call("Page.startScreencast", CAST_PARAMS, cast.session)
    except Exception as exc:  # noqa: BLE001 -- the tab closed, or the connection went
        log.info("cdp mux: cast did not start (%s)", type(exc).__name__)
        S.cast_attaching.pop(cast.target, None)
        await _stop_cast(key)
        return
    cast.task = asyncio.ensure_future(_cast_loop(cast))


async def _stop_cast(key: str) -> None:
    cast = S.casts.pop(key, None)
    if cast is None:
        return
    if cast.task is not None and cast.task is not asyncio.current_task():
        cast.task.cancel()
    if cast.session:
        await _send("Target.detachFromTarget", {"sessionId": cast.session})  # ends the screencast too


def _on_cast_event(cast: _Cast, method: str, params: dict) -> None:
    if method != "Page.screencastFrame":
        return
    asyncio.ensure_future(_send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, cast.session))
    meta = params.get("metadata") or {}
    cast.latest = (params.get("data") or "", int(meta.get("deviceWidth") or 0), int(meta.get("deviceHeight") or 0))
    cast.changed.set()


async def _cast_loop(cast: _Cast) -> None:
    """Publish the latest frame, then wait out FRAME_INTERVAL_S: frames arriving meanwhile only
    replace ``latest``. Ends when nobody has asked for frames by the deadline."""
    loop = asyncio.get_running_loop()
    try:
        while True:
            sink, deadline = S.want.get(cast.key, (None, 0.0))
            if sink is None or loop.time() >= deadline:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(cast.changed.wait(), deadline - loop.time())
            cast.changed.clear()
            frame = cast.latest
            if frame is None or (frame is cast.sent and not cast.resend) or len(frame[0]) > MAX_FRAME_B64:
                continue
            cast.sent, cast.resend = frame, False
            cast.w, cast.h = frame[1], frame[2]
            cast.seq += 1
            started = loop.time()
            payload = {"seq": cast.seq, "jpegBase64": frame[0], "w": frame[1], "h": frame[2]}
            try:
                await loop.run_in_executor(None, sink, payload)
            except Exception:  # noqa: BLE001 -- a frame never fails anything
                log.info("cdp mux: frame publish failed", exc_info=True)
            await asyncio.sleep(max(0.0, FRAME_INTERVAL_S - (loop.time() - started)))
    finally:
        if S.casts.get(cast.key) is cast:
            await _stop_cast(cast.key)


async def _dispatch(key: str | None, event: dict) -> None:
    cast = S.casts.get(key) if key else next(iter(S.casts.values()), None)
    if cast is None or not cast.session:
        raise LookupError("that tab is not being shown")
    x = min(max(float(event.get("x") or 0), 0.0), float(cast.w or 0) or 1e4)
    y = min(max(float(event.get("y") or 0), 0.0), float(cast.h or 0) or 1e4)
    kind, session = event.get("type"), cast.session
    if kind == "click":
        button = event.get("button") if event.get("button") in ("left", "middle", "right") else "left"
        await _call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
        for phase in ("mousePressed", "mouseReleased"):
            await _call("Input.dispatchMouseEvent", {"type": phase, "x": x, "y": y, "button": button, "clickCount": 1}, session)
    elif kind == "wheel":
        delta = min(max(float(event.get("deltaY") or 0), -5000.0), 5000.0)
        await _call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta}, session)
    elif kind == "text":
        await _call("Input.insertText", {"text": str(event.get("text") or "")[:1000]}, session)
    elif kind == "key" and event.get("key") in KEYS:
        name = event["key"]
        code, text = KEYS[name]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": name, "windowsVirtualKeyCode": code}
        await _call("Input.dispatchKeyEvent", {**down, **({"text": text} if text else {})}, session)
        await _call("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": code}, session)
    else:
        raise ValueError("unsupported input")


async def _close_run(key: str) -> None:
    S.runs.discard(key)
    S.want.pop(key, None)
    await _stop_cast(key)
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
        asyncio.ensure_future(_start_cast(key))  # someone was already watching before the run's first client
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
                if outer in S.cast_sessions:  # the mux's own screencast session: nobody else's business
                    _on_cast_event(S.cast_sessions[outer], method, params)
                    continue
                if method == "Target.attachedToTarget" and not outer:
                    cast = S.cast_attaching.pop((params.get("targetInfo") or {}).get("targetId"), None)
                    if cast is not None:
                        cast.session = params.get("sessionId")
                        S.cast_sessions[cast.session] = cast
                        continue
                if method == "Target.detachedFromTarget" and (gone := S.cast_sessions.pop(params.get("sessionId"), None)):
                    if S.casts.get(gone.key) is gone:  # its tab closed under it: the next cast() picks another
                        S.casts.pop(gone.key)
                        if gone.task is not None:
                            gone.task.cancel()
                    continue
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
