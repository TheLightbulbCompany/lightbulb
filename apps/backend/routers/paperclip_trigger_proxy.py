"""Public ingress proxy for Paperclip routine webhook triggers.

Routine triggers are run-tracked loops inside Paperclip with per-trigger signing,
replay windows, idempotency keys, and a circuit breaker. The Paperclip BFF is
in-VPC only — this route gives its signed fire endpoint
(``POST /api/routine-triggers/public/{publicId}/fire``) a public front
door on api.isol8.co.

**Auth model.** This router deliberately does NOT
use ``Depends(get_current_user)`` — external webhook senders (GitHub,
Stripe, Zapier) can't hold Clerk tokens. There is no edge token: the REAL authentication is Paperclip's per-trigger
signing (bearer / hmac_sha256 via X-Paperclip-Signature+Timestamp /
github_hmac via X-Hub-Signature-256), verified upstream against the
trigger row.

**Owner binding.** The ``owner_id`` path segment drives edge gating
(entitlement, rate limits) — and because the upstream fire is
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

**Rate limits.** Two sliding-window buckets (shared helper in
``routers._public_ingress``), designed so junk traffic can't starve a legitimate
trigger:

- ``trigger:{public_id}`` — 60/min per TRIGGER, charged only after the
  binding is verified. Keys are bounded by real triggers; one abused
  trigger never 429s its siblings.
- ``trigger-resolve:{owner_id}`` — 60/min per owner for UNCACHED binding
  lookups, bounding the Paperclip fan-out an unauthenticated flood can
  force. Cache hits skip it, so an in-flight flood of garbage public_ids
  cannot block already-bound triggers.

**Provider signature-header normalization.** Verification is NATIVE — this
proxy holds no secret and does no verification. Paperclip's own
``hmac_sha256`` mode signs ``{timestamp}.{body}`` (Stripe's exact scheme) and
verifies against the trigger's stored secret, which is armed with the
provider's own ``whsec_`` (``PUT /automations/{public_id}/webhook-secret`` ->
Paperclip's rotate-secret with a caller-supplied value). The proxy's only
provider-specific job is a header shim: Stripe packs its timestamp and
signature into ONE ``Stripe-Signature: t=,v1=`` header, whereas Paperclip
reads them from separate ``X-Paperclip-Timestamp`` / ``X-Paperclip-Signature``
headers — so it splits the former into the latter (see
``_normalize_stripe_signature``). It also derives an ``Idempotency-Key`` from
the Stripe event id (Stripe sends none, and its delivery retries reuse the
id) so Paperclip dedupes retries to a single run. A ``github_hmac`` provider
sending ``X-Hub-Signature-256`` needs no shim — that header is forwarded and
read natively. Providers that sign the raw body under their OWN header name
(Sentry-Hook-Signature, Linear-Signature, Attio-Signature — declared as
``inbound.signature_header`` in the credentials registry) get a one-line
copy into ``X-Paperclip-Signature``; their retries carry no stable event id
we derive, so duplicate deliveries can start duplicate runs — the same
behavior as a hand-wired "Any other app" webhook today.

Google push channels (auto_google_watch) send no body signature at all —
they echo back the token set at watch time in ``X-Goog-Channel-Token``,
which the seed set to the trigger's own secret. The proxy maps that header
into ``Authorization: Bearer <token>`` (never clobbering a caller-supplied
Authorization) so Paperclip's native ``bearer`` mode does the timing-safe
compare. A delivery with ``X-Goog-Resource-State: sync`` is Google's
channel-confirmation ping, fired on every watch create/renew — it is
answered 204 at the edge instead of forwarded, or every (re)creation would
start a spurious routine run. ``X-Goog-Resource-State`` / ``X-Goog-Changed``
are forwarded on real deliveries as informational run context.

Gmail push (owner_setup / gmail.message) is the one delivery kind the proxy
DOES verify: Google Pub/Sub push authenticates with a per-delivery OIDC JWT
in the Authorization header (rotates every delivery, so no static-secret
mode upstream can check it). Any Authorization value shaped like a Bearer
JWT (three dot-separated segments — Paperclip trigger secrets never contain
dots) must verify against Google's JWKS with iss ∈ accounts.google.com,
``aud`` equal to this exact proxy URL (what the seed's instructions tell the
owner to set on the push subscription), ``email_verified``, and a live
``exp`` — or the delivery is 401'd at the edge. On success the JWT is
REPLACED with ``Bearer <channel_token>`` from the owner's matching
``gmail_watch`` entry on the integrations row (the trigger's own secret,
written there by the seed), so Paperclip's bearer mode still does the final
timing-safe compare and the fail-closed posture is preserved: a caller who
merely knows the URL cannot fire the trigger without a Google-signed JWT
for OUR audience. Pub/Sub retries reuse ``message.messageId``, so an
``Idempotency-Key`` is derived from it (mirrors the Stripe event-id
derivation). Non-JWT Authorization values pass through untouched.

An unarmed trigger (secret not yet pasted) **fails closed for free**:
Paperclip verifies against its own random placeholder secret, which the
provider's signature cannot match, so nothing fires until the real secret is
armed. There is no per-trigger secret lookup, cache, or edge verification in
this proxy anymore (the Gmail OIDC swap above excepted) — all of that moved
into Paperclip.
"""

