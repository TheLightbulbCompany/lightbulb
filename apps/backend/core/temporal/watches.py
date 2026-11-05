"""Task watches: workflow timers instead of `WATCH#` rows (lane A, Task 12; contracts-v2 §3 cut, §4 "Watching").

A Worker asks its task to watch something that finishes later (a PR's checks, a URL that should answer) and
ends its turn. The task goes idle with `watchUntil` set; `WatchMixin` sleeps on the task's workflow until the
next check, runs `check_watch`, and when the watch is met writes a `TRIG#watch:…` row and wakes the task with
it as input. Past `giveUpAt` the task asks its people whether to keep watching (`ask`) or resolves
(`fail`: `failed` is only reachable from `working`, so the row says `resolved/watch_gave_up`).

Workflow code imports only `temporalio`, the stdlib and `core.temporal.*` dataclasses; Activities import their
I/O modules inside. Payloads carry ids and the watch's target (a repo and number, or a URL), never text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from core.temporal.task_questions import AskRef
from core.temporal.types import TaskInputRef

WatchKind = Literal["pr_checks", "url"]
_SHORT = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RetryPolicy(maximum_attempts=5)}
_CHECK = {"start_to_close_timeout": timedelta(seconds=60), "retry_policy": RetryPolicy(maximum_attempts=2)}
MIN_EVERY_MIN, MAX_EVERY_MIN = 5, 24 * 60
MAX_GIVE_UP_DAYS = 14  # a watch is for something that finishes soon; longer is an automation
GIVE_UP_OPTIONS = [{"id": "keep", "label": "Keep watching another day"}, {"id": "stop", "label": "Stop watching"}]


@dataclass(frozen=True)
class WatchSpec:
    watch_id: str
    kind: WatchKind
    target: dict[str, Any] = field(default_factory=dict)  # pr_checks: {repo, number}; url: {url}
    every_min: int = 30
    give_up_at: str = ""  # ISO 8601, UTC
    on_give_up: Literal["ask", "fail"] = "ask"


def _at(iso: str) -> datetime:
    """Aware, so it compares with `workflow.now()`; a naive time (the route refuses them) reads as UTC."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _act(name: str, *args: Any, result_type: type | None = None, **opts: Any) -> Any:
    return await workflow.execute_activity(name, args=list(args), result_type=result_type, **(opts or _SHORT))


