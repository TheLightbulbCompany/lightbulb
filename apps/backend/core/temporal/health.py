"""`HealthWorkflow`: proves worker, client and harness end to end before any real workflow exists.
Workflow code imports only `temporalio` and the stdlib (sandbox-safe)."""

from datetime import timedelta

from temporalio import activity, workflow


@activity.defn
async def ping() -> str:
    return "pong"


@workflow.defn
class HealthWorkflow:
    @workflow.run
    async def run(self, sleep_s: int = 0) -> str:
        if sleep_s > 0:
            await workflow.sleep(sleep_s)
        return "ok:" + await workflow.execute_activity(ping, start_to_close_timeout=timedelta(seconds=10))


WORKFLOWS = [HealthWorkflow]
ACTIVITIES = [ping]
