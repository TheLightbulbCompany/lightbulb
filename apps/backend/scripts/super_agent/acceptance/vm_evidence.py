"""Wave-2 gate (G2 step 3): per-task VM evidence (plans/wave2-V Task 17).

Every Attempt of a task in ``sa-dev-on`` (API-key model provider, so per-task VMs) must have run
on that task's own AgentCore session, and every Attempt of a task in ``sa-dev-chatgpt`` (ChatGPT
login, shared VM until the broker cutover) on the workspace session. ``begin_attempt``
(core/temporal/activities.py) records the AgentCore session id it dispatched to as the Attempt's
``sessionId`` -- ``runtime_transport.session_id_for(ws, task_id)`` for a task session,
``session_id_for(ws)`` for the workspace's -- so the evidence is those rows against those ids.

Pure over ``ctx.tasks_rows(ws)`` (G1's dev-only read of the tasks table); no network. Session ids
are not secrets; task ids and session ids are the evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from core.services.runtime_transport import session_id_for


class Ctx(Protocol):
    async def tasks_rows(self, ws: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Result:
    """G1's ``Result(passed, evidence)``; the gate reads ``evidence["failures"]`` when not passed."""

    passed: bool
    evidence: dict[str, Any]


def _attempts(rows: Iterable[dict[str, Any]], task_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    wanted = set(task_ids)
    out: dict[str, list[dict[str, Any]]] = {tid: [] for tid in wanted}
    for row in rows:
        if row.get("type") == "attempt" and row.get("taskId") in wanted:
            out[row["taskId"]].append(row)
    return out


async def vm_evidence(
    ctx: Ctx,
    *,
    ws_on: str,
    task_ids_on: list[str],
    ws_chatgpt: str,
    task_ids_chatgpt: list[str],
) -> Result:
    """Passes iff every Attempt of ``task_ids_on`` in ``ws_on`` carries that task's own session id
    (no two tasks share one), and every Attempt of ``task_ids_chatgpt`` in ``ws_chatgpt`` carries
    the workspace session id. A task with no Attempt is a failure: there is no evidence."""
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    for label, ws, task_ids, per_task in (
        ("on", ws_on, task_ids_on, True),
        ("chatgpt", ws_chatgpt, task_ids_chatgpt, False),
    ):
        by_task = _attempts(await ctx.tasks_rows(ws), task_ids)
        seen: dict[str, str] = {}  # session id -> first task seen on it
        evidence[label] = {"ws": ws, "attempts": []}
        for tid in task_ids:
            if not by_task[tid]:
                failures.append(f"{label}:{tid}: no attempt rows")
            for att in sorted(by_task[tid], key=lambda a: int(a.get("n") or 0)):
                expected = session_id_for(ws, tid if per_task else None)
                sid = att.get("sessionId")
                evidence[label]["attempts"].append({"taskId": tid, "n": att.get("n"), "sessionId": sid})
                if sid != expected:
                    kind = "task" if per_task else "workspace"
                    failures.append(
                        f"{label}:{tid}#{att.get('n')}: sessionId {sid} is not the {kind} session {expected}"
                    )
                if per_task and sid in seen and seen[sid] != tid:
                    failures.append(f"{label}:{tid}#{att.get('n')}: shares session {sid} with task {seen[sid]}")
                if sid is not None:
                    seen.setdefault(sid, tid)
    evidence["failures"] = failures
    return Result(passed=not failures, evidence=evidence)
