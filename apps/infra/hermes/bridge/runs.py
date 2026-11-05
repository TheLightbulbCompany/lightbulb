"""The generic run contract /internal/v1/runs (spec 02 §5.1): one Worker turn of a task.

Create, poll and stop run on the Paperclip run machinery in turn.py (idempotent create, the
durable S3 record a split session's other microVM answers from, heartbeat, stop), keyed by task
ids instead of Paperclip ids. The runtime never reads flags: the backend puts the mode on the
request, and only what this wave runs is accepted (Worker mode, the workspace's own session).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from threading import Event
import urllib.error
import urllib.request

from bridge import API, HOOK_TOKEN, OWNER, memory_ctx, task_mode, turn

log = logging.getLogger("bridge")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Must equal tasks-schema.json's worker session key pattern (tests/test_runs_v1.py checks it).
SESSION_KEY_RE = re.compile(r"^agent:[0-9a-f-]{36}:lightbulb:task:[0-9a-f-]{36}$")
#: Must equal tasks-schema.json's router session key pattern: one Router session per lane.
ROUTER_SESSION_KEY_RE = re.compile(r"^agent:[0-9a-f-]{36}:lightbulb:router:(shared|u:[0-9a-f]{16})(:r:[0-9a-f]{16})?$")
_RUN_RE = re.compile(r"^/internal/v1/runs/([0-9a-f-]{36})(/stop|/steer)?$")
MAX_INPUT_BYTES = 200 * 1024
#: worker_turn's limit, under the bridge's 7 h recycle (contracts-v2 §14.11).
MAX_RUN_BUDGET_S = 23_400

UNSUPPORTED = (400, {"error": "unsupported_in_this_version"})


class Refused(Exception):
    def __init__(self, code: int, body: dict):
        super().__init__(code)
        self.reply = (code, body)


def _invalid(what: str) -> Refused:
    return Refused(400, {"error": f"invalid_{what}"})


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def validate_identity(identity, *, path_run_id: str | None = None) -> dict:
    """The identity, or Refused: 400 malformed, 400 unsupported_in_this_version, 403 another owner."""
    if not isinstance(identity, dict):
        raise _invalid("run_identity")
    mode = identity.get("mode")
    if mode not in ("worker", "router") and isinstance(mode, str):
        raise Refused(*UNSUPPORTED)
    session = identity.get("session")
    kind = session.get("kind") if isinstance(session, dict) else None
    if isinstance(kind, str) and kind not in ("workspace", "task"):
        raise Refused(*UNSUPPORTED)
    if kind == "task" and mode != "worker":
        raise Refused(*UNSUPPORTED)  # a Router batch never runs in a task session (spec 02 §3.3)
    ok = (
        all(isinstance(identity.get(k), str) and _UUID_RE.fullmatch(identity[k]) for k in ("runId", "taskId", "agentId"))
        and isinstance(identity.get("ownerId"), str) and identity["ownerId"]
        and _positive_int(identity.get("attempt"))
        and session in ({"kind": "workspace"}, {"kind": "task"})
        and isinstance(identity.get("sessionKey"), str) and _session_key_ok(identity)
        and (path_run_id is None or identity["runId"] == path_run_id)
    )
    if not ok:
        raise _invalid("run_identity")
    if identity["ownerId"] != OWNER:
        raise Refused(403, {"error": "owner_mismatch"})
    return identity


def _session_key_ok(identity: dict) -> bool:
    """Worker: the task's own session. Router (taskId = the batch id): its lane's session in its room (`room`,
    `r:<hash>`), so a Router run never reads another room's conversation."""
    key = identity["sessionKey"]
    if identity.get("mode") == "router":
        lane, room = identity.get("lane"), identity.get("room")
        expected = f"agent:{identity['agentId']}:lightbulb:router:{lane}" + (f":{room}" if room else "")
        return bool(ROUTER_SESSION_KEY_RE.fullmatch(key)) and key == expected
    return identity.get("mode") == "worker" and bool(SESSION_KEY_RE.fullmatch(key)) and (
        key == f"agent:{identity['agentId']}:lightbulb:task:{identity['taskId']}")


