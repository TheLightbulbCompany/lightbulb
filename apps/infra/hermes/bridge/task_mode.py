"""Task mode: this microVM is one task's own AgentCore session (spec 02 §3.3, contracts-v2 §15).

The first `/internal/v1/runs` create whose identity says `session.kind == "task"` LATCHES the
process to that task and agent. From then on the process restores the specialist's profile
`<owner>/hermes/<agent>/` read-only, layers and writes only the task overlay
`<owner>/hermes-tasks/<task>/<agent>/` (sync.read_prefixes / sync.write_prefix), never writes the
root `auth.json` or a rotated CLI credential back, never runs the curator, and refuses every other
kind of work: a room turn, a Paperclip run, a run of another task, a workspace-kind run. That is the
16 Sep invariant -- one writer per S3 profile prefix -- kept with a second microVM in the picture:
the overlay has one writer (this VM) and the specialist profile keeps its one (the workspace VM
plus the backend's merge).

Inert until a task-kind run arrives: `active()` is None and every helper answers today's values.
"""

from __future__ import annotations

import threading

import bridge

OVERLAY_ROOT = "hermes-tasks"

#: ``{"taskId", "agentId", "ownerId", "attempt"}`` once latched; None on a workspace VM.
TASK: dict | None = None
_lock = threading.Lock()


class Refused(Exception):
    """Work this task VM must not do. ``reply`` is the HTTP answer the bridge gives for it."""

    reply = (409, {"error": "task_mode"})


def active() -> dict | None:
    return TASK


def latch(identity: dict) -> None:
    """Claim the process for ``identity``'s task (first task-kind run), or re-affirm it for a later
    attempt of the same task; another task or agent is refused."""
    global TASK
    want = {
        "taskId": identity["taskId"],
        "agentId": identity["agentId"],
        "ownerId": identity["ownerId"],
        "attempt": int(identity.get("attempt") or 1),
    }
    with _lock:
        if TASK is None:
            TASK = want
            return
        if (TASK["taskId"], TASK["agentId"]) != (want["taskId"], want["agentId"]):
            raise Refused()
        TASK["attempt"] = max(TASK["attempt"], want["attempt"])  # a stale poll of an older attempt never regresses it


def admit(identity: dict) -> None:
    """A run create, poll, stop or steer: a task-kind identity latches (or matches) this VM; on a
    latched VM anything else is refused -- a workspace-kind run here would restore and write the
    specialist profile. A poll latches too: after a park the backend's next poll lands on a FRESH
    process, and an unlatched read would look in the specialist's ``.runs/`` and answer 404, which
    the backend reads as a lost run (the attempt re-dispatched after every park)."""
    kind = (identity.get("session") or {}).get("kind")
    if kind == "task":
        latch(identity)
    elif TASK is not None:
        raise Refused()


def check_agent(agent_id: str) -> None:
    """Only the latched agent's profile is ever restored or saved on a task VM."""
    if TASK is not None and TASK["agentId"] != agent_id:
        raise Refused()


def overlay_prefix() -> str:
    """``<owner>/hermes-tasks/<task>/<agent>`` (no trailing slash, like ``S3_PREFIX``)."""
    assert TASK is not None, "overlay_prefix outside task mode"
    return f"{bridge.OWNER}/{OVERLAY_ROOT}/{TASK['taskId']}/{TASK['agentId']}"
