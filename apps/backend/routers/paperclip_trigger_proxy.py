"""Public ingress proxy for Paperclip routine webhook triggers.

Tier 2 of the two-tier webhook ingress: container hooks (``hooks_proxy``)
are the doorbell that wakes an OpenClaw container; routine triggers are
run-tracked loops inside Paperclip with per-trigger signing, replay
windows, idempotency keys, and a circuit breaker. The Paperclip BFF is
in-VPC only — this route gives its signed fire endpoint
(``POST /api/routine-triggers/public/{publicId}/fire``) a public front
door on api.isol8.co.

**Auth model.** Like ``hooks_proxy``, this router deliberately does NOT
use ``Depends(get_current_user)`` — external webhook senders (GitHub,
Stripe, Zapier) can't hold Clerk tokens. Unlike ``hooks_proxy`` there is
no edge token either: the REAL authentication is Paperclip's per-trigger
signing (bearer / hmac_sha256 via X-Paperclip-Signature+Timestamp /
github_hmac via X-Hub-Signature-256), verified upstream against the
trigger row.

**Owner binding.** The ``owner_id`` path segment drives edge gating
(entitlement, rate limits, audit) — and because the upstream fire is
keyed purely by ``public_id``, the segment MUST NOT be trusted bare: a
trial owner could name a paid owner's id and ride their entitlement.
Before anything is forwarded we verify the trigger actually belongs to
the named owner's Paperclip company, by listing the company's routines
with the cached instance-admin session (the bootstrap admin holds owner
membership on every provisioned company) and matching ``publicId`` on
the webhook triggers. Positive bindings are cached in-process for an
hour — company↔trigger ownership never migrates, so a stale entry can
at worst forward a fire for a since-deleted trigger, which upstream
404s. Entitlement itself is re-checked on EVERY request against the
billing row of the verified owner, so downgrades bite immediately.
Resolution failure fails CLOSED (503). A fork-side resolve endpoint
(``publicId`` → company) would collapse the list+detail fan-out to one
GET — follow-up, not v1.

**Byte-verbatim forwarding.** The hmac_sha256 and github_hmac schemes
sign the raw request body; any re-serialization breaks the signature.
The body is forwarded untouched, and only the signing-relevant headers
(Authorization, X-Paperclip-Signature, X-Paperclip-Timestamp,
X-Hub-Signature-256, Idempotency-Key, Content-Type) plus the query
string pass through. Inbound X-Forwarded-* are dropped (spoofable) and
re-asserted with values we control. Upstream status + body return
verbatim.

**Rate limits.** Two sliding-window buckets (shared helper with
``hooks_proxy``), designed so junk traffic can't starve a legitimate
trigger:

- ``trigger:{public_id}`` — 60/min per TRIGGER, charged only after the
  binding is verified. Keys are bounded by real triggers; one abused
  trigger never 429s its siblings.
- ``trigger-resolve:{owner_id}`` — 60/min per owner for UNCACHED binding
  lookups, bounding the Paperclip fan-out an unauthenticated flood can
  force. Cache hits skip it, so an in-flight flood of garbage public_ids
  cannot block already-bound triggers.

**Audit.** Same posture as ``hooks_proxy``: best-effort row to
``hook-events`` (kind="paperclip_trigger").

**Stripe edge verification.** Stripe imposes its own ``whsec_`` signing
secret that Paperclip's engine can't ingest, so a registry provider that
declares ``inbound.signing_mode: stripe_edge`` (see
``core/services/credentials/registry.py``) gets its trigger armed by the
seed service with Paperclip ``signingMode: "none"`` and verified HERE
instead. The discriminator is NOT signingMode — "None (unsigned)" is also a
first-class Phase 1 option (``webhookSigning.ts``) — but the per-trigger
credential record (``core/services/credentials/stripe_edge.py``,
``integration_secret_store`` keyed by ``(owner_id, public_id)``). Three
states, resolved by ``_stripe_trigger_cred`` and cached per-trigger:

- **no record** — an ordinary trigger (including a deliberately-unsigned
  Phase 1 one). Forwards untouched; the publicId remains its shared secret.
- **marker only** (``stripe_edge_seeded``, whsec_ not pasted yet — written
  by the seed service before the owner pastes anything back) — a Stripe
  trigger that isn't armed. **Fails CLOSED (401)**, never forwarded
  unsigned, until the whsec_ lands (Stripe retries in the meantime).
- **whsec_ stored** — this fire must carry a valid ``Stripe-Signature``,
  checked before the forward. The event id is then forwarded as an
  ``Idempotency-Key`` so Stripe's own delivery retries (Stripe sends no
  such header itself) don't double-fire the routine — Paperclip dedupes
  a public-trigger fire on that key.

A genuine store failure (throttle, network, IAM — anything other than
"definitively no record") is never cached and never silently treated as
"no record": it 503s the fire rather than 500ing or fail-opening a Stripe
trigger through unverified.
"""

