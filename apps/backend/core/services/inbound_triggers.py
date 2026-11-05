"""Seed a real webhook-triggered Paperclip routine from a registry-declared
inbound integration event — the backend seam behind "connect Stripe -> pick
'payment failed' -> get a pre-filled automation."

Composes the same two-call ``create_routine`` -> ``create_routine_trigger``
sequence ``core/services/agent_routine_provisioning.py`` uses for scheduled
routines (reuses its routine-body builder directly), driven by the registry
(``core/services/credentials/registry.py``) instead of a hardcoded spec list.

Pass ``routine_id`` and the first call is skipped: the trigger is ATTACHED to
a routine the owner already wrote, and the registry's boilerplate starter
prompt is never rendered onto it. Everything from ``create_routine_trigger``
down — signing mode, provider-side registration, arming, the returned dict —
is the same code on both paths; only the rollback differs (an attach may
delete the triggers it added, never the owner's routine). The three shapes
whose setup writes the routine's own body APPEND it as a delimited block
instead of composing the description: see ``_append_platform_block``.

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

``webhook_registration: "auto_meta"`` (Meta Ads / Instagram): auto like GitHub,
but the ingress is APP-LEVEL — Meta configures one callback URL for the whole
app, not one per owner, so the seed's job is only to bind the owner's ASSET to
it: ``POST /{asset-id}/subscribed_apps`` with the owner's own token, then a
``{asset_id, …, trigger_public_id, trigger_secret}`` entry on the owner's
integrations row so ``routers/meta_webhook.py`` can route a delivery whose only
identifying mark is ``entry[].id``. The trigger is created ``bearer``: Meta
signs with the APP secret (nothing Paperclip holds), so the edge verifies that
signature and then injects the trigger's own secret as the Authorization —
exactly the Gmail-push shape one paragraph down, minus the OIDC. The resource
is the asset id, validated as digits AND as a member of the owner's live asset
list (that membership check is the ownership fence: it is the only thing
stopping an owner from binding somebody else's Page). Unsubscribing on rollback
is best-effort, same posture as the Vercel webhook delete.

The two providers differ only in which Graph host holds the asset:
``meta_ads`` Pages come from ``graph.facebook.com/me/accounts`` using the
retained OAuth token (and are subscribed with the PAGE token that call returns
— ``subscribed_apps`` on a Page wants a Page token, not the user one), while
``instagram`` has exactly ONE professional account whose ``graph.instagram.com``
token the backend never retains (``refresh_owner: actor``) and so is read from
the materialized ``skills/instagram/credentials.json`` on EFS.

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

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from core.config import settings
from core.observability.product_analytics import capture_product_event
from core.dynamodb import run_in_thread
from core.repositories import google_accounts_repo, integrations_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import google_watch
from core.services.agent_routine_provisioning import _build_routine_body
from core.services.credentials import integration_secret_store, registry
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_github_token import github_token as _github_token
from core.services.super_agent_flags import flags_for
from core.services.trigger_fire_url import trigger_fire_url

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

# Meta Graph. v23.0 rather than the v21.0 the design sketched: Meta retires a
# version ~2 years after release, which puts v21.0 (Oct 2024) inside its
# deprecation window already. v23.0 also matches the only other Graph call in
# the backend (the instagram_cli materializer's identity fetch).
_GRAPH_VERSION = "v23.0"
_FACEBOOK_GRAPH_BASE = "https://graph.facebook.com"
_INSTAGRAM_GRAPH_BASE = "https://graph.instagram.com"

# A Meta asset id (Page id / IG professional-account id) — always numeric. The
# only resource shape auto_meta accepts, and an injection fence: the id rides
# into the subscribed_apps URL path.
_META_ASSET_RE = re.compile(r"^\d{1,32}$")

# Where instagram_cli projects the owner's IG token — profile-relative, identical in every
# roster profile (credentials/hermes_cli.py's OWNED_PATHS["instagram_cli"]). The backend does
# not retain that credential (refresh_owner: actor — the agent renews it in place), so the
# profile is the only place to read it from.
_INSTAGRAM_CREDENTIALS_REL = "skills/instagram/credentials.json"

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

# Where the AGENT sees the seeded watch state file, and where the backend writes it inside a
# Hermes profile. ``$HOME`` for a turn's shell is ``<profile>/home`` (the bridge's
# ``PROFILE_HOME`` under ``TERMINAL_HOME_MODE: profile``), so the profile-relative key and the
# agent-facing path are the same file. Unlike the CLI-credential rail this one is NOT
# backend-owned: the maintenance clause REWRITES it after re-arming, and the bridge's
# recency pull/push carries that rewrite both ways.
_WATCH_STATE_AGENT_DIR = "~/google-watch"
_WATCH_STATE_PROFILE_DIR = "home/google-watch"

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


async def _write_watch_state(owner_id: str, public_id: str, state: dict[str, Any]) -> None:
    """Seed the agent-side watch state file the maintenance clause reads, at
    ``{_WATCH_STATE_PROFILE_DIR}/{public_id}.json`` in EVERY roster profile —
    ``{_WATCH_STATE_AGENT_DIR}/{public_id}.json`` to the agent.

    Fanned across the roster because the trigger names a routine, not an agent, and the file
    is owner-level state (one Google watch per account). Each profile gets an identical seed;
    whichever agent's routine actually runs re-arms and rewrites ITS copy, and the bridge's
    recency sync carries that rewrite back to S3. Siblings then hold a stale expiration they
    never read.
    """
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    body = (json.dumps(state, indent=2) + "\n").encode()
    rel = f"{_WATCH_STATE_PROFILE_DIR}/{public_id}.json"
    for agent in await load_agent_roster(owner_id):
        await run_in_thread(hermes_profiles.write_profile_subtree, owner_id, agent["id"], {rel: body}, ())


# `inbound.signing_mode` in the registry IS a native Paperclip signingMode
# now (e.g. Stripe -> "hmac_sha256"); it's passed straight through to the
# created trigger. Paperclip verifies natively against the trigger's secret,
# which the webhook-secret PUT arms with the provider's own value.
_DEFAULT_REPLAY_WINDOW_SEC = 300

# The signingModes Paperclip verifies natively — an inbound.signing_mode must
# be one of these (validated at seed time so a providers.yaml typo fails with a
# clear error instead of an opaque upstream 4xx).
_NATIVE_SIGNING_MODES = {"bearer", "hmac_sha256", "hmac_sha1", "github_hmac", "none"}

# Three registration shapes WRITE THE ROUTINE'S OWN BODY as part of their
# setup: auto_google_watch and owner_setup prepend a maintenance clause (their
# ≤7-day channel only stays alive because the routine's own agent re-arms it),
# and poll's entire behaviour lives in the starter prompt. On the CREATE path
# they compose the whole description, so they prepend. On the ATTACH path the
# routine is somebody's hand-written work, so the same text is APPENDED as a
# delimited, platform-owned block at the very END — every user-written word
# survives, and the owner can see exactly what the platform added. Refusing
# instead (the shape this replaced) left the owner at a dead end with a
# capability the machinery already supports.
_PLATFORM_BLOCK_START = "--- Added by Lightbulb when this trigger was armed — leave this block in place ---"
_PLATFORM_BLOCK_END = "--- end Lightbulb block ---"

# Every delimiter generation, current first. Re-attaching a routine appends a
# fresh block, so without recognising the retired brands' delimiters an owner
# who armed a trigger before a rename ends up with two (or three) stacked
# blocks contradicting each other. Keep the retired pairs verbatim — they are
# matched against descriptions already stored in Paperclip.
_PLATFORM_BLOCK_DELIMITERS: tuple[tuple[str, str], ...] = (
    (_PLATFORM_BLOCK_START, _PLATFORM_BLOCK_END),
    (
        "--- Added by Lightbulb AI when this trigger was armed — leave this block in place ---",
        "--- end Lightbulb AI block ---",
    ),
    (
        "--- Added by Isol8 when this trigger was armed — leave this block in place ---",
        "--- end Isol8 block ---",
    ),
)

# ONE trigger can carry MANY events. A Paperclip webhook trigger has no event
# filter (its schema is kind/signingMode/replayWindowSec — see the fork's
# validators/routine.ts), it fires on any signed POST and hands the whole
# payload to the agent; and every provider that fans out this way (Stripe,
# GitHub, PostHog, Vercel, Meta) lets ONE endpoint subscribe to MANY events.
# So event selection is multi-select on one trigger: one URL to paste, one
# hook to register, and the routine's instructions branch on the payload.
#
# These three shapes are the exception — each opens exactly ONE provider-side
# thing per trigger (a Google push channel per resource, a Gmail watch, a poll
# schedule), so their events can only be armed one per trigger.
_ONE_EVENT_REGISTRATIONS = {"auto_google_watch", "poll", "owner_setup"}

# Paperclip's own caps (fork validators/routine.ts): routine title max 200,
# trigger label max 120. A joined multi-event name blows past both long before
# it stops being readable, so it falls back to the compact "Stripe (3 events)".
_ROUTINE_TITLE_MAX = 200
_TRIGGER_LABEL_MAX = 120

# Appended to a multi-event draft description. The agent wakes from ONE
# trigger for several different payloads, so the first thing it has to do is
# work out which one arrived — Stripe's `type`, GitHub's X-GitHub-Event, and
# so on. Single-event drafts never see this (parity with the 1-click flow).
_MULTI_EVENT_CLAUSE = (
    "This trigger is armed for {count} events: {event_labels}. Exactly one of "
    "them fired this run — read the event type out of the payload FIRST and "
    "branch on it before doing anything else."
)


def _join_human(parts: list[str]) -> str:
    """``["a"] -> "a"``, ``["a","b"] -> "a and b"``, ``["a","b","c"] -> "a, b and c"``.

    A one-item join is the item itself, which is what keeps every single-event
    rendering (title, starter prompt, guided instructions) byte-identical to
    what it was before events became multi-select."""
    if len(parts) < 3:
        return " and ".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _provider_event_names(events: list[registry.InboundEvent]) -> list[str]:
    """Every selected event's PROVIDER-side names, deduped in order — the
    GitHub hook's ``events``, the Vercel webhook's ``events``, the Meta
    ``subscribed_apps`` fields. The curated id is the fallback where the two
    coincide, exactly as it was for a single event."""
    return list(dict.fromkeys(name for e in events for name in (e.provider_events or [e.id])))


async def _company_id_for_owner(owner_id: str) -> str | None:
    """Same resolution ``routers.paperclip_trigger_proxy._company_id_for_owner``
    uses, duplicated here rather than imported across the router/service
    boundary (services must not depend on routers)."""
    ws = await PaperclipRepo(table_name="paperclip-companies").get_workspace(owner_id)
    return (ws.company_id or None) if ws is not None else None


@lru_cache(maxsize=1)
def _titles_to_automation() -> dict[str, tuple[str, str]]:
    """Rendered routine title -> ``(provider_key, event_id)``.

    The exact inverse of what ``seed_trigger_routine`` names a seeded routine
    and its webhook trigger (``inbound.template.title.format(event_label=…)``).
    Both halves come from the registry, so the mapping inverts deterministically
    — which is how the marketplace recovers what automation a captured routine
    represents, having only its title. Titles are unique across the whole
    registry (pinned by ``test_automation_titles_are_unique_across_the_registry``);
    a collision would make this ambiguous, so keep them that way.
    """
    out: dict[str, tuple[str, str]] = {}
    for key, provider in registry.all_providers().items():
        inbound = provider.inbound
        if inbound is None:
            continue
        for event in inbound.events:
            out[inbound.template.title.format(event_label=event.label)] = (key, event.id)
    return out


def automation_from_title(title: str) -> tuple[str, str] | None:
    """``(provider_key, event_id)`` for a title the seed rendered, else None
    (a hand-made routine, or an event the registry no longer carries)."""
    return _titles_to_automation().get(title)


def automation_requirement(provider_key: str, event_id: str) -> dict[str, Any] | None:
    """One webhook automation as a buyer-facing REQUIREMENT —
    ``{provider, event_id, needs_resource}`` — or None when the registry no
    longer carries that provider/event.

    ``needs_resource`` is True for every registration shape that binds a
    provider-side resource only the buyer can name (their repo, Vercel
    project, Meta Page, Google account); guided_paste needs nothing but the
    proxy URL Isol8 hands them.
    """
    try:
        provider = registry.get_provider(provider_key)
    except KeyError:
        return None
    inbound = provider.inbound
    if inbound is None:
        return None
    event = next((e for e in inbound.events if e.id == event_id), None)
    if event is None:
        return None
    registration = event.registration or inbound.webhook_registration
    return {
        "provider": provider_key,
        "event_id": event_id,
        "needs_resource": registration != "guided_paste",
    }


def _agent_rows(listed: Any) -> list:
    """The agent rows out of whatever ``list_agents`` returned.

    Upstream's ``GET /companies/{companyId}/agents`` returns a bare JSON array,
    but the client is typed ``-> dict`` and the BFF envelopes it under
    ``"agents"`` — so accept both, same defensiveness (and same reason) as
    ``company_deploy_service._find_friday_id`` and
    ``agent_routine_provisioning._existing_titles_for_agent``. Iterating the
    enveloped dict directly walks its KEYS, which would fail every assignee
    check below against a roster the agent really is on.
    """
    if isinstance(listed, dict):
        listed = listed.get("agents") or listed.get("items") or []
    return listed if isinstance(listed, list) else []


def _routine_rows(listed: Any) -> list:
    """The routine rows out of whatever ``list_routines`` returned — the
    ``_agent_rows`` treatment for the same reason (bare array upstream, an
    enveloped dict through the BFF)."""
    if isinstance(listed, dict):
        listed = listed.get("routines") or listed.get("items") or []
    return listed if isinstance(listed, list) else []


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


async def _create_github_hook(
    *, token: str, resource: str, proxy_url: str, events: list[str], secret: str
) -> str | None:
    """Create the repo webhook pointed at the trigger proxy, signed with the
    trigger's own secret. Returns GitHub's hook id (what a later delete needs).
    Raises ``ValueError`` with a caller-facing message on the expected
    refusals (no admin on the repo, duplicate hook)."""
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
        raise ValueError(
            f"Lightbulb couldn't create the webhook on {resource} — you need admin access to the repository"
        )
    resp.raise_for_status()
    try:
        hook_id = resp.json().get("id")
    except ValueError:
        hook_id = None
    return str(hook_id) if hook_id is not None else None


async def _delete_github_hook(*, token: str, resource: str, hook_id: str) -> None:
    """Best-effort delete of a repo webhook whose automation is gone — left
    behind, GitHub would deliver into a URL that 404s forever. Never masks
    the caller's own error — only logs its own failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.delete(
                f"{_GITHUB_API_BASE}/repos/{resource}/hooks/{hook_id}",
                headers={**_GITHUB_HEADERS, "Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 404:
            resp.raise_for_status()
    except Exception:
        logger.exception("inbound_triggers: delete of GitHub hook %s on %s failed", hook_id, resource)


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
            "Lightbulb couldn't create the Vercel webhook — your Vercel plan or token can't create webhooks "
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


# --- Meta (auto_meta) -------------------------------------------------------
#
# An "asset" is one Facebook Page (meta_ads) or one Instagram professional
# account (instagram). The pickers only ever see {id, label}; the seed also
# needs the access token and which Graph host holds it, so the internal shape
# carries those and the public listers project them away — the resources
# endpoint returns owner-facing JSON and must never carry a token.


async def _meta_pages(owner_id: str) -> list[dict[str, str]]:
    """The Pages the owner's connected Meta Ads token administers, each with
    the PAGE access token ``/me/accounts`` hands back — ``subscribed_apps`` on
    a Page wants a Page token, not the user one.

    Raises ``LookupError`` when Meta Ads isn't connected (the router maps it to
    404); httpx errors and an unrecognised 200 body both bubble up for the
    router's 502 rather than being guessed at.
    """
    credential = await integration_secret_store.get(owner_id, "meta_ads")
    token = (credential or {}).get("access_token")
    if not token:
        raise LookupError("Meta Ads is not connected")
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(
            f"{_FACEBOOK_GRAPH_BASE}/{_GRAPH_VERSION}/me/accounts",
            params={"fields": "id,name,access_token", "access_token": token},
        )
    resp.raise_for_status()
    data = resp.json().get("data")
    if not isinstance(data, list):
        raise RuntimeError("Meta /me/accounts returned no page list")
    pages = [
        {
            "id": str(p["id"]),
            "label": p.get("name") or str(p["id"]),
            "token": p["access_token"],
            "graph_base": _FACEBOOK_GRAPH_BASE,
            "object_type": "page",
        }
        for p in data
        if isinstance(p, dict) and p.get("id") and p.get("access_token")
    ]
    if data and not pages:
        # A user token must never be substituted here: Page-gated Graph edges
        # require the Page access token returned for that Page.
        raise RuntimeError("Meta /me/accounts returned pages without Page access tokens")
    return pages


async def _read_instagram_credentials(owner_id: str) -> dict:
    """The projected IG credential, read from the first roster profile that carries it.

    Every profile holds a byte-identical copy (hermes_cli fans one ``files`` dict out to the
    whole roster), so the first hit is the answer; the loop only covers a profile created AFTER
    the connect, which has no copy at all. ``FileNotFoundError`` when no profile has one — the
    caller maps it to "not connected"."""
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    for agent in await load_agent_roster(owner_id):
        try:
            info = await run_in_thread(
                hermes_profiles.read_profile_file, owner_id, agent["id"], _INSTAGRAM_CREDENTIALS_REL
            )
        except FileNotFoundError:
            continue
        return json.loads(info["content"])
    raise FileNotFoundError(_INSTAGRAM_CREDENTIALS_REL)


async def _instagram_accounts(owner_id: str) -> list[dict[str, str]]:
    """The single Instagram professional account the owner connected.

    Business Login for Instagram grants exactly ONE account and issues a
    ``graph.instagram.com`` token — there is no ``/me/accounts`` there to fan
    out over, and the backend never retains the token (``refresh_owner:
    actor``). The projection already wrote the id, username and token into every roster
    profile, so this is one S3 read of the first profile that has the file.

    Raises ``LookupError`` when Instagram isn't connected — no projected credentials file in
    any profile, or one missing the token/id (the router maps it to 404).
    """
    try:
        creds = await _read_instagram_credentials(owner_id)
    except (FileNotFoundError, ValueError) as exc:
        raise LookupError("Instagram is not connected") from exc
    token = creds.get("access_token")
    account_id = str(creds.get("user_id") or "")
    if not token or not account_id:
        raise LookupError("Instagram is not connected")
    return [
        {
            "id": account_id,
            "label": creds.get("username") or account_id,
            "token": token,
            "graph_base": _INSTAGRAM_GRAPH_BASE,
            "object_type": "instagram",
        }
    ]


# provider_key -> its asset lister. Also the set of providers auto_meta accepts.
_META_ASSET_LISTERS = {"meta_ads": _meta_pages, "instagram": _instagram_accounts}


async def list_meta_pages(owner_id: str) -> list[dict[str, str]]:
    """Picker feed for meta_ads' auto_meta shape — Pages, tokens stripped."""
    return [{"id": a["id"], "label": a["label"]} for a in await _meta_pages(owner_id)]


async def list_instagram_accounts(owner_id: str) -> list[dict[str, str]]:
    """Picker feed for instagram's auto_meta shape — the one connected account,
    token stripped. A one-entry dropdown, but it keeps the create dialog's
    resource step identical across every auto shape."""
    return [{"id": a["id"], "label": a["label"]} for a in await _instagram_accounts(owner_id)]


async def _subscribe_meta_asset(asset: dict[str, str], fields: list[str]) -> None:
    """Bind the app's ONE webhook callback to this asset for ``fields``.

    Raises ``ValueError`` with a caller-facing message on the expected refusals
    — the connected account doesn't administer the asset, or the Isol8 Meta app
    doesn't hold the webhook permission for that object type. Graph reports
    both as a 400/403 whose subcode we'd only be guessing at, so they share one
    message that names both causes."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.post(
            f"{asset['graph_base']}/{_GRAPH_VERSION}/{asset['id']}/subscribed_apps",
            params={"subscribed_fields": ",".join(fields), "access_token": asset["token"]},
        )
    if resp.status_code in (400, 403):
        raise ValueError(
            f"Lightbulb couldn't subscribe to {asset['label']} — the connected Meta account must administer it, "
            "and the Lightbulb Meta app must hold the webhook permission for it"
        )
    resp.raise_for_status()
    # Graph answers 200 {"success": true}. A 200 that doesn't say so means the
    # binding didn't take, and a trigger nothing ever delivers to is worse than
    # a visible failure.
    try:
        confirmed = resp.json().get("success") is True
    except ValueError:
        confirmed = False
    if not confirmed:
        raise RuntimeError(f"Meta subscribed_apps did not confirm the subscription for {asset['id']}")


async def _unsubscribe_meta_asset(asset: dict[str, str]) -> None:
    """Best-effort unbind of an asset whose binding write failed — left
    subscribed, Meta would deliver events this fleet can route nowhere. Never
    masks the caller's own error — only logs its own failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.delete(
                f"{asset['graph_base']}/{_GRAPH_VERSION}/{asset['id']}/subscribed_apps",
                params={"access_token": asset["token"]},
            )
        resp.raise_for_status()
    except Exception:
        logger.exception("inbound_triggers: rollback unsubscribe of Meta asset %s failed", asset["id"])


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


