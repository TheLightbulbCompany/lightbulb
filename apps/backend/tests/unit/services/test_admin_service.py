"""Tests for admin_service composition layer (CEO C1, P1, E2, E3, S3).

Read-side composition. Mutation-side composition is tested at the
router layer in Phase C.
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---- list_users ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_joins_clerk_with_container_status():
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):
        return {
            "users": [
                {"id": "user_a", "email_addresses": [{"email_address": "a@x.com"}]},
                {"id": "user_b", "email_addresses": [{"email_address": "b@x.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_container_lookup(uid):
        return {"user_a": {"status": "running"}}.get(uid)

    # subscription_status lives on billing_accounts, not containers.
    async def fake_billing_lookup(uid):
        return {"user_a": {"subscription_status": "active"}}.get(uid)

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=fake_container_lookup),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=fake_billing_lookup),
    ):
        result = await admin_service.list_users(q="", limit=10)

    assert len(result["users"]) == 2
    a = next(r for r in result["users"] if r["clerk_id"] == "user_a")
    b = next(r for r in result["users"] if r["clerk_id"] == "user_b")
    assert a["email"] == "a@x.com"
    assert a["container_status"] == "running"
    assert a["subscription_status"] == "active"
    assert b["container_status"] == "none"  # no container → "none" sentinel
    assert b["subscription_status"] is None


@pytest.mark.asyncio
async def test_list_users_passes_through_stubbed_flag():
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):
        return {"users": [], "next_offset": None, "stubbed": True}

    with patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list):
        result = await admin_service.list_users()

    assert result["stubbed"] is True


@pytest.mark.asyncio
async def test_list_users_paginates_via_cursor():
    from core.services import admin_service

    captured_offset = {}

    async def fake_clerk_list(*, query, limit, offset):
        captured_offset["v"] = offset
        return {"users": [], "next_offset": None, "stubbed": False}

    with patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list):
        await admin_service.list_users(cursor="50", limit=25)

    assert captured_offset["v"] == 50


# ---- get_overview ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_overview_runs_sources_in_parallel():
    from core.services import admin_service

    with (
        patch("core.services.admin_service.clerk_admin.get_user", new=AsyncMock(return_value={"id": "u1"})),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"plan_tier": "starter"}),
        ),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={"tokens": 1000}),
        ),
    ):
        result = await admin_service.get_overview("u1")

    assert result["identity"]["id"] == "u1"
    assert result["container"]["status"] == "running"
    assert result["billing"]["plan_tier"] == "starter"
    assert result["usage"]["tokens"] == 1000


@pytest.mark.asyncio
async def test_get_overview_isolates_slow_upstream_via_timeout(monkeypatch):
    """CEO P1: a stuck upstream returns {error: timeout}, others succeed."""
    monkeypatch.setattr("core.services.admin_service._PARALLEL_TIMEOUT_S", 0.05)

    from core.services import admin_service

    async def slow_stripe(uid):
        await asyncio.sleep(1.0)
        return {"plan_tier": "should_not_be_seen"}

    with (
        patch("core.services.admin_service.clerk_admin.get_user", new=AsyncMock(return_value={"id": "u1"})),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=slow_stripe),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={"tokens": 1000}),
        ),
    ):
        result = await admin_service.get_overview("u1")

    assert result["identity"]["id"] == "u1"
    assert result["container"]["status"] == "running"
    assert result["billing"]["error"] == "timeout"
    assert result["usage"]["tokens"] == 1000


@pytest.mark.asyncio
async def test_get_overview_errors_dont_starve_other_sources():
    from core.services import admin_service

    async def failing_billing(uid):
        raise RuntimeError("ddb_blip")

    with (
        patch("core.services.admin_service.clerk_admin.get_user", new=AsyncMock(return_value={"id": "u1"})),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=failing_billing),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={"tokens": 1000}),
        ),
    ):
        result = await admin_service.get_overview("u1")

    assert result["billing"]["error"] == "ddb_blip"
    assert result["container"]["status"] == "running"


# ---- list_user_agents -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_user_agents_returns_stopped_when_container_not_running():
    """CEO E3: container_status surfaces, agents=[]."""
    from core.services import admin_service

    with patch(
        "core.services.admin_service.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "stopped"}),
    ):
        result = await admin_service.list_user_agents("u1")

    assert result["agents"] == []
    assert result["container_status"] == "stopped"


@pytest.mark.asyncio
async def test_list_user_agents_returns_none_when_no_container():
    from core.services import admin_service

    with patch(
        "core.services.admin_service.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        result = await admin_service.list_user_agents("u1")

    assert result["container_status"] == "none"


def _fake_pool(send_rpc_callable):
    """Build a fake gateway pool whose send_rpc is the given AsyncMock-like
    callable. Uses MagicMock instead of `type(...)` so the function isn't
    bound as a method and self isn't auto-injected."""
    from unittest.mock import MagicMock

    pool = MagicMock()
    pool.send_rpc = send_rpc_callable
    return pool


