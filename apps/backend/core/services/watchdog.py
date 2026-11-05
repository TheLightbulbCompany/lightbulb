"""The watchdog (spec 11, contracts-v2 §18): code checks C1–C6 over super-agent workspaces, every 5 min.

Observe first: every check writes a Finding (`watchdog_repo.put_finding`, deterministic key, so a re-scan writes
nothing). The only action in wave 2 is the founder-accepted reconcile of orphaned task rows (F1), done in
`watchdog_act` through `TaskWorkflow`. This module never writes Task, Attempt, Question or Effect rows, never
creates tasks and never stops work (`test_guards.py`).
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.observability.metrics import put_metric
from core.repositories import tasks_repo, watchdog_repo
from core.services import runtime_runs, super_agent_flags
from core.temporal.watchdog_types import Candidate, Proposal, ScanOut, WatchdogConfig

logger = logging.getLogger(__name__)

# --- thresholds (spec 11 §3.4) -----------------------------------------------------------------------

ORPHAN_AFTER = timedelta(minutes=5)
RETRY_AFTER = timedelta(minutes=10)
RETRY_JITTER_S = 120
FAILED_WINDOW = timedelta(hours=24)
LONG_RUN = timedelta(minutes=60)
REPEAT_SAME_CALL = 4
REPEAT_SAME_ERROR = 3
RECHECK_BACKOFF_MIN = (5, 10, 20, 40, 80, 160)
HINT_WINDOW_DAYS = 2  # findings ≤ 48 h old get outcome hints
SCOPE_TTL_S = 3600

# Error classes a retry can fix. O's `end_attempt` today writes exception names (`RunRefused`, `RunFailed`,
# `HeartbeatTimeout`, `RuntimeBusy`); none of them is here, so C3 fires on `runtime_lost` until O maps
# provider errors to these names. Unknown classes never qualify.
RETRYABLE = frozenset({"provider_5xx", "provider_rate_limited", "recycling"})
EXTERNAL_PREFIXES = ("integration_unavailable:", "provider_")


def _retryable(error: str | None) -> bool:
    return bool(error) and (error in RETRYABLE or error.startswith("integration_unavailable:"))


def _external(error: str | None) -> bool:
    return bool(error) and error.startswith(EXTERNAL_PREFIXES)


def _at(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def _day(now: datetime) -> str:
    return now.strftime("%Y%m%d")


# --- pure checks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WfState:
    status: str  # WorkflowExecutionStatus name
    pending_worker_heartbeat_at: datetime | None
    has_pending_worker_turn: bool


def check_c1(task: dict, wf: WfState | None, now: datetime) -> bool:
    """Orphan: the row says live, the workflow is closed or not found, and the row is older than 5 min."""
    if task["status"] not in ("working", "queued"):
        return False
    if wf is not None and wf.status == "RUNNING":
        return False
    return now - _at(task["updatedAt"]) >= ORPHAN_AFTER


def check_c2(task: dict, wf: WfState | None, cfg: WatchdogConfig, now: datetime) -> bool:
    """Stalled: workflow open, task working, but no `worker_turn` pending or its heartbeat is stale."""
    if task["status"] != "working" or wf is None or wf.status != "RUNNING":
        return False
    stall = timedelta(minutes=cfg.stall_minutes)
    if now - _at(task["updatedAt"]) < stall:
        return False
    if not wf.has_pending_worker_turn:
        return True
    return wf.pending_worker_heartbeat_at is None or now - wf.pending_worker_heartbeat_at >= stall


def _jitter(task_id: str) -> timedelta:
    return timedelta(seconds=zlib.crc32(task_id.encode()) % (2 * RETRY_JITTER_S + 1) - RETRY_JITTER_S)


def check_c3(task: dict, attempt: dict | None, now: datetime) -> bool:
    """Retryable failure: failed by a lost runtime or a retryable error class, not stopped by a person, not
    hidden, has a requester, and quiet for 10 min (±2 min by task id)."""
    if task["status"] != "failed" or task.get("hidden") or task.get("stopRequestedBy") or not task.get("ownerUserId"):
        return False
    if not attempt or not (attempt.get("outcome") == "runtime_lost" or _retryable(attempt.get("error"))):
        return False
    return now - _at(task["updatedAt"]) >= RETRY_AFTER + _jitter(task["taskId"])


def next_recheck_due(n_failed: int, last_at: datetime) -> datetime | None:
    """C4 backoff: 5, 10, 20, 40, 80, 160 min after the last failed probe; then escalate (None)."""
    if n_failed >= len(RECHECK_BACKOFF_MIN):
        return None
    return last_at + timedelta(minutes=RECHECK_BACKOFF_MIN[n_failed])


def _longest_run(keys: list[tuple]) -> int:
    best = run = 0
    prev = object()
    for k in keys:
        run = run + 1 if k == prev else 1
        best, prev = max(best, run), k
    return best


def check_c5(recent_tools: list[dict]) -> bool:
    """Repeats: the same call ≥ 4 times in a row, or the same tool failing with the same error ≥ 3 in a row."""
    calls = [(t.get("name"), t.get("argsHash")) for t in recent_tools]
    errors = [(t.get("name"), t.get("errorClass")) if t.get("errorClass") else object() for t in recent_tools]
    return _longest_run(calls) >= REPEAT_SAME_CALL or _longest_run(errors) >= REPEAT_SAME_ERROR


def check_c6(attempt: dict, c5: bool, last_judged_at: datetime | None, cfg: WatchdogConfig, now: datetime) -> bool:
    """Long-run gate for the judge: ≥ 20 min with C5, or ≥ 60 min; never sooner than 30 min after the last."""
    age = now - _at(attempt["startedAt"])
    due = (c5 and age >= timedelta(minutes=cfg.judge_min_age_minutes)) or age >= LONG_RUN
    return due and (last_judged_at is None or now - last_judged_at >= timedelta(minutes=cfg.judge_every_minutes))


# --- scan --------------------------------------------------------------------------------------------

_scope: tuple[float, list[str]] = (0.0, [])


async def _workspaces() -> list[str]:
    global _scope
    if not _scope[0] or time.monotonic() - _scope[0] > SCOPE_TTL_S:
        _scope = (time.monotonic(), await super_agent_flags.list_on())
    return _scope[1]


class DescribeFailed(Exception):
    """`describe()` failed for a reason other than NOT_FOUND: the workflow's state is unknown."""


