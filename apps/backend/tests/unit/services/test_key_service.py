"""Tests for KeyService (user BYOK API key management, DynamoDB-backed).

Only LLM provider keys (OpenAI, Anthropic) are supported BYOK. The dead
tool-key paths (elevenlabs, openai_tts, perplexity, firecrawl) were removed in
the credential-layer redesign — they were stored encrypted but never delivered
to the container. ``set_key`` for any non-LLM id now raises ``ValueError``.

The LLM save/delete plumbing (validate -> encrypt to DDB + mirror to Secrets
Manager) is exercised in depth in ``test_key_service_llm.py``; here we cover
the generic set/get/delete/list surface plus the unsupported-id guard.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cryptography.fernet import Fernet

from core.services.key_service import KeyService, SUPPORTED_LLM_PROVIDERS
from core.encryption import encrypt, decrypt

# Generate a stable test key for the entire module
_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def encryption_key():
    """Inject a test ENCRYPTION_KEY for all tests in this module."""
    from core.config import settings as _settings

    with patch.object(_settings, "ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY):
        yield


async def _ok_get(self, url, **kwargs):  # noqa: ANN001 — httpx signature
    """Stub httpx GET that always validates an LLM key as good (200)."""
    return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))


def _make_sm_mock(arn: str) -> MagicMock:
    """Build a mock Secrets Manager client matching boto3's exception surface."""
    sm = MagicMock()
    sm.exceptions.ResourceExistsException = type("ResourceExistsException", (Exception,), {})
    sm.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    sm.create_secret.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.put_secret_value.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.describe_secret.return_value = {"ARN": arn, "Name": "n"}
    sm.delete_secret.return_value = {}
    return sm


class TestKeyServiceSetKey:
    """Test KeyService.set_key."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    @patch("core.services.key_service.api_key_repo")
    async def test_creates_new_key(self, mock_repo, mock_boto):
        """set_key calls api_key_repo.set_key with encrypted value."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.set_key = AsyncMock(
            return_value={
                "user_id": "user_ks_1",
                "tool_id": "openai",
                "encrypted_key": "encrypted-blob",
                "created_at": "2025-01-01T00:00:00Z",
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)

        svc = KeyService()
        result = await svc.set_key("user_ks_1", "openai", "sk-secret-123")

        assert result["user_id"] == "user_ks_1"
        assert result["tool_id"] == "openai"
        mock_repo.set_key.assert_called_once()
        # Verify the encrypted_key arg is not the plaintext
        call_kwargs = mock_repo.set_key.call_args[1]
        assert call_kwargs["encrypted_key"] != "sk-secret-123"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    @patch("core.services.key_service.api_key_repo")
    async def test_updates_existing_key(self, mock_repo, mock_boto):
        """set_key overwrites when calling again for same user/tool."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.set_key = AsyncMock(
            return_value={
                "user_id": "user_ks_2",
                "tool_id": "openai",
                "encrypted_key": "encrypted-new",
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)

        svc = KeyService()
        await svc.set_key("user_ks_2", "openai", "old-key")
        await svc.set_key("user_ks_2", "openai", "new-key")

        assert mock_repo.set_key.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_for_unsupported_tool(self):
        """set_key raises ValueError for unknown tool IDs."""
        svc = KeyService()
        with pytest.raises(ValueError, match="Unsupported tool"):
            await svc.set_key("user_ks_3", "unknown_tool", "some-key")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dead_tool", ["perplexity", "firecrawl", "elevenlabs", "openai_tts"])
    async def test_raises_for_removed_tool_keys(self, dead_tool):
        """The four removed BYOK tool keys are no longer supported."""
        svc = KeyService()
        with pytest.raises(ValueError, match="Unsupported tool"):
            await svc.set_key("user_ks_dead", dead_tool, "some-key")

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    @patch("core.services.key_service.api_key_repo")
    async def test_all_supported_providers_accepted(self, mock_repo, mock_boto):
        """Every provider in SUPPORTED_LLM_PROVIDERS can be set without error."""
        mock_boto.return_value = _make_sm_mock("arn:test:provider")
        mock_repo.set_key = AsyncMock(
            side_effect=lambda **kw: {
                "user_id": kw["user_id"],
                "tool_id": kw["tool_id"],
                "encrypted_key": kw["encrypted_key"],
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)

        svc = KeyService()
        for i, tool_id in enumerate(SUPPORTED_LLM_PROVIDERS):
            result = await svc.set_key(f"user_ks_prov_{i}", tool_id, f"key-{i}")
            assert result["tool_id"] == tool_id

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    @patch("core.services.key_service.api_key_repo")
    async def test_different_users_same_tool_independent(self, mock_repo, mock_boto):
        """Different users can set the same provider without conflict."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        # set_key just stores; get_key returns the decryptable value
        encrypted_a = encrypt("key-a")
        encrypted_b = encrypt("key-b")

        mock_repo.set_key = AsyncMock(return_value={})
        mock_repo.set_secret_arn = AsyncMock(return_value=None)
        mock_repo.get_key = AsyncMock(
            side_effect=lambda uid, tid: (
                {"encrypted_key": encrypted_a} if uid == "user_ks_a" else {"encrypted_key": encrypted_b}
            )
        )

        svc = KeyService()
        await svc.set_key("user_ks_a", "openai", "key-a")
        await svc.set_key("user_ks_b", "openai", "key-b")

        result_a = await svc.get_key("user_ks_a", "openai")
        result_b = await svc.get_key("user_ks_b", "openai")
        assert result_a == "key-a"
        assert result_b == "key-b"


