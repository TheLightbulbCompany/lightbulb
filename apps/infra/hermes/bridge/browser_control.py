"""The workspace browser from inside the bridge: one connection for every run, and the person-yield.

Every run in this microVM browses at the same time, each on its own tab, over the ONE automation
connection AWS admits per session (``cdp_mux``). The backend's lease is held by the bridge as a
whole (``LEASE_ID``): every browser call renews it (and learns the signed URL, the generation and
when a person last typed); once no run has browsed for ``RELEASE_GRACE_S`` the bridge gives the
connection back and releases, which saves the browser.

People use the workspace browser at the same time through the live view. The agent yields: when
a person acted in the last YIELD_QUIET_S, its next browser action waits until they have been
quiet that long, and any action after a person's input needs a fresh snapshot first. Agents may
do anything else in this browser: any URL, page scripts, raw CDP (``browser_cdp``), JavaScript
dialogs (``browser_dialog``) and real mouse/keyboard input on the screen (``computer_use``).

People who cannot have the live view (AWS streams it to one viewer) see live frames instead:
the backend says someone wants them (``framesAt`` on every acquire, and the ``browser.frames``
RPC on each watcher's heartbeat), and every browsing run with a room is cast there
(``cdp_mux.cast``) until FRAMES_TTL_S after the last ask. Their clicks and keys come back as
``browser.input`` and count as a person's input, so the agent yields to them as well.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import uuid
from contextvars import ContextVar

from bridge import API, HOOK_TOKEN, OWNER, cdp_mux

#: Wait for this much quiet after a person's last input before acting.
YIELD_QUIET_S = 3.0
#: Never hold a turn longer than this waiting for a person who keeps typing.
YIELD_MAX_S = 10.0
#: pre_tool_call's whole budget: acquire (and polling a starting browser) plus the yield.
#: Hermes abandons a hook after `plugins.hook_callback_timeout` (30 s, fail-closed for
#: pre_tool_call) and then fails EVERY tool call for the turn while the abandoned thread runs
#: (prod 2026-09-27 03:38Z) -- so this must stay well inside it.
HOOK_BUDGET_S = 20.0
#: A cold browser (session + saved sign-ins) outlasts API Gateway's 30 s, so the backend
#: answers 202 "starting" and finishes in the background; the hook polls within its budget.
START_POLL_S = 2.0
STARTING_MESSAGE = "The shared browser is starting; retry this browser action in a few seconds."
#: The backend lease's controller id: the bridge, on behalf of all its runs. Stable across
#: processes, so a restarted microVM renews the lease its predecessor left instead of waiting it out.
LEASE_ID = "bridge"
#: Keep the connection this long after the last browsing run ends, then release (and save).
RELEASE_GRACE_S = 30.0
#: Gated tools that act on the page or screen directly, not through snapshot element refs.
NO_REFS = ("browser_cdp", "browser_dialog", "computer_use")
#: Cast frames this long after the last "someone is watching frames" (watchers beat every ~20 s).
FRAMES_TTL_S = 45.0

_LOCK = threading.Lock()
_LEASE: dict = {}                    # {"generation", "cdpUrl"} while the bridge holds the lease
_BROWSING: dict[str, set] = {}       # run id -> its Hermes task ids, for runs that browsed and have not ended
_timer: threading.Timer | None = None
_SINKS: dict = {}                    # run id -> publish(frame) to its room, for browsing runs with one
_frames_until = 0.0                  # epoch: someone wants live frames until then
_TURN: ContextVar[dict | None] = ContextVar("browser_control", default=None)


def _post(path: str, body: dict, timeout: float = 20.0) -> dict | None:
    request = urllib.request.Request(
        f"{API}/api/v1/internal/browser/{OWNER}/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
            return payload if 200 <= response.status < 300 else None
    except (OSError, ValueError):  # HTTPError is an OSError
        return None


def begin_turn(run_id: str, agent_id: str, frame_sink=None) -> dict:
    """Bind identity only; the first browser tool lazily acquires. ``frame_sink`` publishes a
    live frame of this run's tab to its room (room turns only)."""
    state = {"runId": str(run_id or f"browser:{agent_id}:{uuid.uuid4().hex}"), "agentId": agent_id,
             "generation": None, "snapshotGeneration": None, "pendingSnapshots": {}, "frameSink": frame_sink}
    _TURN.set(state)
    return state


