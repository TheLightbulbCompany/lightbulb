"""Per-turn browser controller lease and local CDP discovery."""
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
    except (OSError, ValueError, urllib.error.HTTPError):
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


def _refresh(state: dict, task_id: str = "") -> bool:
    if not os.environ.get("AGENT_BROWSER_CDP"):
        return False
    result = _post("acquire", {"runId": state["runId"], "agentId": state["agentId"]}, timeout=10.0)
    if not result or not result.get("cdpUrl") or result.get("generation") is None:
        return False
    changed = state.get("generation") not in (None, result["generation"])
    if changed and not _disconnect(task_id):
        return False
    with _LOCK:
        state.update({"generation": result["generation"], "acquired": True,
                      "snapshotGeneration": None if changed else state.get("snapshotGeneration")})
        if changed:
            state["pendingSnapshots"].clear()
        _CURRENT.clear()
        _CURRENT.update({"cdpUrl": result["cdpUrl"],
                         "leaseExpiresAt": result.get("leaseExpiresAt", time.time() + 30), **state})
    return True


def release(state: dict | None) -> None:
    _TURN.set(None)
    if not state or not state.get("acquired"):
        return
    # The checkpoint helper must attach to the automation stream. AWS permits one automation
    # client, so reap this turn's generic CLI before asking the backend to checkpoint/release.
    if not _disconnect(str(state.get("taskId") or "default")):
        return
    _post("release", {"runId": state["runId"], "generation": state["generation"]}, timeout=190.0)
    with _LOCK:
        if _CURRENT.get("runId") == state["runId"] and _CURRENT.get("generation") == state["generation"]:
            _CURRENT.clear()


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Hermes hook: block browser calls unless this turn owns the lease."""
    if not str(tool_name).startswith("browser_"):
        return None
    state = _TURN.get()
    task_id = str(kwargs.get("task_id") or "default")
    if state:
        state["taskId"] = task_id
    if not state or not _refresh(state, task_id):
        return {"action": "block", "message": "Browser control is unavailable for this turn."}
    if state.get("snapshotGeneration") != state.get("generation"):
        if tool_name != "browser_snapshot":
            return {"action": "block", "message": "Refresh the browser snapshot before this action."}
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