class TestKeyServiceGetKey:
    """Test KeyService.get_key."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_stored_key(self, mock_repo):
        """get_key returns the decrypted key value."""
        encrypted = encrypt("oa-secret")
        mock_repo.get_key = AsyncMock(return_value={"encrypted_key": encrypted})

        svc = KeyService()
        result = await svc.get_key("user_ks_get_1", "openai")
        assert result == "oa-secret"

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_none_when_not_set(self, mock_repo):
        """get_key returns None when no key exists for that user/tool."""
        mock_repo.get_key = AsyncMock(return_value=None)

        svc = KeyService()
        result = await svc.get_key("user_ks_nokey", "openai")
        assert result is None

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_none_for_different_tool(self, mock_repo):
        """get_key returns None when querying a different tool than what was set."""
        mock_repo.get_key = AsyncMock(return_value=None)

        svc = KeyService()
        result = await svc.get_key("user_ks_get_2", "anthropic")
        assert result is None

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_none_for_different_user(self, mock_repo):
        """get_key returns None when querying a different user's key."""
        mock_repo.get_key = AsyncMock(return_value=None)

        svc = KeyService()
        result = await svc.get_key("user_ks_other", "anthropic")
        assert result is None


class TestKeyServiceDeleteKey:
    """Test KeyService.delete_key."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch("core.services.key_service.api_key_repo")
    async def test_deletes_existing_key(self, mock_repo, mock_boto):
        """delete_key removes the key and returns True."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.delete_key = AsyncMock(return_value=True)

        svc = KeyService()
        deleted = await svc.delete_key("user_ks_del_1", "openai")
        assert deleted is True
        mock_repo.delete_key.assert_called_once_with("user_ks_del_1", "openai")

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_false_when_not_found(self, mock_repo, mock_boto):
        """delete_key returns False when no key exists to delete."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.delete_key = AsyncMock(return_value=False)

        svc = KeyService()
        deleted = await svc.delete_key("user_ks_del_2", "openai")
        assert deleted is False

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch("core.services.key_service.api_key_repo")
    async def test_delete_is_specific_to_tool(self, mock_repo, mock_boto):
        """Deleting one provider's key leaves other providers intact."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.delete_key = AsyncMock(return_value=True)
        encrypted_an = encrypt("an-key")
        mock_repo.get_key = AsyncMock(
            side_effect=lambda uid, tid: None if tid == "openai" else {"encrypted_key": encrypted_an}
        )

        svc = KeyService()
        await svc.delete_key("user_ks_del_3", "openai")

        assert await svc.get_key("user_ks_del_3", "openai") is None
        assert await svc.get_key("user_ks_del_3", "anthropic") == "an-key"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch("core.services.key_service.api_key_repo")
    async def test_delete_is_specific_to_user(self, mock_repo, mock_boto):
        """Deleting a key for one user leaves other users' keys intact."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.delete_key = AsyncMock(return_value=True)
        encrypted_ub = encrypt("key-ub")
        mock_repo.get_key = AsyncMock(
            side_effect=lambda uid, tid: None if uid == "user_ks_del_ua" else {"encrypted_key": encrypted_ub}
        )

        svc = KeyService()
        await svc.delete_key("user_ks_del_ua", "openai")

        assert await svc.get_key("user_ks_del_ua", "openai") is None
        assert await svc.get_key("user_ks_del_ub", "openai") == "key-ub"


class TestKeyServiceListKeys:
    """Test KeyService.list_keys."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_empty_list_when_no_keys(self, mock_repo):
        mock_repo.list_keys = AsyncMock(return_value=[])

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_0")
        assert result == []

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_returns_configured_tools(self, mock_repo):
        """list_keys returns one entry per configured provider."""
        mock_repo.list_keys = AsyncMock(
            return_value=[
                {"tool_id": "openai", "created_at": "2025-01-01T00:00:00Z"},
                {"tool_id": "anthropic", "created_at": "2025-01-01T00:00:00Z"},
            ]
        )

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_1")
        assert len(result) == 2
        tool_ids = {r["tool_id"] for r in result}
        assert tool_ids == {"openai", "anthropic"}

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_includes_display_name(self, mock_repo):
        """Each list entry includes the human-readable display_name."""
        mock_repo.list_keys = AsyncMock(
            return_value=[
                {"tool_id": "anthropic", "created_at": "2025-01-01T00:00:00Z"},
            ]
        )

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_2")
        assert len(result) == 1
        assert result[0]["display_name"] == SUPPORTED_LLM_PROVIDERS["anthropic"]["display_name"]
        assert result[0]["tool_id"] == "anthropic"

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_includes_created_at(self, mock_repo):
        """Each list entry includes a created_at ISO timestamp."""
        mock_repo.list_keys = AsyncMock(
            return_value=[
                {"tool_id": "openai", "created_at": "2025-01-15T12:30:00Z"},
            ]
        )

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_3")
        assert len(result) == 1
        assert result[0]["created_at"] is not None
        from datetime import datetime

        datetime.fromisoformat(result[0]["created_at"])

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_does_not_include_key_value(self, mock_repo):
        """list_keys must not expose the actual key value (security)."""
        mock_repo.list_keys = AsyncMock(
            return_value=[
                {"tool_id": "openai", "created_at": "2025-01-01T00:00:00Z"},
            ]
        )

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_4")
        for entry in result:
            assert "encrypted_key" not in entry
            assert "secret-value" not in str(entry)

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_only_shows_own_user_keys(self, mock_repo):
        """list_keys only returns keys for the requested user."""
        mock_repo.list_keys = AsyncMock(
            return_value=[
                {"tool_id": "openai", "created_at": "2025-01-01T00:00:00Z"},
            ]
        )

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_5a")
        assert len(result) == 1
        assert result[0]["tool_id"] == "openai"
        mock_repo.list_keys.assert_called_once_with("user_ks_list_5a")


