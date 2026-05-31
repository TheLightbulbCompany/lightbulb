"""Service for managing user-provided LLM API keys (BYOK).

Only LLM provider keys (OpenAI, Anthropic) live here. The plaintext is mirrored
into AWS Secrets Manager so the per-user ECS task definition can reference it
via ``secrets:[{name, valueFrom}]``, alongside an encrypted-at-rest copy in
DynamoDB. Saving a key first validates it against the provider's API, so bad
keys are rejected at save-time, not at chat-time.
"""

import logging
from typing import Optional

import boto3
import httpx

from core.config import settings
from core.encryption import decrypt, encrypt
from core.repositories import api_key_repo

logger = logging.getLogger(__name__)

# LLM provider keys: validated against the provider, and the plaintext is
# mirrored into Secrets Manager for ECS env-var injection.
SUPPORTED_LLM_PROVIDERS = {
    "openai": {"display_name": "OpenAI"},
    "anthropic": {"display_name": "Anthropic"},
}


def _allowed_ids() -> set[str]:
    return set(SUPPORTED_LLM_PROVIDERS)


def _is_llm_provider(tool_id: str) -> bool:
    return tool_id in SUPPORTED_LLM_PROVIDERS


def _secret_name(user_id: str, provider: str) -> str:
    """Per-user, per-provider Secrets Manager name.

    Must match the resource scope granted to the per-user ECS task role in
    ``service-stack.ts`` (``isol8/{env}/user-keys/*``).
    """
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/user-keys/{user_id}/{provider}"


async def _validate_llm_key(provider: str, api_key: str) -> None:
    """Validate the API key with a 1-call ping to the provider.

    Raises ``ValueError`` with a user-actionable message on auth failure.
    Other failures (network, rate limit) also raise so a save isn't silently
    treated as successful — the user can retry.
    """
    if provider == "openai":
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code == 401:
            raise ValueError("OpenAI API key rejected — verify the key and try again")
        if not r.is_success:
            raise ValueError(f"OpenAI API call failed validating key: HTTP {r.status_code}")
        return

    if provider == "anthropic":
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
        if r.status_code == 401:
            raise ValueError("Anthropic API key rejected — verify the key and try again")
        if not r.is_success:
            raise ValueError(f"Anthropic API call failed validating key: HTTP {r.status_code}")
        return

    # Defensive: caller should already have gated on _is_llm_provider.
    raise ValueError(f"Unsupported LLM provider for validation: {provider}")


def _put_user_secret(*, user_id: str, provider: str, api_key_plaintext: str) -> str:
    """Create-or-update the per-user Secrets Manager secret. Returns the ARN.

    Boto3 is sync — caller wraps with ``run_in_thread`` if it needs to await.
    """
    name = _secret_name(user_id, provider)
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        sm.create_secret(Name=name, SecretString=api_key_plaintext)
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=name, SecretString=api_key_plaintext)
    return sm.describe_secret(SecretId=name)["ARN"]


def _delete_user_secret(user_id: str, provider: str) -> None:
    """Force-delete the per-user Secrets Manager secret (no recovery window).

    Per spec: switching providers should stop billing immediately, so we skip
    the default 30-day recovery window via ``ForceDeleteWithoutRecovery``.
    """
    name = _secret_name(user_id, provider)
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        pass


class KeyService:
    """Manages user-provided API keys."""

    async def set_key(self, user_id: str, tool_id: str, api_key: str) -> dict:
        if tool_id not in _allowed_ids():
            raise ValueError(f"Unsupported tool: {tool_id}")

        # LLM keys: validate against the provider before storing anything.
        if _is_llm_provider(tool_id):
            await _validate_llm_key(tool_id, api_key)

        encrypted = encrypt(api_key)
        item = await api_key_repo.set_key(
            user_id=user_id,
            tool_id=tool_id,
            encrypted_key=encrypted,
        )

        # LLM keys: mirror plaintext into Secrets Manager for ECS injection.
        if _is_llm_provider(tool_id):
            try:
                arn = _put_user_secret(
                    user_id=user_id,
                    provider=tool_id,
                    api_key_plaintext=api_key,
                )
                await api_key_repo.set_secret_arn(user_id, tool_id, arn)
                item["secret_arn"] = arn
            except Exception:
                # Roll back the DDB row so a re-attempt isn't blocked.
                # Wrap the rollback itself: if it raises, the original Secrets
                # Manager exception is what the caller needs to see, and the
                # phantom DDB row becomes an operator-sweep concern surfaced
                # via the rollback_failed metric.
                logger.exception("Failed to push LLM key to Secrets Manager; rolling back DDB row")
                try:
                    await api_key_repo.delete_key(user_id, tool_id)
                except Exception:
                    logger.exception(
                        "key_service rollback failed for %s/%s",
                        user_id,
                        tool_id,
                    )
                    from core.observability.metrics import put_metric

                    put_metric(
                        "key_service.rollback_failed",
                        dimensions={"tool_id": tool_id},
                    )
                raise  # Re-raise the original Secrets Manager exception

        return item

    async def delete_key(self, user_id: str, tool_id: str) -> bool:
        deleted = await api_key_repo.delete_key(user_id, tool_id)
        if deleted and _is_llm_provider(tool_id):
            try:
                _delete_user_secret(user_id, tool_id)
            except Exception:
                # Don't fail the user-facing delete — the DDB row is already
                # gone, so the key won't be used. Emit a metric so the orphan
                # secret can be swept up by an operator job.
                logger.exception(
                    "Failed to delete Secrets Manager secret for user=%s provider=%s",
                    user_id,
                    tool_id,
                )
                from core.observability.metrics import put_metric

                put_metric(
                    "key_service.secret_delete_orphan",
                    dimensions={"tool_id": tool_id},
                )
        return deleted

    async def list_keys(self, user_id: str) -> list[dict]:
        items = await api_key_repo.list_keys(user_id)

        def _display_for(tool_id: str) -> str:
            if tool_id in SUPPORTED_LLM_PROVIDERS:
                return SUPPORTED_LLM_PROVIDERS[tool_id]["display_name"]
            return tool_id

        return [
            {
                "tool_id": item["tool_id"],
                "display_name": _display_for(item["tool_id"]),
                "created_at": item.get("created_at"),
            }
            for item in items
        ]

    async def get_key(self, user_id: str, tool_id: str) -> Optional[str]:
        item = await api_key_repo.get_key(user_id, tool_id)
        return decrypt(item["encrypted_key"]) if item else None
