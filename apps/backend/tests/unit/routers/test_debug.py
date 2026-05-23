"""Unit tests for routers/debug.py — dev/test-only debug endpoints."""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest


class TestDeleteUserData:
    """Test DELETE /api/v1/debug/user-data — atomic per-owner teardown."""

    @pytest.mark.asyncio
    @patch("routers.debug.container_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.get_workspace")
    async def test_full_teardown_returns_summary(
        self,
        mock_workspace,
        mock_ecs_mgr,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_apikey_repo,
        mock_container_repo,
        async_client,
    ):
        """Endpoint returns deleted summary and calls each subsystem teardown."""
        mock_container = {
            "owner_id": "user_test_123",
            "service_name": "openclaw-user_test_123-abc",
            "access_point_id": "fsap-abc",
            "task_definition_arn": "arn:aws:ecs:...:task-definition/foo:5",
        }
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=mock_container)
        mock_container_repo.delete = AsyncMock()
        mock_apikey_repo.delete_all_for_owner = AsyncMock(return_value=2)
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=1)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=4)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=3)

        ecs_mgr = MagicMock()
        ecs_mgr.delete_user_service = AsyncMock()
        ecs_mgr._deregister_task_definition = MagicMock()
        mock_ecs_mgr.return_value = ecs_mgr

        ws = MagicMock()
        ws.delete_user_dir = MagicMock()
        mock_workspace.return_value = ws

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        body = res.json()
        assert body["deleted"]["ecs"] is True
        assert body["deleted"]["efs"] is True
        assert "users" in body["deleted"]["ddb"]
        assert "containers" in body["deleted"]["ddb"]
        assert "billing-accounts" in body["deleted"]["ddb"]
        assert "api-keys" in body["deleted"]["ddb"]
        assert "usage-counters" in body["deleted"]["ddb"]
        assert "pending-updates" in body["deleted"]["ddb"]
        assert "channel-links" in body["deleted"]["ddb"]
        assert "ws-connections" in body["deleted"]["ddb"]

        ecs_mgr.delete_user_service.assert_called_once_with("user_test_123")
        ws.delete_user_dir.assert_called_once_with("user_test_123")
        mock_user_repo.delete.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.debug.settings")
    async def test_endpoint_disabled_in_prod(self, mock_settings, async_client):
        """Endpoint returns 403 when ENVIRONMENT == 'prod' (existing /debug guard)."""
        mock_settings.ENVIRONMENT = "prod"
        res = await async_client.delete("/api/v1/debug/user-data")
        assert res.status_code == 403

    @pytest.mark.asyncio
    @patch("routers.debug.container_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.get_workspace")
    async def test_org_context_uses_user_id_for_user_scoped_tables(
        self,
        mock_workspace,
        mock_ecs_mgr,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_apikey_repo,
        mock_container_repo,
        app,
        mock_org_admin_user,
    ):
        """In an org context, user_repo + connection_service must use the
        Clerk user_id, while owner-scoped tables (container, billing, etc.)
        use the org_id from resolve_owner_id."""
        from httpx import AsyncClient, ASGITransport
        from core.auth import get_current_user

        mock_container_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_container_repo.delete = AsyncMock()
        mock_apikey_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)

        ws = MagicMock()
        ws.delete_user_dir = MagicMock()
        mock_workspace.return_value = ws
        ecs_mgr = MagicMock()
        ecs_mgr.delete_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.delete("/api/v1/debug/user-data")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200
        # Owner-scoped: org_id (resolve_owner_id returns org when present)
        mock_container_repo.delete.assert_called_once_with("org_test_456")
        mock_billing_repo.delete.assert_called_once_with("org_test_456")
        mock_apikey_repo.delete_all_for_owner.assert_called_once_with("org_test_456")
        # User-scoped: Clerk user_id, NOT org_id
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        mock_conn_svc.delete_all_for_user.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.debug.container_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.get_workspace")
    async def test_returns_500_when_destructive_step_fails(
        self,
        mock_workspace,
        mock_ecs_mgr,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_apikey_repo,
        mock_container_repo,
        async_client,
    ):
        """If ECS or EFS teardown raises, every other step still runs but the
        endpoint surfaces 500 with the failure list — silently 200-ing while
        leaking ECS services is the original bug this guards against."""
        mock_container = {
            "owner_id": "user_test_123",
            "service_name": "openclaw-user_test_123-abc",
            "task_definition_arn": "arn:aws:ecs:...:task-definition/foo:5",
        }
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=mock_container)
        mock_container_repo.delete = AsyncMock()
        mock_apikey_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)

        # ECS teardown blows up — every other step should still run.
        ecs_mgr = MagicMock()
        ecs_mgr.delete_user_service = AsyncMock(side_effect=RuntimeError("ECS throttled"))
        ecs_mgr._deregister_task_definition = MagicMock()
        mock_ecs_mgr.return_value = ecs_mgr

        ws = MagicMock()
        ws.delete_user_dir = MagicMock()
        mock_workspace.return_value = ws

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 500
        body = res.json()["detail"]
        # Every other DDB step still ran despite the ECS failure...
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        # ...EXCEPT the containers row, which is preserved when ECS failed
        # so retries still have access_point_id + task_definition_arn to
        # clean up the orphaned resources.
        mock_container_repo.delete.assert_not_called()
        assert "containers" not in body["deleted"]["ddb"]
        # The failure list surfaces the ECS error so the caller knows what leaked.
        assert any("ecs" in f.lower() for f in body["failures"])
        assert body["deleted"]["ecs"] is False  # ECS step did NOT complete

    @pytest.mark.asyncio
    @patch("routers.debug.container_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.get_workspace")
    async def test_attempts_ecs_teardown_when_ddb_row_already_gone(
        self,
        mock_workspace,
        mock_ecs_mgr,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_apikey_repo,
        mock_container_repo,
        async_client,
    ):
        """Regression: a previous partial run that failed at ECS but
        succeeded at the DDB sweep would leave the row gone but the ECS
        service orphaned. The next retry must still attempt ECS teardown
        (service name is derived from owner_id alone) instead of skipping
        it because `container is None`."""
        # No container row — simulates the partial-cleanup state.
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_container_repo.delete = AsyncMock()
        mock_apikey_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)

        # Service still exists in ECS even though DDB row is gone.
        ecs_mgr = MagicMock()
        ecs_mgr._service_name = MagicMock(return_value="openclaw-user_test_123-abc")
        ecs_mgr._service_exists = MagicMock(return_value={"status": "ACTIVE"})
        ecs_mgr.delete_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr

        ws = MagicMock()
        ws.delete_user_dir = MagicMock()
        mock_workspace.return_value = ws

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        # Critical: ECS teardown WAS attempted despite no container row.
        ecs_mgr.delete_user_service.assert_called_once_with("user_test_123")
        assert res.json()["deleted"]["ecs"] is True

    @pytest.mark.asyncio
    async def test_org_member_non_admin_is_rejected(self, app, mock_org_member_user):
        """Non-admin org members cannot wipe shared org-scoped state via this
        endpoint, even in dev/staging."""
        from httpx import AsyncClient, ASGITransport
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = mock_org_member_user
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.delete("/api/v1/debug/user-data")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 403


