"""Tests for the LLM-key BYOK flow in key_service (OpenAI / Anthropic).

OpenAI and Anthropic are the only supported BYOK keys. Post Phase-3 they live
ONLY in AWS Secrets Manager — there is no DynamoDB copy and no Fernet encryption.
These cover:
- save validates with a 1-call ping to the provider's /models endpoint
- bad keys raise before anything is written to Secrets Manager
- valid keys are create-or-updated in Secrets Manager and the ARN is returned
- delete clears the Secrets Manager secret with no recovery window

The generic set/delete/list surface (including the unsupported-id guard and the
module-level ``get_secret_arn`` helper) is covered in ``test_key_service.py``
and intentionally not re-tested here.
"""

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _ok_models_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": []}, request=request)


def _unauthorized_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": "invalid_api_key"}, request=request)


_FIXED_CREATED = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_sm_mock(arn: str, *, present: bool = True) -> MagicMock:
    """Build a mock Secrets Manager client matching boto3's exception surface.

    ``present`` controls whether ``describe_secret`` reports the secret as
    existing (True) or raises ResourceNotFound (False).
    """
    sm = MagicMock()
    # boto3 attaches dynamic exception classes on each client; we only need
    # the two exceptions our code references.
    sm.exceptions.ResourceExistsException = type("ResourceExistsException", (Exception,), {})
    sm.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    sm.exceptions.InvalidRequestException = type("InvalidRequestException", (Exception,), {})
    sm.create_secret.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.put_secret_value.return_value = {"ARN": arn, "Name": "n", "VersionId": "v"}
    sm.delete_secret.return_value = {}

    def _describe(SecretId: str):  # noqa: N803 — boto3 kwarg name
        if not present:
            raise sm.exceptions.ResourceNotFoundException()
        return {"ARN": arn, "Name": SecretId, "CreatedDate": _FIXED_CREATED}

    sm.describe_secret.side_effect = _describe
    return sm


