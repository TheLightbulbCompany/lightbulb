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
from types import SimpleNamespace

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://stub-management-api.local")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _replay_miss():
    from core.services.paperclip_admin_client import PaperclipAgentCreateReplayMiss

    return PaperclipAgentCreateReplayMiss(
        "replay miss",
        status_code=409,
        body='{"code":"agent_create_idempotency_replay_miss"}',
    )


@pytest.mark.asyncio
async def test_chat_create_at_cap_allows_only_an_existing_operation():
    """The /chat path CONSUMES the RPC, so its refusal must be an error ``res``
    frame on the original req id — raising would hang the frontend on a request
    nobody answers. The frontend keys the upgrade card off ``code``.
    """
    from routers.websocket_chat import _intercept_agents_create

    mint = AsyncMock(side_effect=_replay_miss())
    mgmt = MagicMock()
    mgmt.send_message = MagicMock()
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)

    with (
        patch("core.services.agent_budget.is_at_agent_budget", new=AsyncMock(return_value=True)),
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
    mint.assert_awaited_once()
    assert mint.await_args.kwargs["idempotency_replay_only"] is True
    frame = mgmt.send_message.call_args[0][1]
    assert frame["ok"] is False
    assert frame["id"] == "r1"
    assert frame["error"]["code"] == "agent_budget_exceeded"


@pytest.mark.asyncio
async def test_teams_rest_create_at_cap_allows_only_an_existing_operation(monkeypatch):
    from routers.teams import agents as teams_agents

    admin = MagicMock()
    admin.create_agent = AsyncMock(side_effect=_replay_miss())
    monkeypatch.setattr(teams_agents, "_admin", lambda: admin)
    monkeypatch.setattr(teams_agents, "_resolve_owner_id", lambda _ctx: "owner")
    monkeypatch.setattr(teams_agents, "_creator_user_id", lambda _ctx: "user")
    monkeypatch.setattr(teams_agents, "is_at_agent_budget", AsyncMock(return_value=True))
    monkeypatch.setattr(teams_agents, "_user_session", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(teams_agents.service_token, "mint", lambda _owner_id: "token")

    body = SimpleNamespace(
        name="Helper",
        role="engineer",
        title=None,
        capabilities=None,
        reports_to=None,
        budget_monthly_cents=0,
    )
    with pytest.raises(HTTPException) as exc:
        await teams_agents.create_agent(body, SimpleNamespace(company_id="company"))

    assert exc.value.status_code == 409
    admin.create_agent.assert_awaited_once()
    assert admin.create_agent.await_args.kwargs["idempotency_replay_only"] is True


@pytest.mark.asyncio
async def test_atlas_bridge_threads_cap_state_into_the_claimed_operation(monkeypatch):
    from routers import agent_builder

    claim = AsyncMock(return_value=("claim", None))
    release = AsyncMock()
    register = AsyncMock(side_effect=HTTPException(status_code=409, detail="at limit"))
    monkeypatch.setattr(agent_builder, "is_at_agent_budget", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_builder, "_require_hook_token", AsyncMock(return_value={}))
    monkeypatch.setattr(agent_builder.container_repo, "try_claim_owner_deploy", claim)
    monkeypatch.setattr(agent_builder.container_repo, "release_owner_deploy_claim", release)
    monkeypatch.setattr(agent_builder, "_register_built_agent", register)

    with pytest.raises(HTTPException) as exc:
        await agent_builder.register_built_agent(
            "owner",
            SimpleNamespace(staging_agent_id="staging-1"),
            MagicMock(),
        )

    assert exc.value.status_code == 409
    claim.assert_awaited_once()
    assert register.await_args.kwargs["idempotency_replay_only"] is True
    release.assert_awaited_once()
