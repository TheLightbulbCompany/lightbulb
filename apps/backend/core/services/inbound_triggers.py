"""Seed a real webhook-triggered Paperclip routine from a registry-declared
inbound integration event — the backend seam behind "connect Stripe -> pick
'payment failed' -> get a pre-filled automation."

Composes the same two-call ``create_routine`` -> ``create_routine_trigger``
sequence ``core/services/agent_routine_provisioning.py`` uses for scheduled
routines (reuses its routine-body builder directly), driven by the registry
(``core/services/credentials/registry.py``) instead of a hardcoded spec list.

Today the only inbound shape is ``webhook_registration: "guided_paste"``
(Stripe): Isol8 has no reachable register-a-webhook API for the provider
(creating a Stripe endpoint needs a restricted API key we don't hold), so the
owner pastes the returned ``proxy_url`` into the provider's own dashboard, then
pastes the ``whsec_`` it hands back via ``PUT /automations/{public_id}/webhook-
secret`` (``routers/automations.py``) — THAT call ARMS the trigger by rotating
its Paperclip secret to the pasted value. Verification is NATIVE: the trigger
is created with a native ``signingMode`` straight from the registry (Stripe ->
``hmac_sha256``, whose ``{t}.{body}`` scheme is Stripe's exact one), and
Paperclip verifies the provider's signature against that armed secret. Until
it's armed the trigger holds a random placeholder secret, so a provider
signature can't match and nothing fires — it fails closed for free, no marker
needed.

(An ``"auto"`` shape — where Isol8 registers the webhook itself via the
provider's own API — was prototyped for GitHub and removed: native GitHub
verification is fine, but reliable auto-registration needs webhook-lifecycle
handling (ping deliveries, redelivery idempotency, hook cleanup ordering) that
belongs in its own focused change. It returns with the generic-rail follow-up.)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.agent_routine_provisioning import _build_routine_body
from core.services.credentials import registry
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

# `inbound.signing_mode` in the registry IS a native Paperclip signingMode
# now (e.g. Stripe -> "hmac_sha256"); it's passed straight through to the
# created trigger. Paperclip verifies natively against the trigger's secret,
# which the webhook-secret PUT arms with the provider's own value.
_DEFAULT_REPLAY_WINDOW_SEC = 300

# The signingModes Paperclip verifies natively — an inbound.signing_mode must
# be one of these (validated at seed time so a providers.yaml typo fails with a
# clear error instead of an opaque upstream 4xx).
_NATIVE_SIGNING_MODES = {"bearer", "hmac_sha256", "github_hmac", "none"}


async def _company_id_for_owner(owner_id: str) -> str | None:
    """Same resolution ``routers.paperclip_trigger_proxy._company_id_for_owner``
    uses, duplicated here rather than imported across the router/service
    boundary (services must not depend on routers)."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    if is_org_owner_id(owner_id):
        return await repo.get_org_company_id(owner_id)
    row = await repo.get(owner_id)
    return row.company_id if row is not None and row.status == "active" else None


def _build_webhook_trigger_body(*, signing_mode: str, replay_window_sec: int, label: str) -> dict[str, Any]:
    """The webhook-kind create_routine_trigger body — the ``kind="webhook"``
    counterpart to ``agent_routine_provisioning.build_dormant_trigger_body``'s
    ``kind="schedule"``. Always enabled (upstream's own default): unlike a
    scheduled routine there is no "arm it later" step here — the trigger is
    live as soon as it exists, gated instead by whether the sender can reach
    it (guided_paste needs the owner to actually paste the URL into their
    dashboard first)."""
    return {
        "kind": "webhook",
        "signingMode": signing_mode,
        "replayWindowSec": replay_window_sec,
        "label": label,
    }


async def _rollback_routine(
    admin: PaperclipAdminClient, *, routine_id: str, cookie: str, owner_id: str, provider_key: str
) -> None:
    """Best-effort delete of a routine orphaned by a later step in
    ``seed_trigger_routine`` failing (trigger create/validation, or the
    stripe-edge seed marker). Never masks the caller's own error — only
    logs its own failure — so every call site still re-raises the original
    exception right after this returns."""
    try:
        await admin.delete_routine(routine_id=routine_id, session_cookie=cookie)
    except Exception:
        logger.exception(
            "inbound_triggers: rollback delete_routine failed for routine=%s owner=%s provider=%s",
            routine_id,
            owner_id,
            provider_key,
        )


