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

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from core.temporal.types import TaskInputRef

REMIND_AFTER = timedelta(hours=24)
_NO_ANSWERER_POST_S = 10  # each no-answerer post, well inside the 30 s Activity timeout
EXECUTOR_KINDS = frozenset({"mail.send", "pr.merge"})  # effect_executors.EXECUTORS (test pins it)
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
    blocking: bool = True  # False: the Worker goes on; the answer arrives as input (ask_person without `blocked`)


@dataclass(frozen=True)
class AskRef:
    """A Worker's ask_person / request_approval (ids only; the text is on `TRIG#ask:{qid}`)."""

    trigger_id: str
    kind: str  # question | approval
    subkind: str
    blocking: bool


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
    _action: tuple[str, str] | None  # a queued resolve/dismiss
    _transition: Any  # TaskWorkflow's status writer
    _woken: Any  # TaskWorkflow's "something to do" predicate

    def _q_init(self) -> None:
        self._open_qs: dict[str, list[str]] = {}  # qid → answerers (the validator's no-I/O check)
        self._nonblocking: set[str] = set()  # open questions the Worker need not wait for
        self._asked_questions: set[str] = set()  # `question`-kind rows the Worker asked (one open at a time)
        self._q_remind_at: dict[str, datetime] = {}  # unreminded open questions
        self._landings: list[tuple[str, str]] = []  # (effect idem key, answered by) waiting to land

    # --- hooks -----------------------------------------------------------------------------------------

    async def _q_before_attempt(self) -> bool:
        """An open blocking Question holds the next attempt: inputs wait behind it (spec 05 §3.5 layer 2)."""
        return not (set(self._open_qs) - self._nonblocking)

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
        for ref in refs:
            await self._q_track(ref)

    async def _q_track(self, ref: QuestionRef) -> None:
        if ref.qid in self._open_qs:
            return
        self._open_qs[ref.qid] = list(ref.answerers)
        if not ref.blocking:
            self._nonblocking.add(ref.qid)
        self._q_remind_at[ref.qid] = workflow.now() + REMIND_AFTER
        await _act("relay_question", self._ws, self._task_id, ref.qid)

    def _q_forget(self, qid: str) -> None:
        self._open_qs.pop(qid, None)
        self._nonblocking.discard(qid)
        self._q_remind_at.pop(qid, None)

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
            await self._q_remind_due()

    def _q_next_remind(self) -> datetime | None:
        """When the earliest open Question is due its +24 h reminder (None: none is)."""
        return min((t for q, t in self._q_remind_at.items() if q in self._open_qs), default=None)

    async def _q_remind_due(self) -> None:
        now = workflow.now()
        for qid, at in list(self._q_remind_at.items()):
            if at <= now:
                del self._q_remind_at[qid]
                if qid in self._open_qs:
                    await _act("remind_question", self._ws, self._task_id, qid)

    async def _q_serve(self) -> None:
        """Q's work without Q's wait (a watch is waiting instead): land approved drafts, send due reminders."""
        await self._q_land()
        await self._q_remind_due()

    async def _q_land(self) -> None:
        """Landing (contracts-v2 §4, §8): run each approved draft through its backend executor, then Idle. A refusal
        (definitely not done) goes back to the Worker as input; an unknown outcome is shown and never retried."""
        # Follow-up (review #4878): an answer accepted just before a Close can still land here; guarding on `_action`
        # changes commands, so it needs `workflow.patched`. Closing retires unused approvals (effects_repo).
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
        await self._q_cancel_open(by_user_id, "stopped")

    async def _q_cancel_open(self, by: str, reason: str) -> None:
        """The task is ending: its open Questions are cancelled (an answer after this is refused, not lost)."""
        if self._open_qs:
            await _act("cancel_open_questions", self._ws, self._task_id, by, reason)
            self._open_qs.clear()
            self._nonblocking.clear()
            self._q_remind_at.clear()

    # --- the one answer path ---------------------------------------------------------------------------

    @workflow.update
    async def answer(self, ref: AnswerRef) -> QuestionResult:
        out = await _act("record_answer", self._ws, self._task_id, ref, result_type=AnswerOut)
        self._q_forget(ref.qid)
        if out.land_idem_key:
            self._landings.append((out.land_idem_key, ref.by_user_id))
            return QuestionResult(ref.qid, "answered", landing=True)
        if out.trigger_id not in self._seen_inputs:
            self._seen_inputs.add(out.trigger_id)
            self._pending.append(TaskInputRef(out.trigger_id, "answer"))
            self._wakes += 1
        return QuestionResult(ref.qid, "answered")

    @workflow.update
    async def ask(self, ref: AskRef) -> str:
        """A Worker's question or approval on this task (runtime_internal's ask route). One open `question` per
        task: a new one supersedes the earlier unanswered one. Returns the qid."""
        opened = await _act("open_asked_question", self._ws, self._task_id, ref, result_type=QuestionRef)
        if ref.kind == "question":
            for qid in [q for q in self._asked_questions if q != opened.qid]:
                self._q_forget(qid)
            self._asked_questions = {opened.qid}
        await self._q_track(opened)
        return opened.qid

    @ask.validator
    def _validate_ask(self, ref: AskRef) -> None:
        if self._stop_by is not None:
            raise ValueError("task_closed")
        if ref.kind not in ("question", "approval"):
            raise ValueError("bad_kind")

    @answer.validator
    def _validate_answer(self, ref: AnswerRef) -> None:
        # `_action`: a Close (resolve) is queued; the loop will cancel this Question, so an answer now is refused,
        # never recorded on a task that is ending. Validators do not run on replay: no patch needed.
        if self._stop_by is not None or self._action is not None:
            raise ValueError("task_closed")
        if ref.qid not in self._open_qs:
            raise ValueError("not_open")
        # Only the first clause of `questions.can_answer_now` (in the stored list, which is what this workflow
        # holds); the rest of the rule is re-checked at answer time in `record_answer`, the same check the route ran.
        if ref.by_user_id not in self._open_qs[ref.qid]:
            raise ValueError("not_answerer")