import hashlib
import hmac
import json
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories import billing_repo, hook_event_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.credentials import integration_secret_store
from core.services.credentials.stripe_edge import (
    STRIPE_SECRET_CACHE as _STRIPE_SECRET_CACHE,
)
from core.services.credentials.stripe_edge import (
    STRIPE_SECRET_NEGATIVE_TTL_SECONDS as _STRIPE_SECRET_NEGATIVE_TTL_SECONDS,
)
from core.services.credentials.stripe_edge import (
    STRIPE_SECRET_TTL_SECONDS as _STRIPE_SECRET_TTL_SECONDS,
)
from core.services.credentials.stripe_edge import (
    WEBHOOK_SECRET_FIELD,
    stripe_secret_cache_generation,
    stripe_trigger_secret_provider,
)
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.provision_gate import has_always_on_entitlement
from routers.hooks_proxy import _check_rate_limit, _client_factory, _extract_caller_ip

logger = logging.getLogger(__name__)
router = APIRouter()

# Headers Paperclip's trigger verification reads (plus Content-Type).
# Everything else — cookies, Clerk tokens, spoofed X-Forwarded-* — is dropped.
_FORWARDED_HEADERS = (
    "Authorization",
    "X-Paperclip-Signature",
    "X-Paperclip-Timestamp",
    "X-Hub-Signature-256",
    "Idempotency-Key",
    "Content-Type",
)

# Verified (owner_id, public_id) bindings. Ownership never migrates, so a
# long TTL is safe (see module docstring). Only positives are cached —
# misses are bounded by the trigger-resolve rate bucket instead.
# ponytail: in-process cache; multi-task fleets re-resolve independently,
# which is correct just slightly chattier.
_BINDING_TTL_SECONDS = 3600
_BINDING_CACHE: dict[tuple[str, str], float] = {}


def _binding_cached(owner_id: str, public_id: str) -> bool:
    ts = _BINDING_CACHE.get((owner_id, public_id))
    return ts is not None and (time.monotonic() - ts) < _BINDING_TTL_SECONDS


# The resolved credential record (dict, or None = no record) for a
# (owner_id, public_id), same cache shape as _BINDING_CACHE but a SPLIT TTL
# keyed on whether the trigger is ARMED: an armed record (whsec_ present)
# lives for _STRIPE_SECRET_TTL_SECONDS (5 min — bounds cross-task rotation
# staleness); a non-armed one (no record, or the seeded marker only) lives
# for only _STRIPE_SECRET_NEGATIVE_TTL_SECONDS (60s). Every trigger fire of
# every kind hits this path, so an uncached read would mean a live Secrets
# Manager GetSecretValue on every single fire; caching is what makes that
# free after the first lookup. Storing the whole record (not just the
# secret) is what lets the caller tell a seeded-but-unarmed trigger (marker
# only -> fail CLOSED) apart from an ordinary unsigned one (no record ->
# forward) — see stripe_edge.py.
#
# Owned by ``core.services.credentials.stripe_edge`` (imported above by
# reference, not copied) so the webhook-secret WRITE path
# (``routers/automations.py``) can invalidate it via
# ``stripe_edge.invalidate_stripe_secret_cache`` right after every store.
# On a single task that's immediate; on a multi-task fleet a sibling task's
# stale verdict is bounded by whichever of the two TTLs above applies.


