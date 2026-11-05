"""Questions on `TaskWorkflow` (wave2-Q Task 3; contracts-v2 §3, §5, §7, §8; spec 05 §3.2–§3.4).

`TaskQuestionsMixin` is mixed into `TaskWorkflow` through its four hooks:
- an attempt that ends `waiting_approval` opens one Question per proposed Effect and relays it (card, push to the
  answerers only, away email);
- while a Question is open no new attempt starts (a person's typed "go ahead" waits as input, it never answers);
- `answer` is the only way an answer reaches the task: the update records it (Effect `proposed → approved|denied`)
  and hands the answer to the next attempt as input;
- Stop cancels open Questions; one reminder push per Question after 24 h.

Workflow side: stdlib + `temporalio` only (sandbox). The Activities below import their services lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from core.temporal.types import TaskInputRef

REMIND_AFTER = timedelta(hours=24)
EXECUTOR_KINDS = frozenset({"mail.send", "app.publish", "pr.merge"})  # effect_executors.EXECUTORS (test pins it)
_LANDING = {
    "start_to_close_timeout": timedelta(minutes=15),  # an app publish builds and promotes
    "retry_policy": RetryPolicy(maximum_attempts=3),  # a retry never re-sends: apply_effect reads the Effect first
}
_SHORT = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=5)}
_DENYING = ("deny", "request_changes")


@dataclass(frozen=True)
class AnswerRef:
    """A person's press (ids only; a free-text answer is already on `TRIG#answer:…`)."""

    qid: str
    option_id: str
    trigger_id: str | None
    by_user_id: str
    client_answer_id: str
    expected_version: int | None = None


@dataclass(frozen=True)
class QuestionRef:
    qid: str
    answerers: list[str]


@dataclass(frozen=True)
class AnswerOut:
    trigger_id: str
    option_id: str
    land_idem_key: str | None = None  # an approved draft a backend executor lands (Landing), instead of input


@dataclass(frozen=True)
class LandResult:
    outcome: str  # done | unknown | refused | used
    trigger_id: str | None = None  # refused: the reason, as input to the next attempt


@dataclass(frozen=True)
class QuestionResult:
    qid: str
    state: str
    landing: bool = False


async def _act(name: str, *args: Any, result_type: type | None = None) -> Any:
    return await workflow.execute_activity(name, args=list(args), result_type=result_type, **_SHORT)


def _error_type(err: ActivityError) -> str | None:
    cause = err.cause
    return cause.type if isinstance(cause, ApplicationError) else None


