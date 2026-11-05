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
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.services.inbound_triggers import (
    list_github_repos,
    list_google_accounts,
    list_instagram_accounts,
    list_meta_pages,
    list_vercel_projects,
    seed_trigger_routine,
)
from core.services import automation_hooks
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.super_agent_flags import flags_for
from routers import paperclip_trigger_proxy
from routers.teams._guards import WEBHOOK_AUTOMATIONS_MESSAGE, require_always_on_entitlement

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
    # The events this ONE trigger is armed for. A Paperclip webhook trigger has
    # no event filter and every fan-out provider lets one endpoint subscribe to
    # many events, so picking three Stripe events is one URL to paste, not
    # three. All of them must share a registration shape — the seed 400s with
    # the conflicting ids otherwise.
    # 300 comfortably clears the largest catalog in the registry (Stripe's
    # ~265 events) while still bounding the request.
    event_ids: list[str] | None = Field(default=None, min_length=1, max_length=300)
    # Deprecated single-event alias, mapped into `event_ids` below. Kept so
    # callers written against the one-event shape keep working; send one or the
    # other, never both.
    event_id: str | None = Field(default=None, min_length=1)
    # Provider-side resource for auto-registering shapes — the "owner/repo"
    # to hook for auto_github. Guided-paste providers ignore it.
    resource: str | None = None
    # The owner's own Pub/Sub topic ("projects/<proj>/topics/<name>") — only
    # the owner_setup shape (gmail.message) reads it.
    topic: str | None = None
    # Who the seeded routine lands on. Omitted, the seed resolves the owner's
    # Friday as before. Validated against the caller's own Paperclip company
    # roster inside the seed — never trusted from here.
    assignee_agent_id: str | None = None
    # ATTACH instead of create: arm this event on a routine the owner already
    # wrote, leaving its assignee/reviewers/variables/schedule untouched and
    # its instructions intact (the shapes that need maintenance text APPEND a
    # delimited block). Validated against the caller's own company's routines
    # inside the seed — never trusted from here.
    routine_id: str | None = None
    # What the owner typed over the pre-filled draft in the create dialog.
    # The frontend sends these ONLY when the draft was actually edited, so the
    # 1-click path stays byte-identical to the registry template.
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def _exactly_one_event_source(self) -> "FromIntegrationBody":
        if (self.event_ids is None) == (self.event_id is None):
            raise ValueError("send event_ids (preferred) or the deprecated event_id, not both and not neither")
        return self

    def events(self) -> list[str]:
        """The armed event list either spelling resolves to, deduped in the
        order the caller sent them (the first is the primary event)."""
        return list(dict.fromkeys(self.event_ids if self.event_ids is not None else [self.event_id or ""]))


class WebhookSecretBody(BaseModel):
    webhook_secret: str = Field(min_length=1, max_length=512)


@router.post("/from-integration")
async def from_integration(
    body: FromIntegrationBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Seed a webhook-triggered routine from a connected integration's events
    — or, with ``routine_id``, arm them on a routine the owner already wrote
    (same gating, same provider-side registration, nothing the owner wrote
    overwritten).

    ``event_ids`` is a multi-select: ONE trigger, one URL, every event
    registered on it. ``event_id`` is the deprecated one-event alias.

    ``title``/``description`` are the owner's edits to the pre-filled draft;
    omitted, the registry template renders exactly as it always has.

    Returns ``seed_trigger_routine``'s dict verbatim — ``{routine_id,
    trigger_public_id, proxy_url, guided, instructions}``. Bad input
    (unknown provider/event, provider not wired as a trigger source, no
    active Paperclip company, an assignee or target routine outside the
    caller's company) surfaces as 400 rather than an opaque 500.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=WEBHOOK_AUTOMATIONS_MESSAGE)
    try:
        return await seed_trigger_routine(
            owner_id,
            body.provider,
            body.events(),
            resource=body.resource,
            topic=body.topic,
            assignee_agent_id=body.assignee_agent_id,
            routine_id=body.routine_id,
            title=body.title,
            description=body.description,
            actor_user_id=auth.user_id,
        )
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
        "meta_ads": list_meta_pages,
        "instagram": list_instagram_accounts,
    }.get(provider)
    if lister is None:
        raise HTTPException(status_code=404, detail="unknown_provider")
    try:
        return await lister(owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # RuntimeError = the provider answered 200 with a body we don't recognise
    # (Meta's /me/accounts without a `data` list). Same class of "the provider
    # is not behaving" as an HTTP error, and far better than guessing a shape.
    except (httpx.HTTPError, RuntimeError) as exc:
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

    # Super-agent workspaces: the secret goes to Secrets Manager and the HOOK# row is armed (lane A, Task 8).
    # No HOOK# row = still a Paperclip trigger: today's path below.
    if await flags_for(owner_id) and await automation_hooks.arm(owner_id, public_id, body.webhook_secret):
        return {"status": "stored"}

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