import asyncio
import json
import logging
import time

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response

from core.config import settings
from core.repositories import automations_repo, billing_repo, integrations_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.provision_gate import has_always_on_entitlement
from core.services.super_agent_flags import flags_for
from core.services.trigger_fire_url import trigger_fire_url
from routers import hooks
from routers._public_ingress import _check_rate_limit, _client_factory, _extract_caller_ip

logger = logging.getLogger(__name__)
router = APIRouter()

# Headers Paperclip's trigger verification reads (plus Content-Type), and the
# informational X-Goog-* pair a Google push delivery carries (what kind of
# change fired — the notification has no body, so these ARE the run context).
# Everything else — cookies, Clerk tokens, spoofed X-Forwarded-* — is dropped.
_FORWARDED_HEADERS = (
    "Authorization",
    "X-Paperclip-Signature",
    "X-Paperclip-Timestamp",
    "X-Hub-Signature-256",
    "X-Goog-Resource-State",
    "X-Goog-Changed",
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

# --- Gmail push OIDC verification (see module docstring) --------------------

_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Google historically stamps either form; both are Google.
_GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
# Module-level like core/auth.py's Clerk JWKS cache — PyJWKClient does its own
# TTL'd JWKS caching (and per-kid key caching), so verification only hits
# Google's cert endpoint on cache expiry / unknown kid.
_google_jwks_client = jwt.PyJWKClient(_GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)

# (owner_id, public_id) → the bearer secret to inject after OIDC success,
# read from the owner's gmail_watch entry. Same TTL idiom as _BINDING_CACHE
# (the secret is stable for the trigger's lifetime).
_GMAIL_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def _bearer_jwt(value: str | None) -> str | None:
    """The token when an Authorization value is a Bearer JWT (three non-empty
    dot-separated segments), else None. Paperclip trigger secrets are random
    hex/base64url with no dots, so a hand-wired bearer sender never matches
    this shape — its Authorization passes through untouched."""
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    parts = token.split(".")
    return token if len(parts) == 3 and all(parts) else None


def _verify_google_oidc_sync(token: str, audience: str) -> bool:
    """Whether ``token`` is a live Google-signed OIDC JWT for ``audience``.

    Blocking (PyJWKClient fetches over urllib on a cache miss) — call via
    ``asyncio.to_thread``. Any failure — unknown kid, bad signature, wrong
    iss/aud, expired, unverified email — is simply "not verified"; the
    caller 401s without distinguishing why (the sender is a machine)."""
    try:
        key = _google_jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_GOOGLE_ISSUERS,
            options={"require": ["exp"]},
        )
    except Exception:
        return False
    return claims.get("email_verified") is True


