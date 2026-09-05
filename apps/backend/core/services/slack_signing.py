"""Slack request signing (v0) — https://docs.slack.dev/authentication/verifying-requests-from-slack

Two callers: the events route VERIFIES Slack's signature with the platform
app's signing secret, then RE-SIGNS the identical body with the owner's
``hook_token`` so the tenant container's Bolt receiver can verify it without
ever holding the app-wide secret (spec §3.2). Pure functions; no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import time

VERSION = "v0"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``v0=`` + hex(HMAC-SHA256(secret, ``v0:{timestamp}:{body}``))."""
    basestring = f"{VERSION}:{timestamp}:".encode() + body
    return f"{VERSION}=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    *,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Constant-time check of ``signature`` against ``body``/``timestamp``, and a
    replay guard: a timestamp more than ``tolerance_seconds`` from ``now`` is
    refused. Missing or malformed inputs are simply False — the caller is a
    machine, not a person."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
