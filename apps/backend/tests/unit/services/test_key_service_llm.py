"""Tests for the LLM-key BYOK flow in key_service (OpenAI / Anthropic).

OpenAI and Anthropic are the only supported BYOK keys. These cover:
- save validates with a 1-call ping to the provider's /models endpoint
- bad keys raise before anything is written
- valid keys are encrypted to DDB AND mirrored into Secrets Manager
- the secret ARN is persisted on the DDB row via api_key_repo.set_secret_arn
- delete clears the Secrets Manager secret with no recovery window
- delete of a non-LLM id never touches Secrets Manager

The generic set/get/delete/list surface (including the unsupported-id guard)
is covered in ``test_key_service.py`` and intentionally not re-tested here.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

# Stable per-module Fernet key so encrypt/decrypt round-trips work.
_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _encryption_key():
    """Inject a test ENCRYPTION_KEY for all tests in this module."""
    from core.config import settings as _settings

    with patch.object(_settings, "ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY):
        yield


def _ok_models_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": []}, request=request)


def _unauthorized_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": "invalid_api_key"}, request=request)


def _make_sm_mock(arn: str) -> MagicMock:
    """Build a mock Secrets Manager client matching boto3's exception surface."""
    sm = MagicMock()
    # boto3 attaches dynamic exception classes on each client; we only need
    # the two exceptions our code references.
    sm.exceptions.ResourceExistsException = type("ResourceExistsException", (Exception,), {})
    sm.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    sm.create_secret.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.put_secret_value.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.describe_secret.return_value = {"ARN": arn, "Name": "n"}
    sm.delete_secret.return_value = {}
    return sm