def cdp_url() -> str:
    """This turn's own loopback CDP endpoint (Hermes's CDP override: see ``install``)."""
    state = _TURN.get()
    return cdp_mux.url_for(state["runId"]) if state and os.environ.get("BROWSER_CDP_URL") else ""


def install() -> None:
    """Point Hermes's one CDP resolver at this turn's endpoint. agent-browser's ``--cdp``, the
    supervisor and ``browser_cdp`` all resolve through ``browser_tool_cdp._get_cdp_override``;
    ``BROWSER_CDP_URL`` (set at boot) only switches Hermes into CDP mode."""
    try:
        from tools import browser_tool_cdp
    except ImportError:  # bridge-only unit tests
        return
    browser_tool_cdp._get_cdp_override = cdp_url


def _disconnect(task_id: str) -> bool:
    try:
        from tools.browser_tool_lifecycle import _force_reap_browser_session
        _force_reap_browser_session(task_id or "default")
        return True
    except Exception:
        return False


def _refresh(state: dict, deadline: float | None = None) -> bool:
    global _timer
    if not os.environ.get("BROWSER_CDP_URL"):
        return False
    deadline = time.monotonic() + HOOK_BUDGET_S if deadline is None else deadline
    body = {"runId": LEASE_ID, "agentId": state["agentId"]}
    result = _post("acquire", body, timeout=max(1.0, deadline - time.monotonic()))
    while result and result.get("status") == "starting" and deadline - time.monotonic() > START_POLL_S + 1:
        time.sleep(START_POLL_S)
        result = _post("acquire", body, timeout=max(1.0, deadline - time.monotonic()))
    state["starting"] = bool(result and result.get("status") == "starting")
    if not result or not result.get("cdpUrl") or result.get("generation") is None:
        return False
    state["humanInputAt"] = float(result.get("humanInputAt") or 0)
    if result.get("framesAt"):
        _want_frames(float(result["framesAt"]) + FRAMES_TTL_S)
    generation = result["generation"]
    cdp_mux.set_upstream(result["cdpUrl"], generation)
    with _LOCK:
        _LEASE.update(generation=generation, cdpUrl=result["cdpUrl"])
        tasks = set(_BROWSING.setdefault(state["runId"], set()))
        if _timer:
            _timer.cancel()
            _timer = None
        changed = state["generation"] not in (None, generation)
        state["generation"] = generation
        if changed:  # a new session or a revoked connection: this run's clients and refs are gone
            state["snapshotGeneration"] = None
            state["pendingSnapshots"].clear()
    if changed:
        for task_id in tasks:
            _disconnect(task_id)
    return True


def renew() -> None:
    """Keep the lease while a vault tool waits on a person (website_logins._ask): a lapsed lease
    hands the browser away mid-login."""
    state = _TURN.get()
    if state and state.get("generation") is not None:
        _post("acquire", {"runId": LEASE_ID, "agentId": state["agentId"]}, timeout=30.0)


def _want_frames(until: float) -> None:
    global _frames_until
    with _LOCK:
        _frames_until = max(_frames_until, until)


def _cast(run_id: str, sink, resend: bool = False) -> None:
    ttl = _frames_until - time.time()
    if sink is not None and ttl > 0:
        cdp_mux.cast(run_id, sink, ttl, resend)


def frames_wanted(ttl: float = FRAMES_TTL_S) -> int:
    """A watcher's heartbeat: cast every browsing run for ``ttl`` more seconds, and resend each
    one's latest frame so a viewer who just arrived at a still page sees it."""
    _want_frames(time.time() + min(max(ttl, 0.0), 120.0))
    with _LOCK:
        runs = list(_SINKS.items())
    for run_id, sink in runs:
        _cast(run_id, sink, resend=True)
    return len(runs)


def dispatch_input(event: dict) -> None:
    """A watcher's click/wheel/key/text, on the run's cast tab (LookupError: not cast)."""
    cdp_mux.dispatch(event.get("runId") or None, event)


