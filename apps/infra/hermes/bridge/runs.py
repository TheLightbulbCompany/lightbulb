"""The generic run contract /internal/v1/runs (spec 02 §5.1): one Worker turn of a task.

Create, poll and stop run on the Paperclip run machinery in turn.py (idempotent create, the
durable S3 record a split session's other microVM answers from, heartbeat, stop), keyed by task
ids instead of Paperclip ids. The runtime never reads flags: the backend puts the mode on the
request, and only what this wave runs is accepted (Worker mode, the workspace's own session).
"""
from __future__ import annotations

import logging
import re
import threading

from bridge import OWNER, turn

log = logging.getLogger("bridge")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Must equal tasks-schema.json's worker session key pattern (tests/test_runs_v1.py checks it).
SESSION_KEY_RE = re.compile(r"^agent:[0-9a-f-]{36}:lightbulb:task:[0-9a-f-]{36}$")
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
    if identity.get("mode") != "worker" and isinstance(identity.get("mode"), str):
        raise Refused(*UNSUPPORTED)
    session = identity.get("session")
    if isinstance(session, dict) and session.get("kind") != "workspace" and isinstance(session.get("kind"), str):
        raise Refused(*UNSUPPORTED)
    ok = (
        all(isinstance(identity.get(k), str) and _UUID_RE.fullmatch(identity[k]) for k in ("runId", "taskId", "agentId"))
        and isinstance(identity.get("ownerId"), str) and identity["ownerId"]
        and _positive_int(identity.get("attempt"))
        and identity.get("mode") == "worker" and session == {"kind": "workspace"}
        and isinstance(identity.get("sessionKey"), str) and SESSION_KEY_RE.fullmatch(identity["sessionKey"])
        and identity["sessionKey"] == f"agent:{identity['agentId']}:lightbulb:task:{identity['taskId']}"
        and (path_run_id is None or identity["runId"] == path_run_id)
    )
    if not ok:
        raise _invalid("run_identity")
    if identity["ownerId"] != OWNER:
        raise Refused(403, {"error": "owner_mismatch"})
    return identity


def validate_request(request) -> dict:
    if not isinstance(request, dict):
        raise _invalid("run_request")
    if request.get("effects", "off") != "off":
        raise Refused(*UNSUPPORTED)
    limits = request.get("limits", {})
    ok = (
        isinstance(request.get("input"), str) and request["input"]
        and len(request["input"].encode()) <= MAX_INPUT_BYTES
        and isinstance(request.get("instructions"), str)
        and isinstance(limits, dict) and set(limits) <= {"max_iterations", "run_budget_seconds"}
        and all(_positive_int(v) for v in limits.values())
        and limits.get("run_budget_seconds", 0) <= MAX_RUN_BUDGET_S
    )
    if not ok:
        raise _invalid("run_request")
    return request


def _create(headers: dict, body) -> tuple[int, dict]:
    if not isinstance(body, dict):
        raise _invalid("run_identity")
    identity = validate_identity(body.get("identity"))
    request = validate_request(body.get("request"))
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
            target=turn.run_task_turn,
            args=(dict(identity, localSessionToken=token), dict(request), True),
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
