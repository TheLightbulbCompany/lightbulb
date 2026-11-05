"""`RouterWorkflow` and `TaskWorkflow` (wave-2 O Task 2; contracts-v2 §7, §9; spec 01 §3.4–§3.9).

Sandbox-pure: imports only `temporalio`, the stdlib and `core.temporal.types`. Activities are called by name
(`core/temporal/activities.py` registers them), and every payload is ids/enums/numbers.

`TaskWorkflow` is the only writer of task status, attempts and subscriptions: each change is an Activity it runs.
Lanes Q, A and S add behaviour through the four hook methods (`_on_waiting`, `_before_attempt`,
`_after_attempt`, `_on_stop`) with a few call-site lines, never by editing the loop.

Replay: no `workflow.patched` before the first dev deploy; after it, every behaviour change takes
`"<lane>-<slug>-v1"` and a recorded history in `tests/unit/super_agent/orchestration/histories/`.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeout
from temporalio.workflow import ActivityCancellationType

from core.temporal.automations import PATCH_AUTOMATION_RUN, AutomationRunMixin
from core.temporal.task_questions import TaskQuestionsMixin
from core.temporal.watches import WatchMixin, WatchSpec
from core.temporal.types import (
    ActionRequest,
    AttemptRef,
    RouterState,
    RouterTurnIn,
    RouterTurnOut,
    RunEvent,
    StopRequest,
    StopResult,
    TaskInputRef,
    TaskStart,
    TriggerRef,
    WorkerTurnIn,
    WorkerTurnOut,
)

SEEN_MAX = 2_000
BATCH_MAX = 20
CAN_AFTER_SIGNALS = 2_000
QUIET_AFTER = timedelta(days=7)
MAX_SAME_RUN_RECONCILES = 3
MAX_LOST_ATTEMPTS = 3  # the first attempt plus two new ones, then `failed`
# A Router batch is one lane in one room: "shared" spans rooms, and a batch's memory audience, open tasks and
# fallback line are one room's (audit #6). Histories without the marker keep batching by lane.
PATCH_ROUTER_ROOM_BATCH = "o-router-room-batch-v1"

_SHORT = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=5)}
_ROUTER_TURN = {
    "start_to_close_timeout": timedelta(minutes=5),
    "heartbeat_timeout": timedelta(seconds=60),
    "retry_policy": RetryPolicy(maximum_attempts=4),  # 3 retries
}
_WORKER_TURN = {
    "start_to_close_timeout": timedelta(hours=6, minutes=30),  # under the bridge's 7 h recycle (§14.11)
    "heartbeat_timeout": timedelta(minutes=6),  # the bridge posts a heartbeat every 2 min
    "retry_policy": RetryPolicy(maximum_attempts=1),
    "cancellation_type": ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
}

PATCH_CANCEL_QUESTIONS_ON_END = "O-cancel-questions-on-end-v1"  # audit #5
PATCH_RECONCILE_CANCEL_QUESTIONS = "O-reconcile-cancel-questions-v1"  # a watchdog-failed orphan's open Questions
SYSTEM_ACTOR = "__system__"  # == core.constants.SYSTEM_ACTOR_ID (the sandbox imports no core modules)

# Outcome → the status `end_attempt` leaves the task in (the workflow mirrors it to pick its next wait).
_STATUS_AFTER = {
    "completed": "idle",
    "cancelled": "resolved",
    "waiting_approval": "waiting",
    "runtime_lost": "working",
}


async def _act(name: str, *args: Any, result_type: type | None = None, **opts: Any) -> Any:
    return await workflow.execute_activity(name, args=list(args), result_type=result_type, **(opts or _SHORT))


def _error_name(err: BaseException) -> str:
    cause = err.cause if isinstance(err, ActivityError) and err.cause else err
    if isinstance(cause, ApplicationError) and cause.type:
        return cause.type
    return type(cause).__name__


@workflow.defn
class RouterWorkflow:
    """`router:{ws}`: batches admitted triggers per lane and runs one `router_turn` per batch."""

    @workflow.init
    def __init__(self, state: RouterState) -> None:
        self._ws = state.ws
        self._queue: list[TriggerRef] = list(state.queue)
        self._seen: list[str] = list(state.seen)
        self._seen_set = set(self._seen)
        self._signals = 0

    @workflow.signal
    def trigger(self, ref: TriggerRef) -> None:
        self._signals += 1
        if ref.trigger_id in self._seen_set:
            return
        self._seen.append(ref.trigger_id)
        self._seen_set.add(ref.trigger_id)
        if len(self._seen) > SEEN_MAX:
            for old in self._seen[:-SEEN_MAX]:
                self._seen_set.discard(old)
            self._seen = self._seen[-SEEN_MAX:]
        self._queue.append(ref)

    def _should_continue_as_new(self) -> bool:
        return self._signals >= CAN_AFTER_SIGNALS or workflow.info().is_continue_as_new_suggested()

    @workflow.run
    async def run(self, state: RouterState) -> None:
        while True:
            await workflow.wait_condition(lambda: bool(self._queue) or self._should_continue_as_new())
            if self._queue:
                head = self._queue[0]
                lane = head.lane
                by_room = workflow.patched(PATCH_ROUTER_ROOM_BATCH)
                batch = [r for r in self._queue if r.lane == lane and (not by_room or r.room_id == head.room_id)]
                batch = batch[:BATCH_MAX]
                taken = {r.trigger_id for r in batch}
                self._queue = [r for r in self._queue if r.trigger_id not in taken]
                trigger_ids = [r.trigger_id for r in batch]
                batch_id = str(workflow.uuid4())
                try:
                    await _act(
                        "router_turn",
                        RouterTurnIn(self._ws, lane, batch_id, trigger_ids),
                        result_type=RouterTurnOut,
                        **_ROUTER_TURN,
                    )
                except ActivityError:
                    try:
                        await _act("apply_fallback", self._ws, trigger_ids)
                    except ActivityError:
                        # Never let one batch kill the Router (the queue lives in this run).
                        workflow.logger.error("router fallback failed for batch %s", batch_id)
            if self._should_continue_as_new():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(RouterState(self._ws, list(self._queue), list(self._seen)))


@workflow.defn
class TaskWorkflow(TaskQuestionsMixin, AutomationRunMixin, WatchMixin):
    """`task:{ws}:{taskId}`: slot lease → Attempt → `worker_turn` (async, bridge-completed) → `end_attempt`."""

    @workflow.init
    def __init__(self, start: TaskStart) -> None:
        self._ws, self._task_id = start.ws, start.task_id
        # `migrated` = started by a signal/update-with-start: wait for what came with it.
        self._attempt_needed = start.reason != "migrated"
        # Consent for a proposed task is an explicit Start only (the route's start, or the `start` signal).
        self._started = start.reason == "propose_start"
        self._status: str | None = None  # mirror of the row's status, None until this run changes it
        self._pending: list[TaskInputRef] = []
        self._seen_inputs: set[str] = set()
        self._action: tuple[str, str] | None = None  # (action, by_user_id) for resolve/dismiss
        self._stop_by: str | None = None
        self._slot_free = False
        self._attempt: AttemptRef | None = None
        self._turn: workflow.ActivityHandle | None = None
        self._wakes = 0  # bumped by every input/start/retry/reopen
        self._blocked_at: int | None = None  # `_wakes` when budget or proposed status deferred an attempt
        self._awaiting_start = False  # begin_attempt found a proposed task: only Start runs it
        self.last_event: RunEvent | None = None
        self._q_init()  # Lane Q: open questions (task_questions.py)
        self._w_init()  # Lane A: watches (watches.py)

    # --- hooks (Q, A, S add behaviour here) ------------------------------------------------------------

    async def _on_waiting(self) -> None:
        """The task is about to wait for inputs or actions (idle/waiting)."""
        self._w_wake = False  # one-shot: an add/poke/unwatch brought the task back here
        if not self._watches:
            await self._q_on_waiting()
        # While watches run, the watch loop waits (checks go on with a Question open) and hands Q's work
        # (an approved draft to land, a reminder due) back here; Q's own wait takes over once no watch is left.
        watched = False  # without a watch this is exactly Q's wait above: no new commands for older histories
        while self._watches and not self._woken() and self._status != "resolved":
            watched = True
            await self._w_on_waiting()
            if self._status != "resolved":
                await self._q_serve()
        if watched and self._open_qs and not self._woken() and self._status != "resolved":
            await self._q_on_waiting()  # a give-up or unwatch left a Question open: Q's wait, no deadline

    async def _before_attempt(self) -> bool:
        """False ⇒ do not start an attempt now; the workflow waits for the next input or action."""
        return await self._q_before_attempt()

    async def _after_attempt(self, outcome: str) -> None:
        """An attempt ended (after `end_attempt`)."""
        await self._q_after_attempt(outcome)
        if workflow.patched(PATCH_AUTOMATION_RUN):  # Lane A: an automation run closes with its task
            await self._a_after_attempt(outcome)

    async def _on_stop(self, by_user_id: str) -> None:
        """A person stopped the task, before the running turn is cancelled (Q cancels open questions)."""
        await self._q_on_stop(by_user_id)

    # --- signals ---------------------------------------------------------------------------------------

    @workflow.signal
    async def input(self, ref: TaskInputRef) -> None:
        if ref.trigger_id in self._seen_inputs:
            return
        self._seen_inputs.add(ref.trigger_id)
        self._pending.append(ref)
        self._wakes += 1
        attempt = self._attempt
        if attempt is not None and self._turn is not None and not self._turn.done():
            # Steer the live run; not accepted ⇒ stays pending for the next attempt (lost-steer redispatch, §14.4).
            if await _act("deliver_input", self._ws, self._task_id, attempt.n, ref.trigger_id, result_type=bool):
                self._pending = [p for p in self._pending if p.trigger_id != ref.trigger_id]

    @workflow.signal
    def start(self) -> None:
        self._started = True
        self._attempt_needed = True
        self._awaiting_start = False
        self._wakes += 1

    # Lane A: watches (watches.py)
    @workflow.update
    async def add_watch(self, spec: WatchSpec) -> str:
        return await self._w_add(spec)

    @workflow.signal
    def watch_poke(self, watch_id: str) -> None:
        self._w_poke(watch_id)

    @workflow.signal
    def unwatch(self, watch_id: str) -> None:
        self._w_remove(watch_id)

    @workflow.signal
    def run_event(self, event: RunEvent) -> None:
        """Heartbeats are recorded; the terminal result arrives by Activity completion, this only wakes waits."""
        self.last_event = event

    @workflow.signal
    def slot_free(self) -> None:
        self._slot_free = True

    # --- updates ---------------------------------------------------------------------------------------

    def _running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    @workflow.update
    async def stop(self, req: StopRequest) -> StopResult:
        if self._status == "resolved" or self._stop_by is not None:
            return StopResult("not_running")
        self._stop_by = req.by_user_id
        # Local copies: the turn may end (and clear these) during any await below.
        attempt, turn = self._attempt, self._turn
        await self._on_stop(req.by_user_id)
        if attempt is not None and turn is not None and not turn.done():
            await _act("mark_stop_requested", self._ws, self._task_id, req.by_user_id)
            await _act("stop_attempt", self._ws, self._task_id, attempt.n)
            if not turn.done():
                turn.cancel()
                return StopResult("stopping")
        await workflow.wait_condition(lambda: self._status == "resolved")
        return StopResult("stopped")

    @stop.validator
    def _validate_stop(self, req: StopRequest) -> None:
        if not req.by_user_id:
            raise ValueError("stop names who stopped it")

    def _queue_action(self, action: str, req: ActionRequest) -> None:
        self._action = (action, req.by_user_id)

    @workflow.update
    async def retry(self, req: ActionRequest) -> None:
        self._attempt_needed = True
        self._wakes += 1

    @retry.validator
    def _validate_retry(self, req: ActionRequest) -> None:
        if self._running() or self._status not in (None, "failed", "idle"):
            raise ValueError("retry needs a failed or idle task")

    @workflow.update
    async def reopen(self, req: ActionRequest) -> None:
        self._attempt_needed = True
        self._wakes += 1

    @reopen.validator
    def _validate_reopen(self, req: ActionRequest) -> None:
        if self._status not in (None, "resolved", "failed", "idle"):
            raise ValueError("reopen needs a resolved, failed or idle task")

    @workflow.update
    async def resolve(self, req: ActionRequest) -> None:
        self._queue_action("resolve", req)

    @resolve.validator
    def _validate_resolve(self, req: ActionRequest) -> None:
        if self._running() or self._status == "resolved":
            raise ValueError("resolve needs a task that is not running (stop it first)")

    @workflow.update
    async def dismiss(self, req: ActionRequest) -> None:
        self._queue_action("dismiss", req)

    @dismiss.validator
    def _validate_dismiss(self, req: ActionRequest) -> None:
        if self._started or self._status not in (None, "proposed"):
            raise ValueError("dismiss needs a proposed task")

    # --- main loop -------------------------------------------------------------------------------------

    async def _transition(self, to: str, reason: str | None) -> None:
        # A task that ends leaves no open Question (Waiting on you, Activity, away email): a resolve, the quiet
        # resolve, no_answerer, a failure. A Stop cancels them itself, with the person's id (`_q_on_stop`).
        if to in ("resolved", "failed") and self._stop_by is None and workflow.patched(PATCH_CANCEL_QUESTIONS_ON_END):
            try:
                await self._q_cancel_open(self._action[1] if self._action else SYSTEM_ACTOR, reason or to)
            except ActivityError:  # the task still ends; a Question left open is shown, never answerable
                workflow.logger.error("cancel_open_questions failed for task %s", self._task_id)
        await _act("transition", self._ws, self._task_id, to, reason)
        self._status = to
        if workflow.patched(PATCH_AUTOMATION_RUN):  # Lane A: an automation run ends with its task
            await self._a_on_transition(to)

    def _deferred(self) -> bool:
        """An attempt was refused (budget, proposed) and nothing new has arrived since."""
        return self._awaiting_start or self._blocked_at == self._wakes

    def _woken(self) -> bool:
        work = (bool(self._pending) or self._attempt_needed) and not self._deferred()
        return work or self._action is not None or self._stop_by is not None or self._w_wake

    @workflow.run
    async def run(self, start: TaskStart) -> None:
        lost = 0
        # A reconcile start (the watchdog) runs the loop only when it restarted a queued orphan; it still
        # waits for handlers below, so an update racing the start is never left hanging.
        active = start.reason != "reconcile" or await self._reconcile()
        while active:
            if self._stop_by is not None:
                if self._status != "resolved":
                    await self._transition("resolved", "stopped")
                break
            if self._action is not None:
                action, _ = self._action
                await self._transition("resolved", "dismissed" if action == "dismiss" else "resolved")
                break
            if (self._attempt_needed or self._pending) and not self._deferred():
                self._attempt_needed = False
                outcome = await self._run_attempt()
                if outcome is None:
                    continue
                if outcome.outcome == "runtime_lost":
                    lost += 1
                    if lost >= MAX_LOST_ATTEMPTS:
                        await self._transition("failed", "runtime_lost")
                    else:
                        self._attempt_needed = True
                    continue
                lost = 0
                if self._status == "resolved":
                    break
                continue
            await self._on_waiting()
            if self._status == "resolved":  # a watch's `fail` give-up ended the task while it waited
                break
            try:
                await workflow.wait_condition(self._woken, timeout=QUIET_AFTER)
            except asyncio.TimeoutError:
                await self._transition("resolved", "quiet")
                break
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _reconcile(self) -> bool:
        """The watchdog's start for an orphaned row (spec 11 §3.6, Lane W). Any attempt left open is closed as
        `runtime_lost` first (releases its slot). A queued task then runs normally (True); a working one is
        failed `lost_run` and never re-attempted; a task that moved on is left alone."""
        state = await _act("reconcile_state", self._ws, self._task_id, result_type=str)
        if state == "other":
            return False
        status, n, live = state.split(":")
        if live == "live":
            await _act("end_attempt", self._ws, self._task_id, int(n), "runtime_lost", "lost_run")
        if status == "queued":
            self._attempt_needed = True
            return True
        if live != "live":
            await _act("release_slot", self._ws, self._task_id)
        if workflow.patched(PATCH_RECONCILE_CANCEL_QUESTIONS):
            # This run never tracked the orphan's Questions (`_open_qs` is empty): cancel them from the store.
            try:
                await _act("cancel_open_questions", self._ws, self._task_id, SYSTEM_ACTOR, "lost_run")
            except ActivityError:
                workflow.logger.error("cancel_open_questions failed for task %s", self._task_id)
        await self._transition("failed", "lost_run")
        return False

    async def _run_attempt(self) -> WorkerTurnOut | None:
        """One attempt. None ⇒ nothing ran (budget, hook, stop while waiting for a slot)."""
        if not await self._before_attempt():
            self._blocked_at = self._wakes  # wait for the next input or action, as documented (no busy loop)
            return None
        if not await _act("check_budget", self._ws, self._task_id, result_type=bool):
            self._status = "waiting"  # check_budget wrote waiting/budget
            self._blocked_at = self._wakes
            return None
        while True:
            self._slot_free = False  # cleared before asking, so a release during the ask is not lost
            if await _act("acquire_slot", self._ws, self._task_id, result_type=bool):
                break
            await workflow.wait_condition(
                lambda: self._slot_free or self._stop_by is not None or self._action is not None
            )
            if self._stop_by is not None or self._action is not None:
                await _act("release_slot", self._ws, self._task_id)  # drops this task from the waiters
                return None
        inputs, self._pending = self._pending, []
        try:
            ref = await _act(
                "begin_attempt",
                self._ws,
                self._task_id,
                [i.trigger_id for i in inputs],
                self._started,
                result_type=AttemptRef,
            )
        except ActivityError:
            await _act("release_slot", self._ws, self._task_id)
            self._pending = inputs + self._pending
            raise
        if ref.n == 0:  # still proposed: a thread reply alone never starts work (consent is Start)
            await _act("release_slot", self._ws, self._task_id)
            self._pending = inputs + self._pending
            self._awaiting_start = True
            return None
        self._attempt = ref
        self._status = "working"
        out = WorkerTurnOut("cancelled") if self._stop_by is not None else await self._worker_turn(ref)
        await _act("end_attempt", self._ws, self._task_id, ref.n, out.outcome, out.error)  # releases the slot
        self._status = _STATUS_AFTER.get(out.outcome, "failed")
        self._attempt, self._turn = None, None
        await self._after_attempt(out.outcome)
        return out

    async def _worker_turn(self, ref: AttemptRef) -> WorkerTurnOut:
        reconciles = 0
        while True:
            self._turn = workflow.start_activity(
                "worker_turn",
                WorkerTurnIn(self._ws, self._task_id, ref.n, ref.run_id),
                result_type=WorkerTurnOut,
                **_WORKER_TURN,
            )
            try:
                return await self._turn
            except ActivityError as err:
                if self._stop_by is not None or isinstance(err.cause, CancelledError):
                    return WorkerTurnOut("cancelled")
                timed_out = isinstance(err.cause, ActivityTimeout) and err.cause.type in (
                    TimeoutType.HEARTBEAT,
                    TimeoutType.START_TO_CLOSE,
                )
                if not timed_out:
                    return WorkerTurnOut("failed", _error_name(err))
            # The temporal-worker lost the turn; the bridge run may still be going (review focus 3).
            state = await _act("reconcile_attempt", self._ws, self._task_id, ref.n, result_type=str)
            if state.startswith("terminal:"):
                return WorkerTurnOut(state.removeprefix("terminal:"))
            if state == "running" and reconciles < MAX_SAME_RUN_RECONCILES:
                reconciles += 1
                continue  # same run_id: the bridge replays the run it already has
            return WorkerTurnOut("runtime_lost", "HeartbeatTimeout")


WORKFLOWS = [RouterWorkflow, TaskWorkflow]