def _fake_ecs(container_token="t", ip="10.0.0.1"):
    from unittest.mock import MagicMock

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": container_token}, ip))
    return ecs


@pytest.mark.asyncio
async def test_list_user_agents_handles_gateway_timeout(monkeypatch):
    """CEO E2: gateway RPC > timeout → render container_status=timeout, no hang."""
    monkeypatch.setattr("core.services.admin_service._GATEWAY_RPC_TIMEOUT_S", 0.05)

    from core.services import admin_service

    async def slow_rpc(**kwargs):
        await asyncio.sleep(1.0)
        return {"agents": []}

    pool = _fake_pool(slow_rpc)
    ecs = _fake_ecs()

    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=pool),
    ):
        result = await admin_service.list_user_agents("u1")

    assert result["container_status"] == "timeout"
    assert result["error"] == "gateway_rpc_timeout"


@pytest.mark.asyncio
async def test_list_user_agents_returns_agents_on_running_container():
    from core.services import admin_service

    pool = _fake_pool(AsyncMock(return_value={"agents": [{"agent_id": "a", "name": "Agent A"}], "cursor": None}))
    ecs = _fake_ecs()

    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=pool),
    ):
        result = await admin_service.list_user_agents("u1")

    assert result["container_status"] == "running"
    assert len(result["agents"]) == 1
    assert result["agents"][0]["agent_id"] == "a"


# ---- get_agent_detail -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_detail_redacts_config_secrets():
    """CEO S3: openclaw.json secrets stripped before return."""
    from core.services import admin_service

    # RPC names match the main-app call sites (see get_agent_detail in
    # admin_service.py): agent.identity.get / sessions.list / skills.status /
    # config.get. sessions.list is unfiltered; the service filters by agentId
    # client-side — seed s1 with a matching agentId so it survives the filter.
    rpc_results = {
        "agent.identity.get": {"agent_id": "a", "name": "Agent A"},
        "sessions.list": {"sessions": [{"id": "s1", "agentId": "a"}]},
        "skills.status": {"skills": [{"id": "x"}]},
        "config.get": {"providers": {"anthropic_api_key": "sk-secret-shh"}},
    }

    async def fake_rpc(**kwargs):
        return rpc_results[kwargs["method"]]

    pool = _fake_pool(fake_rpc)
    ecs = _fake_ecs()

    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=pool),
    ):
        result = await admin_service.get_agent_detail("u1", "a")

    assert result["agent"]["agent_id"] == "a"
    assert result["sessions"][0]["id"] == "s1"
    assert result["config_redacted"]["providers"]["anthropic_api_key"] == "***redacted***"


@pytest.mark.asyncio
async def test_get_agent_detail_returns_error_when_container_not_running():
    from core.services import admin_service

    with patch(
        "core.services.admin_service.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "stopped"}),
    ):
        result = await admin_service.get_agent_detail("u1", "a")

    assert result["error"] == "container_not_running"
    assert result["container_status"] == "stopped"


