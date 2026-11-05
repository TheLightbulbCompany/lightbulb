"""Webhook signature checks, dedupe keys and event types, in the backend.

Super-agent workspaces verify webhook deliveries here instead of in Paperclip
(contracts-v2 §11, spec 06 §3.5). Pure: ``hmac``/``hashlib``/``base64`` only, no I/O
except the Google OIDC check, which reuses the trigger proxy's verifier.

Signing modes use the names ``providers.yaml`` already stores (``inbound.signing_mode``),
so registry values and migrated Paperclip triggers map one to one:

- ``hmac_sha256``: Stripe's scheme, HMAC-SHA256 over ``{t}.{body}``, from
  ``Stripe-Signature: t=..,v1=..`` (every ``v1`` is tried, so secret rotation works) or
  ``X-Paperclip-Timestamp`` + ``X-Paperclip-Signature``; replay window applies.
- ``github_hmac``: HMAC-SHA256 hex over the raw body, ``sha256=`` prefix optional, from
  ``X-Hub-Signature-256`` or the registry's ``signature_header`` (Sentry, Linear, Attio).
- ``hmac_sha1``: HMAC-SHA1 hex over the raw body (Vercel's ``x-vercel-signature``).
- ``standard_webhooks``: ``webhook-id``/``webhook-timestamp``/``webhook-signature``
  (``v1,<base64>`` over ``{id}.{ts}.{body}``, secret ``whsec_<base64>``).
- ``bearer``: ``Authorization: Bearer <secret>`` (or Google's ``X-Goog-Channel-Token``).
- ``google_channel_token``: ``X-Goog-Channel-Token`` only.
- ``none``: no check (only migrated hooks; the repo refuses it for new ones).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping

_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _hex(algo, secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, algo).hexdigest()


def _stale(ts: str, window: int, now: float) -> bool:
    try:
        t = float(ts)
    except ValueError:
        return True
    if t > 1e11:  # milliseconds
        t /= 1000
    return abs(now - t) > window


def verify(
    mode: str,
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    replay_window_s: int = 300,
    signature_header: str | None = None,
    now: float | None = None,
) -> str | None:
    """None when the delivery is genuine, else ``bad_signature | stale | missing | not_armed``."""
    if mode == "none":
        return None
    if not secret:
        return "not_armed"
    h = _lower(headers)
    now = time.time() if now is None else now

    if mode == "hmac_sha256":
        sigs: list[str] = []
        ts: str | None = None
        if "stripe-signature" in h:
            for part in h["stripe-signature"].split(","):
                k, _, v = part.strip().partition("=")
                if k == "t" and ts is None:
                    ts = v
                elif k == "v1" and v:
                    sigs.append(v)
        elif "x-paperclip-signature" in h:
            ts = h.get("x-paperclip-timestamp")
            sigs = [h["x-paperclip-signature"].removeprefix("sha256=")]
        if not ts or not sigs:
            return "missing"
        if _stale(ts, replay_window_s, now):
            return "stale"
        want = _hex(hashlib.sha256, secret, ts.encode() + b"." + body)
        return None if any(_eq(want, s) for s in sigs) else "bad_signature"

    if mode in ("github_hmac", "hmac_sha1"):
        default = "x-hub-signature-256" if mode == "github_hmac" else "x-vercel-signature"
        got = h.get((signature_header or default).lower())
        if not got:
            return "missing"
        algo = hashlib.sha256 if mode == "github_hmac" else hashlib.sha1
        got = got.removeprefix("sha256=" if mode == "github_hmac" else "sha1=")
        return None if _eq(_hex(algo, secret, body), got) else "bad_signature"

    if mode == "standard_webhooks":
        wid, ts, sig = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
        if not (wid and ts and sig):
            return "missing"
        if _stale(ts, replay_window_s, now):
            return "stale"
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except ValueError:
            return "bad_signature"
        want = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        got = [p.partition(",")[2] for p in sig.split() if p.startswith("v1,")]
        return None if any(_eq(want, g) for g in got) else "bad_signature"

    if mode in ("bearer", "google_channel_token"):
        token = h.get("x-goog-channel-token")
        if mode == "bearer" and not token:
            scheme, _, value = h.get("authorization", "").partition(" ")
            token = value if scheme.lower() == "bearer" else None
        if not token:
            return "missing"
        return None if _eq(secret, token) else "bad_signature"

    return "bad_signature"  # unknown mode fails closed


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dedupe_key(provider: str, headers: Mapping[str, str], body: bytes) -> str:
    """Stable id of one delivery, the same across the provider's retries.

    Always matches ``^[A-Za-z0-9:_-]{1,128}$`` (safe in trigger ids and S3 keys);
    a provider id outside that shape is hashed."""
    h = _lower(headers)
    data = _json(body)
    msg = data.get("message")
    raw: str | None = None
    if provider == "stripe" and data.get("id"):
        raw = str(data["id"])
    elif h.get("x-github-delivery"):
        raw = h["x-github-delivery"]
    elif isinstance(msg, dict) and msg.get("messageId"):
        raw = str(msg["messageId"])
    elif h.get("x-goog-channel-id") and h.get("x-goog-message-number"):
        raw = f"{h['x-goog-channel-id']}:{h['x-goog-message-number']}"
    elif h.get("linear-delivery"):
        raw = h["linear-delivery"]
    elif h.get("webhook-id"):
        raw = h["webhook-id"]
    if raw and _KEY_RE.match(raw):
        return raw
    return hashlib.sha256(raw.encode() if raw else body).hexdigest()


def event_type(provider: str, headers: Mapping[str, str], body: bytes) -> str | None:
    """The event id as ``providers.yaml`` names it (the ids ``HOOK#.events`` filters on).

    None = unknown, which the caller treats as "matches every filter"."""
    h = _lower(headers)
    if provider == "github":
        return h.get("x-github-event") or None
    if provider == "sentry":
        return h.get("sentry-hook-resource") or None
    if provider in ("stripe", "vercel", "linear"):
        value = _json(body).get("type")
        return str(value) if value else None
    return None


def verify_google_oidc(token: str | None, audience: str) -> bool:
    """Gmail Pub/Sub push: a live Google-signed OIDC JWT for ``audience`` (blocking)."""
    if not token:
        return False
    from routers.paperclip_trigger_proxy import _verify_google_oidc_sync

    return _verify_google_oidc_sync(token, audience)