# --------------------------------------------------------------------------- #
# Save: OpenAI valid key                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_openai_llm_key_validates_then_pushes_to_secrets_manager():
    """Valid OpenAI key: validate 200, encrypt to DDB, push to Secrets Manager."""
    fake_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:isol8/dev/user-keys/u_1/openai-AbCdEf"

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — httpx signature
        assert "openai.com" in url
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock(fake_arn)

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.set_key = AsyncMock(
            return_value={
                "user_id": "u_1",
                "tool_id": "openai",
                "encrypted_key": "ciphertext-blob",
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)
        mock_repo.delete_key = AsyncMock(return_value=True)

        from core.services.key_service import KeyService

        svc = KeyService()
        result = await svc.set_key("u_1", "openai", "sk-test-real-key")

    # DDB write happened with an encrypted blob (not plaintext).
    mock_repo.set_key.assert_awaited_once()
    set_key_kwargs = mock_repo.set_key.call_args.kwargs
    assert set_key_kwargs["user_id"] == "u_1"
    assert set_key_kwargs["tool_id"] == "openai"
    assert set_key_kwargs["encrypted_key"] != "sk-test-real-key"

    # Secrets Manager was called (either create_secret or put_secret_value).
    assert sm.create_secret.called or sm.put_secret_value.called
    # ARN was stored back on the DDB row.
    mock_repo.set_secret_arn.assert_awaited_once_with("u_1", "openai", fake_arn)
    # Result surfaces the ARN to the caller.
    assert result["secret_arn"] == fake_arn


# --------------------------------------------------------------------------- #
# Save: bad OpenAI key — reject before storing                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_openai_llm_key_rejects_bad_key():
    """401 from OpenAI: raise before writing DDB or Secrets Manager."""

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _unauthorized_response(httpx.Request("GET", url))

    sm = _make_sm_mock("arn:never:created")

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.set_key = AsyncMock()
        mock_repo.set_secret_arn = AsyncMock()
        mock_repo.delete_key = AsyncMock()

        from core.services.key_service import KeyService

        svc = KeyService()
        with pytest.raises(ValueError, match="rejected"):
            await svc.set_key("u_bad", "openai", "sk-bogus")

    # Nothing was persisted.
    mock_repo.set_key.assert_not_awaited()
    mock_repo.set_secret_arn.assert_not_awaited()
    sm.create_secret.assert_not_called()
    sm.put_secret_value.assert_not_called()


# --------------------------------------------------------------------------- #
# Save: Anthropic valid key                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_anthropic_llm_key_validates():
    """Valid Anthropic key: validate via x-api-key header and store ARN."""
    fake_arn = "arn:aws:secretsmanager:us-east-1:111:secret:isol8/dev/user-keys/u_2/anthropic-Z"

    seen_headers: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        seen_headers.update(kwargs.get("headers", {}))
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock(fake_arn)

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.set_key = AsyncMock(
            return_value={
                "user_id": "u_2",
                "tool_id": "anthropic",
                "encrypted_key": "ciphertext",
            }
        )
        mock_repo.set_secret_arn = AsyncMock(return_value=None)
        mock_repo.delete_key = AsyncMock(return_value=True)

        from core.services.key_service import KeyService

        svc = KeyService()
        await svc.set_key("u_2", "anthropic", "sk-ant-test")

    # Anthropic uses x-api-key + anthropic-version headers, not Bearer.
    assert seen_headers.get("x-api-key") == "sk-ant-test"
    assert seen_headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in seen_headers

    # Mirrored to Secrets Manager and ARN persisted.
    assert sm.create_secret.called or sm.put_secret_value.called
    mock_repo.set_secret_arn.assert_awaited_once_with("u_2", "anthropic", fake_arn)


# --------------------------------------------------------------------------- #
# Delete: LLM key removes the Secrets Manager secret                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_llm_key_force_deletes_secret():
    """Deleting an LLM key force-deletes the Secrets Manager secret."""
    sm = _make_sm_mock("arn:doesnt:matter")

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.delete_key = AsyncMock(return_value=True)

        from core.services.key_service import KeyService

        svc = KeyService()
        ok = await svc.delete_key("u_del", "openai")

    assert ok is True
    mock_repo.delete_key.assert_awaited_once_with("u_del", "openai")
    sm.delete_secret.assert_called_once()
    # Force delete — no 30-day recovery window.
    assert sm.delete_secret.call_args.kwargs["ForceDeleteWithoutRecovery"] is True


@pytest.mark.asyncio
async def test_delete_non_llm_id_does_not_touch_secrets_manager():
    """Deleting a non-LLM id must NOT call Secrets Manager.

    delete_key is intentionally lenient (it does not gate on supported ids), so
    a stray non-LLM id only removes the DDB row and never reaches Secrets
    Manager — only ``_is_llm_provider`` ids do.
    """
    sm = _make_sm_mock("arn:noop")

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.delete_key = AsyncMock(return_value=True)

        from core.services.key_service import KeyService

        svc = KeyService()
        await svc.delete_key("u_tool", "perplexity")

    sm.delete_secret.assert_not_called()


# --------------------------------------------------------------------------- #
# Roll back DDB write if Secrets Manager push fails                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_rolls_back_ddb_on_secrets_manager_failure():
    """If Secrets Manager push fails, the DDB row must be rolled back."""

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock("arn:never:returned")
    sm.create_secret.side_effect = RuntimeError("secrets manager exploded")
    sm.put_secret_value.side_effect = RuntimeError("secrets manager exploded")

    with (
        patch("core.services.key_service.api_key_repo") as mock_repo,
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        mock_repo.set_key = AsyncMock(
            return_value={
                "user_id": "u_rb",
                "tool_id": "openai",
                "encrypted_key": "ciphertext",
            }
        )
        mock_repo.set_secret_arn = AsyncMock()
        mock_repo.delete_key = AsyncMock(return_value=True)

        from core.services.key_service import KeyService

        svc = KeyService()
        with pytest.raises(RuntimeError, match="secrets manager exploded"):
            await svc.set_key("u_rb", "openai", "sk-real")

    # DDB row was rolled back.
    mock_repo.delete_key.assert_awaited_once_with("u_rb", "openai")
    mock_repo.set_secret_arn.assert_not_awaited()
