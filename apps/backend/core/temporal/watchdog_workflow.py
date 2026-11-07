"""`WatchdogWorkflow` (spec 11 §3.3): one tick, started every 5 min by the `watchdog` Schedule on queue
`lightbulb-watchdog`. tick metric → scan → judge (≤ `judge_per_tick`, oldest attempts first) → act → digest.
A failed judge or act never stops the tick. Imports only `temporalio`, the stdlib and `watchdog_types`."""

from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from core.temporal.watchdog_types import ActOut, Candidate, Proposal, ScanOut, TickReport, WatchdogConfig

_SHORT = {"start_to_close_timeout": timedelta(seconds=30)}
_SCAN = {"start_to_close_timeout": timedelta(minutes=2), "retry_policy": RetryPolicy(maximum_attempts=1)}
_JUDGE = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=2)}
_ACT = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=3)}
_DIGEST = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=2)}


def _mode(cfg: WatchdogConfig) -> str:
    if cfg.all_off():
        return "off"
    return "act" if cfg.reconcile == "act" else "observe"


@workflow.defn
class WatchdogWorkflow:
    @workflow.run
    async def run(self, cfg: WatchdogConfig) -> TickReport:
        mode = _mode(cfg)
        await workflow.execute_activity("watchdog_tick_metric", mode, **_SHORT)
        if mode == "off":
            return TickReport(0, 0, 0, mode)
        scan = await workflow.execute_activity("watchdog_scan", cfg, result_type=ScanOut, **_SCAN)
        finding_ids, proposals = list(scan.finding_ids), list(scan.proposals)
        judged = 0
        if cfg.judge:
            oldest: list[Candidate] = sorted(scan.candidates, key=lambda c: -c.age_min)[: cfg.judge_per_tick]
            for c in oldest:
                try:
                    p = await workflow.execute_activity(
                        "watchdog_judge", args=[cfg, c], result_type=Optional[Proposal], **_JUDGE
                    )
                except ActivityError:
                    continue
                if p is not None:
                    judged += 1
                    finding_ids.append(p.finding_id)
                    proposals.append(p)
        acted = 0
        for p in proposals:
            if p.action == "none":
                continue
            try:
                out = await workflow.execute_activity(
                    "watchdog_act", args=[cfg, p, "watchdog"], result_type=ActOut, **_ACT
                )
            except ActivityError:
                continue
            acted += out.outcome == "done"
        now = workflow.now()
        daily = now.hour == 9 and now.minute < 5  # the first 5-min tick after 09:00 UTC
        if finding_ids or daily:
            await workflow.execute_activity("watchdog_digest", args=[cfg, finding_ids, daily], **_DIGEST)
        return TickReport(len(finding_ids), judged, acted, mode)


WATCHDOG_WORKFLOWS = [WatchdogWorkflow]
