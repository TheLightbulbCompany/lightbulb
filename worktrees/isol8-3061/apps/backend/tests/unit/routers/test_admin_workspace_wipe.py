"""Tests for the admin Workspace-Wipe endpoints (org-keyed).

Endpoints under test:
  POST /api/v1/admin/workspaces/{org_id}/wipe/preview  body: {layers?, include_billing?}
  POST /api/v1/admin/workspaces/{org_id}/wipe           body: {confirm_org, layers?,
                                                              include_billing?, confirm_billing?,
                                                              preview_fingerprint}
  POST /api/v1/admin/users/{user_id}/wipe/workspace-resolve

The orchestrator (core/services/workspace_wipe.py) does NOT write the
admin-actions audit row — the execute endpoint does, with action
"account_wipe_workspace". Preview writes no audit row.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, get_current_user
from core.services.workspace_wipe import (
    StaleWorkspacePreviewError,
    WorkspaceConfirmMismatchError,
    WorkspaceWipeError,
    WorkspaceWipeReport,
    WipeStep,
)

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

ORG_ID = "org_test123"


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


@pytest.fixture
def mock_audit_repo():
    with patch(
        "core.repositories.admin_actions_repo.create",
        new=AsyncMock(return_value={}),
    ) as mock:
        yield mock


def _sample_report(*, dry_run: bool, failed: bool = False) -> WorkspaceWipeReport:
    steps = [
        WipeStep(system="runtime", action="delete_workspace_runtime", status="would_delete" if dry_run else "ok"),
        WipeStep(
            system="clerk_org",
            action="delete_organization",
            status="would_delete" if dry_run else ("failed" if failed else "ok"),
            error="boom" if failed else None,
        ),
    ]
    return WorkspaceWipeReport(
        owner_id=ORG_ID, steps=steps, dry_run=dry_run, fingerprint="fp123", layers=["runtime"], billing_preserved=True
    )


class TestWorkspaceWipePreview:
    @pytest.mark.asyncio
    async def test_happy_path_returns_report_and_writes_no_audit(self, async_client, admin_env, mock_audit_repo):
        report = _sample_report(dry_run=True)
        with patch("routers.admin.preview_workspace_wipe", new=AsyncMock(return_value=report)) as mock_preview:
            res = await async_client.post(
                f"/api/v1/admin/workspaces/{ORG_ID}/wipe/preview", json={"include_billing": False}
            )
        assert res.status_code == 200
        assert res.json()["fingerprint"] == "fp123"
        mock_preview.assert_awaited_once_with(ORG_ID, layers=None, include_billing=False, delete_shared_members=False)
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(f"/api/v1/admin/workspaces/{ORG_ID}/wipe/preview", json={})
        assert res.status_code == 403


class TestWorkspaceWipeExecute:
    def _body(self, **overrides):
        body = {"confirm_org": ORG_ID, "preview_fingerprint": "fp123"}
        body.update(overrides)
        return body

    @pytest.mark.asyncio
    async def test_matching_confirm_executes_and_audits_as_workspace_wipe(
        self, async_client, admin_env, mock_audit_repo
    ):
        report = _sample_report(dry_run=False)
        with patch("routers.admin.wipe_workspace", new=AsyncMock(return_value=report)) as mock_wipe:
            res = await async_client.post(f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body())
        assert res.status_code == 200
        assert res.json()["fingerprint"] == "fp123"
        mock_wipe.assert_awaited_once_with(
            ORG_ID,
            actor_admin_id="admin_1",
            confirm_org=ORG_ID,
            layers=None,
            include_billing=False,
            confirm_billing=None,
            expected_fingerprint="fp123",
            delete_shared_members=False,
        )
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "account_wipe_workspace"
        assert kwargs["target_user_id"] == ORG_ID
        assert kwargs["payload"]["fingerprint"] == "fp123"

    @pytest.mark.asyncio
    async def test_confirm_mismatch_400_and_no_wipe(self, async_client, admin_env, mock_audit_repo):
        with (
            patch(
                "routers.admin.wipe_workspace",
                new=AsyncMock(side_effect=WorkspaceConfirmMismatchError("nope")),
            ) as mock_wipe,
        ):
            res = await async_client.post(
                f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body(confirm_org="org_other")
            )
        assert res.status_code == 400
        assert res.json()["detail"].startswith("workspace_mismatch: ")
        mock_wipe.assert_awaited_once()
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_preview_409_and_no_audit(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.wipe_workspace",
            new=AsyncMock(side_effect=StaleWorkspacePreviewError("fp123", "fp456")),
        ):
            res = await async_client.post(f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body())
        assert res.status_code == 409
        assert res.json()["detail"].startswith("stale_preview: ")
        mock_audit_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_fingerprint_400_and_no_wipe(self, async_client, admin_env, mock_audit_repo):
        with patch("routers.admin.wipe_workspace", new=AsyncMock()) as mock_wipe:
            res = await async_client.post(
                f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body(preview_fingerprint="  ")
            )
        assert res.status_code == 400
        assert res.json()["detail"].startswith("workspace_wipe_refused: ")
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refusal_detail_is_code_then_message(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.wipe_workspace",
            new=AsyncMock(side_effect=WorkspaceWipeError("org_x is the vendor (Lightbulb) org; refusing.")),
        ):
            res = await async_client.post(
                f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body(delete_shared_members=True)
            )
        assert res.status_code == 400
        assert res.json()["detail"] == "workspace_wipe_refused: org_x is the vendor (Lightbulb) org; refusing."

    @pytest.mark.asyncio
    async def test_partial_failure_still_200_with_audit(self, async_client, admin_env, mock_audit_repo):
        report = _sample_report(dry_run=False, failed=True)
        with patch("routers.admin.wipe_workspace", new=AsyncMock(return_value=report)):
            res = await async_client.post(f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body())
        assert res.status_code == 200
        assert any(s["status"] == "failed" for s in res.json()["steps"])
        mock_audit_repo.assert_awaited_once()
        assert mock_audit_repo.await_args.kwargs["result"] == "partial_failure"

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(f"/api/v1/admin/workspaces/{ORG_ID}/wipe", json=self._body())
        assert res.status_code == 403


class TestWorkspaceResolve:
    @pytest.mark.asyncio
    async def test_resolves_single_workspace(self, async_client, admin_env):
        with patch("routers.admin.resolve_workspace_org", new=AsyncMock(return_value=ORG_ID)):
            res = await async_client.post("/api/v1/admin/users/user_x/wipe/workspace-resolve")
        assert res.status_code == 200
        assert res.json() == {"user_id": "user_x", "org_id": ORG_ID}
