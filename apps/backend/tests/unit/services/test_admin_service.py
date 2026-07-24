"""Tests for admin_service composition layer (CEO C1, P1, E2, E3, S3).

Read-side composition. Mutation-side composition is tested at the
router layer in Phase C.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_get_overview_reads_current_month_usage_period():
    """usage_repo rows only exist as YYYY-MM / lifetime / member:* keys — the
    literal string "current" is never written, so the read must use the
    actual current UTC month."""
    from core.services import admin_service

    now = datetime.now(timezone.utc)
    expected_period = f"{now.year}-{now.month:02d}"

    usage_mock = AsyncMock(return_value={"tokens": 1000})

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
        patch("core.services.admin_service.usage_repo.get_period_usage", new=usage_mock),
    ):
        await admin_service.get_overview("u1")

    usage_mock.assert_awaited_with("u1", expected_period)


# ---- get_provisioning -------------------------------------------------------


def _paperclip_repo_returning(company) -> MagicMock:
    """A fake ``_paperclip_repo()`` return value whose ``.get`` resolves to
    ``company`` (a PaperclipCompany instance or None)."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=company)
    return repo


@pytest.mark.asyncio
async def test_get_provisioning_narrows_and_excludes_secrets():
    """CEO admin-provisioning-tab: secrets on both the containers row
    (gateway_token) and the paperclip-companies row
    (paperclip_password_encrypted / service_token_encrypted) must never
    reach the composed payload, even though both source rows carry them."""
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services import admin_service

    now = datetime.now(timezone.utc)
    container_row = {
        "owner_id": "owner_1",
        "status": "error",
        "substatus": "provision_failed",
        "last_error": "boom",
        "last_error_at": now.isoformat(),
        "retryable": True,
        "gateway_token": "SECRET-GW",
        "service_name": "openclaw-owner_1-abc123",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    paperclip_company = PaperclipCompany(
        user_id="user_1",
        org_id="",
        company_id="company_1",
        paperclip_user_id="pc_user_1",
        paperclip_password_encrypted="SECRET-PW",
        service_token_encrypted="SECRET-ST",
        status="active",
        created_at=now,
        updated_at=now,
        funnel_state="submitted",
        bootstrap_state="failed",
    )

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=container_row),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "error"}),
        ),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(paperclip_company),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.describe_dispatch_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")

    flat = json.dumps(result)
    assert "SECRET-GW" not in flat
    assert "SECRET-PW" not in flat
    assert "SECRET-ST" not in flat
    assert result["container"]["status"] == "error"
    assert result["container"]["last_error"] == "boom"
    assert result["container"]["claim_stale"] in (True, False)
    assert result["paperclip"]["funnel_state"] == "submitted"
    assert result["paperclip"]["bootstrap_state"] == "failed"


@pytest.mark.asyncio
async def test_get_provisioning_no_rows_degrades_to_nones():
    """A brand-new owner with no containers row, no paperclip-companies row,
    no gate block, and no SFN execution must still answer 200 with every
    optional panel null — not raise, not 500."""
    from core.services import admin_service

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "needs_signup"}),
        ),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.describe_dispatch_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")

    assert result["container"] is None
    assert result["paperclip"] is None
    assert result["gate"] is None
    assert result["sfn_execution"] is None
    assert result["status_contract"] is not None  # composer answers even with no row


@pytest.mark.asyncio
async def test_get_provisioning_container_read_failure_isolated_from_compute_status():
    """A failing/timed-out container_repo.get_by_owner_id flows through
    _with_timeout as an {"error", "source"} sentinel -- not None, and not a
    real row. compute_status's container= contract (readiness_state.py:
    169-176) only anticipates an already-fetched row or an explicit None;
    threading the sentinel through as if it were a real row would silently
    fabricate a plausible-but-wrong lifecycle instead of surfacing that the
    read itself failed. The composer must (a) pass compute_status
    container=None on a failed read, and (b) surface the read failure in the
    `container` field distinguishably from "row genuinely absent" (which is
    None) so an operator can tell the two apart."""
    from core.services import admin_service

    compute_status_mock = AsyncMock(return_value={"lifecycle": "needs_signup"})

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("ddb_blip")),
        ),
        patch("core.services.admin_service.compute_status", new=compute_status_mock),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.describe_dispatch_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")  # must not raise

    compute_status_mock.assert_awaited_once_with("owner_1", container=None)
    assert result["container"] == {"error": "ddb_blip", "source": "ddb_containers"}
    # Distinguishable from the genuinely-no-row case, which is bare None —
    # asserted separately by test_get_provisioning_no_rows_degrades_to_nones.
    assert result["container"] is not None


