"""`LearningWorkflow` (`learn:{ws}:{specialistId}`): the ONE writer that folds what a task attempt
learned back into its specialist's profile (spec 07 §3.5; contracts-v2 §15).

Attempt ends arrive by `signal_with_start(merge, MergeRef)` from `close_task_attempt` (called by O's
`end_attempt` for every task-session attempt), queue FIFO, and run one `merge_learning` Activity at
a time; when the queue drains and at least one merge applied, `curate_specialist` runs the curator on
the WORKSPACE VM (never in a Worker). Continue-As-New every `CAN_AFTER_MERGES`. A `MergeRef` seen
before is dropped (a replayed signal), and `merge_learning` is idempotent on its `LEARN#` row anyway.

Layout the bridge's task mode (V2) writes, read here:
  specialist profile  <ws>/hermes/<agent>/                    (C, the only thing this module writes)
  task overlay        <ws>/hermes-tasks/<task>/<agent>/       (T: the files the task saved,
                      base-{attempt:04d}.json at each attempt's first restore, sealed.json after
                      the final save)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from core import tasks_schema as ts
    from core.config import settings
    from core.dynamodb import run_in_thread
    from core.repositories import learning_repo, tasks_repo
    from core.services import hermes_profiles, learning_merge as lm, runtime_runs, runtime_transport
    from core.services.hermes_threat_patterns import scan_for_threats
    from core.temporal.client import get_client

logger = logging.getLogger(__name__)

CAN_AFTER_MERGES = 500
SEEN_MAX = 500
OVERLAY_ROOT = "hermes-tasks"

_MERGE = {
    "start_to_close_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0),
}
# Every curate failure is transient (curate_specialist): the bridge caches its roster 60 s, so a
# specialist first written within that window is a 400 until it refreshes. 30 s + 60 s + 120 s of
# backoff outlasts that; after the last attempt the next applied merge tries again. Activity
# options only (no new command): replay-safe without a patch marker.
_CURATE = {
    "start_to_close_timeout": timedelta(minutes=10),
    "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=30), backoff_coefficient=2.0, maximum_attempts=4),
}


@dataclass(frozen=True)
class MergeRef:
    task_id: str
    attempt: int


@dataclass(frozen=True)
class LearningState:
    """Start and Continue-As-New argument: what is queued, what was seen, merges not yet curated."""

    ws: str
    specialist_id: str
    queue: list[MergeRef] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    applied_since_curate: int = 0


def _key(ref: MergeRef) -> str:
    return f"{ref.task_id}#{ref.attempt:04d}"


@workflow.defn
class LearningWorkflow:
    @workflow.init
    def __init__(self, state: LearningState) -> None:
        self._ws = state.ws
        self._specialist = state.specialist_id
        self._queue: list[MergeRef] = list(state.queue)
        self._seen: list[str] = list(state.seen)
        self._seen_set = set(self._seen)
        self._applied = state.applied_since_curate
        self._merges = 0

    @workflow.signal
    def merge(self, ref: MergeRef) -> None:
        key = _key(ref)
        if key in self._seen_set:
            return  # a replayed close_task_attempt
        self._seen.append(key)
        self._seen_set.add(key)
        if len(self._seen) > SEEN_MAX:
            for old in self._seen[:-SEEN_MAX]:
                self._seen_set.discard(old)
            self._seen = self._seen[-SEEN_MAX:]
        self._queue.append(ref)

    @workflow.query
    def pending(self) -> list[MergeRef]:
        return list(self._queue)

    def _should_continue_as_new(self) -> bool:
        return self._merges >= CAN_AFTER_MERGES or workflow.info().is_continue_as_new_suggested()

    @workflow.run
    async def run(self, state: LearningState) -> None:
        while True:
            await workflow.wait_condition(lambda: bool(self._queue) or self._should_continue_as_new())
            # Stop at the CAN threshold even with work queued: the rest crosses over in the state.
            while self._queue and not self._should_continue_as_new():
                ref = self._queue[0]  # stays queued (visible, deduped) until its merge is done
                try:
                    report = await workflow.execute_activity(
                        "merge_learning",
                        args=[self._ws, self._specialist, ref.task_id, ref.attempt],
                        result_type=dict,
                        **_MERGE,
                    )
                    if report.get("applied"):
                        self._applied += 1
                except ActivityError:
                    # Never let one attempt's merge stop the specialist's queue: the next attempt of
                    # that task merges against the earliest unapplied base and carries this one.
                    workflow.logger.error("learning merge failed task=%s attempt=%s", ref.task_id, ref.attempt)
                self._queue.pop(0)
                self._merges += 1
            if self._applied:
                try:
                    await workflow.execute_activity(
                        "curate_specialist", args=[self._ws, self._specialist], result_type=dict, **_CURATE
                    )
                except ActivityError:
                    workflow.logger.warning("curator did not run for %s (next merge retries it)", self._specialist)
                self._applied = 0
            if self._should_continue_as_new():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(
                    LearningState(self._ws, self._specialist, list(self._queue), list(self._seen), self._applied)
                )


# --- Activities ---------------------------------------------------------------------------------------


def overlay_prefix(ws: str, task_id: str, agent_id: str) -> str:
    return f"{ws}/{OVERLAY_ROOT}/{task_id}/{agent_id}/"


def _s3_client():
    import boto3

    return boto3.client("s3")


def _get(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 -- botocore ClientError; only "absent" is swallowed
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in ("NoSuchKey", "404", "NotFound"):
            return None
        raise


def _threat(entry: str) -> bool:
    return bool(scan_for_threats(entry, scope="strict"))


def _merge_sync(
    ws: str, specialist_id: str, task_id: str, attempt: int, *, private: bool, prior: dict | None, interrupted: bool
) -> dict:
    s3, bucket = _s3_client(), settings.OWNER_STATE_BUCKET
    overlay = overlay_prefix(ws, task_id, specialist_id)
    specialist = hermes_profiles.profile_prefix(ws, specialist_id)
    overlay_files = lm.read_snapshot(s3, bucket, overlay)
    seal = _get(s3, bucket, overlay + lm.SEALED)
    status = lm.check_seal(seal, attempt, overlay_files)
    # Merge against the base of the earliest attempt of this task whose learning never REACHED the
    # profile (no row, or a report that applied nothing): T carries every attempt since, so nothing
    # is counted twice and nothing is lost (learning_merge docstring). A superseded or unsealed
    # attempt is marked applied for idempotence but applied nothing (#4762 review).
    base_attempt = next(
        (
            k
            for k in range(1, attempt + 1)
            if not ((learning_repo.get_sync(ws, specialist_id, task_id, k) or {}).get("report") or {}).get("applied")
        ),
        attempt,
    )
    base_raw = _get(s3, bucket, overlay + lm.base_rel(base_attempt))
    base, base_text = lm.parse_base(base_raw) if base_raw else ({}, {})
    if base_raw is None:
        logger.warning("learning merge: no base for task=%s attempt=%s; adding only", task_id, base_attempt)

    def plan_for(current: lm.Snapshot, planned: dict[str, str]) -> lm.MergePlan:
        # The layered view needs C (an untouched file's bytes come from it): built per plan, from the
        # very snapshot this plan merges against, never a second read of the profile.
        task_view = lm.layered_task_view(seal, overlay_files, current) if status is None else overlay_files
        plan = lm.plan_merge(
            base,
            task_view,
            current,
            private=private,
            sealed=status != "unsealed",
            superseded=status == "superseded",
            threat_scan=_threat,
            task_id=task_id,
            base_text=base_text,
            planned=planned,
        )
        # The intent row BEFORE every apply, including the replans a 412 forces.
        learning_repo.put_planned_sync(ws, specialist_id, task_id, attempt, plan.planned)
        return plan

    _, report = lm.run_merge(s3, bucket, specialist, plan_for, planned=prior, force_revision=interrupted)
    return report


@activity.defn
async def merge_learning(ws: str, specialist_id: str, task_id: str, attempt: int) -> dict:
    """Idempotent: an `applied` row returns its report; a `planned` row (an interrupted earlier run)
    hands its shas to the planner and bumps `.revision` regardless. Raises `CurrentChanged` after
    three lost conditional writes, for Temporal's retry."""
    existing = await learning_repo.get(ws, specialist_id, task_id, attempt)
    if existing and existing.get("state") == "applied":
        return existing.get("report") or {}
    task = await tasks_repo.get_task(ws, task_id)
    if task is None:  # the task row is gone: nothing of it is merged, and the row says so
        report = lm._empty_report("no_task")
        await learning_repo.put_planned(ws, specialist_id, task_id, attempt, {})
        await learning_repo.mark_applied(ws, specialist_id, task_id, attempt, report)
        return report
    private = task.get("lane") == "private"
    prior = (existing or {}).get("planned") or None
    # A planned row is an interrupted earlier run: its writes may have landed without the
    # `.revision` bump, whatever it had planned (even a delete-only plan).
    interrupted = (existing or {}).get("state") == "planned"
    report = await run_in_thread(
        _merge_sync, ws, specialist_id, task_id, attempt, private=private, prior=prior, interrupted=interrupted
    )
    await learning_repo.mark_applied(ws, specialist_id, task_id, attempt, report)
    return report