def validate_request(request) -> dict:
    if not isinstance(request, dict):
        raise _invalid("run_request")
    effects = request.get("effects", "off")
    if effects not in ("off", "required"):
        raise _invalid("run_request")
    if effects == "required" and not turn.effects_supported():
        raise Refused(*UNSUPPORTED)  # no F1 in this image: the backend keeps today's path
    faults = request.get("faults", [])
    if not (isinstance(faults, list) and all(isinstance(f, str) and f for f in faults)):
        raise _invalid("run_request")
    limits = request.get("limits", {})
    ok = (
        isinstance(request.get("input"), str) and request["input"]
        and len(request["input"].encode()) <= MAX_INPUT_BYTES
        and isinstance(request.get("instructions"), str)
        and isinstance(limits, dict) and set(limits) <= {"max_iterations", "run_budget_seconds"}
        and all(_positive_int(v) for v in limits.values())
        and limits.get("run_budget_seconds", 0) <= MAX_RUN_BUDGET_S
        and ("memory" not in request or request["memory"] is None or memory_ctx.valid(request["memory"]))
    )
    if not ok:
        raise _invalid("run_request")
    return request


def _create(headers: dict, body) -> tuple[int, dict]:
    if not isinstance(body, dict):
        raise _invalid("run_identity")
    identity = validate_identity(body.get("identity"))
    request = validate_request(body.get("request"))
    task_mode.admit(identity)  # a task-kind run latches this VM to its task; anything else on a latched VM is 409
    # The attempt's secret rides only the thread's copy (like localSessionToken): never in RUNS or the
    # S3 run record, which other turns of the workspace can read.
    run_secret = identity.pop("runSecret", None)
    if not turn.reserve_turn():
        return 503, {"error": "recycling"}
    registered = False
    try:
        registration = turn.register_durable_run(identity, request, kind="task")
        if registration == "conflict":
            turn.release_turn()
            return 409, {"error": "idempotency_conflict"}
        registered = registration != "replayed"
        status = turn.durable_run_status(identity["runId"], identity, kind="task")
        if registration == "replayed":
            turn.release_turn()
            return 200, status
        token = next((v for k, v in headers.items() if k.lower() == "x-lightbulb-local-session-token"), None)
        threading.Thread(
            target=_run_and_report,
            args=(dict(identity, localSessionToken=token, runSecret=run_secret), dict(request)),
            daemon=True,
            name=f"task-run-{identity['runId']}",
        ).start()
    except Exception:
        if registered:
            turn.discard_run(identity["runId"])
        turn.release_turn()
        raise
    return 202, status


def _poll_or_stop(run_id: str, stop: bool, body) -> tuple[int, dict]:
    identity = validate_identity((body or {}).get("identity") if isinstance(body, dict) else None, path_run_id=run_id)
    task_mode.admit(identity)  # latches a fresh VM BEFORE the record read (its overlay's .runs/, not the specialist's)
    try:
        status = turn.durable_run_status(run_id, identity, kind="task")
    except Exception:  # noqa: BLE001 -- the durable-record read failed (an S3 blip)
        # Never 404: the activity reads 404 as "the run is lost" and starts another attempt.
        log.warning("task run status lookup failed run=%s", run_id, exc_info=True)
        return 503, {"error": "run_status_unavailable"}
    if status is None:
        return 404, {"error": "run_not_found"}
    if not stop or status["status"] in ("completed", "failed", "cancelled"):
        return 200, status
    if not turn.abort_run(run_id):
        latest = turn.durable_run_status(run_id, identity, kind="task")
        return (200, latest) if latest is not None else (404, {"error": "run_not_found"})
    return 202, {"run_id": run_id, "status": "stopping", "session_id": identity["sessionKey"]}


