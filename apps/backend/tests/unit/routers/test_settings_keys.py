"""Tests for legacy BYOK-key compatibility without new setup."""

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


@pytest.fixture(autouse=True)
def _provider(request):
    """Default: the owner is not on BYO, so set/delete touch no profile."""
    choice = getattr(request, "param", ("bedrock_claude", None))
    with patch("routers.settings_keys.resolve_provider_choice", AsyncMock(return_value=choice)):
        yield


class TestByoKeyRotation:
    """New LLM key uploads are closed; saved keys remain readable and usable."""

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_upload_is_rejected_without_secret_or_profile_writes(self, mock_svc_cls, async_client):
        resp = await async_client.put("/api/v1/settings/keys/openai", json={"api_key": "sk-new"})
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "byo_setup_retired"
        assert "saved key" in resp.json()["detail"]["message"]
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("_provider", [("byo_key", "openai")], indirect=True)
    @patch("routers.settings_keys.KeyService")
    async def test_deleting_active_key_is_refused(self, mock_svc_cls, async_client):
        mock_svc = AsyncMock()
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.delete("/api/v1/settings/keys/openai")
        assert resp.status_code == 409
        mock_svc.delete_key.assert_not_awaited()


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
        # Legacy rows remain readable, but new LLM keys are not supported.
        assert "supported_tools" not in data
        assert data["supported_llm_providers"] == []

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_set_key_is_retired(self, mock_svc_cls, async_client):
        """PUT rejects new BYO setup and does not touch stored credentials."""
        resp = await async_client.put(
            "/api/v1/settings/keys/openai",
            json={"api_key": "sk-test-key-123"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "byo_setup_retired"
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_key_unsupported_tool(self, async_client):
        """PUT /settings/keys/{tool_id} rejects unsupported tools."""
        resp = await async_client.put(
            "/api/v1/settings/keys/unknown_tool",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "byo_setup_retired"

    @pytest.mark.asyncio
    async def test_set_key_dead_tool_now_rejected(self, async_client):
        """The dead BYOK tool keys (e.g. perplexity) now return 400."""
        resp = await async_client.put(
            "/api/v1/settings/keys/perplexity",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "byo_setup_retired"

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_delete_key(self, mock_svc_cls, async_client):
        """DELETE /settings/keys/{tool_id} removes the key."""
        mock_svc = AsyncMock()
        mock_svc.delete_key = AsyncMock(return_value=True)
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.delete("/api/v1/settings/keys/openai")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_delete_key_not_found(self, mock_svc_cls, async_client):
        """DELETE /settings/keys/{tool_id} returns 404 when no key exists."""
        mock_svc = AsyncMock()
        mock_svc.delete_key = AsyncMock(return_value=False)
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.delete("/api/v1/settings/keys/openai")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_key_dead_tool_now_rejected(self, async_client):
        """DELETE for a dead BYOK tool key (e.g. perplexity) now returns 400."""
        resp = await async_client.delete("/api/v1/settings/keys/perplexity")
        assert resp.status_code == 400
        assert "Unsupported tool" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_list_keys_shows_configured(self, mock_svc_cls, async_client):
        """GET /settings/keys shows configured tools without values."""
        mock_svc = AsyncMock()
        mock_svc.list_keys = AsyncMock(
            return_value=[{"tool_id": "openai", "display_name": "OpenAI", "created_at": "2026-01-01T00:00:00Z"}]
        )
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.get("/api/v1/settings/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["keys"]) == 1
        assert data["keys"][0]["tool_id"] == "openai"


class TestSettingsKeysLLM:
    """Saved keys remain readable, but no new LLM key can be uploaded."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_id", ["openai", "anthropic", "unknown", "perplexity"])
    @patch("routers.settings_keys.KeyService")
    async def test_put_rejects_new_key_upload(self, mock_svc_cls, async_client, tool_id):
        resp = await async_client.put(
            f"/api/v1/settings/keys/{tool_id}",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "byo_setup_retired"
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_list_advertises_no_new_llm_key_providers(self, mock_svc_cls, async_client):
        mock_svc = AsyncMock()
        mock_svc.list_keys = AsyncMock(return_value=[])
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.get("/api/v1/settings/keys")
        assert resp.status_code == 200
        assert resp.json()["supported_llm_providers"] == []
