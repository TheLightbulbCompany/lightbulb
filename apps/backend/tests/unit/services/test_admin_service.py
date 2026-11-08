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
async def test_get_overview_never_returns_container_tokens():
    from core.services import admin_service

    row = {"status": "running", "hook_token": "h-secret", "gateway_token": "g-secret"}
    with (
        patch("core.services.admin_service.clerk_admin.get_user", new=AsyncMock(return_value={"id": "u1"})),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.usage_repo.get_period_usage", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_overview("u1")

    assert result["container"]["status"] == "running"
    assert "token" not in str(result["container"])


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
    """A fake ``_workspace_repo()`` resolving to ``company``."""
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=company)
    return repo


@pytest.mark.asyncio
async def test_get_provisioning_narrows_and_excludes_secrets():
    """CEO admin-provisioning-tab: the containers row's gateway_token must
    never reach the composed payload, and the paperclip panel is narrowed
    through an ALLOWLIST — an attribute outside it (here a credential stuck
    onto the row object) is excluded by default, not leaked by default."""
    from core.repositories.workspace_repo import Workspace
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
    paperclip_company = Workspace(
        org_id="owner_1",
        company_id="company_1",
        status="active",
        created_at=now,
        updated_at=now,
        provisioner_user_id="user_1",
        member_user_ids={"user_2", "user_1"},
        funnel_state="submitted",
        starter_agent_ids={"ceo": "agent-ceo"},
    )
    paperclip_company.paperclip_password_encrypted = "SECRET-PW"
    paperclip_company.service_token_encrypted = "SECRET-ST"

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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(paperclip_company),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")

    flat = json.dumps(result)
    assert "SECRET-GW" not in flat
    assert "SECRET-PW" not in flat
    assert "SECRET-ST" not in flat
    assert result["container"]["status"] == "error"
    assert result["container"]["last_error"] == "boom"
    assert "claim_stale" not in result["container"]
    assert result["paperclip"]["funnel_state"] == "submitted"
    assert result["paperclip"]["starter_agent_ids"] == {"ceo": "agent-ceo"}
    assert result["paperclip"]["provisioner_user_id"] == "user_1"
    # A set is not JSON — the wire carries a sorted list.
    assert result["paperclip"]["member_user_ids"] == ["user_1", "user_2"]
    assert "paperclip_user_id" not in result["paperclip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "provisioning"])
async def test_get_provisioning_includes_inactive_authoritative_org_row(status):
    """Diagnostics retain failure details while the org row is not active."""
    from core.repositories.workspace_repo import Workspace
    from core.services import admin_service

    now = datetime.now(timezone.utc)
    paperclip_company = Workspace(
        org_id="org_acme",
        company_id="company_1",
        provisioner_user_id="owner_user",
        status=status,
        created_at=now,
        updated_at=now,
        last_error="starter seed incomplete",
        starter_agent_ids={"ceo": "agent-ceo"},
    )
    repo = MagicMock()

    async def resolve_owner_row(owner_id, *, include_inactive=False):
        return paperclip_company if include_inactive else None

    repo.resolve_owner_row = AsyncMock(side_effect=resolve_owner_row)

    with (
        patch(
            "core.services.admin_service.resolve_admin_owner_id",
            new=AsyncMock(return_value=("org_acme", {"id": "org_acme"})),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.compute_status",
            new=AsyncMock(return_value={"lifecycle": "provisioning"}),
        ),
        patch("core.services.admin_service._workspace_repo", return_value=repo),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("admin_user")

    assert result["paperclip"]["status"] == status
    assert result["paperclip"]["last_error"] == "starter seed incomplete"
    assert result["paperclip"]["starter_agent_ids"] == {"ceo": "agent-ceo"}


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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")  # must not raise

    compute_status_mock.assert_awaited_once_with("owner_1", container=None)
    assert result["container"] == {"error": "ddb_blip", "source": "ddb_containers"}
    # Distinguishable from the genuinely-no-row case, which is bare None —
    # asserted separately by test_get_provisioning_no_rows_degrades_to_nones.
    assert result["container"] is not None


@pytest.mark.asyncio
async def test_get_provisioning_paperclip_read_failure_not_mistaken_for_empty_row():
    """A failing/timed-out owner-row lookup flows through _with_timeout
    as an {"error", "source"} sentinel -- not None, and not a real
    Workspace. _narrow_paperclip only special-cases `company is None`;
    threading the sentinel through it unnarrowed would silently fabricate a
    present-but-all-None paperclip row (every allowlisted field None) instead
    of surfacing that the read itself failed -- the admin UI would then
    wrongly assert a Paperclip company row exists during a degraded-read
    window. The composer must surface the raw sentinel in the `paperclip`
    field, distinguishable from "no row" (which is None)."""
    from core.services import admin_service

    failing_repo = MagicMock()
    failing_repo.resolve_owner_row = AsyncMock(side_effect=RuntimeError("ddb_blip"))

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
            "core.services.admin_service._workspace_repo",
            return_value=failing_repo,
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
    ):
        result = await admin_service.get_provisioning("user_1")  # must not raise

    assert result["paperclip"] == {"error": "ddb_blip", "source": "ddb_paperclip"}
    # Distinguishable from the genuinely-no-row case, which is bare None —
    # asserted separately by test_get_provisioning_no_rows_degrades_to_nones.
    assert result["paperclip"] is not None


@pytest.mark.asyncio
async def test_get_provisioning_sfn_read_failure_not_mistaken_for_no_execution():
    """A failing/timed-out running_execution flows through
    _with_timeout as an {"error", "source"} sentinel -- not None, and not a
    real ListExecutions item. _narrow_execution only special-cases a
    falsy `resp`; threading the sentinel through it unnarrowed would silently
    fabricate a present-but-all-None execution row instead of surfacing that
    the read itself failed. The composer must surface the raw sentinel in the
    `sfn_execution` field, distinguishable from "no execution stamped" (which
    is None)."""
    from core.services import admin_service

    container_row = {
        "status": "provisioning",
    }

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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.admin_service.running_execution",
            new=AsyncMock(side_effect=RuntimeError("sfn_blip")),
        ),
    ):
        result = await admin_service.get_provisioning("user_1")  # must not raise

    assert result["sfn_execution"] == {"error": "sfn_blip", "source": "sfn_execution"}
    # Distinguishable from the genuinely-no-execution case, which is bare
    # None — asserted separately by test_get_provisioning_no_rows_degrades_to_nones.
    assert result["sfn_execution"] is not None