class TestEfsExists:
    """Test GET /api/v1/debug/efs-exists — read-only EFS path probe."""

    @pytest.mark.asyncio
    async def test_efs_exists_true_for_present_path(self, async_client, tmp_path, monkeypatch):
        from core.containers import get_workspace

        ws = get_workspace()
        # Point the workspace mount at a temp dir we control. Path layout:
        # `<mount>/<owner_id>/...` (matches workspace.user_path in prod).
        monkeypatch.setattr(ws, "_mount", tmp_path)
        owner_dir = tmp_path / "user_test_123"
        owner_dir.mkdir()
        (owner_dir / "agents").mkdir()

        res = await async_client.get(
            "/api/v1/debug/efs-exists",
            params={"path": str(owner_dir / "agents")},
        )
        assert res.status_code == 200
        assert res.json() == {"exists": True}

    @pytest.mark.asyncio
    async def test_efs_exists_false_for_absent_path(self, async_client, tmp_path, monkeypatch):
        from core.containers import get_workspace

        ws = get_workspace()
        monkeypatch.setattr(ws, "_mount", tmp_path)
        # owner_root must exist for path.resolve() to succeed.
        (tmp_path / "user_test_123").mkdir()
        res = await async_client.get(
            "/api/v1/debug/efs-exists",
            params={"path": str(tmp_path / "user_test_123" / "nope")},
        )
        assert res.status_code == 200
        assert res.json() == {"exists": False}

    @pytest.mark.asyncio
    async def test_efs_exists_rejects_path_outside_users_dir(self, async_client):
        """Server-side guard: path must be inside the caller's owner_root."""
        res = await async_client.get(
            "/api/v1/debug/efs-exists",
            params={"path": "/etc/passwd"},
        )
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_efs_exists_rejects_other_owners_workspace(self, async_client, tmp_path, monkeypatch):
        """Caller cannot probe another owner's workspace. Even though the
        requested path is under the shared mount root, it belongs to a
        different owner_id so the endpoint must 400."""
        from core.containers import get_workspace

        ws = get_workspace()
        monkeypatch.setattr(ws, "_mount", tmp_path)
        (tmp_path / "user_test_123").mkdir()  # caller's own dir
        other_owner_dir = tmp_path / "user_someone_else"
        other_owner_dir.mkdir()
        (other_owner_dir / "secret").touch()

        res = await async_client.get(
            "/api/v1/debug/efs-exists",
            params={"path": str(other_owner_dir / "secret")},
        )
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_efs_exists_rejects_traversal_under_owner_prefix(self, async_client, tmp_path, monkeypatch):
        """Path traversal must be canonicalized — `<owner_root>/../<other>`
        passes a naive string-prefix check but resolves outside the
        owner_root."""
        from core.containers import get_workspace

        ws = get_workspace()
        monkeypatch.setattr(ws, "_mount", tmp_path)
        owner_dir = tmp_path / "user_test_123"
        owner_dir.mkdir()
        (tmp_path / "user_someone_else").mkdir()  # traversal target

        traversal = f"{owner_dir}/../user_someone_else"
        res = await async_client.get(
            "/api/v1/debug/efs-exists",
            params={"path": traversal},
        )
        assert res.status_code == 400


