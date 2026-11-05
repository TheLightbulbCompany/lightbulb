"""Real Temporal dev server on localhost:7233 (`temporal server start-dev`); skipped when none is running."""

import socket
import uuid

import pytest


def _dev_server_up() -> bool:
    try:
        socket.create_connection(("localhost", 7233), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _dev_server_up(), reason="no Temporal dev server on localhost:7233")
async def test_health_on_dev_server():
    from temporalio.client import Client
    from temporalio.worker import Worker

    from core.temporal.health import HealthWorkflow
    from core.temporal.registry import ALL_ACTIVITIES, ALL_WORKFLOWS

    client = await Client.connect("localhost:7233", namespace="default")
    queue = f"it-{uuid.uuid4()}"
    async with Worker(client, task_queue=queue, workflows=ALL_WORKFLOWS, activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(HealthWorkflow.run, 0, id=f"health-{uuid.uuid4()}", task_queue=queue)
    assert result == "ok:pong"
