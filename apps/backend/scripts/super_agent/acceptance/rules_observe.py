"""Wave-2 gate (G2): proactive rules in observe left a record and did nothing (plans/wave2-S Task 9).

Manual checks 1-3 (and 9) are driven by a person in ``sa-dev-on``: Sam writes the rule in ``#dev``, Priya's
Turn on is refused, Sam's lands it in observe (check 1); three bug reports and two chatter lines are posted
(check 2); Sam asks Lightbulb to fix one reported bug in its thread (check 3); an invite reaches the fixture
Google account (check 9). This module is the evidence those steps leave in the tasks table, pure over
``ctx.tasks_rows(ws)`` (G1's dev-only read), no network. Thread keys, trigger ids and the rule id are the
evidence; no message text is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core import tasks_schema as ts


class Ctx(Protocol):
    async def tasks_rows(self, ws: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Result:
    """G1's ``Result(passed, evidence)``; the gate reads ``evidence["failures"]`` when not passed."""

    passed: bool
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Expect:
    ws: str
    rule_id: str
    matched_threads: list[str]  # the bug-report threads (check 2): one record each
    quiet_threads: list[str] = field(default_factory=list)  # the chatter lines: no record
    done_by_human: list[str] = field(default_factory=list)  # the thread Sam had fixed (check 3)
    invites: list[str] = field(default_factory=list)  # ``cal:{account}:{eventId}`` thread keys (check 9)


async def rules_observe(ctx: Ctx, expect: Expect) -> Result:
    """Passes iff the rule row is active in observe, every matched thread has exactly one ``TRIG#rule`` row and
    one ``OBS#`` marker (and a stats entry), no quiet thread has any, no task carries the rule as its policy,
    and each thread a person handled is marked ``doneByHuman``."""
    rows = await ctx.tasks_rows(expect.ws)
    by_sk = {r["sk"]: r for r in rows}
    failures: list[str] = []
    rule = by_sk.get(ts.schedule_sk(expect.rule_id))
    if rule is None:
        return Result(False, {"failures": [f"rule {expect.rule_id}: no row"]})
    if (rule.get("status"), rule.get("mode")) != ("active", "observe"):
        failures.append(
            f"rule {expect.rule_id}: status={rule.get('status')} mode={rule.get('mode')}, not active/observe"
        )
    entries = {m.get("threadKey"): m for m in (rule.get("stats") or {}).get("matches") or []}
    records: dict[str, dict[str, Any]] = {}
    for thread in [*expect.matched_threads, *expect.invites]:
        trig = by_sk.get(ts.trigger_sk(ts.rule_trigger_id(expect.rule_id, thread)))
        obs = by_sk.get(ts.observed_sk(thread))
        records[thread] = {"trigger": trig is not None, "observed": obs is not None, "entry": thread in entries}
        if trig is None:
            failures.append(f"{thread}: no TRIG#rule row")
        elif (trig.get("ruleMatch") or {}).get("decision") != "observed":
            failures.append(f"{thread}: ruleMatch.decision is {(trig.get('ruleMatch') or {}).get('decision')!r}")
        if obs is None:
            failures.append(f"{thread}: no OBS# marker")
        if thread not in entries:
            failures.append(f"{thread}: no stats.matches entry")
    for thread in expect.quiet_threads:
        if ts.trigger_sk(ts.rule_trigger_id(expect.rule_id, thread)) in by_sk or thread in entries:
            failures.append(f"{thread}: chatter was recorded as a match")
    for thread in expect.done_by_human:
        if not (entries.get(thread) or {}).get("doneByHuman"):
            failures.append(f"{thread}: not marked doneByHuman")
    policy_ref = ts.schedule_sk(expect.rule_id)
    tasks = [r["taskId"] for r in rows if r.get("type") == "task" and r.get("policyRef") == policy_ref]
    if tasks:
        failures.append(f"rule {expect.rule_id}: started tasks {tasks}; observe never acts")
    evidence = {
        "rule": {"id": expect.rule_id, "status": rule.get("status"), "mode": rule.get("mode"), "ruleVersion": rule.get("ruleVersion")},
        "records": records,
        "matchesThisVersion": sum(1 for m in entries.values() if m.get("version") == rule.get("ruleVersion", 1)),
        "doneByHuman": sorted(k for k, m in entries.items() if m.get("doneByHuman")),
        "tasksFromRule": tasks,
        "failures": failures,
    }  # fmt: skip
    return Result(not failures, evidence)