# --- Activities ------------------------------------------------------------------------------------------


def _fail(err: Exception) -> ApplicationError:
    return ApplicationError(str(err), type=type(err).__name__, non_retryable=True)


@activity.defn(name="open_questions")
async def open_questions(ws: str, task_id: str) -> list[QuestionRef]:
    """One Question per proposed Effect of the task that has none yet (idempotent on the qid). Opening adds the
    answerers to the task's audience (questions_repo). Raises `NoAnswerer` (non-retryable) when nobody can answer."""
    from core.repositories import effects_repo, questions_repo, tasks_repo
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
        kind, options = questions.kind_and_options(effect["kind"], effect.get("operands"))
        subkind = questions.subkind_for(effect["kind"])
        try:
            ids, rule, base = await questions.answerers(ws, task, kind, subkind)
        except questions.NoAnswerer as err:
            await _say_no_answerer(ws, task, questions.title_for(effect))
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
                "preview": {
                    "kind": effect["kind"],
                    **(effect.get("operands") or {}),
                    # request_purchase's amount is US dollars (its schema); the card says so (dev walk 7).
                    **({"currency": "USD"} if effect["kind"] == "purchase" else {}),
                },
            }
        )
    # Every answerer list is known; only now write (each open is idempotent, so a retry completes the set).
    for q in new:
        row, _ = await questions_repo.open_question(ws, q)
        refs[q["qid"]] = QuestionRef(q["qid"], list(row["answerers"]))
    return list(refs.values())


