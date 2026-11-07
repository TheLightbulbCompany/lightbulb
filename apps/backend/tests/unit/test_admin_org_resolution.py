"""Tests for admin_service org-aware owner_id resolution.

Background: the DDB partition key ``owner_id`` equals Clerk ``org_id`` for
org-member resources and ``user_id`` for personal-mode resources. The admin
dashboard receives the target user_id from the URL, so it must resolve the
effective owner_id via Clerk before querying container_repo / billing_repo /
usage_repo — otherwise org-member users render as "no container provisioned".

These tests pin:
- get_overview queries DDB with org_id (not user_id) when Clerk reports org
  membership, and returns org_context in the ``org`` response field.
- get_overview falls back to user_id when Clerk reports no orgs, and returns
  ``org: None``.
- get_overview is defensive: when Clerk itself errors, we still render the
  personal-mode payload (fail-open) rather than 500 the whole dashboard.

The second half of this file (``TestAdminMutationOwnerResolution``) covers
admin MUTATION endpoints — they must also resolve owner_id before
dispatching to downstream services. If only reads do, writes
(container stop/start/reprovision/resize, billing cancel/pause/credit/invoice,
PATCH /config, agent delete) still targeted the raw
``{user_id}`` from the URL — so for org members the mutations hit a
non-existent ``openclaw-{user_id}-{hash}`` ECS service instead of the real
``openclaw-{org_id}-{hash}``. Account ops (suspend/reactivate/force-signout/
resend-verification) target the Clerk user directly and MUST NOT resolve.
"""

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.mark.asyncio
async def test_get_overview_uses_org_id_as_owner_when_user_is_in_org():
    """User in an org → repos queried with org_id, response includes org."""
    from core.services import admin_service

    org = {
        "id": "org_abc",
        "slug": "acme",
        "name": "Acme Co.",
        "role": "org:admin",
    }

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_abc", "email_addresses": []}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "plan_tier": "pro"}),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"plan_tier": "pro"}),
        ) as mock_billing,
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={"total_spend_microdollars": 0}),
        ) as mock_usage,
    ):
        result = await admin_service.get_overview("user_abc")

    # Repos must be queried with the ORG id, not the USER id.
    mock_container.assert_awaited_once_with("org_abc")
    mock_billing.assert_awaited_once_with("org_abc")
    # usage_repo.get_period_usage takes (owner_id, period)
    assert mock_usage.await_args.args[0] == "org_abc"

    # Response carries org context.
    assert result["org"] == org
    assert result["identity"]["id"] == "user_abc"
    assert result["container"]["status"] == "running"


@pytest.mark.asyncio
async def test_get_overview_uses_user_id_when_personal_mode():
    """User in no orgs → repos queried with user_id, ``org`` is None."""
    from core.services import admin_service

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_solo"}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_billing,
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ) as mock_usage,
    ):
        result = await admin_service.get_overview("user_solo")

    mock_container.assert_awaited_once_with("user_solo")
    mock_billing.assert_awaited_once_with("user_solo")
    assert mock_usage.await_args.args[0] == "user_solo"

    assert result["org"] is None


@pytest.mark.asyncio
async def test_get_overview_falls_back_to_user_id_when_clerk_errors():
    """Clerk org lookup raising must NOT 500 the dashboard — fall back to
    personal-mode lookup so the admin can still see something."""
    from core.services import admin_service

    async def raising_clerk(user_id: str):  # noqa: ARG001
        raise RuntimeError("clerk 503")

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            side_effect=raising_clerk,
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await admin_service.get_overview("user_flaky")

    mock_container.assert_awaited_once_with("user_flaky")
    assert result["org"] is None


@pytest.mark.asyncio
async def test_get_overview_picks_first_org_when_clerk_returns_multiple():
    """project_single_org_per_user: one org per user. If Clerk returns
    multiple (shouldn't happen) we use the first and log a warning."""
    from core.services import admin_service

    orgs = [
        {"id": "org_first", "slug": "first", "name": "First", "role": "org:admin"},
        {"id": "org_second", "slug": "second", "name": "Second", "role": "org:member"},
    ]

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=orgs),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_multi"}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await admin_service.get_overview("user_multi")

    mock_container.assert_awaited_once_with("org_first")
    assert result["org"]["id"] == "org_first"


@pytest.mark.asyncio
async def test_list_user_agents_uses_org_id_for_container_lookup():
    """Agents-list container lookup must hit org_id when user is in an org."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
    ):
        result = await admin_service.list_user_agents("user_abc")

    mock_container.assert_awaited_once_with("org_abc")
    assert result["org"] == org
    assert result["container_status"] == "none"


@pytest.mark.asyncio
async def test_get_agent_detail_uses_org_id_for_container_lookup():
    """Agent detail container lookup must hit org_id when user is in an org."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "stopped"}),
        ) as mock_container,
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    mock_container.assert_awaited_once_with("org_abc")
    assert result["error"] == "container_not_running"
    assert result["org"] == org


