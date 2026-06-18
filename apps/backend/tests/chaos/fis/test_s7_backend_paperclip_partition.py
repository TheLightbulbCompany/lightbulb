"""SP2-S7 — backend<->Paperclip network partition.

Fault: ``aws:network:disrupt-connectivity`` for 3m against the private subnets
(see fis-stack.ts S7 TODO(gameday): the current template targets the shared
private subnets with scope=all as a placeholder — the operator must confirm
the intended blast radius before running). The assertion is recovery once the
partition heals: a chat turn succeeds again.

SHARED-INFRA — game-day quiet window only.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s7-backend-paperclip-partition"
AGENT_ID = "friday"


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def test_s7_backend_paperclip_partition(fis_client):
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
    assert state == "completed", f"S7 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — after the partition heals (NACL deny rules removed), a
    # chat turn succeeds again. Allow a longer budget for connection-pool /
    # Paperclip-session re-establishment after the cut.
    async def _chat_recovered() -> bool:
        fresh = await mint_test_token()
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=45.0)

    seconds = await poll_until(_chat_recovered, timeout_s=240.0, interval_s=10.0)
    assert seconds <= 240.0
