"""Fake Activities with the real names, recording calls; tests set `Fakes` behaviour per name."""

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from core.temporal.types import AttemptRef, RouterTurnIn, RouterTurnOut, WorkerTurnIn, WorkerTurnOut


class Fakes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.behaviour: dict[str, Callable[..., Any]] = {}
        self.attempts = 0

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def args(self, name: str) -> list[tuple]:
        return [a for n, a in self.calls if n == name]

    async def _call(self, name: str, *args: Any, default: Any = None) -> Any:
        self.calls.append((name, args))
        fn = self.behaviour.get(name)
        if fn is None:
            return default
        out = fn(*args)
        return await out if asyncio.iscoroutine(out) else out

    def activities(self) -> list:
        f = self

        @activity.defn(name="router_turn")
        async def router_turn(inp: RouterTurnIn) -> RouterTurnOut:
            return await f._call("router_turn", inp, default=RouterTurnOut(inp.batch_id))

        @activity.defn(name="apply_fallback")
        async def apply_fallback(ws: str, trigger_ids: list[str]) -> None:
            await f._call("apply_fallback", ws, trigger_ids)

        @activity.defn(name="check_budget")
        async def check_budget(ws: str, task_id: str) -> bool:
            return await f._call("check_budget", ws, task_id, default=True)

        @activity.defn(name="acquire_slot")
        async def acquire_slot(ws: str, task_id: str) -> bool:
            return await f._call("acquire_slot", ws, task_id, default=True)

        @activity.defn(name="release_slot")
        async def release_slot(ws: str, task_id: str) -> None:
            await f._call("release_slot", ws, task_id)

        @activity.defn(name="begin_attempt")
        async def begin_attempt(ws: str, task_id: str, input_trigger_ids: list[str], started: bool) -> AttemptRef:
            f.attempts += 1
            return await f._call(
                "begin_attempt",
                ws,
                task_id,
                input_trigger_ids,
                started,
                default=AttemptRef(f.attempts, f"run-{f.attempts}"),
            )

        @activity.defn(name="worker_turn")
        async def worker_turn(inp: WorkerTurnIn) -> WorkerTurnOut:
            return await f._call("worker_turn", inp, default=WorkerTurnOut("completed"))

        @activity.defn(name="reconcile_attempt")
        async def reconcile_attempt(ws: str, task_id: str, n: int) -> str:
            return await f._call("reconcile_attempt", ws, task_id, n, default="lost")

        @activity.defn(name="mark_stop_requested")
        async def mark_stop_requested(ws: str, task_id: str, by: str) -> None:
            await f._call("mark_stop_requested", ws, task_id, by)

        @activity.defn(name="stop_attempt")
        async def stop_attempt(ws: str, task_id: str, n: int) -> None:
            await f._call("stop_attempt", ws, task_id, n)

        @activity.defn(name="deliver_input")
        async def deliver_input(ws: str, task_id: str, n: int, trigger_id: str) -> bool:
            return await f._call("deliver_input", ws, task_id, n, trigger_id, default=False)

        @activity.defn(name="end_attempt")
        async def end_attempt(ws: str, task_id: str, n: int, outcome: str, error: str | None) -> None:
            await f._call("end_attempt", ws, task_id, n, outcome, error)

        @activity.defn(name="transition")
        async def transition(ws: str, task_id: str, to: str, reason: str | None) -> None:
            await f._call("transition", ws, task_id, to, reason)

        @activity.defn(name="reconcile_state")
        async def reconcile_state(ws: str, task_id: str) -> str:
            return await f._call("reconcile_state", ws, task_id, default="other")

        @activity.defn(name="close_automation_run")  # lane A (automations.py): after an ended attempt
        async def close_automation_run(ws: str, task_id: str, failed: bool) -> None:
            await f._call("close_automation_run", ws, task_id, failed)

        return [
            router_turn,
            apply_fallback,
            check_budget,
            acquire_slot,
            release_slot,
            begin_attempt,
            worker_turn,
            reconcile_attempt,
            mark_stop_requested,
            stop_attempt,
            deliver_input,
            end_attempt,
            transition,
            reconcile_state,
            close_automation_run,
        ]


def non_retryable(*_: Any) -> None:
    raise ApplicationError("boom", type="RouterBroken", non_retryable=True)


def go_async(*_: Any) -> None:
    """A worker_turn that hands completion to the bridge and never hears back (lost temporal-worker)."""
    activity.raise_complete_async()


async def until_cancelled(*_: Any) -> WorkerTurnOut:
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.05)