def _end_run(run_id: str) -> None:
    global _timer
    with _LOCK:
        _SINKS.pop(run_id, None)
        tasks = _BROWSING.pop(run_id, None)
    if tasks is None:
        return
    for task_id in tasks:
        _disconnect(task_id)
    cdp_mux.close_run(run_id)
    with _LOCK:
        if not _BROWSING and _LEASE and not _timer:
            _timer = threading.Timer(RELEASE_GRACE_S, _release_if_idle)
            _timer.daemon = True
            _timer.start()


def _release_if_idle() -> None:
    global _timer
    with _LOCK:
        _timer = None
        if _BROWSING or not _LEASE:
            return
        generation = _LEASE["generation"]
        _LEASE.clear()
    # ponytail: a run that acquires between this check and the save loses one call to the
    # disconnect; its next call sees the save's new generation and reconnects.
    cdp_mux.disconnect()  # the backend's save needs the one automation connection
    _post("release", {"runId": LEASE_ID, "generation": generation}, timeout=190.0)


def release(state: dict | None) -> None:
    """Turn end: close this run's tabs and clients; the lease goes once every run is done."""
    _TURN.set(None)
    if state:
        _end_run(state["runId"])


def abort(run_id: str) -> bool:
    """Stop: end the run's browser clients now, so an in-flight agent-browser command fails
    instead of running to its own timeout."""
    with _LOCK:
        held = bool(run_id) and run_id in _BROWSING
    if held:
        _end_run(run_id)
    return held


def _yield_to_person(state: dict, budget: float | None = None) -> bool:
    """Wait until a person has been quiet for YIELD_QUIET_S (bounded by YIELD_MAX_S and the
    hook's budget); False if the browser is lost."""
    deadline = time.monotonic() + YIELD_MAX_S
    if budget is not None:
        deadline = min(deadline, budget)
    while True:
        quiet = time.time() - state.get("humanInputAt", 0)
        if quiet >= YIELD_QUIET_S or time.monotonic() >= deadline:
            return True
        time.sleep(min(YIELD_QUIET_S - quiet, max(0.0, deadline - time.monotonic())))
        if not _refresh(state, budget):  # also re-reads humanInputAt
            return False


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Hermes hook: acquire for browser calls, yield to a person, fence stale element refs."""
    if not (str(tool_name).startswith("browser_") or tool_name == "computer_use"):
        return None
    state = _TURN.get()
    budget = time.monotonic() + HOOK_BUDGET_S
    if not state or not _refresh(state, budget) or not _yield_to_person(state, budget):
        if state and state.get("starting"):
            return {"action": "block", "message": STARTING_MESSAGE}
        return {"action": "block", "message": "Browser control is unavailable for this turn."}
    with _LOCK:
        _BROWSING.setdefault(state["runId"], set()).add(str(kwargs.get("task_id") or "default"))
        if state.get("frameSink") is not None:
            _SINKS[state["runId"]] = state["frameSink"]
    _cast(state["runId"], state.get("frameSink"))
    if tool_name in NO_REFS:
        return None
    with _LOCK:
        # The page may have changed under a person's hands since this turn last looked.
        if state.get("humanInputAt", 0) > state.get("snapshotAt", 0):
            state["snapshotGeneration"] = None
    if state.get("snapshotGeneration") != state.get("generation"):
        if tool_name != "browser_snapshot":
            return {"action": "block", "message": "The page may have changed (a person used the shared browser, "
                    "or the session changed). Take a fresh browser_snapshot before this action."}
        with _LOCK:
            state["pendingSnapshots"][str(kwargs.get("tool_call_id") or "")] = state["generation"]
    return None


def post_tool_call(tool_name: str = "", status: str = "", **kwargs) -> None:
    """Credit only a completed snapshot from the generation that authorized it."""
    if tool_name != "browser_snapshot":
        return
    state = _TURN.get()
    if not state:
        return
    with _LOCK:
        generation = state["pendingSnapshots"].pop(str(kwargs.get("tool_call_id") or ""), None)
        if status == "ok" and generation == state.get("generation"):
            state["snapshotGeneration"] = generation
            state["snapshotAt"] = time.time()


def session_ref() -> dict | None:
    """The session the bridge holds, for the screen driver: {browserIdentifier, sessionId}."""
    with _LOCK:
        url = _LEASE.get("cdpUrl") or ""
    match = re.search(r"/browser-streams/([^/]+)/sessions/([^/?]+)/", url)
    return {"browserIdentifier": match.group(1), "sessionId": match.group(2)} if match else None
