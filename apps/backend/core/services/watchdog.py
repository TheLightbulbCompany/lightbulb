"""The watchdog (spec 11, contracts-v2 §18): code checks C1–C6 over super-agent workspaces, every 5 min.

Observe first: every check writes a Finding (`watchdog_repo.put_finding`, deterministic key, so a re-scan writes
nothing). The only action in wave 2 is the founder-accepted reconcile of orphaned task rows (F1), done in
`watchdog_act` through `TaskWorkflow`. This module never writes Task, Attempt, Question or Effect rows, never
creates tasks and never stops work (`test_guards.py`).
"""

from __future__ import annotations

import json
import logging
import random
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo, tasks_repo, watchdog_repo
from core.services import jev, runtime_runs, super_agent_flags
from core.temporal.watchdog_types import ActOut, Candidate, Proposal, ScanOut, WatchdogConfig

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


# --- reconcile state (read by TaskWorkflow's reconcile start, on the main queue) ------------------------


@activity.defn
async def reconcile_state(ws: str, task_id: str) -> str:
    """`{status}:{n}:live|ended` for a `queued`/`working` row (n = its last attempt, 0 = none), or `other` when
    the task moved on since the watchdog looked (do nothing)."""
    task = await tasks_repo.get_task(ws, task_id)
    if not task or task["status"] not in ("working", "queued") or task.get("stopRequestedBy"):
        return "other"  # moved on, or a person stopped it: never touched
    n = int(task.get("attemptCount") or 0)
    attempt = await tasks_repo.get_attempt(ws, task_id, n) if n else None
    return f"{task['status']}:{n}:{'live' if attempt and attempt.get('status') != 'ended' else 'ended'}"


# --- judge (spec 11 §3.5) ----------------------------------------------------------------------------

MAX_JUDGEMENTS_PER_ATTEMPT = 16  # 8 h ÷ 30 min
JUDGE_TIMEOUT_S = 15.0
STATE_MAX_CHARS = 24_000
NOTES = 10
NOTE_MAX = 1_000

# Changing this text is a PR (spec 11 §3.8): the model never writes text that reaches a Worker or a person.
VERDICT_INSTRUCTIONS = (
    "You are checking on a long-running background task done by an AI worker. Judge only from the evidence: the "
    "task's title and description, the worker's recent notes, its recent tool calls (name, args hash, ok, error "
    "class), its spend and any open questions. Pick the one verdict that best describes the last stretch of work."
)
VERDICT_CRITERIA = {
    "progressing": "New information or finished steps since the last notes; tool calls vary and succeed.",
    "looping": "The same calls or the same error repeat with no new result.",
    "stalled_waiting": "The Worker is waiting on something no one was asked for.",
    "drifting": "Recent work no longer serves the task's title and description.",
    "claims_done_without_evidence": "It says it is done but shows no test, output or check.",
    "blocked_external": "A service, credential or permission outside the Worker keeps failing.",
}
QUESTIONS = {"verdict": {"type": "choice", "instructions": VERDICT_INSTRUCTIONS, "criteria": VERDICT_CRITERIA}}
_STEER = frozenset({"looping", "drifting", "claims_done_without_evidence"})
_QUESTION = frozenset({"stalled_waiting", "blocked_external"})


def verdict_to_action(choice: str, p: float, cfg: WatchdogConfig) -> str:
    if choice == "progressing" or p < cfg.steer_threshold_p:
        return "none"
    if choice in _STEER:
        return "steer"
    return "question" if choice in _QUESTION else "none"


def judge_state(
    task: dict,
    attempt: dict,
    notes: list[dict],
    tools: list[dict],
    usage: dict,
    open_questions: list[str],
    previous_verdicts: list[str],
    *,
    age_min: int = 0,
) -> dict:
    """What Jev sees, ≤ 24k chars, oldest notes dropped first. Watchdog-authored lines never count as progress."""
    lines = [
        {"author": (m.get("author") or {}).get("kind"), "text": str(m.get("text") or "")[:NOTE_MAX]}
        for m in notes
        if m.get("kind") != "watchdog"
    ][-NOTES:]
    state = {
        "task": {
            "title": str(task.get("title") or "")[:200],
            "description": str(task.get("description") or "")[:1000],
            "specialist": task.get("specialist"),
            "attemptN": attempt.get("n"),
            "attemptAgeMin": age_min,
        },
        "notes": lines,
        "tools": [{k: t.get(k) for k in ("name", "argsHash", "ok", "errorClass")} for t in tools[-20:]],
        "usage": usage,
        "openQuestions": {"count": len(open_questions), "kinds": sorted(set(open_questions))},
        "previousVerdicts": previous_verdicts,
    }
    while len(json.dumps(state, ensure_ascii=False)) > STATE_MAX_CHARS and lines:
        lines.pop(0)
    return state


