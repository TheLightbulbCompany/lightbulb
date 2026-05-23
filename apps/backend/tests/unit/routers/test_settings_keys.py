"""Tests for BYOK API key management."""

from unittest.mock import patch, AsyncMock

import pytest
from cryptography.fernet import Fernet

# Deterministic test encryption key (Fernet-compatible)
_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _set_encryption_key():
    """Ensure ENCRYPTION_KEY is set for all tests in this module."""
    with patch("core.config.settings.ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY):
        yield


class TestSettingsKeys:
    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_list_keys_empty(self, mock_svc_cls, async_client):
        """GET /settings/keys returns empty list initially."""
        mock_svc = AsyncMock()
        mock_svc.list_keys = AsyncMock(return_value=[])
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.get("/api/v1/settings/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert data["keys"] == []
        assert "supported_tools" in data

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_set_key(self, mock_svc_cls, async_client):
        """PUT /settings/keys/{tool_id} stores a key."""
        mock_svc = AsyncMock()
        mock_svc.set_key = AsyncMock(return_value={"user_id": "user_test_123", "tool_id": "elevenlabs"})
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.put(
            "/api/v1/settings/keys/elevenlabs",
            json={"api_key": "sk-test-key-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["tool_id"] == "elevenlabs"

    @pytest.mark.asyncio
    async def test_set_key_unsupported_tool(self, async_client):
        """PUT /settings/keys/{tool_id} rejects unsupported tools."""
        resp = await async_client.put(
            "/api/v1/settings/keys/unknown_tool",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_delete_key(self, mock_svc_cls, async_client):
        """DELETE /settings/keys/{tool_id} removes the key."""
        mock_svc = AsyncMock()
        mock_svc.delete_key = AsyncMock(return_value=True)
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.delete("/api/v1/settings/keys/elevenlabs")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_delete_key_not_found(self, mock_svc_cls, async_client):
        """DELETE /settings/keys/{tool_id} returns 404 when no key exists."""
        mock_svc = AsyncMock()
        mock_svc.delete_key = AsyncMock(return_value=False)
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.delete("/api/v1/settings/keys/elevenlabs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_list_keys_shows_configured(self, mock_svc_cls, async_client):
        """GET /settings/keys shows configured tools without values."""
        mock_svc = AsyncMock()
        mock_svc.list_keys = AsyncMock(
            return_value=[{"tool_id": "elevenlabs", "display_name": "ElevenLabs", "created_at": "2026-01-01T00:00:00Z"}]
        )
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.get("/api/v1/settings/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["keys"]) == 1
        assert data["keys"][0]["tool_id"] == "elevenlabs"


class TestSettingsKeysLLM:
    """The router accepts the LLM provider IDs at the gate."""

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_put_settings_keys_accepts_openai(self, mock_svc_cls, async_client):
        """The router no longer rejects openai at the gate."""
        mock_svc = AsyncMock()
        mock_svc.set_key = AsyncMock(
            return_value={
                "user_id": "user_test_123",
                "tool_id": "openai",
                "secret_arn": "arn:aws:secretsmanager:us-east-1:000:secret:n",
            }
        )
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.put(
            "/api/v1/settings/keys/openai",
            json={"api_key": "sk-test-key"},
        )
        # Either 200 or 204 is fine depending on the existing handler shape;
        # the key assertion is "not 400 / Unsupported tool".
        assert resp.status_code != 400, f"Got 400: {resp.text}"
        assert resp.status_code == 200
        assert resp.json()["tool_id"] == "openai"

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_put_settings_keys_accepts_anthropic(self, mock_svc_cls, async_client):
        """The router no longer rejects anthropic at the gate."""
        mock_svc = AsyncMock()
        mock_svc.set_key = AsyncMock(
            return_value={
                "user_id": "user_test_123",
                "tool_id": "anthropic",
                "secret_arn": "arn:aws:secretsmanager:us-east-1:000:secret:n",
            }
        )
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.put(
            "/api/v1/settings/keys/anthropic",
            json={"api_key": "sk-ant-test"},
        )
        assert resp.status_code != 400, f"Got 400: {resp.text}"
        assert resp.status_code == 200
        assert resp.json()["tool_id"] == "anthropic"

    @pytest.mark.asyncio
    async def test_put_settings_keys_still_rejects_garbage(self, async_client):
        """Negative test: an unknown tool_id still returns 400."""
        resp = await async_client.put(
            "/api/v1/settings/keys/totally_made_up",
            json={"api_key": "x"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_list_keys_exposes_llm_providers(self, mock_svc_cls, async_client):
        """GET /settings/keys advertises the LLM providers in its envelope."""
        mock_svc = AsyncMock()
        mock_svc.list_keys = AsyncMock(return_value=[])
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.get("/api/v1/settings/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert "supported_llm_providers" in data
        # Order isn't guaranteed; assert as a set.
        assert set(data["supported_llm_providers"]) == {"openai", "anthropic"}
