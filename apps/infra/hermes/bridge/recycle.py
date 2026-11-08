"""Recycle the session before AgentCore's 8 h maxLifetime SIGTERMs it mid-turn.

The lifetime clock starts when the session claims this microVM, i.e. at the first /invocations,
not at process start (a warm-pool microVM can sit booted for hours first). After RECYCLE_AFTER_S
the bridge waits for an idle moment (no turn, no live delegation), holds new turns (a TTL'd
refusal: 503 -> the room queue retries, Paperclip reads it as transient) and asks the backend to
stop the session. The stop SIGTERMs this process, which saves every profile; the retried turn
cold-starts a fresh microVM. RECYCLE_HARD_S stops it even when busy, still ahead of the kill.

Low disk recycles too: a fresh microVM is the only reclaim (see sync.disk_free_mb). Below
DISK_SOFT_MB it waits for idle; below DISK_HARD_MB it stops even a busy session.

Prod 2026-09-25 21:00:04: maxLifetime killed the founder's session with a room run and a
Paperclip run in flight (43a12a76 swept, f686a67e runtime_lost).
"""
from __future__ import annotations

import logging
import os
import threading
import time

import bridge
from bridge import sync, turn

log = logging.getLogger("bridge")

RECYCLE_AFTER_S = float(os.environ.get("LIGHTBULB_RECYCLE_AFTER_S") or 7 * 3600)
RECYCLE_HARD_S = float(os.environ.get("LIGHTBULB_RECYCLE_HARD_S") or 7 * 3600 + 45 * 60)
# ponytail: calibration knobs; the outer had ~4.1 GB free at idle on dev (10-08).
DISK_SOFT_MB = int(os.environ.get("LIGHTBULB_RECYCLE_DISK_SOFT_MB") or 1000)
DISK_HARD_MB = int(os.environ.get("LIGHTBULB_RECYCLE_DISK_HARD_MB") or 500)
CHECK_S = 30.0
#: A stop the backend confirmed but that did not kill THIS process (a split session: it stopped
#: the other microVM) is not repeated at once -- each repeat would kill a fresh session.
RETRY_AFTER_STOP_S = 600.0

first_invoke_at: float | None = None
_next_try = 0.0
_lock = threading.Lock()


def note_invoke() -> None:
    """The first /invocations claims the session: start the lifetime clock and the checker."""
    global first_invoke_at
    with _lock:
        if first_invoke_at is not None:
            return
        first_invoke_at = time.time()
    threading.Thread(target=_loop, name="recycle", daemon=True).start()


def _loop() -> None:
    while True:
        time.sleep(CHECK_S)
        try:
            check_once()
        except Exception:  # noqa: BLE001 -- the checker must outlive any one bad pass
            turn.release_hold()
            log.warning("recycle check failed", exc_info=True)


def check_once() -> bool:
    """One pass. True when the backend accepted a stop request."""
    global _next_try
    if time.monotonic() < _next_try:
        return False
    age = time.time() - (first_invoke_at or time.time())
    outer, inner = sync.disk_free_mb()
    disk = "hard" if min(outer, inner) < DISK_HARD_MB else "soft" if min(outer, inner) < DISK_SOFT_MB else ""
    if age < RECYCLE_AFTER_S and not disk:
        return False
    hard = age >= RECYCLE_HARD_S or disk == "hard"
    if not turn.hold_turns(force=hard):
        return False  # busy: the next idle pass takes it
    if disk:
        log.warning("recycle owner=%s: disk low (outer %d MB, inner %d MB free; %s floor); asking the "
                    "backend to stop it", bridge.OWNER, outer, inner, disk)
    else:
        log.info("recycle: session age %.0fs%s; asking the backend to stop it", age, " (hard)" if hard else "")
    status = turn._post_backend(f"runtime/{bridge.OWNER}/recycle", {"ageS": round(age)})
    if not 200 <= status < 300:
        turn.release_hold()  # a failed stop must not wedge the workspace; retry next pass
        log.warning("recycle: stop request -> %s; turns re-admitted", status)
        return False
    turn.hold_turns(force=True)  # refresh: the SIGTERM should land inside the hold
    _next_try = time.monotonic() + RETRY_AFTER_STOP_S
    return True
