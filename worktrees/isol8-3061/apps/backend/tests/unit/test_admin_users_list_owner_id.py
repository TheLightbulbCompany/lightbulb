"""Tests for admin_service.list_users owner_id resolution.

Background: the DDB partition key ``owner_id`` equals Clerk ``org_id`` for
org-member resources and ``user_id`` for personal-mode resources. Without
correct resolution at ``/admin/users`` the list view falls back to
``container_repo.get_by_owner_id(user_id)`` using the raw Clerk id, so every
org member shows "no container" even when their org has one.

These tests pin the list-view behavior:

1. Two Clerk users in the same org → container_repo is called once for the
   org_id (dedupe by owner_id), not twice.
2. Personal-mode user → container_repo is called with the user_id unchanged.
3. Mixed page (1 org member + 1 personal) → both rows carry correct
   container_status; the org member row has ``org`` populated, the personal
   row has ``org: None``.
4. Clerk throws for one user → that row falls back to personal mode; other
   rows are unaffected.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


ORG_FIXTURE = {
    "id": "org_abc",
    "slug": "acme",
    "name": "Acme Co.",
    "role": "org:admin",
}


@pytest.mark.asyncio
async def test_list_users_dedupes_container_lookup_for_same_org_members():
    """Two Clerk users in the same org share one owner_id → one DDB call."""
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_alice", "email_addresses": [{"email_address": "alice@acme.com"}]},
                {"id": "user_bob", "email_addresses": [{"email_address": "bob@acme.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    # Both users are in the same org → both resolve to owner_id "org_abc".
    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        return [ORG_FIXTURE]

    container_mock = AsyncMock(return_value={"status": "running"})
    # plan_tier lives on billing_accounts, not containers.
    billing_mock = AsyncMock(return_value={"subscription_status": "active"})

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    # Exactly one container + one billing lookup for the shared org_id.
    assert container_mock.await_count == 1
    assert container_mock.await_args.args[0] == "org_abc"
    assert billing_mock.await_count == 1
    assert billing_mock.await_args.args[0] == "org_abc"

    # Both rows report the org's container status + plan tier.
    assert len(result["users"]) == 2
    for row in result["users"]:
        assert row["container_status"] == "running"
        assert row["subscription_status"] == "active"
        assert row["org"] == ORG_FIXTURE


@pytest.mark.asyncio
async def test_list_users_personal_mode_looks_up_container_by_user_id():
    """Personal-mode user → container lookup key equals user_id (unchanged)."""
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_solo", "email_addresses": [{"email_address": "solo@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        return []

    container_mock = AsyncMock(return_value={"status": "running"})
    billing_mock = AsyncMock(return_value={"subscription_status": "active"})

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    container_mock.assert_awaited_once_with("user_solo")
    billing_mock.assert_awaited_once_with("user_solo")
    assert len(result["users"]) == 1
    assert result["users"][0]["container_status"] == "running"
    assert result["users"][0]["subscription_status"] == "active"
    assert result["users"][0]["org"] is None


@pytest.mark.asyncio
async def test_list_users_mixed_page_resolves_each_row_correctly():
    """Mixed page: one org member + one personal user. Each row's
    container_status reflects its own owner_id; ``org`` is populated for the
    member and None for the personal user."""
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_member", "email_addresses": [{"email_address": "member@acme.com"}]},
                {"id": "user_solo", "email_addresses": [{"email_address": "solo@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        if user_id == "user_member":
            return [ORG_FIXTURE]
        return []

    async def fake_container_lookup(owner_id):
        return {
            "org_abc": {"status": "running"},
            "user_solo": {"status": "stopped"},
        }.get(owner_id)

    async def fake_billing_lookup(owner_id):
        return {
            "org_abc": {"subscription_status": "active"},
            "user_solo": {},
        }.get(owner_id)

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=fake_container_lookup),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=fake_billing_lookup),
    ):
        result = await admin_service.list_users()

    assert len(result["users"]) == 2
    member_row = next(r for r in result["users"] if r["clerk_id"] == "user_member")
    solo_row = next(r for r in result["users"] if r["clerk_id"] == "user_solo")

    assert member_row["container_status"] == "running"
    assert member_row["subscription_status"] == "active"
    assert member_row["org"] == ORG_FIXTURE

    assert solo_row["container_status"] == "stopped"
    assert solo_row["subscription_status"] is None
    assert solo_row["org"] is None


@pytest.mark.asyncio
async def test_list_users_falls_back_to_personal_mode_when_clerk_errors_for_one_user():
    """Clerk raising for one user in the page must not take the whole page
    down — that row falls back to personal mode (owner_id == user_id, org None)
    and other rows resolve normally.

    ``resolve_admin_owner_id`` already catches its own exceptions internally,
    but we still gather with ``return_exceptions=True`` as a defense-in-depth
    check. Here we simulate Clerk flakiness by returning a *different* org for
    one user and raising for the other — the raising row must land in
    personal-mode (user_id as owner, org=None), and the healthy row must still
    see the org_id-backed container.
    """
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_ok", "email_addresses": [{"email_address": "ok@acme.com"}]},
                {"id": "user_flaky", "email_addresses": [{"email_address": "flaky@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        if user_id == "user_flaky":
            raise RuntimeError("clerk 503")
        return [ORG_FIXTURE]

    async def fake_container_lookup(owner_id):
        return {
            "org_abc": {"status": "running"},
            "user_flaky": {"status": "stopped"},
        }.get(owner_id)

    async def fake_billing_lookup(owner_id):
        return {
            "org_abc": {"subscription_status": "active"},
            "user_flaky": {},
        }.get(owner_id)

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=fake_container_lookup),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=fake_billing_lookup),
    ):
        result = await admin_service.list_users()

    assert len(result["users"]) == 2
    ok_row = next(r for r in result["users"] if r["clerk_id"] == "user_ok")
    flaky_row = next(r for r in result["users"] if r["clerk_id"] == "user_flaky")

    # Healthy row picks up the org-scoped container.
    assert ok_row["container_status"] == "running"
    assert ok_row["org"] == ORG_FIXTURE

    # Flaky row fails open to personal mode: user_id used as owner_id, org None.
    assert flaky_row["container_status"] == "stopped"
    assert flaky_row["org"] is None


@pytest.mark.asyncio
async def test_list_users_reads_subscription_status_from_billing_not_container():
    """Regression: plan_tier lives on the billing_accounts row (see
    billing_repo.put_billing), NOT on the container row. Reading it from the
    container row causes every user in the list view to render as "free"
    tier regardless of actual subscription, because container rows never
    carry a plan_tier field.
    """
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_paying", "email_addresses": [{"email_address": "pay@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        return []

    # Container row is deliberately WITHOUT plan_tier — which matches reality,
    # since plan_tier is not a field on the containers table.
    container_mock = AsyncMock(return_value={"status": "running", "service_name": "openclaw-x"})
    # Billing row is the authoritative source for plan_tier.
    billing_mock = AsyncMock(return_value={"subscription_status": "active"})

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    assert len(result["users"]) == 1
    assert result["users"][0]["subscription_status"] == "active", (
        "plan_tier must come from billing_repo, not container_repo; "
        "reading from containers returns 'free' because containers never have plan_tier"
    )


@pytest.mark.asyncio
async def test_list_users_subscription_status_is_none_when_no_billing_row():
    """Users with no billing row (never subscribed, pre-provisioning, etc.)
    should render as 'free' — the sensible default. Guards against the fix
    regressing in the opposite direction (KeyError on missing billing row).
    """
    from core.services import admin_service

    async def fake_clerk_list(*, query, limit, offset):  # noqa: ARG001
        return {
            "users": [
                {"id": "user_new", "email_addresses": [{"email_address": "new@example.com"}]},
            ],
            "next_offset": None,
            "stubbed": False,
        }

    async def fake_list_orgs(user_id, *, limit=25):  # noqa: ARG001
        return []

    container_mock = AsyncMock(return_value=None)
    billing_mock = AsyncMock(return_value=None)  # no billing row

    with (
        patch("core.services.admin_service.clerk_admin.list_users", new=fake_clerk_list),
        patch("core.services.admin_service.clerk_admin.list_user_organizations", new=fake_list_orgs),
        patch("core.services.admin_service.container_repo.get_by_owner_id", new=container_mock),
        patch("core.services.admin_service.billing_repo.get_by_owner_id", new=billing_mock),
    ):
        result = await admin_service.list_users()

    assert result["users"][0]["subscription_status"] is None
    assert result["users"][0]["container_status"] == "none"