async def _gmail_watch_token(owner_id: str, public_id: str) -> str | None:
    """The bearer secret to inject for an OIDC-verified delivery — the
    ``channel_token`` of the owner's watch entry feeding ``public_id``, or
    None when no such entry exists (the caller fails closed)."""
    cached = _GMAIL_TOKEN_CACHE.get((owner_id, public_id))
    if cached and (time.monotonic() - cached[1]) < _BINDING_TTL_SECONDS:
        return cached[0]
    for entry in await integrations_repo.get_google_watch_channels(owner_id):
        if entry.get("trigger_public_id") == public_id and entry.get("channel_token"):
            token = str(entry["channel_token"])
            _GMAIL_TOKEN_CACHE[(owner_id, public_id)] = (token, time.monotonic())
            return token
    return None


def _provider_signature_headers() -> tuple[str, ...]:
    """Provider-native raw-body signature headers, from the registry's
    ``inbound.signature_header`` declarations (Sentry-Hook-Signature,
    Linear-Signature, …). The proxy copies whichever one a delivery carries
    into ``X-Paperclip-Signature`` so Paperclip's ``github_hmac`` mode can
    verify it — which keeps wiring a new raw-body-HMAC provider YAML-only.
    Sorted for a deterministic pick in the never-expected case of a request
    carrying two providers' headers."""
    from core.services.credentials import registry

    return tuple(
        sorted(
            {
                p.inbound.signature_header
                for p in registry.all_providers().values()
                if p.inbound and p.inbound.signature_header
            }
        )
    )


def _binding_cached(owner_id: str, public_id: str) -> bool:
    ts = _BINDING_CACHE.get((owner_id, public_id))
    return ts is not None and (time.monotonic() - ts) < _BINDING_TTL_SECONDS


def _normalize_stripe_signature(header: str) -> tuple[str | None, str | None]:
    """Split a Stripe ``Stripe-Signature: t=<unixSeconds>,v1=<hexHmac>`` header
    into ``(timestamp, signature)`` for Paperclip's native ``hmac_sha256`` mode,
    which reads them from separate ``X-Paperclip-Timestamp`` /
    ``X-Paperclip-Signature`` headers. Returns ``(None, None)`` on a malformed
    header. Only the FIRST ``v1`` is used: during a Stripe endpoint-secret
    rotation Stripe briefly sends a ``v1`` per active secret and Paperclip's
    single-signature check verifies against one — a narrow, self-healing window
    (Stripe retries; it resolves once the old secret is removed).

    # ponytail: first-v1 only; multi-signature verify would need a Paperclip
    # mode change — not worth it for the rare rotation window.
    """
    t: str | None = None
    v1: str | None = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and t is None:
            t = value or None
        elif key == "v1" and v1 is None:
            v1 = value or None
    return t, v1