class TaskQuestionsMixin:
    """State and hook bodies; `TaskWorkflow` owns `_ws`, `_task_id`, `_pending`, `_seen_inputs`, `_wakes`,
    `_stop_by` and calls `_q_init` from its `__init__`."""

    _ws: str
    _task_id: str
    _pending: list[TaskInputRef]
    _seen_inputs: set[str]
    _wakes: int
    _stop_by: str | None
    _transition: Any  # TaskWorkflow's status writer
    _woken: Any  # TaskWorkflow's "something to do" predicate

    def _q_init(self) -> None:
        self._open_qs: dict[str, list[str]] = {}  # qid → answerers (the validator's no-I/O check)
        self._q_remind_at: dict[str, datetime] = {}  # unreminded open questions
        self._landings: list[tuple[str, str]] = []  # (effect idem key, answered by) waiting to land

    # --- hooks -----------------------------------------------------------------------------------------

    async def _q_before_attempt(self) -> bool:
        """An open Question blocks the next attempt: inputs wait behind it (spec 05 §3.5 layer 2)."""
        return not self._open_qs

    async def _q_after_attempt(self, outcome: str) -> None:
        if outcome != "waiting_approval":
            return
        try:
            refs = await _act("open_questions", self._ws, self._task_id, result_type=list[QuestionRef])
        except ActivityError as err:
            if _error_type(err) == "NoAnswerer":
                await self._transition("resolved", "no_answerer")  # nobody can answer: never a silent wait
            else:
                # Clerk/policy unreachable after retries (spec 05 R5): stay waiting, say so; a reply re-runs the
                # Worker, whose repeated call asks again.
                await _act("note_answerers_unavailable", self._ws, self._task_id)
            return
        now = workflow.now()
        for ref in refs:
            if ref.qid in self._open_qs:
                continue
            self._open_qs[ref.qid] = list(ref.answerers)
            self._q_remind_at[ref.qid] = now + REMIND_AFTER
            await _act("relay_question", self._ws, self._task_id, ref.qid)

    async def _q_on_waiting(self) -> None:
        """While a Question is open the task waits here, with no deadline: the 7-day quiet resolve never ends a task
        that is waiting on a person. Reminds each Question once at +24 h. Returns when something wakes the task or
        no Question is open any more."""
        while True:
            await self._q_land()
            if not self._open_qs:
                return
            due = [t for q, t in self._q_remind_at.items() if q in self._open_qs]
            wait = max(min(due) - workflow.now(), timedelta(0)) if due else None
            try:
                await workflow.wait_condition(
                    lambda: self._woken() or not self._open_qs or bool(self._landings), timeout=wait
                )
                if self._landings:
                    continue
                return
            except TimeoutError:
                pass
            now = workflow.now()
            for qid, at in list(self._q_remind_at.items()):
                if at <= now:
                    del self._q_remind_at[qid]
                    if qid in self._open_qs:
                        await _act("remind_question", self._ws, self._task_id, qid)

    async def _q_land(self) -> None:
        """Landing (contracts-v2 §4, §8): run each approved draft through its backend executor, then Idle. A refusal
        (definitely not done) goes back to the Worker as input; an unknown outcome is shown and never retried."""
        while self._landings and self._stop_by is None:
            idem, by = self._landings.pop(0)
            try:
                res = await workflow.execute_activity(
                    "apply_effect", args=[self._ws, self._task_id, idem, by], result_type=LandResult, **_LANDING
                )
            except ActivityError:
                # Landing failed after its retries: unreconcilable, shown plainly, never retried (a row left in
                # `intent` keeps the gate refusing a repeat until a person looks).
                workflow.logger.error("apply_effect failed for %s", idem)
                res = LandResult("unknown")
            if res.trigger_id and res.trigger_id not in self._seen_inputs:
                self._seen_inputs.add(res.trigger_id)
                self._pending.append(TaskInputRef(res.trigger_id, "answer"))
                self._wakes += 1
            elif res.outcome in ("done", "unknown") and not self._open_qs and not self._landings:
                await self._transition("idle", "landed" if res.outcome == "done" else "effect_unknown")

    async def _q_on_stop(self, by_user_id: str) -> None:
        if self._open_qs:
            await _act("cancel_open_questions", self._ws, self._task_id, by_user_id, "stopped")
            self._open_qs.clear()
            self._q_remind_at.clear()

    # --- the one answer path ---------------------------------------------------------------------------

    @workflow.update
    async def answer(self, ref: AnswerRef) -> QuestionResult:
        out = await _act("record_answer", self._ws, self._task_id, ref, result_type=AnswerOut)
        self._open_qs.pop(ref.qid, None)
        self._q_remind_at.pop(ref.qid, None)
        if out.land_idem_key:
            self._landings.append((out.land_idem_key, ref.by_user_id))
            return QuestionResult(ref.qid, "answered", landing=True)
        if out.trigger_id not in self._seen_inputs:
            self._seen_inputs.add(out.trigger_id)
            self._pending.append(TaskInputRef(out.trigger_id, "answer"))
            self._wakes += 1
        return QuestionResult(ref.qid, "answered")

    @answer.validator
    def _validate_answer(self, ref: AnswerRef) -> None:
        if self._stop_by is not None:
            raise ValueError("task_closed")
        if ref.qid not in self._open_qs:
            raise ValueError("not_open")
        if ref.by_user_id not in self._open_qs[ref.qid]:
            raise ValueError("not_answerer")


# --- Activities ------------------------------------------------------------------------------------------


def _fail(err: Exception) -> ApplicationError:
    return ApplicationError(str(err), type=type(err).__name__, non_retryable=True)


