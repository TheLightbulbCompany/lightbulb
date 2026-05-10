"""Public webhook ingress proxy for per-user OpenClaw containers.

External webhook senders (Zapier / Stripe / GitHub / custom integrations)
POST to ``/api/v1/hooks/u/{owner_id}/{hook_path}`` with bearer-token
auth. We validate the token at the edge against the ``containers`` row,
look up the user's container IP via ``EcsManager.resolve_running_container``,
rewrite X-Forwarded-* with the true caller IP, and forward to
``http://<ip>:18789/hooks/{hook_path}``.

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

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.containers import get_ecs_manager
from core.repositories import container_repo, hook_event_repo

logger = logging.getLogger(__name__)
router = APIRouter()

GATEWAY_PORT = 18789
RATE_LIMIT_PER_MINUTE = 60

# Module-level so the bucket survives across requests inside one process.
# Multi-task fleets get independent buckets — fine for v1, flagged in spec.
_RATE_BUCKETS_PER_OWNER: dict[str, deque[float]] = defaultdict(deque)

# Module-level instance so tests can patch
# ``routers.hooks_proxy.ecs_manager.resolve_running_container``.
ecs_manager = get_ecs_manager()


def _client_factory() -> httpx.AsyncClient:
    """Build the upstream HTTP client. Module-level so tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


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


def _build_forwarded_headers(request: Request, hook_token: str) -> dict[str, str]:
    """Strip inbound X-Forwarded-* and re-assert them with values we control."""
    caller_ip = _extract_caller_ip(request)
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in ("host", "content-length", "authorization"):
            continue
        if kl.startswith("x-forwarded-"):
            continue
        headers[k] = v
    headers["Authorization"] = f"Bearer {hook_token}"
    headers["X-Forwarded-For"] = caller_ip
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Host"] = "api.isol8.co"
    return headers


@router.post("/u/{owner_id}/{hook_path:path}")
async def proxy_hook(owner_id: str, hook_path: str, request: Request) -> Response:
    """Forward an external webhook call to the user's OpenClaw container.

    Bearer-token authenticated against ``containers.hook_token`` via timing-safe
    compare. Resolves the container's private IP via Cloud Map, rewrites
    X-Forwarded-* with the true caller IP, and forwards to the container's
    ``/hooks`` ingress. Audits every call to the ``hook-events`` DDB table.

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

    # 5. Resolve container IP
    _, ip = await ecs_manager.resolve_running_container(owner_id)
    if not ip:
        return Response(
            status_code=503,
            headers={"Retry-After": "30"},
            content=b'{"detail":"container_not_running"}',
            media_type="application/json",
        )

    # 6. Forward
    body = await request.body()
    headers = _build_forwarded_headers(request, expected)
    # Preserve the inbound query string verbatim — webhook senders carry
    # signature/verification material there (Slack URL-verification
    # ``?challenge=``, custom ``?routing_key=``, etc.). Dropping it
    # silently breaks those handshakes.
    query = request.url.query  # raw query string (no leading '?'), '' if absent
    target = f"http://{ip}:{GATEWAY_PORT}/hooks/{hook_path}"
    if query:
        target = f"{target}?{query}"

    upstream_status = 502
    upstream_body = b""
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
        logger.warning("hook proxy upstream error owner=%s err=%s", owner_id, exc)
        upstream_body = b'{"detail":"upstream_unreachable"}'

    # 7. Audit (best-effort — DDB outage must not drop a real webhook)
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
