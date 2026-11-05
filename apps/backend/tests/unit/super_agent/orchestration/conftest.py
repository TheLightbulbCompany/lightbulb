"""Temporal time-skipping harness for the orchestration suites (wave-1 plan T, Task 1).

Function scope: pytest-asyncio 0.23 gives each test its own event loop, so a session-scoped
environment would outlive the loop it was started on. One start is ~1 s; switch to session scope
when pytest-asyncio's ``loop_scope`` is available and the suite has enough workflow tests to matter."""

import uuid

import pytest_asyncio


@pytest_asyncio.fixture
async def temporal_env():
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture
async def temporal_worker(temporal_env):
    """A worker on a random task queue with every registered workflow and activity. Yields the queue name."""
    from temporalio.worker import Worker

    from core.temporal.registry import ALL_ACTIVITIES, ALL_WORKFLOWS

    queue = f"test-{uuid.uuid4()}"
    async with Worker(temporal_env.client, task_queue=queue, workflows=ALL_WORKFLOWS, activities=ALL_ACTIVITIES):
        yield queue
