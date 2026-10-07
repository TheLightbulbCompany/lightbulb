"""Issue workspace OAuth access tokens without exposing the stored refresh grant."""

from datetime import datetime, timedelta, timezone

import httpx

from core.repositories import integrations_repo
from core.services import owner_lifecycle
from core.services.credentials import integration_secret_store, oauth_dance, registry


class ReconnectRequired(RuntimeError):
    """The workspace has no usable, refreshable grant for this provider."""


async def _mark_reconnect(owner_id: str, provider_key: str) -> None:
    from core.services.owner_facts import write_owner_facts

    await integrations_repo.set_status(owner_id, provider_key, "reconnect_required")
    await write_owner_facts(owner_id)  # best-effort: drops it from the agents' connected_integrations


async def get(owner_id: str, provider_key: str) -> str:
    """Return a fresh access token for a connected backend-refreshed OAuth provider."""
    provider = registry.get_provider(provider_key)
    if not provider.oauth or registry.refresh_owner_for(provider) != "backend":
        raise ValueError(f"{provider_key} is not a backend-refreshed OAuth provider")

    # ponytail: one owner-wide lease and Secrets Manager read per request keep
    # refresh correct; use provider leases/access-token caching if latency grows.
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        row = await integrations_repo.get(owner_id, provider_key)
        if not row or row.get("status", "connected") != "connected":
            raise ReconnectRequired(provider_key)
        credential = await integration_secret_store.get(owner_id, provider_key)
        if not credential or not credential.get("access_token"):
            await _mark_reconnect(owner_id, provider_key)
            raise ReconnectRequired(provider_key)

        expiry = credential.get("token_expiry")
        if expiry:
            try:
                expires_at = datetime.fromisoformat(expiry)
                if expires_at.tzinfo is None:
                    raise ValueError("naive token expiry")
            except (TypeError, ValueError) as exc:
                await _mark_reconnect(owner_id, provider_key)
                raise ReconnectRequired(provider_key) from exc
            if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=1):
                if not credential.get("refresh_token"):
                    await _mark_reconnect(owner_id, provider_key)
                    raise ReconnectRequired(provider_key)
                try:
                    credential = await oauth_dance.refresh(owner_id, provider_key, credential)
                except ValueError as exc:
                    await _mark_reconnect(owner_id, provider_key)
                    raise ReconnectRequired(provider_key) from exc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (400, 401):
                        await _mark_reconnect(owner_id, provider_key)
                        raise ReconnectRequired(provider_key) from exc
                    raise
                await integration_secret_store.put(owner_id, provider_key, credential)
        return credential["access_token"]