async def _rollback_trigger(
    admin: PaperclipAdminClient, *, trigger_id: str, cookie: str, owner_id: str, provider_key: str
) -> None:
    """The ATTACH branch's counterpart to ``_rollback_routine``: best-effort
    delete of the trigger this call added to a routine the owner already
    owned. Only the trigger — deleting the routine would destroy work this
    call never created."""
    try:
        await admin.delete_routine_trigger(trigger_id=trigger_id, session_cookie=cookie)
    except Exception:
        logger.exception(
            "inbound_triggers: rollback delete_routine_trigger failed for trigger=%s owner=%s provider=%s",
            trigger_id,
            owner_id,
            provider_key,
        )


def _trigger_id(resp: Any) -> str | None:
    """The internal trigger id out of a ``create_routine_trigger`` response."""
    trigger = resp.get("trigger") if isinstance(resp, dict) else None
    return trigger.get("id") if isinstance(trigger, dict) else None


def _strip_platform_blocks(description: str) -> str:
    """Drop every complete platform-owned block, current or retired brand.

    An UNTERMINATED start marker is left alone: the block is always appended
    at the very end, so a start without its end means somebody edited the
    body, and cutting from there would delete their work. A duplicate block is
    the cheaper failure.

    So the scan anchors on the END marker and takes the NEAREST PRECEDING
    start, not the first start and any end after it. Anchoring the other way
    round eats everything between an orphaned start and a later complete
    block — which is exactly the owner prose the stacking bug leaves stranded.

    An orphaned END is skipped rather than ending the scan: it is content now,
    and a real block can still follow it.
    """
    for start, end in _PLATFORM_BLOCK_DELIMITERS:
        search_from = 0
        while (close_at := description.find(end, search_from)) >= 0:
            open_at = description.rfind(start, 0, close_at)
            if open_at < 0:
                search_from = close_at + len(end)
                continue
            description = description[:open_at] + description[close_at + len(end) :]
            search_from = open_at
    return description