@activity.defn(name="open_asked_question")
async def open_asked_question(ws: str, task_id: str, ref: AskRef) -> QuestionRef:
    """The Question row for a Worker's ask (text from `TRIG#ask:{qid}`), answerers per spec 05 §3.3; a new
    `question` supersedes the task's earlier open one (`cancelled`, reason `superseded`). Idempotent on the qid."""
    from core.repositories import questions_repo, tasks_repo
    from core.services import questions

    trigger = await tasks_repo.get_trigger(ws, ref.trigger_id)
    task = await tasks_repo.get_task(ws, task_id)
    if trigger is None or task is None:
        raise _fail(LookupError(f"ask {ref.trigger_id} on {task_id} not found"))
    ask = trigger["ref"]
    qid = ask["qid"]
    existing = await questions_repo.get_question(ws, task_id, qid)
    if existing is None:
        try:
            ids, rule, base = await questions.answerers(ws, task, ref.kind, ref.subkind)
        except questions.NoAnswerer as err:
            raise _fail(err) from err
        existing, _ = await questions_repo.open_question(
            ws,
            {
                "taskId": task_id,
                "qid": qid,
                "kind": ref.kind,
                "subkind": ref.subkind,
                "title": ask["title"],
                "text": ask.get("text") or "",
                "options": ask.get("options") or [],
                "askedBy": {"kind": "worker", "specialistId": task.get("specialist"), "attempt": ask.get("attempt")},
                "answerers": ids,
                "answererRule": rule,
                "baseAnswerers": base,
                **({"preview": ask["preview"]} if ask.get("preview") else {}),
            },
        )
    if ref.kind == "question":
        await questions_repo.cancel_open(
            ws, task_id, by="worker", reason="superseded", kinds=("question",), except_qid=qid
        )
    return QuestionRef(qid, list(existing["answerers"]), ref.blocking)


async def _say_no_answerer(ws: str, task: dict, what: str) -> None:
    """The task is about to end `no_answerer` (an effect nobody can approve): say so where it was asked and in its
    room, never a silent stop. Keyed on the task and attempt, so a retried Activity posts once. Each post is capped
    at 10 s and a failure is logged, never raised: the caller's NoAnswerer must still reach the workflow inside the
    Activity's timeout. (A Worker's own ask with no answerer does not end the task, so it posts nothing.)"""
    from core.repositories import tasks_repo
    from core.services import task_conversation

    text = f"Nobody in this workspace can approve this ({what}), so I've stopped here. An org admin can ask me again."
    key = f"q-no-answerer:{task['taskId']}:{task.get('attemptCount', 0)}"
    refs = [{"taskId": task["taskId"], "role": "started"}]

    async def post(room: str, event_id: str, thread_root: str | None = None) -> None:
        await asyncio.wait_for(
            task_conversation.post_as_lightbulb(
                ws,
                room,
                text=text,
                event_id=event_id,
                specialist=task.get("specialist"),
                thread_root=thread_root,
                task_refs=refs,
            ),
            _NO_ANSWERER_POST_S,
        )

    try:
        room = await asyncio.wait_for(task_conversation.ensure_task_room(ws, task), _NO_ANSWERER_POST_S)
        await post(room, key)
        origin = _origin_thread((await tasks_repo.get_task_tree(ws, task["taskId"])).get("subscriptions") or [])
        if origin and origin[0] != room:
            await post(origin[0], f"{key}:origin", origin[1])
    except Exception:  # noqa: BLE001 -- includes TimeoutError
        activity.logger.warning("no-answerer line not posted for task %s", task["taskId"], exc_info=True)


def _origin_thread(subscriptions: list[dict]) -> tuple[str, str | None] | None:
    for sub in subscriptions:
        parts = (sub.get("threadKey") or "").split(":")
        if sub.get("createdBy") == "router" and parts[0] == "room" and len(parts) >= 2:
            return parts[1], (parts[2] if len(parts) > 2 else None)
    return None


