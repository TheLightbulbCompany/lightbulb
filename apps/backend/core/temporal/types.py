"""Payloads that cross Temporal: ids, enums and numbers only (wave-2 README cross-lane names).

Message text, briefs and outputs stay on the rows; `test_payload_types_carry_no_text` pins every field name.
Workflow code imports this module, so it uses the stdlib only (the enums mirror `core.tasks_schema`, and
`test_enums_mirror_tasks_schema` fails when they drift).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

TaskStartReason = Literal["router", "propose_start", "reopen", "retry", "schedule", "migrated", "reconcile"]
TaskInputKind = Literal["message", "job_completed", "delegation_completed", "answer", "watch", "network_result"]
AttemptOutcome = Literal[
    "completed",
    "failed",
    "cancelled",
    "runtime_lost",
    "refused",
    "waiting_approval",
    "checkpoint_failed",
    "unavailable",
]
Admission = Literal["respond", "candidate"]
StopReason = Literal["stopped", "budget", "watchdog"]
# The bridge's v1 run statuses (bridge/runs.py, runtime_runs.RunStatus.status).
RunStatusName = Literal["queued", "running", "stopping", "completed", "failed", "cancelled"]

_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


@dataclass(frozen=True)
class TriggerRef:
    ws: str
    trigger_id: str
    lane: str  # "shared" or ts.router_lane_key(user)
    admission: Admission


@dataclass(frozen=True)
class TaskStart:
    ws: str
    task_id: str
    reason: TaskStartReason


@dataclass(frozen=True)
class TaskInputRef:
    trigger_id: str
    kind: TaskInputKind


@dataclass(frozen=True)
class StopRequest:
    by_user_id: str
    reason: StopReason = "stopped"


@dataclass(frozen=True)
class StopResult:
    status: Literal["stopping", "stopped", "not_running"]


@dataclass(frozen=True)
class ActionRequest:
    by_user_id: str
    expected_version: int | None = None


@dataclass(frozen=True)
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    kind: Literal["heartbeat", "terminal"]
    status: RunStatusName
    usage: RunUsage | None = None


@dataclass(frozen=True)
class RouterTurnIn:
    ws: str
    lane: str
    batch_id: str
    trigger_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RouterTurnOut:
    batch_id: str
    started_task_ids: list[str] = field(default_factory=list)
    posted_message_ids: list[str] = field(default_factory=list)
    fallback: bool = False


@dataclass(frozen=True)
class AttemptRef:
    n: int
    run_id: str


@dataclass(frozen=True)
class WorkerTurnIn:
    ws: str
    task_id: str
    n: int
    run_id: str


@dataclass(frozen=True)
class WorkerTurnOut:
    outcome: AttemptOutcome
    error: str | None = None  # an exception class name, never its message

    def __post_init__(self) -> None:
        if self.error is not None and not _CLASS_NAME.match(self.error):
            raise ValueError("WorkerTurnOut.error is an error class name")


@dataclass(frozen=True)
class RouterState:
    """`RouterWorkflow`'s start and Continue-As-New argument: queued refs and the recent trigger ids seen."""

    ws: str
    queue: list[TriggerRef] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
