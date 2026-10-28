"""Issue workspace OAuth access tokens without exposing the stored refresh grant."""

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from core.repositories import integrations_repo
from core.services import owner_lifecycle
from core.services.credentials import integration_secret_store, oauth_dance, registry

logger = logging.getLogger(__name__)


class ReconnectRequired(RuntimeError):
    """The workspace has no usable, refreshable grant for this provider."""


def _secret_values(value, key: str = "") -> list[str]:
    """Every string under a ``*token*`` / ``*secret*`` key, at any depth (a BYO credential can
    carry ``client_secret``, a DCR one ``client_information.client_secret``)."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _secret_values(v, str(k).lower())]
    if isinstance(value, list):
        return [s for v in value for s in _secret_values(v, key)]
    return [value] if isinstance(value, str) and value and ("token" in key or "secret" in key) else []


async def _refresh_reason(owner_id: str, provider_key: str, exc: Exception, credential: dict) -> str:
    """Why the provider refused the refresh, secret-free: the OAuth ``error`` (+ description)
    from the body, else the HTTP status. Every token or secret the refresh could have sent --
    the credential's own, and the OAuth app's client secret from its store -- is redacted, in
    case a provider echoes one back."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            reason = body["error"]
            if isinstance(body.get("error_description"), str):
                reason += f": {body['error_description']}"
        else:
            reason = f"http_{exc.response.status_code}"
    else:
        reason = f"refresh_error: {exc}"
    secrets = _secret_values(credential)
    try:
        secrets.append((await oauth_dance._oauth_client(owner_id, provider_key))[1])
    except Exception:  # noqa: BLE001 -- no app means no app secret to redact
        pass
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        reason = reason.replace(secret, "[redacted]")
    return reason[:200]


async def _mark_reconnect(owner_id: str, provider_key: str, reason: str = "unspecified") -> None:
    """Flip the row, then make the flip visible: agents' facts, the profiles' MCP entry, and one
    Needs-you item per member. Only the transition files the item, so a repeat stays silent."""
    from core.services.owner_facts import write_owner_facts

    row = await integrations_repo.get(owner_id, provider_key)
    first = (row or {}).get("status") != "reconnect_required"
    await integrations_repo.set_status(owner_id, provider_key, "reconnect_required", reason=reason)
    await write_owner_facts(owner_id)  # best-effort: drops it from the agents' connected_integrations
    if first:
        logger.warning(
            "integration reconnect_required owner=%s provider=%s reason=%s",
            owner_id,
            provider_key,
            reason,
            extra={"owner_id": owner_id, "provider": provider_key, "reconnect_reason": reason},
        )
        await _announce_reconnect(owner_id, provider_key)


async def _announce_reconnect(owner_id: str, provider_key: str) -> None:
    """Best-effort: the flip already stands, so a failure here costs visibility, never the call."""
    from core.dynamodb import utc_now_iso
    from core.repositories import messaging_repo
    from core.services import website_logins
    from core.services.activity import reconnect_item_id
    from core.services.credentials import mcp_server
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    try:
        await mcp_server.reconcile_integrations(owner_id)  # the profile build skips non-connected rows
    except Exception:  # noqa: BLE001
        logger.warning("reconnect profile reconcile failed owner=%s provider=%s", owner_id, provider_key)
    try:
        created = utc_now_iso()
        for user_id in await website_logins._humans_to_ask(owner_id, None):
            try:  # one member's failed write must not cost the others their item
                await messaging_repo.put_activity_row(
                    {
                        "pk": messaging_repo.activity_pk(owner_id, user_id),
                        "sk": messaging_repo.activity_sk(created, reconnect_item_id(provider_key)),
                        "kind": "reconnect",
                        "provider": provider_key,
                        "createdAt": created,
                        "expiresAt": int(time.time()) + website_logins.ACTIVITY_TTL_S,
                    }
                )
            except Exception:  # noqa: BLE001
                logger.warning("reconnect item write failed owner=%s user=%s", owner_id, user_id)
        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"}))
    except Exception:  # noqa: BLE001
        logger.warning("reconnect needs-you item failed owner=%s provider=%s", owner_id, provider_key)


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
            await _mark_reconnect(owner_id, provider_key, "no_access_token")
            raise ReconnectRequired(provider_key)

        expiry = credential.get("token_expiry")
        if expiry:
            try:
                expires_at = datetime.fromisoformat(expiry)
                if expires_at.tzinfo is None:
                    raise ValueError("naive token expiry")
            except (TypeError, ValueError) as exc:
                await _mark_reconnect(owner_id, provider_key, "bad_expiry")
                raise ReconnectRequired(provider_key) from exc
            if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=1):
                if not credential.get("refresh_token"):
                    await _mark_reconnect(owner_id, provider_key, "no_refresh_token")
                    raise ReconnectRequired(provider_key)
                try:
                    credential = await oauth_dance.refresh(owner_id, provider_key, credential)
                except ValueError as exc:
                    await _mark_reconnect(
                        owner_id, provider_key, await _refresh_reason(owner_id, provider_key, exc, credential)
                    )
                    raise ReconnectRequired(provider_key) from exc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (400, 401):
                        await _mark_reconnect(
                            owner_id, provider_key, await _refresh_reason(owner_id, provider_key, exc, credential)
                        )
                        raise ReconnectRequired(provider_key) from exc
                    raise
                await integration_secret_store.put(owner_id, provider_key, credential)
        return credential["access_token"]