def _steer(run_id: str, body) -> tuple[int, dict]:
    """kind "user": the person's words (agent.steer). kind "input": a non-user nudge (the watchdog's),
    through F3's inject_input; refused on an image without it, never promoted to a user steer."""
    if not isinstance(body, dict):
        raise _invalid("run_identity")
    identity = validate_identity(body.get("identity"), path_run_id=run_id)
    task_mode.admit(identity)
    text, kind = body.get("text"), body.get("kind")
    if kind not in ("user", "input"):
        raise _invalid("steer")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > turn.MAX_FOLLOWUP_TEXT_BYTES:
        raise _invalid("steer")
    try:
        status = turn.durable_run_status(run_id, identity, kind="task")
    except Exception:  # noqa: BLE001 -- an S3 blip, as on a poll
        log.warning("task run status lookup failed run=%s", run_id, exc_info=True)
        return 503, {"error": "run_status_unavailable"}
    if status is None:
        return 404, {"error": "run_not_found"}
    return 200, {"accepted": turn.steer_run(run_id, text.strip(), kind=kind)}


def handle(path: str, method: str, headers: dict, body) -> tuple[int, dict] | None:
    """None when `path` is not a v1 run route. The caller has checked the gateway token."""
    try:
        if path == "/internal/v1/runs":
            return _create(headers, body) if method == "POST" else None
        match = _RUN_RE.fullmatch(path)
        if not match:
            return None
        action = match.group(2)
        if method != ("POST" if action else "GET"):
            return None
        if action == "/steer":
            return _steer(match.group(1), body)
        return _poll_or_stop(match.group(1), action == "/stop", body)
    except Refused as refused:
        return refused.reply
    except task_mode.Refused as refused:
        return refused.reply


# ---- run events: the backend's async worker_turn is completed by these (wave-2 O Task 4) ------------
HEARTBEAT_EVERY_S = 120.0
MAX_EVENT_OUTPUT = 65536  # the backend's Attempt.output cap
EVENT_ATTEMPTS = 6
EVENT_BACKOFF_S = 2.0


def _post_event(identity: dict, body: dict) -> int:
    """POST one run event; retries 5xx and network errors with backoff, never throws."""
    url = f"{API}/api/v1/internal/runtime/{OWNER}/runs/{identity['runId']}/events"
    payload = json.dumps({"taskId": identity["taskId"], "attempt": identity["attempt"],
                          "runSecret": identity.get("runSecret") or "", **body}).encode()
    status = 0
    for attempt in range(EVENT_ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if exc.code < 500 and exc.code != 409:  # 409: the backend has not stored the task token yet
                return exc.code
        except Exception:  # noqa: BLE001 -- a network error retries too
            status = 0
        time.sleep(EVENT_BACKOFF_S * (2 ** attempt))
    log.warning("run event %s for %s undelivered (status %s)", body.get("kind"), identity["runId"], status)
    return status


def _heartbeats(identity: dict, stop: Event) -> None:
    while not stop.wait(HEARTBEAT_EVERY_S):
        status = turn.durable_run_status(identity["runId"], identity, kind="task") or {}
        if status.get("status") not in ("queued", "running", "stopping"):
            return
        _post_event(identity, {"kind": "heartbeat", "status": status["status"],
                               "recentTools": status.get("recentTools") or []})


def _run_and_report(identity: dict, request: dict) -> None:
    """Run the turn; heartbeat the backend while it runs and post its terminal status once
    (`completed | failed | cancelled | waiting_approval`). Router runs are polled by `router_turn`, not
    completed by events: they post none."""
    events = identity.get("mode") == "worker" and bool(API)
    stop = Event()
    if events:
        threading.Thread(target=_heartbeats, args=(identity, stop), daemon=True,
                         name=f"task-hb-{identity['runId']}").start()
    def terminal() -> None:
        stop.set()
        if events:
            status = turn.durable_run_status(identity["runId"], identity, kind="task") or {}
            output, files = status.get("output") or "", []
            if identity.get("taskId"):  # a Worker's result files go to its task room (never a raw MEDIA: path)
                try:
                    output, files = turn.task_result_media(identity["agentId"], identity["taskId"], output)
                except Exception:  # noqa: BLE001 -- the result text still goes up
                    log.warning("task result media failed run=%s", identity["runId"], exc_info=True)
            _post_event(identity, {"kind": "terminal", "status": status.get("status") or "failed",
                                   "output": output[:MAX_EVENT_OUTPUT] or None,
                                   "error": status.get("error"), "usage": status.get("usage"),
                                   **({"attachments": files} if files else {})})

    try:
        turn.run_task_turn(identity, request, True, on_finished=terminal)
    finally:
        stop.set()
