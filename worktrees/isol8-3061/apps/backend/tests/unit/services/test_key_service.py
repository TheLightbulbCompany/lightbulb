"""Tests for KeyService (user BYOK API key management, Secrets-Manager-backed).

Only LLM provider keys (OpenAI, Anthropic) are supported BYOK. The dead
tool-key paths (elevenlabs, openai_tts, perplexity, firecrawl) were removed in
the credential-layer redesign — they were stored encrypted but never delivered
to the container. ``set_key`` for any non-LLM id now raises ``ValueError``.

Post Phase-3 the DynamoDB api-keys table is gone: keys live ONLY in AWS Secrets
Manager (``isol8/<env>/user-keys/<user>/<provider>``). ``set_key`` validates the
key then create-or-updates the secret and returns ``{"tool_id", "secret_arn"}``;
``list_keys`` is sourced from ``describe_secret`` per provider; ``delete_key``
reports whether the secret existed then force-deletes it. ``get_key`` is gone —
readers resolve the SM ARN directly via the module-level ``get_secret_arn``.

The LLM validate/store/delete plumbing is exercised in depth in
``test_key_service_llm.py``; here we cover the generic set/delete/list surface
plus the unsupported-id guard and the ``get_secret_arn`` helper.
"""

from datetime import datetime, timezone

import httpx
import pytest
from unittest.mock import MagicMock, patch

from core.services.key_service import (
    KeyService,
    SUPPORTED_LLM_PROVIDERS,
    get_secret_arn,
)


async def _ok_get(self, url, **kwargs):  # noqa: ANN001 — httpx signature
    """Stub httpx GET that always validates an LLM key as good (200)."""
    return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))


_FIXED_CREATED = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_sm_mock(arn: str, *, present: set[str] | None = None) -> MagicMock:
    """Build a mock Secrets Manager client matching boto3's exception surface.

    ``present`` controls which provider secrets ``describe_secret`` reports as
    existing — its ``SecretId`` is matched by suffix (``.../<provider>``). When
    ``present`` is None every describe succeeds (single-provider tests).
    """
    sm = MagicMock()
    sm.exceptions.ResourceExistsException = type("ResourceExistsException", (Exception,), {})
    sm.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    sm.exceptions.InvalidRequestException = type("InvalidRequestException", (Exception,), {})
    sm.create_secret.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.put_secret_value.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.delete_secret.return_value = {}

    def _describe(SecretId: str):  # noqa: N803 — boto3 kwarg name
        provider = SecretId.rsplit("/", 1)[-1]
        if present is not None and provider not in present:
            raise sm.exceptions.ResourceNotFoundException()
        return {"ARN": arn, "Name": SecretId, "CreatedDate": _FIXED_CREATED}

    sm.describe_secret.side_effect = _describe
    return sm


class TestKeyServiceSetKey:
    """Test KeyService.set_key."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    async def test_creates_new_key(self, mock_boto):
        """set_key writes the SM secret and returns the tool_id + ARN."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai")

        svc = KeyService()
        result = await svc.set_key("user_ks_1", "openai", "sk-secret-123")

        assert result == {"tool_id": "openai", "secret_arn": "arn:test:openai"}
        # The plaintext key was handed to Secrets Manager, never to a DDB repo.
        mock_boto.return_value.create_secret.assert_called_once()
        create_kwargs = mock_boto.return_value.create_secret.call_args.kwargs
        assert create_kwargs["SecretString"] == "sk-secret-123"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    async def test_updates_existing_key(self, mock_boto):
        """set_key falls back to put_secret_value when the secret already exists."""
        sm = _make_sm_mock("arn:test:openai")
        sm.create_secret.side_effect = sm.exceptions.ResourceExistsException()
        mock_boto.return_value = sm

        svc = KeyService()
        result = await svc.set_key("user_ks_2", "openai", "new-key")

        sm.put_secret_value.assert_called_once()
        assert sm.put_secret_value.call_args.kwargs["SecretString"] == "new-key"
        assert result["secret_arn"] == "arn:test:openai"

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
    async def test_all_supported_providers_accepted(self, mock_boto):
        """Every provider in SUPPORTED_LLM_PROVIDERS can be set without error."""
        mock_boto.return_value = _make_sm_mock("arn:test:provider")

        svc = KeyService()
        for i, tool_id in enumerate(SUPPORTED_LLM_PROVIDERS):
            result = await svc.set_key(f"user_ks_prov_{i}", tool_id, f"key-{i}")
            assert result["tool_id"] == tool_id
            assert result["secret_arn"] == "arn:test:provider"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    @patch.object(httpx.AsyncClient, "get", new=_ok_get)
    async def test_stores_plaintext_in_secrets_manager(self, mock_boto):
        """The raw key is written to Secrets Manager (ECS injects it by ARN)."""
        sm = _make_sm_mock("arn:test:openai")
        mock_boto.return_value = sm

        svc = KeyService()
        await svc.set_key("user_ks_plain", "openai", "sk-plaintext")

        assert sm.create_secret.call_args.kwargs["SecretString"] == "sk-plaintext"


