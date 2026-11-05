import time

from core.temporal.health import HealthWorkflow


async def test_health_workflow_time_skipping(temporal_env, temporal_worker):
    started = time.monotonic()
    result = await temporal_env.client.execute_workflow(
        HealthWorkflow.run, 7 * 24 * 3600, id="health-skip", task_queue=temporal_worker
    )
    assert result == "ok:pong"
    assert time.monotonic() - started < 10  # a week of workflow time, seconds of wall time