@activity.defn(name="open_questions")
async def open_questions(ws: str, task_id: str) -> list[QuestionRef]:
    """One Question per proposed Effect of the task that has none yet (idempotent on the qid). Opening adds the
    answerers to the task's audience (questions_repo). Raises `NoAnswerer` (non-retryable) when nobody can answer."""
    from core.repositories import apps_repo, effects_repo, questions_repo, tasks_repo
    from core.services import questions

    task = await tasks_repo.get_task(ws, task_id)
    if task is None:
        raise _fail(LookupError(f"task {task_id} not found"))
    refs: dict[str, QuestionRef] = {}
    new: list[dict] = []
    for effect in await effects_repo.effects_of_task(ws, task_id):
        if effect["state"] != "proposed":
            continue
        qid = effect.get("questionId") or questions.qid_for(effect["idemKey"])
        if qid in refs or any(q["qid"] == qid for q in new):
            continue
        existing = await questions_repo.get_question(ws, task_id, qid)
        if existing is not None:
            if existing["state"] == "open":
                refs[qid] = QuestionRef(qid, list(existing["answerers"]))
            continue  # answered or cancelled: never reopened
        kind, options = questions.kind_and_options(effect["kind"])
        subkind = questions.subkind_for(effect["kind"])
        app = None
        if subkind == "publish" and (effect.get("operands") or {}).get("appId"):
            app = await apps_repo.get(effect["operands"]["appId"], consistent_read=True)
        try:
            ids, rule, base = await questions.answerers(ws, task, kind, subkind, app=app)
        except questions.NoAnswerer as err:
            raise _fail(err) from err  # before any row is written: no half-opened set
        new.append(
            {
                "taskId": task_id,
                "qid": qid,
                "kind": kind,
                "subkind": subkind,
                "title": questions.title_for(effect),
                "options": [{"id": o, "label": questions.OPTION_LABELS[o]} for o in options],
                "askedBy": {"kind": "worker", "specialistId": task.get("specialist"), "attempt": effect["attempt"]},
                "answerers": ids,
                "answererRule": rule,
                "baseAnswerers": base,
                "operandsHash": effect["operandsHash"],
                "effectIdemKey": effect["idemKey"],
                "preview": {"kind": effect["kind"], **(effect.get("operands") or {})},
            }
        )
    # Every answerer list is known; only now write (each open is idempotent, so a retry completes the set).
    for q in new:
        row, _ = await questions_repo.open_question(ws, q)
        refs[q["qid"]] = QuestionRef(q["qid"], list(row["answerers"]))
    return list(refs.values())


def _origin_thread(subscriptions: list[dict]) -> tuple[str, str | None] | None:
    for sub in subscriptions:
        parts = (sub.get("threadKey") or "").split(":")
        if sub.get("createdBy") == "router" and parts[0] == "room" and len(parts) >= 2:
            return parts[1], (parts[2] if len(parts) > 2 else None)
    return None


async def _push_once(key: str, user_id: str, title: str, body: str, data: dict) -> None:
    from core.services import push_service, webhook_dedup

    if (
        await webhook_dedup.record_event_or_skip(key, source="task_question")
        is webhook_dedup.WebhookDedupResult.RECORDED
    ):
        await push_service.send_push_to_user(user_id, title, body, data)


@activity.defn(name="relay_question")
async def relay_question(ws: str, task_id: str, qid: str) -> None:
    """The card in the task room and the origin thread, a push to each answerer (never anyone else), the away
    email. Event ids and push keys come from the qid, so a retry posts and pushes once."""
    from core.repositories import questions_repo, tasks_repo
    from core.services import needs_you_email, questions, task_conversation

    q = await questions_repo.get_question(ws, task_id, qid)
    tree = await tasks_repo.get_task_tree(ws, task_id)
    task = tree["task"]
    if q is None or task is None or q["state"] != "open":
        return
    label = task.get("specialist") or "Lightbulb"
    title, body = questions.card_copy(q, label)
    refs = [{"taskId": task_id, "role": "started"}]
    room = await task_conversation.ensure_task_room(ws, task)
    for uid in q["answerers"]:
        await task_conversation.ensure_task_room(ws, task, seat_viewer=uid)
    await task_conversation.post_as_lightbulb(
        ws, room, text=title, event_id=f"q:{qid}", specialist=task.get("specialist"), task_refs=refs, kind="question"
    )
    origin = _origin_thread(tree.get("subscriptions") or [])
    if origin:
        await task_conversation.post_as_lightbulb(
            ws,
            origin[0],
            text=title,
            event_id=f"q:{qid}:origin",
            specialist=task.get("specialist"),
            thread_root=origin[1],
            task_refs=refs,
            kind="question",
        )
    data = {"source": "task_question", "taskId": task_id, "qid": qid}
    push_title = f"{label} needs your answer" if q["kind"] == "question" else f"{label} needs your OK"
    for uid in q["answerers"]:
        await _push_once(f"qpush:{ws}:{qid}:{uid}", uid, push_title, body, data)
    await needs_you_email.queue_question(ws, task_id, qid, list(q["answerers"]), title.replace("**", ""), body)


@activity.defn(name="remind_question")
async def remind_question(ws: str, task_id: str, qid: str) -> None:
    from core.repositories import questions_repo, tasks_repo
    from core.services import questions

    q = await questions_repo.get_question(ws, task_id, qid)
    task = await tasks_repo.get_task(ws, task_id)
    if q is None or task is None or q["state"] != "open":
        return
    label = task.get("specialist") or "Lightbulb"
    _, body = questions.card_copy(q, label)
    data = {"source": "task_question", "taskId": task_id, "qid": qid}
    for uid in q["answerers"]:
        need = "needs your answer" if q["kind"] == "question" else "needs your OK"
        await _push_once(f"qremind:{ws}:{qid}:{uid}", uid, f"Still waiting: {label} {need}", body, data)