class WatchMixin:
    """TaskWorkflow hooks (≤15 call-site lines in workflows.py): `_w_init`, `_w_on_waiting`, `_w_wake` in
    `_woken`, and the `add_watch` update, `watch_poke` and `unwatch` signals."""

    # TaskWorkflow's own state the hooks read (declared for the type checker, set by TaskWorkflow):
    _ws: str
    _task_id: str
    _pending: list[TaskInputRef]
    _seen_inputs: set[str]
    _wakes: int
    _woken: Any
    _transition: Any
    _status: str | None
    _landings: list[tuple[str, str]]  # Lane Q's approved drafts waiting to land
    _q_next_remind: Any  # Lane Q: when the earliest open Question's reminder is due
    ask: Any  # Lane Q's update: opens and tracks a question on this task

    def _w_init(self) -> None:
        self._watches: dict[str, WatchSpec] = {}
        self._w_due: dict[str, datetime] = {}
        self._w_checks: dict[str, int] = {}
        self._w_poked: set[str] = set()
        self._w_wake = False  # one-shot: a watch was added/poked/removed while the task waited
        self._w_until: str | None = None  # the `watchUntil` the row holds

    # --- handlers --------------------------------------------------------------------------------------

    async def _w_add(self, spec: WatchSpec) -> str:
        self._watches[spec.watch_id] = spec
        self._w_due[spec.watch_id] = workflow.now()  # the first check runs at once
        self._w_checks.setdefault(spec.watch_id, 0)
        self._w_wake = True
        return spec.watch_id

    def _w_poke(self, watch_id: str) -> None:
        if watch_id in self._watches:
            self._w_poked.add(watch_id)
            self._w_wake = True

    def _w_remove(self, watch_id: str) -> None:
        if self._w_drop(watch_id):
            self._w_wake = True

    def _w_drop(self, watch_id: str) -> bool:
        self._w_due.pop(watch_id, None)
        self._w_poked.discard(watch_id)
        return self._watches.pop(watch_id, None) is not None

    # --- the wait ---------------------------------------------------------------------------------------

    async def _w_on_waiting(self) -> None:
        """While watches exist the task waits here: checks run when due or poked, the row's `watchUntil`
        follows the latest give-up time. Returns when something else wakes the task or no watch is left."""
        self._w_wake = False
        while self._watches and not self._woken():
            await self._w_sync_until()
            # Q's work (an approved draft to land, a reminder due) never waits behind a watch: return to it
            remind = self._q_next_remind()
            if self._landings or (remind is not None and remind <= workflow.now()):
                break
            due = min(self._w_due.values())
            if due > workflow.now() and not self._w_poked:
                try:
                    await workflow.wait_condition(
                        lambda: self._woken() or bool(self._w_poked) or not self._watches or bool(self._landings),
                        timeout=min(due, remind or due) - workflow.now(),
                    )
                except asyncio.TimeoutError:
                    pass
                if self._woken() or not self._watches:
                    break
                self._w_wake = False  # an add/poke/remove woke us; handled below
                remind = self._q_next_remind()
                if self._landings or (remind is not None and remind <= workflow.now()):
                    break
            for watch_id, spec in list(self._watches.items()):
                if watch_id not in self._w_poked and self._w_due[watch_id] > workflow.now():
                    continue
                self._w_poked.discard(watch_id)
                if spec.give_up_at and _at(spec.give_up_at) <= workflow.now():
                    await self._w_give_up(spec)
                    if self._status == "resolved":  # a `fail` give-up ended the task
                        return
                    continue
                self._w_checks[watch_id] += 1
                result = await _act("check_watch", self._ws, self._task_id, spec, result_type=str, **_CHECK)
                if result.startswith("met"):
                    trigger_id = await _act(
                        "watch_met", self._ws, self._task_id, spec, self._w_checks[watch_id], result, result_type=str
                    )
                    self._w_drop(watch_id)
                    if trigger_id not in self._seen_inputs:
                        self._seen_inputs.add(trigger_id)
                        self._pending.append(TaskInputRef(trigger_id, "watch"))
                        self._wakes += 1
                    break
                self._w_due[watch_id] = workflow.now() + timedelta(minutes=spec.every_min)
        await self._w_sync_until()

    async def _w_give_up(self, spec: WatchSpec) -> None:
        self._w_drop(spec.watch_id)
        if spec.on_give_up == "ask":
            ref = await _act("watch_gave_up", self._ws, self._task_id, spec, result_type=AskRef)
            await self.ask(ref)  # Lane Q opens the Question and keeps the task waiting on it
        else:
            await self._transition("resolved", "watch_gave_up")

    async def _w_sync_until(self) -> None:
        until = max((s.give_up_at for s in self._watches.values() if s.give_up_at), default=None)
        if until != self._w_until:
            await _act("set_watch_until", self._ws, self._task_id, until)
            self._w_until = until


# --- Activities -------------------------------------------------------------------------------------------


@activity.defn(name="check_watch")
async def check_watch(ws: str, task_id: str, spec: WatchSpec) -> str:
    """`met[:detail]` | `not_met` | `error`. `url`: one GET answers 2xx. `pr_checks`: every status and check run
    on the PR's head has concluded (the detail says how)."""
    import httpx

    from core.services import link_preview

    try:
        if spec.kind == "url":
            # The SSRF-safe fetch the link previews use: public hosts only, pinned to the resolved address.
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False) as http:
                request = await link_preview.pinned_request(http, "GET", str(spec.target.get("url") or ""))
                resp = await http.send(request, stream=True)  # the status is all we need: never read the body
                await resp.aclose()
            return "met" if 200 <= resp.status_code < 300 else "not_met"
        return await _pr_checks(ws, spec.target)
    except link_preview.Refused:
        return "error"  # a private or unresolvable host: never met, the give-up time ends it
    except Exception:  # noqa: BLE001 — a flaky provider is "not yet", never a failed task
        return "error"


