"""SP2-S6 — force an Aurora failover on the dev Paperclip cluster.

Fault: ``aws:rds:failover-db-cluster`` to promote a standby and exercise that
provisioning + /teams reconnect to the new writer. Recovery (not
zero-disruption) is the assertion — failover is disruptive by design.

NOT RUNNABLE IN CURRENT DEV — see the skip below. The dev Paperclip cluster is
writer-only (no serverlessV2 reader), so failover has nothing to promote. The
full harness is kept intact under the skip, ready for when the reader exists.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s6-aurora-failover"
AGENT_ID = "friday"


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s6_aurora_failover(fis_client):
    pytest.skip(
        "S6 needs a serverlessV2 reader on the dev Paperclip Aurora cluster — see "
        "fis-stack.ts S6 PREREQ note; failover has nothing to promote in current writer-only dev"
    )

    # --- Harness below is ready for when the dev cluster has a reader. ---
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
    assert state == "completed", f"S6 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — the Paperclip-bound chat path reconnects to the new writer.
    # Allow a longer budget for the writer flip + connection-pool reconnect.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=45.0)

    seconds = await poll_until(_chat_recovered, timeout_s=240.0, interval_s=10.0)
    assert seconds <= 240.0