# ---- pass-through delegations ---------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_delegates_to_cloudwatch_logs():
    from core.services import admin_service

    fake = AsyncMock(return_value={"events": [{"x": 1}], "cursor": None, "missing": False})
    with patch("core.services.admin_service.cloudwatch_logs.filter_user_logs", new=fake):
        result = await admin_service.get_logs("u1", level="ERROR", hours=12, limit=5)

    assert result["events"][0]["x"] == 1
    fake.assert_awaited_once_with(user_id="u1", level="ERROR", hours=12, limit=5, cursor=None)


def test_get_cloudwatch_url_delegates():
    from core.services import admin_service

    with patch(
        "core.services.admin_service.cloudwatch_url.build_insights_url",
        return_value="https://example.aws/url",
    ):
        url = admin_service.get_cloudwatch_url("u1", start="x", end="y", level="WARN")

    assert url == "https://example.aws/url"


@pytest.mark.asyncio
async def test_get_posthog_timeline_delegates():
    from core.services import admin_service

    fake = AsyncMock(return_value={"events": [], "stubbed": False, "missing": False, "error": None})
    with patch("core.services.admin_service.posthog_admin.get_person_events", new=fake):
        result = await admin_service.get_posthog_timeline("u1", limit=20)

    fake.assert_awaited_once_with(distinct_id="u1", limit=20)
    assert result["stubbed"] is False


@pytest.mark.asyncio
async def test_get_actions_audit_routes_to_target_query():
    from core.services import admin_service

    fake = AsyncMock(return_value={"items": [{"action": "x"}], "cursor": None})
    with patch("core.services.admin_service.admin_actions_repo.query_by_target", new=fake):
        result = await admin_service.get_actions_audit(target_user_id="t1", limit=10)

    fake.assert_awaited_once_with("t1", limit=10, cursor=None)
    assert result["items"][0]["action"] == "x"


@pytest.mark.asyncio
async def test_get_actions_audit_routes_to_admin_query():
    from core.services import admin_service

    fake = AsyncMock(return_value={"items": [], "cursor": None})
    with patch("core.services.admin_service.admin_actions_repo.query_by_admin", new=fake):
        await admin_service.get_actions_audit(admin_user_id="a1")

    fake.assert_awaited_once_with("a1", limit=50, cursor=None)


@pytest.mark.asyncio
async def test_get_actions_audit_requires_either_target_or_admin():
    from core.services import admin_service

    with pytest.raises(ValueError):
        await admin_service.get_actions_audit()


def test_current_image_tag_mirrors_match_openclaw_version_json():
    """admin_service.CURRENT_DEV_TAG / CURRENT_PROD_TAG must stay in sync
    with openclaw-version.json so the admin dropdown's "(current dev/prod)"
    badges reflect reality. Drift in either direction breaks CI. Same
    pattern as test_openclaw_upstream_version_matches_pinned_json in
    tests/unit/containers/test_config.py.
    """
    import json
    from pathlib import Path

    from core.services import admin_service

    # apps/backend/tests/unit/services/test_admin_service.py -> repo root is parents[5]
    repo_root = Path(__file__).resolve().parents[5]
    pinned = json.loads((repo_root / "openclaw-version.json").read_text())
    assert admin_service.CURRENT_DEV_TAG == pinned["dev"]["tag"], (
        f"admin_service.CURRENT_DEV_TAG ({admin_service.CURRENT_DEV_TAG!r}) "
        f"is out of sync with openclaw-version.json#dev.tag "
        f"({pinned['dev']['tag']!r}). Update both."
    )
    assert admin_service.CURRENT_PROD_TAG == pinned["prod"]["tag"], (
        f"admin_service.CURRENT_PROD_TAG ({admin_service.CURRENT_PROD_TAG!r}) "
        f"is out of sync with openclaw-version.json#prod.tag "
        f"({pinned['prod']['tag']!r}). Update both."
    )
