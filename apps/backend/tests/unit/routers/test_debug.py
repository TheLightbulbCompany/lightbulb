"""Unit tests for routers/debug.py — dev/test-only debug endpoints."""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from routers.debug import assert_debug_enabled


class TestDebugEnvironmentAllowlist:
    """The /debug gate is an allowlist (issue #743 item (c)).

    It used to test ``ENVIRONMENT == "prod"``, which fails OPEN for every
    value nobody remembered to add — ``staging`` is a legal deployed value
    (core/config.py) with no stage today, so the exposure was latent rather
    than theoretical. Four call sites now route through one guard.
    """

    @pytest.mark.parametrize("env", ["dev", "local", "test", ""])
    def test_allowed_environments_pass(self, env):
        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = env
            assert_debug_enabled() is None

    @pytest.mark.parametrize("env", ["staging", "prod"])
    def test_non_sandbox_environments_are_refused(self, env):
        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = env
            with pytest.raises(HTTPException) as exc:
                assert_debug_enabled()
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path,params",
        [
            # Dependency-guarded surface (require_non_production)
            ("post", "/api/v1/debug/provision", None),
            # Inline call-sites, one per endpoint metric label
            ("delete", "/api/v1/debug/user-data", None),
            ("get", "/api/v1/debug/efs-exists", {"path": "/mnt/efs/users/user_test_123"}),
            ("get", "/api/v1/debug/ddb-rows", {"owner_id": "user_test_123"}),
        ],
    )
    async def test_every_surface_refuses_staging(self, async_client, method, path, params):
        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "staging"
            res = await getattr(async_client, method)(path, params=params)
        assert res.status_code == 403, f"{method.upper()} {path} served in staging"

    @pytest.mark.asyncio
    async def test_dev_still_reaches_the_handler(self, async_client, tmp_path, monkeypatch):
        """Sanity check on the other side of the allowlist: dev is unaffected."""
        from core.containers import get_workspace

        ws = get_workspace()
        monkeypatch.setattr(ws, "_mount", tmp_path)
        (tmp_path / "user_test_123").mkdir()

        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "dev"
            res = await async_client.get(
                "/api/v1/debug/efs-exists",
                params={"path": str(tmp_path / "user_test_123")},
            )
        assert res.status_code == 200
        assert res.json() == {"exists": True}


