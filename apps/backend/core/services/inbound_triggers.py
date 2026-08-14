"""Seed a real webhook-triggered Paperclip routine from a registry-declared
inbound integration event — the backend seam behind "connect Stripe -> pick
'payment failed' -> get a pre-filled automation."

Composes the same two-call ``create_routine`` -> ``create_routine_trigger``
sequence ``core/services/agent_routine_provisioning.py`` uses for scheduled
routines (reuses its routine-body builder directly), driven by the registry
(``core/services/credentials/registry.py``) instead of a hardcoded spec list.

The inbound shapes:

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

The BEARER-guided variant (PostHog, ``signing_mode: "bearer"`` on a
guided_paste block): the provider sends no signature at all, only whatever
headers the owner configures, so the secret flows the OPPOSITE direction —
the trigger's own one-time ``secretMaterial.webhookSecret`` is returned as
``webhook_secret`` for the owner to set as the provider-side Authorization
header (shown ONCE: Paperclip has no secret read-back, so this response is
the only place it ever exists outside the trigger). No paste-back step, no
arming PUT — the trigger is born holding the right secret and deliveries
fail closed until the owner wires the header.

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

``webhook_registration: "auto_google_watch"`` (Google): auto like GitHub —
the trigger's one-time ``secretMaterial.webhookSecret`` becomes the Google
push channel's token — but Google channels EXPIRE (≤7 days), so the watch
must be re-armed for as long as the automation lives. Renewal is the
ROUTINE'S OWN job, not the platform's: the seed attaches a second,
twice-weekly ``kind="schedule"`` trigger to the same routine and writes the
watch's identity to a state file on the owner's EFS
(``google-watch/{trigger_public_id}.json`` under the owner root, which the
container sees at ``/home/node/.openclaw/google-watch/``); the routine
description opens with a maintenance clause telling the agent to read that
file and re-arm via gog when expiry is near. Renewal thereby dies with the
automation — delete the routine and nothing re-arms the orphaned channel
(it lapses at its ≤7-day expiry; deliveries fail closed at Paperclip until
then). The resource is a connected Google ACCOUNT email, validated against
the stored inventory (which doubles as the argv injection fence — the email
rides into gog's fixed argv). Google sends no body signature: deliveries
carry ``X-Goog-Channel-Token``, which the trigger proxy maps into
``Authorization: Bearer`` for Paperclip's native bearer mode.

Two PER-EVENT shapes ride the same google block (``InboundEvent.registration``
overrides the block-level ``webhook_registration``; see the registry):

``registration: "poll"`` (gmail.poll): no webhook at all — the seed creates
the routine plus an ENABLED ``kind="schedule"`` trigger (``*/10 * * * *``
UTC) and the starter prompt has the agent gog-search for mail newer than the
last run. Zero owner setup, nothing to renew, no proxy_url in the result.

``registration: "owner_setup"`` (gmail.message): Gmail push publishes only to
a topic in the OWNER'S OWN GCP project (Google is BYO here, so they have
one). The seed creates a bearer trigger, runs ``gog gmail watch start
--topic <their topic>``, records a ``kind="gmail_watch"`` entry on the
``google_watch_channels`` list of the owner's "google" integrations row, and
returns one-time Pub/Sub setup instructions (create the topic, grant
gmail-api-push publisher, add an OIDC push subscription aimed at the proxy
URL). Deliveries carry a per-delivery Google OIDC JWT, which the trigger
proxy verifies and swaps for the entry's ``channel_token`` (= the trigger's
own secret) so Paperclip's bearer mode still does the final compare — that
proxy lookup is the ONLY reader of the row, and gmail push is now its only
writer (calendar/drive channels live solely in the agent-side state file).
Renewal rides the same self-renewal shape as the push channels: a second
schedule trigger + state file + maintenance clause, with the agent running
``gog gmail watch renew`` — which needs no secret, so the gmail state file
carries none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories import google_accounts_repo, integrations_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import google_watch
from core.services.agent_routine_provisioning import _build_routine_body
from core.services.credentials import integration_secret_store, registry
from core.services.credentials.materializers.base import atomic_write, owner_efs_root
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

# A fully-qualified Pub/Sub topic ("projects/<proj>/topics/<name>") — the only
# topic shape owner_setup (gmail.message) accepts. GCP's own naming rules for
# both segments; also an injection fence: the topic rides into gog's fixed
# argv and back out in the setup instructions.
_PUBSUB_TOPIC_RE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/topics/[A-Za-z0-9-_.~+%]{3,255}$")

# gmail.poll's cadence. */10 balances inbox latency against waking the
# container 144 times a day; the routine's coalesce_if_active policy keeps a
# slow run from stacking.
_GMAIL_POLL_CRON = "*/10 * * * *"

# The self-renewal schedule the google shapes attach as a SECOND trigger on
# the same routine. Twice a week (Mon+Thu 06:00 UTC) gives every ≤7-day
# watch two maintenance ticks before it can expire; the routine's own agent
# does the re-arm (see the maintenance clauses below), so renewal lives and
# dies with the automation instead of outliving it in a platform sweep.
_GOOGLE_RENEWAL_CRON = "0 6 * * 1,4"
_GOOGLE_RENEWAL_TRIGGER_LABEL = "Keep the Google watch alive"

# Where the container sees the seeded watch state file: the per-user EFS
# access point (rooted at the owner's EFS dir, = owner_efs_root) mounts at
# /home/node/.openclaw, so owner_efs_root/google-watch/{trigger_public_id}.json
# is this path in every agent-facing prompt.
_WATCH_STATE_CONTAINER_DIR = "/home/node/.openclaw/google-watch"

# Maintenance clauses prepended to the google starter prompts. The routine
# wakes from BOTH triggers (webhook delivery and renewal schedule) with the
# same description, so the clause is self-discriminating instead of
# source-detecting: maintenance first, then act on what changed, and a run
# where nothing changed was just a maintenance tick. The concrete account /
# address / file path are interpolated at seed time so the agent needs zero
# lookups beyond the file; the channel token is NOT — the description is
# visible in the Teams UI, so the secret stays in the 0600 state file (and
# gmail renewal needs no secret at all).
_CALENDAR_MAINTENANCE_CLAUSE = (
    "Every run, FIRST do maintenance: read {path}; if its expiration_ms is "
    "within 3 days of now, re-arm the watch by running `gog api call calendar "
    'v3 events.watch --params \'{{"calendarId":"primary"}}\' --body '
    '\'{{"id":"<new uuid>","type":"web_hook","address":"{address}",'
    '"token":"<channel_token from the file>","params":{{"ttl":"604800"}}}}\' '
    "--allow-write --force -a {account}` and rewrite the file with the new "
    "channel_id/resource_id/expiration_ms. THEN check what changed and act on "
    "it; if nothing changed, this was a maintenance tick — stop quietly."
)
_DRIVE_MAINTENANCE_CLAUSE = (
    "Every run, FIRST do maintenance: read {path}; if its expiration_ms is "
    "within 3 days of now, re-arm the watch by running `gog drive changes "
    "watch --token=<page_token from the file> --webhook-url={address} "
    "--channel-id=<new uuid> --channel-token=<channel_token from the file> "
    "--expiration-ms=<now + 7 days, in ms> -a {account}`, rewrite the file "
    "with the new channel_id/resource_id/expiration_ms, then best-effort "
    "`gog drive changes stop <old channel_id> <old resource_id>`. THEN check "
    "what changed and act on it; if nothing changed, this was a maintenance "
    "tick — stop quietly."
)
_GMAIL_MAINTENANCE_CLAUSE = (
    "Every run, FIRST do maintenance: read {path}; if its expiration_ms is "
    "within 3 days of now, run `gog gmail watch renew -a {account}` and "
    "rewrite the file's expiration_ms with the returned expiration. THEN "
    "check what changed and act on it; if nothing changed, this was a "
    "maintenance tick — stop quietly."
)


def _write_watch_state(owner_id: str, public_id: str, state: dict[str, Any]) -> None:
    """Seed the agent-side watch state file the maintenance clause reads —
    ``owner_efs_root/google-watch/{public_id}.json``, which the container
    mounts at ``{_WATCH_STATE_CONTAINER_DIR}/{public_id}.json``. atomic_write
    is the materializer idiom exactly: mkdir parents, atomic replace, 0600,
    chown to node so the routine's own runs can read AND rewrite it after
    re-arming."""
    atomic_write(
        owner_efs_root(owner_id) / "google-watch" / f"{public_id}.json",
        (json.dumps(state, indent=2) + "\n").encode(),
    )


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


def _build_schedule_trigger_body(cron: str, *, label: str | None = None) -> dict[str, Any]:
    """The ENABLED schedule-trigger body the poll shape and the google
    renewal shapes attach — the ``kind="schedule"`` counterpart to
    ``_build_webhook_trigger_body``. Live on create for the same reason the
    webhook one is: the owner explicitly asked for this automation
    (always-on-gated), unlike the dormant-by-invariant provisioning seeds in
    ``agent_routine_provisioning``. UTC keeps the cadence unambiguous across
    the fleet. ``label`` names the trigger in the detail panel — the renewal
    trigger carries one so the owner can see (and delete) the maintenance
    schedule for what it is."""
    body: dict[str, Any] = {
        "kind": "schedule",
        "cronExpression": cron,
        "timezone": "UTC",
        "enabled": True,
    }
    if label is not None:
        body["label"] = label
    return body


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


async def list_google_accounts(owner_id: str) -> list[dict[str, str]]:
    """Connected Google account emails — feeds the create dialog's account
    picker for the auto_google_watch registration shape. The email is both
    the id and the label (it IS the human-readable name of the account).

    Raises ``LookupError`` when no Google account is connected (the router
    maps it to 404).
    """
    accounts = await google_accounts_repo.list_accounts(owner_id)
    if not accounts:
        raise LookupError("Google is not connected")
    return [{"id": a["email"], "label": a["email"]} for a in accounts]


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
    topic: str | None = None,
) -> dict[str, Any]:
    """Create a routine + trigger pre-filled from ``provider_key``'s registry
    ``inbound`` declaration, for the event named ``event_id``.

    Returns ``{routine_id, trigger_public_id, proxy_url, guided, instructions,
    secret_hint}``; bearer-guided providers add ``webhook_secret`` (the
    trigger's own one-time secret, which the owner sets as the provider-side
    Authorization header — see the module docstring); the auto shapes add
    ``resource`` and need one — the
    "owner/repo" to hook for auto_github, the project id for auto_vercel,
    a connected Google account email for the google shapes (guided providers
    ignore it). The poll shape adds ``poll: True`` (and has no
    trigger_public_id/proxy_url — there is no webhook); owner_setup
    additionally needs ``topic``, the owner's own Pub/Sub topic.

    Raises ``ValueError`` for any caller-facing input problem (unknown
    provider/event, provider not wired as a trigger source, missing/invalid
    resource or topic, GitHub/Vercel/Google refusing the registration, no
    active Paperclip company, no resolvable suggested agent) — the router
    maps these to a 4xx. Raises ``RuntimeError`` on a malformed upstream
    response (Paperclip is expected to always return an id / publicId /
    secretMaterial).
    """
    provider = registry.get_provider(provider_key)
    inbound = provider.inbound
    if inbound is None:
        raise ValueError(f"{provider_key!r} is not wired as an Automations trigger source")

    event = next((e for e in inbound.events if e.id == event_id), None)
    if event is None:
        raise ValueError(f"unknown event {event_id!r} for provider {provider_key!r}")

    # Events may override the block-level registration (the google block:
    # calendar/drive stay auto_google_watch, gmail.poll/gmail.message carry
    # their own shape) — everything below dispatches on the EFFECTIVE value.
    registration = event.registration or inbound.webhook_registration

    # auto_github preconditions up front — a missing repo or token must fail
    # before anything exists upstream, not after a create/rollback cycle.
    gh_token: str | None = None
    if registration == "auto_github":
        if not resource or not _GITHUB_REPO_RE.match(resource):
            raise ValueError("auto_github needs a repository in the form owner/repo")
        gh_token = await _github_token(owner_id)
        if not gh_token:
            raise ValueError("GitHub is not connected — connect it in the Integrations tab first")

    # auto_vercel preconditions, same posture: fail before anything exists.
    vercel_token: str | None = None
    if registration == "auto_vercel":
        if not resource or not _VERCEL_PROJECT_RE.match(resource):
            raise ValueError("auto_vercel needs a Vercel project id (prj_…)")
        vercel_token = await _vercel_token(owner_id)
        if not vercel_token:
            raise ValueError("Vercel is not connected — connect it in the Integrations tab first")

    # Google-shape preconditions (push channels, gmail poll, gmail push),
    # same posture. The resource is a connected Google ACCOUNT email,
    # validated against the stored inventory rather than a regex — inventory
    # membership is both the "is Google connected" check and the injection
    # fence (the email rides into gog's fixed argv and the routine
    # description, so only values the connect path recorded may pass).
    if registration in ("auto_google_watch", "poll", "owner_setup"):
        if not resource:
            raise ValueError(f"{registration} needs a connected Google account email")
        accounts = await google_accounts_repo.list_accounts(owner_id)
        if not any(a["email"] == resource for a in accounts):
            raise ValueError("That Google account isn't connected — connect it in the Integrations tab first")

    # owner_setup additionally needs the owner's own Pub/Sub topic — the
    # regex is both a UX check (typo'd topics fail here, not at gog) and the
    # argv/instructions injection fence.
    if registration == "owner_setup":
        if not topic or not _PUBSUB_TOPIC_RE.match(topic):
            raise ValueError("owner_setup needs a Pub/Sub topic in the form projects/<project>/topics/<name>")

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
    # str.format ignores surplus kwargs, so `account` is inert for templates
    # that don't use it. Google's starter_prompt names the account so the
    # agent knows which `-a` to pass to gog — the push notification carries
    # no payload, so the routine description is the only carrier. An event
    # may override the template's prompt (a scheduled inbox check reads
    # nothing like a push notification).
    starter_prompt = event.starter_prompt or inbound.template.starter_prompt
    description = starter_prompt.format(event_label=event.label, account=resource or "")

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

        if registration == "poll":
            # No webhook at all — the trigger is an enabled schedule and the
            # automation is live the moment it exists. Nothing to renew, no
            # proxy URL to hand the owner.
            try:
                await admin.create_routine_trigger(
                    session_cookie=cookie,
                    routine_id=routine_id,
                    body=_build_schedule_trigger_body(_GMAIL_POLL_CRON),
                )
            except Exception:
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            logger.info(
                "inbound_triggers: seeded poll routine=%s owner=%s account=%s event=%s",
                routine_id,
                owner_id,
                resource,
                event_id,
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": None,
                "proxy_url": None,
                "guided": False,
                "instructions": None,
                "secret_hint": None,
                "resource": resource,
                "poll": True,
            }

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
            # The auto shapes arm the provider side with the trigger's own
            # secret (GitHub: the hook's HMAC secret; Google: the channel
            # token; gmail push: the watch entry's channel_token the proxy
            # injects after OIDC verification), shown ONCE in the create
            # response — absent means the trigger is unusable for auto
            # registration, same severity as no publicId. Bearer-guided
            # providers (PostHog) need it for the same reason: the OWNER arms
            # the provider side by hand with this value, and there is no
            # read-back later.
            webhook_secret: str | None = None
            if registration in ("auto_github", "auto_google_watch", "owner_setup") or (
                registration == "guided_paste" and inbound.signing_mode == "bearer"
            ):
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
            if registration == "auto_vercel":
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

        if registration == "guided_paste":
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
                # Bearer-guided only (None for every other guided provider):
                # the trigger's own secret, which the owner sets as the
                # provider-side Authorization header. Shown ONCE — the
                # reverse of the whsec paste-back, with no read-back later.
                "webhook_secret": webhook_secret,
            }

        if registration == "auto_github":
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

        if registration == "auto_vercel":
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

        if registration == "auto_google_watch":
            assert resource and webhook_secret  # narrowed by the pre-checks above
            is_calendar = event.id == google_watch.CALENDAR_EVENT
            clause = (_CALENDAR_MAINTENANCE_CLAUSE if is_calendar else _DRIVE_MAINTENANCE_CLAUSE).format(
                path=f"{_WATCH_STATE_CONTAINER_DIR}/{public_id}.json",
                address=proxy_url,
                account=resource,
            )
            try:
                # The state-file path is keyed by the trigger's public id,
                # which only exists now — so the maintenance clause lands via
                # a description patch, not the create body.
                await admin.patch_routine(
                    session_cookie=cookie,
                    routine_id=routine_id,
                    body={"description": f"{clause}\n\n{description}"},
                )
            except Exception:
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            try:
                channel = await google_watch.start_watch(
                    owner_id=owner_id,
                    account_email=resource,
                    provider_event=event.id,
                    proxy_url=proxy_url,
                    channel_token=webhook_secret,
                )
            except Exception:
                # gog / Google refused the watch (broken grant, revoked
                # account, API outage) — the routine+trigger are unusable,
                # unwind them.
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            try:
                # The renewal trigger + state file are what keep the ≤7-day
                # channel alive; without either the automation silently dies
                # within a week — same severity as the watch itself failing,
                # so stop the just-created channel (best-effort, never masks
                # this error) and unwind.
                await admin.create_routine_trigger(
                    session_cookie=cookie,
                    routine_id=routine_id,
                    body=_build_schedule_trigger_body(_GOOGLE_RENEWAL_CRON, label=_GOOGLE_RENEWAL_TRIGGER_LABEL),
                )
                state: dict[str, Any] = {
                    "kind": "calendar" if is_calendar else "drive",
                    "account": resource,
                    **({"calendar_id": "primary"} if is_calendar else {}),
                    "address": proxy_url,
                    "channel_token": webhook_secret,
                    "channel_id": channel["channel_id"],
                    "resource_id": channel["resource_id"],
                    "expiration_ms": channel["expiration_ms"],
                }
                if channel.get("page_token"):
                    state["page_token"] = channel["page_token"]
                await asyncio.to_thread(_write_watch_state, owner_id, public_id, state)
            except Exception:
                await google_watch.stop_watch(
                    owner_id=owner_id,
                    account_email=resource,
                    provider_event=event.id,
                    channel_id=channel["channel_id"],
                    resource_id=channel["resource_id"],
                )
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            logger.info(
                "inbound_triggers: seeded auto_google_watch routine=%s trigger=%s owner=%s account=%s event=%s",
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

        if registration == "owner_setup":
            assert resource and topic and webhook_secret  # narrowed by the pre-checks above
            clause = _GMAIL_MAINTENANCE_CLAUSE.format(
                path=f"{_WATCH_STATE_CONTAINER_DIR}/{public_id}.json",
                account=resource,
            )
            try:
                # Same description patch as auto_google_watch above: the
                # state-file path needs the trigger's public id.
                await admin.patch_routine(
                    session_cookie=cookie,
                    routine_id=routine_id,
                    body={"description": f"{clause}\n\n{description}"},
                )
            except Exception:
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            try:
                watch = await google_watch.start_gmail_watch(owner_id=owner_id, account_email=resource, topic=topic)
            except Exception:
                # gog / Google refused the watch — broken grant, revoked
                # account, API outage, or a topic Gmail can't publish to yet
                # (an owner who hasn't done the Pub/Sub setup retries this
                # create afterwards; the error names Google's reason). The
                # routine+trigger are unusable either way — unwind them.
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            try:
                # Renewal trigger + state file, same posture as the push
                # channels. The gmail state file carries NO secret: renewal
                # is `gog gmail watch renew -a <account>`, which re-arms
                # from the topic/labels gog stored at start time.
                await admin.create_routine_trigger(
                    session_cookie=cookie,
                    routine_id=routine_id,
                    body=_build_schedule_trigger_body(_GOOGLE_RENEWAL_CRON, label=_GOOGLE_RENEWAL_TRIGGER_LABEL),
                )
                await asyncio.to_thread(
                    _write_watch_state,
                    owner_id,
                    public_id,
                    {
                        "kind": "gmail",
                        "account": resource,
                        "topic": topic,
                        "expiration_ms": watch["expiration_ms"],
                    },
                )
                # gmail push is the ONLY writer of google_watch_channels
                # now (calendar/drive state lives solely in the agent-side
                # file): the trigger proxy still needs this entry's
                # channel_token to swap a verified OIDC delivery for the
                # trigger's bearer secret, and renewal changes neither the
                # token nor the trigger id, so the row never needs an
                # update after seed.
                entry = {
                    "kind": "gmail_watch",
                    "account_email": resource,
                    "topic": topic,
                    "trigger_public_id": public_id,
                    "proxy_url": proxy_url,
                    # The trigger's own secret. The proxy injects it as the
                    # Authorization bearer after verifying a delivery's
                    # Google OIDC JWT — Paperclip has no secret read-back,
                    # so this row is where the proxy finds it.
                    "channel_token": webhook_secret,
                    "expiration_ms": watch["expiration_ms"],
                }
                channels = await integrations_repo.get_google_watch_channels(owner_id)
                channels.append(entry)
                await integrations_repo.set_google_watch_channels(owner_id, channels)
            except Exception:
                # Without the renewal trigger the watch dies within a week;
                # without its row entry the proxy has no bearer secret to
                # inject and deliveries would 401 forever. Either way the
                # automation is unusable — stop the watch (best-effort)
                # before unwinding.
                await google_watch.stop_gmail_watch(owner_id=owner_id, account_email=resource)
                await _rollback_routine(
                    admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
                )
                raise
            logger.info(
                "inbound_triggers: seeded owner_setup routine=%s trigger=%s owner=%s account=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id,
            )
            # The one-time Pub/Sub setup in the OWNER'S project. Everything
            # interpolated is fence-validated above (topic regex, proxy_url is
            # ours). The dialog is informational — no paste-back step.
            instructions = (
                "One-time setup in your Google Cloud project: "
                "(1) enable the Pub/Sub API; "
                f"(2) create the topic {topic}; "
                "(3) grant gmail-api-push@system.gserviceaccount.com the Pub/Sub Publisher role on it; "
                f'(4) create a PUSH subscription to {proxy_url} with "Enable authentication" ON '
                f"(any service account) and the audience set to exactly {proxy_url}."
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": False,
                "instructions": instructions,
                "secret_hint": None,
                "resource": resource,
            }

        # No other registration shape exists; the routine+trigger were
        # already created, so unwind before surfacing the misconfiguration.
        await _rollback_routine(
            admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
        )
        raise ValueError(f"unsupported registration {registration!r}")