class _StripeSecretStoreError(Exception):
    """Secrets Manager errored while resolving a trigger's credential record
    — NOT the same as "no record" (that's ResourceNotFoundException, already
    collapsed to None by integration_secret_store.get). The caller turns
    this into a 503, never a 500 and never a silent fail-open forward of an
    unverified Stripe fire."""


async def _stripe_trigger_cred(owner_id: str, public_id: str) -> dict | None:
    """The stored credential record for THIS trigger, or None if the owner
    never seeded a stripe_edge trigger here. Three shapes the caller
    distinguishes (see module docstring): ``None`` (no record — ordinary
    trigger, forward unsigned); ``{stripe_edge_seeded: True}`` (seeded,
    whsec_ not pasted — fail CLOSED); ``{webhook_secret: ...}`` (armed —
    verify). Nothing in this module ever WRITES this record.

    Cached per (owner_id, public_id) with a SPLIT TTL — an armed record (has
    a whsec_) for _STRIPE_SECRET_TTL_SECONDS, a non-armed one (None or
    marker-only) for the much shorter _STRIPE_SECRET_NEGATIVE_TTL_SECONDS
    (see the cache's module-level comment). Raises _StripeSecretStoreError
    (uncached) on a genuine store failure.

    Write-during-read TOCTOU guard: the generation is captured BEFORE the
    Secrets Manager call, and the result is only written into the cache if
    the generation is still unchanged afterward. A concurrent
    ``invalidate_stripe_secret_cache`` (the webhook-secret PUT, right after
    its own store) bumps the generation, so a read that straddles that
    write discards its own now-stale result instead of clobbering the
    invalidate — see ``stripe_edge.py``'s module docstring.
    """
    key = (owner_id, public_id)
    cached = _STRIPE_SECRET_CACHE.get(key)
    if cached is not None:
        cached_at, cached_cred = cached
        armed = bool(cached_cred and cached_cred.get(WEBHOOK_SECRET_FIELD))
        ttl = _STRIPE_SECRET_TTL_SECONDS if armed else _STRIPE_SECRET_NEGATIVE_TTL_SECONDS
        if (time.monotonic() - cached_at) < ttl:
            return cached_cred
    generation = stripe_secret_cache_generation(owner_id, public_id)
    try:
        cred = await integration_secret_store.get(owner_id, stripe_trigger_secret_provider(public_id))
    except Exception as exc:  # noqa: BLE001 — any non-ResourceNotFound Secrets Manager failure
        raise _StripeSecretStoreError from exc
    if stripe_secret_cache_generation(owner_id, public_id) == generation:
        _STRIPE_SECRET_CACHE[key] = (time.monotonic(), cred)
    return cred


def verify_stripe_signature(secret: str, raw_body: bytes, header: str, *, tolerance_s: int = 300, now: float) -> bool:
    """Verify a Stripe ``Stripe-Signature: t=<unixSeconds>,v1=<hexHmac>``
    header against ``raw_body`` with the owner's ``whsec_`` secret — stdlib
    HMAC-SHA256, the same scheme ``stripe.Webhook.construct_event``
    implements (https://docs.stripe.com/webhooks#verify-manually), so this
    can run at the edge with no Stripe SDK dependency. The signed payload is
    ``f"{t}.{raw_body}"``; a header may carry multiple ``v1=`` entries
    (secret rotation) and any match is accepted.
    """
    t: str | None = None
    v1s: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            t = value
        elif key == "v1":
            v1s.append(value)
    if t is None or not v1s:
        return False
    try:
        timestamp = int(t)
    except ValueError:
        return False
    if abs(now - timestamp) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v1) for v1 in v1s)


async def _company_id_for_owner(owner_id: str) -> str | None:
    repo = PaperclipRepo(table_name="paperclip-companies")
    if is_org_owner_id(owner_id):
        return await repo.get_org_company_id(owner_id)
    row = await repo.get(owner_id)
    return row.company_id if row is not None and row.status == "active" else None