async def seed_trigger_routine(
    owner_id: str,
    provider_key: str,
    event_id: str,
) -> dict[str, Any]:
    """Create a routine + webhook trigger pre-filled from ``provider_key``'s
    registry ``inbound`` declaration, for the event named ``event_id``.

    Returns ``{routine_id, trigger_public_id, proxy_url, guided, instructions}``.
    ``instructions`` carries the guided-paste copy (Stripe today).

    Raises ``ValueError`` for any caller-facing input problem (unknown
    provider/event, provider not wired as a trigger source, no active
    Paperclip company, no resolvable suggested agent) — the router maps
    these to a 4xx. Raises ``RuntimeError`` on a malformed upstream
    response (Paperclip is expected to always return an id / publicId).
    """
    provider = registry.get_provider(provider_key)
    inbound = provider.inbound
    if inbound is None:
        raise ValueError(f"{provider_key!r} is not wired as an Automations trigger source")

    event = next((e for e in inbound.events if e.id == event_id), None)
    if event is None:
        raise ValueError(f"unknown event {event_id!r} for provider {provider_key!r}")

    # inbound.signing_mode IS a native Paperclip signingMode, passed straight
    # through to the created trigger.
    if inbound.signing_mode not in _NATIVE_SIGNING_MODES:
        raise ValueError(f"unsupported inbound.signing_mode {inbound.signing_mode!r}")

    # Only "friday" is used by any live registry template today (Stripe's).
    # Extend this when a second suggested_agent shows up.
    if inbound.template.suggested_agent != "friday":
        raise ValueError(f"no agent resolver for suggested_agent={inbound.template.suggested_agent!r}")

    company_id = await _company_id_for_owner(owner_id)
    if not company_id:
        raise ValueError(f"no active Paperclip company for owner {owner_id!r}")

    title = inbound.template.title.format(event_label=event.label)
    description = inbound.template.starter_prompt.format(event_label=event.label)

    # Lazy import: keeps this module's load light and sidesteps a cycle —
    # same convention agent_export.py uses for the identical import.
    from core.services.company_deploy_service import _find_friday_id

    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        cookie = await get_admin_session_cookie(http)
        admin = PaperclipAdminClient(http_client=http)

        agents = await admin.list_agents(session_cookie=cookie, company_id=company_id)
        agent_id = _find_friday_id(agents)
        if agent_id is None:
            raise ValueError(f"no CEO agent (Friday) found in company {company_id!r}")

        routine = await admin.create_routine(
            session_cookie=cookie,
            company_id=company_id,
            body=_build_routine_body(title=title, description=description, agent_id=agent_id),
        )
        routine_id = routine.get("id") if isinstance(routine, dict) else None
        if not routine_id:
            raise RuntimeError("create_routine returned no id")

        try:
            trigger_resp = await admin.create_routine_trigger(
                session_cookie=cookie,
                routine_id=routine_id,
                body=_build_webhook_trigger_body(
                    signing_mode=inbound.signing_mode,
                    replay_window_sec=_DEFAULT_REPLAY_WINDOW_SEC,
                    label=title,
                ),
            )

            trigger = trigger_resp.get("trigger") if isinstance(trigger_resp, dict) else None
            public_id = trigger.get("publicId") if isinstance(trigger, dict) else None
            if not public_id:
                raise RuntimeError("create_routine_trigger returned no trigger.publicId")
        except Exception:
            # The routine already exists upstream at this point. If the
            # trigger call itself throws (timeout, upstream error) or comes
            # back without a usable publicId, that routine is an orphan the
            # owner can't see or arm — unwind it here before re-raising.
            await _rollback_routine(
                admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
            )
            raise

        proxy_url = f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}"

        if inbound.webhook_registration == "guided_paste":
            logger.info(
                "inbound_triggers: seeded guided_paste routine=%s trigger=%s owner=%s provider=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                provider_key,
                event_id,
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": True,
                "instructions": (
                    f"In your {provider.display_name} dashboard, add a webhook endpoint "
                    f"pointed at {proxy_url} listening for the '{event.label}' event "
                    f"({event.id}). Paste the signing secret it gives you back into Isol8 "
                    "to finish arming this automation."
                ),
            }

        # Only guided_paste is implemented today; the routine+trigger were
        # already created, so unwind before surfacing the misconfiguration.
        await _rollback_routine(
            admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
        )
        raise ValueError(f"unsupported inbound.webhook_registration {inbound.webhook_registration!r}")
