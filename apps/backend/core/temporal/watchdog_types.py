"""Watchdog payloads that cross Temporal (spec 11 §5.1): ids, enums and numbers only. Stdlib only, so
`WatchdogWorkflow` imports it inside the sandbox. Lane O's `types.py` is left alone."""

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["off", "observe", "act"]


@dataclass(frozen=True)
class WatchdogConfig:
    """The schedule's workflow args; changed only through `scripts/watchdog_ctl.py`. No text."""

    # F1 says reconcile acts in dev from day one; it ships as observe until W6 (TaskWorkflow's reconcile path)
    # is merged, then `watchdog_ctl --set reconcile=act` (or this default) turns it on.
    reconcile: Literal["off", "observe", "act"] = "observe"
    tier1: Literal["off", "observe"] = "observe"  # retry, recheck
    tier2: Literal["off", "observe"] = "observe"  # steer, question
    judge: bool = True
    judge_per_tick: int = 10
    judge_per_day: int = 500
    fleet_actions_per_hour: int = 60
    reconciles_per_task: int = 2
    retries_per_task: int = 2
    rechecks_per_task: int = 6
    steer_threshold_p: float = 0.7
    stall_minutes: int = 30
    judge_min_age_minutes: int = 20
    judge_every_minutes: int = 30

    def all_off(self) -> bool:
        return self.reconcile == self.tier1 == self.tier2 == "off" and not self.judge


@dataclass(frozen=True)
class Candidate:
    """A working attempt for the judge (C5/C6)."""

    ws: str
    task_id: str
    attempt_n: int
    run_id: str
    age_min: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Proposal:
    ws: str
    task_id: str
    attempt_n: int
    check: str
    action: str  # ts.WatchdogAction
    finding_id: str
    p: float | None = None


@dataclass(frozen=True)
class ScanOut:
    candidates: list[Candidate] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)  # new this tick, for the digest


@dataclass(frozen=True)
class ActOut:
    outcome: Literal["observed", "done", "refused", "capped", "limit", "failed", "skipped"]


@dataclass(frozen=True)
class TickReport:
    findings_new: int
    judged: int
    acted: int
    mode: str
