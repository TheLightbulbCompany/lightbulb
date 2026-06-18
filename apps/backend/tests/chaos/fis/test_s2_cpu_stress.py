"""SP2-S2 — CPU-stress the per-user OpenClaw task to 100% for 5m.

Fault: ``aws:ecs:task-cpu-stress`` against the dedicated chaos test user's
task, to reproduce (or rule out) the healthcheck crash-loop on the 0.5-vCPU
box. The assertion is that the container stays healthy through the stress and
chat recovers afterward.

NOT RUNNABLE IN CURRENT DEV — see the skip below. The full harness is kept
intact under the skip so it is ready the moment the prereq exists.

DEV ONLY — the autouse ``_dev_only_guard`` (conftest.py) skips this suite
unless ENVIRONMENT=dev AND CHAOS_FIS_CONFIRM=dev. NO boto3/network call at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s2-cpu-stress"
AGENT_ID = "friday"


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s2_cpu_stress(fis_client):
    pytest.skip(
        "S2 needs a dedicated chaos-test task def with an SSM-agent sidecar + ECS Exec "
        "disabled — see fis-stack.ts S2 PREREQ note; not runnable in current dev"
    )

    # --- Harness below is ready for when the prereq task def exists. ---
    token = await mint_test_token()
    agent_id = _agent_id()

    # (a) STEADY STATE.
    assert await drive_one_chat_turn(token, agent_id), "steady-state chat turn failed before injection"

    # (b) RESOLVE.
    template_id = resolve_template_id(fis_client, TEMPLATE_NAME)

    # (c) START.
    experiment = fis_client.start_experiment(experimentTemplateId=template_id)["experiment"]
    experiment_id = experiment["id"]

    # (d) WAIT — must COMPLETE. A stop (container-error-state / 5xx) means the
    # CPU stress crash-looped the box: a real defect, not a clean pass.
    state = wait_experiment_terminal(fis_client, experiment_id, timeout_s=420.0)
    assert state == "completed", f"S2 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — chat works again once the 5m stress lifts.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=30.0)

    seconds = await poll_until(_chat_recovered, timeout_s=120.0, interval_s=5.0)
    assert seconds <= 120.0