@pytest.mark.asyncio
async def test_resolve_owner_falls_back_to_personal_mode_on_clerk_timeout(caplog):
    """P2: _resolve_owner_for_admin must bound the Clerk call with the same
    per-panel timeout budget. When Clerk is slow/unreachable, fall back to
    personal-mode (user_id, None) and log a warning — don't block the
    dashboard on upstream latency before _with_timeout-wrapped reads."""
    from core.services import admin_service

    async def never_returns(user_id: str):  # noqa: ARG001
        # Sleep far longer than the patched timeout to force asyncio.TimeoutError.
        await asyncio.sleep(10)
        return [{"id": "org_never"}]

    with (
        patch(
            "core.services.admin_service._PARALLEL_TIMEOUT_S",
            0.05,  # 50ms — enough time for wait_for to trip, too short for the sleep.
        ),
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            side_effect=never_returns,
        ),
        caplog.at_level(logging.WARNING, logger="core.services.admin_service"),
    ):
        owner_id, org_context = await admin_service._resolve_owner_for_admin("user_slow")

    assert owner_id == "user_slow"
    assert org_context is None
    # Warning must mention timeout so operators know Clerk is slow.
    assert any("timeout" in rec.message.lower() for rec in caplog.records)


# =============================================================================
# Admin MUTATION endpoints must resolve owner_id
# =============================================================================
#
# These tests drive the admin router (routers/admin.py) through FastAPI's
# TestClient and verify each mutation endpoint's internal dispatch switches
# from the raw URL path param to the Clerk-resolved owner_id when the target
# user is in an org. They use the same dependency_overrides pattern as
# tests/unit/routers/test_admin_actions_writes.py — the ``app`` and
# ``async_client`` fixtures from conftest.py provide a full FastAPI app with
# auth shimmed to an allowlisted admin.
#
# Org-mode fixtures mock ``clerk_admin.list_user_organizations`` to return a
# single org so ``resolve_admin_owner_id`` yields the org_id. Personal-mode
# tests return []. Account-ops tests assert the raw user_id still flows
# through (regression guard — suspending an org-member's account must ban
# the specific user, not the org).


ORG_FIXTURE = {
    "id": "org_abc",
    "slug": "acme",
    "name": "Acme Co.",
    "role": "org:admin",
}


@pytest.fixture
def admin_env(app):
    """Caller is on PLATFORM_ADMIN_USER_IDS — require_platform_admin admits them."""
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_platform_admin", email="admin@thelightbulb.company"
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def org_mode():
    """Patch resolve_admin_owner_id to return ('org_abc', ORG_FIXTURE).

    Patching at the router's import path (``routers.admin.resolve_admin_owner_id``)
    is enough — that's the symbol the mutation handlers call. We avoid
    patching ``clerk_admin.list_user_organizations`` directly so the test
    stays tight: a future refactor that moves resolution elsewhere still fails
    loudly if it stops calling the resolver.
    """
    with patch(
        "routers.admin.resolve_admin_owner_id",
        new=AsyncMock(return_value=("org_abc", ORG_FIXTURE)),
    ) as mock:
        yield mock


@pytest.fixture
def personal_mode():
    """Patch resolve_admin_owner_id to return ('user_123', None) — personal mode."""
    with patch(
        "routers.admin.resolve_admin_owner_id",
        new=AsyncMock(return_value=("user_123", None)),
    ) as mock:
        yield mock


