"""Issue workspace OAuth access tokens without exposing the stored refresh grant."""

import logging
import time
from datetime import datetime

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


async def _announce_reconnect(owner_id: str, provider_key: str, *, reconcile: bool = True) -> None:
    """Best-effort: the flip already stands, so a failure here costs visibility, never the call.
    ``reconcile=False`` for a provider with no MCP wiring (ChatGPT login): the item only."""
    from core.dynamodb import utc_now_iso
    from core.repositories import messaging_repo
    from core.services import website_logins
    from core.services.activity import reconnect_item_id
    from core.services.credentials import mcp_server
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    if reconcile:
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


def _expires_at(credential: dict) -> int | None:
    """Epoch seconds of ``token_expiry``; 0 when there is none; None when it is unreadable."""
    expiry = credential.get("token_expiry")
    if not expiry:
        return 0
    try:
        expires_at = datetime.fromisoformat(expiry)
    except (TypeError, ValueError):
        return None
    return int(expires_at.timestamp()) if expires_at.tzinfo is not None else None


async def get(owner_id: str, provider_key: str) -> str:
    """Return a fresh access token for a connected backend-refreshed OAuth provider."""
    return (await get_with_expiry(owner_id, provider_key))[0]


async def get_with_expiry(owner_id: str, provider_key: str) -> tuple[str, int]:
    """``(access_token, expires_at epoch s; 0 = unknown)`` for a connected backend-refreshed
    OAuth provider, refreshing it when it expires within ``provider.refresh_window_s``.

    For a ``custody: backend`` row, a token outside the window comes back after one read and no
    lease: N task VMs vending at once take the lease only when a refresh is actually due."""
    provider = registry.get_provider(provider_key)
    row = await integrations_repo.get(owner_id, provider_key)
    if provider_key == "google" and registry.refresh_owner_for(provider, row) == "actor":
        # gog keeps custody; the backend reads beside it from the same grant (google_grant).
        from core.services.credentials import google_grant

        if not row or row.get("status", "connected") != "connected":
            google_grant._grants.pop(owner_id, None)  # a disconnect ends the backend's copy too
            raise ValueError("google is not connected")
        return await google_grant.access_token(owner_id)
    if not provider.oauth or registry.refresh_owner_for(provider, row) != "backend":
        raise ValueError(f"{provider_key} is not a backend-refreshed OAuth provider")
    window = provider.refresh_window_s

    def _fresh(credential: dict | None) -> int | None:
        expires_at = _expires_at(credential or {})
        if not (credential or {}).get("access_token") or expires_at is None:
            return None
        if expires_at == 0:
            # Unknown expiry: fresh, except Instagram -- Meta drops its token for good at 60 days,
            # so an unrecorded expiry gets a refresh check (which records one).
            return None if provider.oauth.long_lived_exchange == "ig_exchange_token" else 0
        return expires_at if expires_at > time.time() + window else None

    # Only for custody rows: legacy backend providers keep today's lease-per-call path in prod.
    if row and row.get("custody") == "backend" and row.get("status", "connected") == "connected":
        credential = await integration_secret_store.get(owner_id, provider_key)
        expires_at = _fresh(credential)
        if expires_at is not None:
            return credential["access_token"], expires_at

    # ponytail: one owner-wide lease and Secrets Manager read per refresh keep
    # refresh correct; use provider leases if refreshes contend.
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        row = await integrations_repo.get(owner_id, provider_key)
        if not row or row.get("status", "connected") != "connected":
            raise ReconnectRequired(provider_key)
        # A rollback (custody -> actor) may have landed between the unleased read and the lease:
        # the VM owns the grant again, and X/QuickBooks refresh tokens are single-use.
        if registry.refresh_owner_for(provider, row) != "backend":
            raise ValueError(f"{provider_key} is no longer backend-refreshed")
        credential = await integration_secret_store.get(owner_id, provider_key)
        # A cut-over QuickBooks grant is a refresh token alone (their store never held an access
        # token): refresh it rather than calling it unusable.
        refreshable = row.get("custody") == "backend" and (credential or {}).get("refresh_token")
        if not credential or not (credential.get("access_token") or refreshable):
            await _mark_reconnect(owner_id, provider_key, "no_access_token")
            raise ReconnectRequired(provider_key)
        if _expires_at(credential) is None:
            await _mark_reconnect(owner_id, provider_key, "bad_expiry")
            raise ReconnectRequired(provider_key)
        if _fresh(credential) is None:
            # Instagram has no refresh token: its long-lived token refreshes itself.
            if not credential.get("refresh_token") and provider.oauth.long_lived_exchange != "ig_exchange_token":
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
        return credential["access_token"], _expires_at(credential) or 0