async def _evidence(c: Candidate, tree: dict) -> tuple[list[dict], list[dict]]:
    notes = await messaging_repo.list_messages_before(ts.task_room_id(c.task_id), 10**18, NOTES + 5)
    tools: list[dict] = []
    att = next((a for a in tree["attempts"] if a["n"] == c.attempt_n), None)
    if att:
        try:
            identity = runtime_runs.RunIdentity(att["runId"], c.ws, c.task_id, c.attempt_n, att["agentId"])
            tools = (await runtime_runs.get_run(c.ws, identity)).recent_tools
        except Exception:
            logger.info("watchdog judge: run status unavailable", extra={"taskId": c.task_id})
    return notes, tools


@activity.defn
async def watchdog_judge(cfg: WatchdogConfig, c: Candidate) -> Proposal | None:
    """One Jev verdict on a flagged long attempt → a judge Finding (observe) and its Proposal. `None` = nothing
    recorded (caps, task moved on, Jev down or an invalid answer)."""
    now = datetime.now(timezone.utc)
    judged = await judge_findings(c.ws, c.task_id, c.attempt_n, now)
    if len(judged) >= MAX_JUDGEMENTS_PER_ATTEMPT:
        return None
    if not await watchdog_repo.take_cap("judge", _day(now), cfg.judge_per_day):
        put_metric("watchdog.cap_hit", dimensions={"Kind": "judge"})
        return None
    tree = await tasks_repo.get_task_tree(c.ws, c.task_id)
    task = tree["task"]
    att = next((a for a in tree["attempts"] if a["n"] == c.attempt_n), None)
    if not task or not att or task["status"] != "working" or task.get("hidden") or att.get("status") == "ended":
        return None
    notes, tools = await _evidence(c, tree)
    usage = {
        "tokens": int((task.get("usage") or {}).get("inputTokens") or 0)
        + int((task.get("usage") or {}).get("outputTokens") or 0),
        "usd": float((task.get("usage") or {}).get("costUsd") or 0),
        "budgetUsd": task.get("budgetUsd"),
    }
    questions = [q.get("kind") or "question" for q in tree["questions"] if q.get("state") == "open"]
    state = judge_state(
        task, att, notes, tools, usage, questions, [j.get("verdict") for j in judged], age_min=c.age_min
    )
    answers = await jev.decide(state, QUESTIONS, timeout_s=JUDGE_TIMEOUT_S, metric_prefix="watchdog.judge")
    if not isinstance(answers, dict):
        return None
    verdict = answers.get("verdict") if isinstance(answers.get("verdict"), dict) else {}
    choice = verdict.get("choice")
    p = (verdict.get("probabilities") or {}).get(choice)
    if choice not in VERDICT_CRITERIA or not isinstance(p, (int, float)):
        return None
    action = verdict_to_action(choice, float(p), cfg)
    item, _ = await watchdog_repo.put_finding(
        {
            "pk": ts.wd_finding_pk(_day(now)),
            "sk": ts.wd_finding_sk(c.ws, c.task_id, "judge", c.attempt_n, len(judged) + 1),
            "ws": c.ws,
            "taskId": c.task_id,
            "attemptN": c.attempt_n,
            "check": "judge",
            "verdict": choice,
            "p": round(float(p), 4),
            "proposed": action,
            "mode": cfg.tier2,
            "reasons": list(c.reasons),
            "at": now.isoformat(),
            # ponytail: evidence on the row (≤ 24k chars) instead of S3; move to S3 if rows near 400 KB.
            "evidence": json.dumps(state, ensure_ascii=False),
        }
    )
    put_metric("watchdog.finding", dimensions={"Check": "judge"})
    # Returned even for "none": the digest lists every judge finding; act skips "none".
    return Proposal(c.ws, c.task_id, c.attempt_n, "judge", action, item["findingId"], round(float(p), 4))