async def _lookup_trigger_detail(owner_id: str, public_id: str) -> dict | None:
    """Ask Paperclip for the webhook-trigger dict matching ``public_id`` in
    the owner's company (``kind``, ``publicId``, ``signingMode``, ...), or
    None if no such trigger exists there. The routines LIST omits publicId,
    so we fetch the detail of each routine that carries a webhook trigger.
    Shared by ``_lookup_binding`` (bool projection, used by the fire route's
    hot path) and ``_resolve_trigger_binding`` (used by the webhook-secret
    PUT, ``routers/automations.py``, to verify ownership before that
    endpoint checks its own stripe-edge-seeded marker)."""
    company_id = await _company_id_for_owner(owner_id)
    if not company_id:
        return None
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        cookie = await get_admin_session_cookie(http)
        client = PaperclipAdminClient(http_client=http)
        routines = await client.list_routines(session_cookie=cookie, company_id=company_id)
        for routine in routines or []:
            if not any(t.get("kind") == "webhook" for t in routine.get("triggers") or []):
                continue
            detail = await client.get_routine(session_cookie=cookie, routine_id=routine["id"])
            for trig in detail.get("triggers") or []:
                if trig.get("kind") == "webhook" and trig.get("publicId") == public_id:
                    return trig
    return None


async def _lookup_binding(owner_id: str, public_id: str) -> bool:
    """Ask Paperclip whether ``public_id`` is a webhook trigger in the
    owner's company."""
    return await _lookup_trigger_detail(owner_id, public_id) is not None


async def _resolve_trigger_binding(owner_id: str, public_id: str) -> bool:
    """Verify + cache the owner↔trigger binding. Raises on upstream
    failure (caller fails closed). Retries once on an expired admin
    session, same as provisioning."""
    try:
        found = await _lookup_binding(owner_id, public_id)
    except PaperclipApiError as e:
        if e.status_code != 401:
            raise
        invalidate_admin_session()
        found = await _lookup_binding(owner_id, public_id)
    if found:
        _BINDING_CACHE[(owner_id, public_id)] = time.monotonic()
    return found


