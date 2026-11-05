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


def sweep_allowed() -> bool:
    """Whether this worker may delete Schedules: only when its tasks table belongs to the namespace's env.

    The orphan sweep deletes every `sched:` Schedule with no row, so a worker reading another table (a
    developer's local DynamoDB, a prod table) against `lightbulb-dev` would wipe that namespace. A local dev
    server (`default`) owns nothing shared."""
    from core import dynamodb
    from core.config import settings

    ns = settings.TEMPORAL_NAMESPACE
    if ns == "default":
        return True
    if getattr(settings, "DYNAMODB_ENDPOINT_URL", None):
        return False
    return dynamodb.table_name("tasks").startswith(ns.split(".", 1)[0] + "-")


def _beat(detail: str) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


@activity.defn
async def reconcile_schedules() -> dict:
    """Make Temporal match the `SCHED#` rows: create missing, fix drift, delete orphans and stale drafts.

    ponytail: row is truth and edits write the row first then the Schedule, so a crash in between is fixed
    here within a day; a per-automation mutate workflow (spec 06) if edits ever race."""
    import logging
    from datetime import datetime, timezone

    from core import tasks_schema as ts
    from core.repositories import automations_repo as repo
    from core.services import automation_schedule as asch
    from core.services.credentials import integration_secret_store
    from core.temporal.client import get_client

    deletes = sweep_allowed()
    if not deletes:
        logging.getLogger(__name__).warning("reconcile: tasks table is not this namespace's env; no deletes")
    out = {"created": 0, "updated": 0, "deleted_orphans": 0, "deleted_drafts": 0}
    cutoff = (datetime.now(timezone.utc) - DRAFT_TTL).isoformat()
    keep: set[str] = set()
    for row in await repo.scan_schedules():
        ws, sid = row["pk"].removeprefix("WS#"), row["scheduleId"]
        _beat(row["sk"])
        if row["status"] == "draft" and row["createdAt"] < cutoff:
            if deletes and await repo.delete_schedule(ws, sid, only_if_status="draft"):
                await asch.delete(ws, sid)
                # ponytail: provider-side hooks of a never-activated draft are left registered (the URL then
                # 404s); unregister them here if drafts ever register provider hooks (A8 registers on create).
                for public_id in row.get("hookPublicIds") or []:
                    hook = await repo.get_hook(ws, public_id)
                    if hook:
                        await integration_secret_store.delete(ws, hook["secretRef"])
                    await repo.delete_hook(ws, public_id)
                out["deleted_drafts"] += 1
            continue
        keep.add(asch.schedule_id(row))
        changed = await asch.upsert(row)
        if changed:
            out[changed] += 1
    if not deletes:
        return out
    prefix = ts.schedule_workflow_id("", "").split(":", 1)[0] + ":"
    async for listed in await get_client().list_schedules():
        _beat(listed.id)
        if not listed.id.startswith(prefix) or listed.id in keep:
            continue
        ws, _, sid = listed.id.removeprefix(prefix).partition(":")
        if await repo.get_schedule(ws, sid):  # created after the scan (consistent read): not an orphan
            continue
        await asch.delete(ws, sid)  # NOT_FOUND = someone else deleted it: done
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
