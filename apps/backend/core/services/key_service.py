"""Service for managing user-provided LLM API keys (BYOK).

Only LLM provider keys (OpenAI, Anthropic) live here, stored solely in AWS
Secrets Manager (``lightbulb/<env>/user-keys/<user>/<provider>``). The per-user ECS
task definition references the secret by ARN via ``secrets:[{name, valueFrom}]``.
No DynamoDB copy is kept — the secret name is deterministic from (user,
provider), and existence/ARN are read back with ``describe_secret``. Saving a
key first validates it against the provider's API, so bad keys are rejected at
save-time, not at chat-time.
"""

import logging
from typing import Optional

import boto3
import httpx

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret

logger = logging.getLogger(__name__)

# LLM provider keys: validated against the provider, and the plaintext is
# stored in Secrets Manager for ECS env-var injection.
SUPPORTED_LLM_PROVIDERS = {
    "openai": {"display_name": "OpenAI"},
    "anthropic": {"display_name": "Anthropic"},
}


def _allowed_ids() -> set[str]:
    return set(SUPPORTED_LLM_PROVIDERS)


def _secret_name(user_id: str, provider: str) -> str:
    """Per-user, per-provider Secrets Manager name.

    Must match the resource scope granted to the per-user ECS task role in
    ``service-stack.ts`` (``lightbulb/{env}/user-keys/*``).
    """
    env = settings.ENVIRONMENT or "dev"
    return f"lightbulb/{env}/user-keys/{user_id}/{provider}"


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

    # Defensive: caller should already have gated on _allowed_ids.
    raise ValueError(f"Unsupported LLM provider for validation: {provider}")


def _put_user_secret(*, user_id: str, provider: str, api_key_plaintext: str) -> str:
    """Create-or-update the per-user Secrets Manager secret. Returns the ARN.

    Boto3 is sync — caller wraps with ``run_in_thread`` if it needs to await.
    """
    name = _secret_name(user_id, provider)
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    create_or_update_secret(sm, name, api_key_plaintext)
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


def _describe_secret(user_id: str, provider: str) -> Optional[dict]:
    """Return the Secrets Manager describe-response for a user's LLM key, or
    None if the secret does not exist. Sync (boto3); wrap with run_in_thread."""
    name = _secret_name(user_id, provider)
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        return sm.describe_secret(SecretId=name)
    except sm.exceptions.ResourceNotFoundException:
        return None


async def get_secret_arn(user_id: str, provider: str) -> Optional[str]:
    """The Secrets Manager ARN for a user's BYO LLM key, or None if unset.

    The single source of truth for "does this owner have a BYO <provider> key":
    used by the provisioning path (ECS task ``secrets:``) and the change-provider
    gate. No DynamoDB lookup — the secret name is deterministic.
    """
    info = await run_in_thread(_describe_secret, user_id, provider)
    return info["ARN"] if info else None


def _get_secret_value(user_id: str, provider: str) -> Optional[str]:
    """The plaintext BYO key VALUE for user_id/provider, or None if absent.

    The SecretString IS the plaintext key — ``_put_user_secret`` stores it directly, no JSON
    wrapper. Sync (boto3); wrap with ``run_in_thread``.
    """
    name = _secret_name(user_id, provider)
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        return sm.get_secret_value(SecretId=name)["SecretString"]
    except sm.exceptions.ResourceNotFoundException:
        return None


async def read_byo_key(owner_id: str, provider: str) -> Optional[str]:
    """The plaintext BYO LLM key VALUE for ``owner_id``'s ``provider``, or None if unset.

    M4-R32. Unlike ``get_secret_arn`` (the ECS path's own need — a task-definition ``secrets:``
    reference AWS resolves by ARN, so the backend never reads the plaintext back), the Hermes
    fresh-provision path (``hermes_provisioning.write_profiles``) has no task-definition
    mechanism: it bakes the value directly into a per-agent profile ``.env`` uploaded to S3, so it
    must read the plaintext here.

    Requires ``secretsmanager:GetSecretValue`` on ``lightbulb/{env}/user-keys/*`` for the
    BACKEND's own task role (not any per-owner role) — already granted twice over in
    ``service-stack.ts``: the broad ``SecretsAccess`` statement (``lightbulb/{env}/*``) and the
    more specific ``UserKeysSecretsRW`` statement scoped exactly to this prefix. No CDK change
    needed.
    """
    return await run_in_thread(_get_secret_value, owner_id, provider)


class KeyService:
    """Manages user-provided LLM API keys (Secrets Manager-backed)."""

    async def set_key(self, user_id: str, tool_id: str, api_key: str) -> dict:
        if tool_id not in _allowed_ids():
            raise ValueError(f"Unsupported tool: {tool_id}")
        # Validate against the provider before storing anything.
        await _validate_llm_key(tool_id, api_key)
        # Store ONLY in Secrets Manager — the per-user ECS task references it by
        # ARN. There is no DynamoDB copy (every reader resolves the SM ARN).
        arn = await run_in_thread(
            _put_user_secret,
            user_id=user_id,
            provider=tool_id,
            api_key_plaintext=api_key,
        )
        return {"tool_id": tool_id, "secret_arn": arn}

    async def delete_key(self, user_id: str, tool_id: str) -> bool:
        """Force-delete the user's BYO LLM key from Secrets Manager.

        Returns True if a secret existed. Lenient on the id (no allowlist gate)
        so a stray provider name is a no-op, not an error — matching the prior
        behaviour the change-provider cleanup relies on."""
        existed = (await run_in_thread(_describe_secret, user_id, tool_id)) is not None
        try:
            await run_in_thread(_delete_user_secret, user_id, tool_id)
        except Exception:
            # Don't fail the user-facing delete — emit a metric so the orphan
            # secret can be swept by an operator job.
            logger.exception(
                "key_service: Secrets Manager delete failed for user=%s provider=%s",
                user_id,
                tool_id,
            )
            from core.observability.metrics import put_metric

            # tool_id is intentionally NOT a dimension — it is user-supplied /
            # unbounded (see _DENIED_DIMENSIONS); the provider is captured in the
            # log line above for diagnosis.
            put_metric("key_service.secret_delete_orphan")
        return existed

    async def list_keys(self, user_id: str) -> list[dict]:
        """List the user's configured BYO LLM keys (no values), from Secrets
        Manager. Each provider is probed by its deterministic secret name."""
        keys: list[dict] = []
        for provider, meta in SUPPORTED_LLM_PROVIDERS.items():
            info = await run_in_thread(_describe_secret, user_id, provider)
            if info is None:
                continue
            created = info.get("CreatedDate")
            keys.append(
                {
                    "tool_id": provider,
                    "display_name": meta["display_name"],
                    "created_at": created.isoformat() if created else None,
                }
            )
        return keys
