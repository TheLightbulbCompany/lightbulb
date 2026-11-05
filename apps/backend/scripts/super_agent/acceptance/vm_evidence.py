"""Wave-2 gate (G2 step 3): per-task VM evidence (plans/wave2-V Task 17).

One workspace. Whether its tasks get their own AgentCore session is the backend's single rule,
``token_custody.task_vm_allowed`` (spec 13 §5.4: ChatGPT login only under ``custody: backend``, the token broker;
``custody: actor`` keeps the VM refreshing, so the shared workspace session). Under per-task VMs every Attempt of
every task must carry that task's own session id and no two tasks may share one; otherwise every Attempt must carry
the workspace session id. ``begin_attempt`` (core/temporal/activities.py) records the AgentCore session id it
dispatched to as the Attempt's ``sessionId`` -- ``runtime_transport.session_id_for(ws, task_id)`` for a task
session, ``session_id_for(ws)`` for the workspace's -- so the evidence is those rows against those ids.

Reads ``ctx.tasks_rows(ws)`` and the custody rule. Session ids are not secrets; task ids and session ids are the evidence.
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


async def vm_evidence(ctx: Ctx, *, ws: str, task_ids: list[str], per_task: bool | None = None) -> Result:
    """Passes iff every Attempt of ``task_ids`` carries the expected session id (see the module docstring).
    ``per_task`` defaults to the backend's own rule (``task_vm_allowed``) at gate time; a task with no Attempt
    is a failure: there is no evidence."""
    if per_task is None:
        from core.services.token_custody import task_vm_allowed

        per_task = await task_vm_allowed(ws)
    failures: list[str] = []
    attempts: list[dict[str, Any]] = []
    seen: dict[str, str] = {}  # session id -> first task seen on it
    by_task = _attempts(await ctx.tasks_rows(ws), task_ids)
    for tid in task_ids:
        if not by_task[tid]:
            failures.append(f"{tid}: no attempt rows")
        for att in sorted(by_task[tid], key=lambda a: int(a.get("n") or 0)):
            expected = session_id_for(ws, tid if per_task else None)
            sid = att.get("sessionId")
            attempts.append({"taskId": tid, "n": att.get("n"), "sessionId": sid})
            if sid != expected:
                kind = "task" if per_task else "workspace"
                failures.append(f"{tid}#{att.get('n')}: sessionId {sid} is not the {kind} session {expected}")
            if per_task and sid in seen and seen[sid] != tid:
                failures.append(f"{tid}#{att.get('n')}: shares session {sid} with task {seen[sid]}")
            if sid is not None:
                seen.setdefault(sid, tid)
    return Result(
        passed=not failures, evidence={"ws": ws, "perTaskVm": per_task, "attempts": attempts, "failures": failures}
    )
