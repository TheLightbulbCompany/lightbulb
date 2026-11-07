"""Shared helpers for the unauthenticated public ingress routers (meta_webhook,
hooks, launch): a per-key rate bucket, the upstream client
factory, and the caller-IP read.

**Rate limit.** In-process sliding-window bucket, 60/min per key. RULING D17:
per-instance window, so at desiredCount 2-4 each instance keeps its own bucket and
the effective ceiling is 2-4x this value, not a hard cap.
"""

import httpx
from fastapi import Request

from core.services.rate_bucket import RateBucket

RATE_LIMIT_PER_MINUTE = 60

# Module-level so the bucket survives across requests inside one process.
_RATE = RateBucket(RATE_LIMIT_PER_MINUTE)


def _client_factory() -> httpx.AsyncClient:
    """Build the upstream HTTP client. Module-level so tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


def _check_rate_limit(key: str) -> bool:
    """In-process sliding-window token bucket. True = allowed."""
    return _RATE.allow(key)


def _extract_caller_ip(request: Request) -> str:
    """Get the immediate TCP-peer IP, ignoring inbound X-Forwarded-* (untrusted).

    In production this is the ALB's private IP. When uvicorn is configured with
    ``forwarded_allow_ips`` covering the ALB's subnet, starlette already surfaces the
    trusted rightmost XFF entry (= the real client) as ``request.client.host`` — so this
    returns the real-client IP without trusting user-controlled headers.
    """
    return request.client.host if request.client else "0.0.0.0"
