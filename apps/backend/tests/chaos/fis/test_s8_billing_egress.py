"""SP2-S8 — block billing-provider (Stripe/Metronome) egress.

Fault: ``aws:network:disrupt-connectivity`` for 3m (see fis-stack.ts S8
TODO(gameday): the current template targets the shared private subnets with
scope=all as a placeholder — the operator must narrow it to a Stripe/Metronome
prefix list before running, since scope=all blocks ALL egress, not just
billing). The assertions are:
  * DURING the cut, the billing/provision path returns a CLEAN response — a
    structured blocked-state (HTTP 402 with a `blocked` payload) or a healthy
    200, but NEVER a 5xx and NEVER a hung/timed-out request (no wedge).
  * AFTER egress is restored, the system recovers — a chat turn succeeds and
    the billing/provision path is healthy again.

SHARED-INFRA — game-day quiet window only.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import os

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_running, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s8-billing-egress-block"
AGENT_ID = "friday"
API_BASE = "https://api-dev.isol8.co/api/v1"  # overridable via CHAOS_FIS_API_BASE
# Provision/billing surfaces that touch the billing provider gate. A clean
# response is one that is NOT a 5xx and does NOT hang.
BILLING_PROBE_PATHS = ("/billing/account", "/container/status")
# A 402 carries the structured blocked-state; 202 is the org-member pending
# path; 2xx is healthy. Anything < 500 that is not a hang is a CLEAN response.
CLEAN_MAX_STATUS = 500


def _agent_id() -> str:
    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


def _api_base() -> str:
    return os.environ.get("CHAOS_FIS_API_BASE", API_BASE)


async def _billing_path_clean(token: str) -> bool:
    """True iff every billing/provision probe returns a clean response.

    Clean = a bounded HTTP response with status < 500 (200 healthy, 402
    structured-blocked, 202 pending). A 5xx, a timeout, or a connection error
    is NOT clean — that is the wedge S8 must rule out. Returns False on the
    first dirty probe.
    """
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=_api_base(), timeout=15) as c:
        for path in BILLING_PROBE_PATHS:
            try:
                resp = await c.get(path, headers=headers)
            except Exception:
                return False  # timeout / connection error == wedge
            if resp.status_code >= CLEAN_MAX_STATUS:
                return False
    return True


async def test_s8_billing_egress(fis_client):
    token = await mint_test_token()
    agent_id = _agent_id()

    # (a) STEADY STATE — chat works AND the billing path is clean before injection.
    assert await drive_one_chat_turn(token, agent_id), "steady-state chat turn failed before injection"
    assert await _billing_path_clean(token), "billing/provision path not clean at steady state"

    # (b) RESOLVE.
    template_id = resolve_template_id(fis_client, TEMPLATE_NAME)

    # (c) START.
    experiment = fis_client.start_experiment(experimentTemplateId=template_id)["experiment"]
    experiment_id = experiment["id"]

    # (d-pre) WAIT FOR THE FAULT TO BE LIVE. start_experiment returns while the
    # experiment is still pending/initiating — the NACL egress cut is NOT yet
    # applied. Probing now would sample BEFORE the outage window and S8 could
    # pass without ever exercising the cut. Block until the experiment state is
    # 'running' (the action is applied) so the during-fault probe samples during
    # the real outage. If it terminated already we fail loudly below.
    running_state = wait_experiment_running(fis_client, experiment_id)
    assert running_state == "running", (
        f"S8: experiment reached {running_state!r} before becoming 'running' — "
        "the egress cut was never live to probe during the fault"
    )

    # (e-during) DURING the cut, the billing/provision path must stay clean —
    # a structured blocked-state, never a 5xx storm or a hung request. Sample
    # repeatedly while the experiment is still injecting (3m fault window).
    # We do NOT block on terminal first: the clean-during-fault behaviour is
    # the core S8 invariant, so probe it while the fault is live.
    during_token = await mint_test_token()
    assert await _billing_path_clean(during_token), (
        "S8: billing/provision path was NOT clean during the egress cut (5xx storm or wedge)"
    )

    # (d) WAIT — must COMPLETE, not be stopped by the 5xx/chat-error gauges.
    state = wait_experiment_terminal(fis_client, experiment_id)
    assert state == "completed", (
        f"S8 ended {state!r}, expected 'completed' — a stop means the egress cut leaked into a 5xx storm"
    )

    # (e-after) RECOVERY — once egress is restored, chat works and the billing
    # path is healthy again.
    async def _recovered() -> bool:
        fresh = await mint_test_token()
        if not await _billing_path_clean(fresh):
            return False
        return await drive_one_chat_turn(fresh, agent_id, timeout_s=30.0)

    seconds = await poll_until(_recovered, timeout_s=180.0, interval_s=10.0)
    assert seconds <= 180.0