class TestDeleteUserData:
    """Test DELETE /api/v1/debug/user-data — atomic per-owner teardown."""

    @pytest.fixture(autouse=True)
    def _mock_integrations_teardown(self):
        # Phase 2 added an integrations sweep (per-provider Secrets Manager
        # credentials + the secret-free status rows) to the teardown. Stub it so
        # these tests don't hit real AWS; the empty-list case exercises the
        # happy path (nothing connected -> nothing to delete).
        #
        # Phase 3 replaced the api-keys DDB sweep with a byo-llm-keys step that
        # force-deletes the openai/anthropic secrets via KeyService().delete_key.
        # KeyService is imported function-locally inside _wipe_byo_llm_keys, so
        # patch it at its definition module. Stub delete_key as an AsyncMock so
        # the sweep doesn't hit Secrets Manager.
        with (
            patch("routers.debug.integrations_repo") as ir,
            patch("routers.debug.integration_secret_store") as iss,
            patch("core.services.key_service.KeyService") as key_service_cls,
        ):
            ir.list_for_owner = AsyncMock(return_value=[])
            ir.delete = AsyncMock()
            iss.delete = AsyncMock()
            key_service_cls.return_value.delete_key = AsyncMock(return_value=True)
            yield

    @pytest.mark.asyncio
    @patch("routers.debug.PaperclipRepo")
    @patch("routers.debug.container_repo")
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
        mock_container_repo,
        mock_paperclip_repo_cls,
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
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=1)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=4)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=3)
        mock_paperclip_repo_cls.return_value.delete = AsyncMock()
        mock_paperclip_repo_cls.return_value.delete_pending_brief = AsyncMock()

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
        assert "byo-llm-keys" in body["deleted"]["ddb"]
        assert "usage-counters" in body["deleted"]["ddb"]
        assert "pending-updates" in body["deleted"]["ddb"]
        assert "channel-links" in body["deleted"]["ddb"]
        assert "integrations" in body["deleted"]["ddb"]
        assert "ws-connections" in body["deleted"]["ddb"]
        # paperclip-companies (whose row now carries onboarding
        # generation/funnel/bootstrap fields) must be part of the atomic
        # wipe -- INV-10, "atomic wipe leaves zero orphan".
        assert "paperclip-companies" in body["deleted"]["ddb"]
        # ...and its pending-brief sentinel, which lives in the SAME table under
        # a synthetic PK the row delete does not reach. Left behind, it outlives
        # the teardown by design (a pending brief deliberately survives
        # reprovisioning) -- and since this endpoint does not delete the Clerk
        # account, the next signup reuses the owner_id and silently inherits the
        # previous run's answer: no funnel, and a new company seeded from an old
        # brief.
        assert "paperclip-companies:pending-brief" in body["deleted"]["ddb"]

        ecs_mgr.delete_user_service.assert_called_once_with("user_test_123")
        ws.delete_user_dir.assert_called_once_with("user_test_123")
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        mock_paperclip_repo_cls.return_value.delete.assert_called_once_with("user_test_123")
        mock_paperclip_repo_cls.return_value.delete_pending_brief.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.debug.PaperclipRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.get_workspace")
    @patch("routers.debug.integration_secret_store")
    @patch("routers.debug.integrations_repo")
    async def test_wipe_skips_secret_delete_for_hash_namespaced_rows(
        self,
        mock_integrations_repo,
        mock_secret_store,
        mock_workspace,
        mock_ecs_mgr,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_container_repo,
        mock_paperclip_repo_cls,
        async_client,
    ):
        """`#`-namespaced sub-records (google#account#..., google#assign#...) have
        no Secrets Manager secret and would 500 the wipe — their DDB row is still
        deleted, but the SM delete is skipped. A real provider row still hits SM."""
        mock_integrations_repo.list_for_owner = AsyncMock(
            return_value=[
                {"provider": "github"},
                {"provider": "google#account#alice@example.com"},
                {"provider": "google#assign#agent_1"},
            ]
        )
        mock_integrations_repo.delete = AsyncMock()
        mock_secret_store.delete = AsyncMock()

        mock_container_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_container_repo.delete = AsyncMock()
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)
        mock_paperclip_repo_cls.return_value.delete = AsyncMock()
        mock_paperclip_repo_cls.return_value.delete_pending_brief = AsyncMock()

        ecs_mgr = MagicMock()
        ecs_mgr.delete_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr
        ws = MagicMock()
        ws.delete_user_dir = MagicMock()
        mock_workspace.return_value = ws

        with patch("core.services.key_service.KeyService") as key_service_cls:
            key_service_cls.return_value.delete_key = AsyncMock(return_value=True)
            res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        assert "integrations" in res.json()["deleted"]["ddb"]
        # SM delete only for the real provider, never for the `#`-namespaced rows.
        mock_secret_store.delete.assert_awaited_once_with("user_test_123", "github")
        # Every row's DDB record is still removed.
        assert mock_integrations_repo.delete.await_count == 3

    @pytest.mark.asyncio
    @patch("routers.debug.settings")
    async def test_endpoint_disabled_in_prod(self, mock_settings, async_client):
        """Endpoint returns 403 when ENVIRONMENT == 'prod' (existing /debug guard)."""
        mock_settings.ENVIRONMENT = "prod"
        res = await async_client.delete("/api/v1/debug/user-data")
        assert res.status_code == 403

    @pytest.mark.asyncio
    @patch("routers.debug.PaperclipRepo")
    @patch("routers.debug.container_repo")
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
        mock_container_repo,
        mock_paperclip_repo_cls,
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
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)
        mock_paperclip_repo_cls.return_value.delete = AsyncMock()
        mock_paperclip_repo_cls.return_value.delete_pending_brief = AsyncMock()

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
        # User-scoped: Clerk user_id, NOT org_id
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        mock_conn_svc.delete_all_for_user.assert_called_once_with("user_test_123")
        # paperclip-companies is ALSO user-scoped (the row provision_org
        # wrote is keyed by the org creator's user_id, not org_id) -- see
        # PaperclipRepo.resolve_owner_row's docstring.
        mock_paperclip_repo_cls.return_value.delete.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.debug.PaperclipRepo")
    @patch("routers.debug.container_repo")
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
        mock_container_repo,
        mock_paperclip_repo_cls,
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
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)
        mock_paperclip_repo_cls.return_value.delete = AsyncMock()
        mock_paperclip_repo_cls.return_value.delete_pending_brief = AsyncMock()

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
    @patch("routers.debug.PaperclipRepo")
    @patch("routers.debug.container_repo")
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
        mock_container_repo,
        mock_paperclip_repo_cls,
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
        mock_billing_repo.delete = AsyncMock()
        mock_update_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_usage_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_chan_repo.delete_all_for_owner = AsyncMock(return_value=0)
        mock_user_repo.delete = AsyncMock()
        mock_conn_svc.delete_all_for_user = AsyncMock(return_value=0)
        mock_paperclip_repo_cls.return_value.delete = AsyncMock()
        mock_paperclip_repo_cls.return_value.delete_pending_brief = AsyncMock()

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
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_returns_counts_per_table(
        self,
        mock_container,
        mock_user,
        mock_billing,
        mock_usage,
        mock_update,
        mock_chan,
        mock_conn,
        async_client,
    ):
        """All per-user tables are scanned; counts returned. api-keys is gone
        (BYO LLM keys now live in Secrets Manager, not DynamoDB)."""
        mock_user.get_by_user_id = AsyncMock(return_value=None)
        mock_container.get_by_owner_id = AsyncMock(return_value=None)
        mock_billing.get_by_owner_id = AsyncMock(return_value=None)
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
            "usage-counters",
            "pending-updates",
            "channel-links",
            "ws-connections",
        ):
            assert tbl in body["tables"]
            assert body["tables"][tbl] == 0
        assert "api-keys" not in body["tables"]

    @pytest.mark.asyncio
    @patch("routers.debug.connection_service")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_returns_nonzero_counts(
        self,
        mock_container,
        mock_user,
        mock_billing,
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
        assert "api-keys" not in body
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
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.container_repo")
    async def test_ddb_rows_org_uses_user_id_for_user_scoped_lookups(
        self,
        mock_container,
        mock_user,
        mock_billing,
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
    async def test_ddb_rows_disabled_in_staging(self, async_client):
        """`staging` is a legal deployed ENVIRONMENT with no stage today — the
        old `== "prod"` blacklist would have served it the full debug surface."""
        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "staging"
            res = await async_client.get("/api/v1/debug/ddb-rows", params={"owner_id": "user_test_123"})
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


class TestReprovisionThreadsBedrockAips:
    """POST/PATCH /debug/provision must thread the owner's existing Bedrock AIP
    ARNs into write_user_configs. build_provider_block fails closed for
    bedrock_claude without them, so before the fix these routes 500'd for every
    Bedrock/trial owner (#2028), breaking the documented dev re-provision lever.

    They must READ the ARNs off the row, never call ensure_owner_aips — ensuring
    re-stamps a trial owner's reverse-index plan to "paid", which disarms the
    meter Lambda's daily-cap kill switch.
    """

    @pytest.mark.asyncio
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.container_repo")
    async def test_patch_reprovision_passes_container_aip_arns(self, mock_container_repo, mock_ecs_mgr, async_client):
        aips = {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/x"}
        mock_container_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "service_name": "openclaw-user_test_123-abc",
                "gateway_token": "gw-tok",
                "hook_token": "hook-tok",
                "bedrock_aip_arns": aips,
            }
        )
        ecs_mgr = MagicMock()
        ecs_mgr.write_user_configs = AsyncMock()
        ecs_mgr.start_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr

        res = await async_client.patch("/api/v1/debug/provision")

        assert res.status_code == 200
        ecs_mgr.write_user_configs.assert_awaited_once()
        assert ecs_mgr.write_user_configs.await_args.kwargs["aip_arns"] == aips

    @pytest.mark.asyncio
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.container_repo")
    async def test_post_provision_reuses_existing_row_aip_arns(self, mock_container_repo, mock_ecs_mgr, async_client):
        aips = {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/y"}
        # A prior (stopped) row still carries the owner's minted AIPs; status is
        # not running/provisioning, so provisioning proceeds and reuses them.
        mock_container_repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "stopped", "bedrock_aip_arns": aips}
        )
        ecs_mgr = MagicMock()
        ecs_mgr.create_user_service = AsyncMock(return_value="openclaw-user_test_123-abc")
        ecs_mgr.write_user_configs = AsyncMock()
        ecs_mgr.start_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr

        res = await async_client.post("/api/v1/debug/provision")

        assert res.status_code == 200
        ecs_mgr.write_user_configs.assert_awaited_once()
        assert ecs_mgr.write_user_configs.await_args.kwargs["aip_arns"] == aips

    @pytest.mark.asyncio
    @patch("routers.debug.get_ecs_manager")
    @patch("routers.debug.container_repo")
    async def test_post_provision_no_prior_row_passes_none(self, mock_container_repo, mock_ecs_mgr, async_client):
        # Brand-new owner: no prior row, so no AIPs to reuse. The dev shortcut
        # passes None (real provisioning mints them); this documents that we do
        # NOT silently ensure_owner_aips here and re-stamp a plan.
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=None)
        ecs_mgr = MagicMock()
        ecs_mgr.create_user_service = AsyncMock(return_value="openclaw-user_test_123-abc")
        ecs_mgr.write_user_configs = AsyncMock()
        ecs_mgr.start_user_service = AsyncMock()
        mock_ecs_mgr.return_value = ecs_mgr

        res = await async_client.post("/api/v1/debug/provision")

        assert res.status_code == 200
        assert ecs_mgr.write_user_configs.await_args.kwargs["aip_arns"] is None
