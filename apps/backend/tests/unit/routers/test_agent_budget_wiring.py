"""The agent-budget gate is wired into EVERY agent-creation path.

This PR's thesis is "a limit enforced on some of them is not a limit", and the
wiring at each call site is exactly what a later refactor drops silently — the
shared guard keeps passing its own unit tests while a path quietly stops calling
it. So each path gets one assertion here that an over-budget owner is refused,
IN THE SHAPE THAT PATH USES:

  /chat            -> an error ``res`` frame (the RPC is consumed, never forwarded)
  Teams REST       -> HTTPException 409
  Atlas bridge     -> HTTPException 409
  hire approval    -> returns None (runs inside /recommend, must degrade not fail)

The marketplace single-agent hire (and the ordering guarantee that keeps its
gate below the already-installed short-circuit) lives in
test_deploy_gate_ultra.py, next to the other deploy-route gates; the approval
path's boolean refusal is in test_marketplace_hire.py.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://stub-management-api.local")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_OVER_BUDGET = AsyncMock(side_effect=HTTPException(status_code=409, detail="This workspace is at its 10-agent limit."))


@pytest.mark.asyncio
async def test_chat_create_refuses_over_budget_without_minting():
    """The /chat path CONSUMES the RPC, so its refusal must be an error ``res``
    frame on the original req id — raising would hang the frontend on a request
    nobody answers. The frontend keys the upgrade card off ``code``.
    """
    from routers.websocket_chat import _intercept_agents_create

    mint = AsyncMock()
    mgmt = MagicMock()
    mgmt.send_message = MagicMock()
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)

    with (
        patch("core.services.agent_budget.enforce_agent_budget", new=_OVER_BUDGET),
        patch("routers.websocket_chat.create_paperclip_for_chat", new=mint),
        patch(
            "routers.websocket_chat.get_management_api_client",
            new=AsyncMock(return_value=mgmt),
        ),
    ):
        consumed = await _intercept_agents_create(
            connection_id="c1",
            req_id="r1",
            user_id="u",
            owner_id="owner",
            params={"name": "Ray"},
            ip="10.0.0.1",
            token="tok",
        )

    assert consumed is True, "must consume the RPC, not fall through to the gateway"
    mint.assert_not_awaited()
    frame = mgmt.send_message.call_args[0][1]
    assert frame["ok"] is False
    assert frame["id"] == "r1"
    assert frame["error"]["code"] == "agent_budget_exceeded"


@pytest.mark.asyncio
async def test_teams_rest_create_refuses_over_budget_before_minting(monkeypatch):
    """Refused before the service token is minted and before Paperclip is
    touched, so a refusal leaves nothing to reconcile."""
    from routers.teams import agents as teams_agents

    admin = MagicMock()
    admin.create_agent = AsyncMock()
    monkeypatch.setattr(teams_agents, "_admin", lambda: admin)
    monkeypatch.setattr(teams_agents, "_resolve_owner_id", lambda _ctx: "owner")
    monkeypatch.setattr(teams_agents, "_creator_user_id", lambda _ctx: "user")
    monkeypatch.setattr(teams_agents, "enforce_agent_budget", _OVER_BUDGET)

    body = MagicMock(name="Helper", role="engineer")
    with pytest.raises(HTTPException) as exc:
        await teams_agents.create_agent(body, MagicMock())

    assert exc.value.status_code == 409
    admin.create_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_atlas_bridge_refuses_over_budget_before_claiming(monkeypatch):
    """The Atlas bridge is the path Atlas reaches headlessly. Refused before the
    owner-wide deploy claim, so a refusal never has to unwind a held claim."""
    from routers import agent_builder

    claim = AsyncMock()
    monkeypatch.setattr(agent_builder, "enforce_agent_budget", _OVER_BUDGET)
    monkeypatch.setattr(agent_builder, "_require_hook_token", AsyncMock(return_value={}))
    monkeypatch.setattr(agent_builder.container_repo, "try_claim_owner_deploy", claim)

    with pytest.raises(HTTPException) as exc:
        await agent_builder.register_built_agent("owner", MagicMock(), MagicMock())

    assert exc.value.status_code == 409
    claim.assert_not_awaited()