async def _append_platform_block(admin: PaperclipAdminClient, *, cookie: str, routine_id: str, block: str) -> None:
    """Replace the delimited, platform-owned block at the END of an existing
    routine's description, leaving every user-written word intact.

    Reads the CURRENT description with ``get_routine`` rather than trusting
    the list projection: the list row is not guaranteed to carry a
    description, and treating a missing one as empty would erase the very
    instructions this exists to preserve. A failed read raises, and the
    caller unwinds — better than a patch that destroys the body.

    Any block already there is stripped first, across every delimiter
    generation. Re-arming a trigger otherwise stacks blocks — silently, and
    worst across a rename, where the old delimiters no longer match the new
    ones and the owner is left with two sets of platform instructions."""
    detail = await admin.get_routine(session_cookie=cookie, routine_id=routine_id)
    existing = _strip_platform_blocks((detail.get("description") if isinstance(detail, dict) else None) or "")
    appended = f"{_PLATFORM_BLOCK_START}\n{block}\n{_PLATFORM_BLOCK_END}"
    await admin.patch_routine(
        session_cookie=cookie,
        routine_id=routine_id,
        body={"description": f"{existing.rstrip()}\n\n{appended}" if existing.strip() else appended},
    )


def _guided_instructions(
    provider: registry.Provider,
    inbound: registry.Inbound,
    *,
    proxy_url: str,
    event_label_text: str,
    event_id_text: str,
    count: int,
) -> str:
    """The paste-this-URL copy for a guided provider: the registry's own text when it has one (every
    dashboard buries webhooks somewhere different), else the generic sentence."""
    if inbound.instructions:
        # `event_labels`/`event_ids` are the plural-safe names the provider
        # copy uses; `event_label`/`event_id` stay bound to the same rendering
        # so a template written before multi-select still names every
        # selected event.
        return inbound.instructions.format(
            proxy_url=proxy_url,
            event_label=event_label_text,
            event_id=event_id_text,
            event_labels=event_label_text,
            event_ids=event_id_text,
        )
    noun = "event" if count == 1 else "events"
    return (
        f"In your {provider.display_name} dashboard, add a webhook endpoint "
        f"pointed at {proxy_url} listening for the '{event_label_text}' {noun} "
        f"({event_id_text}). Paste the signing secret it gives you back into Lightbulb "
        "to finish arming this automation."
    )


