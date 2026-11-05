"""RouterWorkflow: per-lane batches, trigger dedup, fallback, Continue-As-New keeping `seen` (O Task 2)."""

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from core.temporal.types import RouterState, TriggerRef
from core.temporal.workflows import WORKFLOWS, RouterWorkflow

from .fake_activities import Fakes, non_retryable

WS = "org_1"


@pytest.fixture
def fakes():
    return Fakes()


async def _router(env, fakes, refs, *, wait_turns):
    queue = f"q-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        handle = await env.client.start_workflow(
            RouterWorkflow.run, RouterState(WS), id=f"router:{uuid.uuid4()}", task_queue=queue
        )
        for r in refs:
            await handle.signal(RouterWorkflow.trigger, r)
        for _ in range(200):
            if len([n for n in fakes.names() if n in ("router_turn", "apply_fallback")]) >= wait_turns:
                break
            await asyncio.sleep(0.05)
        return handle


def _ref(tid, lane="shared", room=None):
    return TriggerRef(WS, tid, lane, "respond", room)


async def test_router_batches_by_lane(temporal_env, fakes):
    gate = asyncio.Event()

    async def slow_first(inp):
        await gate.wait()

    fakes.behaviour["router_turn"] = slow_first
    queue = f"q-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        handle = await temporal_env.client.start_workflow(
            RouterWorkflow.run,
            RouterState(WS, [_ref("a"), _ref("b", "u:x"), _ref("c")]),
            id="router:b",
            task_queue=queue,
        )
        await asyncio.sleep(0.5)
        gate.set()
        for _ in range(100):
            if len(fakes.args("router_turn")) >= 2:
                break
            await asyncio.sleep(0.05)
        await handle.terminate()
    turns = [a[0] for a in fakes.args("router_turn")]
    assert turns[0].trigger_ids == ["a", "c"] and turns[0].lane == "shared"
    assert turns[1].trigger_ids == ["b"] and turns[1].lane == "u:x"


async def test_router_batch_never_spans_rooms(temporal_env, fakes):
    """Audit #6: the shared lane spans rooms; one batch is one room (memory audience, open tasks, fallback)."""
    refs = [_ref("a", room="r1"), _ref("b", room="r2"), _ref("c", room="r1")]
    queue = f"q-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        h = await temporal_env.client.start_workflow(
            RouterWorkflow.run, RouterState(WS, refs), id=f"router:{uuid.uuid4()}", task_queue=queue
        )
        for _ in range(200):
            if len(fakes.args("router_turn")) >= 2:
                break
            await asyncio.sleep(0.05)
        await h.terminate()
    assert sorted(a[0].trigger_ids for a in fakes.args("router_turn")) == [["a", "c"], ["b"]]


async def test_router_turn_tries_twice_then_falls_back(temporal_env, fakes):
    """Audit #17: a failing batch gets one retry (not three) before the fallback, so other lanes wait ~10 min, not 20."""

    def down(inp):
        raise RuntimeError("bridge down")

    fakes.behaviour["router_turn"] = down
    h = await _router(temporal_env, fakes, [_ref("a")], wait_turns=3)
    await h.terminate()
    assert fakes.names().count("router_turn") == 2
    assert fakes.args("apply_fallback") == [(WS, ["a"])]


async def test_router_dedups_trigger(temporal_env, fakes):
    h = await _router(temporal_env, fakes, [_ref("a"), _ref("a")], wait_turns=1)
    await asyncio.sleep(0.3)
    assert [a[0].trigger_ids for a in fakes.args("router_turn")] == [["a"]]
    await h.terminate()


async def test_router_fallback_on_failure(temporal_env, fakes):
    fakes.behaviour["router_turn"] = non_retryable
    h = await _router(temporal_env, fakes, [_ref("a")], wait_turns=2)
    assert fakes.args("apply_fallback") == [(WS, ["a"])]
    await h.terminate()


async def test_router_continue_as_new_keeps_seen(temporal_env, fakes):
    queue = f"q-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        handle = await temporal_env.client.start_workflow(
            RouterWorkflow.run, RouterState(WS), id="router:can", task_queue=queue
        )
        first_run = (await handle.describe()).run_id
        await handle.signal(RouterWorkflow.trigger, _ref("early"))
        for i in range(0, 2_001, 100):
            await asyncio.gather(
                *(handle.signal(RouterWorkflow.trigger, _ref(f"t{j}")) for j in range(i, min(i + 100, 2_001)))
            )
        for _ in range(200):
            if (await handle.describe()).run_id != first_run:
                break
            await asyncio.sleep(0.05)
        assert (await handle.describe()).run_id != first_run
        await handle.signal(RouterWorkflow.trigger, _ref("t2000"))  # seen before CAN
        await handle.signal(RouterWorkflow.trigger, _ref("fresh"))
        for _ in range(100):
            ids = [t for a in fakes.args("router_turn") for t in a[0].trigger_ids]
            if "fresh" in ids and len(ids) >= 2_003:
                break
            await asyncio.sleep(0.05)
        await handle.terminate()
    ids = [t for a in fakes.args("router_turn") for t in a[0].trigger_ids]
    assert ids.count("t2000") == 1 and ids.count("early") == 1
    assert ids.count("fresh") == 1
    assert len(ids) == len(set(ids)) == 2_003  # every queued ref survived Continue-As-New, once


async def test_router_survives_fallback_failure(temporal_env, fakes):
    fakes.behaviour["router_turn"] = non_retryable
    fakes.behaviour["apply_fallback"] = non_retryable
    queue = f"q-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=WORKFLOWS, activities=fakes.activities()):
        h = await temporal_env.client.start_workflow(
            RouterWorkflow.run, RouterState(WS, [_ref("a")]), id="router:fb", task_queue=queue
        )
        for _ in range(100):
            if fakes.args("apply_fallback"):
                break
            await asyncio.sleep(0.05)
        await h.signal(RouterWorkflow.trigger, _ref("b"))
        for _ in range(100):
            if len(fakes.args("router_turn")) >= 2:
                break
            await asyncio.sleep(0.05)
        await h.terminate()
    assert [a[0].trigger_ids for a in fakes.args("router_turn")] == [["a"], ["b"]]
