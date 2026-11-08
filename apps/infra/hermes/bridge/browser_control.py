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
the backend says someone wants them (``framesAt`` on every CLOUD acquire, and the ``browser.frames``
RPC on each watcher's heartbeat), and every browsing CLOUD run with a room is cast there
(``cdp_mux.cast``) until FRAMES_TTL_S after the last ask. Their clicks and keys come back as
``browser.input`` and count as a person's input, so the agent yields to them as well.

A run can instead browse on the requester's paired Mac (``select_mac``, the agent's
``lightbulb_desktop use_browser``): the same ``browser_*`` tools drive a separate "Lightbulb agent"
Chrome window there, through the Mac's ticketed CDP door on the run's own ``cdp_mux`` upstream
(``mac:<device>:<run>``). The lease is the run's own, not the bridge's. The desktop app casts that
Mac's own live view to watchers itself -- the bridge never screencasts a Mac run. The
workspace sign-ins sync with the same ``browser_state.checkpoint`` the cloud uses: the backend
answers the steps, the bridge runs them over the run's pipe and reports ``mac/synced``; the run's
release saves before the turn reports run.ended, while the run is still active.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextvars import ContextVar

import bridge
from bridge import API, browser_state, cdp_mux
from bridge.cdp_mux import CLOUD

log = logging.getLogger("bridge.browser_control")

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
#: Tools whose result is a fresh snapshot: Hermes's browser_navigate returns one ("no need to call
#: browser_snapshot separately after navigating"), and it must be the first browser call of a turn.
SNAPSHOTS = ("browser_snapshot", "browser_navigate")
#: Cast frames this long after the last "someone is watching frames" (watchers beat every ~20 s).
FRAMES_TTL_S = 45.0
SESSION_FRAME_INTERVAL_S = 0.5  # a shared session's cloud thumbs: <= 2 fps
UNAVAILABLE = "Browser control is unavailable for this turn."
#: One Mac sync (all its steps) including the synced report; and a Mac run's whole release.
#: Inside the backend's CLAIM_SECONDS (180): the steps share it, so the sync ends before the claim.
MAC_SYNC_BUDGET_S = 120
#: The ``mac/synced`` report's own wait, even when the steps used the whole budget (the claim's
#: 60 s of slack past MAC_SYNC_BUDGET_S covers it; Stop's 10 s + this stays inside the 20 s grace).
MAC_SYNCED_MIN_S = 5.0
#: Stop still saves the Mac, inside the backend's 20 s abort grace; past it the Mac keeps its changes.
MAC_STOP_SAVE_S = 10
MAC_SYNCING = ("The Mac's browser is loading the workspace's sign-ins; retry this browser "
               "action in a few seconds.")
MAC_UNAVAILABLE = "The Mac's browser is unavailable."
ACCESS_ENDED = "Browser access on that Mac was turned off."
#: After ACCESS_ENDED the run stays off the cloud until the agent chooses (never a silent fallback).
MAC_DESELECTED = (ACCESS_ENDED + " Call lightbulb_desktop use_browser to try again, or use_cloud_browser "
                  "to browse in the cloud browser.")
#: select_mac's whole wait, polling 202s: under API Gateway's 30 s (one acquire may use it all).
MAC_SELECT_BUDGET_S = 28.0
#: The most one acquire may take (sent as ``budgetS``): the backend answers inside it, 202 when a
#: cold Chrome launch is still running, so no claim is written after its caller gave up.
MAC_ACQUIRE_MAX_S = 25.0
#: A browser call keeps at least this for its acquire before waiting on a running sync.
MAC_ACQUIRE_MIN_S = 8.0
MAC_BUSY = "This Mac's browser is busy; try again shortly."
COMPUTER_USE_MAC = "computer_use works on the cloud browser's screen only; on a Mac use the browser_* tools."
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
#: A backend ticketed Mac door (mac_cdp.door_url): ws://<task>/api/v1/internal/mac-cdp/<ticket>.
_DOOR = re.compile(r"ws://[A-Za-z0-9.:-]+/api/v1/internal/mac-cdp/[A-Za-z0-9_=-]+")
#: A named Mac upstream, ``mac:<device>:<run>``.
_MAC_UPSTREAM = re.compile(r"mac:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}")
#: A shared Claude Code or Codex session's run (backend session_browser.run_id).
_SESSION_RUN = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Who holds the bridge's lease on a session's behalf (the panel names no agent for it).
AGENT_SESSION = "session"
#: Renew the bridge's lease this often while a session's commands flow (the backend's lease is 300 s).
SESSION_RENEW_S = 120.0

