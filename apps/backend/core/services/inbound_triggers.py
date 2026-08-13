"""Seed a real webhook-triggered Paperclip routine from a registry-declared
inbound integration event — the backend seam behind "connect Stripe -> pick
'payment failed' -> get a pre-filled automation."

Composes the same two-call ``create_routine`` -> ``create_routine_trigger``
sequence ``core/services/agent_routine_provisioning.py`` uses for scheduled
routines (reuses its routine-body builder directly), driven by the registry
(``core/services/credentials/registry.py``) instead of a hardcoded spec list.

Three inbound shapes exist:

``webhook_registration: "guided_paste"`` (Stripe/Sentry/Linear/Attio): Isol8
has no reachable register-a-webhook API for the provider (creating a Stripe
endpoint needs a restricted API key we don't hold), so the owner pastes the
returned ``proxy_url`` into the provider's own dashboard, then pastes the
secret it hands back via ``PUT /automations/{public_id}/webhook-secret``
(``routers/automations.py``) — THAT call ARMS the trigger by rotating its
Paperclip secret to the pasted value. Verification is NATIVE: the trigger is
created with a native ``signingMode`` straight from the registry (Stripe ->
``hmac_sha256``, whose ``{t}.{body}`` scheme is Stripe's exact one), and
Paperclip verifies the provider's signature against that armed secret. Until
it's armed the trigger holds a random placeholder secret, so a provider
signature can't match and nothing fires — it fails closed for free, no marker
needed.

``webhook_registration: "auto_github"`` (GitHub): the owner's connected gh
token carries ``repo`` scope, which is enough to create repo webhooks, so the
seed registers the hook itself — the trigger create's one-time
``secretMaterial.webhookSecret`` becomes the GitHub hook's HMAC secret, and
Paperclip's ``github_hmac`` mode reads GitHub's own ``X-Hub-Signature-256``
natively. No paste step: the automation is live when the seed returns. Known
limitation: the GitHub hook's id is NOT stored (recoverable via GitHub's
list-hooks API by URL), so deleting the automation leaves the hook behind as
an orphan — it fails closed at Paperclip (the trigger is gone, deliveries
404) and GitHub disables hooks after repeated delivery failures.

``webhook_registration: "auto_vercel"`` (Vercel): auto like GitHub, but the
secret flows the OPPOSITE direction — Vercel GENERATES the webhook secret,
shown once in the create response. So the seed creates the trigger (its own
secretMaterial goes unused; the trigger holds a random placeholder), creates
the Vercel webhook on the named project, then ARMS the trigger by rotating
its Paperclip secret to Vercel's value. Vercel signs HMAC-SHA1 over the raw
body (hex, no prefix, ``x-vercel-signature``), verified by Paperclip's
``hmac_sha1`` mode via the trigger proxy's generic X-Paperclip-Signature
header copy. If the ARM fails the Vercel webhook is deleted (best-effort)
before the routine is unwound — never leave a live webhook pointed at a
trigger armed with a random secret. The webhook id is likewise not stored.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.agent_routine_provisioning import _build_routine_body
from core.services.credentials import integration_secret_store, registry
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"

# "owner/repo" — the only resource shape auto_github accepts. Also a
# path-injection fence: the resource is interpolated into the GitHub hooks URL.
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

_VERCEL_API_BASE = "https://api.vercel.com"

# A Vercel project id ("prj_…") — the only resource shape auto_vercel accepts.
# Also an injection fence: the resource rides into the webhook create's
# projectIds and the rollback DELETE path.
_VERCEL_PROJECT_RE = re.compile(r"^[A-Za-z0-9_]+$")

# `inbound.signing_mode` in the registry IS a native Paperclip signingMode
# now (e.g. Stripe -> "hmac_sha256"); it's passed straight through to the
# created trigger. Paperclip verifies natively against the trigger's secret,
# which the webhook-secret PUT arms with the provider's own value.
_DEFAULT_REPLAY_WINDOW_SEC = 300

# The signingModes Paperclip verifies natively — an inbound.signing_mode must
# be one of these (validated at seed time so a providers.yaml typo fails with a
# clear error instead of an opaque upstream 4xx).
_NATIVE_SIGNING_MODES = {"bearer", "hmac_sha256", "hmac_sha1", "github_hmac", "none"}


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


async def _github_token(owner_id: str) -> str | None:
    """The owner's stored GitHub credential, or None when not connected.
    Same dual shape the gh_cli materializer handles: the paste rail stores
    {"token": ...}, the direct_oauth rail stores "access_token"."""
    credential = await integration_secret_store.get(owner_id, "github")
    if not credential:
        return None
    return credential.get("token") or credential.get("access_token")


async def list_github_repos(owner_id: str) -> list[dict[str, str]]:
    """Repos the owner's gh token can see — feeds the create dialog's
    repository picker for the auto_github registration shape.

    Raises ``LookupError`` when GitHub isn't connected (the router maps it to
    404); httpx errors bubble up for the router's 502.
    """
    token = await _github_token(owner_id)
    if not token:
        raise LookupError("GitHub is not connected")
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(
            f"{_GITHUB_API_BASE}/user/repos",
            params={"per_page": 100, "sort": "updated"},
            headers={**_GITHUB_HEADERS, "Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return [{"id": r["full_name"], "label": r["full_name"]} for r in resp.json()]


async def _create_github_hook(*, token: str, resource: str, proxy_url: str, events: list[str], secret: str) -> None:
    """Create the repo webhook pointed at the trigger proxy, signed with the
    trigger's own secret. Raises ``ValueError`` with a caller-facing message
    on the expected refusals (no admin on the repo, duplicate hook)."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.post(
            f"{_GITHUB_API_BASE}/repos/{resource}/hooks",
            headers={**_GITHUB_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "config": {"url": proxy_url, "content_type": "json", "secret": secret},
                "events": events,
                "active": True,
            },
        )
    # 422 = a hook for this exact URL already exists on the repo (each seed
    # mints a fresh trigger, so this means a previous half-finished attempt).
    if resp.status_code == 422:
        raise ValueError(f"A webhook for this URL already exists on {resource}")
    # GitHub answers 404 (not 403) when the token can see the repo but can't
    # administer it, so both map to the same caller-facing message.
    if resp.status_code in (403, 404):
        raise ValueError(f"Isol8 couldn't create the webhook on {resource} — you need admin access to the repository")
    resp.raise_for_status()