async def _specialist_label(ws: str, specialist: str | None) -> str:
    """The name a card says ("Engineering"), never the specialist's id (dev walk 7); Lightbulb when none or unknown."""
    from core.services import messaging

    if not specialist:
        return "Lightbulb"
    try:
        roster = await messaging.load_agent_roster(ws)
    except Exception:  # noqa: BLE001 -- a roster blip never holds a card back
        return "Lightbulb"
    return next((a["name"] for a in roster if a["id"] == specialist), "Lightbulb")


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
    from core.services import messaging, needs_you_email, questions, task_conversation

    q = await questions_repo.get_question(ws, task_id, qid)
    tree = await tasks_repo.get_task_tree(ws, task_id)
    task = tree["task"]
    if q is None or task is None or q["state"] != "open":
        return
    label = await _specialist_label(ws, task.get("specialist"))
    title, body = questions.card_copy(q, label)
    refs = [{"taskId": task_id, "role": "started", "qid": qid}]  # the card's buttons bind to {taskId, qid}
    room = await task_conversation.ensure_task_room(ws, task)
    for uid in q["answerers"]:
        await task_conversation.ensure_task_room(ws, task, seat_viewer=uid)
    await task_conversation.post_as_lightbulb(
        ws, room, text=title, event_id=f"q:{qid}", specialist=task.get("specialist"), task_refs=refs, kind="question"
    )
    origin = _origin_thread(tree.get("subscriptions") or [])
    if origin:
        card = dict(text=title, event_id=f"q:{qid}:origin", specialist=task.get("specialist"), task_refs=refs,
                    kind="question")  # fmt: skip
        try:
            await task_conversation.post_as_lightbulb(ws, origin[0], thread_root=origin[1], **card)
        except messaging.ApiProblem as exc:
            # 8 Nov prod (a Meet task): the Router's ack was moved to the meeting's output room, so the thread its
            # subscription names is not in the origin room, and 5 of 5 retries failed before any push or email.
            # The card goes to the origin room itself (which sends a Meet room's post to the output room again).
            if exc.error.code != "MESSAGE_NOT_FOUND" or not origin[1]:
                raise
            activity.logger.warning("origin thread %s gone for task %s; card posted unthreaded", origin[1], task_id)
            await task_conversation.post_as_lightbulb(ws, origin[0], thread_root=None, **card)
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
    label = await _specialist_label(ws, task.get("specialist"))
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

    # Re-check authority at answer time (spec 05 §3.3), the same rule the route used: a demotion or a tightened
    # policy between the press and this Activity is refused. An answered row skips it (a retry is idempotent below).
    now = await questions_repo.get_question(ws, task_id, ref.qid)
    if now is not None and now["state"] == "open" and not await questions.can_answer_now(ws, now, ref.by_user_id):
        raise ApplicationError("not_answerer", type="NotAnswerer", non_retryable=True)
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
        if option == "allow_always" and effect:
            # Allow this one and make it a standing rule (idempotent: a retried Activity writes the same rule).
            await questions.add_always_allow(ws, effect["kind"], effect.get("operands") or {}, by=ref.by_user_id)
        # A stale card: the same action already went ahead without it (the mode changed or a rule matched after
        # the card opened). Its approval is used up at once, so it can never let a second identical send through.
        stale = to == "approved" and any(
            p["idemKey"] != effect.get("idemKey")
            and p.get("state") in ("intent", "done", "unknown")
            and p.get("createdAt", "") > effect.get("createdAt", "")
            for p in await effects_repo.effects_for(ws, task_id, effect.get("operandsHash") or "")
            if p.get("type") == "effect"
        )
        if stale:
            await effects_repo.retire_approvals(ws, task_id, only=q["effectIdemKey"], used_by="superseded")
        # A reviewed draft the backend can land itself; everything else resumes the Worker (contracts-v2 §8).
        if (
            not stale
            and to == "approved"
            and effect.get("executorOk")
            and option in (questions.REVIEW_KINDS.get(effect.get("kind")), "allow_always")
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
        row = await effects_repo.mark_effect_done(ws, idem_key, disposition="done", result_ref=ref)
        from core.services import tasks_access

        await tasks_access.record_effect_output(ws, row)  # what it made stays on the task (UX slice 5)
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


@activity.defn(name="report_a2a")
async def report_a2a(ws: str, task_id: str, outcome: str) -> None:
    """How an A2A-started task ended, for governance (wave2-Q Task 10). No call site yet: the governance
    completion functions are the A2A session's (spec 05 T9); until then this records `a2a.report_pending`."""
    from core.services import a2a_admission

    await a2a_admission.report_a2a(ws, task_id, outcome)


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
    report_a2a,
    apply_effect,
    open_asked_question,
]
