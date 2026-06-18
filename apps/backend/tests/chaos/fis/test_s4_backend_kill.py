"""SP2-S4 — kill the backend FastAPI task.

Fault: ``aws:ecs:stop-task`` against the backend Fargate task (selected by the
ECS service parameter). ECS restarts the task and the ALB target group brings
it back into rotation; the assertion is recovery within SLO — a chat turn
succeeds once the service is healthy again. A longer recovery budget is used
because a full task restart + ALB health-check pass takes longer than a
gateway IP re-discovery.

SHARED-INFRA — a full backend outage; game-day quiet window only.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s4-backend-kill"
AGENT_ID = "friday"


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s4_backend_kill(fis_client):
    token = await mint_test_token()
    agent_id = _agent_id()

    # (a) STEADY STATE.
    assert await drive_one_chat_turn(token, agent_id), "steady-state chat turn failed before injection"

    # (b) RESOLVE.
    template_id = resolve_template_id(fis_client, TEMPLATE_NAME)

    # (c) START.
    experiment = fis_client.start_experiment(experimentTemplateId=template_id)["experiment"]
    experiment_id = experiment["id"]

    # (d) WAIT — must COMPLETE.
    state = wait_experiment_terminal(fis_client, experiment_id)
    assert state == "completed", f"S4 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — allow a longer budget: ECS must restart the task and the
    # ALB target group must pass health checks before the gateway/chat path is
    # served again.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=45.0)

    seconds = await poll_until(_chat_recovered, timeout_s=300.0, interval_s=10.0)
    assert seconds <= 300.0
