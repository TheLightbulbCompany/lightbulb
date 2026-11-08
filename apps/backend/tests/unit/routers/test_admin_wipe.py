"""Tests for the admin Account-Wipe endpoints.

TDD-style: written BEFORE the endpoints existed (Task 4 of the
admin-account-wipe design, docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md).

Endpoints under test:
  POST /api/v1/admin/users/{owner_id}/wipe/preview   -> preview_wipe(owner_id).to_dict()
  POST /api/v1/admin/users/{owner_id}/wipe           body: {confirm_email} -> wipe_account(...).to_dict()

The orchestrator (core/services/account_wipe.py, Task 3) does NOT write the
admin-actions audit row -- the router does, on the execute endpoint only.
The email match is server-side (resolved Clerk-first via
resolve_account_emails), matched case-insensitively, never trusted from the
client body.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, get_current_user
from core.services.account_wipe import (
    OrgWipeUnsupportedError,
    WipeReport,
    WipeStep,
)

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors tests/unit/routers/test_admin_actions_writes.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(app):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="admin_1", email="admin@isol8.co")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def non_admin_env(app):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="user@example.com")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def shared_company():
    """Default: the target's Paperclip company is theirs alone (no workspace confirm)."""
    with patch("routers.admin.resolve_workspace_memberships", new=AsyncMock(return_value=[])) as mock:
        yield mock


@pytest.fixture
def mock_audit_repo():
    with patch(
        "core.repositories.admin_actions_repo.create",
        new=AsyncMock(return_value={}),
    ) as mock:
        yield mock


def _sample_report(*, dry_run: bool, failed: bool = False) -> WipeReport:
    steps = [
        WipeStep(system="paperclip_membership", action="archive_member", status="would_delete" if dry_run else "ok"),
        WipeStep(
            system="clerk_user",
            action="delete_user",
            status="would_delete" if dry_run else ("failed" if failed else "ok"),
            error="boom" if failed else None,
        ),
    ]
    return WipeReport(owner_id="user_target", steps=steps, dry_run=dry_run)


# ===========================================================================
# Preview
# ===========================================================================


