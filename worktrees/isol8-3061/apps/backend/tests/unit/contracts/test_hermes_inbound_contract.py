"""Cross-lane conformance test (final-review Important 5 / I5).

C2, C3 and I1 were each invisible to a green suite because the two doors that build an
AgentCore ingress body (agent_mail_delivery.py, routers/messaging.py::post_paperclip_run) are
tested against a MOCKED runtime_transport.request -- nothing ever fed the body they build
through the bridge's own `bridge.turn.parse_ingress`, and nothing ever checked which of the
container's two tokens (`hook_token` vs `gateway_token`) rode the Authorization header. This
test drives the REAL backend call sites and the REAL bridge parser together, so a wire mismatch
on either side fails here first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

# The bridge lives outside the backend's own package tree (apps/infra/hermes), so it is not on
# sys.path by default -- put it there before importing bridge.turn.
_HERMES_ROOT = Path(__file__).resolve().parents[4] / "infra" / "hermes"
if str(_HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_ROOT))

from bridge import turn as bridge_turn  # noqa: E402

from core.services import agent_mail_delivery as delivery  # noqa: E402
from core.services import runtime_transport  # noqa: E402

OWNER, AGENT = "org_1", "agent_ceo"
HOOK_TOKEN, GATEWAY_TOKEN = "hook-tok-for-a", "gateway-tok-for-a"


def _agentcore_row() -> dict:
    return {
        "hook_token": HOOK_TOKEN,
        "gateway_token": GATEWAY_TOKEN,
        "agentcore_runtime_arn": "arn:x",
    }


def _patch_capturing_request(monkeypatch, module):
    captured: dict = {}

    async def fake_request(
        container,
        path,
        *,
        ip=None,
        method="POST",
        json_body=None,
        content=None,
        headers=None,
        timeout_s=30.0,
    ):
        captured["path"] = path
        captured["headers"] = headers
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(202, '{"status": "accepted"}', {"content-type": "application/json"})

    monkeypatch.setattr(module, "request", fake_request)
    return captured


async def test_agent_mail_with_a_thread_key_is_a_conformant_gateway_authed_ingress(
    monkeypatch,
):
    monkeypatch.setattr(delivery.container_repo, "get_by_owner_id", AsyncMock(return_value=_agentcore_row()))
    monkeypatch.setattr(delivery, "trial_block_reason", AsyncMock(return_value=None))
    captured = _patch_capturing_request(monkeypatch, runtime_transport)

    result = await delivery.deliver_to_agent(
        owner_id=OWNER,
        agent_id=AGENT,
        message="hi",
        idempotency_key="eml_1",
        session_key="lightbulb-email:thread-42",
    )

    assert result == "delivered"
    assert bridge_turn.parse_ingress(dict(captured["json_body"]))
    assert captured["headers"] == {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


async def test_agent_mail_with_no_thread_key_is_still_a_conformant_ingress(monkeypatch):
    """No derivable Message-ID -> agent_mail_delivery omits sessionKey entirely (I1's other
    half): the bridge must still accept the body, deriving its own session key."""
    monkeypatch.setattr(delivery.container_repo, "get_by_owner_id", AsyncMock(return_value=_agentcore_row()))
    monkeypatch.setattr(delivery, "trial_block_reason", AsyncMock(return_value=None))
    captured = _patch_capturing_request(monkeypatch, runtime_transport)

    result = await delivery.deliver_to_agent(
        owner_id=OWNER,
        agent_id=AGENT,
        message="hi",
        idempotency_key="eml_2",
    )

    assert result == "delivered"
    assert "sessionKey" not in captured["json_body"]
    assert bridge_turn.parse_ingress(dict(captured["json_body"]))
    assert captured["headers"] == {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def test_paperclip_run_is_a_conformant_gateway_authed_ingress(client, monkeypatch):
    captured = _patch_capturing_request(monkeypatch, runtime_transport)
    body = {
        "runId": "run_1",
        "agentId": AGENT,
        "companyId": "company_1",
        "context": {"taskId": "task_1", "wakeReason": "New comment on your task"},
    }

    with patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": OWNER,
                "hook_token": HOOK_TOKEN,
                "gateway_token": GATEWAY_TOKEN,
                "runtime_host": "agentcore",
            }
        ),
    ):
        r = client.post(
            f"/api/v1/internal/paperclip/runs/{OWNER}",
            json=body,
            headers={"Authorization": f"Bearer {HOOK_TOKEN}"},
        )

    assert r.status_code == 202
    assert bridge_turn.parse_ingress(dict(captured["json_body"]))
    assert captured["headers"] == {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
