"""Delegation-completion drain: wake the agent when an async `delegate_task` finishes.

Hermes pushes a finished background delegation onto `tools.process_registry.completion_queue`.
Upstream's gateway drains that queue (gateway/run_notifications.py `_async_delegation_watcher`);
this bridge replaced the gateway and never did, so the agent was never woken and the result was
lost. Here each session's completions become ONE POST to the backend's follow-up route, which
comes back through the work queue as an ordinary room turn whose input is the completion text.

Started lazily by the first room turn (`ensure_started`), never at import: warm-pool microVMs
import this module and must do nothing session-bound before their first /invocations.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from bridge import API, HOOK_TOKEN, OWNER, turn

log = logging.getLogger("bridge")

POLL_S = 2.0
CONSUMER = "lightbulb-bridge"
# Upstream's own batch header (run_notifications.py `_deliver_async_delegation_group`), verbatim.
_BATCH_HEADER = (
    "[IMPORTANT: {n} background subagent delegations completed for this session. Treat these results "
    "as one completion batch and send at most one consolidated user-facing response. If a result does "
    "not change the current conclusion, absorb it silently.]"
)

_started = False
_start_lock = threading.Lock()
_warned_missing = False
# Delegation events re-queued after a retryable POST failure on the last pass: /ping stays busy
# until they are delivered or the ledger's own attempt budget drops them.
_retrying = 0
# Retryable failures per delivery key. Durable completions also spend the ledger's own budget; an
# interim notice (and a legacy event) has none, so without this a sustained 5xx re-queues it every
# POLL_S forever and /ping never idles.
# ponytail: in-memory, per process -- a restart forgets the count, and the queue with it.
MAX_ATTEMPTS = 8
_attempts: dict[str, int] = {}


#: Worker session key -> {"taskId", "runSecret"} of its latest run (bridge/turn._run_task_turn). In
#: memory only, like the thread's identity copy: never in RUNS or the S3 run record.
#: ponytail: never cleared -- one small entry per Worker session the process ever ran (a task VM runs
#: one; a shared VM is recycled within 7 h). Pop on run end if a long-lived shared VM shows growth.
_TASKS: dict[str, dict] = {}


def remember_task(session_key: str, task_id: str, run_secret: str) -> None:
    _TASKS[session_key] = {"taskId": task_id, "runSecret": run_secret}


def _post_input(body: dict) -> int:
    """One POST to the task's `/inputs` (backend: a `job_result` trigger + a signal to the task's
    workflow). The drain's own pass is the retry loop; never throws."""
    req = urllib.request.Request(
        f"{API}/api/v1/internal/runtime/{OWNER}/inputs", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 -- network: retried by the next pass
        return 0


def ensure_started() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="delegation-drain", daemon=True).start()


def _loop() -> None:
    while True:
        time.sleep(POLL_S)
        try:
            drain_once()
        except Exception:  # noqa: BLE001 -- the drain must outlive any one bad pass
            log.warning("delegation drain pass failed", exc_info=True)


def busy() -> bool:
    """A live delegation, or a completion still owed to the backend. Only consults Hermes if a
    turn already imported it -- no turn in this process means no delegation, and /ping must not
    import Hermes (and open a state.db) on a warm-pool microVM.

    Bounded, so an idle microVM still parks: `_retrying` by MAX_ATTEMPTS, and a wedged delegation
    by Hermes's own stale monitor (async_delegation: no progress for 450 s, or 1200 s inside a
    tool, then a 120 s grace, then force-finalized). Only a delegation still making progress
    holds the session busy, which is the point; the 7 h recycle's hard fallback bounds even that."""
    module = sys.modules.get("tools.async_delegation")
    try:
        live = int(module.active_count()) if module is not None else 0
    except Exception:  # noqa: BLE001 -- a renamed symbol degrades to "not busy", never a 500
        live = 0
    return live > 0 or _retrying > 0


def _hermes():
    """(registry, async_delegation, formatter), or None outside the Hermes image."""
    global _warned_missing
    try:
        from tools import async_delegation
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import format_process_notification
    except Exception:  # noqa: BLE001 -- ImportError or a renamed symbol: log once, never crash
        if not _warned_missing:
            _warned_missing = True
            log.warning("delegation drain: Hermes delegation modules unavailable", exc_info=True)
        return None
    return process_registry, async_delegation, format_process_notification


def drain_once() -> None:
    global _retrying
    hermes = _hermes()
    if hermes is None:
        return
    registry, ledger, fmt = hermes
    q = registry.completion_queue
    events, requeue = [], []
    while True:
        try:
            evt = q.get_nowait()
        except Exception:  # queue.Empty
            break
        (events if evt.get("type") == "async_delegation" else requeue).append(evt)
    # Process completions/watch events: out of scope. Nothing else in the bridge consumes them, so
    # they accumulate (pre-existing behaviour, not introduced by the drain).
    for evt in requeue:
        q.put(evt)
    # One follow-up per (session, originating turn): the turn that started the delegation, else the
    # session's latest turn that ran Hermes.
    groups: dict[tuple, tuple[str, dict | None, list[dict]]] = {}
    for evt in events:
        session_key = evt.get("session_key") or evt.get("parent_session_id") or ""
        last = turn.DELEGATION_TURN.get(str(evt.get("delegation_id") or "")) or turn.LAST_TURN.get(session_key)
        group_key = (session_key, (last or {}).get("triggerMessageId"))
        groups.setdefault(group_key, (session_key, last, []))[2].append(evt)
    retrying = 0
    for session_key, last, group in groups.values():
        try:
            failed = _deliver(session_key, last, group, ledger, fmt)
        except Exception:  # noqa: BLE001 -- they were dequeued: put them back rather than lose them
            log.warning("delegation drain: session %s failed", session_key, exc_info=True)
            failed = group
        for evt in failed:
            key = _delivery_key(evt)
            _attempts[key] = _attempts.get(key, 0) + 1
            if _attempts[key] >= MAX_ATTEMPTS:
                _attempts.pop(key, None)
                _forget(evt)
                log.warning("delegation drain: %s failed %d times; dropping", key, MAX_ATTEMPTS)
                continue
            q.put(evt)
            retrying += 1
    _retrying = retrying


