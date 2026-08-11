"""Seed a real webhook-triggered Paperclip routine from a registry-declared
inbound integration event — the backend seam behind "connect Stripe -> pick
'payment failed' -> get a pre-filled automation."

Composes the same two-call ``create_routine`` -> ``create_routine_trigger``
sequence ``core/services/agent_routine_provisioning.py`` uses for scheduled
routines (reuses its routine-body builder directly), driven by the registry
(``core/services/credentials/registry.py``) instead of a hardcoded spec list.

Two ``inbound.webhook_registration`` shapes:
  - ``"guided_paste"`` (Stripe, live today) — Isol8 has no reachable
    register-a-webhook API for this provider (creating a Stripe endpoint
    needs a restricted API key we don't hold), so the owner pastes the
    returned ``proxy_url`` into the provider's own dashboard, then pastes
    the ``whsec_`` it hands back via ``PUT /automations/{public_id}/webhook-
    secret`` (``routers/automations.py``) — THAT write is what actually arms
    Stripe-edge verification in ``routers/paperclip_trigger_proxy.py``. This
    module never stores that secret itself, but for any ``signing_mode:
    "stripe_edge"`` provider it DOES mark the trigger as stripe-edge-seeded
    the instant it's created (see ``core/services/credentials/stripe_edge.py``'s
    ``STRIPE_EDGE_SEEDED_FIELD``) — that marker, not Paperclip's own
    ``signingMode``, is what the webhook-secret PUT gates on, so a
    deliberately-unsigned Phase 1 trigger (also ``signingMode: "none"``)
    can never be bricked by an errant paste.
  - ``"auto"`` (GitHub) — Isol8 registers the webhook itself via the
    provider's own API (``_auto_register_webhook``, ``POST
    /repos/{repo}/hooks``) using the owner's stored GitHub token, so there is
    no owner paste step at all. If registration fails (missing repo, no
    token, GitHub API error), the routine+trigger already created upstream
    are rolled back — see ``_auto_register_webhook``'s docstring — so a
    failed GitHub setup never leaves an orphaned, unreachable routine. On
    success, the registered hook's id + repo are retained (best-effort,
    keyed by the trigger's ``public_id`` — see ``cleanup_github_hook``) so
    deleting the routine/trigger later can unregister it instead of leaving
    a live GitHub webhook delivering to a dead proxy forever.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.agent_routine_provisioning import _build_routine_body
from core.services.credentials import integration_secret_store, registry
from core.services.credentials.stripe_edge import STRIPE_EDGE_SEEDED_FIELD, stripe_trigger_secret_provider
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

# Registry `inbound.signing_mode` -> the trigger's OWN Paperclip `signingMode`.
# "stripe_edge" arms Paperclip with "none" because verification happens at the
# Isol8 edge instead (routers/paperclip_trigger_proxy.py); "github_hmac" lets
# Paperclip verify natively. Registry-level marker, never forwarded to
# Paperclip verbatim — see registry.Inbound's docstring.
_PAPERCLIP_SIGNING_MODE = {"stripe_edge": "none", "github_hmac": "github_hmac"}

# Stripe-edge verification tolerates a 300s clock skew
# (paperclip_trigger_proxy.verify_stripe_signature's default `tolerance_s`) —
# matching the trigger's own replay window keeps the two numbers from
# drifting apart for the one signing_mode that uses both.
_DEFAULT_REPLAY_WINDOW_SEC = 300


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
    GitHub auto-register call). Never masks the caller's own error — only
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


_GITHUB_API_BASE = "https://api.github.com"


def _github_client_factory() -> httpx.AsyncClient:
    """Build the GitHub API HTTP client. Module-level so tests can swap in a
    stub transport — same convention ``routers/hooks_proxy.py``'s
    ``_client_factory`` uses."""
    return httpx.AsyncClient(timeout=15.0)


def _github_hook_provider(public_id: str) -> str:
    """The ``integration_secret_store`` ``provider`` slot for one
    auto-registered GitHub webhook's retained ``{hook_id, repo}`` — written
    by ``_auto_register_webhook`` right after a successful registration,
    read (and cleared) by ``cleanup_github_hook``. Hyphenated, not
    colon-joined — Secrets Manager names only allow ``/_+=.@-`` (same
    convention as ``stripe_edge.stripe_trigger_secret_provider``)."""
    return f"github-hook-{public_id}"


async def cleanup_github_hook(owner_id: str, public_id: str) -> None:
    """Best-effort: unregister the GitHub webhook hook ``_auto_register_webhook``
    created for this trigger, so deleting the routine/trigger (the Isol8 BFF's
    ``routers/teams/work.py`` delete handlers) doesn't leave a live GitHub
    webhook delivering to a dead proxy forever.

    No-op if nothing was retained for this ``public_id`` — a guided_paste
    (Stripe) trigger, a schedule trigger, or a github_hmac trigger whose
    hook was already cleaned up. Never raises: a webhook-unregister failure
    (expired/revoked token, repo renamed or deleted, hook already gone)
    must never block the delete it's running ahead of — only logged. The
    retained record is dropped either way once cleanup has been attempted;
    this is a best-effort action, not a retryable job.
    """
    record = await integration_secret_store.get(owner_id, _github_hook_provider(public_id))
    if not record:
        return
    repo = record.get("repo")
    hook_id = record.get("hook_id")
    try:
        if repo and hook_id:
            credential = await integration_secret_store.get(owner_id, "github")
            token = (credential or {}).get("token") or (credential or {}).get("access_token")
            if token:
                async with _github_client_factory() as http:
                    await http.delete(
                        f"{_GITHUB_API_BASE}/repos/{repo}/hooks/{hook_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
    except Exception:
        logger.warning(
            "inbound_triggers: GitHub hook cleanup failed repo=%s hook_id=%s owner=%s",
            repo,
            hook_id,
            owner_id,
            exc_info=True,
        )
    finally:
        await integration_secret_store.delete(owner_id, _github_hook_provider(public_id))


async def _auto_register_webhook(
    *,
    owner_id: str,
    provider_key: str,
    repo: str | None,
    proxy_url: str,
    trigger_secret: str | None,
    event_id: str,
    public_id: str,
) -> None:
    """Seam for the ``webhook_registration: "auto"`` branch (GitHub, Task 4):
    register the webhook with the provider's own API so it starts sending
    signed events with no owner paste step.

    Only GitHub is live today: ``POST /repos/{repo}/hooks`` with
    ``Authorization: Bearer <token>`` (the token from
    ``integration_secret_store.get(owner_id, "github")`` — the same
    OAuth-app-issued token ``materializers/gh_cli.py`` reads, hence the
    identical ``.get("token") or .get("access_token")`` fallback: the
    paste rail and the direct_oauth rail key the credential dict
    differently). ``config.secret`` is the Paperclip-generated trigger
    secret (``trigger_resp["secretMaterial"]["webhookSecret"]`` —
    ``RoutineTriggerSecretMaterial`` is ``{webhookUrl, webhookSecret}``,
    never a bare ``"secret"`` key) — one shared value signs both sides,
    since the trigger itself is armed with Paperclip
    ``signingMode: "github_hmac"``.

    The registered hook subscribes to ONLY ``event_id`` — the single event
    the caller selected — never the provider's whole ``inbound.events``
    list. The proxy forwards to a routine keyed purely by trigger publicId
    with no per-event filtering, so a hook subscribed to every declared
    event would fire that routine on events it never asked for, and a
    second automation for a different event on the same repo would end up
    subscribed to the same full set too — every push firing BOTH routines
    (duplicate agent runs, duplicate spend). One hook per automation, one
    event per hook, keeps automations on the same repo independent.

    Raises ``ValueError`` on any failure (missing repo, missing/absent
    token, or a non-2xx from GitHub) — the caller (``seed_trigger_routine``)
    treats that uniformly as "roll back the routine+trigger we just
    created and surface a clear error", so there is no reason to
    distinguish a caller mistake from an upstream failure here.

    On success, retains the registered hook's id + ``repo`` under
    ``public_id`` (best-effort — see ``cleanup_github_hook``) so a later
    routine/trigger delete can unregister this hook instead of leaving it
    live forever.
    """
    if not repo:
        raise ValueError(f"{provider_key!r} requires a repo to auto-register a webhook")

    # A github_hmac trigger is only verifiable if the hook we register
    # actually carries a secret — a None here (Paperclip returned no
    # secretMaterial, or no secretMaterial.webhookSecret) would register a
    # secret-less GitHub hook that 401s every real event forever while this
    # call still reports success.
    if trigger_secret is None:
        raise ValueError(f"no trigger secret to arm the {provider_key} webhook with")

    credential = await integration_secret_store.get(owner_id, provider_key)
    token = (credential or {}).get("token") or (credential or {}).get("access_token")
    if not token:
        raise ValueError(f"no {provider_key} credential for owner {owner_id!r} — connect {provider_key} first")

    async with _github_client_factory() as http:
        resp = await http.post(
            f"{_GITHUB_API_BASE}/repos/{repo}/hooks",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "web",
                "active": True,
                "events": [event_id],
                "config": {"url": proxy_url, "secret": trigger_secret, "content_type": "json"},
            },
        )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"GitHub webhook registration failed for repo={repo!r}: {exc.response.status_code} {exc.response.text}"
        ) from exc

    # Retain {hook_id, repo} for cleanup_github_hook — best-effort: the
    # registration above already succeeded and the hook is now actually
    # live, so a retention-write failure must not roll back a webhook GitHub
    # has already armed (it just means a future delete won't find anything
    # to unregister, same as a pre-retention trigger).
    try:
        hook_id = resp.json().get("id")
        if hook_id is not None:
            await integration_secret_store.put(
                owner_id, _github_hook_provider(public_id), {"hook_id": hook_id, "repo": repo}
            )
    except Exception:
        logger.warning(
            "inbound_triggers: failed to retain GitHub hook id for later cleanup, repo=%s owner=%s",
            repo,
            owner_id,
            exc_info=True,
        )


async def seed_trigger_routine(
    owner_id: str,
    provider_key: str,
    event_id: str,
    *,
    repo: str | None = None,
) -> dict[str, Any]:
    """Create a routine + webhook trigger pre-filled from ``provider_key``'s
    registry ``inbound`` declaration, for the event named ``event_id``.

    Returns ``{routine_id, trigger_public_id, proxy_url, guided, instructions}``.
    ``instructions`` carries the guided-paste copy (Stripe today) or is
    ``None`` once an ``auto`` provider lands (Task 4).

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

    paperclip_signing_mode = _PAPERCLIP_SIGNING_MODE.get(inbound.signing_mode)
    if paperclip_signing_mode is None:
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

    # Stays open through the "auto" branch below (not just routine+trigger
    # creation): a failed GitHub auto-register needs `admin`/`cookie` alive to
    # roll back the routine it just created via the SAME session, not a fresh
    # one.
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
                    signing_mode=paperclip_signing_mode,
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
            # owner can't see or arm — same failure shape the "auto"
            # webhook-registration branch below unwinds. Unwind it here too,
            # for BOTH the guided and auto branches, before re-raising.
            await _rollback_routine(
                admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
            )
            raise

        proxy_url = f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}"

        if inbound.signing_mode == "stripe_edge":
            # Mark this trigger as stripe-edge-seeded BEFORE returning — the
            # ONLY thing PUT /automations/{public_id}/webhook-secret
            # (routers/automations.py) may gate on. Paperclip signingMode
            # "none" is NOT a safe substitute: it's also Phase 1's own
            # deliberately-unsigned option, so gating on signingMode alone
            # would let a legitimate unsigned trigger accept (and be
            # bricked by) a pasted Stripe whsec_ later. Written to the SAME
            # per-trigger Secrets Manager slot the whsec_ itself later
            # occupies — see stripe_edge.STRIPE_EDGE_SEEDED_FIELD. Failure
            # here rolls back like any other post-creation step: a trigger
            # the owner can never legitimately arm is the same orphan shape
            # as a failed trigger-create.
            try:
                await integration_secret_store.put(
                    owner_id, stripe_trigger_secret_provider(public_id), {STRIPE_EDGE_SEEDED_FIELD: True}
                )
            except Exception:
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise

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

        if inbound.webhook_registration == "auto":
            secret_material = trigger_resp.get("secretMaterial") if isinstance(trigger_resp, dict) else None
            # RoutineTriggerSecretMaterial is {webhookUrl, webhookSecret} —
            # NEVER a bare "secret" key. Reading the wrong key here means
            # `trigger_secret` is always None on a real response, which trips
            # the not-None guard in `_auto_register_webhook` and rolls back
            # EVERY GitHub auto-registration.
            trigger_secret = secret_material.get("webhookSecret") if isinstance(secret_material, dict) else None
            try:
                await _auto_register_webhook(
                    owner_id=owner_id,
                    provider_key=provider_key,
                    repo=repo,
                    proxy_url=proxy_url,
                    trigger_secret=trigger_secret,
                    event_id=event_id,
                    public_id=public_id,
                )
            except Exception as exc:
                # The routine + webhook trigger both already exist upstream at
                # this point. If the provider's own webhook never gets
                # registered, that pair is an orphan the owner can't see or
                # clean up — not armed guided (no instructions were ever
                # returned) and not armed auto (no sender knows the URL).
                # Unwind it: same best-effort-delete-then-reraise shape
                # `agent_routine_provisioning._arm_enabled_routine` uses for
                # its own post-create trigger failure.
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise ValueError(f"failed to auto-register {provider_key} webhook: {exc}") from exc
            logger.info(
                "inbound_triggers: seeded auto routine=%s trigger=%s owner=%s provider=%s event=%s",
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
                "guided": False,
                "instructions": None,
            }

        raise ValueError(f"unknown inbound.webhook_registration {inbound.webhook_registration!r}")
