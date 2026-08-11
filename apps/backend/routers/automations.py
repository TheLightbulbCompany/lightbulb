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
companion: once the owner has pasted the proxy URL into their own Stripe
Dashboard, Stripe hands them a signing secret, and the frontend posts it
here. Storing it is what flips the trigger from "ordinary/unsigned" to
"Stripe edge-verified" in ``routers/paperclip_trigger_proxy.py`` — so the
write MUST invalidate that router's cached whsec_ lookup immediately, or a
just-armed (or just-rotated) trigger keeps forwarding UNSIGNED for up to
that cache's TTL. See ``core/services/credentials/stripe_edge.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services.credentials import integration_secret_store
from core.services.credentials.stripe_edge import (
    WEBHOOK_SECRET_FIELD,
    invalidate_stripe_secret_cache,
    stripe_trigger_secret_provider,
)
from core.services.inbound_triggers import seed_trigger_routine
from routers.teams._guards import require_always_on_entitlement

router = APIRouter(prefix="/automations", tags=["automations"])


class FromIntegrationBody(BaseModel):
    provider: str
    event_id: str
    repo: str | None = None


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
        return await seed_trigger_routine(owner_id, body.provider, body.event_id, repo=body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{public_id}/webhook-secret")
async def set_webhook_secret(
    public_id: str,
    body: WebhookSecretBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Store the Stripe-Dashboard-issued signing secret for one guided-paste
    trigger (the paste half of the guided flow — the frontend calls this
    once the owner has the ``whsec_`` Stripe showed them).

    Invalidates the trigger proxy's cached lookup immediately after the
    store — see the module docstring for why that ordering is load-bearing.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    await integration_secret_store.put(
        owner_id,
        stripe_trigger_secret_provider(public_id),
        {WEBHOOK_SECRET_FIELD: body.webhook_secret},
    )
    invalidate_stripe_secret_cache(owner_id, public_id)
    return {"status": "stored"}