# --- act (spec 11 §3.6, §3.7) ------------------------------------------------------------------------


def _mode_for(action: str, cfg: WatchdogConfig) -> str:
    """Wave 2: reconcile follows `cfg.reconcile` (F1: act in dev); every other action is observe only."""
    if action == "none":
        return "off"
    return cfg.reconcile if action == "reconcile" else "observe"


async def _escalate_once(p: Proposal) -> None:
    """A per-task limit was reached: one `ACT#escalate#01` row and one escalate Finding, then record-only."""
    if await watchdog_repo.spend_action(p.ws, p.task_id, "escalate", limit=1, finding_id=p.finding_id, by="watchdog"):
        await watchdog_repo.finish_action(p.ws, p.task_id, "escalate", 1, "done")
        now = datetime.now(timezone.utc)
        await watchdog_repo.put_finding(
            {
                "pk": ts.wd_finding_pk(_day(now)),
                "sk": ts.wd_finding_sk(p.ws, p.task_id, p.check, p.attempt_n) + "#ESC",
                "ws": p.ws,
                "taskId": p.task_id,
                "attemptN": p.attempt_n,
                "check": p.check,
                "mode": "observe",
                "proposed": "escalate",
                "at": now.isoformat(),
            }
        )


@activity.defn
async def watchdog_act(cfg: WatchdogConfig, p: Proposal, by: str = "watchdog") -> ActOut:
    outcome = await _act(cfg, p, by)
    put_metric("watchdog.action", dimensions={"Kind": p.action, "Outcome": outcome})
    return ActOut(outcome)


async def _act(cfg: WatchdogConfig, p: Proposal, by: str) -> str:
    mode = _mode_for(p.action, cfg)
    if mode == "off":
        return "skipped"
    if mode != "act":
        return "observed"  # the Finding was written by the scan
    task = await tasks_repo.get_task(p.ws, p.task_id)  # consistent
    if not task or task.get("stopRequestedBy") or task["status"] not in ("working", "queued"):
        return "skipped"  # a person stopped it, or it resolved/failed/moved on since the scan
    try:
        wf = await _wf_state(p.ws, p.task_id)
    except DescribeFailed:
        return "skipped"
    if not check_c1(task, wf, datetime.now(timezone.utc)):
        return "skipped"  # live again, or the row moved within the last 5 min: not an orphan
    if await watchdog_repo.count_actions(p.ws, p.task_id, "reconcile") >= cfg.reconciles_per_task:
        await _escalate_once(p)  # checked before the cap so a task at its limit never spends fleet budget
        return "limit"
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if not await watchdog_repo.take_cap("act", hour, cfg.fleet_actions_per_hour):
        put_metric("watchdog.cap_hit", dimensions={"Kind": "act"})
        return "capped"  # no per-task row: a fleet storm must not burn tasks' attempts
    n = await watchdog_repo.spend_action(
        p.ws, p.task_id, "reconcile", limit=cfg.reconciles_per_task, finding_id=p.finding_id, by=by
    )
    if n is None:  # another writer took the last slot between the count and the spend
        await watchdog_repo.release_cap("act", hour)
        await _escalate_once(p)
        return "limit"
    from core.temporal.client import start_task_workflow

    try:
        await start_task_workflow(p.ws, p.task_id, "reconcile")
    except Exception as exc:  # the attempt is spent either way (Paperclip lesson)
        await watchdog_repo.finish_action(p.ws, p.task_id, "reconcile", n, "failed", type(exc).__name__)
        return "failed"
    await watchdog_repo.finish_action(p.ws, p.task_id, "reconcile", n, "done")
    return "done"


# --- tick metric, digest, report, schedule (spec 11 §3.3, §3.10) --------------------------------------

DIGEST_LINES = 20
REPORT_LABELS = 100
REPORT_DAYS = 30
SCHEDULE_ID = "watchdog"
TICK_EVERY = timedelta(minutes=5)
TICK_TIMEOUT = timedelta(minutes=4)
_TIER = {"judge": "tier2", "C3": "tier1", "C4": "tier1", "C1": "reconcile"}


@activity.defn
async def watchdog_tick_metric(mode: str) -> None:
    """Every tick, also when off: P48 alarms on its silence."""
    put_metric("watchdog.tick", dimensions={"Mode": mode})