def _gmail_setup_instructions(topic: str, proxy_url: str) -> str:
    """The one-time Pub/Sub setup for Gmail push. Everything interpolated is
    fence-validated by the caller (topic regex, proxy_url is ours). The dialog
    is informational — no paste-back step."""
    return (
        "One-time setup in your Google Cloud project: "
        "(1) enable the Pub/Sub API; "
        f"(2) create the topic {topic}; "
        "(3) grant gmail-api-push@system.gserviceaccount.com the Pub/Sub Publisher role on it; "
        f'(4) create a PUSH subscription to {proxy_url} with "Enable authentication" ON '
        f"(any service account) and the audience set to exactly {proxy_url}."
    )


@dataclass(frozen=True)
class _Registration:
    """What a validated trigger request resolves to, before anything exists anywhere: the registry
    declarations, the effective registration shape, and the provider-side credentials the auto shapes need.
    Shared by the Paperclip seed below and the super-agent one (``automation_hooks``)."""

    provider: registry.Provider
    inbound: registry.Inbound
    events: list[registry.InboundEvent]
    registration: str
    resource: str | None
    gh_token: str | None = None
    vercel_token: str | None = None
    meta_asset: dict[str, str] | None = None


async def _resolve_registration(
    owner_id: str,
    provider_key: str,
    event_ids: list[str],
    *,
    resource: str | None,
    topic: str | None,
) -> _Registration:
    """Every caller-facing precondition of a trigger request (``ValueError`` names the problem), with the
    auto shapes' provider credentials fetched up front so a refusal happens before anything is created."""
    gh_token: str | None = None
    vercel_token: str | None = None
    meta_asset: dict[str, str] | None = None
    provider = registry.get_provider(provider_key)
    inbound = provider.inbound
    if inbound is None:
        raise ValueError(f"{provider_key!r} is not wired as an Automations trigger source")

    # Deduped, caller's order. The FIRST event is the primary one: it names
    # the registration shape, supplies the starter-prompt override, and (for
    # the one-event shapes below) is the watch actually opened.
    ordered = list(dict.fromkeys(event_ids))
    if not ordered:
        raise ValueError("pick at least one event")
    by_id = {e.id: e for e in inbound.events}
    unknown = [e for e in ordered if e not in by_id]
    if unknown:
        raise ValueError(f"unknown event {', '.join(repr(u) for u in unknown)} for provider {provider_key!r}")
    events = [by_id[e] for e in ordered]
    event = events[0]

    # Events may override the block-level registration (the google block:
    # calendar/drive stay auto_google_watch, gmail.poll/gmail.message carry
    # their own shape) — everything below dispatches on the EFFECTIVE value.
    registration = event.registration or inbound.webhook_registration

    # One trigger = one URL = one signing mode = one registration shape. Events
    # that wire up differently physically cannot share it, so say which ones
    # and send the owner back to add them separately rather than silently
    # arming the first shape for all of them.
    shapes = {e.id: (e.registration or inbound.webhook_registration) for e in events}
    if len(set(shapes.values())) > 1:
        conflict = ", ".join(f"{eid} ({shape})" for eid, shape in shapes.items())
        raise ValueError(
            f"these events wire up differently and can't share one trigger: {conflict} — add them as separate triggers"
        )
    # One trigger hands the agent ONLY the delivery body — the proxy forwards
    # it byte-verbatim (HMAC is over the raw bytes) and the fork's
    # firePublicTrigger takes `payload: req.body`, so no header reaches the run
    # context. A provider that names its event only in a header (GitHub's
    # X-GitHub-Event, Sentry's Sentry-Hook-Resource) would therefore wake the
    # agent with no way to tell which armed event fired. Isol8AI/isol8#1916 is
    # the durable fix; until it lands those providers stay one event per
    # trigger.
    if len(events) > 1 and not inbound.event_in_body:
        # Deliberately generic: the reason is a header for GitHub/Sentry but an
        # owner-templated body for PostHog, and naming the wrong one sends the
        # owner hunting for something that isn't there.
        raise ValueError(
            f"an agent woken by one {provider.display_name} trigger can't tell which event fired from "
            f"the payload alone, so {', '.join(e.id for e in events)} must be added as separate triggers"
        )
    if len(events) > 1 and registration in _ONE_EVENT_REGISTRATIONS:
        raise ValueError(
            f"{registration} arms one event per trigger, so {', '.join(e.id for e in events)} "
            "must be added as separate triggers"
        )

    # auto_github preconditions up front — a missing repo or token must fail
    # before anything exists upstream, not after a create/rollback cycle.
    if registration == "auto_github":
        if not resource or not _GITHUB_REPO_RE.match(resource):
            raise ValueError("auto_github needs a repository in the form owner/repo")
        gh_token = await _github_token(owner_id)
        if not gh_token:
            raise ValueError("GitHub is not connected — connect it in the Integrations tab first")

    # auto_vercel preconditions, same posture: fail before anything exists.
    if registration == "auto_vercel":
        if not resource or not _VERCEL_PROJECT_RE.match(resource):
            raise ValueError("auto_vercel needs a Vercel project id (prj_…)")
        vercel_token = await _vercel_token(owner_id)
        if not vercel_token:
            raise ValueError("Vercel is not connected — connect it in the Integrations tab first")

    # auto_meta preconditions, same posture: fail before anything exists. The
    # asset must be numeric AND present in the owner's live asset list — that
    # membership check is the ownership fence (nothing else stops an owner
    # naming a Page they don't administer) as well as the connected check.
    if registration == "auto_meta":
        if not resource or not _META_ASSET_RE.match(resource):
            raise ValueError("auto_meta needs a numeric Meta asset id")
        asset_lister = _META_ASSET_LISTERS.get(provider_key)
        if asset_lister is None:
            raise ValueError(f"{provider_key!r} has no Meta asset lister")
        try:
            assets = await asset_lister(owner_id)
        except LookupError as exc:
            raise ValueError(
                f"{provider.display_name} is not connected — connect it in the Integrations tab first"
            ) from exc
        meta_asset = next((a for a in assets if a["id"] == resource), None)
        if meta_asset is None:
            raise ValueError("That Meta asset isn't on your connected account — reconnect it in the Integrations tab")

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

    return _Registration(provider, inbound, events, registration, resource, gh_token, vercel_token, meta_asset)


