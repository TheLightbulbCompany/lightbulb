"""SP2-S1 — kill one per-user OpenClaw container task mid-chat.

Fault: ``aws:ecs:stop-task`` against the dedicated chaos test user's per-user
OpenClaw Fargate task. ECS restarts it; the assertion is that the gateway
re-discovers the new task IP and a fresh chat turn succeeds within ~30s of the
experiment reaching terminal.

DEV ONLY — the autouse ``_dev_only_guard`` (conftest.py) skips this entire
suite unless ENVIRONMENT=dev AND CHAOS_FIS_CONFIRM=dev. NO boto3/network call
happens at import or collection; everything below runs only inside the test.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s1-container-kill"
AGENT_ID = "friday"  # the per-owner CEO seed agent; overridable via CHAOS_FIS_AGENT_ID


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s1_container_kill(fis_client):
    token = await mint_test_token()
    agent_id = _agent_id()

    # (a) STEADY STATE — a chat turn must succeed before we inject the fault.
    assert await drive_one_chat_turn(token, agent_id), "steady-state chat turn failed before injection"

    # (b) RESOLVE the deployed template by its Name tag.
    template_id = resolve_template_id(fis_client, TEMPLATE_NAME)

    # (c) START the experiment.
    experiment = fis_client.start_experiment(experimentTemplateId=template_id)["experiment"]
    experiment_id = experiment["id"]

    # (d) WAIT for terminal — must COMPLETE, not be stopped by a stop-condition
    # (which would signal real customer impact) nor failed.
    state = wait_experiment_terminal(fis_client, experiment_id)
    assert state == "completed", f"S1 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — the gateway must re-discover the fresh task IP and a new
    # chat turn must succeed within ~30s of the kill+restart. A fresh token per
    # poll keeps the probe self-contained.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=30.0)

    seconds = await poll_until(_chat_recovered, timeout_s=120.0, interval_s=5.0)
    assert seconds <= 120.0
