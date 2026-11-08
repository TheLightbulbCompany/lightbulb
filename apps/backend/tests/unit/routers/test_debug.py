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
            ("get", "/api/v1/debug/ddb-rows", {"owner_id": "org_test_456"}),
        ],
    )
    async def test_every_surface_refuses_staging(self, async_client, method, path, params):
        with patch("routers.debug.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "staging"
            res = await getattr(async_client, method)(path, params=params)
        assert res.status_code == 403, f"{method.upper()} {path} served in staging"


class TestDeleteUserData:
    """Test DELETE /api/v1/debug/user-data — atomic per-owner teardown."""

    @pytest.fixture(autouse=True)
    def runtime_teardown(self):
        """``delete_workspace_runtime`` is a real AgentCore + S3 teardown -- stub it for every
        test here and hand the mock to the ones that assert on it."""
        with patch("routers.debug.delete_workspace_runtime", new_callable=AsyncMock) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def person_memory(self):
        """The person's own memory data (workspace_memory.delete_person_memory), stubbed."""
        with patch("routers.debug.workspace_memory.delete_person_memory", new_callable=AsyncMock) as mock:
            yield mock

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
    @patch("routers.debug.WorkspaceRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    async def test_full_teardown_returns_summary(
        self,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_container_repo,
        mock_paperclip_repo_cls,
        async_client,
        runtime_teardown,
        person_memory,
    ):
        """Endpoint returns deleted summary and calls each subsystem teardown."""
        mock_container = {
            "owner_id": "user_test_123",
            "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
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

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        body = res.json()
        assert body["deleted"]["runtime"] is True
        assert "users" in body["deleted"]["ddb"]
        assert "containers" in body["deleted"]["ddb"]
        assert "billing-accounts" in body["deleted"]["ddb"]
        assert "byo-llm-keys" in body["deleted"]["ddb"]
        assert "usage-counters" in body["deleted"]["ddb"]
        assert "pending-updates" in body["deleted"]["ddb"]
        assert "channel-links" in body["deleted"]["ddb"]
        assert "integrations" in body["deleted"]["ddb"]
        assert "person-memory" in body["deleted"]["ddb"]
        person_memory.assert_awaited_once_with("user_test_123")  # the caller's user id
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

        # ``delete_profiles=True`` is this endpoint's answer to the EFS ``rm -rf`` it replaces:
        # the owner's S3 profile prefix IS their workspace content, and the e2e harness's
        # empty-slate contract breaks silently if the next provision restores it.
        runtime_teardown.assert_awaited_once_with("org_test_456", delete_profiles=True, report={})
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        # The WORKSPACE row (org-keyed) goes; the caller's identity row stays.
        mock_paperclip_repo_cls.return_value.delete.assert_called_once_with("org_test_456")
        mock_paperclip_repo_cls.return_value.delete_pending_brief.assert_called_once_with("org_test_456")

    @pytest.mark.asyncio
    @patch("routers.debug.WorkspaceRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    @patch("routers.debug.integration_secret_store")
    @patch("routers.debug.integrations_repo")
    async def test_wipe_skips_secret_delete_for_hash_namespaced_rows(
        self,
        mock_integrations_repo,
        mock_secret_store,
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

        with patch("core.services.key_service.KeyService") as key_service_cls:
            key_service_cls.return_value.delete_key = AsyncMock(return_value=True)
            res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        assert "integrations" in res.json()["deleted"]["ddb"]
        # SM delete only for the real provider, never for the `#`-namespaced rows.
        mock_secret_store.delete.assert_awaited_once_with("org_test_456", "github")
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
    @patch("routers.debug.WorkspaceRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    async def test_org_context_uses_user_id_for_user_scoped_tables(
        self,
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
        # paperclip-companies: the WORKSPACE row is owner-scoped (keyed by
        # org_id); the caller's user-keyed identity row is deliberately kept.
        mock_paperclip_repo_cls.return_value.delete.assert_called_once_with("org_test_456")

    @pytest.mark.asyncio
    @patch("routers.debug.WorkspaceRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    async def test_returns_500_when_destructive_step_fails(
        self,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_container_repo,
        mock_paperclip_repo_cls,
        async_client,
        runtime_teardown,
    ):
        """If the runtime teardown raises, every other step still runs but the endpoint surfaces
        500 with the failure list — silently 200-ing while leaking a runtime is the original bug
        this guards against."""
        runtime_teardown.side_effect = RuntimeError("DeleteAgentRuntime throttled")
        mock_container = {
            "owner_id": "user_test_123",
            "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
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

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 500
        body = res.json()["detail"]
        # Every other DDB step still ran despite the ECS failure...
        mock_user_repo.delete.assert_called_once_with("user_test_123")
        # ...EXCEPT the containers row, which is preserved when the teardown failed: it still
        # holds the runtime ARN and gate-token hash the next retry needs, so wiping it turns a
        # transient error into an unrecoverable leak.
        mock_container_repo.delete.assert_not_called()
        assert "containers" not in body["deleted"]["ddb"]
        # The failure list names what leaked.
        assert any("DeleteAgentRuntime throttled" in f for f in body["failures"])
        assert body["deleted"]["runtime"] is False  # the teardown step did NOT complete

    @pytest.mark.asyncio
    @patch("routers.debug.WorkspaceRepo")
    @patch("routers.debug.container_repo")
    @patch("routers.debug.billing_repo")
    @patch("routers.debug.update_repo")
    @patch("routers.debug.usage_repo")
    @patch("routers.debug.channel_link_repo")
    @patch("routers.debug.user_repo")
    @patch("routers.debug.connection_service")
    async def test_attempts_runtime_teardown_when_ddb_row_already_gone(
        self,
        mock_conn_svc,
        mock_user_repo,
        mock_chan_repo,
        mock_usage_repo,
        mock_update_repo,
        mock_billing_repo,
        mock_container_repo,
        mock_paperclip_repo_cls,
        async_client,
        runtime_teardown,
    ):
        """Regression: a previous partial run that failed at teardown but succeeded at
        the DDB sweep would leave the row gone and the runtime orphaned. The next retry
        must still attempt teardown (everything is derived from owner_id alone) instead
        of skipping it because `container is None`."""
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

        res = await async_client.delete("/api/v1/debug/user-data")

        assert res.status_code == 200
        # Critical: teardown WAS attempted despite no container row.
        # Critical: teardown WAS attempted despite no container row, and with delete_profiles --
        # this, ``account_wipe`` and ``workspace_wipe`` are the only three terminal callers
        # that pass it (M6-R42).
        runtime_teardown.assert_awaited_once_with("org_test_456", delete_profiles=True, report={})
        assert res.json()["deleted"]["runtime"] is True

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
            params={"owner_id": "org_test_456"},
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
        mock_user.get_by_user_id = AsyncMock(return_value={"user_id": "org_test_456"})
        mock_container.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_test_456"})
        mock_billing.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_test_456"})
        mock_usage.count_for_owner = AsyncMock(return_value=4)
        mock_update.count_for_owner = AsyncMock(return_value=1)
        mock_chan.count_for_owner = AsyncMock(return_value=3)
        mock_conn.count_for_user = AsyncMock(return_value=5)

        res = await async_client.get(
            "/api/v1/debug/ddb-rows",
            params={"owner_id": "org_test_456"},
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


class TestAgentCoreDebugRoutes:
    @pytest.fixture(autouse=True)
    def no_existing_session(self, monkeypatch):
        monkeypatch.setattr("core.repositories.container_repo.get_session_sync", lambda *a, **k: {})

    """/debug/provision: POST runs the canonical provision funnel, DELETE tears the
    runtime down, PATCH rewrites the roster profiles and stops the session."""

    @staticmethod
    def _agentcore_row(**overrides):
        row = {
            "owner_id": "org_test_456",
            "status": "running",
            "runtime_host": "agentcore",
            "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        }
        row.update(overrides)
        return row

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "patch"])
    async def test_org_member_cannot_provision_or_rewrite_the_workspace(self, app, mock_org_member_user, method):
        from httpx import ASGITransport, AsyncClient

        from core.auth import get_current_user

        dispatch, write = AsyncMock(), AsyncMock()
        app.dependency_overrides[get_current_user] = mock_org_member_user
        try:
            with (
                patch("routers.debug.dispatch_provision", dispatch),
                patch("core.services.hermes_provisioning.write_profiles", write),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    res = await getattr(client, method)("/api/v1/debug/provision")
        finally:
            app.dependency_overrides.clear()
        assert res.status_code == 403
        dispatch.assert_not_awaited()
        write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_provision_stands_down_for_a_live_row(self, async_client):
        repo = MagicMock()
        repo.get_by_owner_id = AsyncMock(return_value=self._agentcore_row())
        dispatch = AsyncMock()
        with (
            patch("routers.debug.container_repo", repo),
            patch("routers.debug.dispatch_provision", dispatch),
        ):
            res = await async_client.post("/api/v1/debug/provision")
        assert res.status_code == 200
        assert res.json()["status"] == "already_running"
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_provision_dispatches_the_canonical_funnel(self, async_client):
        """Not a hand-rolled provision: the runner owns the AIP/role/gate-token mint, the
        roster profiles and the runtime, in that order."""
        repo = MagicMock()
        repo.get_by_owner_id = AsyncMock(return_value=None)
        dispatch = AsyncMock()
        with (
            patch("routers.debug.container_repo", repo),
            patch("routers.debug.dispatch_provision", dispatch),
        ):
            res = await async_client.post("/api/v1/debug/provision")
        assert res.status_code == 200
        assert res.json()["status"] == "provisioning"
        dispatch.assert_awaited_once_with("org_test_456")

    @pytest.mark.asyncio
    async def test_delete_provision_tears_the_runtime_down(self, async_client):
        teardown = AsyncMock()
        with patch("routers.debug.delete_workspace_runtime", teardown):
            res = await async_client.delete("/api/v1/debug/provision")
        assert res.status_code == 200
        assert res.json() == {"status": "removed"}
        # M6-R42: the routine reprovision door NEVER passes delete_profiles -- destroying a
        # workspace's memory/skills/SOUL.md is /debug/user-data's job, and this route is what a
        # dev hits repeatedly.
        teardown.assert_awaited_once_with("org_test_456")

    @pytest.mark.asyncio
    async def test_delete_provision_surfaces_a_teardown_failure_as_a_503(self, async_client):
        """A dev whose teardown half-ran needs to know. Reporting "removed" would send them
        straight into a POST that re-provisions on top of a runtime still being deleted."""
        with patch(
            "routers.debug.delete_workspace_runtime", AsyncMock(side_effect=RuntimeError("DeleteAgentRuntime failed"))
        ):
            res = await async_client.delete("/api/v1/debug/provision")
        assert res.status_code == 503
        assert "DeleteAgentRuntime failed" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_provision_rewrites_profiles_and_stops_the_session(self, async_client):
        row = self._agentcore_row()
        repo = MagicMock()
        repo.get_by_owner_id = AsyncMock(return_value=row)
        write_profiles = AsyncMock()
        stop_session = MagicMock()
        with (
            patch("routers.debug.container_repo", repo),
            patch("core.services.hermes_provisioning.write_profiles", write_profiles),
            patch("core.services.hermes_provisioning._stop_runtime_session", stop_session),
        ):
            res = await async_client.patch("/api/v1/debug/provision")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "profiles_rewritten"
        write_profiles.assert_awaited_once_with("org_test_456")
        stop_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_patch_provision_stop_session_failure_is_best_effort(self, async_client):
        row = self._agentcore_row()
        repo = MagicMock()
        repo.get_by_owner_id = AsyncMock(return_value=row)
        with (
            patch("routers.debug.container_repo", repo),
            patch("core.services.hermes_provisioning.write_profiles", AsyncMock()),
            patch(
                "core.services.hermes_provisioning._stop_runtime_session",
                MagicMock(side_effect=RuntimeError("unknown session")),
            ),
        ):
            res = await async_client.patch("/api/v1/debug/provision")
        assert res.status_code == 200, res.text