_LOCK = threading.Lock()
_LEASE: dict = {}                    # {"generation", "cdpUrl"} while the bridge holds the lease
_BROWSING: dict[str, set] = {}       # run id -> its Hermes task ids, for runs that browsed and have not ended
_timer: threading.Timer | None = None
_SINKS: dict = {}                    # run id -> publish(frame) to its room, for browsing CLOUD runs with one
_SESSION_SINKS: dict = {}            # session run id -> publish(frame) to its session frames channel
_PUMPS: dict = {}                    # session run id -> its current door pump ({"outer": door socket})
_MAC_STATES: dict = {}               # run id -> its turn state, while it has a Mac selected (for Stop)
_frames_until = 0.0                  # epoch: someone wants live frames until then
_TURN: ContextVar[dict | None] = ContextVar("browser_control", default=None)


def _request(path: str, body: dict) -> urllib.request.Request:
    return urllib.request.Request(
        f"{API}/api/v1/internal/browser/{bridge.OWNER}/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bridge.HOOK_TOKEN}"},
        method="POST",
    )


def _post(path: str, body: dict, timeout: float = 20.0) -> dict | None:
    try:
        with urllib.request.urlopen(_request(path, body), timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
            return payload if 200 <= response.status < 300 else None
    except (OSError, ValueError):  # HTTPError is an OSError
        return None


def _post_mac(path: str, body: dict, timeout: float = 20.0) -> tuple[int, dict]:
    """A Mac route: (status, answer), where a refusal's answer is its ``detail`` ({code, message});
    (0, {}) when the backend was not reached."""
    try:
        with urllib.request.urlopen(_request(path, body), timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:  # every 4xx/5xx
        try:
            detail = json.loads(exc.read() or b"{}").get("detail")
        except (OSError, ValueError, AttributeError):
            detail = None
        return exc.code, detail if isinstance(detail, dict) else {}
    except (OSError, ValueError):
        return 0, {}


def begin_turn(run_id: str, agent_id: str, frame_sink=None, family: str = "room", room_id: str | None = None) -> dict:
    """Bind identity only; the first browser tool lazily acquires. ``frame_sink`` publishes a
    live frame of this run's tab to its room (room turns only). ``family`` (room|paperclip|task) and
    ``room_id`` go with the Mac routes."""
    state = {"runId": str(run_id or f"browser:{agent_id}:{uuid.uuid4().hex}"), "agentId": agent_id,
             "generation": None, "snapshotGeneration": None, "pendingSnapshots": {}, "frameSink": frame_sink,
             "family": family, "roomId": room_id, "mac": None, "macEnded": False, "error": None, "macSync": None,
             "lock": threading.Lock()}
    _TURN.set(state)
    return state


def _mac_upstream(state: dict, mac: dict | None = None) -> str | None:
    """``mac:<device>:<run>``, from ``mac`` (a snapshot) or the state's current Mac; None when
    there is none -- Stop may deselect it from another thread at any moment."""
    mac = mac if mac is not None else state.get("mac")
    device = mac.get("deviceId") if mac else None
    return f"mac:{device}:{state['runId']}" if device else None


def _upstream_of(state: dict) -> str:
    return _mac_upstream(state) or CLOUD


def cdp_url() -> str:
    """This turn's own loopback CDP endpoint (Hermes's CDP override: see ``install``): on the Mac
    once ``use_browser`` selected one, else the cloud."""
    state = _TURN.get()
    if not state or not os.environ.get("BROWSER_CDP_URL") or state.get("macEnded"):
        return ""
    mac = state.get("mac")
    if mac:
        name = _mac_upstream(state, mac)
        return cdp_mux.url_for(state["runId"], name) if name else ""
    return cdp_mux.url_for(state["runId"])


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
    state["error"] = None
    if not os.environ.get("BROWSER_CDP_URL"):
        return False
    deadline = time.monotonic() + HOOK_BUDGET_S if deadline is None else deadline
    if state.get("macEnded"):  # the Mac ended access: stay off the cloud until the agent chooses
        state["error"] = MAC_DESELECTED
        return False
    mac = state.get("mac")
    if mac:
        return _refresh_mac(state, deadline, mac)
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


def _refresh_mac(state: dict, deadline: float, mac: dict) -> bool:
    """The run's Mac lease: acquire (or renew) it, adopting the device and generation the backend
    picks, and start the sign-in sync it hands back. Never touches the cloud's lease. ``mac`` is
    this call's snapshot: Stop may deselect the Mac from another thread meanwhile."""
    run = state["runId"]
    if mac.get("deviceId"):
        closed = cdp_mux.last_close(_mac_upstream(state, mac))  # how the door closed the pipe, if it did
        code, reason = closed or (0, "")
        if code == 4403 or (code == 4502 and reason in ("BROWSER_OFF", "PROFILE_CHANGED")):
            state["error"] = ACCESS_ENDED
            _forget_mac(state)  # the backend lease lapses
            state["macEnded"] = True  # the agent must use_browser or use_cloud_browser
            return False
        if code == 4503 or (code == 4502 and reason == "CAPACITY"):
            state["error"] = MAC_BUSY
            return False
    body = {"runId": run, "agentId": state["agentId"], "family": state["family"],
            **({"deviceId": mac["deviceId"]} if mac.get("deviceId") else {})}

    def acquire() -> tuple[int, dict]:
        left = deadline - time.monotonic()
        budget = round(max(1.0, min(MAC_ACQUIRE_MAX_S, left - 2)), 3)  # 2 s for the hops back
        return _post_mac("mac/acquire", {**body, "budgetS": budget}, timeout=max(1.0, left))

    status, result = acquire()
    while status == 202 and deadline - time.monotonic() > START_POLL_S + 1:
        time.sleep(START_POLL_S)
        status, result = acquire()
    state["starting"] = status == 202  # a sign-in sync is running for this Mac (maybe another run's)
    device, generation = result.get("deviceId"), result.get("generation")
    if status != 200 or not isinstance(result.get("cdpUrl"), str) or not generation \
            or not isinstance(device, str) or not _ID.fullmatch(device):
        message = result.get("message") if status not in (0, 202) else None
        state["error"] = MAC_SYNCING if status == 202 else message if isinstance(message, str) and message else MAC_UNAVAILABLE
        return False
    if state.get("mac") is not mac:  # Stop gave the Mac back while this acquire was in flight
        return False
    old = _mac_upstream(state, mac) if mac.get("generation") else None
    mac["deviceId"] = device
    name = _mac_upstream(state, mac)
    if old and old != name:  # the backend resolved the pair's other id: forget the old pipe
        cdp_mux.close_run(run, old)
        cdp_mux.drop(old)
    mac["deviceName"] = result["deviceName"] if isinstance(result.get("deviceName"), str) else None
    generation = str(generation)
    cdp_mux.set_upstream(result["cdpUrl"], generation, upstream=name)  # a new generation drops the old pipe
    state["humanInputAt"] = cdp_mux.human_input_at(name)  # the person in the agent window, never the cloud's
    with _LOCK:
        tasks = set(_BROWSING.setdefault(run, set()))
        changed = state["generation"] not in (None, generation)
        state["generation"] = mac["generation"] = generation
        if changed:  # the Mac slept and reconnected: this run's clients and refs are gone
            state["snapshotGeneration"] = None
            state["pendingSnapshots"].clear()
    if changed:
        for task_id in tasks:
            _disconnect(task_id)
    sync = result.get("sync")
    running = state.get("macSync")
    if isinstance(sync, dict) and sync.get("steps") and (running is None or not running.is_alive()):
        thread = threading.Thread(target=_run_mac_sync, args=(state, sync), name="mac-sync", daemon=True)
        state["macSync"] = thread
        thread.start()
    return True


def _mac_steps(url: str, steps: list, deadline: float) -> tuple[bool, str | None]:
    """Each step through ``browser_state.checkpoint`` on ``url`` (a loopback client on a Mac pipe), in
    order, stopping at the first failure: (all ran, the last step's action). Never logs a URL or a step."""
    kind = None
    for step in steps:
        kind = step.get("action") if isinstance(step, dict) else None
        left = deadline - time.monotonic()
        if kind is None or left <= 0:
            return False, kind
        try:
            browser_state.checkpoint({**step, "source": "mac", "cdpUrl": url}, budget_s=left)
        except Exception as exc:  # noqa: BLE001 -- the class only
            log.warning("mac browser sync failed: %s", type(exc).__name__)
            return False, kind
    return True, kind


def _run_mac_sync(state: dict, sync: dict, deadline: float | None = None) -> bool:
    """The one sync engine: each step through ``browser_state.checkpoint`` over a loopback client
    on the run's own Mac pipe, in order, stopping at the first failure; then ``mac/synced``.
    Never logs a URL, a step or an exception message."""
    deadline = time.monotonic() + MAC_SYNC_BUDGET_S if deadline is None else deadline
    mac = state.get("mac")
    name, key = _mac_upstream(state, mac), f"sync:{state['runId']}"
    if name is None:  # deselected before this thread ran: the claim lapses on the backend
        return False
    device = mac["deviceId"]
    try:
        ok, kind = _mac_steps(cdp_mux.url_for(key, upstream=name), sync.get("steps") or [], deadline)
    except Exception as exc:  # noqa: BLE001 -- the mux did not start
        ok, kind = False, None
        log.warning("mac browser sync failed: %s", type(exc).__name__)
    finally:
        with contextlib.suppress(Exception):
            cdp_mux.close_run(key, name)
    body = {"runId": state["runId"], "agentId": state["agentId"], "family": state["family"],
            "deviceId": device, "syncId": str(sync.get("syncId") or ""), "ok": ok}
    if not ok and kind:  # a failed save gets a muted note in the room (the backend posts it)
        body["kind"] = kind
        if state.get("roomId"):
            body["roomId"] = state["roomId"]
    _post_mac("mac/synced", body, timeout=max(MAC_SYNCED_MIN_S, min(20.0, deadline - time.monotonic())))
    return ok


def mac_checkpoint(params: dict) -> dict:
    """``browser.state`` with a ``doorUrl``: the backend's own sign-in sync
    for a shared Claude Code or Codex session's Mac browser. Points the named upstream at the ticketed
    door, runs the steps over one loopback client on it, then forgets the upstream. Same engine as a
    run's own sync; raises ValueError for anything but a backend door."""
    door, upstream, steps = params.get("doorUrl"), params.get("upstream"), params.get("steps")
    if (not (isinstance(door, str) and _DOOR.fullmatch(door))
            or not (isinstance(upstream, str) and _MAC_UPSTREAM.fullmatch(upstream))
            or not isinstance(steps, list) or not steps):
        raise ValueError("invalid Mac checkpoint")
    key = f"sync:{upstream}"
    try:
        cdp_mux.set_upstream(door, str(params.get("generation") or ""), upstream=upstream)
        ok, _ = _mac_steps(cdp_mux.url_for(key, upstream=upstream), steps, time.monotonic() + MAC_SYNC_BUDGET_S)
    finally:
        with contextlib.suppress(Exception):
            cdp_mux.close_run(key, upstream)
        with contextlib.suppress(Exception):
            cdp_mux.drop(upstream)
    return {"ok": ok}


def _forget_mac(state: dict) -> None:
    """Close the run's tabs on the Mac, then its pipe; back to the cloud with a fresh snapshot."""
    run = state["runId"]
    name = _mac_upstream(state)
    if name:
        with contextlib.suppress(Exception):
            cdp_mux.close_run(run, name)  # closes the run's tabs (Target.closeTarget) before the pipe goes
            cdp_mux.drop(name)
    with _LOCK:
        _MAC_STATES.pop(run, None)
        tasks = set(_BROWSING.get(run, ()))
        state["mac"] = state["macSync"] = None
        state["generation"] = state["snapshotGeneration"] = None
        state["pendingSnapshots"].clear()
    for task_id in tasks:
        _disconnect(task_id)


def _mac_release(state: dict, budget: float) -> None:
    """Give the Mac back, synchronously: wait for a running sync, release, run the save the
    backend hands back over the still-open pipe, then close the run's tabs and the pipe. The
    caller holds ``state["lock"]``; bounded by ``budget`` (a save past it is abandoned)."""
    mac = state.get("mac")
    if not mac:
        return
    deadline = time.monotonic() + budget
    running = state.get("macSync")
    if running is not None:
        running.join(max(0.0, deadline - time.monotonic()))
    if mac.get("deviceId") and mac.get("generation"):
        status, answer = _post_mac(
            "mac/release",
            {"runId": state["runId"], "agentId": state["agentId"], "family": state["family"],
             "deviceId": mac["deviceId"], "generation": mac["generation"]},
            timeout=max(1.0, min(20.0, deadline - time.monotonic())),
        )
        sync = answer.get("sync") if status == 200 else None
        if isinstance(sync, dict) and sync.get("steps"):
            if isinstance(answer.get("cdpUrl"), str) and answer.get("generation"):
                # A fresh ticket: the run's own is single-use and may have expired, so a pipe that
                # dropped would redial into a 4401. A new generation drops the old pipe.
                cdp_mux.set_upstream(answer["cdpUrl"], str(answer["generation"]), upstream=_mac_upstream(state, mac))
            worker = threading.Thread(target=_run_mac_sync, args=(state, sync, deadline), name="mac-save", daemon=True)
            worker.start()
            worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                log.warning("mac browser save gave up after %.0f s", budget)
    _forget_mac(state)


def select_mac(device_id: str | None = None) -> str | None:
    """``use_browser``: point this run's browser tools at the requester's Mac (the backend picks
    the ready one when ``device_id`` is None). None when ready (or its sign-ins are still
    loading); else the message to show the agent, verbatim."""
    state = _TURN.get()
    if not state:
        return MAC_UNAVAILABLE
    if device_id is not None and not (isinstance(device_id, str) and _ID.fullmatch(device_id)):
        return "Invalid deviceId."
    state["macEnded"] = False
    mac = state.get("mac")
    if mac and device_id not in (None, mac.get("deviceId")):
        select_cloud()  # another Mac: give this one back (and save it) first
        mac = None
    if not mac:
        run = state["runId"]
        with _LOCK:
            tasks = set(_BROWSING.get(run, ()))
        cdp_mux.close_run(run, CLOUD)  # the run's cloud tabs and clients end here
        for task_id in tasks:
            _disconnect(task_id)
        with _LOCK:
            state["mac"] = {"deviceId": device_id, "generation": None, "deviceName": None}
            state["generation"] = state["snapshotGeneration"] = None
            state["pendingSnapshots"].clear()
            _MAC_STATES[run] = state
            _SINKS.pop(run, None)  # a stale CLOUD sink would otherwise cast a now-closed tab
    if _refresh(state, time.monotonic() + MAC_SELECT_BUDGET_S) or state.get("starting"):
        return None
    message = state.get("error") or MAC_UNAVAILABLE
    if state.get("mac") and not state["mac"].get("generation"):  # never acquired: nothing to give back
        with _LOCK:
            state["mac"] = None
            _MAC_STATES.pop(state["runId"], None)
    return message


def select_cloud() -> None:
    """``use_cloud_browser``: give the Mac back (saving its sign-ins) and browse in the cloud again."""
    state = _TURN.get()
    if state:
        state["macEnded"] = False
    if state and state.get("mac"):
        with state["lock"]:
            _mac_release(state, MAC_SYNC_BUDGET_S)


def selected_mac() -> dict | None:
    """This run's Mac: {deviceId, deviceName, generation}, or None on the cloud."""
    state = _TURN.get()
    mac = state.get("mac") if state else None
    return dict(mac) if mac else None


def show() -> bool:
    """Raise the Mac's agent window on this run's tab (a login/2FA/CAPTCHA handed to the person)."""
    state = _TURN.get()
    name = _mac_upstream(state) if state else None
    if not name:
        return False
    try:
        return bool(cdp_mux.show(state["runId"], name))
    except Exception:  # noqa: BLE001 -- the pipe is down: nothing to raise
        return False


def renew() -> None:
    """Keep the lease while a vault tool waits on a person (website_logins._ask): a lapsed lease
    hands the browser away mid-login."""
    state = _TURN.get()
    if state and state.get("mac"):
        _refresh(state)
        return
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
    """A watcher's heartbeat: cast every browsing CLOUD run for ``ttl`` more seconds, and resend
    each one's latest frame so a viewer who just arrived at a still page sees it. A Mac-upstream
    run is never in ``_SINKS`` -- the desktop app casts its own Mac view."""
    _want_frames(time.time() + min(max(ttl, 0.0), 120.0))
    with _LOCK:
        runs = list(_SINKS.items())
    for run_id, sink in runs:
        _cast(run_id, sink, resend=True)
    return len(runs)


def dispatch_input(event: dict) -> None:
    """A watcher's click/wheel/key/text, on the run's cast tab (LookupError: not cast)."""
    cdp_mux.dispatch(event.get("runId") or None, event)


def attach_session(params: dict) -> dict:
    """``browser.attach``: a shared Claude Code or Codex session browses the
    workspace's cloud browser as one more run, in its own tabs. The backend hands a single-use
    ticketed door carrying the session's CDP from its Mac; this dials it and pumps it into the run's
    own loopback endpoint until either side closes. "starting" while the cloud browser starts: the
    backend asks again with a fresh ticket."""
    run, door = params.get("runId"), params.get("doorUrl")
    renew = params.get("renew") is True
    if not (isinstance(run, str) and _SESSION_RUN.fullmatch(run)) or not (renew or (isinstance(door, str) and _DOOR.fullmatch(door))):
        raise ValueError("invalid browser attach")
    if renew:  # after idle: the door still pumps; only the lease may have lapsed
        with _LOCK:
            pumped = run in _BROWSING
        if not pumped:
            return {"status": "unavailable"}
    state = begin_turn(run, AGENT_SESSION, family="session")
    try:
        ready = _refresh(state)
    finally:
        _TURN.set(None)
    if not ready:
        return {"status": "starting" if state.get("starting") else "unavailable"}
    if renew:
        return {"status": "attached"}
    sink = _session_sink(run, params.get("frameEndpoint"), params.get("frameChannel"))
    pump: dict = {"outer": None}
    with _LOCK:
        _BROWSING.setdefault(run, set())  # the bridge's lease and grace timer cover it
        if sink is not None:
            _SESSION_SINKS[run] = sink
        old, _PUMPS[run] = _PUMPS.get(run), pump  # a fresh door takes over: the old pump ends quietly
    if old and old["outer"] is not None:
        with contextlib.suppress(Exception):
            old["outer"].close()
    _start_pump(run, door, pump)
    return {"status": "attached"}


def _start_pump(run: str, door: str, pump: dict) -> None:
    threading.Thread(target=_pump_door, args=(run, door, pump), name="session-door", daemon=True).start()


def _renew_session_lease() -> None:
    """Renew the bridge's lease for a session, adopting a new generation the way _refresh does."""
    result = _post("acquire", {"runId": LEASE_ID, "agentId": AGENT_SESSION})
    if result and result.get("cdpUrl") and result.get("generation") is not None:
        cdp_mux.set_upstream(result["cdpUrl"], result["generation"])
        with _LOCK:
            _LEASE.update(generation=result["generation"], cdpUrl=result["cdpUrl"])


def _pump_door(run: str, door: str, pump: dict) -> None:
    """The door <-> the run's loopback endpoint, both ways, until either side closes; the bridge's
    lease is renewed every SESSION_RENEW_S while commands flow Mac -> cloud (a receive-only session
    is kept by the backend's ``active`` re-attach). Then, unless a newer door took the run over
    (``_PUMPS``), the run ends: its tabs close and the lease's grace timer saves the browser once
    nothing else browses."""
    from websockets.sync.client import connect

    renewed = [time.monotonic()]

    def copy(source, target, renew: bool) -> None:
        with contextlib.suppress(Exception):  # either side closing ends the pump
            for message in source:
                target.send(message)
                if renew and time.monotonic() - renewed[0] >= SESSION_RENEW_S:
                    renewed[0] = time.monotonic()
                    threading.Thread(target=_renew_session_lease, daemon=True).start()
        for ws in (source, target):
            with contextlib.suppress(Exception):
                ws.close()

    try:
        with connect(door, max_size=None, open_timeout=10) as outer:
            with _LOCK:
                current = _PUMPS.get(run) is pump
                pump["outer"] = outer
            if not current:  # taken over while dialling
                return
            with connect(cdp_mux.url_for(run), max_size=None, open_timeout=10) as inner:
                back = threading.Thread(target=copy, args=(inner, outer, False), name="session-door-back", daemon=True)
                back.start()
                copy(outer, inner, True)
                back.join(5)
    except Exception as exc:  # noqa: BLE001 -- the class only: the door URL is a capability
        log.info("session door ended: %s", type(exc).__name__)
    finally:
        with _LOCK:
            current = _PUMPS.get(run) is pump
            if current:
                _PUMPS.pop(run)
                _SESSION_SINKS.pop(run, None)
        if current:
            _end_run(run)


def _session_sink(run: str, endpoint, channel):
    """publish(frame) of the session's tab to its own frames channel, as `workspace.session.screen`
    kind "image" (the Mac path's shape; seq in epoch ms like a Mac cast, so a restarted cast never goes
    backwards). None unless `channel` is that session's frames channel."""
    sid = run.removeprefix("session-")
    if not (isinstance(endpoint, str) and endpoint.startswith("https://") and isinstance(channel, str)
            and channel.startswith("/ws/") and channel.endswith(f"/session-frames/{sid}")):
        return None
    last = [float("-inf")]

    def publish(frame: dict) -> None:
        from bridge import turn  # it imports this module

        # At most one per SESSION_FRAME_INTERVAL_S: these go straight to AppSync, past the backend's
        # session frames budget. Waiting here (off the loop) holds the cast, whose latest frame wins.
        wait = last[0] + SESSION_FRAME_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()

        turn.publish_frame(endpoint, channel, {
            "schemaVersion": 1, "eventId": str(uuid.uuid4()), "type": "workspace.session.screen", "occurredAt": turn.now(),
            "payload": {"sessionId": sid, "kind": "image", "data": frame["jpegBase64"], "w": frame["w"], "h": frame["h"],
                        "seq": time.time_ns() // 1_000_000}})

    return publish


def session_frames(run_id: str, ttl: float) -> int:
    """A Browser tab's beat for a session browsing the cloud: cast its tab for ``ttl`` more seconds
    and resend the latest frame (``ttl`` 0: stop it now). 0 when it is not browsing here. Room runs'
    casts are untouched."""
    with _LOCK:
        sink = _SESSION_SINKS.get(run_id)
    if sink is None:
        return 0
    cdp_mux.cast(run_id, sink, min(max(ttl, 0.0), 120.0), True)
    return 1


def _end_run(run_id: str, upstream: str | None = None) -> None:
    global _timer
    with _LOCK:
        _SINKS.pop(run_id, None)
        tasks = _BROWSING.pop(run_id, None)
    if tasks is None:
        return
    for task_id in tasks:
        _disconnect(task_id)
    cdp_mux.close_run(run_id, upstream)
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
    """Turn end: close this run's tabs and clients; the lease goes once every run is done. A Mac
    is saved and given back here, synchronously: turn.py calls this before run.ended, while the
    run is still active (the door's re-check and every Mac route need it active)."""
    _TURN.set(None)
    if state:
        if state.get("mac"):
            with state["lock"]:  # waits for Stop's save, if Stop got there first
                _mac_release(state, MAC_SYNC_BUDGET_S)
        _end_run(state["runId"])


def abort(run_id: str) -> bool:
    """Stop: end the run's browser clients now, so an in-flight agent-browser command fails
    instead of running to its own timeout. A Mac still saves, within MAC_STOP_SAVE_S, off this
    thread; the turn's own release waits for it."""
    with _LOCK:
        state = _MAC_STATES.get(run_id) if run_id else None
        held = bool(run_id) and run_id in _BROWSING
    if state is not None and state["lock"].acquire(blocking=False):
        def stop() -> None:
            try:
                _mac_release(state, MAC_STOP_SAVE_S)
            finally:
                state["lock"].release()

        threading.Thread(target=stop, name="mac-stop-save", daemon=True).start()
    if held:  # a Mac run's tabs close after its save (_mac_release), not here
        _end_run(run_id, CLOUD if state is not None else None)
    return held or state is not None


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


def _sync_done(state: dict, budget: float) -> bool:
    """Wait (within the hook's budget) for the Mac's sign-in sync; False while it still runs."""
    running = state.get("macSync")
    if running is None:
        return True
    running.join(max(0.0, budget - time.monotonic()))
    if running.is_alive():
        return False
    state["macSync"] = None
    return True


def _unavailable(state: dict) -> dict:
    if state.get("error"):
        return {"action": "block", "message": state["error"]}
    return {"action": "block", "message": STARTING_MESSAGE if state.get("starting") else UNAVAILABLE}


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Hermes hook: acquire for browser calls, yield to a person, fence stale element refs."""
    # ponytail: substring match on the command; a proper hook when a second CLI needs the browser.
    if tool_name == "terminal" and "opencli" in str((args or {}).get("command", "")):
        state = _TURN.get()
        if state:  # best effort, never blocks the shell: opencli reports its own failure
            _refresh(state, time.monotonic() + HOOK_BUDGET_S)
        return None
    if not (str(tool_name).startswith("browser_") or tool_name == "computer_use"):
        return None
    state = _TURN.get()
    if not state:
        return {"action": "block", "message": UNAVAILABLE}
    if state.get("mac") and tool_name == "computer_use":
        return {"action": "block", "message": COMPUTER_USE_MAC}
    budget = time.monotonic() + HOOK_BUDGET_S
    if not _sync_done(state, budget - MAC_ACQUIRE_MIN_S):  # the acquire keeps its slice
        return {"action": "block", "message": MAC_SYNCING}
    if not _refresh(state, budget):
        return _unavailable(state)
    if not _sync_done(state, budget):  # this acquire started the Mac's sign-in sync
        return {"action": "block", "message": MAC_SYNCING}
    if not _yield_to_person(state, budget):
        return _unavailable(state)
    upstream = _upstream_of(state)
    with _LOCK:
        _BROWSING.setdefault(state["runId"], set()).add(str(kwargs.get("task_id") or "default"))
        if state.get("frameSink") is not None and upstream == CLOUD:
            _SINKS[state["runId"]] = state["frameSink"]
    if upstream == CLOUD:
        _cast(state["runId"], state.get("frameSink"))
    if tool_name in NO_REFS:
        return None
    with _LOCK:
        # The page may have changed under a person's hands since this turn last looked.
        if state.get("humanInputAt", 0) > state.get("snapshotAt", 0):
            state["snapshotGeneration"] = None
    if state.get("snapshotGeneration") != state.get("generation"):
        if tool_name not in SNAPSHOTS:
            return {"action": "block", "message": "The page may have changed (a person used the shared browser, "
                    "or the session changed). Take a fresh browser_snapshot before this action."}
        with _LOCK:
            state["pendingSnapshots"][str(kwargs.get("tool_call_id") or "")] = state["generation"]
    return None


def post_tool_call(tool_name: str = "", status: str = "", **kwargs) -> None:
    """Credit only a completed snapshot (or navigate) from the generation that authorized it."""
    if tool_name not in SNAPSHOTS:
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
    """The session the bridge holds: {browserIdentifier, sessionId}."""
    with _LOCK:
        url = _LEASE.get("cdpUrl") or ""
    match = re.search(r"/browser-streams/([^/]+)/sessions/([^/?]+)/", url)
    return {"browserIdentifier": match.group(1), "sessionId": match.group(2)} if match else None


def invoke(action: dict) -> dict:
    """One AWS InvokeBrowser action (real mouse/keyboard/screenshot) on the held session, for the
    screen driver. Here, not in the driver: the driver runs with the agent profile's env, whose
    AWS_* settings name the owner's connected account (or a profile that does not exist), not
    this runtime's role. A screenshot's bytes come back base64."""
    ref = session_ref()
    if not ref:
        raise RuntimeError("The shared browser is not running; use a browser tool first.")
    import base64

    import boto3

    # The execution role, on a shared runtime too: InvokeBrowser authorizes on the browser, never a
    # profile, so no per-owner credential narrows it (sessions are found only by id; only the
    # backend lists them), and the session policy has no room left under STS's 2,048 characters.
    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION") or "us-east-1")
    (_, out), = client.invoke_browser(**ref, action=action)["result"].items()
    if isinstance(out.get("data"), bytes):
        out = {**out, "data": base64.b64encode(out["data"]).decode()}
    return out