class TestDdbRows:
    """Test GET /api/v1/debug/ddb-rows — per-owner row counts across all 8 tables."""

    @pytest.mark.asyncio
    @patch("routers.debug.connection_service")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_returns_counts_per_table(
        self,
        mock_container,
        mock_user,
        mock_billing,
        mock_apikey,
        mock_usage,
        mock_update,
        mock_chan,
        mock_conn,
        async_client,
    ):
        """All 8 per-user tables are scanned; counts returned."""
        mock_user.get_by_user_id = AsyncMock(return_value=None)
        mock_container.get_by_owner_id = AsyncMock(return_value=None)
        mock_billing.get_by_owner_id = AsyncMock(return_value=None)
        mock_apikey.count_for_owner = AsyncMock(return_value=0)
        mock_usage.count_for_owner = AsyncMock(return_value=0)
        mock_update.count_for_owner = AsyncMock(return_value=0)
        mock_chan.count_for_owner = AsyncMock(return_value=0)
        mock_conn.count_for_user = AsyncMock(return_value=0)

        res = await async_client.get(
            "/api/v1/debug/ddb-rows",
            params={"owner_id": "user_test_123"},
        )
        assert res.status_code == 200
        body = res.json()
        assert "tables" in body
        for tbl in (
            "users",
            "containers",
            "billing-accounts",
            "api-keys",
            "usage-counters",
            "pending-updates",
            "channel-links",
            "ws-connections",
        ):
            assert tbl in body["tables"]
            assert body["tables"][tbl] == 0

    @pytest.mark.asyncio
    @patch("routers.debug.connection_service")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_returns_nonzero_counts(
        self,
        mock_container,
        mock_user,
        mock_billing,
        mock_apikey,
        mock_usage,
        mock_update,
        mock_chan,
        mock_conn,
        async_client,
    ):
        """Single-row repos return 1 when present; multi-row repos return their counts."""
        mock_user.get_by_user_id = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_container.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_test_123"})
        mock_billing.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_test_123"})
        mock_apikey.count_for_owner = AsyncMock(return_value=2)
        mock_usage.count_for_owner = AsyncMock(return_value=4)
        mock_update.count_for_owner = AsyncMock(return_value=1)
        mock_chan.count_for_owner = AsyncMock(return_value=3)
        mock_conn.count_for_user = AsyncMock(return_value=5)

        res = await async_client.get(
            "/api/v1/debug/ddb-rows",
            params={"owner_id": "user_test_123"},
        )
        assert res.status_code == 200
        body = res.json()["tables"]
        assert body["users"] == 1
        assert body["containers"] == 1
        assert body["billing-accounts"] == 1
        assert body["api-keys"] == 2
        assert body["usage-counters"] == 4
        assert body["pending-updates"] == 1
        assert body["channel-links"] == 3
        assert body["ws-connections"] == 5

    @pytest.mark.asyncio
    @patch("routers.debug.settings")
    async def test_ddb_rows_disabled_in_prod(self, mock_settings, async_client):
        """Endpoint returns 403 when ENVIRONMENT == 'prod'."""
        mock_settings.ENVIRONMENT = "prod"
        res = await async_client.get(
            "/api/v1/debug/ddb-rows",
            params={"owner_id": "user_test_123"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    @patch("routers.debug.connection_service")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.api_key_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_org_uses_user_id_for_user_scoped_lookups(
        self,
        mock_container,
        mock_user,
        mock_billing,
        mock_apikey,
        mock_usage,
        mock_update,
        mock_chan,
        mock_conn,
        app,
        mock_org_admin_user,
    ):
        """When caller passes both owner_id (org) and user_id (member),
        user-scoped lookups (users, ws-connections) must use the user_id;
        owner-scoped lookups must use the owner_id."""
        from httpx import AsyncClient, ASGITransport
        from core.auth import get_current_user

        mock_user.get_by_user_id = AsyncMock(return_value=None)
        mock_container.get_by_owner_id = AsyncMock(return_value=None)
        mock_billing.get_by_owner_id = AsyncMock(return_value=None)
        mock_apikey.count_for_owner = AsyncMock(return_value=0)
        mock_usage.count_for_owner = AsyncMock(return_value=0)
        mock_update.count_for_owner = AsyncMock(return_value=0)
        mock_chan.count_for_owner = AsyncMock(return_value=0)
        mock_conn.count_for_user = AsyncMock(return_value=0)

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.get(
                    "/api/v1/debug/ddb-rows",
                    params={"owner_id": "org_test_456", "user_id": "user_test_123"},
                )
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 200

        # Owner-scoped: org_id
        mock_container.get_by_owner_id.assert_called_once_with("org_test_456")
        mock_billing.get_by_owner_id.assert_called_once_with("org_test_456")
        mock_apikey.count_for_owner.assert_called_once_with("org_test_456")
        # User-scoped: explicit user_id, NOT owner_id
        mock_user.get_by_user_id.assert_called_once_with("user_test_123")
        mock_conn.count_for_user.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_ddb_rows_rejects_other_owner_id(self, async_client):
        """Caller cannot probe another owner's row counts."""
        res = await async_client.get(
            "/api/v1/debug/ddb-rows",
            params={"owner_id": "user_someone_else"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_ddb_rows_rejects_other_user_id_in_org_context(self, app, mock_org_admin_user):
        """Org admin cannot probe a different member's user-scoped rows."""
        from httpx import AsyncClient, ASGITransport
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.get(
                    "/api/v1/debug/ddb-rows",
                    params={
                        "owner_id": "org_test_456",
                        "user_id": "user_someone_else",
                    },
                )
        finally:
            app.dependency_overrides.clear()
        assert res.status_code == 403