def _line(f: dict) -> str:
    what = f.get("verdict") or f.get("check")
    p = f" p={f['p']}" if f.get("p") is not None else ""
    hint = f" hint={f['hint']}" if f.get("hint") else ""
    return f"{f['findingId']} {what}{p} proposed={f.get('proposed')} mode={f.get('mode')}{hint}"


async def _post(text: str, event_id: str) -> None:
    from core.services.task_conversation import post_as_lightbulb

    await post_as_lightbulb(settings.WATCHDOG_OWNER_ID, settings.WATCHDOG_ROOM_ID, text=text, event_id=event_id)


async def report(now: datetime | None = None) -> dict:
    """Precision per tier over its latest 100 labelled findings (`unsure` left out)."""
    now = now or datetime.now(timezone.utc)
    labelled = []
    # ponytail: 30 day-partition queries per report; add a sparse label GSI if reports get frequent.
    for d in range(REPORT_DAYS):
        labelled += [f for f in await watchdog_repo.query_findings(_day(now - timedelta(days=d)), "") if f.get("label")]
    labelled.sort(key=lambda f: f.get("labelledAt") or "", reverse=True)
    out: dict = {}
    for tier in ("tier1", "tier2", "reconcile"):
        rows = [f for f in labelled if _TIER.get(f.get("check")) == tier][:REPORT_LABELS]
        judged = [f for f in rows if f["label"] in ("tp", "fp")]
        tp = sum(f["label"] == "tp" for f in judged)
        out[tier] = {"n": len(judged), "precision": round(tp / len(judged), 3) if judged else None}
    return out


@activity.defn
async def watchdog_digest(cfg: WatchdogConfig, finding_ids: list[str], daily: bool) -> None:
    """One `#watchdog` post per tick with new findings (≤ 20 lines); a daily summary once a day. Silent when
    nothing is new. Idempotent on the tick's workflow id."""
    if not (settings.WATCHDOG_OWNER_ID and settings.WATCHDOG_ROOM_ID):
        logger.info("watchdog digest: no #watchdog room configured")
        return
    if finding_ids:
        rows = [await watchdog_repo.get_finding(*watchdog_repo.finding_key(i)) for i in finding_ids[:DIGEST_LINES]]
        more = len(finding_ids) - DIGEST_LINES
        text = "\n".join(
            [f"Watchdog: {len(finding_ids)} new findings", *(_line(f) for f in rows if f)]
            + ([f"…and {more} more"] if more > 0 else [])
        )
        await _post(text, f"watchdog:digest:{activity.info().workflow_id}")
    if not daily:
        return
    now = datetime.now(timezone.utc)
    yday = _day(now - timedelta(days=1))
    found = await watchdog_repo.query_findings(yday, "")
    if not found:
        return  # a healthy day is silent
    counts: dict[str, int] = {}
    for f in found:
        counts[f.get("verdict") or f["check"]] = counts.get(f.get("verdict") or f["check"], 0) + 1
    unlabelled = [f for f in found if not f.get("label")]
    sample = random.sample(unlabelled, min(DIGEST_LINES, len(unlabelled)))
    precision = await report(now)
    text = "\n".join(
        [
            f"Watchdog daily {yday}: {len(found)} findings, {len(unlabelled)} unlabelled",
            " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            " ".join(f"{t}: n={r['n']} precision={r['precision']}" for t, r in precision.items()),
            "Label with: watchdog_ctl --label <id> tp|fp|unsure --by <admin id>",
            *(_line(f) for f in sample),
        ]
    )
    await _post(text, f"watchdog:daily:{yday}")


async def ensure_watchdog_schedule(client) -> bool:
    """Create the `watchdog` Schedule with the default config if it is missing. Never overwrites the args a
    maintainer set. No-op unless the env master is on (prod)."""
    if not super_agent_flags.env_enabled():
        return False
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    "WatchdogWorkflow",
                    WatchdogConfig(),
                    id="watchdog-tick",
                    task_queue=settings.TEMPORAL_WATCHDOG_TASK_QUEUE,
                    execution_timeout=TICK_TIMEOUT,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=TICK_EVERY)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=1)),
            ),
        )
    except ScheduleAlreadyRunningError:
        return False
    return True


WATCHDOG_ACTIVITIES = [watchdog_tick_metric, watchdog_scan, watchdog_judge, watchdog_act, watchdog_digest]
