"""Tests for the per-agent Google-account assignment router.

Mirrors the auth-override + async_client idiom from test_connections.py:
``get_current_user`` is overridden by the ``async_client`` fixture (active
workspace, owner_id == "org_test_456"); the org fixtures exercise the admin gate.
The repo and the IDENTITY.md seeder are patched with ``AsyncMock`` so the
awaits resolve without touching DynamoDB or EFS — the assertions are that the
router reads/writes the right rows and re-seeds the right agent.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestListAccounts:
    """GET /api/v1/google/accounts — owner's accounts + per-agent assignments."""

    @pytest.mark.asyncio
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_list_returns_accounts_and_assignments(self, mock_repo, async_client):
        mock_repo.list_accounts = AsyncMock(
            return_value=[
                {"email": "alice@example.com", "connected_at": "2026-01-01T00:00:00Z"},
                {"email": "bob@example.com", "connected_at": "2026-02-01T00:00:00Z"},
            ]
        )
        mock_repo.list_assignments = AsyncMock(return_value={"agent_1": "bob@example.com"})

        response = await async_client.get("/api/v1/google/accounts")
        assert response.status_code == 200
        body = response.json()
        assert body["accounts"] == [
            {"email": "alice@example.com", "connected_at": "2026-01-01T00:00:00Z"},
            {"email": "bob@example.com", "connected_at": "2026-02-01T00:00:00Z"},
        ]
        assert body["assignments"] == {"agent_1": "bob@example.com"}

        # Owner-scoped reads.
        mock_repo.list_accounts.assert_awaited_once_with("org_test_456")
        mock_repo.list_assignments.assert_awaited_once_with("org_test_456")

    @pytest.mark.asyncio
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_list_not_gated_for_org_member(self, mock_repo, async_client_org_member):
        # GET exposes no secrets (emails + assignment map) -> members may read it,
        # scoped to the shared org workspace.
        mock_repo.list_accounts = AsyncMock(return_value=[])
        mock_repo.list_assignments = AsyncMock(return_value={})

        response = await async_client_org_member.get("/api/v1/google/accounts")
        assert response.status_code == 200
        mock_repo.list_accounts.assert_awaited_once_with("org_test_456")


class TestAssignAccount:
    """POST /api/v1/google/accounts/{email}/assign — bind an agent to an account."""

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_validates_sets_and_seeds(self, mock_repo, mock_seed, async_client):
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock(return_value={"agent_1": "alice@example.com"})

        response = await async_client.post(
            "/api/v1/google/accounts/alice@example.com/assign",
            json={"agent_id": "agent_1"},
        )
        assert response.status_code == 200
        assert response.json() == {"assignments": {"agent_1": "alice@example.com"}}

        # Assignment written for the resolved owner.
        mock_repo.set_assignment.assert_awaited_once_with("org_test_456", "agent_1", "alice@example.com")
        # IDENTITY.md re-seeded for that agent so the gog-account block reflects
        # the new account immediately.
        mock_seed.assert_awaited_once_with("org_test_456", "agent_1")

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_unknown_email_rejected(self, mock_repo, mock_seed, async_client):
        # The email must be one of the owner's connected accounts — assigning an
        # agent to an account the owner never connected is a 404, with no write
        # and no re-seed.
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock()

        response = await async_client.post(
            "/api/v1/google/accounts/stranger@example.com/assign",
            json={"agent_id": "agent_1"},
        )
        assert response.status_code == 404
        mock_repo.set_assignment.assert_not_called()
        mock_seed.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_traversal_agent_id_rejected(self, mock_repo, mock_seed, async_client):
        # A traversal-y agent_id is rejected before any repo write or re-seed, so
        # no junk `google#assign#...` row is persisted.
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock()

        response = await async_client.post(
            "/api/v1/google/accounts/alice@example.com/assign",
            json={"agent_id": "../../etc"},
        )
        assert response.status_code == 400
        mock_repo.set_assignment.assert_not_called()
        mock_seed.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.google_accounts.runtime_transport.is_agentcore_owner", new_callable=AsyncMock)
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_refuses_on_an_agentcore_workspace(self, mock_repo, mock_seed, mock_agentcore, async_client):
        """M6 task-14 row 4: the binding is only ever delivered as a ``gog -a <email>``
        instruction, and neither the ``gog`` binary nor its EFS keyring exists on a Hermes
        microVM. Storing the row and answering 200 is the silent-success shape this pass
        removes — refuse, same as the Shopify container login (task 10-D)."""
        mock_agentcore.return_value = True
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()

        response = await async_client.post(
            "/api/v1/google/accounts/alice@example.com/assign",
            json={"agent_id": "agent_1"},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "unsupported_on_agentcore"
        mock_repo.set_assignment.assert_not_called()
        mock_seed.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_non_admin_member_forbidden(self, mock_repo, mock_seed, async_client_org_member):
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()

        response = await async_client_org_member.post(
            "/api/v1/google/accounts/alice@example.com/assign",
            json={"agent_id": "agent_1"},
        )
        assert response.status_code == 403
        # Gate fires before any membership check / write / re-seed.
        mock_repo.set_assignment.assert_not_called()
        mock_seed.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_assign_org_admin_allowed_targets_org_workspace(self, mock_repo, mock_seed, async_client_org_admin):
        mock_repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
        mock_repo.set_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock(return_value={"agent_1": "alice@example.com"})

        response = await async_client_org_admin.post(
            "/api/v1/google/accounts/alice@example.com/assign",
            json={"agent_id": "agent_1"},
        )
        assert response.status_code == 200
        # Admin clears the gate; write + re-seed target the shared org workspace.
        mock_repo.set_assignment.assert_awaited_once_with("org_test_456", "agent_1", "alice@example.com")
        mock_seed.assert_awaited_once_with("org_test_456", "agent_1")


class TestUnassignAgent:
    """DELETE /api/v1/google/accounts/assign/{agent_id} — clear an agent binding."""

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_unassign_removes_and_reseeds(self, mock_repo, mock_seed, async_client):
        mock_repo.remove_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock(return_value={})

        response = await async_client.delete("/api/v1/google/accounts/assign/agent_1")
        assert response.status_code == 200
        assert response.json() == {"assignments": {}}

        mock_repo.remove_assignment.assert_awaited_once_with("org_test_456", "agent_1")
        # Re-seed re-resolves the agent: it falls back to the owner default (or
        # None), so the IDENTITY.md block is updated/removed accordingly.
        mock_seed.assert_awaited_once_with("org_test_456", "agent_1")

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_unassign_traversal_agent_id_rejected(self, mock_repo, mock_seed, async_client):
        # A traversal-y agent_id is rejected before remove_assignment, so the
        # guard can't be used to churn an unrelated DDB row. A backslash survives
        # URL path-normalization where forward-slash segments would not.
        mock_repo.remove_assignment = AsyncMock()
        mock_repo.list_assignments = AsyncMock()

        response = await async_client.delete("/api/v1/google/accounts/assign/..\\..\\etc")
        assert response.status_code == 400
        mock_repo.remove_assignment.assert_not_called()
        mock_seed.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.google_accounts.seed_agent_google_identity", new_callable=AsyncMock)
    @patch("routers.google_accounts.google_accounts_repo")
    async def test_unassign_non_admin_member_forbidden(self, mock_repo, mock_seed, async_client_org_member):
        mock_repo.remove_assignment = AsyncMock()

        response = await async_client_org_member.delete("/api/v1/google/accounts/assign/agent_1")
        assert response.status_code == 403
        mock_repo.remove_assignment.assert_not_called()
        mock_seed.assert_not_called()