class TestGetSecretArn:
    """Test the module-level get_secret_arn(user, provider) helper."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_returns_arn_when_present(self, mock_boto):
        """Returns the SM ARN when the secret exists."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai", present={"openai"})

        arn = await get_secret_arn("user_arn_1", "openai")
        assert arn == "arn:test:openai"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_returns_none_when_absent(self, mock_boto):
        """Returns None when the secret does not exist (ResourceNotFound)."""
        mock_boto.return_value = _make_sm_mock("arn:test:openai", present=set())

        arn = await get_secret_arn("user_arn_2", "anthropic")
        assert arn is None


class TestKeyServiceDeleteKey:
    """Test KeyService.delete_key."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_deletes_existing_key(self, mock_boto):
        """delete_key force-deletes the secret and returns True when it existed."""
        sm = _make_sm_mock("arn:test:openai", present={"openai"})
        mock_boto.return_value = sm

        svc = KeyService()
        deleted = await svc.delete_key("user_ks_del_1", "openai")

        assert deleted is True
        sm.delete_secret.assert_called_once()
        assert sm.delete_secret.call_args.kwargs["ForceDeleteWithoutRecovery"] is True

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_returns_false_when_not_found(self, mock_boto):
        """delete_key returns False when no secret exists to delete."""
        sm = _make_sm_mock("arn:test:openai", present=set())
        mock_boto.return_value = sm

        svc = KeyService()
        deleted = await svc.delete_key("user_ks_del_2", "openai")

        assert deleted is False

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_delete_is_specific_to_tool(self, mock_boto):
        """Deleting one provider targets only that provider's secret name."""
        sm = _make_sm_mock("arn:test:openai", present={"openai", "anthropic"})
        mock_boto.return_value = sm

        svc = KeyService()
        await svc.delete_key("user_ks_del_3", "openai")

        # The SM SecretId for delete is scoped to the requested provider.
        assert sm.delete_secret.call_args.kwargs["SecretId"].endswith("/openai")

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_delete_is_specific_to_user(self, mock_boto):
        """Deleting a key scopes the SM SecretId to the requested user."""
        sm = _make_sm_mock("arn:test:openai", present={"openai"})
        mock_boto.return_value = sm

        svc = KeyService()
        await svc.delete_key("user_ks_del_ua", "openai")

        assert "user_ks_del_ua" in sm.delete_secret.call_args.kwargs["SecretId"]


class TestKeyServiceListKeys:
    """Test KeyService.list_keys (sourced from describe_secret per provider)."""

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_returns_empty_list_when_no_keys(self, mock_boto):
        """No secrets present → empty list."""
        mock_boto.return_value = _make_sm_mock("arn:test", present=set())

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_0")
        assert result == []

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_returns_configured_tools(self, mock_boto):
        """list_keys returns one entry per provider whose secret exists."""
        mock_boto.return_value = _make_sm_mock("arn:test", present={"openai", "anthropic"})

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_1")
        assert len(result) == 2
        tool_ids = {r["tool_id"] for r in result}
        assert tool_ids == {"openai", "anthropic"}

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_only_present_providers_listed(self, mock_boto):
        """A provider whose secret is absent is skipped (openai exists, anthropic absent)."""
        mock_boto.return_value = _make_sm_mock("arn:test", present={"openai"})

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_partial")
        assert [r["tool_id"] for r in result] == ["openai"]

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_includes_display_name(self, mock_boto):
        """Each list entry includes the human-readable display_name."""
        mock_boto.return_value = _make_sm_mock("arn:test", present={"anthropic"})

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_2")
        assert len(result) == 1
        assert result[0]["display_name"] == SUPPORTED_LLM_PROVIDERS["anthropic"]["display_name"]
        assert result[0]["tool_id"] == "anthropic"

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_includes_created_at_from_secret_metadata(self, mock_boto):
        """created_at is the SM CreatedDate rendered via .isoformat()."""
        mock_boto.return_value = _make_sm_mock("arn:test", present={"openai"})

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_3")
        assert len(result) == 1
        assert result[0]["created_at"] == _FIXED_CREATED.isoformat()
        # Round-trips back to a datetime.
        datetime.fromisoformat(result[0]["created_at"])

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_does_not_include_key_value(self, mock_boto):
        """list_keys must not expose the actual key value (security)."""
        mock_boto.return_value = _make_sm_mock("arn:test", present={"openai"})

        svc = KeyService()
        result = await svc.list_keys("user_ks_list_4")
        for entry in result:
            assert "encrypted_key" not in entry
            assert "secret_value" not in entry
            assert set(entry.keys()) == {"tool_id", "display_name", "created_at"}

    @pytest.mark.asyncio
    @patch("core.services.key_service.boto3.client")
    async def test_scopes_describe_to_requested_user(self, mock_boto):
        """list_keys probes secret names scoped to the requested user."""
        sm = _make_sm_mock("arn:test", present={"openai"})
        mock_boto.return_value = sm

        svc = KeyService()
        await svc.list_keys("user_ks_list_5a")

        for call in sm.describe_secret.call_args_list:
            assert "user_ks_list_5a" in call.kwargs["SecretId"]
