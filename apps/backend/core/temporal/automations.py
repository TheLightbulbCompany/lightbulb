"""Automation workflows (spec 06, contracts-v2 §11, §13: lane A owns `TriggerFireWorkflow`).

Workflow code imports only `temporalio` and the stdlib (sandbox-safe); Activities import their I/O modules
inside the function. Payloads carry ids only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

DRAFT_TTL = timedelta(days=7)


@dataclass(frozen=True)
class FireInput:
    ws: str
    schedule_id: str | None
    source: Literal["schedule", "webhook", "manual"]
    public_id: str | None = None
    dedupe_key: str | None = None
    payload_ref: str | None = None  # S3 key of a webhook body, never the body
    actor_user_id: str | None = None


@activity.defn
async def reconcile_schedules() -> dict:
    """Make Temporal match the `SCHED#` rows: create missing, fix drift, delete orphans and stale drafts.

    ponytail: row is truth and edits write the row first then the Schedule, so a crash in between is fixed
    here within a day; a per-automation mutate workflow (spec 06) if edits ever race."""
    from datetime import datetime, timezone

    from core import tasks_schema as ts
    from core.repositories import automations_repo as repo
    from core.services import automation_schedule as asch
    from core.temporal.client import get_client

    out = {"created": 0, "updated": 0, "deleted_orphans": 0, "deleted_drafts": 0}
    cutoff = (datetime.now(timezone.utc) - DRAFT_TTL).isoformat()
    keep: set[str] = set()
    for row in await repo.scan_schedules():
        ws = row["pk"].removeprefix("WS#")
        if activity.in_activity():
            activity.heartbeat(row["sk"])
        if row["status"] == "draft" and row["createdAt"] < cutoff:
            await asch.delete(ws, row["scheduleId"])
            for public_id in row.get("hookPublicIds") or []:
                await repo.delete_hook(ws, public_id)
            await repo.delete_schedule(ws, row["scheduleId"])
            out["deleted_drafts"] += 1
            continue
        keep.add(asch.schedule_id(row))
        changed = await asch.upsert(row)
        if changed:
            out[changed] += 1
    prefix = ts.schedule_workflow_id("", "").split(":", 1)[0] + ":"
    async for listed in await get_client().list_schedules():
        if listed.id.startswith(prefix) and listed.id not in keep:
            await get_client().get_schedule_handle(listed.id).delete()
            out["deleted_orphans"] += 1
    return out


@workflow.defn
class ScheduleReconcileWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            reconcile_schedules,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


WORKFLOWS = [ScheduleReconcileWorkflow]
ACTIVITIES = [reconcile_schedules]
