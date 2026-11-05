"""Automation workflows (spec 06, contracts-v2 §11, §13: lane A owns `TriggerFireWorkflow`): Schedule
reconcile, the Google watch keeper, and the fire path (a Schedule action, a verified webhook or Run now → one
deterministic `start_task` trigger, no Router model turn, C31; each run reads the previous run's result).

Workflow code imports only `temporalio` and the stdlib (sandbox-safe); Activities import their I/O modules
inside the function. Payloads carry ids only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError

DRAFT_TTL = timedelta(days=7)
# spec 06 §3.6: renew a Google watch 48 h before it expires; three failed renewals pause the automation.
KEEPER_LEAD = timedelta(hours=48)
KEEPER_RETRY = timedelta(hours=1)
KEEPER_MAX_FAILURES = 3
# A Schedule stamps each fire with its scheduled start; the Activity turns it into the trigger id.
SCHEDULED_START = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
PREVIOUS_RESULT_CHARS = 4_000
PAYLOAD_EXCERPT_CHARS = 2_000
BREAKER_FAILURES = 3
PATCH_AUTOMATION_RUN = "A-automation-run-v1"


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


def schedule_trigger_id(schedule_id: str, tz: str, at_ms: int) -> str:
    """`sched:{sid}:{epoch}` where the epoch is the fire's local wall-clock minute in the automation's zone
    read as if it were UTC. A DST fall-back fires a 01:30 schedule twice (S-04, recorded in
    test_schedules_dst); both map to the same minute, so the second is the same trigger and is not admitted."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    local = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz))
    minute = local.replace(second=0, microsecond=0, tzinfo=timezone.utc)
    from core import tasks_schema as ts

    return ts.trigger_id("schedule", schedule_id, int(minute.timestamp()))


def _fire_trigger_id(inp: FireInput, row: dict, at_ms: int) -> str:
    from core import tasks_schema as ts

    if inp.source == "schedule":
        return schedule_trigger_id(row["scheduleId"], row["tz"], at_ms)
    if inp.source == "webhook":
        return ts.trigger_id("webhook", inp.public_id, inp.dedupe_key)
    return ts.trigger_id("manual", row["scheduleId"], inp.dedupe_key or at_ms)


async def _payload_excerpt(payload_ref: str | None) -> str | None:
    if not payload_ref:
        return None
    import boto3

    from core.config import settings
    from core.dynamodb import run_in_thread

    s3 = boto3.client("s3", region_name=settings.AWS_REGION)
    obj = await run_in_thread(s3.get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=payload_ref)
    return obj["Body"].read(PAYLOAD_EXCERPT_CHARS * 4).decode("utf-8", errors="replace")[:PAYLOAD_EXCERPT_CHARS]


@activity.defn
async def fire_automation(inp: FireInput, at_ms: int) -> str:
    """One fire → one trigger with a deterministic `start_task` in `ref["start"]` (no Router model turn, C31).
    Returns the trigger id, or `skipped:paused` / `skipped:overlap`. Idempotent on retry: the same fire maps
    to the same trigger and task ids, the claim accepts its own task id again, the trigger put is conditional."""
    from core import tasks_schema as ts
    from core.dynamodb import utc_now_iso
    from core.repositories import automations_repo as repo
    from core.repositories import tasks_repo
    from core.services import admission, task_conversation

    ws = inp.ws
    row = await repo.get_schedule(ws, inp.schedule_id) if inp.schedule_id else None
    if row is None or row["status"] != "active":
        return "skipped:paused"
    trigger_id = _fire_trigger_id(inp, row, at_ms)
    task_id = ts.router_task_id(trigger_id, 0, 0)
    previous = next((t for t in row.get("runTaskIds") or [] if t != task_id), None)  # newest closed run
    if not await repo.claim_run(ws, row["scheduleId"], task_id):
        return "skipped:overlap"

    brief = row["policyText"]
    if previous:
        prev_task = await tasks_repo.get_task(ws, previous)
        result = await task_conversation.last_result_text(ws, previous)
        if prev_task or result:
            status = (prev_task or {}).get("status", "unknown")
            brief += f"\n\nPrevious run ({status}): {(result or 'no result posted')[:PREVIOUS_RESULT_CHARS]}"
    excerpt = await _payload_excerpt(inp.payload_ref)
    if excerpt:
        brief += f"\n\nWebhook payload:\n{excerpt}"

    deliver_to = row.get("deliverTo") or "none"
    room_id = deliver_to.removeprefix("room:") if deliver_to.startswith("room:") else None
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=row["name"],
        brief=brief,
        specialist=row.get("specialist"),
        ack=f"{row['name']}: started.",
        visibility="room" if room_id else "person",
        policy_ref=ts.schedule_sk(row["scheduleId"]),
        budget_usd=row.get("budgetPerRunUsd"),
        previous_run_task_id=previous,
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=ws,
        trigger_id=trigger_id,
        source=inp.source,
        admission="respond",
        lane="shared",
        # Run now carries the person who pressed it; a Schedule or webhook fire has nobody behind it, so the
        # automation's owner is the actor and the task lands in that person's lane (or the deliverTo room).
        actor_user_id=inp.actor_user_id or row["ownerUserId"],
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "scheduleId": row["scheduleId"],
            "taskId": task_id,  # the id the deterministic start creates (= router_task_id(trigger_id, 0, 0))
            "deliverTo": deliver_to,
            "payloadRef": inp.payload_ref,
            "publicId": inp.public_id,
            "start": start.model_dump(by_alias=True),
        },
    )
    await admission.enqueue_trigger(ws, trigger, room_id=room_id)
    return trigger_id


