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
        # The dead "supported_tools" field is gone; only the LLM-provider
        # envelope remains.
        assert "supported_tools" not in data
        assert "supported_llm_providers" in data

    @pytest.mark.asyncio
    @patch("routers.settings_keys.KeyService")
    async def test_set_key(self, mock_svc_cls, async_client):
        """PUT /settings/keys/{tool_id} stores an LLM provider key."""
        mock_svc = AsyncMock()
        mock_svc.set_key = AsyncMock(return_value={"user_id": "user_test_123", "tool_id": "openai"})
        mock_svc_cls.return_value = mock_svc
        resp = await async_client.put(
            "/api/v1/settings/keys/openai",
            json={"api_key": "sk-test-key-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["tool_id"] == "openai"

    @pytest.mark.asyncio
    @patch("core.containers.get_ecs_manager")
    @patch("core.services.container_provision.resolve_provider_choice")
    @patch("routers.settings_keys.KeyService")
    async def test_set_key_redeploys_when_rotating_the_live_provider(
        self, mock_svc_cls, mock_resolve, mock_get_ecs, async_client
    ):
        """Rotating the key the container is actually running on rolls it.

        ECS resolves `secrets:` at task start, so without a redeploy the owner
        keeps running on the old key with no error.
        """
        mock_svc_cls.return_value = AsyncMock(set_key=AsyncMock(return_value={}))
        mock_resolve.return_value = ("byo_key", "openai")
        ecs = AsyncMock()
        mock_get_ecs.return_value = ecs

        resp = await async_client.put("/api/v1/settings/keys/openai", json={"api_key": "sk-new"})

        assert resp.status_code == 200
        assert resp.json()["container_restarting"] is True
        ecs.force_redeploy_user_service.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("core.containers.get_ecs_manager")
    @patch("core.services.container_provision.resolve_provider_choice")
    @patch("routers.settings_keys.KeyService")
    async def test_set_key_does_not_redeploy_for_the_inactive_provider(
        self, mock_svc_cls, mock_resolve, mock_get_ecs, async_client
    ):
        """Saving the OTHER provider's key must not restart anything.

        Only the active byo_provider's key is injected, so this changes nothing
        a running task holds — and the provider switch that makes it live is
        already a cold reprovision.
        """
        mock_svc_cls.return_value = AsyncMock(set_key=AsyncMock(return_value={}))
        mock_resolve.return_value = ("byo_key", "openai")
        ecs = AsyncMock()
        mock_get_ecs.return_value = ecs

        resp = await async_client.put("/api/v1/settings/keys/anthropic", json={"api_key": "sk-ant-new"})

        assert resp.status_code == 200
        assert resp.json()["container_restarting"] is False
        ecs.force_redeploy_user_service.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("core.containers.get_ecs_manager")
    @patch("core.services.container_provision.resolve_provider_choice")
    @patch("routers.settings_keys.KeyService")
    async def test_set_key_does_not_redeploy_when_not_on_byo_key(
        self, mock_svc_cls, mock_resolve, mock_get_ecs, async_client
    ):
        """A chatgpt_oauth/bedrock owner injects no BYO secret at all."""
        mock_svc_cls.return_value = AsyncMock(set_key=AsyncMock(return_value={}))
        mock_resolve.return_value = ("chatgpt_oauth", None)
        ecs = AsyncMock()
        mock_get_ecs.return_value = ecs

        resp = await async_client.put("/api/v1/settings/keys/openai", json={"api_key": "sk-new"})

        assert resp.status_code == 200
        assert resp.json()["container_restarting"] is False
        ecs.force_redeploy_user_service.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("core.containers.get_ecs_manager")
    @patch("core.services.container_provision.resolve_provider_choice")
    @patch("routers.settings_keys.KeyService")
    async def test_set_key_survives_a_redeploy_failure(self, mock_svc_cls, mock_resolve, mock_get_ecs, async_client):
        """A redeploy failure must not fail a write that already succeeded."""
        mock_svc_cls.return_value = AsyncMock(set_key=AsyncMock(return_value={}))
        mock_resolve.return_value = ("byo_key", "openai")
        ecs = AsyncMock()
        ecs.force_redeploy_user_service = AsyncMock(side_effect=RuntimeError("no such service"))
        mock_get_ecs.return_value = ecs

        resp = await async_client.put("/api/v1/settings/keys/openai", json={"api_key": "sk-new"})

        assert resp.status_code == 200
        assert resp.json()["container_restarting"] is False

    @pytest.mark.asyncio
    async def test_set_key_unsupported_tool(self, async_client):
        """PUT /settings/keys/{tool_id} rejects unsupported tools."""
        resp = await async_client.put(
            "/api/v1/settings/keys/unknown_tool",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_set_key_dead_tool_now_rejected(self, async_client):
        """The dead BYOK tool keys (e.g. perplexity) now return 400."""
        resp = await async_client.put(
            "/api/v1/settings/keys/perplexity",
            json={"api_key": "sk-test"},
        )
        assert resp.status_code == 400
        assert "Unsupported tool" in resp.json()["detail"]

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
    @pytest.mark.parametrize("dead_tool", ["elevenlabs", "openai_tts", "perplexity", "firecrawl"])
    async def test_put_settings_keys_rejects_dead_tool_keys(self, dead_tool, async_client):
        """The four removed BYOK tool keys are no longer accepted (400)."""
        resp = await async_client.put(
            f"/api/v1/settings/keys/{dead_tool}",
            json={"api_key": "x"},
        )
        assert resp.status_code == 400
        assert "Unsupported tool" in resp.json()["detail"]

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
        # The dead tool-keys envelope field is gone.
        assert "supported_tools" not in data