class TestAdminMutationOwnerResolution:
    """Each admin mutation endpoint dispatches to the resolved owner_id, not
    the raw URL path param."""

    # -- Container mutations -------------------------------------------------
    #
    # One left: stop / start / resize went with the ECS runtime (a microVM has no desiredCount
    # and its size is the runtime spec).

    @pytest.mark.asyncio
    async def test_container_reprovision_uses_org_owner_id(self, async_client, admin_env, org_mode):
        """The whole point of this suite: an org member's workspace is keyed on the ORG, so a
        dispatch on the raw URL path param provisions a workspace that belongs to nobody."""
        with patch("routers.admin.dispatch_reconcile", new_callable=AsyncMock) as dispatch:
            res = await async_client.post("/api/v1/admin/users/user_123/container/reprovision")
        assert res.status_code == 200
        assert res.json()["owner_id"] == "org_abc"
        dispatch.assert_awaited_once_with("org_abc")
        org_mode.assert_awaited_once_with("user_123")

    @pytest.mark.asyncio
    async def test_container_reprovision_personal_mode_uses_user_id(self, async_client, admin_env, personal_mode):
        """Negative test: a caller with no org resolves to the Clerk user_id unchanged."""
        with patch("routers.admin.dispatch_reconcile", new_callable=AsyncMock) as dispatch:
            res = await async_client.post("/api/v1/admin/users/user_123/container/reprovision")
        assert res.status_code == 200
        dispatch.assert_awaited_once_with("user_123")

    # -- Billing mutations ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_billing_cancel_subscription_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.cancel_subscription_for_owner = AsyncMock(return_value={"status": "canceled"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/user_123/billing/cancel-subscription")
        assert res.status_code == 200
        billing.cancel_subscription_for_owner.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_billing_pause_subscription_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.pause_subscription_for_owner = AsyncMock(return_value={"status": "paused"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/user_123/billing/pause-subscription")
        assert res.status_code == 200
        billing.pause_subscription_for_owner.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_billing_issue_credit_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock(return_value={"credit_id": "cn_1"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/user_123/billing/issue-credit",
                json={"amount_cents": 500, "reason": "incident_comp", "request_id": "submit-aaaa"},
            )
        assert res.status_code == 200
        billing.issue_credit_for_owner.assert_awaited_once_with(
            "org_abc",
            amount_cents=500,
            reason="incident_comp",
            request_id="submit-aaaa",
        )

    @pytest.mark.asyncio
    async def test_billing_mark_invoice_resolved_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.mark_invoice_resolved = AsyncMock(return_value={"status": "resolved"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/user_123/billing/mark-invoice-resolved",
                json={"invoice_id": "in_1"},
            )
        assert res.status_code == 200
        billing.mark_invoice_resolved.assert_awaited_once_with("org_abc", "in_1")

    # -- Config + agent mutations -------------------------------------------

    @pytest.mark.asyncio
    async def test_config_patch_uses_org_owner_id(self, async_client, admin_env, org_mode):
        with patch("routers.admin.config_patch_roster", new=AsyncMock(return_value={"hash": "h"})) as mock_patch:
            res = await async_client.patch(
                "/api/v1/admin/users/user_123/config",
                json={"patch": {"agents": {"defaults": {}}}},
            )
        assert res.status_code == 200
        mock_patch.assert_awaited_once()
        # The org owner id, not the {user_id} in the path.
        assert mock_patch.await_args.args[0] == "org_abc"

    @pytest.mark.asyncio
    async def test_agent_delete_uses_org_owner_id(self, async_client, admin_env, org_mode):
        delete_everywhere = AsyncMock(return_value={"deleted": True})
        with patch(
            "core.services.agent_lifecycle.delete_agent_everywhere",
            new=delete_everywhere,
        ):
            res = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")

        assert res.status_code == 200
        delete_everywhere.assert_awaited_once_with(owner_id="org_abc", agent_id="agt_42")

    @pytest.mark.asyncio
    async def test_agent_delete_delegates_each_call_to_lifecycle(self, async_client, admin_env, org_mode):
        delete_everywhere = AsyncMock(return_value={"deleted": True})
        with patch(
            "core.services.agent_lifecycle.delete_agent_everywhere",
            new=delete_everywhere,
        ):
            r1 = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")
            r2 = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")

        assert r1.status_code == r2.status_code == 200
        assert delete_everywhere.await_count == 2
        assert all(
            call.kwargs == {"owner_id": "org_abc", "agent_id": "agt_42"} for call in delete_everywhere.await_args_list
        )

    # -- Account mutations MUST NOT resolve (regression guard) ---------------

    @pytest.mark.asyncio
    async def test_account_suspend_uses_raw_user_id_not_org(self, async_client, admin_env):
        """Regression guard: account/* endpoints target the Clerk user
        directly, not the org. If these ever start calling
        resolve_admin_owner_id an admin would end up banning "org_abc" (which
        Clerk would 404) instead of the actual misbehaving user.

        We patch resolve_admin_owner_id so, if the handler DID call it, we'd
        see ``org_abc`` land in the ban payload and fail the assertion.
        """
        with (
            patch(
                "routers.admin.resolve_admin_owner_id",
                new=AsyncMock(return_value=("org_abc", ORG_FIXTURE)),
            ) as mock_resolve,
            patch(
                "routers.admin.clerk_admin.ban_user",
                new=AsyncMock(return_value={"banned": True}),
            ) as mock_ban,
        ):
            res = await async_client.post("/api/v1/admin/users/user_123/account/suspend")

        assert res.status_code == 200
        mock_ban.assert_awaited_once_with("user_123")
        mock_resolve.assert_not_awaited()