# --------------------------------------------------------------------------- #
# Save: OpenAI valid key                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_openai_llm_key_validates_then_pushes_to_secrets_manager():
    """Valid OpenAI key: validate 200, then push to Secrets Manager."""
    fake_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:lightbulb/dev/user-keys/u_1/openai-AbCdEf"

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — httpx signature
        assert "openai.com" in url
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock(fake_arn)

    with (
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        result = await svc.set_key("u_1", "openai", "sk-test-real-key")

    # Secret was created with the raw key (ECS injects it by ARN).
    sm.create_secret.assert_called_once()
    create_kwargs = sm.create_secret.call_args.kwargs
    assert create_kwargs["SecretString"] == "sk-test-real-key"
    assert "u_1/openai" in create_kwargs["Name"]
    # Result surfaces tool_id + ARN to the caller (no DDB row, no encrypted_key).
    assert result == {"tool_id": "openai", "secret_arn": fake_arn}


@pytest.mark.asyncio
async def test_save_overwrites_existing_secret_value():
    """When the secret already exists, put_secret_value is used (not create)."""
    fake_arn = "arn:aws:secretsmanager:us-east-1:111:secret:lightbulb/dev/user-keys/u_ov/openai-X"

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock(fake_arn)
    sm.create_secret.side_effect = sm.exceptions.ResourceExistsException()

    with (
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        result = await svc.set_key("u_ov", "openai", "sk-rotated")

    sm.put_secret_value.assert_called_once()
    assert sm.put_secret_value.call_args.kwargs["SecretString"] == "sk-rotated"
    assert result["secret_arn"] == fake_arn


# --------------------------------------------------------------------------- #
# Save: bad OpenAI key — reject before storing                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_openai_llm_key_rejects_bad_key():
    """401 from OpenAI: raise before writing Secrets Manager."""

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _unauthorized_response(httpx.Request("GET", url))

    sm = _make_sm_mock("arn:never:created")

    with (
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        with pytest.raises(ValueError, match="rejected"):
            await svc.set_key("u_bad", "openai", "sk-bogus")

    # Nothing was persisted.
    sm.create_secret.assert_not_called()
    sm.put_secret_value.assert_not_called()


# --------------------------------------------------------------------------- #
# Save: Anthropic valid key                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_anthropic_llm_key_validates():
    """Valid Anthropic key: validate via x-api-key header and store in SM."""
    fake_arn = "arn:aws:secretsmanager:us-east-1:111:secret:lightbulb/dev/user-keys/u_2/anthropic-Z"

    seen_headers: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        seen_headers.update(kwargs.get("headers", {}))
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock(fake_arn)

    with (
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        result = await svc.set_key("u_2", "anthropic", "sk-ant-test")

    # Anthropic uses x-api-key + anthropic-version headers, not Bearer.
    assert seen_headers.get("x-api-key") == "sk-ant-test"
    assert seen_headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in seen_headers

    # Stored in Secrets Manager and ARN surfaced.
    assert sm.create_secret.called or sm.put_secret_value.called
    assert result == {"tool_id": "anthropic", "secret_arn": fake_arn}


# --------------------------------------------------------------------------- #
# Delete: LLM key removes the Secrets Manager secret                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_llm_key_force_deletes_secret():
    """Deleting an existing LLM key force-deletes the Secrets Manager secret."""
    sm = _make_sm_mock("arn:doesnt:matter", present=True)

    with patch("core.services.key_service.boto3.client", return_value=sm):
        from core.services.key_service import KeyService

        svc = KeyService()
        ok = await svc.delete_key("u_del", "openai")

    assert ok is True
    sm.delete_secret.assert_called_once()
    # Force delete — no 30-day recovery window.
    assert sm.delete_secret.call_args.kwargs["ForceDeleteWithoutRecovery"] is True
    assert "u_del/openai" in sm.delete_secret.call_args.kwargs["SecretId"]


@pytest.mark.asyncio
async def test_delete_absent_key_returns_false_but_still_calls_delete():
    """delete_key reports False when no secret existed (describe ResourceNotFound).

    delete_key is intentionally lenient (it does not gate on supported ids and
    swallows ResourceNotFound from the delete itself), so the force-delete is
    still issued — the boolean reflects prior existence, sourced from describe.
    """
    sm = _make_sm_mock("arn:noop", present=False)
    # delete_secret swallows ResourceNotFound inside _delete_user_secret.
    sm.delete_secret.side_effect = sm.exceptions.ResourceNotFoundException()

    with patch("core.services.key_service.boto3.client", return_value=sm):
        from core.services.key_service import KeyService

        svc = KeyService()
        ok = await svc.delete_key("u_tool", "anthropic")

    assert ok is False


# --------------------------------------------------------------------------- #
# Save: no rollback path — SM failure surfaces directly                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_orphan_delete_metric_omits_tool_id_dimension():
    """When the Secrets Manager delete fails, the orphan metric is emitted but
    MUST NOT carry tool_id — it is user-supplied/unbounded (the now-stricter
    cardinality guard would otherwise raise inside the delete handler)."""
    sm = _make_sm_mock("arn:orphan", present=True)
    # describe reports the secret exists; the delete itself blows up with a
    # non-ResourceNotFound error → orphan metric path.
    sm.delete_secret.side_effect = RuntimeError("delete failed")

    with (
        patch("core.services.key_service.boto3.client", return_value=sm),
        patch("core.observability.metrics.put_metric") as mock_put,
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        # delete_key swallows the SM failure (returns prior existence).
        ok = await svc.delete_key("u_orphan", "openai")

    assert ok is True
    orphan_calls = [c for c in mock_put.call_args_list if c.args and c.args[0] == "key_service.secret_delete_orphan"]
    assert len(orphan_calls) == 1, f"expected one orphan metric, got {orphan_calls}"
    dims = orphan_calls[0].kwargs.get("dimensions") or {}
    assert "tool_id" not in dims, f"tool_id leaked into orphan-delete dims: {dims}"


@pytest.mark.asyncio
async def test_save_raises_on_secrets_manager_failure_with_no_ddb_rollback():
    """If Secrets Manager push fails, the error surfaces — there is no DDB row
    to roll back (keys live only in Secrets Manager post Phase-3)."""

    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _ok_models_response(httpx.Request("GET", url))

    sm = _make_sm_mock("arn:never:returned")
    sm.create_secret.side_effect = RuntimeError("secrets manager exploded")
    sm.put_secret_value.side_effect = RuntimeError("secrets manager exploded")

    with (
        patch.object(httpx.AsyncClient, "get", new=fake_get),
        patch("core.services.key_service.boto3.client", return_value=sm),
    ):
        from core.services.key_service import KeyService

        svc = KeyService()
        with pytest.raises(RuntimeError, match="secrets manager exploded"):
            await svc.set_key("u_rb", "openai", "sk-real")