@activity.defn(name="record_answer")
async def record_answer(ws: str, task_id: str, ref: AnswerRef) -> AnswerOut:
    """`open → answered`, its Effect `proposed → approved|denied`, and the answer as a Trigger for the next attempt.
    Refusals are non-retryable errors typed by the repo class (`NotOpen`, `NotAnswerer`, `InvalidOption`, …)."""
    from core import tasks_schema as ts
    from core.dynamodb import utc_now_iso
    from core.repositories import effects_repo, questions_repo, tasks_repo
    from core.services import questions

    try:
        q = await questions_repo.record_answer(
            ws,
            task_id,
            ref.qid,
            option_id=ref.option_id,
            trigger_id=ref.trigger_id,
            by=ref.by_user_id,
            client_answer_id=ref.client_answer_id,
            expected_version=ref.expected_version,
        )
    except questions_repo.NotOpen as err:
        raise ApplicationError(str(err), err.state, err.by, err.at, type="NotOpen", non_retryable=True) from err
    except tasks_repo.TasksRepoError as err:
        raise _fail(err) from err
    option = q["answer"]["optionId"]
    land = None
    if q.get("effectIdemKey"):
        to = "denied" if option in _DENYING else "approved"
        try:
            await effects_repo.set_effect_state(ws, q["effectIdemKey"], to=to, fields={"questionId": ref.qid})
        except tasks_repo.IllegalTransition:
            pass  # a retry after the first run moved it
        effect = await effects_repo.get_effect(ws, q["effectIdemKey"]) or {}
        # A reviewed draft the backend can land itself; everything else resumes the Worker (contracts-v2 §8).
        if (
            to == "approved"
            and effect.get("executorOk")
            and questions.REVIEW_KINDS.get(effect.get("kind")) == option
            and effect.get("kind") in EXECUTOR_KINDS
        ):
            land = q["effectIdemKey"]
    trigger_id = ref.trigger_id or ts.trigger_id("answer", ref.qid, ref.client_answer_id)
    await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": "answer",
            "ref": {"taskId": task_id, "qid": ref.qid, "optionId": option},
            "admission": "respond",
            "lane": "shared",
            "actorUserId": ref.by_user_id,
            "receivedAt": q["answeredAt"] or utc_now_iso(),
        },
    )
    return AnswerOut(trigger_id, option, land)


async def _set_landing(ws: str, task_id: str, value: str | None) -> None:
    """Task `landingEffect` (Lane O's `summarize` shows the Landing group while it is set)."""
    from core.repositories import tasks_repo

    for _ in range(5):
        task = await tasks_repo.get_task(ws, task_id)
        if task is None:
            raise LookupError(f"task {task_id} not found")
        if task.get("landingEffect") == value:
            return
        try:
            await tasks_repo.update_task_fields(ws, task_id, {"landingEffect": value}, expected_version=task["version"])
            return
        except tasks_repo.VersionConflict:
            continue
    raise tasks_repo.VersionConflict(f"task {task_id}: landingEffect kept racing")


async def _answer_trigger_id(ws: str, task_id: str, effect: dict) -> str:
    """The `TRIG#answer:…` record_answer wrote for this effect's question (what the Worker resumes with)."""
    from core import tasks_schema as ts
    from core.repositories import questions_repo

    q = await questions_repo.get_question(ws, task_id, effect.get("questionId") or "")
    if q is None or not q.get("clientAnswerId"):
        raise LookupError(f"effect {effect['idemKey']} has no answered question")
    return ts.trigger_id("answer", q["qid"], q["clientAnswerId"])


async def _refused_trigger(
    ws: str, task_id: str, effect: dict, idem_key: str, code: str, next_step: str | None, answered_by: str
) -> str:
    """The refusal as the next attempt's input (idempotent put: a retry re-puts the same trigger)."""
    from core import tasks_schema as ts
    from core.dynamodb import utc_now_iso
    from core.repositories import tasks_repo

    trigger_id = ts.trigger_id("answer", effect.get("questionId") or idem_key, "landing")
    await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": "answer",
            "ref": {"taskId": task_id, "refused": code, "next": next_step, "effect": idem_key},
            "admission": "respond",
            "lane": "shared",
            "actorUserId": answered_by,
            "receivedAt": utc_now_iso(),
        },
    )
    return trigger_id