async def _company_id_for_owner(owner_id: str) -> str | None:
    ws = await PaperclipRepo(table_name="paperclip-companies").get_workspace(owner_id)
    return (ws.company_id or None) if ws is not None else None


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

    # Super-agent workspaces verify and fire in the backend (routers/hooks.py) once the trigger has a
    # HOOK# row; until then (and with the switch off) the delivery takes today's Paperclip path below.
    if await flags_for(owner_id):
        hook = await automations_repo.get_hook(owner_id, public_id)
        if hook is not None:
            return await hooks.fire(owner_id, public_id, request, hook)

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

    # Google channel-confirmation ping: every watch create/renew fires one
    # delivery with X-Goog-Resource-State: sync and no meaningful payload.
    # Forwarding it would start a spurious routine run on every (re)creation,
    # so it's acknowledged at the edge. The gates above still applied.
    if request.headers.get("X-Goog-Resource-State") == "sync":
        return Response(status_code=204)

    # GitHub's equivalent: hook creation fires one `ping` delivery, signed
    # with the hook's secret like any real event — verified live: it started
    # a run the moment auto-registration created the hook. Any 2xx marks the
    # hook healthy in GitHub's UI.
    if request.headers.get("X-GitHub-Event") == "ping":
        return Response(status_code=204)

    # Raw bytes, untouched — hmac_sha256/github_hmac sign the exact body.
    body = await request.body()

    # Gmail push OIDC (see module docstring): a Bearer-JWT-shaped
    # Authorization MUST verify as a Google-signed token whose audience is
    # this exact proxy URL, or the delivery is rejected here — forwarding it
    # would only have Paperclip compare the raw JWT against the trigger
    # secret and 401 anyway, but rejecting at the edge keeps the semantics
    # explicit and the swap below possible. Non-JWT Authorization values are
    # untouched.
    oidc_bearer: str | None = None
    oidc_jwt = _bearer_jwt(request.headers.get("Authorization"))
    if oidc_jwt:
        audience = trigger_fire_url(owner_id, public_id)
        if not await asyncio.to_thread(_verify_google_oidc_sync, oidc_jwt, audience):
            return Response(
                status_code=401,
                content=b'{"detail":"invalid_oidc_token"}',
                media_type="application/json",
            )
        oidc_bearer = await _gmail_watch_token(owner_id, public_id)
        if not oidc_bearer:
            # A verified Google delivery for a trigger with no recorded
            # gmail_watch entry — there is nothing to authenticate to
            # Paperclip with, so fail closed rather than forward a JWT the
            # bearer compare can never match.
            return Response(
                status_code=401,
                content=b'{"detail":"unknown_gmail_watch"}',
                media_type="application/json",
            )

    # Provider header normalization (see module docstring). Verification is
    # native in Paperclip; the proxy only ADAPTS Stripe's combined
    # ``Stripe-Signature: t=,v1=`` into the separate ``X-Paperclip-*`` headers
    # Paperclip's hmac_sha256 mode reads, and derives an idempotency key from
    # the event id (Stripe sends none and its retries reuse it). No secret and
    # no verification live here.
    stripe_ts: str | None = None
    stripe_v1: str | None = None
    stripe_event_id: str | None = None
    stripe_sig = request.headers.get("Stripe-Signature")
    if stripe_sig:
        stripe_ts, stripe_v1 = _normalize_stripe_signature(stripe_sig)
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
    # Feed Paperclip's hmac_sha256 mode the split Stripe timestamp + signature,
    # and the derived idempotency key (so Stripe's delivery retries dedupe to a
    # single routine run). A caller-supplied Idempotency-Key is never overridden.
    if stripe_ts and stripe_v1:
        headers["X-Paperclip-Timestamp"] = stripe_ts
        headers["X-Paperclip-Signature"] = stripe_v1
    if stripe_event_id and "Idempotency-Key" not in headers:
        headers["Idempotency-Key"] = stripe_event_id

    # Google push channels authenticate by echoing back the channel token
    # (set at watch time to the trigger's own secret) and send no
    # Authorization of their own — map it so Paperclip's native bearer mode
    # does the timing-safe compare. A caller-supplied Authorization (a
    # hand-wired bearer sender) is never clobbered.
    goog_token = request.headers.get("X-Goog-Channel-Token")
    if goog_token and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {goog_token}"

    # OIDC-verified Gmail push: swap the per-delivery JWT for the trigger's
    # own bearer secret, and derive an Idempotency-Key from the Pub/Sub
    # messageId (its delivery retries reuse it; same gap-filling posture as
    # the Stripe event-id derivation above).
    if oidc_bearer:
        headers["Authorization"] = f"Bearer {oidc_bearer}"
        if "Idempotency-Key" not in headers:
            try:
                payload = json.loads(body)
                message = payload.get("message") if isinstance(payload, dict) else None
                message_id = message.get("messageId") if isinstance(message, dict) else None
            except ValueError:
                message_id = None
            if message_id:
                headers["Idempotency-Key"] = str(message_id)

    # Raw-body HMAC providers (Sentry/Linear/Attio — registry-declared): copy
    # the provider's own signature header into X-Paperclip-Signature so
    # Paperclip's github_hmac mode reads it. Never clobbers a signature the
    # Stripe split or the caller already supplied — precedence goes to the
    # more specific source, and a delivery only ever carries its own
    # provider's header anyway.
    if "X-Paperclip-Signature" not in headers:
        for header_name in _provider_signature_headers():
            provider_sig = request.headers.get(header_name)
            if provider_sig:
                headers["X-Paperclip-Signature"] = provider_sig.strip()
                break

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

    return Response(
        status_code=upstream_status,
        content=upstream_body,
        headers=upstream_headers,
        media_type=upstream_headers.get("content-type", "application/json"),
    )