@pytest.mark.asyncio
async def test_get_provisioning_narrows_sfn_response():
    """The SFN DescribeExecution response is boto3-shaped (datetimes +
    ResponseMetadata); only name/status/start_date/stop_date should survive,
    with dates ISO-serialized and ResponseMetadata dropped."""
    from core.services import admin_service

    container_row = {
        "status": "provisioning",
        "provision_claim_id": "claim-123",
        "provision_execution_name": "provision-owner_1-claim123",
    }
    start = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    stop = datetime(2026, 7, 24, 12, 5, 0, tzinfo=timezone.utc)
    sfn_response = {
        "executionArn": "arn:aws:states:us-east-1:123456789012:execution:x:y",
        "name": "provision-owner_1-claim123",
        "status": "RUNNING",
        "startDate": start,
        "stopDate": stop,
        "input": "{}",
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    describe_mock = AsyncMock(return_value=sfn_response)

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=container_row),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "provisioning"}),
        ),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.describe_dispatch_execution", new=describe_mock),
    ):
        result = await admin_service.get_provisioning("user_1")

    describe_mock.assert_awaited_once_with("owner_1", "claim-123")
    assert set(result["sfn_execution"]) == {"name", "status", "start_date", "stop_date"}
    assert result["sfn_execution"]["name"] == "provision-owner_1-claim123"
    assert result["sfn_execution"]["status"] == "RUNNING"
    assert result["sfn_execution"]["start_date"] == start.isoformat()
    assert result["sfn_execution"]["stop_date"] == stop.isoformat()


@pytest.mark.asyncio
async def test_get_provisioning_gate_payload_shape():
    """A blocked gate narrows to exactly Gate.to_payload()["blocked"] — the
    same structured shape /container/status already returns on 402, so the
    admin tab and the owner-facing blocked card render identically."""
    from core.services import admin_service
    from core.services.provision_gate import Gate

    gate = Gate(
        code="subscription_required",
        title="Subscription not active",
        message="Reactivate your subscription to start your container.",
        action_label="Manage subscription",
        action_href="/settings/billing",
        action_admin_only=True,
        owner_role="admin",
    )

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "blocked"}),
        ),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=gate)),
        patch("core.services.admin_service.describe_dispatch_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")

    assert result["gate"]["code"] == "subscription_required"
    assert result["gate"] == gate.to_payload()["blocked"]


@pytest.mark.asyncio
async def test_get_provisioning_gate_probe_failure_degrades_to_none_not_crash():
    """A failing/timed-out probe_provision_gate flows through _with_timeout as
    an {"error", "source"} dict, not None or a Gate — asserting `if gate` would
    treat that dict as truthy and crash calling .to_payload() on it. The
    composer must degrade to gate=None instead of raising, same as any other
    degraded upstream in this composition."""
    from core.services import admin_service

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("owner_1", None)),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "needs_signup"}),
        ),
        patch(
            "core.services.admin_service._paperclip_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch(
            "core.services.admin_service.probe_provision_gate",
            new=AsyncMock(side_effect=RuntimeError("ddb_blip")),
        ),
        patch("core.services.admin_service.describe_dispatch_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")  # must not raise

    assert result["gate"] is None


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


def test_current_image_tags_resolve_from_single_source():
    """admin_service.CURRENT_DEV_TAG / CURRENT_PROD_TAG are resolved from the
    single source of truth via core.openclaw_version, so they reflect
    openclaw-version.json without a hand-mirrored constant. In CI (no env vars
    set) they fall back to the repo JSON, so they must equal the pinned values.
    """
    import json
    from pathlib import Path

    from core.services import admin_service

    # apps/backend/tests/unit/services/test_admin_service.py -> repo root is parents[5]
    repo_root = Path(__file__).resolve().parents[5]
    pinned = json.loads((repo_root / "openclaw-version.json").read_text())
    assert admin_service.CURRENT_DEV_TAG == pinned["dev"]["tag"]
    assert admin_service.CURRENT_PROD_TAG == pinned["prod"]["tag"]
