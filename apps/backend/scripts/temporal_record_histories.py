"""Record the replay fixtures (wave-2 O Task 13): the TaskWorkflow happy path and one Router batch, run on
Temporal's time-skipping test server with the orchestration suite's fake Activities, written as JSON histories
under tests/unit/super_agent/orchestration/histories/. `test_replay_recorded_histories` replays them against
today's workflow code, so a change to a workflow that is not behind `workflow.patched` fails there first.

    cd apps/backend && uv run python -m scripts.temporal_record_histories [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from core.temporal.types import RouterState, RouterTurnOut, TaskStart, TriggerRef
from core.temporal.workflows import WORKFLOWS, RouterWorkflow, TaskWorkflow
from tests.unit.super_agent.orchestration.fake_activities import Fakes

WS, TASK_ID = "org_1", "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests/unit/super_agent/orchestration/histories"


async def _happy_path(env: WorkflowEnvironment, fakes: Fakes) -> str:
    queue = f"q-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        handle = await env.client.start_workflow(
            TaskWorkflow.run, TaskStart(WS, TASK_ID, "router"), id=f"task:{WS}:{TASK_ID}", task_queue=queue
        )
        await handle.result()
        return (await handle.fetch_history()).to_json()


async def _router_batch(env: WorkflowEnvironment, fakes: Fakes) -> str:
    """Two shared-lane triggers batched into one `router_turn`, then a private-lane one; the Router never
    completes, so the history ends with a termination (replay stops at the last workflow task)."""
    turns: list[str] = []

    async def router_turn(inp):
        turns.append(inp.lane)
        return RouterTurnOut(inp.batch_id, [TASK_ID] if inp.lane == "shared" else [], ["m1"])

    fakes.behaviour["router_turn"] = router_turn
    queue = f"q-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        handle = await env.client.start_workflow(
            RouterWorkflow.run,
            RouterState(
                WS, [TriggerRef(WS, "chat:m1", "shared", "respond"), TriggerRef(WS, "chat:m2", "shared", "candidate")]
            ),
            id=f"router:{WS}",
            task_queue=queue,
        )
        await handle.signal(RouterWorkflow.trigger, TriggerRef(WS, "chat:m3", "u:0123456789abcdef", "respond"))
        for _ in range(200):
            if len(turns) >= 2:
                break
            await asyncio.sleep(0.05)
        await handle.terminate("recorded")
        return (await handle.fetch_history()).to_json()


async def record(out: Path) -> list[Path]:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        written = []
        for name, fn in (("happy_path", _happy_path), ("router_batch", _router_batch)):
            path = out / f"{name}.json"
            path.write_text(await fn(env, Fakes()) + "\n")
            written.append(path)
        return written
    finally:
        await env.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for path in asyncio.run(record(args.out)):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