async def _wf_state(ws: str, task_id: str) -> WfState | None:
    from core.temporal.client import task_handle

    try:
        desc = await task_handle(ws, task_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return None
        raise DescribeFailed(str(exc.status)) from exc
    turn = next((a for a in desc.raw_description.pending_activities if a.activity_type.name == "worker_turn"), None)
    hb = None
    if turn is not None and turn.HasField("last_heartbeat_time"):
        hb = turn.last_heartbeat_time.ToDatetime(tzinfo=timezone.utc)
    status = desc.status.name if isinstance(desc.status, WorkflowExecutionStatus) else str(desc.status)
    return WfState(status, hb, turn is not None)


async def _tasks(ws: str, status: str, since: datetime | None = None) -> list[dict]:
    out, cursor = [], None
    while True:
        page, cursor = await tasks_repo.list_tasks_by_status(ws, status, limit=100, cursor=cursor)
        for t in page:
            if since and _at(t["updatedAt"]) < since:
                return out  # newest first: the rest are older
            out.append(t)
        if not cursor:
            return out


async def judge_findings(ws: str, task_id: str, n: int, now: datetime) -> list[dict]:
    """This attempt's judge findings (an attempt lives ≤ 8 h, so today and yesterday cover it)."""
    out = []
    for d in (now - timedelta(days=1), now):
        prefix = ts.wd_finding_sk(ws, task_id, "judge", n) + "#"
        out += await watchdog_repo.query_findings(_day(d), prefix)
    return out


class _Scan:
    def __init__(self, cfg: WatchdogConfig, now: datetime):
        self.cfg, self.now = cfg, now
        self.candidates: list[Candidate] = []
        self.proposals: list[Proposal] = []
        self.finding_ids: list[str] = []

    async def finding(self, task: dict, check: str, n: int, mode: str, proposed: str) -> str:
        item, new = await watchdog_repo.put_finding(
            {
                "pk": ts.wd_finding_pk(_day(self.now)),
                "sk": ts.wd_finding_sk(task["pk"][3:], task["taskId"], check, n),
                "ws": task["pk"][3:],
                "taskId": task["taskId"],
                "attemptN": n,
                "check": check,
                "mode": mode,
                "proposed": proposed,
                "at": self.now.isoformat(),
            }
        )
        if new:
            put_metric("watchdog.finding", dimensions={"Check": check})
            self.finding_ids.append(item["findingId"])
        return item["findingId"]

    def propose(self, task: dict, check: str, n: int, action: str, fid: str) -> None:
        self.proposals.append(Proposal(task["pk"][3:], task["taskId"], n, check, action, fid))

    async def live(self, ws: str, row: dict) -> None:
        """C1 (row says live, workflow gone), C2 (stalled), C5/C6 (judge candidates) for working/queued rows."""
        cfg = self.cfg
        if row.get("hidden") and cfg.reconcile == "off":
            return
        task = await tasks_repo.get_task(ws, row["taskId"])  # the GSI lags a transition: re-read consistently
        if not task or task["status"] not in ("working", "queued"):
            return
        try:
            wf = await _wf_state(ws, task["taskId"])
        except DescribeFailed:
            put_metric("watchdog.scan_error", dimensions={"Check": "C1"})
            return
        n = int(task.get("attemptCount") or 0)
        if check_c1(task, wf, self.now):
            if cfg.reconcile != "off":
                fid = await self.finding(task, "C1", n, cfg.reconcile, "reconcile")
                self.propose(task, "C1", n, "reconcile", fid)
            return
        if task.get("hidden"):
            return
        if check_c2(task, wf, cfg, self.now):
            put_metric("task.stuck")
            await self.finding(task, "C2", n, "observe", "none")
        if cfg.judge and task["status"] == "working" and n:
            await self.judge_gate(ws, task, n)

    async def judge_gate(self, ws: str, task: dict, n: int) -> None:
        attempt = await tasks_repo.get_attempt(ws, task["taskId"], n)
        if not attempt or attempt.get("status") == "ended" or not attempt.get("startedAt"):
            return
        age = self.now - _at(attempt["startedAt"])
        if age < timedelta(minutes=self.cfg.judge_min_age_minutes):
            return
        c5 = False
        try:
            identity = runtime_runs.RunIdentity(attempt["runId"], ws, task["taskId"], n, attempt["agentId"])
            c5 = check_c5((await runtime_runs.get_run(ws, identity)).recent_tools)
        except Exception:  # a lost or busy runtime is C2's business; the judge just has no tool evidence
            logger.info("watchdog: run status unavailable", extra={"taskId": task["taskId"]})
        if c5:
            await self.finding(task, "C5", n, "observe", "none")
        judged = await judge_findings(ws, task["taskId"], n, self.now)
        last = max((_at(j["at"]) for j in judged), default=None)
        if check_c6(attempt, c5, last, self.cfg, self.now):
            reasons = ["C5", "C6"] if c5 else ["C6"]
            self.candidates.append(
                Candidate(ws, task["taskId"], n, attempt["runId"], int(age.total_seconds() // 60), reasons)
            )

    async def failed(self, ws: str, row: dict) -> None:
        """C3 retryable failure, or C4 recheck when the error is external. Observe only in wave 2."""
        if self.cfg.tier1 == "off" or row.get("hidden"):
            return
        n = int(row.get("attemptCount") or 0)
        attempt = await tasks_repo.get_attempt(ws, row["taskId"], n) if n else None
        if not check_c3(row, attempt, self.now):
            return
        check, action = ("C4", "recheck") if _external((attempt or {}).get("error")) else ("C3", "retry")
        fid = await self.finding(row, check, n, self.cfg.tier1, action)
        self.propose(row, check, n, action, fid)

    async def hints(self) -> None:
        """Label aids for findings ≤ 48 h old: what happened next without the watchdog."""
        # ponytail: one GetItem per un-hinted finding per tick; fine at dev volume, batch_get when findings grow.
        for d in range(HINT_WINDOW_DAYS):
            day = _day(self.now - timedelta(days=d))
            for f in await watchdog_repo.query_findings(day, ""):
                if f.get("hint") or f.get("check") == "judge":
                    continue
                task = await tasks_repo.get_task(f["ws"], f["taskId"])
                hint = _hint(f, task)
                if hint:
                    await watchdog_repo.set_hint(f["pk"], f["sk"], hint)


def _hint(finding: dict, task: dict | None) -> str | None:
    if not task:
        return None
    if task.get("stopRequestedBy"):
        return "person_stopped"
    if int(task.get("attemptCount") or 0) > int(finding["attemptN"]) and finding["check"] in ("C3", "C4"):
        return "person_retried"
    if task["status"] in ("resolved", "idle") and finding["check"] in ("C2", "C5"):
        return "resolved_without_help"
    return None


@activity.defn
async def watchdog_scan(cfg: WatchdogConfig) -> ScanOut:
    if cfg.all_off():
        return ScanOut()
    scan = _Scan(cfg, datetime.now(timezone.utc))
    for ws in await _workspaces():
        try:
            for status in ("working", "queued"):
                for row in await _tasks(ws, status):
                    await scan.live(ws, row)
            for row in await _tasks(ws, "failed", since=scan.now - FAILED_WINDOW):
                await scan.failed(ws, row)
        except Exception:  # one bad workspace never costs the others their tick
            logger.warning("watchdog scan failed for a workspace", extra={"ws": ws}, exc_info=True)
            put_metric("watchdog.scan_error", dimensions={"Check": "workspace"})
    try:
        await scan.hints()
    except Exception:
        logger.warning("watchdog hints failed", exc_info=True)
        put_metric("watchdog.scan_error", dimensions={"Check": "hints"})
    return ScanOut(scan.candidates, scan.proposals, scan.finding_ids)
