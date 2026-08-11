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
write MUST invalidate that router's cached whsec_ lookup immediately, so a
just-armed (or just-rotated) trigger isn't left forwarding UNSIGNED any
longer than that cache's TTL bounds it to. See
``core/services/credentials/stripe_edge.py`` for the exact guarantee (an
immediate invalidate on a single-task deployment; a 60s-bounded window on a
multi-task fleet where the write and a later fire land on different tasks).

Before storing, it verifies two things about ``public_id``:

1. It actually resolves to a webhook trigger in the CALLER's own Paperclip
   company — reusing the same binding lookup ``routers/paperclip_trigger_proxy.py``
   uses to gate a fire. Today this ownership check is inert
   defense-in-depth (``owner_id`` is self-derived from the caller's own
   auth, so a write can only ever land in the caller's own Secrets Manager
   namespace, and the proxy's own read path re-checks the binding
   independently) — but it's a webhook-secret write endpoint, it should
   confirm ownership like the proxy does, and a foreign/typo'd
   ``public_id`` should 404 instead of silently reporting success.
2. The trigger was actually SEEDED as a ``stripe_edge`` trigger —
   ``core.services.inbound_triggers.seed_trigger_routine`` writes a marker
   into this trigger's Secrets Manager slot
   (``core/services/credentials/stripe_edge.py``'s
   ``STRIPE_EDGE_SEEDED_FIELD``) the instant it creates one, before the
   owner has pasted anything back. This is NOT the same check as Paperclip
   ``signingMode == "none"`` — that field is ALSO Phase 1's own
   deliberately-unsigned, user-selectable webhook option
   (``webhookSigning.ts``), so a legitimate unsigned Phase-1 trigger would
   also read ``"none"`` and, gated that way, would incorrectly accept a
   pasted Stripe whsec_ — which would then silently brick it: the trigger
   would start being held to a ``Stripe-Signature`` header no real sender
   of its will ever produce. Gating on the seed-time marker instead means
   only a trigger this module itself armed as stripe_edge can ever accept
   the paste.
"""

from __future__ import annotations

import logging

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
from routers import paperclip_trigger_proxy
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automations", tags=["automations"])


class FromIntegrationBody(BaseModel):
    provider: str
    event_id: str


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
        return await seed_trigger_routine(owner_id, body.provider, body.event_id)
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

    404s on a ``public_id`` that isn't a webhook trigger in the caller's own
    company; 400s if it IS one but was never seeded as a ``stripe_edge``
    trigger — see the module docstring for both. Invalidates the trigger
    proxy's cached lookup immediately after the store — see the module
    docstring for why that ordering is load-bearing.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)

    # Same path-injection / unbounded-cache-key guard the proxy fire route
    # uses (publicIds are 24-hex upstream).
    if not public_id.isalnum() or len(public_id) > 64:
        raise HTTPException(status_code=400, detail="invalid_public_id")

    try:
        found = await paperclip_trigger_proxy._resolve_trigger_binding(owner_id, public_id)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on resolver trouble, same as the proxy
        logger.warning("automations: trigger binding resolve failed owner=%s err=%s", owner_id, exc)
        raise HTTPException(status_code=503, detail="trigger_resolution_unavailable") from exc
    if not found:
        raise HTTPException(status_code=404, detail="unknown_trigger")

    # The stripe-edge-seeded gate — NOT Paperclip signingMode == "none",
    # which is ALSO Phase 1's own deliberately-unsigned option and would let
    # a legitimate unsigned trigger accept (and be bricked by) this paste.
    # Presence at this Secrets Manager slot is unambiguous: nothing but
    # seed_trigger_routine's seed-time marker or a previously-pasted whsec_
    # ever writes here — see the module docstring and stripe_edge.py's
    # STRIPE_EDGE_SEEDED_FIELD.
    existing = await integration_secret_store.get(owner_id, stripe_trigger_secret_provider(public_id))
    if existing is None:
        raise HTTPException(status_code=400, detail="not_a_stripe_edge_trigger")

    await integration_secret_store.put(
        owner_id,
        stripe_trigger_secret_provider(public_id),
        {WEBHOOK_SECRET_FIELD: body.webhook_secret},
    )
    invalidate_stripe_secret_cache(owner_id, public_id)
    return {"status": "stored"}
