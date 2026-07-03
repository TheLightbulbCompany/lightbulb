"""Public webhook ingress proxy for per-user OpenClaw containers.

External webhook senders (Zapier / Stripe / GitHub / custom integrations)
POST to ``/api/v1/hooks/u/{owner_id}/{hook_path}`` with bearer-token
auth. We validate the token at the edge against the ``containers`` row,
rewrite X-Forwarded-* with the true caller IP, and hand the payload to
the owner's runtime adapter, which resolves the container and forwards
to its ``/hooks/{hook_path}`` ingress.

**Auth model.** This router is the ONLY isol8 router that does NOT use
``Depends(get_current_user)``. External callers don't have Clerk tokens.
Auth is timing-safe bearer-token compare against ``containers.hook_token``
via ``hmac.compare_digest``. Do not add Clerk auth — it would break every
real webhook source.

**Forwarded headers.** X-Forwarded-* values from the public caller are
NOT trusted (they could be spoofed). We strip and re-assert the chain
with values we control. OpenClaw on the container side trusts these
headers because our backend's subnet is in ``gateway.trustedProxies``.

**Audit.** Best-effort write to ``hook-events``. A DDB outage does not
drop a real webhook — failures are logged and swallowed.

**Rate limit.** In-process sliding-window bucket, 60/min per ``owner_id``.
Fine for the current single-task backend; needs DDB-backed counter when
we scale out (flagged in the spec, not v1 work).
"""

import hmac
import logging
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response

from core.config import settings
from core.repositories import container_repo, hook_event_repo
from core.runtime import get_runtime_for_owner
from core.runtime.base import ContainerNotRunningError

logger = logging.getLogger(__name__)
router = APIRouter()

RATE_LIMIT_PER_MINUTE = 60

# Module-level so the bucket survives across requests inside one process.
# Multi-task fleets get independent buckets — fine for v1, flagged in spec.
_RATE_BUCKETS_PER_OWNER: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(owner_id: str) -> bool:
    """In-process sliding-window token bucket. True = allowed."""
    now = time.monotonic()
    cutoff = now - 60.0
    q = _RATE_BUCKETS_PER_OWNER[owner_id]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


def _extract_caller_ip(request: Request) -> str:
    """Get the immediate TCP-peer IP, ignoring inbound X-Forwarded-* (untrusted).

    In production this is the ALB's private IP. When uvicorn is configured
    with ``forwarded_allow_ips`` covering the ALB's subnet, starlette will
    already surface the trusted rightmost XFF entry (= the real client) as
    ``request.client.host`` — so this same code path returns the correct
    real-client IP without trusting user-controlled headers.

    Without uvicorn's forwarded-trust configuration, this returns the ALB's
    private IP. Less informative for forensics, but never spoofable.
    """
    return request.client.host if request.client else "0.0.0.0"


def _build_forwarded_headers(request: Request) -> dict[str, str]:
    """Strip inbound X-Forwarded-* and re-assert them with values we control.

    Inbound ``Authorization`` (the edge hook token) is stripped here; the
    runtime adapter attaches the container-side auth header itself.
    """
    caller_ip = _extract_caller_ip(request)
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in ("host", "content-length", "authorization"):
            continue
        if kl.startswith("x-forwarded-"):
            continue
        headers[k] = v
    headers["X-Forwarded-For"] = caller_ip
    headers["X-Forwarded-Proto"] = "https"
    # Derive from PUBLIC_API_BASE so dev/staging containers see their own
    # hostname instead of the prod literal. Today no upstream consumer reads
    # this value meaningfully, but pinning the prod hostname in non-prod
    # poisons audit trails and pre-stages a footgun for any future OAuth-
    # callback / absolute-URL builder that reads X-Forwarded-Host.
    headers["X-Forwarded-Host"] = urlparse(settings.PUBLIC_API_BASE).netloc
    return headers


@router.post("/u/{owner_id}/{hook_path:path}")
async def proxy_hook(owner_id: str, hook_path: str, request: Request) -> Response:
    """Forward an external webhook call to the user's agent container.

    Bearer-token authenticated against ``containers.hook_token`` via timing-safe
    compare. Rewrites X-Forwarded-* with the true caller IP, then delegates
    container resolution + the forward itself to the owner's runtime adapter.
    Audits every call to the ``hook-events`` DDB table.

    Returns the upstream's status code verbatim, or 401/404/429/503 for
    auth/lookup/rate-limit/availability failures.
    """
    # Defense-in-depth: prevent path-traversal escape from the /hooks/* containment.
    # Without this, a holder of a valid hook_token could probe non-/hooks paths on
    # the OpenClaw gateway via httpx's RFC-3986 normalization. Today every other
    # gateway endpoint requires gateway_token (which the hook_token holder doesn't
    # have), so the impact is limited to /healthz / /ready info disclosure — but
    # this guard pre-empts any future weakening of upstream auth.
    if ".." in hook_path.split("/") or "\\" in hook_path:
        raise HTTPException(status_code=400, detail="invalid_hook_path")

    # 1. Bearer extract
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    presented = auth[len("Bearer ") :]

    # 2. Owner lookup
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="owner_not_found")

    # 3. Timing-safe compare
    expected = container.get("hook_token") or ""
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid_token")

    # 4. Rate limit (after auth so 401s don't fill the bucket)
    if not _check_rate_limit(owner_id):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    # 5. Forward via the owner's runtime — container resolution, the
    # container-side auth header, and the POST mechanics live in the adapter.
    body = await request.body()
    headers = _build_forwarded_headers(request)
    # Preserve the inbound query string verbatim — webhook senders carry
    # signature/verification material there (Slack URL-verification
    # ``?challenge=``, custom ``?routing_key=``, etc.). Dropping it
    # silently breaks those handshakes.
    query = request.url.query  # raw query string (no leading '?'), '' if absent
    target_path = f"{hook_path}?{query}" if query else hook_path

    try:
        upstream_status, upstream_headers, upstream_body = await get_runtime_for_owner(owner_id).forward_hook(
            owner_id, target_path, headers, body
        )
    except ContainerNotRunningError:
        return Response(
            status_code=503,
            headers={"Retry-After": "30"},
            content=b'{"detail":"container_not_running"}',
            media_type="application/json",
        )

    # 6. Audit (best-effort — DDB outage must not drop a real webhook)
    try:
        hook_event_repo.write_event(
            owner_id=owner_id,
            hook_path=hook_path,
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