class TestWipePreview:
    """POST /api/v1/admin/users/{owner_id}/wipe/preview."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_report_and_calls_no_delete(self, async_client, admin_env):
        report = _sample_report(dry_run=True)
        with patch(
            "routers.admin.preview_wipe",
            new=AsyncMock(return_value=report),
        ) as mock_preview:
            res = await async_client.post("/api/v1/admin/users/user_target/wipe/preview")
        assert res.status_code == 200
        body = res.json()
        assert body == report.to_dict()
        assert all(s["status"] in ("would_delete", "skipped") for s in body["steps"])
        mock_preview.assert_awaited_once_with("user_target")

    @pytest.mark.asyncio
    async def test_org_owner_returns_400(self, async_client, admin_env):
        with patch(
            "routers.admin.preview_wipe",
            new=AsyncMock(side_effect=OrgWipeUnsupportedError("org_target")),
        ):
            res = await async_client.post("/api/v1/admin/users/org_target/wipe/preview")
        assert res.status_code == 400
        assert res.json()["detail"] == "org_wipe_unsupported"

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/user_target/wipe/preview")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_preview_does_not_write_audit_row(self, async_client, admin_env, mock_audit_repo):
        report = _sample_report(dry_run=True)
        with patch("routers.admin.preview_wipe", new=AsyncMock(return_value=report)):
            res = await async_client.post("/api/v1/admin/users/user_target/wipe/preview")
        assert res.status_code == 200
        mock_audit_repo.assert_not_awaited()


# ===========================================================================
# Execute
# ===========================================================================


class TestWipeExecute:
    """POST /api/v1/admin/users/{owner_id}/wipe body: {confirm_email}."""

    @pytest.mark.asyncio
    async def test_matching_email_executes_and_writes_audit_row(self, async_client, admin_env, mock_audit_repo):
        report = _sample_report(dry_run=False)
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=report)) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "owner@example.com"},
            )
        assert res.status_code == 200
        assert res.json() == report.to_dict()
        mock_wipe.assert_awaited_once_with("user_target", actor_admin_id="admin_1")

        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "account_wipe"
        assert kwargs["admin_user_id"] == "admin_1"
        assert kwargs["target_user_id"] == "user_target"
        assert kwargs["payload"] == report.to_dict()

    @pytest.mark.asyncio
    async def test_mismatched_email_400_and_no_wipe(self, async_client, admin_env, mock_audit_repo):
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock()) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "wrong@example.com"},
            )
        assert res.status_code == 400
        assert res.json()["detail"] == "email_mismatch"
        mock_wipe.assert_not_awaited()
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_case_insensitive_email_match_executes(self, async_client, admin_env, mock_audit_repo):
        """Typed confirm differs only in case from a stored address -> matches
        and executes (the match is case-insensitive on both sides)."""
        report = _sample_report(dry_run=False)
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=report)) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "Owner@Example.com"},
            )
        assert res.status_code == 200
        mock_wipe.assert_awaited_once_with("user_target", actor_admin_id="admin_1")
        mock_audit_repo.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clerk_authoritative_email_executes_when_users_row_has_none(
        self, async_client, admin_env, mock_audit_repo
    ):
        """resolve_account_emails is Clerk-first: even when the users DDB row
        has no email, the Clerk-sourced address is authoritative for the match,
        so a wipe that would 400 under the old users-row-only lookup executes."""
        report = _sample_report(dry_run=False)
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["clerk-only@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=report)) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "clerk-only@example.com"},
            )
        assert res.status_code == 200
        mock_wipe.assert_awaited_once_with("user_target", actor_admin_id="admin_1")
        mock_audit_repo.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_account_email_400_and_no_wipe(self, async_client, admin_env, mock_audit_repo):
        """No email resolvable (Clerk stubbed + no users row) -- can never
        match, so refuse rather than silently allowing an empty confirm_email
        through."""
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=[])),
            patch("routers.admin.wipe_account", new=AsyncMock()) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": ""},
            )
        assert res.status_code == 400
        mock_wipe.assert_not_awaited()
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_org_owner_returns_400_before_email_or_wipe(self, async_client, admin_env, mock_audit_repo):
        """Org owners are refused up front by is_org_owner_id — before the email
        match and before wipe_account. This exercises the REAL org_-prefix path
        (no resolve_account_emails mock returning a matching email): Clerk has no
        record for an org id, so resolve_account_emails would return [] and,
        without the up-front check, the request would wrongly 400 email_mismatch
        instead of the documented org_wipe_unsupported."""
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock()) as mock_resolve,
            patch("routers.admin.wipe_account", new=AsyncMock()) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/org_target/wipe",
                json={"confirm_email": "anything@example.com"},
            )
        assert res.status_code == 400
        assert res.json()["detail"] == "org_wipe_unsupported"
        # Refused before either the email resolution or the teardown ran.
        mock_resolve.assert_not_awaited()
        mock_wipe.assert_not_awaited()
        # No audit row for a rejected wipe -- nothing was actually executed.
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(
            "/api/v1/admin/users/user_target/wipe",
            json={"confirm_email": "owner@example.com"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_confirm_email_422(self, async_client, admin_env):
        res = await async_client.post("/api/v1/admin/users/user_target/wipe", json={})
        assert res.status_code == 422

    @pytest.mark.parametrize("confirm_workspace", [None, "", "Other Co"])
    @pytest.mark.asyncio
    async def test_shared_company_without_the_workspace_name_400_and_no_wipe(
        self, async_client, admin_env, mock_audit_repo, shared_company, confirm_workspace
    ):
        """A member's company is the whole workspace's: the admin must name the
        workspace as well as the account, or nothing runs."""
        shared_company.return_value = [{"org_id": "org_founder", "workspace_name": "Isol8"}]
        body = {"confirm_email": "owner@example.com"}
        if confirm_workspace is not None:
            body["confirm_workspace"] = confirm_workspace
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock()) as mock_wipe,
        ):
            res = await async_client.post("/api/v1/admin/users/user_target/wipe", json=body)
        assert res.status_code == 400
        assert res.json()["detail"] == "workspace_mismatch"
        mock_wipe.assert_not_awaited()
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.parametrize(
        ("workspace_name", "typed"),
        [("Isol8", "  isol8 "), (None, "org_founder")],  # no Clerk name -> the org id is the phrase
    )
    @pytest.mark.asyncio
    async def test_shared_company_with_the_workspace_name_executes(
        self, async_client, admin_env, mock_audit_repo, shared_company, workspace_name, typed
    ):
        shared_company.return_value = [{"org_id": "org_founder", "workspace_name": workspace_name}]
        report = _sample_report(dry_run=False)
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=report)) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "owner@example.com", "confirm_workspace": typed},
            )
        assert res.status_code == 200
        shared_company.assert_awaited_once_with("user_target")
        mock_wipe.assert_awaited_once()
        mock_audit_repo.assert_awaited_once()

    @pytest.mark.parametrize("confirm_teardown", [[], ["Other Co"]])
    @pytest.mark.asyncio
    async def test_the_last_member_of_a_workspace_must_confirm_its_teardown(
        self, async_client, admin_env, mock_audit_repo, shared_company, confirm_teardown
    ):
        """Removing a workspace's last member cancels its subscription and deletes its runtime
        (#3115), so the admin names that workspace a second time or nothing runs."""
        shared_company.return_value = [{"org_id": "org_blue", "workspace_name": "Blue Caller", "last_member": True}]
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock()) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={
                    "confirm_email": "owner@example.com",
                    "confirm_workspace": "Blue Caller",
                    "confirm_teardown": confirm_teardown,
                },
            )
        assert res.status_code == 400 and res.json()["detail"] == "teardown_unconfirmed"
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_confirmed_teardown_executes(self, async_client, admin_env, mock_audit_repo, shared_company):
        shared_company.return_value = [{"org_id": "org_blue", "workspace_name": "Blue Caller", "last_member": True}]
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=_sample_report(dry_run=False))) as mock_wipe,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={
                    "confirm_email": "owner@example.com",
                    "confirm_workspace": "Blue Caller",
                    "confirm_teardown": ["  blue caller "],
                },
            )
        assert res.status_code == 200
        mock_wipe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partial_failure_report_still_returns_200_with_audit(self, async_client, admin_env, mock_audit_repo):
        """wipe_account is partial-failure-tolerant -- a failed step doesn't
        raise, so the endpoint still returns 200 with the report showing the
        failure, and still audits it."""
        report = _sample_report(dry_run=False, failed=True)
        with (
            patch("routers.admin.resolve_account_emails", new=AsyncMock(return_value=["owner@example.com"])),
            patch("routers.admin.wipe_account", new=AsyncMock(return_value=report)),
        ):
            res = await async_client.post(
                "/api/v1/admin/users/user_target/wipe",
                json={"confirm_email": "owner@example.com"},
            )
        assert res.status_code == 200
        body = res.json()
        assert any(s["status"] == "failed" for s in body["steps"])
        mock_audit_repo.assert_awaited_once()