@activity.defn(name="apply_effect")
async def apply_effect(ws: str, task_id: str, idem_key: str, answered_by: str) -> LandResult:
    """Land one approved draft: `approved → intent` (one transaction with the single-use mark) BEFORE the executor,
    then `done` / `unknown`. A retry that finds `intent` it did not write cannot know whether the send happened:
    `unknown`, never a second send (a PR merge re-checks GitHub, so it may run again). A refusal is `done` with
    `resultRef "refused:<code>"` (shown as "Didn't happen"), and its trigger is re-put on a retry."""
    from core.repositories import effects_repo
    from core.services import effect_executors as ex

    effect = await effects_repo.get_effect(ws, idem_key)
    if effect is None:
        raise LookupError(f"effect {idem_key} not found")
    if effect["kind"] not in ex.EXECUTORS:
        # No backend executor: the Worker runs it, with the answer as its input; the approval is not consumed here.
        return LandResult("refused", await _answer_trigger_id(ws, task_id, effect))
    await _set_landing(ws, task_id, idem_key)
    try:
        fresh = False
        if effect["state"] == "approved":
            fresh = await effects_repo.use_approval(ws, idem_key, effect["operandsHash"])
            effect = await effects_repo.get_effect(ws, idem_key)
        if effect["state"] == "approved":
            return LandResult("used")  # the Worker's own retry used this approval first
        if effect["state"] == "unknown":
            return LandResult("unknown")
        if effect["state"] == "done":
            if str(effect.get("resultRef") or "").startswith("refused:"):
                # A retry after the refusal was recorded but before its trigger was: finish that, never "landed".
                code = effect["resultRef"].removeprefix("refused:")
                return LandResult(
                    "refused", await _refused_trigger(ws, task_id, effect, idem_key, code, None, answered_by)
                )
            return LandResult("done")
        if effect["state"] != "intent":
            raise LookupError(f"effect {idem_key} in state {effect['state']} cannot land")
        if not fresh and effect["kind"] != "pr.merge":
            await effects_repo.mark_effect_done(ws, idem_key, disposition="unknown", result_ref=None)
            return LandResult("unknown")
        try:
            ref = await ex.EXECUTORS[effect["kind"]](ws, effect.get("operands") or {}, answered_by)
        except ex.ExecutorRefused as err:
            # Definitely not done: `done` with the refusal as its result, so the same draft can be asked again.
            await effects_repo.mark_effect_done(ws, idem_key, disposition="done", result_ref=f"refused:{err.code}")
            return LandResult(
                "refused", await _refused_trigger(ws, task_id, effect, idem_key, err.code, err.next_step, answered_by)
            )
        except Exception:  # ExecutorUnknown or anything after the request may have left: never retried
            await effects_repo.mark_effect_done(ws, idem_key, disposition="unknown", result_ref=None)
            return LandResult("unknown")
        await effects_repo.mark_effect_done(ws, idem_key, disposition="done", result_ref=ref)
        return LandResult("done")
    finally:
        await _set_landing(ws, task_id, None)


@activity.defn(name="note_answerers_unavailable")
async def note_answerers_unavailable(ws: str, task_id: str) -> None:
    """Spec 05 R5: the answerer lookup (Clerk, policy) failed after retries. One line in the task room; the task
    stays waiting with no open question, and a reply re-runs the Worker, whose repeated call asks again."""
    from core import tasks_schema as ts
    from core.repositories import tasks_repo
    from core.services import task_conversation

    task = await tasks_repo.get_task(ws, task_id)
    if task is None:
        raise LookupError(f"task {task_id} not found")  # a workflow without its row is a bug: fail loudly
    await task_conversation.ensure_task_room(ws, task)
    await task_conversation.post_as_lightbulb(
        ws,
        ts.task_room_id(task_id),
        text="Couldn't find who can approve this right now. Reply here to try again.",
        event_id=f"q-lookup-failed:{task_id}:{task.get('attemptCount', 0)}",
        specialist=task.get("specialist"),
        task_refs=[{"taskId": task_id, "role": "started"}],
    )


@activity.defn(name="cancel_open_questions")
async def cancel_open_questions(ws: str, task_id: str, by: str, reason: str) -> list[str]:
    from core.repositories import questions_repo

    return await questions_repo.cancel_open(ws, task_id, by=by, reason=reason)


ACTIVITIES = [
    open_questions,
    relay_question,
    remind_question,
    record_answer,
    note_answerers_unavailable,
    cancel_open_questions,
    apply_effect,
]
