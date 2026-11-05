"""Automation workflows (spec 06, contracts-v2 §11, §13: lane A owns `TriggerFireWorkflow`).

Workflow code imports only `temporalio` and the stdlib (sandbox-safe); Activities import their I/O modules
inside the function. Payloads carry ids only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

DRAFT_TTL = timedelta(days=7)
# spec 06 §3.6: renew a Google watch 48 h before it expires; three failed renewals pause the automation.
KEEPER_LEAD = timedelta(hours=48)
KEEPER_RETRY = timedelta(hours=1)
KEEPER_MAX_FAILURES = 3


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
    from core.services import automation_hooks
    from core.services import automation_schedule as asch
    from core.temporal.client import get_client

    deletes = sweep_allowed()
    if not deletes:
        logging.getLogger(__name__).warning("reconcile: tasks table is not this namespace's env; no deletes")
    out = {"created": 0, "updated": 0, "deleted_orphans": 0, "deleted_drafts": 0, "keepers": 0}
    cutoff = (datetime.now(timezone.utc) - DRAFT_TTL).isoformat()
    keep: set[str] = set()
    for row in await repo.scan_schedules():
        ws, sid = row["pk"].removeprefix("WS#"), row["scheduleId"]
        _beat(row["sk"])
        if row["status"] == "draft" and row["createdAt"] < cutoff:
            if deletes and await repo.delete_schedule(ws, sid, only_if_status="draft"):
                await asch.delete(ws, sid)
                for public_id in row.get("hookPublicIds") or []:  # provider hook, keeper, secret, row
                    await automation_hooks.delete(ws, public_id)
                out["deleted_drafts"] += 1
            continue
        # The scan is stale: act on the current row (consistent read), so a pause or delete since the scan
        # is not undone. A row gone since the scan gets no Schedule (the sweep below re-checks too).
        row = await repo.get_schedule(ws, sid)
        if row is None:
            continue
        keep.add(asch.schedule_id(row))
        changed = await asch.upsert(row)
        if changed:
            out[changed] += 1
        if row["status"] == "paused_system":
            continue
        for public_id in row.get("hookPublicIds") or []:  # a Google hook without its keeper gets one
            hook = await repo.get_hook(ws, public_id)
            if hook and hook.get("account") and hook.get("expiresAt"):
                await automation_hooks.start_keeper(ws, public_id, automation_hooks.expires_ms(hook["expiresAt"]))
                out["keepers"] += 1
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


@activity.defn
async def renew_google_watch(ws: str, public_id: str) -> int | None:
    from core.services import automation_hooks

    return await automation_hooks.renew(ws, public_id)


@activity.defn
async def mark_watch_broken(ws: str, public_id: str) -> None:
    from core.services import automation_hooks

    await automation_hooks.mark_broken(ws, public_id)


@workflow.defn
class GoogleWatchKeeperWorkflow:
    """One per Google `HOOK#` (id `gwatch:{ws}:{publicId}`): sleeps until 48 h before the watch expires, renews
    it, Continues-As-New with the new expiry. Three failed renewals or a passed expiry pause the automation."""

    @workflow.run
    async def run(self, ws: str, public_id: str, expires_ms: int, failures: int = 0, renewals: int = 0) -> dict:
        expires_at = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
        if expires_at <= workflow.now():
            await self._broken(ws, public_id)
            return {"outcome": "broken", "renewals": renewals}
        renew_at = expires_at - KEEPER_LEAD
        if renew_at > workflow.now():
            await workflow.sleep(renew_at - workflow.now())
        try:
            new_expiry = await workflow.execute_activity(
                renew_google_watch,
                args=[ws, public_id],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError:
            failures += 1
            if failures >= KEEPER_MAX_FAILURES:
                await self._broken(ws, public_id)
                return {"outcome": "broken", "renewals": renewals}
            await workflow.sleep(KEEPER_RETRY)
            workflow.continue_as_new(args=[ws, public_id, expires_ms, failures, renewals])
        if new_expiry is None:  # the hook was deleted
            return {"outcome": "gone", "renewals": renewals}
        workflow.continue_as_new(args=[ws, public_id, new_expiry, 0, renewals + 1])

    async def _broken(self, ws: str, public_id: str) -> None:
        await workflow.execute_activity(
            mark_watch_broken,
            args=[ws, public_id],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


WORKFLOWS = [ScheduleReconcileWorkflow, GoogleWatchKeeperWorkflow]
ACTIVITIES = [reconcile_schedules, renew_google_watch, mark_watch_broken]
