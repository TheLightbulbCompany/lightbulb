"""Unit tests for the device-registration router."""

import pytest
from unittest.mock import AsyncMock, patch


class TestRegisterDevice:
    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_register_writes_token(self, mock_repo, async_client):
        mock_repo.set_push_token = AsyncMock(return_value=None)

        resp = await async_client.post(
            "/api/v1/devices",
            json={"token": "ExponentPushToken[abc]", "platform": "ios", "deviceId": "dev_1"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "registered"
        mock_repo.set_push_token.assert_awaited_once()
        args = mock_repo.set_push_token.await_args
        assert args.args[0] == "user_test_123"  # auth.user_id from conftest
        assert args.args[1] == "dev_1"
        assert args.args[2]["token"] == "ExponentPushToken[abc]"
        assert args.args[2]["platform"] == "ios"
        assert "updatedAt" in args.args[2]

    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_register_rejects_bad_token(self, mock_repo, async_client):
        mock_repo.set_push_token = AsyncMock()
        resp = await async_client.post(
            "/api/v1/devices",
            json={"token": "not-a-token", "platform": "ios", "deviceId": "dev_1"},
        )
        assert resp.status_code == 422
        mock_repo.set_push_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, unauthenticated_async_client):
        resp = await unauthenticated_async_client.post(
            "/api/v1/devices",
            json={"token": "ExponentPushToken[abc]", "platform": "ios", "deviceId": "dev_1"},
        )
        assert resp.status_code in [401, 403]

    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_delete_clears_token(self, mock_repo, async_client):
        mock_repo.clear_push_token = AsyncMock(return_value=None)
        resp = await async_client.delete("/api/v1/devices/dev_1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cleared"
        mock_repo.clear_push_token.assert_awaited_once_with("user_test_123", "dev_1")