class TestFernetEncryption:
    """Test Fernet encrypt/decrypt helpers from core.encryption."""

    def test_encrypt_returns_non_empty_string(self):
        """encrypt returns a non-empty string different from the input."""
        result = encrypt("my-secret-key")
        assert result != "my-secret-key"
        assert len(result) > 0

    def test_decrypt_recovers_plaintext(self):
        """decrypt reverses encrypt."""
        ciphertext = encrypt("my-secret-key")
        result = decrypt(ciphertext)
        assert result == "my-secret-key"

    def test_encrypt_decrypt_roundtrip(self):
        """encrypt + decrypt round-trips any plaintext."""
        plaintext = "super-secret-api-key-12345"
        assert decrypt(encrypt(plaintext)) == plaintext

    def test_encrypt_raises_without_key(self):
        """encrypt raises RuntimeError when ENCRYPTION_KEY is not set."""
        from core.config import settings as _settings

        with patch.object(_settings, "ENCRYPTION_KEY", ""):
            with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
                encrypt("some-key")

    def test_decrypt_raises_for_invalid_ciphertext(self):
        """decrypt raises ValueError for corrupted or wrong-key ciphertext."""
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt("not-valid-ciphertext")

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    @patch("core.services.key_service.api_key_repo")
    async def test_set_key_encrypts_before_storing(self, mock_repo, mock_boto):
        """set_key stores an encrypted value, not the raw plaintext."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")
        mock_repo.set_key = AsyncMock(
            side_effect=lambda **kw: {
                "user_id": kw["user_id"],
                "tool_id": kw["tool_id"],
                "encrypted_key": kw["encrypted_key"],
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)

        svc = KeyService()
        await svc.set_key("user_fernet_1", "openai", "plaintext-key")

        # The encrypted_key passed to repo should not be plaintext
        call_kwargs = mock_repo.set_key.call_args[1]
        assert call_kwargs["encrypted_key"] != "plaintext-key"
        assert len(call_kwargs["encrypted_key"]) > 0

    @pytest.mark.asyncio
    @patch("core.services.key_service.api_key_repo")
    async def test_get_key_decrypts_on_retrieval(self, mock_repo):
        """get_key returns the original plaintext after round-tripping."""
        encrypted = encrypt("original-key")
        mock_repo.get_key = AsyncMock(return_value={"encrypted_key": encrypted})

        svc = KeyService()
        retrieved = await svc.get_key("user_fernet_2", "anthropic")

        assert retrieved == "original-key"