@pytest.mark.asyncio
async def test_get_provisioning_narrows_sfn_response():
    """The SFN ListExecutions item is boto3-shaped (datetimes); only
    name/status/start_date/stop_date should survive, with dates ISO-serialized."""
    from core.services import admin_service

    container_row = {
        "status": "provisioning",
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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.admin_service.running_execution", new=describe_mock),
    ):
        result = await admin_service.get_provisioning("user_1")

    describe_mock.assert_awaited_once_with("owner_1")
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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch("core.services.admin_service.probe_provision_gate", new=AsyncMock(return_value=gate)),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
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
            "core.services.admin_service._workspace_repo",
            return_value=_paperclip_repo_returning(None),
        ),
        patch(
            "core.services.admin_service.probe_provision_gate",
            new=AsyncMock(side_effect=RuntimeError("ddb_blip")),
        ),
        patch("core.services.admin_service.running_execution", new=AsyncMock(return_value=None)),
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


@pytest.mark.asyncio
async def test_list_user_agents_answers_from_the_roster_never_the_runtime():
    """Parity audit row 80: an invoke wakes a parked microVM (90s RPC floor) -- admin
    inspection must answer from the Paperclip roster the backend already owns. The module
    imports no RPC client at all now, which is what keeps that true."""
    from core.services import admin_service

    # The prose still explains WHY; the assertion is that no RPC client is imported.
    assert not hasattr(admin_service, "container_rpc"), "admin inspection must never reach the runtime"

    roster_listing = AsyncMock(
        return_value={
            "defaultId": "a1",
            "agents": [{"id": "a1", "name": "Agent A", "role": "ceo", "model": "m1", "modelName": "Sonnet"}],
        }
    )
    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "runtime_host": "agentcore"}),
        ),
        patch("core.services.admin_service.runtime_rpc.agentcore_agents_list", roster_listing),
    ):
        result = await admin_service.list_user_agents("u1")

    assert result["container_status"] == "running"
    assert result["agents"] == [{"id": "a1", "name": "Agent A", "role": "ceo", "model": "m1", "modelName": "Sonnet"}]
    roster_listing.assert_awaited_once_with("u1")


# ---- get_agent_detail -----------------------------------------------------


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


@pytest.mark.asyncio
async def test_get_agent_detail_answers_from_the_roster_and_the_profile():
    """Parity audit row 80: identity comes from the roster/model listing, config from the
    agent's own Hermes profile in S3. Sessions/skills come back empty rather than waking a
    parked microVM to enumerate them."""
    from core.services import admin_service

    roster_listing = AsyncMock(
        return_value={
            "defaultId": "a1",
            "agents": [{"id": "a1", "name": "Agent A", "role": "ceo", "model": "m1", "modelName": "Sonnet"}],
        }
    )
    config_snapshot = MagicMock(return_value=({"providers": {"anthropic_api_key": "sk-secret-shh"}}, "hash123"))

    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "runtime_host": "agentcore"}),
        ),
        patch("core.services.admin_service.runtime_rpc.agentcore_agents_list", roster_listing),
        patch("core.services.admin_service.hermes_profiles.config_snapshot", config_snapshot),
    ):
        result = await admin_service.get_agent_detail("u1", "a1")

    assert result["agent"] == {"id": "a1", "name": "Agent A", "role": "ceo", "model": "m1", "modelName": "Sonnet"}
    assert result["sessions"] == []
    assert result["skills"] == []
    assert result["config_redacted"]["providers"]["anthropic_api_key"] == "***redacted***"
    config_snapshot.assert_called_once_with("u1", "a1")


@pytest.mark.asyncio
async def test_get_agent_detail_agentcore_owner_unknown_agent_id():
    from core.services import admin_service

    roster_listing = AsyncMock(return_value={"defaultId": "a1", "agents": [{"id": "a1", "name": "Agent A"}]})

    with (
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "runtime_host": "agentcore"}),
        ),
        patch("core.services.admin_service.runtime_rpc.agentcore_agents_list", roster_listing),
    ):
        result = await admin_service.get_agent_detail("u1", "does-not-exist")

    assert result["error"] == "agent_not_found"


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
async def test_get_actions_audit_no_filter_scans_recent(monkeypatch):
    """No filter is the platform-wide audit view: it now scans recent rows
    newest-first instead of raising (the old ValueError was swallowed to a
    400 and rendered as "No admin actions recorded" while ~348 rows existed)."""
    from core.repositories import admin_actions_repo
    from core.services import admin_service

    async def fake_scan_recent(limit=50, cursor=None):
        return {"items": [{"action_id": "a1"}], "cursor": None}

    monkeypatch.setattr(admin_actions_repo, "scan_recent", fake_scan_recent)
    result = await admin_service.get_actions_audit()
    assert result["items"] == [{"action_id": "a1"}]
