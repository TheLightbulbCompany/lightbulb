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
import logging
import re
import sys
import threading
import time
import uuid

from bridge import OWNER, turn

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
    import Hermes (and open a state.db) on a warm-pool microVM."""
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
    for evt in requeue:  # process completions/watch events: out of scope, left for their owners
        q.put(evt)
    groups: dict[str, list[dict]] = {}
    for evt in events:
        groups.setdefault(evt.get("session_key") or evt.get("parent_session_id") or "", []).append(evt)
    retrying = 0
    for session_key, group in groups.items():
        try:
            failed = _deliver(session_key, group, ledger, fmt)
        except Exception:  # noqa: BLE001 -- they were dequeued: put them back rather than lose them
            log.warning("delegation drain: session %s failed", session_key, exc_info=True)
            failed = group
        for evt in failed:
            q.put(evt)
        retrying += len(failed)
    _retrying = retrying


def _agent_from_session_key(session_key: str) -> str | None:
    match = re.match(r"^agent:([^:]+):", session_key)
    return match.group(1) if match else None


def _delivery_key(evt: dict) -> str:
    # An interim task-failure notice shares its batch's delegation_id; keep its followUpId apart
    # from the final result's or the backend would replay the result away as a duplicate.
    did = str(evt.get("delegation_id") or "")
    return f"{did}:notice" if evt.get("task_failure_notice") else did


def _drop(ledger, evt: dict, claim: str) -> None:
    if claim:
        ledger.drop_completion_delivery(str(evt.get("delegation_id") or ""), claim)


def _deliver(session_key: str, group: list[dict], ledger, fmt) -> list[dict]:
    """Claim, format and POST one session's completions. Returns the events to retry."""
    last = turn.LAST_TURN.get(session_key)
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
        if last is None:
            # ponytail: only room sessions are woken; mail/Paperclip delegations are dropped (gap list).
            log.warning("delegation drain: session %s has no room turn in this process; dropping %d",
                        session_key, len(claimed))
            for evt, claim in claimed:
                _drop(ledger, evt, claim)
            return []
        blocks = [text for evt, _claim in claimed if (text := fmt(evt))]
        if len(blocks) > 1:
            blocks.insert(0, _BATCH_HEADER.format(n=len(blocks)))
        text = "\n\n".join(blocks).encode()[:turn.MAX_FOLLOWUP_TEXT_BYTES].decode("utf-8", "ignore")
        ids = ",".join(sorted(_delivery_key(evt) for evt, _claim in claimed))
        body = {
            "followUpId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-followup:{ids}")),
            "roomId": last["roomId"], "agentId": last["agentId"],
            "triggerMessageId": last["triggerMessageId"], "eventKind": last["eventKind"],
            "reason": "delegation_completed", "text": text,
        }
        status = turn._post_backend(f"followups/{OWNER}", body) if text else 200
        if 200 <= status < 300:
            for evt, claim in claimed:
                ledger.complete_event_delivery(evt, claim)
            log.info("delegation follow-up %s -> %s (%d events)", body["followUpId"], status, len(claimed))
            return []
        if 400 <= status < 500:  # final: unknown room/agent/trigger, malformed, capped, or old backend
            for evt, claim in claimed:
                _drop(ledger, evt, claim)
            log.warning("delegation follow-up %s refused %s; dropped %d", body["followUpId"], status, len(claimed))
            return []
        for evt, claim in claimed:  # 5xx or network: the ledger counts the attempt and may drop it
            ledger.release_event_delivery(evt, claim)
        return [evt for evt, _claim in claimed]