async def seed_trigger_routine(
    owner_id: str,
    provider_key: str,
    event_ids: list[str],
    resource: str | None = None,
    topic: str | None = None,
    assignee_agent_id: str | None = None,
    routine_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Create a routine + trigger pre-filled from ``provider_key``'s registry
    ``inbound`` declaration, armed for every event in ``event_ids``, and record
    the funnel event on success.

    ONE trigger carries the whole list: a Paperclip webhook trigger has no
    event filter, and every provider that fans out (Stripe, GitHub, Vercel,
    Meta, PostHog) lets one endpoint subscribe to many events — so the owner
    pastes ONE URL / registers ONE hook and the routine branches on the
    payload. The events must share a registration shape, and the three
    one-watch shapes (``auto_google_watch`` / ``poll`` / ``owner_setup``) take
    exactly one event; anything else is a ValueError naming the conflict.
    A multi-event title is not invertible by ``automation_from_title`` — the
    same cost a custom title already carries.

    Thin wrapper so ``automation_armed`` fires from ONE place instead of once
    per registration shape's return — every shape below is a success path.

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

    ``assignee_agent_id`` is who the routine lands on. Omitted, it resolves to
    the owner's Friday as before; supplied, it must be an agent on the
    CALLER's own Paperclip company roster (a Gmail automation for the support
    agent should not arrive Friday-owned and need reassigning by hand).

    ``routine_id`` ATTACHES the trigger to a routine the owner already wrote
    instead of seeding a new one — the "arm this event on my Revenue recovery
    routine" path. The routine must be in the CALLER's own company (same
    tenancy fence as ``assignee_agent_id``), and nothing the owner wrote is
    overwritten: no assignee, reviewers, variables or schedule, and the
    registry's boilerplate starter prompt is skipped entirely — a hand-authored
    routine already says what to do. The three shapes whose setup writes the
    routine's own body (auto_google_watch / owner_setup / poll) APPEND their
    maintenance clause as a delimited block at the end of the existing
    description instead of composing it (see ``_append_platform_block``); the
    frontend discloses that before the owner confirms. The provider side runs
    exactly as it does for a fresh seed (auto-register or guided paste).
    Rollback unwinds only the triggers this call created, never the routine.

    ``title`` / ``description`` OVERRIDE the registry template — what the
    owner typed over the pre-filled draft in the create dialog. Omitted (the
    untouched-draft path, which is what the 1-click flow sends), the template
    renders exactly as it always did. Note a custom title is no longer
    invertible by ``automation_from_title``, so a published agent carrying
    that automation declares no provider requirement — the cost of letting
    owners name their own automations.

    Raises ``ValueError`` for any caller-facing input problem (an empty or
    unknown event list, events whose registration shapes can't share one
    trigger, provider not wired as a trigger source, missing/invalid
    resource or topic, GitHub/Vercel/Google refusing the registration, no
    active Paperclip company, no resolvable suggested agent, an assignee or
    target routine outside the caller's company, a registration shape that
    can't attach) — the router maps these to a 4xx. Raises ``RuntimeError``
    on a malformed upstream response (Paperclip is expected to always return
    an id / publicId / secretMaterial).
    """
    # Deduped HERE so the funnel counts what was actually armed — the seed
    # dedupes too, and recording the raw list would report event_count=2 for
    # a caller that sent the same id twice.
    event_ids = list(dict.fromkeys(event_ids))
    if await flags_for(owner_id):
        # Super-agent workspaces: a HOOK# row + Secrets Manager instead of a Paperclip routine (lane A,
        # Task 8). ``routine_id`` is the automation to attach to; ``assignee_agent_id`` its specialist.
        from core.services import automation_hooks  # lazy: it reuses this module's helpers

        out = await automation_hooks.seed(
            owner_id,
            provider_key,
            event_ids,
            resource=resource,
            topic=topic,
            specialist=assignee_agent_id,
            automation_id=routine_id,
            title=title,
            description=description,
            actor_user_id=actor_user_id,
        )
    else:
        out = await _seed_trigger_routine(
            owner_id,
            provider_key,
            event_ids,
            resource=resource,
            topic=topic,
            assignee_agent_id=assignee_agent_id,
            routine_id=routine_id,
            title=title,
            description=description,
        )
    capture_product_event(
        distinct_id=owner_id,
        event="automation_armed",
        properties={
            "provider": provider_key,
            # The whole armed set, plus the first id kept under its old name so
            # funnels built on the single-event shape keep reading.
            "event_id": event_ids[0],  # non-empty: the seed above raises on an empty list
            "event_ids": event_ids,
            "event_count": len(event_ids),
            "source": "from_integration",
            # Which half of the funnel this was — a new automation or a
            # trigger armed on one the owner already wrote.
            "target": "existing_routine" if routine_id else "new_routine",
        },
    )
    return out


async def _seed_trigger_routine(
    owner_id: str,
    provider_key: str,
    event_ids: list[str],
    resource: str | None = None,
    topic: str | None = None,
    assignee_agent_id: str | None = None,
    routine_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """The seed itself — see ``seed_trigger_routine`` for the contract.

    ``routine_id`` doubles as the branch switch and the answer: supplied, it
    is the existing routine to attach to; omitted, it is filled in by the
    create below. Everything downstream reads the same name either way."""
    attach = routine_id is not None
    reg = await _resolve_registration(owner_id, provider_key, event_ids, resource=resource, topic=topic)
    provider, inbound, events, registration = reg.provider, reg.inbound, reg.events, reg.registration
    gh_token, vercel_token, meta_asset = reg.gh_token, reg.vercel_token, reg.meta_asset
    event = events[0]

    # What every renderer below names the selection: one event reads exactly as
    # it always did, several read as a list.
    event_label_text = _join_human([e.label for e in events])
    event_id_text = _join_human([e.id for e in events])
    # The fallback name when the joined one blows Paperclip's caps.
    compact_name = f"{provider.display_name} ({len(events)} events)" if len(events) > 1 else provider.display_name

    # Attach precondition, before anything exists upstream: an attach adds a
    # trigger and leaves the routine the owner's own. Reassignment is the one
    # thing it can never do — the shapes that would otherwise rewrite the body
    # append a delimited block instead (see _append_platform_block).
    if attach and assignee_agent_id is not None:
        raise ValueError("attaching to an existing automation can't reassign it")

    # Only "friday" is used by any live registry template today (Stripe's).
    # Extend this when a second suggested_agent shows up. An explicit assignee
    # bypasses the suggestion entirely — the caller named the agent, so there
    # is nothing to resolve, and an attach resolves no agent at all.
    if assignee_agent_id is None and not attach and inbound.template.suggested_agent != "friday":
        raise ValueError(f"no agent resolver for suggested_agent={inbound.template.suggested_agent!r}")

    company_id = await _company_id_for_owner(owner_id)
    if not company_id:
        raise ValueError(f"no active Paperclip company for owner {owner_id!r}")

    # An owner-supplied title/description wins over the registry template.
    # `or` (not `is None`) so an empty string falls back too — a blanked field
    # means "use the default", not "name this automation nothing".
    # `event_label_text` is `event.label` verbatim for one event, so this is
    # the same expression it always was on the single-event path. Several
    # events join into one title, which can outgrow Paperclip's 200-char cap —
    # only then does it fall back to the compact name.
    default_title = inbound.template.title.format(event_label=event_label_text)
    if len(events) > 1 and len(default_title) > _ROUTINE_TITLE_MAX:
        default_title = compact_name
    title = (title or "").strip() or default_title
    # str.format ignores surplus kwargs, so `account` is inert for templates
    # that don't use it. Google's starter_prompt names the account so the
    # agent knows which `-a` to pass to gog — the push notification carries
    # no payload, so the routine description is the only carrier. An event
    # may override the template's prompt (a scheduled inbox check reads
    # nothing like a push notification).
    starter_prompt = event.starter_prompt or inbound.template.starter_prompt
    # Kept separate from `description` below: the poll shape APPENDS this to a
    # routine the owner already wrote, and a platform-owned block must be
    # exactly that — the registry's text, never something the caller supplied.
    # (The other two appending shapes build their clause server-side already.)
    rendered_description = starter_prompt.format(event_label=event_label_text, account=resource or "")
    # Several events on one trigger means the agent can't know which arrived
    # from the instructions alone — tell it to read the type off the payload.
    if len(events) > 1:
        rendered_description += "\n\n" + _MULTI_EVENT_CLAUSE.format(count=len(events), event_labels=event_label_text)
    description = (description or "").strip() or rendered_description

    # Lazy import: keeps this module's load light and sidesteps a cycle —
    # same convention agent_export.py uses for the identical import.
    from core.services.company_deploy_service import _find_friday_id

    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        cookie = await get_admin_session_cookie(http)
        admin = PaperclipAdminClient(http_client=http)

        # The triggers this call added to a pre-existing routine, as they come
        # into being — what `unwind` is allowed to delete on the attach path.
        # A list, not one id: the google shapes attach a renewal SCHEDULE
        # alongside the webhook, and leaving that behind would keep waking the
        # owner's routine for a watch that no longer exists.
        attached_trigger_ids: list[str] = []

        def claim(resp: Any) -> None:
            """Remember a trigger this call just created, on the attach path.
            No-op on the create path — there the whole routine is unwound."""
            tid = _trigger_id(resp)
            if attach and tid:
                attached_trigger_ids.append(tid)

        async def unwind() -> None:
            """Undo what THIS call created when a later step fails. A fresh
            seed owns the whole routine, so it goes; an attach owns only the
            triggers it added — the routine is the owner's own work."""
            if attach:
                for tid in attached_trigger_ids:
                    await _rollback_trigger(
                        admin,
                        trigger_id=tid,
                        cookie=cookie,
                        owner_id=owner_id,
                        provider_key=provider_key,
                    )
                return
            await _rollback_routine(
                admin, routine_id=routine_id, cookie=cookie, owner_id=owner_id, provider_key=provider_key
            )

        if attach:
            # Never trust a client-supplied routine id: the target has to be
            # in the CALLER's own company — the same tenancy fence the
            # assignee check below applies, and the reason the id is safe to
            # interpolate into the trigger-create path afterwards.
            routines = _routine_rows(await admin.list_routines(session_cookie=cookie, company_id=company_id))
            if not any(isinstance(r, dict) and r.get("id") == routine_id for r in routines):
                raise ValueError(f"automation {routine_id!r} is not in this workspace")
        else:
            agents = _agent_rows(await admin.list_agents(session_cookie=cookie, company_id=company_id))
            if assignee_agent_id is not None:
                # Never trust a client-supplied agent id: it has to be on the
                # CALLER's own company roster, the same tenancy fence every Teams
                # route applies via ctx.company_id. The roster is already in hand.
                if not any(isinstance(a, dict) and a.get("id") == assignee_agent_id for a in agents):
                    raise ValueError(f"agent {assignee_agent_id!r} is not in this workspace")
                agent_id = assignee_agent_id
            else:
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
                claim(
                    await admin.create_routine_trigger(
                        session_cookie=cookie,
                        routine_id=routine_id,
                        body=_build_schedule_trigger_body(_GMAIL_POLL_CRON),
                    )
                )
                # poll has no webhook and no maintenance clause — the whole
                # behaviour IS the starter prompt, so an attach appends it
                # (the create path already rendered it as the description).
                # The REGISTRY's rendering, not `description`: an owner's own
                # instructions override belongs to a routine this call CREATES,
                # never inside a block stamped as platform-owned.
                if attach:
                    await _append_platform_block(
                        admin, cookie=cookie, routine_id=routine_id, block=rendered_description
                    )
            except Exception:
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded poll routine=%s owner=%s account=%s event=%s",
                routine_id,
                owner_id,
                resource,
                event_id_text,
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
                    # Paperclip rejects a label over 120 chars outright, which
                    # a joined multi-event title reaches (5 Stripe events is
                    # already 139). Only multi-event titles fall back — a
                    # single-event label is whatever it always was, including
                    # a long custom title, which this must not silently
                    # rewrite behind the owner's back.
                    label=title if len(events) == 1 or len(title) <= _TRIGGER_LABEL_MAX else compact_name,
                ),
            )

            trigger = trigger_resp.get("trigger") if isinstance(trigger_resp, dict) else None
            # Claimed for unwinding BEFORE anything below can raise — losing
            # the id would strand this trigger on the owner's routine.
            claim(trigger_resp)
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
            # auto_meta joins them for a different reason: Meta signs with the
            # APP secret, so the trigger's own secret is what the app-level
            # ingress injects as the bearer AFTER verifying that signature.
            webhook_secret: str | None = None
            if registration in ("auto_github", "auto_google_watch", "owner_setup", "auto_meta") or (
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
            # Something already exists upstream at this point. If the trigger
            # call itself throws (timeout, upstream error) or comes back
            # without a usable publicId, a freshly seeded routine is an orphan
            # the owner can't see or arm, and an attached trigger is dead
            # weight on a routine they wrote — unwind before re-raising.
            await unwind()
            raise

        proxy_url = trigger_fire_url(owner_id, public_id)

        if registration == "guided_paste":
            logger.info(
                "inbound_triggers: seeded guided_paste routine=%s trigger=%s owner=%s provider=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                provider_key,
                event_id_text,
            )
            # Per-provider copy from the registry when present — every dashboard
            # buries webhooks somewhere different, and a wrong path strands the
            # owner mid-setup. The generic sentence is only the fallback.
            instructions = _guided_instructions(
                provider,
                inbound,
                proxy_url=proxy_url,
                event_label_text=event_label_text,
                event_id_text=event_id_text,
                count=len(events),
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
                    # EVERY selected event, on the one hook — that is the
                    # whole point of multi-select: one endpoint, many events.
                    events=_provider_event_names(events),
                    secret=webhook_secret,
                )
            except Exception:
                # GitHub refused the hook (no admin on the repo, duplicate,
                # outage) — the routine+trigger are unusable, unwind them.
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded auto_github routine=%s trigger=%s owner=%s repo=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id_text,
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
                    # Every selected event on the one webhook, same as the
                    # GitHub hook above.
                    events=_provider_event_names(events),
                )
            except Exception:
                # Vercel refused the webhook (plan/token can't create them,
                # outage) — the routine+trigger are unusable, unwind them.
                await unwind()
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
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded auto_vercel routine=%s trigger=%s owner=%s project=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id_text,
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

        if registration == "auto_meta":
            assert meta_asset and webhook_secret  # narrowed by the pre-checks above
            # The union of every selected event's Graph fields — meta_webhook
            # routes a delivery by intersecting these with the payload's.
            fields = _provider_event_names(events)
            try:
                await _subscribe_meta_asset(meta_asset, fields)
            except Exception:
                # Meta refused the binding (not an admin of the asset, missing
                # app permission, outage) — the routine+trigger are unusable.
                await unwind()
                raise
            try:
                # The routing map for the app-level ingress. A delivery names
                # only the asset id, so this entry IS how the route finds the
                # owner, the trigger, and the bearer to fire it with.
                bindings = await integrations_repo.get_meta_webhook_bindings(owner_id, provider_key)
                bindings.append(
                    {
                        "asset_id": meta_asset["id"],
                        "asset_name": meta_asset["label"],
                        "object_type": meta_asset["object_type"],
                        "event_id": event.id,
                        "fields": fields,
                        "trigger_public_id": public_id,
                        "trigger_secret": webhook_secret,
                    }
                )
                await integrations_repo.set_meta_webhook_bindings(owner_id, provider_key, bindings)
            except Exception:
                # Subscribed but unroutable: Meta would deliver into a fleet
                # that can't resolve the asset. Unbind (best-effort) first.
                await _unsubscribe_meta_asset(meta_asset)
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded auto_meta routine=%s trigger=%s owner=%s provider=%s asset=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                provider_key,
                meta_asset["id"],
                event_id_text,
            )
            return {
                "routine_id": routine_id,
                "trigger_public_id": public_id,
                "proxy_url": proxy_url,
                "guided": False,
                "instructions": None,
                "secret_hint": None,
                # The asset NAME, not the id — a numeric Page id in the success
                # copy tells the owner nothing about which Page went live.
                "resource": meta_asset["label"],
            }

        if registration == "auto_google_watch":
            assert resource and webhook_secret  # narrowed by the pre-checks above
            is_calendar = event.id == google_watch.CALENDAR_EVENT
            clause = (_CALENDAR_MAINTENANCE_CLAUSE if is_calendar else _DRIVE_MAINTENANCE_CLAUSE).format(
                path=f"{_WATCH_STATE_AGENT_DIR}/{public_id}.json",
                address=proxy_url,
                account=resource,
            )
            if not attach:
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
                    await unwind()
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
                await unwind()
                raise
            try:
                # The renewal trigger + state file are what keep the ≤7-day
                # channel alive; without either the automation silently dies
                # within a week — same severity as the watch itself failing,
                # so stop the just-created channel (best-effort, never masks
                # this error) and unwind.
                claim(
                    await admin.create_routine_trigger(
                        session_cookie=cookie,
                        routine_id=routine_id,
                        body=_build_schedule_trigger_body(_GOOGLE_RENEWAL_CRON, label=_GOOGLE_RENEWAL_TRIGGER_LABEL),
                    )
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
                await _write_watch_state(owner_id, public_id, state)
                # Attach: the owner's own instructions stay first and whole —
                # the renewal step is appended as a delimited block, LAST, so
                # a failure anywhere above never half-writes their body.
                if attach:
                    await _append_platform_block(admin, cookie=cookie, routine_id=routine_id, block=clause)
            except Exception:
                await google_watch.stop_watch(
                    owner_id=owner_id,
                    account_email=resource,
                    provider_event=event.id,
                    channel_id=channel["channel_id"],
                    resource_id=channel["resource_id"],
                )
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded auto_google_watch routine=%s trigger=%s owner=%s account=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id_text,
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
                path=f"{_WATCH_STATE_AGENT_DIR}/{public_id}.json",
                account=resource,
            )
            if not attach:
                try:
                    # Same description patch as auto_google_watch above: the
                    # state-file path needs the trigger's public id.
                    await admin.patch_routine(
                        session_cookie=cookie,
                        routine_id=routine_id,
                        body={"description": f"{clause}\n\n{description}"},
                    )
                except Exception:
                    await unwind()
                    raise
            try:
                watch = await google_watch.start_gmail_watch(owner_id=owner_id, account_email=resource, topic=topic)
            except Exception:
                # gog / Google refused the watch — broken grant, revoked
                # account, API outage, or a topic Gmail can't publish to yet
                # (an owner who hasn't done the Pub/Sub setup retries this
                # create afterwards; the error names Google's reason). The
                # routine+trigger are unusable either way — unwind them.
                await unwind()
                raise
            try:
                # Renewal trigger + state file, same posture as the push
                # channels. The gmail state file carries NO secret: renewal
                # is `gog gmail watch renew -a <account>`, which re-arms
                # from the topic/labels gog stored at start time.
                claim(
                    await admin.create_routine_trigger(
                        session_cookie=cookie,
                        routine_id=routine_id,
                        body=_build_schedule_trigger_body(_GOOGLE_RENEWAL_CRON, label=_GOOGLE_RENEWAL_TRIGGER_LABEL),
                    )
                )
                await _write_watch_state(
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
                # Attach: appended last, same posture as auto_google_watch.
                if attach:
                    await _append_platform_block(admin, cookie=cookie, routine_id=routine_id, block=clause)
            except Exception:
                # Without the renewal trigger the watch dies within a week;
                # without its row entry the proxy has no bearer secret to
                # inject and deliveries would 401 forever. Either way the
                # automation is unusable — stop the watch (best-effort)
                # before unwinding.
                await google_watch.stop_gmail_watch(owner_id=owner_id, account_email=resource)
                # The row above may already have landed (only the attach-path
                # append comes after it), and it names a trigger `unwind` is
                # about to delete. Nothing else ever prunes this list, so drop
                # the entry here or it accumulates forever. Best-effort: this
                # runs inside an except and must never mask the real error.
                # (auto_google_watch's counterpart orphan is a private EFS
                # state file the next seed overwrites by public id — left
                # alone deliberately.)
                try:
                    rows = await integrations_repo.get_google_watch_channels(owner_id)
                    kept = [c for c in rows if c.get("trigger_public_id") != public_id]
                    if len(kept) != len(rows):
                        await integrations_repo.set_google_watch_channels(owner_id, kept)
                except Exception:
                    logger.exception(
                        "inbound_triggers: could not drop the stranded gmail watch row for trigger=%s owner=%s",
                        public_id,
                        owner_id,
                    )
                await unwind()
                raise
            logger.info(
                "inbound_triggers: seeded owner_setup routine=%s trigger=%s owner=%s account=%s event=%s",
                routine_id,
                public_id,
                owner_id,
                resource,
                event_id_text,
            )
            instructions = _gmail_setup_instructions(topic, proxy_url)
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
        await unwind()
        raise ValueError(f"unsupported registration {registration!r}")
