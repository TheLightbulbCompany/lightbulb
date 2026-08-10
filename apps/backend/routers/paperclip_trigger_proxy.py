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
trigger row. The ``owner_id`` path segment exists ONLY for edge gating —
the always-on entitlement check, the per-owner rate limit, and the audit
row. It authenticates nothing.

**Byte-verbatim forwarding.** The hmac_sha256 and github_hmac schemes
sign the raw request body; any re-serialization breaks the signature.
The body is forwarded untouched, and only the signing-relevant headers
(Authorization, X-Paperclip-Signature, X-Paperclip-Timestamp,
X-Hub-Signature-256, Idempotency-Key, Content-Type) plus the query
string pass through. Inbound X-Forwarded-* are dropped (spoofable) and
re-asserted with values we control. Upstream status + body return
verbatim.

**Audit / rate limit.** Same posture as ``hooks_proxy``: best-effort row
to ``hook-events`` (kind="paperclip_trigger"), in-process sliding-window
60/min per owner in a bucket separate from container hooks.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.config import settings
from core.repositories import billing_repo, hook_event_repo
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


@router.post("/t/{owner_id}/{public_id}")
async def proxy_trigger(owner_id: str, public_id: str, request: Request) -> Response:
    """Forward a signed webhook call to Paperclip's public trigger fire endpoint.

    No edge auth (see module docstring — signature verification happens
    upstream). Gates: always-on entitlement (402), per-owner rate limit
    (429), Paperclip availability (503). Returns the upstream status and
    body verbatim.
    """
    # publicIds are hex (crypto.randomBytes(12).toString("hex") upstream);
    # rejecting non-alphanumerics blocks path/URL injection outright.
    if not public_id.isalnum():
        raise HTTPException(status_code=400, detail="invalid_public_id")

    # Entitlement gate — triggers are a paid always-on feature. Unknown
    # owner_id fails closed here too (no billing row = not entitled),
    # which also keeps garbage owner_ids out of the rate-limit buckets.
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

    # Separate bucket from container hooks so a chatty trigger sender
    # can't starve the owner's container-hook ingress (and vice versa).
    if not _check_rate_limit(f"trigger:{owner_id}"):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    if not settings.PAPERCLIP_INTERNAL_URL:
        return Response(
            status_code=503,
            content=b'{"detail":"paperclip_unavailable"}',
            media_type="application/json",
        )

    # Raw bytes, untouched — hmac_sha256/github_hmac sign the exact body.
    body = await request.body()
    headers = {h: request.headers[h] for h in _FORWARDED_HEADERS if h in request.headers}
    headers["X-Forwarded-For"] = _extract_caller_ip(request)
    headers["X-Forwarded-Proto"] = "https"

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