def _agent_from_session_key(session_key: str) -> str | None:
    match = re.match(r"^agent:([^:]+):", session_key)
    return match.group(1) if match else None


def _delivery_key(evt: dict) -> str:
    # An interim task-failure notice shares its batch's delegation_id; keep its followUpId apart
    # from the final result's -- and from its siblings' (one notice per failed task) -- or the
    # backend would replay it away as a duplicate.
    did = str(evt.get("delegation_id") or "")
    if not evt.get("task_failure_notice"):
        return did
    task = (evt.get("results") or [{}])[0].get("task_index")
    return f"{did}:notice:{task if task is not None else evt.get('completed_at')}"


def _forget(evt: dict) -> None:
    """The final result is done with: drop its turn mapping (an interim notice leaves it for the result)."""
    if not evt.get("task_failure_notice"):
        turn.DELEGATION_TURN.pop(str(evt.get("delegation_id") or ""), None)


def _drop(ledger, evt: dict, claim: str) -> None:
    _forget(evt)
    _attempts.pop(_delivery_key(evt), None)
    if claim:
        ledger.drop_completion_delivery(str(evt.get("delegation_id") or ""), claim)


def _text(claimed: list[tuple[dict, str]], fmt) -> str:
    blocks = [text for evt, _claim in claimed if (text := fmt(evt))]
    if len(blocks) > 1:
        blocks.insert(0, _BATCH_HEADER.format(n=len(blocks)))
    return "\n\n".join(blocks).encode()[:turn.MAX_FOLLOWUP_TEXT_BYTES].decode("utf-8", "ignore")


def _settle(status: int, claimed: list[tuple[dict, str]], ledger, what: str) -> list[dict]:
    """2xx delivered, 4xx final (dropped), else released for the next pass. Returns the retries."""
    if 200 <= status < 300:
        for evt, claim in claimed:
            ledger.complete_event_delivery(evt, claim)
            _forget(evt)
            _attempts.pop(_delivery_key(evt), None)
        log.info("delegation %s -> %s (%d events)", what, status, len(claimed))
        return []
    if 400 <= status < 500:  # final: unknown room/agent/trigger/task, malformed, capped, or old backend
        for evt, claim in claimed:
            _drop(ledger, evt, claim)
        log.warning("delegation %s refused %s; dropped %d", what, status, len(claimed))
        return []
    for evt, claim in claimed:  # 5xx or network: the ledger counts the attempt and may drop it
        ledger.release_event_delivery(evt, claim)
    return [evt for evt, _claim in claimed]


def _deliver_to_task(session_key: str, task: dict, claimed: list[tuple[dict, str]], ledger, fmt) -> list[dict]:
    """A Worker session's completions become one task input (`delegation_completed`)."""
    ids = ",".join(sorted(_delivery_key(evt) for evt, _claim in claimed))
    text = _text(claimed, fmt)
    body = {
        "taskId": task["taskId"], "sessionKey": session_key, "kind": "delegation_completed",
        "jobId": ids[:256], "text": text, "runSecret": task["runSecret"],
        "deliveryId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-task-input:{ids}")),
    }
    return _settle(_post_input(body) if text else 200, claimed, ledger, f"task input {body['deliveryId']}")


def _deliver(session_key: str, last: dict | None, group: list[dict], ledger, fmt) -> list[dict]:
    """Claim, format and POST one session's completions for one originating turn. Returns the
    events to retry."""
    agent_id = (last or {}).get("agentId") or _agent_from_session_key(session_key)
    # The durable ledger is <HERMES_HOME>/state.db, and the turn that dispatched the delegation ran
    # under its PROFILE's home -- outside that scope every claim hits the root state.db, finds no
    # row, and "succeeds" as a legacy event, which silently disables the whole ledger.
    scope = turn._hermes_home_scope(agent_id) if agent_id else contextlib.nullcontext()
    with scope:
        claimed = [(evt, claim) for evt in group
                   if (claim := ledger.claim_event_delivery(evt, CONSUMER)) is not None]
        if not claimed:
            return []  # another consumer owns every row
        task = _TASKS.get(session_key) if last is None else None
        if task is not None:
            return _deliver_to_task(session_key, task, claimed, ledger, fmt)
        if last is None:
            # ponytail: only room sessions are woken; mail/Paperclip delegations are dropped (gap list).
            log.warning("delegation drain: session %s has no room turn in this process; dropping %d",
                        session_key, len(claimed))
            for evt, claim in claimed:
                _drop(ledger, evt, claim)
            return []
        text = _text(claimed, fmt)
        ids = ",".join(sorted(_delivery_key(evt) for evt, _claim in claimed))
        body = {
            "followUpId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-followup:{ids}")),
            "roomId": last["roomId"], "agentId": last["agentId"],
            "triggerMessageId": last["triggerMessageId"], "eventKind": last["eventKind"],
            "reason": "delegation_completed", "text": text,
        }
        status = turn._post_backend(f"followups/{OWNER}", body) if text else 200
        return _settle(status, claimed, ledger, f"follow-up {body['followUpId']}")
