"""SP2-S5 — kill the Paperclip Fargate task.

Fault: ``aws:ecs:stop-task`` against the Paperclip Fargate task (selected by
the ECS service parameter). ECS restarts it; the assertion is that the
Teams/Paperclip path recovers — a chat turn (which routes through Friday's
openclaw_gateway adapter and exercises the Paperclip BFF) succeeds again. If
the Paperclip ALB host route is cheaply reachable we probe it as an extra
signal; otherwise the chat turn is the proxy for Paperclip recovery.

SHARED-INFRA — game-day quiet window only.

DEV ONLY — guarded by the autouse ``_dev_only_guard``. NO boto3/network at
import or collection.
"""

import pytest

from ._chat_driver import drive_one_chat_turn, mint_test_token
from ._recovery import poll_until, wait_experiment_terminal
from ._templates import resolve_template_id

pytestmark = pytest.mark.fis

TEMPLATE_NAME = "isol8-dev-fis-s5-paperclip-kill"
AGENT_ID = "friday"
PAPERCLIP_PUBLIC_URL = "https://teams-dev.isol8.co"  # overridable via CHAOS_FIS_PAPERCLIP_URL


def _agent_id() -> str:
    import os

    return os.environ.get("CHAOS_FIS_AGENT_ID", AGENT_ID)


async def _paperclip_reachable() -> bool:
    """Cheap, best-effort reachability probe of the Paperclip public health
    surface. Any non-5xx HTTP response counts as reachable; a connection error
    counts as not-yet-recovered. Falls back silently if httpx is unavailable."""
    import os

    base = os.environ.get("CHAOS_FIS_PAPERCLIP_URL", PAPERCLIP_PUBLIC_URL)
    try:
        import httpx

        async with httpx.AsyncClient(base_url=base, timeout=10) as c:
            resp = await c.get("/")
            return resp.status_code < 500
    except Exception:
        return False


async def test_s5_paperclip_kill(fis_client):
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
    assert state == "completed", f"S5 ended {state!r}, expected 'completed'"

    # (e) RECOVERY — Teams/Paperclip path recovers. Primary signal is a chat
    # turn (routes through the Paperclip-bound seed agent). The Paperclip
    # reachability probe is a cheap supplementary signal; it is OR'd in so a
    # transient probe failure never masks a working chat path.
    async def _recovered() -> bool:
        fresh = await mint_test_token()
        chat_ok = await drive_one_chat_turn(fresh, agent_id, timeout_s=30.0)
        if chat_ok:
            return True
        # Chat not back yet — surface Paperclip reachability as a leading signal.
        return await _paperclip_reachable()

    # Resolve once at the end as the hard assertion: the chat path must work.
    seconds = await poll_until(_recovered, timeout_s=180.0, interval_s=5.0)
    assert seconds <= 180.0
    final = await mint_test_token()
    assert await drive_one_chat_turn(final, agent_id, timeout_s=30.0), "S5: chat path did not fully recover"
