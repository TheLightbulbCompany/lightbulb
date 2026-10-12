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

Besides that one cloud (AWS) connection, the same loopback server multiplexes any number of named
upstreams the same way -- each with its own automation connection and tabs, completely isolated
from the cloud one and from each other (``url_for``/``set_upstream``/``close_run`` take an
``upstream`` name; ``CLOUD`` is the default). ``cast`` always targets CLOUD: the desktop app casts
a Mac's own live view itself, so the bridge never screencasts a named upstream's tab. A paired
Mac's raw CDP door is ``mac:<device>:<runId>``, one per (Mac, run); ``drop`` forgets one entirely
once it is done with.
A named upstream differs in three ways: its door's ``Lightbulb.humanInput`` events are swallowed
and recorded (``human_input_at``, the person typing in the Mac's agent window), its close code is
kept for the bridge (``last_close``), and a reconnect (the Mac slept) keeps each run's tabs, which
Chrome still has: the run's next client re-attaches to them when they still exist.

Nothing else is filtered: agents may send any method to any tab (owner ruling 2026-09-27).
One command is rewritten, on a named upstream only: ``DOM.setFileInputFiles`` naming files that
exist HERE (the agent's own PDF in $HERMES_HOME) needs Mac paths, so each such file is first
shipped to the Mac in ``Lightbulb.stageFile`` chunks and ``files`` is pointed at the Mac copies. A
desktop that predates staging answers "wasn't found" and the command goes on unchanged.

Live frames (``cast``): while people who are not holding the one live-view (DCV) slot watch, a
CLOUD run's tab is screencast at 480 px, at most FRAME_INTERVAL_S apart (latest frame wins, never
a queue), and handed to the run's sink, which publishes it to the room. The mux's own session on
that tab is invisible to the run's clients. ``dispatch`` sends a watcher's click or keys to that
same CLOUD cast. A Mac's live view is cast by the desktop app itself, not this module.
Everything below runs on this module's own event loop thread; the public functions are the only
entry points from other threads.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

log = logging.getLogger("bridge.cdp_mux")

PORT = 9222
PREFIX = "/devtools/browser/"
DIALOG_BRIDGE = "hermes-dialog-bridge.invalid"
CALL_TIMEOUT_S = 15.0
#: Frames: 480 px JPEG q50 measured ~10 KB and ~19% of the browser's one vCPU per cast (dev
#: 2026-09-27), so at most MAX_CASTS at once per upstream, and no faster than 5 a second.
CAST_PARAMS = {"format": "jpeg", "quality": 50, "maxWidth": 480, "maxHeight": 480}
MAX_CASTS = 2
#: Origins remembered per upstream between saves (visited_origins).
MAX_VISITED = 200
FRAME_INTERVAL_S = 0.2
#: AppSync silently drops an event over ~240 KB (a 250 KB one returned 200 and never arrived).
MAX_FRAME_B64 = 150_000
#: Scroll the nearest scrollable box under (x, y), else the page; numbers only are formatted in.
SCROLL_JS = ("(() => { let e = document.elementFromPoint(%f, %f); const dy = %f;"
             " while (e && !(e.scrollHeight > e.clientHeight && /auto|scroll/.test(getComputedStyle(e).overflowY)))"
             " e = e.parentElement; (e || document.scrollingElement).scrollBy(0, dy); })()")
#: Keys a watcher can press (windowsVirtualKeyCode, text); printable text goes as insertText.
KEYS = {"Enter": (13, "\r"), "Backspace": (8, ""), "Tab": (9, ""), "Escape": (27, ""), "Delete": (46, ""),
        "ArrowLeft": (37, ""), "ArrowUp": (38, ""), "ArrowRight": (39, ""), "ArrowDown": (40, ""),
        "Home": (36, ""), "End": (35, ""), "PageUp": (33, ""), "PageDown": (34, "")}
#: The one AWS automation connection every run shares. Named upstreams (paired Macs) are additional,
#: independent entries in ``_UPS`` alongside this one.
CLOUD = "cloud"
#: Raw bytes per ``Lightbulb.stageFile``: ~11 MB of base64, under the relay's 16 MiB CDP frame.
STAGE_CHUNK = 8 * 1024 * 1024
#: One setFileInputFiles' staged files, together (the desktop caps each file at the same).
MAX_STAGE_BYTES = 100 * 1024 * 1024
STAGE_TIMEOUT_S = 120.0

_loop: asyncio.AbstractEventLoop | None = None
_start_lock = threading.Lock()


class _State:
    def __init__(self, named: bool = False) -> None:
        self.named = named            # a paired Mac's door, not the cloud
        self.human_at = 0.0           # named: the last Lightbulb.humanInput (epoch s)
        self.closed = None            # named: the door's (code, reason) since the last connect
        self.stale: set = set()       # named: run keys whose kept tabs need checking after a reconnect
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
        self.visited: dict = {}       # http(s) origin -> landing number, oldest first
        self.landings = itertools.count(1)
        self.quiet: set = set()       # background targets (a save's own reads): never "visited"
        self.quiet_calls: set = set() # upstream ids of Target.createTarget {background: true}
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


#: One automation connection + its run/tab/cast bookkeeping per upstream name. CLOUD always exists;
#: named (Mac) upstreams are created on first ``url_for``/``set_upstream`` and forgotten by ``drop``.
_UPS: dict[str, _State] = {}
#: run key -> the upstream name it was registered on, so ``_serve_client`` can route a connection
#: to the right ``_State`` from the path alone.
_KEYS: dict[str, str] = {}


def _ups(upstream: str) -> _State:
    """Loop-side only: get or create the named upstream's state."""
    state = _UPS.get(upstream)
    if state is None:
        state = _UPS[upstream] = _State(named=upstream != CLOUD)
    return state


_ups(CLOUD)
S = _UPS[CLOUD]  # alias kept for the existing tests: cdp_mux.S.runs/want/generation/casts


def key_for(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:24]


def _key_for(run_id: str, upstream: str) -> str:
    """CLOUD keeps today's key (``key_for(run_id)``); any other upstream is namespaced by it, so
    the same run_id on two upstreams never collides."""
    return key_for(run_id) if upstream == CLOUD else key_for(upstream + "\x00" + run_id)


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


def url_for(run_id: str, upstream: str = CLOUD) -> str:
    """This run's loopback CDP endpoint on ``upstream`` (registers the run)."""
    key = _key_for(run_id, upstream)
    _run(_register(key, upstream))
    return f"ws://127.0.0.1:{PORT}{PREFIX}{key}"


def set_upstream(url: str, generation, upstream: str = CLOUD) -> None:
    """The backend's latest signed URL for ``upstream``. A new generation is a new session (or a
    revoked connection): drop the old upstream and every client on it."""
    _run(_set_upstream(url, generation, upstream))


def close_run(run_id: str, upstream: str | None = None) -> None:
    """The run ended: close its tabs and its clients on ``upstream`` (None: every upstream the run
    is registered on). agent-browser's `close` leaves tabs open."""
    if _loop is not None:
        _run(_close_run_everywhere(run_id, upstream))


def visited_origins(cdp_url: str | None = None) -> dict:
    """The http(s) origins pages have landed on over the upstream a checkpoint reads (``cdp_url``
    a run's loopback endpoint: its upstream; None: the cloud), each with its latest landing's
    number. A save reads localStorage from open pages, so an origin a run navigated away from,
    or whose tab closed, needs its own read."""
    return _run(_visited(_upstream_of(cdp_url))) if _loop is not None else {}


def forget_origins(cdp_url: str | None, seen: dict) -> None:
    """``seen`` (origin -> landing number, as visited_origins answered) is saved: drop each
    origin unless a page landed there again since, which a later save must read."""
    if _loop is not None and seen:
        _run(_forget_origins(_upstream_of(cdp_url), dict(seen)))


def _upstream_of(cdp_url: str | None) -> str:
    key = cdp_url.rsplit("/", 1)[-1] if cdp_url else ""
    return _KEYS.get(key, CLOUD) if key else CLOUD


async def _visited(upstream: str) -> dict:
    state = _UPS.get(upstream)
    return dict(state.visited) if state is not None else {}


async def _forget_origins(upstream: str, seen: dict) -> None:
    state = _UPS.get(upstream)
    if state is not None:
        for origin, landing in seen.items():
            if state.visited.get(origin) == landing:
                state.visited.pop(origin)


def _landed(state: _State, url, target) -> None:
    """A page (not a save's background read) landed on ``url``: remember its origin."""
    if not isinstance(url, str) or target in state.quiet:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return
    origin = f"{parsed.scheme}://{parsed.netloc}"
    state.visited.pop(origin, None)
    state.visited[origin] = next(state.landings)  # most recent last
    # ponytail: MAX_VISITED origins per upstream, oldest dropped (its localStorage then waits
    # for its next visit); a per-origin budget or a persistent store if sessions outgrow it.
    while len(state.visited) > MAX_VISITED:
        state.visited.pop(next(iter(state.visited)))


def cast(run_id: str, sink, ttl: float, resend: bool = False) -> None:
    """Someone is watching this CLOUD run's frames for ``ttl`` more seconds: screencast its tab to
    ``sink(frame)`` (a blocking publish, run off the loop). Fire and forget: never waits on CDP.
    ``resend`` republishes the latest frame, for a viewer who just arrived at a still page. Always
    CLOUD: the desktop app casts a Mac's own live view itself, not the bridge."""
    key = _key_for(run_id, CLOUD)
    asyncio.run_coroutine_threadsafe(_cast(CLOUD, key, sink, ttl, resend), _ensure_loop())


def dispatch(run_id: str | None, event: dict) -> None:
    """A watcher's click/wheel/key/text on a CLOUD cast tab (``run_id`` None: the only/first cast).
    Only CLOUD runs are ever cast here -- the desktop app casts a Mac's own live view itself, so
    there is no named-upstream cast to dispatch input to. Raises LookupError when that tab is not
    being cast."""
    _run(_dispatch(S, key_for(run_id) if run_id else None, event))


def disconnect(upstream: str = CLOUD) -> None:
    """Give ``upstream``'s automation connection back (the backend's save needs it)."""
    if _loop is not None:
        _run(_disconnect_upstream(upstream))


def human_input_at(upstream: str) -> float:
    """When (the bridge's epoch clock) a person last used the named upstream's window (0: never)."""
    state = _UPS.get(upstream)
    return state.human_at if state is not None else 0.0


def last_close(upstream: str) -> tuple[int, str] | None:
    """How the named upstream's door last closed the pipe, (code, reason), once: reading forgets it."""
    state = _UPS.get(upstream)
    if state is None:
        return None
    closed, state.closed = state.closed, None  # ponytail: unlocked; one reader (the run's own turn)
    return closed


def show(run_id: str, upstream: str) -> bool:
    """Ask the Mac to raise its window on the run's first tab (a takeover ask). False: no tab yet."""
    return _run(_show(upstream, _key_for(run_id, upstream))) if _loop is not None else False


def drop(upstream: str) -> None:
    """Close and forget a non-cloud upstream entirely: its connection, clients, tabs and casts, and
    every run key routed to it. Refuses CLOUD -- that one lives for the process."""
    if upstream == CLOUD:
        raise ValueError("cannot drop the cloud upstream")
    if _loop is not None:
        _run(_drop(upstream))


# ---- loop-side ---------------------------------------------------------------------------------


async def _register(key: str, upstream: str) -> None:
    _ups(upstream).runs.add(key)
    _KEYS[key] = upstream


async def _set_upstream(url: str, generation, upstream: str) -> None:
    state = _ups(upstream)
    changed = state.generation is not None and generation != state.generation
    state.url, state.generation = url, generation
    if changed:
        await _drop_upstream(state)


async def _show(upstream: str, key: str) -> bool:
    state = _UPS.get(upstream)
    tabs = state.tabs.get(key) if state is not None else None
    if not tabs or state.up is None:
        return False
    await _send(state, "Lightbulb.show", {"targetId": tabs[0]})
    return True


async def _disconnect_upstream(upstream: str) -> None:
    state = _UPS.get(upstream)
    if state is not None:
        await _drop_upstream(state)


async def _drop(upstream: str) -> None:
    state = _UPS.pop(upstream, None)
    if state is None:
        return
    await _drop_upstream(state)
    for key in [k for k, v in _KEYS.items() if v == upstream]:
        _KEYS.pop(key, None)


async def _drop_upstream(state: _State) -> None:
    up, state.up = state.up, None
    if up is not None:
        with contextlib.suppress(Exception):
            await up.close()
    _reset(state)


def _reset(state: _State) -> None:
    for entry in state.pending.values():
        if isinstance(entry, asyncio.Future) and not entry.done():
            entry.set_exception(ConnectionError("browser connection closed"))
    state.pending.clear()
    state.quiet_calls.clear()  # state.visited stays: a save disconnects the mux, then reads it
    state.sessions.clear()
    state.attaching.clear()
    state.auto.clear()
    if state.named:  # the Mac's Chrome outlives its pipe: the run's next client re-attaches
        state.stale = set(state.tabs)
    else:
        state.tabs.clear()  # a new upstream is a new session: those tabs are gone with it
    for cast in state.casts.values():  # state.want stays: a run's next client re-creates its tab and cast
        if cast.task is not None:
            cast.task.cancel()
    state.casts.clear()
    state.cast_sessions.clear()
    state.cast_attaching.clear()
    for client in list(state.clients):
        asyncio.ensure_future(client.close())
    state.clients.clear()


async def _ensure_up(state: _State) -> None:
    async with state.up_lock:
        if state.up is not None:
            return
        if not state.url:
            raise ConnectionError("no browser session")
        state.up = await connect(state.url, max_size=None, open_timeout=10)
        state.closed = None
        asyncio.ensure_future(_pump(state, state.up))


def _command(uid: int, method: str, params: dict | None, session: str | None) -> str:
    return json.dumps({"id": uid, "method": method, "params": params or {}, **({"sessionId": session} if session else {})})


async def _call(state: _State, method: str, params: dict | None = None, session: str | None = None,
                timeout: float = CALL_TIMEOUT_S) -> dict:
    """A command of the mux's own, answered to nobody else."""
    uid = next(state.ids)
    future = asyncio.get_running_loop().create_future()
    state.pending[uid] = future
    await state.up.send(_command(uid, method, params, session))
    reply = await asyncio.wait_for(future, timeout)
    if "error" in reply:
        raise RuntimeError(reply["error"].get("message", "CDP error"))
    return reply.get("result") or {}


async def _send(state: _State, method: str, params: dict | None = None, session: str | None = None) -> None:
    """Fire and forget: its reply matches no pending entry and is dropped."""
    if state.up is not None:
        with contextlib.suppress(Exception):
            await state.up.send(_command(next(state.ids), method, params, session))


# ---- live frames -------------------------------------------------------------------------------


async def _cast(upstream: str, key: str, sink, ttl: float, resend: bool) -> None:
    state = _ups(upstream)
    live = state.casts.get(key)
    if ttl <= 0 and live is None:  # a stop for a cast that is not running: start nothing
        state.want.pop(key, None)
        return
    state.want[key] = (sink, asyncio.get_running_loop().time() + ttl)
    if live is not None:
        live.resend = live.resend or resend
        live.changed.set()  # wakes the loop to read the new deadline
    else:
        await _start_cast(state, key)


async def _start_cast(state: _State, key: str) -> None:
    tabs = state.tabs.get(key)
    if key in state.casts or key not in state.want or not tabs or state.up is None or len(state.casts) >= MAX_CASTS:
        return
    cast = state.casts[key] = _Cast(key, tabs[0])  # ponytail: the run's first tab, not whichever it last used
    state.cast_attaching[cast.target] = cast
    try:
        cast.session = (await _call(state, "Target.attachToTarget", {"targetId": cast.target, "flatten": True}))["sessionId"]
        state.cast_sessions[cast.session] = cast
        # A tab that is not the window's front tab paints nothing without this (dev 2026-09-27).
        await _call(state, "Emulation.setFocusEmulationEnabled", {"enabled": True}, cast.session)
        await _call(state, "Page.startScreencast", CAST_PARAMS, cast.session)
    except Exception as exc:  # noqa: BLE001 -- the tab closed, or the connection went
        log.info("cdp mux: cast did not start (%s)", type(exc).__name__)
        state.cast_attaching.pop(cast.target, None)
        await _stop_cast(state, key)
        return
    cast.task = asyncio.ensure_future(_cast_loop(state, cast))


async def _stop_cast(state: _State, key: str) -> None:
    cast = state.casts.pop(key, None)
    if cast is None:
        return
    if cast.task is not None and cast.task is not asyncio.current_task():
        cast.task.cancel()
    if cast.session:
        await _send(state, "Target.detachFromTarget", {"sessionId": cast.session})  # ends the screencast too


def _on_cast_event(state: _State, cast: _Cast, method: str, params: dict) -> None:
    if method != "Page.screencastFrame":
        return
    asyncio.ensure_future(_send(state, "Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, cast.session))
    meta = params.get("metadata") or {}
    cast.latest = (params.get("data") or "", int(meta.get("deviceWidth") or 0), int(meta.get("deviceHeight") or 0))
    cast.changed.set()


async def _cast_loop(state: _State, cast: _Cast) -> None:
    """Publish the latest frame, then wait out FRAME_INTERVAL_S: frames arriving meanwhile only
    replace ``latest``. Ends when nobody has asked for frames by the deadline."""
    loop = asyncio.get_running_loop()
    try:
        while True:
            sink, deadline = state.want.get(cast.key, (None, 0.0))
            if sink is None or loop.time() >= deadline:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(cast.changed.wait(), deadline - loop.time())
            cast.changed.clear()
            sink, deadline = state.want.get(cast.key, (None, 0.0))
            if sink is None or loop.time() >= deadline:
                return  # stopped while waiting (a ttl 0): not one more frame
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
        if state.casts.get(cast.key) is cast:
            await _stop_cast(state, cast.key)


async def _dispatch(state: _State, key: str | None, event: dict) -> None:
    cast = state.casts.get(key) if key else next(iter(state.casts.values()), None)
    if cast is None or not cast.session:
        raise LookupError("that tab is not being shown")
    x = min(max(float(event.get("x") or 0), 0.0), float(cast.w or 0) or 1e4)
    y = min(max(float(event.get("y") or 0), 0.0), float(cast.h or 0) or 1e4)
    kind, session = event.get("type"), cast.session
    if kind == "click":
        button = event.get("button") if event.get("button") in ("left", "middle", "right") else "left"
        await _call(state, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
        for phase in ("mousePressed", "mouseReleased"):
            await _call(state, "Input.dispatchMouseEvent", {"type": phase, "x": x, "y": y, "button": button, "clickCount": 1}, session)
    elif kind == "wheel":
        # Not Input.dispatchMouseEvent mouseWheel: a behind tab's compositor does not run, so
        # a synthetic wheel never scrolls it (dev 2026-09-28). Scroll the page itself instead.
        delta = min(max(float(event.get("deltaY") or 0), -5000.0), 5000.0)
        await _call(state, "Runtime.evaluate", {"expression": SCROLL_JS % (x, y, delta)}, session)
    elif kind == "text":
        await _call(state, "Input.insertText", {"text": str(event.get("text") or "")[:1000]}, session)
    elif kind == "key" and event.get("key") in KEYS:
        name = event["key"]
        code, text = KEYS[name]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": name, "windowsVirtualKeyCode": code}
        await _call(state, "Input.dispatchKeyEvent", {**down, **({"text": text} if text else {})}, session)
        await _call(state, "Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": code}, session)
    else:
        raise ValueError("unsupported input")


async def _close_run_everywhere(run_id: str, upstream: str | None) -> None:
    for name in ([upstream] if upstream is not None else list(_UPS)):
        state = _UPS.get(name)
        key = _key_for(run_id, name)
        if state is not None and key in state.runs:
            await _close_run(state, key)


async def _close_run(state: _State, key: str) -> None:
    state.runs.discard(key)
    state.stale.discard(key)
    state.want.pop(key, None)
    await _stop_cast(state, key)
    for target in state.tabs.pop(key, []):
        if state.up is not None:
            uid = next(state.ids)  # fire and forget: nobody waits on a closing tab
            with contextlib.suppress(Exception):
                await state.up.send(json.dumps({"id": uid, "method": "Target.closeTarget", "params": {"targetId": target}}))
    for client, owner in list(state.clients.items()):
        if owner == key:
            _forget(state, client)
            with contextlib.suppress(Exception):
                await client.close()
    _KEYS.pop(key, None)


def _forget(state: _State, client) -> None:
    state.clients.pop(client, None)
    state.auto.discard(client)
    for owners in state.sessions.values():
        owners.discard(client)
    for waiting in state.attaching.values():
        while client in waiting:
            waiting.remove(client)


async def _serve_client(client) -> None:
    path = client.request.path
    key = path[len(PREFIX):] if path.startswith(PREFIX) else ""
    upstream = _KEYS.get(key)
    state = _UPS.get(upstream) if upstream else None
    if state is None or key not in state.runs:
        await client.close(4404, "unknown run")
        return
    try:
        await _ensure_up(state)
        async with state.up_lock:
            if key in state.stale:  # after a reconnect: keep only the tabs Chrome still has
                state.stale.discard(key)
                alive = {t.get("targetId") for t in (await _call(state, "Target.getTargets")).get("targetInfos") or []}
                state.tabs[key] = [t for t in state.tabs.get(key, []) if t in alive]
            if not state.tabs.get(key):  # the run's own tab: what "the first page" means for its clients
                state.tabs[key] = [(await _call(state, "Target.createTarget", {"url": "about:blank"}))["targetId"]]
        asyncio.ensure_future(_start_cast(state, key))  # someone was already watching before the run's first client
    except Exception as exc:  # noqa: BLE001 -- no URL in the log: it is a signed capability
        log.warning("cdp mux: upstream unavailable (%s)", type(exc).__name__)
        await client.close(1011, "browser unavailable")
        return
    state.clients[client] = key
    try:
        async for raw in client:
            await _from_client(state, client, raw)
    except Exception:  # noqa: BLE001 -- a client dropping mid-send is routine
        pass
    finally:
        _forget(state, client)


async def _from_client(state: _State, client, raw) -> None:
    message = json.loads(raw)
    session = message.get("sessionId")
    if DIALOG_BRIDGE in raw:
        await client.send(json.dumps({"id": message["id"], "result": {"identifier": "0"},
                                      **({"sessionId": session} if session else {})}))
        return
    method, params = message.get("method"), message.get("params") or {}
    if session:
        state.sessions.setdefault(session, set()).add(client)
    if method == "Target.attachToTarget":
        state.attaching.setdefault(params.get("targetId"), []).append(client)
    if method == "Target.setAutoAttach" and not session:
        state.auto.add(client)
    if state.up is None:
        await client.close(1011, "browser connection closed")
        return
    if state.named and method == "DOM.setFileInputFiles":
        try:
            await _stage_files(state, params)
        except Exception as exc:  # noqa: BLE001 -- the agent reads why its upload did not happen
            error = {"code": -32000, "message": f"upload to the Mac failed: {exc}"}
            await client.send(json.dumps({"id": message["id"], "error": error, **({"sessionId": session} if session else {})}))
            return
    uid = next(state.ids)
    state.pending[uid] = (client, message["id"], method)
    if method == "Target.createTarget" and params.get("background"):
        state.quiet_calls.add(uid)
    message["id"] = uid
    await state.up.send(json.dumps(message))


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _stage_files(state: _State, params: dict) -> None:
    """Rewrite ``params["files"]`` in place: every path that is a file on this runtime becomes the
    Mac path of a copy staged there. Other paths (already on the Mac) are left alone, and so is
    everything when the desktop predates ``Lightbulb.stageFile``. Raises with a reason."""
    files = params.get("files")
    local = [f for f in files if isinstance(f, str) and os.path.isfile(f)] if isinstance(files, list) else []
    if not local:
        return
    if sum(os.path.getsize(f) for f in set(local)) > MAX_STAGE_BYTES:
        raise ValueError(f"files over {MAX_STAGE_BYTES // (1024 * 1024)} MB")
    loop, staged = asyncio.get_running_loop(), {}
    for f in dict.fromkeys(local):
        blob, path = await loop.run_in_executor(None, _read, f), None
        for at in range(0, max(len(blob), 1), STAGE_CHUNK):
            chunk = {"data": base64.b64encode(blob[at:at + STAGE_CHUNK]).decode()}
            chunk.update({"path": path} if path else {"name": os.path.basename(f)})
            try:
                path = (await _call(state, "Lightbulb.stageFile", chunk, timeout=STAGE_TIMEOUT_S))["path"]
            except RuntimeError as exc:
                if "wasn't found" in str(exc):  # Chrome itself answered: a desktop without staging
                    return
                raise
        staged[f] = path
    params["files"] = [staged.get(f, f) for f in files]


def _first_mine(state: _State, key: str, infos: list) -> list:
    mine = state.tabs.get(key, [])
    return sorted(infos, key=lambda t: mine.index(t.get("targetId")) if t.get("targetId") in mine else len(mine))


async def _pump(state: _State, up) -> None:
    try:
        async for raw in up:
            message = json.loads(raw)
            if "id" in message:
                entry = state.pending.pop(message["id"], None)
                quiet_call = message["id"] in state.quiet_calls
                if isinstance(entry, asyncio.Future):
                    if not entry.done():
                        entry.set_result(message)
                    continue
                if entry is None:
                    continue
                client, client_id, method = entry
                key, result = state.clients.get(client), message.get("result") or {}
                if key and method == "Target.createTarget" and result.get("targetId"):
                    state.tabs.setdefault(key, []).append(result["targetId"])
                    if quiet_call:
                        state.quiet.add(result["targetId"])
                if key and method == "Target.getTargets" and isinstance(result.get("targetInfos"), list):
                    result["targetInfos"] = _first_mine(state, key, result["targetInfos"])
                if result.get("sessionId"):
                    state.sessions.setdefault(result["sessionId"], set()).add(client)
                state.quiet_calls.discard(message["id"])
                message["id"] = client_id
                targets = {client}
            else:
                method, params, outer = message.get("method"), message.get("params") or {}, message.get("sessionId")
                if method == "Lightbulb.humanInput" and state.named:  # the door's own, never a client's
                    state.human_at = time.time()  # our clock: the Mac's `at` is not comparable to it
                    continue
                if outer in state.cast_sessions:  # the mux's own screencast session: nobody else's business
                    _on_cast_event(state, state.cast_sessions[outer], method, params)
                    continue
                if method == "Target.attachedToTarget" and not outer:
                    cast = state.cast_attaching.pop((params.get("targetInfo") or {}).get("targetId"), None)
                    if cast is not None:
                        cast.session = params.get("sessionId")
                        state.cast_sessions[cast.session] = cast
                        continue
                if method == "Target.detachedFromTarget" and (gone := state.cast_sessions.pop(params.get("sessionId"), None)):
                    if state.casts.get(gone.key) is gone:  # its tab closed under it: the next cast() picks another
                        state.casts.pop(gone.key)
                        if gone.task is not None:
                            gone.task.cancel()
                    continue
                if method == "Target.attachedToTarget":
                    waiting = state.attaching.get((params.get("targetInfo") or {}).get("targetId")) or []
                    if waiting:
                        owners = {waiting.pop(0)}
                    elif outer:
                        owners = set(state.sessions.get(outer, ()))
                    else:
                        owners = set(state.auto or state.clients)
                    state.sessions[params.get("sessionId")] = owners
                    targets = owners
                elif outer:
                    targets = state.sessions.get(outer) or set(state.clients)
                else:
                    targets = set(state.clients)
                if method in ("Target.targetCreated", "Target.targetInfoChanged"):
                    info = params.get("targetInfo") or {}
                    if info.get("type") == "page":
                        _landed(state, info.get("url"), info.get("targetId"))
                elif method == "Page.frameNavigated":
                    frame = params.get("frame") or {}
                    if not frame.get("parentId"):  # the main frame's id is its target's id
                        _landed(state, frame.get("url"), frame.get("id"))
                if method == "Target.targetDestroyed":
                    state.quiet.discard(params.get("targetId"))
                    for tabs in state.tabs.values():
                        with contextlib.suppress(ValueError):
                            tabs.remove(params.get("targetId"))
                if method == "Target.detachedFromTarget":
                    state.sessions.pop(params.get("sessionId"), None)
            text = json.dumps(message)
            for client in list(targets):
                if client in state.clients:
                    with contextlib.suppress(Exception):
                        await client.send(text)
    except Exception:  # noqa: BLE001 -- AWS closing or revoking the stream
        pass
    finally:
        if state.up is up:
            if state.named:
                state.closed = (up.close_code or 1006, up.close_reason or "")
            state.up = None
            _reset(state)