async def _vercel_token(owner_id: str) -> str | None:
    """The owner's stored Vercel credential, or None when not connected.
    vercel_cli is paste-rail only, so the shape is always {"token": ...}."""
    credential = await integration_secret_store.get(owner_id, "vercel_cli")
    if not credential:
        return None
    return credential.get("token")


async def list_vercel_projects(owner_id: str) -> list[dict[str, str]]:
    """Projects the owner's Vercel token can see — feeds the create dialog's
    project picker for the auto_vercel registration shape. No teamId param:
    a pasted personal token's default scope is the right one, and a
    team-scoped failure surfaces as the endpoint's 502.

    Raises ``LookupError`` when Vercel isn't connected (the router maps it to
    404); httpx errors bubble up for the router's 502.
    """
    token = await _vercel_token(owner_id)
    if not token:
        raise LookupError("Vercel is not connected")
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(
            f"{_VERCEL_API_BASE}/v9/projects",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return [{"id": p["id"], "label": p["name"]} for p in resp.json().get("projects", [])]


async def _create_vercel_webhook(*, token: str, resource: str, proxy_url: str, events: list[str]) -> tuple[str, str]:
    """Create the project webhook pointed at the trigger proxy. Returns the
    webhook's ``(id, secret)`` — Vercel generates the secret and shows it only
    here, and it is what ARMS the trigger. Raises ``ValueError`` with a
    caller-facing message on the expected refusal (webhook creation needs a
    full-access token on a Pro/Enterprise team)."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.post(
            f"{_VERCEL_API_BASE}/v1/webhooks",
            headers={"Authorization": f"Bearer {token}"},
            json={"url": proxy_url, "events": events, "projectIds": [resource]},
        )
    if resp.status_code == 403:
        raise ValueError(
            "Isol8 couldn't create the Vercel webhook — your Vercel plan or token can't create webhooks "
            "(a full-access token on a Pro or Enterprise team is required)"
        )
    resp.raise_for_status()
    data = resp.json()
    webhook_id = data.get("id")
    secret = data.get("secret")
    if not webhook_id or not secret:
        raise RuntimeError("Vercel webhook create returned no id/secret")
    return webhook_id, secret


async def _delete_vercel_webhook(*, token: str, webhook_id: str) -> None:
    """Best-effort delete of a Vercel webhook orphaned by the ARM step failing
    — a live webhook pointed at a trigger still holding its random placeholder
    secret would fail signature checks on every delivery, forever. Never masks
    the caller's own error — only logs its own failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.delete(
                f"{_VERCEL_API_BASE}/v1/webhooks/{webhook_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
    except Exception:
        logger.exception("inbound_triggers: rollback delete of Vercel webhook %s failed", webhook_id)


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
    resource: str | None = None,
) -> dict[str, Any]:
    """Create a routine + webhook trigger pre-filled from ``provider_key``'s
    registry ``inbound`` declaration, for the event named ``event_id``.

    Returns ``{routine_id, trigger_public_id, proxy_url, guided, instructions,
    secret_hint}``; the auto shapes add ``resource`` and need one — the
    "owner/repo" to hook for auto_github, the project id for auto_vercel
    (guided providers ignore it).

    Raises ``ValueError`` for any caller-facing input problem (unknown
    provider/event, provider not wired as a trigger source, missing/invalid
    resource, GitHub/Vercel refusing the webhook, no active Paperclip company,
    no resolvable suggested agent) — the router maps these to a 4xx. Raises
    ``RuntimeError`` on a malformed upstream response (Paperclip is expected
    to always return an id / publicId / secretMaterial).
    """
    provider = registry.get_provider(provider_key)
    inbound = provider.inbound
    if inbound is None:
        raise ValueError(f"{provider_key!r} is not wired as an Automations trigger source")

    event = next((e for e in inbound.events if e.id == event_id), None)
    if event is None:
        raise ValueError(f"unknown event {event_id!r} for provider {provider_key!r}")

    # auto_github preconditions up front — a missing repo or token must fail
    # before anything exists upstream, not after a create/rollback cycle.
    gh_token: str | None = None
    if inbound.webhook_registration == "auto_github":
        if not resource or not _GITHUB_REPO_RE.match(resource):
            raise ValueError("auto_github needs a repository in the form owner/repo")
        gh_token = await _github_token(owner_id)
        if not gh_token:
            raise ValueError("GitHub is not connected — connect it in the Integrations tab first")

    # auto_vercel preconditions, same posture: fail before anything exists.
    vercel_token: str | None = None
    if inbound.webhook_registration == "auto_vercel":
        if not resource or not _VERCEL_PROJECT_RE.match(resource):
            raise ValueError("auto_vercel needs a Vercel project id (prj_…)")
        vercel_token = await _vercel_token(owner_id)
        if not vercel_token:
            raise ValueError("Vercel is not connected — connect it in the Integrations tab first")

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
            # The auto path arms the GitHub hook with the trigger's own secret,
            # shown ONCE in the create response — absent means the trigger is
            # unusable for auto registration, same severity as no publicId.
            webhook_secret: str | None = None
            if inbound.webhook_registration == "auto_github":
                material = trigger_resp.get("secretMaterial") if isinstance(trigger_resp, dict) else None
                webhook_secret = material.get("webhookSecret") if isinstance(material, dict) else None
                if not webhook_secret:
                    raise RuntimeError("create_routine_trigger returned no secretMaterial.webhookSecret")
            # The auto_vercel path arms the trigger AFTER the Vercel webhook
            # exists (Vercel generates the secret), via rotate-secret — which
            # takes the INTERNAL trigger id, only present here in the create
            # response. Absent means the trigger can never be armed, same
            # severity as no publicId.
            trigger_id: str | None = None
            if inbound.webhook_registration == "auto_vercel":
                trigger_id = trigger.get("id") if isinstance(trigger, dict) else None
                if not trigger_id:
                    raise RuntimeError("create_routine_trigger returned no trigger.id")
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
            # Per-provider copy from the registry when present — every dashboard
            # buries webhooks somewhere different, and a wrong path strands the
            # owner mid-setup. The generic sentence is only the fallback.
            if inbound.instructions:
                instructions = inbound.instructions.format(
                    proxy_url=proxy_url, event_label=event.label, event_id=event.id
                )
            else:
                instructions = (
                    f"In your {provider.display_name} dashboard, add a webhook endpoint "
                    f"pointed at {proxy_url} listening for the '{event.label}' event "
                    f"({event.id}). Paste the signing secret it gives you back into Isol8 "
                    "to finish arming this automation."
                )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": True,
                "instructions": instructions,
                # Placeholder for the paste-secret input ("whsec_…" for Stripe,
                # "Client Secret from the integration" for Sentry) — a
                # Stripe-shaped hint on a Linear secret reads as an error.
                "secret_hint": inbound.secret_hint,
            }

        if inbound.webhook_registration == "auto_github":
            assert resource and gh_token and webhook_secret  # narrowed by the pre-checks above
            try:
                await _create_github_hook(
                    token=gh_token,
                    resource=resource,
                    proxy_url=proxy_url,
                    # Provider-side hook events from the registry; the curated
                    # id itself is the fallback when they coincide.
                    events=event.provider_events or [event.id],
                    secret=webhook_secret,
                )
            except Exception:
                # GitHub refused the hook (no admin on the repo, duplicate,
                # outage) — the routine+trigger are unusable, unwind them.
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            logger.info(
                "inbound_triggers: seeded auto_github routine=%s trigger=%s owner=%s repo=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id,
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": False,
                "instructions": None,
                "secret_hint": None,
                "resource": resource,
            }

        if inbound.webhook_registration == "auto_vercel":
            assert resource and vercel_token and trigger_id  # narrowed by the pre-checks above
            try:
                webhook_id, vercel_secret = await _create_vercel_webhook(
                    token=vercel_token,
                    resource=resource,
                    proxy_url=proxy_url,
                    # Provider-side webhook events from the registry; the
                    # curated id itself is the fallback when they coincide.
                    events=event.provider_events or [event.id],
                )
            except Exception:
                # Vercel refused the webhook (plan/token can't create them,
                # outage) — the routine+trigger are unusable, unwind them.
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            try:
                # ARM the trigger with Vercel's generated secret — the
                # opposite direction from GitHub, where the trigger's own
                # secret seeds the hook. Until this lands the trigger holds a
                # random placeholder, so deliveries fail closed.
                await admin.rotate_routine_trigger_secret(
                    session_cookie=cookie, trigger_id=trigger_id, secret=vercel_secret
                )
            except Exception:
                # The webhook is live but the trigger was never armed with its
                # secret — delete it (best-effort) before unwinding, or Vercel
                # would deliver into signature failures forever.
                await _delete_vercel_webhook(token=vercel_token, webhook_id=webhook_id)
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            logger.info(
                "inbound_triggers: seeded auto_vercel routine=%s trigger=%s owner=%s project=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id,
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": False,
                "instructions": None,
                "secret_hint": None,
                "resource": resource,
            }

        # No other registration shape exists; the routine+trigger were
        # already created, so unwind before surfacing the misconfiguration.
        await _rollback_routine(
            admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
        )
        raise ValueError(f"unsupported inbound.webhook_registration {inbound.webhook_registration!r}")
