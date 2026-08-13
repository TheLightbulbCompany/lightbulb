"""1-click Automations trigger seeding — "connect Stripe -> pick 'payment
failed' -> get a pre-filled automation."

``POST /automations/from-integration`` composes the existing routine
machinery via ``core/services/inbound_triggers.py``. Gated the same way as
every other trigger-creation path in the system:

- ``require_org_admin`` — wiring an integration trigger is an admin action,
  same posture as every mutating endpoint in ``routers/connections.py``.
- ``require_always_on_entitlement`` — a webhook trigger IS always-on
  automation (routers/teams/work.py's ``create_routine_trigger`` gates the
  exact same way, for the exact same reason: a trial container scales to
  zero and never wakes to serve a scheduled/triggered dispatch).

``PUT /automations/{public_id}/webhook-secret`` is the guided-paste
companion: once the owner has pasted the proxy URL into their Stripe
Dashboard, Stripe hands them a signing secret and the frontend posts it here.
It ARMS the trigger by rotating its Paperclip secret to that exact value
(``rotate_routine_trigger_secret(secret=...)`` -> the fork's caller-supplied
secret support), so Paperclip verifies the provider's native signature
against it. Verification is NATIVE — there is no Isol8-side secret store,
cache, or edge verification anymore (see ``routers/paperclip_trigger_proxy.py``).

Ownership: the ``public_id`` must resolve to a webhook trigger in the
CALLER's own Paperclip company (``_lookup_trigger_detail``); a foreign or
typo'd id 404s rather than silently succeeding. No separate "is this a Stripe
trigger" marker gate is needed — arming a non-Stripe webhook trigger is
harmless (a ``none``/``bearer`` trigger simply stores a secret its mode may
not use); the old marker existed only because verification used to be
edge-side and a stray paste could brick an unsigned trigger.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.services.inbound_triggers import (
    list_github_repos,
    list_google_accounts,
    list_vercel_projects,
    seed_trigger_routine,
)
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from routers import paperclip_trigger_proxy
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automations", tags=["automations"])


async def _resolve_trigger_id(owner_id: str, public_id: str) -> str | None:
    """Resolve ``public_id`` -> the internal trigger id in the caller's own
    Paperclip company, or None if no such webhook trigger exists there.

    Retries once on an expired admin session (401) — same posture as the fire
    proxy's ``_resolve_trigger_binding``, so a transient session expiry doesn't
    surface as a 503 the owner has to manually retry."""
    try:
        trigger = await paperclip_trigger_proxy._lookup_trigger_detail(owner_id, public_id)
    except PaperclipApiError as exc:
        if exc.status_code != 401:
            raise
        invalidate_admin_session()
        trigger = await paperclip_trigger_proxy._lookup_trigger_detail(owner_id, public_id)
    return trigger.get("id") if trigger else None


class FromIntegrationBody(BaseModel):
    provider: str
    event_id: str
    # Provider-side resource for auto-registering shapes — the "owner/repo"
    # to hook for auto_github. Guided-paste providers ignore it.
    resource: str | None = None


class WebhookSecretBody(BaseModel):
    webhook_secret: str = Field(min_length=1, max_length=512)


@router.post("/from-integration")
async def from_integration(
    body: FromIntegrationBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Seed a webhook-triggered routine from a connected integration's event.

    Returns ``seed_trigger_routine``'s dict verbatim — ``{routine_id,
    trigger_public_id, proxy_url, guided, instructions}``. Bad input
    (unknown provider/event, provider not wired as a trigger source, no
    active Paperclip company) surfaces as 400 rather than an opaque 500.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    try:
        return await seed_trigger_routine(owner_id, body.provider, body.event_id, resource=body.resource)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/providers/{provider}/resources")
async def provider_resources(
    provider: str,
    auth: AuthContext = Depends(get_current_user),
) -> list[dict]:
    """Provider-side resources the owner's connected token can see (GitHub
    repositories, Vercel projects, connected Google account emails) — feeds
    the create dialog's resource picker for the auto-registering registration
    shapes.

    Org-admin gated like ``from_integration``, but WITHOUT the always-on
    entitlement gate: listing resources is read-only and commits the owner to
    nothing. 404 for a provider with no lister or one that isn't connected;
    502 when the provider itself fails.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    # Built per-call, not at module level, so the module-level names stay the
    # patch points (tests replace routers.automations.list_github_repos).
    lister = {
        "github": list_github_repos,
        "vercel_cli": list_vercel_projects,
        "google": list_google_accounts,
    }.get(provider)
    if lister is None:
        raise HTTPException(status_code=404, detail="unknown_provider")
    try:
        return await lister(owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.warning("automations: %s resource listing failed owner=%s err=%s", provider, owner_id, exc)
        raise HTTPException(status_code=502, detail=f"{provider}_unavailable") from exc


@router.put("/{public_id}/webhook-secret")
async def set_webhook_secret(
    public_id: str,
    body: WebhookSecretBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Arm one guided-paste trigger with its provider-issued signing secret
    (the paste half of the guided flow — the frontend calls this once the
    owner has the ``whsec_`` Stripe showed them).

    Resolves ``public_id`` to a webhook trigger in the caller's own Paperclip
    company (404 otherwise), then rotates that trigger's Paperclip secret to
    the supplied value so Paperclip verifies the provider's native signature
    against it. 503 if Paperclip is unreachable.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)

    # Same path-injection / unbounded-key guard the proxy fire route uses
    # (publicIds are 24-hex upstream).
    if not public_id.isalnum() or len(public_id) > 64:
        raise HTTPException(status_code=400, detail="invalid_public_id")

    try:
        trigger_id = await _resolve_trigger_id(owner_id, public_id)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on resolver trouble, same as the proxy
        logger.warning("automations: trigger resolve failed owner=%s err=%s", owner_id, exc)
        raise HTTPException(status_code=503, detail="trigger_resolution_unavailable") from exc
    if not trigger_id:
        raise HTTPException(status_code=404, detail="unknown_trigger")

    if not settings.PAPERCLIP_INTERNAL_URL:
        raise HTTPException(status_code=503, detail="paperclip_unavailable")

    # Arm the trigger by rotating its Paperclip secret to the provider's value
    # (board actor via the bootstrap-admin session — agents can't set a
    # specific secret, see the fork's rotateTriggerSecret guard).
    try:
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
            cookie = await get_admin_session_cookie(http)
            client = PaperclipAdminClient(http_client=http)
            await client.rotate_routine_trigger_secret(
                session_cookie=cookie, trigger_id=trigger_id, secret=body.webhook_secret
            )
    except Exception as exc:  # noqa: BLE001 — Paperclip unreachable / rotate failed
        logger.warning("automations: arm webhook secret failed owner=%s trigger=%s err=%s", owner_id, trigger_id, exc)
        raise HTTPException(status_code=503, detail="arm_secret_unavailable") from exc

    return {"status": "stored"}
