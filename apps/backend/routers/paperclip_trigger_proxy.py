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

An unarmed trigger (secret not yet pasted) **fails closed for free**:
Paperclip verifies against its own random placeholder secret, which the
provider's signature cannot match, so nothing fires until the real secret is
armed. There is no per-trigger secret lookup, cache, or edge verification in
this proxy anymore — all of that moved into Paperclip.
"""

import json
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories import billing_repo, hook_event_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.provision_gate import has_always_on_entitlement
from routers.hooks_proxy import _check_rate_limit, _client_factory, _extract_caller_ip

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

    # Google channel-confirmation ping: every watch create/renew fires one
    # delivery with X-Goog-Resource-State: sync and no meaningful payload.
    # Forwarding it would start a spurious routine run on every (re)creation,
    # so it's acknowledged at the edge. The gates above still applied.
    if request.headers.get("X-Goog-Resource-State") == "sync":
        return Response(status_code=204)

    # Raw bytes, untouched — hmac_sha256/github_hmac sign the exact body.
    body = await request.body()

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
