"""Per-turn browser lease, local CDP discovery, and the agent's side of the shared screen.

People use the workspace browser at the same time as agents, straight through the live view.
The agent yields: when a person acted in the last YIELD_QUIET_S, its next browser action
waits until they have been quiet that long, and any action after a person's input needs a
fresh snapshot first. Page-script evaluation is refused outright, because people sign in in
this browser and a script can read what they type (a password field's value included).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextvars import ContextVar

from bridge import API, HOOK_TOKEN, OWNER

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
_CURRENT: dict = {}
_LOCK = threading.Lock()
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
    except urllib.error.HTTPError as exc:
        # 409 = another agent run holds the lease: say so, not "unavailable".
        return {"conflict": True} if exc.code == 409 else None
    except (OSError, ValueError):
        return None


def begin_turn(run_id: str, agent_id: str) -> dict:
    """Bind identity only; the first browser tool lazily acquires the lease."""
    state = {"runId": str(run_id or f"browser:{agent_id}:{uuid.uuid4().hex}"), "agentId": agent_id,
             "generation": None, "acquired": False, "snapshotGeneration": None,
             "pendingSnapshots": {}, "taskId": "default"}
    _TURN.set(state)
    return state


def _disconnect(task_id: str) -> bool:
    try:
        from tools.browser_tool_lifecycle import _force_reap_browser_session
        _force_reap_browser_session(task_id or "default")
        return True
    except Exception:
        return False


def _refresh(state: dict, task_id: str = "", deadline: float | None = None) -> bool:
    if not os.environ.get("AGENT_BROWSER_CDP"):
        return False
    deadline = time.monotonic() + HOOK_BUDGET_S if deadline is None else deadline
    body = {"runId": state["runId"], "agentId": state["agentId"]}
    result = _post("acquire", body, timeout=max(1.0, deadline - time.monotonic()))
    while result and result.get("status") == "starting" and deadline - time.monotonic() > START_POLL_S + 1:
        time.sleep(START_POLL_S)
        result = _post("acquire", body, timeout=max(1.0, deadline - time.monotonic()))
    state["starting"] = bool(result and result.get("status") == "starting")
    state["inUse"] = bool(result and result.get("conflict"))
    if not result or not result.get("cdpUrl") or result.get("generation") is None:
        return False
    state["humanInputAt"] = float(result.get("humanInputAt") or 0)
    now = time.time()
    with _LOCK:
        cached = dict(_CURRENT)
    same = state.get("generation") == result["generation"] and cached.get("runId") == state["runId"]
    reuse_url = same and cached.get("urlExpiresAt", 0) > now
    # agent-browser hashes the entire CDP URL; changing its signature relaunches the
    # connection and invalidates element refs. Refresh only with a fresh snapshot.
    changed = state.get("generation") is not None and not reuse_url
    if changed and not _disconnect(task_id):
        return False
    with _LOCK:
        state.update({"generation": result["generation"], "acquired": True,
                      "snapshotGeneration": None if changed else state.get("snapshotGeneration")})
        if changed:
            state["pendingSnapshots"].clear()
        _CURRENT.clear()
        _CURRENT.update({"cdpUrl": cached["cdpUrl"] if reuse_url else result["cdpUrl"],
                         "urlExpiresAt": cached["urlExpiresAt"] if reuse_url else now + 240,
                         "leaseExpiresAt": result.get("leaseExpiresAt", now + 30), **state})
    return True


def renew() -> None:
    """Keep this turn's lease while a vault tool waits on a person (website_logins._ask): the
    lease lapses after the backend's LEASE_SECONDS, and a lapsed lease hands the browser away
    mid-login. Only the lease -- the CDP URL and the connection are left alone."""
    state = _TURN.get()
    with _LOCK:
        holding = bool(state and state.get("acquired") and _CURRENT.get("runId") == state["runId"])
    if holding:
        _post("acquire", {"runId": state["runId"], "agentId": state["agentId"]}, timeout=30.0)


def release(state: dict | None) -> None:
    _TURN.set(None)
    if not state or not state.get("acquired"):
        return
    with _LOCK:
        state["acquired"] = False  # a tool worker's copied context still holds this dict (renew)
    # The checkpoint helper must attach to the automation stream. AWS permits one automation
    # client, so reap this turn's generic CLI before asking the backend to checkpoint/release.
    if not _disconnect(str(state.get("taskId") or "default")):
        return
    _post("release", {"runId": state["runId"], "generation": state["generation"]}, timeout=190.0)
    with _LOCK:
        if _CURRENT.get("runId") == state["runId"] and _CURRENT.get("generation") == state["generation"]:
            _CURRENT.clear()


def abort(run_id: str) -> bool:
    """Stop: reap the browser session a stopped run holds, so its in-flight agent-browser
    command fails now. The lease itself is released by the turn's own `release` on its way out."""
    with _LOCK:
        held = _CURRENT.get("runId") == run_id and bool(run_id)
        task_id = str(_CURRENT.get("taskId") or "default")
    return held and _disconnect(task_id)


def _reads_page_script(tool_name: str, args: dict | None) -> bool:
    # browser_console without an expression only reads console messages; with one it runs
    # JS in the page, and browser_cdp is raw CDP (Runtime.evaluate and friends).
    return tool_name == "browser_cdp" or (tool_name == "browser_console" and bool((args or {}).get("expression")))


def _yield_to_person(state: dict, task_id: str, budget: float | None = None) -> bool:
    """Wait until a person has been quiet for YIELD_QUIET_S (bounded by YIELD_MAX_S and the
    hook's budget); False if the lease is lost."""
    deadline = time.monotonic() + YIELD_MAX_S
    if budget is not None:
        deadline = min(deadline, budget)
    while True:
        quiet = time.time() - state.get("humanInputAt", 0)
        if quiet >= YIELD_QUIET_S or time.monotonic() >= deadline:
            return True
        time.sleep(min(YIELD_QUIET_S - quiet, max(0.0, deadline - time.monotonic())))
        if not _refresh(state, task_id, budget):  # also re-reads humanInputAt
            return False


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Hermes hook: block browser calls unless this turn owns the lease; yield to a person."""
    if not str(tool_name).startswith("browser_"):
        return None
    if _reads_page_script(str(tool_name), args):
        return {"action": "block", "message": (
            "Running scripts in the shared browser is disabled: people sign in there and a page script "
            "can read what they type. Use browser_snapshot, browser_vision or browser_console without an expression."
        )}
    state = _TURN.get()
    task_id = str(kwargs.get("task_id") or "default")
    if state:
        state["taskId"] = task_id
    budget = time.monotonic() + HOOK_BUDGET_S
    if not state or not _refresh(state, task_id, budget) or not _yield_to_person(state, task_id, budget):
        if state and state.get("starting"):
            return {"action": "block", "message": STARTING_MESSAGE}
        if state and state.get("inUse"):
            return {"action": "block", "message": "Another agent run is using the shared browser; try again shortly."}
        return {"action": "block", "message": "Browser control is unavailable for this turn."}
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


def discovery_payload() -> dict | None:
    """Return CDP metadata only to loopback HTTP clients."""
    with _LOCK:
        current = dict(_CURRENT)
    url = current.get("cdpUrl")
    try:
        expires = float(current.get("leaseExpiresAt", 0))
    except (TypeError, ValueError):
        expires = 0
    if not url or expires <= time.time():
        return None
    return {"Browser": "Chrome", "Protocol-Version": "1.3", "webSocketDebuggerUrl": url}
