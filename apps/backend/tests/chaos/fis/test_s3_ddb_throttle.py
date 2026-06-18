"""SP2-S3 — deny DynamoDB endpoint reachability from the backend subnets.

Fault: ``aws:network:disrupt-connectivity`` scope=dynamodb for 3m against the
private subnets that run the backend, so the backend's DDB calls fail for the
duration. The assertions are that there is no 5xx storm (the experiment must
COMPLETE, not be stopped by the P9/P10/W10 rate gauges) and that a chat turn
succeeds once the throttle lifts.

SHARED-INFRA — runs against subnets the whole dev env shares; game-day quiet
window only.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s3-ddb-throttle"
AGENT_ID = "friday"


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s3_ddb_throttle(fis_client):
    token = await mint_test_token()
    agent_id = _agent_id()

    # (a) STEADY STATE.
    assert await drive_one_chat_turn(token, agent_id), "steady-state chat turn failed before injection"

    # (b) RESOLVE.
    template_id = resolve_template_id(fis_client, TEMPLATE_NAME)

    # (c) START.
    experiment = fis_client.start_experiment(experimentTemplateId=template_id)["experiment"]
    experiment_id = experiment["id"]

    # (d) WAIT — must COMPLETE. A 'stopped' here means the curated 5xx/chat-error
    # rate gauges (P9/P10/W10) tripped: a DDB throttle that leaked into a 5xx
    # storm rather than a clean degraded path. That is the failure signal.
    state = wait_experiment_terminal(fis_client, experiment_id)
    assert state == "completed", (
        f"S3 ended {state!r}, expected 'completed' — a stop means a 5xx storm leaked from the DDB throttle"
    )

    # (e) RECOVERY — a chat turn succeeds after the throttle lifts.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=30.0)

    seconds = await poll_until(_chat_recovered, timeout_s=120.0, interval_s=5.0)
    assert seconds <= 120.0
