"""Unit tests for users router."""

import pytest
from unittest.mock import AsyncMock, patch


class TestSyncUser:
    """Tests for POST /api/v1/users/sync endpoint."""

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_creates_new_user(self, mock_repo, async_client):
        """Sync creates a new /users row when the user doesn't exist."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "created"
        assert response.json()["user_id"] == "user_test_123"

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_returns_exists_for_existing_user(self, mock_repo, async_client):
        """Sync returns 'exists' for existing user."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123", "created_at": "2026-01-01T00:00:00Z"})

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        assert response.json()["status"] == "exists"
        assert response.json()["user_id"] == "user_test_123"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    @patch("routers.users.user_repo")
    async def test_sync_does_not_create_billing_account(
        self, mock_user_repo, mock_stripe, mock_billing_repo, async_client
    ):
        """Regression: /users/sync must NOT create a billing row.

        Creating billing on sync produced phantom personal-context rows when
        ChatLayout mounted during the transient "Clerk session has no active
        org yet" window. Billing is now created only by POST /billing/checkout.
        """
        mock_user_repo.get = AsyncMock(return_value=None)
        mock_user_repo.put = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        # Zero Stripe + repo writes. Sync only touched /users.
        mock_stripe.Customer.create.assert_not_called()
        mock_billing_repo.create_if_not_exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_requires_authentication(self, unauthenticated_async_client):
        """Sync requires authentication."""
        response = await unauthenticated_async_client.post("/api/v1/users/sync")
        assert response.status_code in [401, 403]


class TestSyncUserProviderChoice:
    """/users/sync does not write provider_choice.

    The canonical write path is POST /billing/trial-checkout, which
    persists synchronously to billing_accounts before creating the
    Stripe Checkout session. /users/sync is now a pure user-row sync
    and silently ignores any provider_choice / byo_provider it receives
    so old frontends keep working.
    """

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_does_not_persist_provider_choice(self, mock_repo, async_client):
        """provider_choice in the body is silently ignored — no user_repo write."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "bedrock_claude"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_does_not_persist_byo_key(self, mock_repo, async_client):
        """byo_key + byo_provider in the body are also silently ignored."""
        mock_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_byo_key_without_provider_no_longer_400s(self, mock_repo, async_client):
        """byo_key-without-byo_provider succeeds (no 400).

        The endpoint silently ignores both fields, so a body that would
        otherwise trigger a 400 just succeeds with no repo write.
        Pydantic Literal validation still rejects unknown values (422).
        """
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "byo_key"},
        )

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_without_provider_choice_unchanged(self, mock_repo, async_client):
        """Existing callers (no body / empty body) still work and bypass set_provider_choice."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync", json={})

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_no_body_unchanged(self, mock_repo, async_client):
        """No body at all (legacy contract) still works."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/users/sync")

        assert response.status_code == 200
        mock_repo.set_provider_choice.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.users.user_repo")
    async def test_sync_invalid_provider_choice_rejected(self, mock_repo, async_client):
        """Unknown provider_choice value is still 422 (pydantic Literal)."""
        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.put = AsyncMock(return_value=None)
        mock_repo.set_provider_choice = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/users/sync",
            json={"provider_choice": "wat"},
        )

        assert response.status_code == 422
        mock_repo.set_provider_choice.assert_not_called()


class TestGetMe:
    """Tests for GET /api/v1/users/me.

    The canonical provider_choice store lives on billing_repo (per-owner).
    For backwards compat with frontend callers (ControlSidebar,
    ControlPanelRouter, LLMPanel, OutOfCreditsBanner), /me still surfaces
    provider_choice and byo_provider — but reads them from billing_repo
    by resolved owner_id.
    """

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_returns_provider_choice_from_billing_repo(self, mock_user_repo, mock_billing_repo, async_client):
        """provider_choice + byo_provider come from billing_repo, not user_repo."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "byo_key",
                "byo_provider": "openai",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] == "byo_key"
        assert body["byo_provider"] == "openai"

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_provider_choice_null_when_no_billing_row(self, mock_user_repo, mock_billing_repo, async_client):
        """No billing row yet -> provider fields surface as null (loading state)."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(return_value=None)

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] is None
        assert body["byo_provider"] is None

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_unsynced_user_still_returns_shape(self, mock_user_repo, mock_billing_repo, async_client):
        """Unsynced user (no /users row) still returns the full provider shape."""
        mock_user_repo.get = AsyncMock(return_value=None)
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "bedrock_claude",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == "user_test_123"
        assert body["provider_choice"] == "bedrock_claude"
        # byo_provider not set on this row -> serialized as null
        assert body["byo_provider"] is None

    @pytest.mark.asyncio
    @patch("routers.users.billing_repo")
    @patch("routers.users.user_repo")
    async def test_me_bedrock_claude_no_byo_provider(self, mock_user_repo, mock_billing_repo, async_client):
        """bedrock_claude row -> byo_provider is null (not set on the row)."""
        mock_user_repo.get = AsyncMock(return_value={"user_id": "user_test_123"})
        mock_billing_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "provider_choice": "bedrock_claude",
            }
        )

        response = await async_client.get("/api/v1/users/me")

        assert response.status_code == 200
        body = response.json()
        assert body["provider_choice"] == "bedrock_claude"
        assert body["byo_provider"] is None
