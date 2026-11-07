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

from core.observability.product_analytics import capture_product_event
from core.dynamodb import run_in_thread
from core.repositories import google_accounts_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.credentials import integration_secret_store, registry
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_github_token import github_token as _github_token

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
    """Arm ``event_ids`` from ``provider_key``'s registry ``inbound`` declaration as a ``HOOK#`` row (secret in
    Secrets Manager) via ``automation_hooks.seed``, then record the ``automation_armed`` funnel event. No
    Paperclip routine is created.

    One hook carries the whole list (one URL / one provider registration); the events must share a
    registration shape, and the one-watch shapes (``auto_google_watch`` / ``poll`` / ``owner_setup``) take
    exactly one event. ``resource`` is what the auto shapes register against: ``owner/repo`` (auto_github), a
    Vercel project id (auto_vercel), a numeric Meta asset id on the owner's connected account (auto_meta), or a
    connected Google account email (google shapes). ``routine_id`` attaches to an existing automation;
    ``assignee_agent_id`` is its specialist; ``title`` / ``description`` override the registry template.

    Raises ``ValueError`` for any caller-facing input problem (unknown or conflicting events, a resource that
    fails its format or ownership check, a provider that isn't connected, a provider refusing the
    registration); the router maps these to a 4xx.
    """
    # Deduped HERE so the funnel counts what was actually armed — the seed
    # dedupes too, and recording the raw list would report event_count=2 for
    # a caller that sent the same id twice.
    event_ids = list(dict.fromkeys(event_ids))
    # A HOOK# row + Secrets Manager (lane A, Task 8). ``routine_id`` is the automation to attach to;
    # ``assignee_agent_id`` its specialist.
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