@activity.defn
async def close_automation_run(ws: str, task_id: str, failed: bool) -> None:
    """After a task's attempt ended: when the task is an automation run (`policyRef` = `SCHED#…`), free the
    claim, record the run, count the failure; three in a row pause the automation (`paused_system: failures`).
    ponytail: one GetItem per ended attempt of every task; carry policyRef on TaskStart if it ever matters."""
    from core.repositories import automations_repo as repo
    from core.repositories import tasks_repo
    from core.repositories.tasks_repo import IllegalTransition, VersionConflict
    from core.services import automation_schedule

    task = await tasks_repo.get_task(ws, task_id)
    policy_ref = (task or {}).get("policyRef") or ""
    if not policy_ref.startswith("SCHED#"):
        return
    sid = policy_ref.removeprefix("SCHED#")
    try:
        row = await repo.close_run(ws, sid, task_id, failed=failed)
    except VersionConflict:  # not this automation's active run (already closed, or replaced)
        return
    if row.get("consecutiveFailures", 0) >= BREAKER_FAILURES and row["status"] == "active":
        try:
            row = await repo.set_status(ws, sid, "paused_system", reason="failures", allowed_from=frozenset({"active"}))
        except IllegalTransition:
            return
        await automation_schedule.upsert(row)


@workflow.defn
class TriggerFireWorkflow:
    """A Schedule action, a verified webhook or Run now. One Activity; the trigger id is derived from the
    Schedule's scheduled start (search attribute) so a retried fire is the same fire."""

    @workflow.run
    async def run(self, inp: FireInput) -> str:
        at = workflow.info().typed_search_attributes.get(SCHEDULED_START)
        at_ms = int((at or workflow.now()).timestamp() * 1000)
        return await workflow.execute_activity(
            fire_automation,
            args=[inp, at_ms],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )


class AutomationRunMixin:
    """TaskWorkflow hooks (≤8 call-site lines in workflows.py): an automation run is closed when its task
    stops continuing: after an ended attempt (not `waiting_approval` or `runtime_lost`, which go on), and on
    every transition to resolved/failed that ends no attempt (lost runtime, Stop while queued, dismiss/resolve,
    the quiet timeout). Closed once per workflow run; the Activity is a no-op for anything else."""

    async def _a_after_attempt(self, outcome: str) -> None:
        if self._status in ("working", "waiting"):
            return
        # A person's Stop (`cancelled`) is not a failure of the automation (lead ruling, 5 Nov).
        await self._a_close(outcome not in ("completed", "cancelled"))

    async def _a_on_transition(self, to: str) -> None:
        if to in ("resolved", "failed"):
            await self._a_close(to == "failed")

    async def _a_close(self, failed: bool) -> None:
        if getattr(self, "_a_closed", False):
            return
        self._a_closed = True
        await workflow.execute_activity(
            close_automation_run,
            args=[self._ws, self._task_id, failed],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )


WORKFLOWS = [ScheduleReconcileWorkflow, GoogleWatchKeeperWorkflow, TriggerFireWorkflow]
ACTIVITIES = [reconcile_schedules, renew_google_watch, mark_watch_broken, fire_automation, close_automation_run]
