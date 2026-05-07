"""Tests for /teams agent create → openclaw pairing."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# get_gateway_pool() lazily constructs a ManagementApiClient that
# requires WS_MANAGEMENT_API_URL — set a bogus value so the singleton
# can be built. The pool is never actually used because
# pair_with_openclaw is monkeypatched in every test below.
os.environ.setdefault(
    "WS_MANAGEMENT_API_URL",
    "https://test.execute-api.us-east-1.amazonaws.com/test",
)


@pytest.mark.asyncio
async def test_teams_create_agent_pairs_with_openclaw_when_container_up(monkeypatch):
    """After Paperclip create_agent succeeds, the route calls
    pair_with_openclaw with the new agent's UUID against the OWNER's
    container."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    paperclip_agent = {
        "id": pc_uuid,
        "name": "Code Reviewer",
        "role": "engineer",
    }

    # Patch the symbols used by the route. Names match the imports in
    # routers/teams/agents.py.
    pair_mock = AsyncMock()
    monkeypatch.setattr(
        "routers.teams.agents.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.teams.agents._container_is_reachable",
        AsyncMock(return_value=(True, "10.0.0.5", "gw_token")),
    )

    # Patch the admin client to return a known paperclip_agent.
    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value=paperclip_agent)
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )

    # Stub anything else the route needs that touches I/O.
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )
    monkeypatch.setattr(
        "routers.teams.agents._service_token_for",
        AsyncMock(return_value="svc_tok"),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    ctx = MagicMock()
    ctx.auth.user_id = "user_member_b"
    ctx.auth.org_id = "org_xyz"
    ctx.company_id = "co_xyz"

    body = CreateAgentBody(name="Code Reviewer", role="engineer")
    await create_agent(body, ctx)

    pair_mock.assert_awaited_once()
    kwargs = pair_mock.await_args.kwargs
    # Container key is owner_id (org_id for org members).
    assert kwargs["owner_id"] == "org_xyz"
    assert kwargs["paperclip_agent"]["id"] == pc_uuid
    assert kwargs["ip"] == "10.0.0.5"
    assert kwargs["token"] == "gw_token"


@pytest.mark.asyncio
async def test_teams_create_agent_uses_user_id_for_personal_owner(monkeypatch):
    """Personal user (no org_id) → owner_id == user_id."""
    pc_uuid = "11111111-1111-1111-1111-111111111111"

    pair_mock = AsyncMock()
    monkeypatch.setattr(
        "routers.teams.agents.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.teams.agents._container_is_reachable",
        AsyncMock(return_value=(True, "10.0.0.5", "gw_token")),
    )

    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": pc_uuid, "name": "Helper", "role": "engineer"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )
    monkeypatch.setattr(
        "routers.teams.agents._service_token_for",
        AsyncMock(return_value="svc_tok"),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    ctx = MagicMock()
    ctx.auth.user_id = "user_personal"
    ctx.auth.org_id = None
    ctx.company_id = "co_personal"

    await create_agent(CreateAgentBody(name="Helper", role="engineer"), ctx)

    assert pair_mock.await_args.kwargs["owner_id"] == "user_personal"


@pytest.mark.asyncio
async def test_teams_create_agent_skips_pairing_when_container_down(monkeypatch):
    """If the container isn't reachable, /teams create still succeeds —
    pairing is deferred to the next container provision (which will
    pick up the new agent via list_agents)."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    pair_mock = AsyncMock()
    monkeypatch.setattr(
        "routers.teams.agents.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.teams.agents._container_is_reachable",
        AsyncMock(return_value=(False, None, None)),
    )

    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )
    monkeypatch.setattr(
        "routers.teams.agents._service_token_for",
        AsyncMock(return_value="svc_tok"),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    ctx = MagicMock()
    ctx.auth.user_id = "user_member_b"
    ctx.auth.org_id = "org_xyz"
    ctx.company_id = "co_xyz"

    await create_agent(CreateAgentBody(name="Code Reviewer", role="engineer"), ctx)

    pair_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_create_agent_pairing_failure_is_non_fatal(monkeypatch, caplog):
    """If pair_with_openclaw raises, the route still returns the
    Paperclip agent (pairing is deferred). Failure is logged."""
    import logging

    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    pair_mock = AsyncMock(side_effect=RuntimeError("gateway timeout"))
    monkeypatch.setattr(
        "routers.teams.agents.pair_with_openclaw",
        pair_mock,
    )
    monkeypatch.setattr(
        "routers.teams.agents._container_is_reachable",
        AsyncMock(return_value=(True, "10.0.0.5", "gw_token")),
    )

    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )
    monkeypatch.setattr(
        "routers.teams.agents._service_token_for",
        AsyncMock(return_value="svc_tok"),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    ctx = MagicMock()
    ctx.auth.user_id = "user_a"
    ctx.auth.org_id = "org_xyz"
    ctx.company_id = "co_xyz"

    with caplog.at_level(logging.WARNING):
        # Should not raise — the route returns successfully.
        result = await create_agent(
            CreateAgentBody(name="Code Reviewer", role="engineer"),
            ctx,
        )
    assert result is not None
    # Warning was logged.
    assert any("pair_with_openclaw failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_container_is_reachable_uses_resolve_running_container(monkeypatch):
    """The helper now goes through ecs_manager.resolve_running_container
    (which uses ECS DescribeTasks) rather than reading a non-existent
    private_ip field from DDB."""
    from unittest.mock import AsyncMock, MagicMock

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=(
            {"owner_id": "user_x", "gateway_token": "gw_tok", "service_name": "svc"},
            "10.0.0.42",
        )
    )
    monkeypatch.setattr("routers.teams.agents.get_ecs_manager", lambda: fake_ecs)

    from routers.teams.agents import _container_is_reachable

    reachable, ip, token = await _container_is_reachable("user_x")
    assert reachable is True
    assert ip == "10.0.0.42"
    assert token == "gw_tok"
    fake_ecs.resolve_running_container.assert_awaited_once_with("user_x")


@pytest.mark.asyncio
async def test_container_is_reachable_returns_false_when_no_running_task(monkeypatch):
    """If resolve_running_container returns (None, None) — no row or
    not running — _container_is_reachable returns (False, None, None)."""
    from unittest.mock import AsyncMock, MagicMock

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    monkeypatch.setattr("routers.teams.agents.get_ecs_manager", lambda: fake_ecs)

    from routers.teams.agents import _container_is_reachable

    assert await _container_is_reachable("user_x") == (False, None, None)
