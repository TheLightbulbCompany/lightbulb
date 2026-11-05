"""Proactive rules, observe mode (contracts §18, spec 12 §3.4): record what a rule would have done.

`record_match` is an Activity and is also awaited directly by admission. It never creates a task, a Router turn
or a push: in wave 2 every mode only records.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from temporalio import activity

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric
from core.repositories import automations_repo, tasks_repo
from core.repositories.tasks_repo import _put_new, _row

logger = logging.getLogger(__name__)

#: ponytail: same value as `rules.DID_IT_YOURSELF_WINDOW_H` (S2); import it from there once both are on main.
DID_IT_YOURSELF_WINDOW_H = 24


@activity.defn
async def record_match(
    ws: str,
    sched_id: str,
    version: int,
    thread_key: str,
    ref: dict[str, Any],
    p: float | None,
    would_do: str,
    source: Literal["chat", "webhook"],
) -> Literal["observed", "duplicate"]:
    """One match per rule per thread, ever: the deterministic `TRIG#rule:…` put is the dedupe. The stats entry and
    the `OBS#` marker are idempotent too, so a retry after a crash between writes completes the record."""
    trigger_id = ts.rule_trigger_id(sched_id, thread_key)
    now = utc_now_iso()
    _, new = await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": source,
            "ref": ref,
            "admission": "candidate",
            "lane": "shared",
            "receivedAt": now,
            "ruleMatch": {"schedId": sched_id, "version": version, "p": p, "decision": "observed", "wouldDo": would_do},
        },
    )
    entry = {"at": now, "threadKey": thread_key, "wouldDo": would_do, "version": version, "doneByHuman": False}
    await automations_repo.append_match(ws, sched_id, entry)
    await _put_new(
        ws, _row(ts.Observed, ws, {"threadKey": thread_key, "schedId": sched_id, "triggerId": trigger_id, "at": now})
    )
    outcome = "observed" if new else "duplicate"
    put_metric("rules.match", dimensions={"outcome": outcome})
    return outcome


async def note_task_started(ws: str, thread_key: str) -> None:
    """Called when a person's request starts or passes through to a task on `thread_key`: every rule that
    observed the thread in the last 24 h gets `doneByHuman=true`. Zero model cost; never raises (it must not
    fail the Router apply)."""
    try:
        obs = await tasks_repo._get(ws, ts.observed_sk(thread_key))
        if obs is None:
            return
        at = datetime.fromisoformat(obs["at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - at > timedelta(hours=DID_IT_YOURSELF_WINDOW_H):
            return
        for rule in await automations_repo.list_rules(ws):
            entries = (rule.get("stats") or {}).get("matches") or []
            if any(m.get("threadKey") == thread_key for m in entries):
                await automations_repo.mark_done_by_human(ws, rule["scheduleId"], thread_key)
    except Exception:  # noqa: BLE001 -- measurement only
        logger.warning("did-it-yourself mark failed for %s", thread_key, exc_info=True)


WORKFLOWS: list = []
ACTIVITIES = [record_match]