@activity.defn
async def curate_specialist(ws: str, specialist_id: str) -> dict:
    """The curator for one specialist on the workspace VM (`POST /internal/v1/curate`). A refusal is
    transient here, never final: 400 = a specialist the bridge's 60 s roster cache does not list
    yet, 404 = an image without the route until the roll, 409 = a live turn."""
    try:
        return await runtime_runs.curate(ws, specialist_id)
    except (runtime_runs.RunRefused, runtime_runs.RunLost) as exc:
        raise runtime_runs.RuntimeBusy(f"curate refused: {exc}") from exc


async def signal_merge(ws: str, specialist_id: str, ref: MergeRef) -> None:
    """Signal-with-start the specialist's `LearningWorkflow` with one attempt to merge."""
    client, wid = get_client(), ts.learning_workflow_id(ws, specialist_id)
    try:
        await client.start_workflow(
            "LearningWorkflow",
            LearningState(ws=ws, specialist_id=specialist_id),
            id=wid,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="merge",
            start_signal_args=[ref],
        )
    except WorkflowAlreadyStartedError:
        # Two attempt ends landing together: the server may refuse the second start-with-signal
        # (seen on the test server); the running workflow takes the signal directly.
        await client.get_workflow_handle(wid).signal("merge", ref)


@activity.defn
async def close_task_attempt(ws: str, task_id: str, specialist_id: str, attempt: int) -> None:
    """After the seal: stop the task's own session, THEN queue its merge. Idempotent -- a stop of a
    gone session is a stop, and the workflow drops a `MergeRef` it has seen."""
    await runtime_runs.stop_task_session(ws, task_id)
    await signal_merge(ws, specialist_id, MergeRef(task_id, attempt))


def is_task_session_attempt(ws: str, task_id: str, att: dict) -> bool:
    """Whether this Attempt row ran in its own task session (O's `begin_attempt` stores the id)."""
    return att.get("sessionId") == runtime_transport.session_id_for(ws, task_id)


async def close_task_attempt_if_task_session(ws: str, task_id: str, att: dict, attempt: int) -> None:
    """O's `end_attempt` hook: nothing for a shared-VM attempt (today's path), else `close_task_attempt`.
    Never fails `end_attempt`: a Temporal/AgentCore blip here is logged; the task session ages out on
    its own and the next attempt's merge carries this one's learning (earliest-unapplied base)."""
    if not is_task_session_attempt(ws, task_id, att):
        return
    try:
        await close_task_attempt(ws, task_id, att["agentId"], attempt)
    except Exception:  # noqa: BLE001
        logger.warning("close_task_attempt failed task=%s attempt=%s", task_id, attempt, exc_info=True)


WORKFLOWS = [LearningWorkflow]
ACTIVITIES = [merge_learning, curate_specialist, close_task_attempt]