@router.post("/t/{owner_id}/{public_id}")
async def proxy_trigger(owner_id: str, public_id: str, request: Request) -> Response:
    """Forward a signed webhook call to Paperclip's public trigger fire endpoint.

    No edge auth (see module docstring — signature verification happens
    upstream). Gates, in order: entitlement of the URL owner (402),
    owner↔trigger binding so the entitlement can't be borrowed (404 /
    503 on resolver failure), per-trigger rate limit (429). Returns the
    upstream status and body verbatim.
    """
    # publicIds are 24-hex upstream (crypto.randomBytes(12).toString("hex"));
    # the guard blocks path/URL injection and unbounded rate-limit keys.
    if not public_id.isalnum() or len(public_id) > 64:
        raise HTTPException(status_code=400, detail="invalid_public_id")

    # Entitlement gate — triggers are a paid always-on feature. Unknown
    # owner_id fails closed here (no billing row = not entitled), which
    # also keeps garbage owner_ids away from the binding resolver.
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "always_on_requires_subscription",
                "message": "Webhook triggers are available on the paid plan. Upgrade to enable always-on automation.",
            },
        )

    # Binding gate — the trigger must live in THIS owner's company, or the
    # entitlement above was checked against the wrong account.
    if not _binding_cached(owner_id, public_id):
        if not _check_rate_limit(f"trigger-resolve:{owner_id}"):
            return Response(
                status_code=429,
                headers={"Retry-After": "60"},
                content=b'{"detail":"rate_limited"}',
                media_type="application/json",
            )
        try:
            bound = await _resolve_trigger_binding(owner_id, public_id)
        except Exception as exc:  # noqa: BLE001 — fail CLOSED on resolver trouble
            logger.warning("trigger binding resolve failed owner=%s err=%s", owner_id, exc)
            return Response(
                status_code=503,
                headers={"Retry-After": "30"},
                content=b'{"detail":"trigger_resolution_unavailable"}',
                media_type="application/json",
            )
        if not bound:
            raise HTTPException(status_code=404, detail="unknown_trigger")

    # Per-trigger rate limit — charged only for verified bindings, so one
    # abused trigger can't starve its siblings (or the container hooks).
    if not _check_rate_limit(f"trigger:{public_id}"):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    # Raw bytes, untouched — hmac_sha256/github_hmac sign the exact body,
    # and stripe_edge verification (below) signs it too.
    body = await request.body()

    # Stripe edge verification — the per-trigger credential record decides
    # (see module docstring): no record forwards untouched (ordinary/Phase-1
    # trigger); a marker-only record fails CLOSED (seeded stripe_edge, whsec_
    # not pasted yet — never forward a Stripe trigger unsigned); a stored
    # whsec_ is verified, then the event id becomes an Idempotency-Key.
    stripe_event_id: str | None = None
    try:
        stripe_cred = await _stripe_trigger_cred(owner_id, public_id)
    except _StripeSecretStoreError:
        return Response(
            status_code=503,
            headers={"Retry-After": "30"},
            content=b'{"detail":"stripe_verification_unavailable"}',
            media_type="application/json",
        )
    if stripe_cred is not None:
        stripe_secret = stripe_cred.get(WEBHOOK_SECRET_FIELD)
        if not stripe_secret:
            # Seeded stripe_edge trigger still awaiting its whsec_ — reject
            # rather than forward unsigned. Stripe retries until it's armed.
            raise HTTPException(status_code=401, detail="stripe_trigger_not_armed")
        sig_header = request.headers.get("Stripe-Signature")
        if not sig_header or not verify_stripe_signature(stripe_secret, body, sig_header, now=time.time()):
            raise HTTPException(status_code=401, detail="invalid_stripe_signature")
        # The signature-verified body is trusted — pull the Stripe event id
        # for idempotency (Stripe's retries reuse the same ``evt_...`` id).
        try:
            payload = json.loads(body)
            stripe_event_id = payload.get("id") if isinstance(payload, dict) else None
        except ValueError:
            stripe_event_id = None

    if not settings.PAPERCLIP_INTERNAL_URL:
        return Response(
            status_code=503,
            content=b'{"detail":"paperclip_unavailable"}',
            media_type="application/json",
        )

    headers = {h: request.headers[h] for h in _FORWARDED_HEADERS if h in request.headers}
    headers["X-Forwarded-For"] = _extract_caller_ip(request)
    headers["X-Forwarded-Proto"] = "https"
    # Stripe sends no Idempotency-Key of its own; without one Paperclip treats
    # each of Stripe's delivery retries as a fresh fire (duplicate agent run +
    # spend). Derive it from the event id so retries of the same event dedupe.
    if stripe_event_id and "Idempotency-Key" not in headers:
        headers["Idempotency-Key"] = stripe_event_id

    base = settings.PAPERCLIP_INTERNAL_URL.rstrip("/")
    target = f"{base}/api/routine-triggers/public/{public_id}/fire"
    query = request.url.query  # raw query string, '' if absent
    if query:
        target = f"{target}?{query}"

    upstream_status = 502
    upstream_body = b'{"detail":"upstream_unreachable"}'
    upstream_headers: dict[str, str] = {}
    try:
        async with _client_factory() as client:
            upstream = await client.post(target, headers=headers, content=body)
        upstream_status = upstream.status_code
        upstream_body = upstream.content
        upstream_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        }
    except httpx.RequestError as exc:
        logger.warning("trigger proxy upstream error owner=%s err=%s", owner_id, exc)

    # Audit (best-effort — DDB outage must not drop a real webhook)
    try:
        hook_event_repo.write_event(
            owner_id=owner_id,
            hook_path=public_id,
            kind="paperclip_trigger",
            status_code=upstream_status,
            source_ip=_extract_caller_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            request_size=len(body),
            response_size=len(upstream_body),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.warning("hook-events write failed owner=%s err=%s", owner_id, exc)

    return Response(
        status_code=upstream_status,
        content=upstream_body,
        headers=upstream_headers,
        media_type=upstream_headers.get("content-type", "application/json"),
    )