async def _pr_checks(ws: str, target: dict) -> str:
    import httpx

    from core.services.inbound_triggers import _GITHUB_API_BASE, _GITHUB_HEADERS, _github_token

    token = await _github_token(ws)
    if not token:
        return "error"
    repo, number = str(target.get("repo") or ""), int(target.get("number") or 0)
    headers = {**_GITHUB_HEADERS, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15.0) as http:
        pr = await http.get(f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{number}", headers=headers)
        pr.raise_for_status()
        sha = pr.json()["head"]["sha"]
        status = await http.get(f"{_GITHUB_API_BASE}/repos/{repo}/commits/{sha}/status", headers=headers)
        runs = await http.get(f"{_GITHUB_API_BASE}/repos/{repo}/commits/{sha}/check-runs", headers=headers)
        status.raise_for_status()
        runs.raise_for_status()
    state = status.json().get("state")  # success | failure | pending (also with no statuses: success)
    check_runs = runs.json().get("check_runs") or []
    if state == "pending" or any(r.get("status") != "completed" for r in check_runs):
        return "not_met"
    bad = [r["name"] for r in check_runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
    if state == "failure" or bad:
        return "met:failed:" + ",".join(bad)[:200]
    return "met:success"


@activity.defn(name="watch_met")
async def watch_met(ws: str, task_id: str, spec: WatchSpec, n: int, result: str) -> str:
    """The input that wakes the task: a `TRIG#watch:…` row with the one line the Worker reads."""
    from core import tasks_schema as ts
    from core.dynamodb import utc_now_iso
    from core.repositories import tasks_repo

    task = await tasks_repo.get_task(ws, task_id) or {}
    what = (
        f"PR {spec.target.get('repo')}#{spec.target.get('number')} checks finished"
        if spec.kind == "pr_checks"
        else f"{spec.target.get('url')} answered"
    )
    detail = result.partition(":")[2]
    text = f"Watch met: {what}" + (f" ({detail})" if detail else "") + "."
    trigger_id = ts.trigger_id("watch", task_id, spec.watch_id, n)
    await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": "watch",
            "ref": {
                "taskId": task_id,
                "watchId": spec.watch_id,
                "kind": spec.kind,
                "target": spec.target,
                "text": text,
            },
            "admission": "respond",
            "lane": task.get("lane") or "shared",
            "receivedAt": utc_now_iso(),
        },
    )
    return trigger_id


@activity.defn(name="watch_gave_up")
async def watch_gave_up(ws: str, task_id: str, spec: WatchSpec) -> AskRef:
    """The `TRIG#ask:{qid}` row Lane Q's `ask` update opens a Question from (the shape of `worker_ask`)."""
    from core import tasks_schema as ts
    from core.dynamodb import utc_now_iso
    from core.repositories import tasks_repo
    from core.services import questions

    task = await tasks_repo.get_task(ws, task_id) or {}
    # the give-up time is in the seed: "keep watching" moves it, and the next give-up is a new question
    qid = questions.qid_for(f"{task_id}:watch:{spec.watch_id}:{spec.give_up_at}")
    trigger_id = ts.trigger_id("ask", qid)
    what = (
        f"the checks on PR {spec.target.get('repo')}#{spec.target.get('number')}"
        if spec.kind == "pr_checks"
        else f"{spec.target.get('url')}"
    )
    await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": "ask",
            "ref": {
                "taskId": task_id,
                "qid": qid,
                "kind": "question",
                "subkind": "question",
                "title": f"Still waiting on {what}: keep watching?"[:200],
                "text": "Nothing happened before the time I was given. Keep watching another day, or stop?",
                "options": GIVE_UP_OPTIONS,
                "blocking": False,
                "watchId": spec.watch_id,
            },
            "admission": "respond",
            "lane": task.get("lane") or "shared",
            "receivedAt": utc_now_iso(),
        },
    )
    return AskRef(trigger_id, "question", "question", False)


@activity.defn(name="set_watch_until")
async def set_watch_until(ws: str, task_id: str, until: str | None) -> None:
    """`Task.watchUntil` = the latest give-up time of the task's watches (shown as "Watching"), None when none."""
    from core.repositories import tasks_repo

    task = await tasks_repo.get_task(ws, task_id)
    if task is None or task.get("watchUntil") == until:
        return
    await tasks_repo.update_task_fields(ws, task_id, {"watchUntil": until}, expected_version=task["version"])


def watch_id_for(task_id: str, kind: str, target: dict) -> str:
    """Deterministic: the same ask twice (a tool retry) is the same watch."""
    import hashlib
    import json

    return hashlib.sha256(f"{task_id}:{kind}:{json.dumps(target, sort_keys=True)}".encode()).hexdigest()[:16]


def give_up_default() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


ACTIVITIES = [check_watch, watch_met, watch_gave_up, set_watch_until]
