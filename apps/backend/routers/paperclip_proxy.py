# LOAD-BEARING, do not delete (owner ruling 2026-09-24): Paperclip's Better Auth session on
# company[-env] depends on this proxy -- removing it broke Better Auth before, low traffic or not.
"""Reverse proxy for ``company.isol8.co`` → internal Paperclip server.

Validates the Clerk session, signs the user in to Paperclip via Better
Auth (using the per-user random password we encrypted at provisioning
time), forwards the HTTP request to the internal Paperclip server with
the resulting session token, streams the response back, and rewrites
HTML brand strings on the way out. A simple sliding-window circuit
breaker opens for 60s if the upstream 5xx rate crosses 50% in any 30s
window.

This module owns the *router* only. The host-conditional dispatch lives
in T16's middleware in ``main.py``: when an incoming request carries
``X-Isol8-Public-Host: company.isol8.co`` (or the env-specific equivalent)
the middleware mounts this router on the request path; for any other
host the middleware passes through to the normal Isol8 routers.

**Why ``X-Isol8-Public-Host`` and not ``Host`` / ``request.url.hostname``?**
API Gateway HTTP API rewrites the upstream ``Host`` header to the
integration target's DNS name (the ALB), so by the time FastAPI sees
the request the original ``company.isol8.co`` is gone from ``Host``.
``api-stack.ts`` adds a parameter mapping that copies
``$context.domainName`` into ``X-Isol8-Public-Host`` so the original
hostname survives the integration hop. (We can't use
``X-Forwarded-Host`` for this — API Gateway HTTP API blocks parameter
mapping on ``x-forwarded-*`` headers with "Operations on header
x-forwarded-host are restricted".) Starlette's ``request.url.hostname``
reflects the rewritten ``Host`` (the ALB DNS) — useless for dispatch.
Reading the custom header directly is the supported path.

**Auth model (v1, no session caching).**
Every proxied request signs the user in to Paperclip from scratch:

1. Validate the Clerk JWT (standard ``get_current_user`` dep).
2. Look up the user's Paperclip identity row, decrypt the stored
   password.
3. Call ``admin_client.sign_in_user(email, password)`` to get a
   fresh Better Auth session cookie (``_session_cookie`` field on the
   response, captured from the upstream ``Set-Cookie`` header). Better
   Auth verifies the password with bcrypt server-side; the call is
   dominated by network RTT (single-digit ms inside the VPC) — no
   session cache needed for v1.
4. Forward the request to Paperclip with
   ``Cookie: <session_cookie>``. Paperclip's Better Auth config does
   NOT enable the bearer() plugin, so cookie is the only auth path
   that authenticates the upstream call.
5. Forward the upstream's ``Set-Cookie`` to the browser, scoped to
   ``.isol8.co`` so subsequent in-page AJAX requests carry it on
   their own. (Useful for Paperclip's own client-side fetches that
   address ``company.isol8.co`` directly without going through us.)
6. If the response is HTML, rewrite the visible brand strings
   (``<title>Paperclip</title>``, ``og:site_name``).

V2 should cache session tokens (Redis or in-memory with short TTL) and
reuse a long-lived ``httpx.AsyncClient`` so we amortize the connection
pool. T14 keeps it boring: per-request ``AsyncClient`` is only ~1ms
overhead inside the VPC and lets us defer the lifecycle/cleanup
question.

**Circuit breaker.** A small sliding-window counter (30s window, 50%
5xx threshold, 10-request minimum) opens for 60s when the upstream is
clearly degraded. While open, every proxied request returns an HTML
"Teams temporarily unavailable" page in 503 — failing fast prevents us
piling load onto a hurting backend. The counter is in-process and
process-local; that's fine for v1 with a single backend task. Multi-
task fleets get independent counters which is the conservative
(slightly less coordinated) behavior — they all break and heal
independently rather than relying on any shared state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from core.auth import AuthContext, _decode_token, _extract_org_claims, resolve_owner_id
from core.config import settings
from core.encryption import decrypt
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.agent_redaction import strip_agent_secrets
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_session_cookie import (
    is_trusted,
    set_session_on_response,
)
from core.services.user_provision import ensure_user_ready

logger = logging.getLogger(__name__)
router = APIRouter()

# RFC 7230 §6.1 hop-by-hop headers — these never propagate across a proxy.
# We strip them on both directions: from the inbound request before
# forwarding upstream, and from the upstream response before returning
# to the browser. ``Host`` is also dropped (we want httpx to set its
# own based on the upstream base_url), and ``Authorization`` and
# ``Cookie`` are dropped so the browser's Clerk credentials never leak
# to Paperclip — we inject our own server-issued bearer token instead.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Brand-rewrite patterns. Run only on responses with
# ``Content-Type: text/html``. Failure of any rewrite is logged and
# falls through to the original body — never break the page just to
# tweak the title.
_BRAND_REWRITES: tuple[tuple[re.Pattern[bytes], bytes], ...] = (
    (
        re.compile(rb"<title>Paperclip</title>", re.IGNORECASE),
        b"<title>Lightbulb Teams</title>",
    ),
    (
        re.compile(
            rb'(<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\'])Paperclip',
            re.IGNORECASE,
        ),
        rb"\1Lightbulb",
    ),
)

# --- Circuit breaker (per spec §6) ---
# Sliding window: any 5xx response from the upstream contributes to a
# 30s rolling failure-rate calculation. Once we have at least
# _MIN_REQUESTS_FOR_OPEN samples in the window AND the 5xx fraction
# crosses _FAILURE_THRESHOLD_PCT, the breaker opens for
# _OPEN_STATE_SECONDS. While open, ``_circuit_open()`` short-circuits
# ``True`` without re-checking the rate — so even if a few requests
# slip through and succeed, we hold open for the full cool-down.
_FAILURE_WINDOW_SECONDS = 30.0
_FAILURE_THRESHOLD_PCT = 0.5
_OPEN_STATE_SECONDS = 60.0
_MIN_REQUESTS_FOR_OPEN = 10

# Bounded deques: 200 samples easily covers the 30s window even at
# 5+ rps and bounds memory. Older entries are auto-evicted by
# ``maxlen``; we additionally filter by timestamp inside
# ``_circuit_open`` so the math reflects the real window, not the
# deque size.
_recent_5xx: deque[float] = deque(maxlen=200)
_recent_total: deque[float] = deque(maxlen=200)
_circuit_open_until: float = 0.0


def _record_outcome(status_code: int) -> None:
    """Log a request outcome into the circuit breaker's sample windows."""
    # monotonic: these timestamps are only ever compared to each other within
    # this process (sliding window + cool-down), never persisted or shown, so a
    # wall-clock step (NTP) must not skew the window math.
    now = time.monotonic()
    _recent_total.append(now)
    if status_code >= 500:
        _recent_5xx.append(now)


def _circuit_open() -> bool:
    """Return True if the breaker is currently open (or should be opened now).

    Side effect: when the failure threshold is crossed for the first
    time we set ``_circuit_open_until`` so subsequent calls in the
    same cool-down period short-circuit without recomputing the rate.
    """
    global _circuit_open_until
    now = time.monotonic()  # same monotonic clock as _record_outcome
    if now < _circuit_open_until:
        return True
    cutoff = now - _FAILURE_WINDOW_SECONDS
    fives = sum(1 for t in _recent_5xx if t >= cutoff)
    total = sum(1 for t in _recent_total if t >= cutoff)
    if total >= _MIN_REQUESTS_FOR_OPEN and (fives / total) >= _FAILURE_THRESHOLD_PCT:
        _circuit_open_until = now + _OPEN_STATE_SECONDS
        logger.warning(
            "paperclip_proxy: circuit breaker OPEN (5xx=%d/%d in last %.0fs)",
            fives,
            total,
            _FAILURE_WINDOW_SECONDS,
        )
        return True
    return False


# Shared CSS for all proxy-served stub pages (bootstrap, provisioning,
# circuit-breaker). Centered card on a soft background — matches the
# Goosetown / Clerk-default look so the user doesn't get jarring
# unstyled HTML when the proxy intercepts a navigation.
_STUB_BASE_CSS = """
  *,*::before,*::after{box-sizing:border-box}
  html,body{height:100%;margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:#222;background:#f3f4f6}
  body{display:flex;align-items:center;justify-content:center;padding:24px}
  .card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.05),0 8px 24px rgba(0,0,0,.06);padding:32px 28px;max-width:420px;width:100%;text-align:center}
  .card h1{font-size:18px;line-height:1.3;margin:0 0 8px;font-weight:600}
  .card p{font-size:14px;line-height:1.5;color:#555;margin:0 0 20px}
  .card .spinner{width:18px;height:18px;border:2px solid #e5e7eb;border-top-color:#6b7280;border-radius:50%;display:inline-block;margin-right:8px;vertical-align:-3px;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .btn{appearance:none;border:0;background:#111827;color:#fff;font:inherit;font-size:14px;font-weight:500;padding:10px 18px;border-radius:8px;cursor:pointer}
  .btn:hover{background:#374151}
  .btn:disabled{background:#9ca3af;cursor:not-allowed}
  .err{color:#b91c1c;font-size:13px;margin-top:12px}
"""


def _provisioning_in_progress_response() -> Response:
    """Auto-refreshing "setting up your workspace" stub.

    Shown when ``proxy()`` finds the user authenticated but their
    paperclip-companies row is missing or status != "active". Typical
    case: handshake just spawned a background ``ensure_user_ready`` task
    that hasn't completed yet (5–10s). ``<meta http-equiv="refresh">``
    polls every 2s with no JS; once provisioning lands and the proxy
    returns the actual Paperclip page, the browser stops auto-refreshing.
    """
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Setting up Teams…</title>
<style>{_STUB_BASE_CSS}</style>
</head><body>
<div class="card">
  <h1><span class="spinner"></span>Setting up your workspace…</h1>
  <p>This usually takes 5–10 seconds. The page will reload automatically.</p>
</div>
</body></html>
"""
    return Response(content=body.encode("utf-8"), status_code=503, media_type="text/html; charset=utf-8")


def _circuit_breaker_response() -> Response:
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Teams temporarily unavailable</title>
<style>{_STUB_BASE_CSS}</style>
</head><body>
<div class="card">
  <h1>Teams temporarily unavailable</h1>
  <p>The workspace backend is having trouble. Try again in a minute.</p>
</div>
</body></html>
"""
    return Response(content=body.encode("utf-8"), status_code=503, media_type="text/html; charset=utf-8")


def _filter_request_headers(req: Request) -> dict[str, str]:
    """Strip hop-by-hop + auth headers from the inbound request.

    Authorization and Cookie are stripped so the browser's Clerk
    credentials don't leak to Paperclip — we inject our own
    server-issued Better Auth bearer instead. Host is stripped so
    httpx sets it based on the upstream base_url.
    """
    out: dict[str, str] = {}
    for name, value in req.headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in {"host", "authorization", "cookie", "content-length"}:
            continue
        # Never relay Paperclip's own trusted-header identity family. Paperclip
        # will honour x-paperclip-cloud-tenant-token + x-paperclip-cloud-user-id
        # / -user-email / -stack-role as a *self-asserted* identity (see
        # paperclip/server/src/middleware/auth.ts), and x-paperclip-run-id is
        # likewise client-forgeable. Isol8 supplies the real identity via the
        # server-minted Better Auth session cookie only, so a browser must never
        # be able to smuggle these upstream.
        if lower.startswith("x-paperclip-"):
            continue
        out[name] = value
    return out


# ---------------------------------------------------------------------------
# Adapter-surface request filter — see docs/audit-2026-05-02-paperclip-routes.md
#
# The shared Paperclip Fargate task holds every tenant's Aurora master creds and
# is IMDS-reachable. Its `process` / `http` / `*_local` adapters execute shell
# (or make arbitrary outbound connections) ON that host, so any tenant who can
# persist a non-gateway adapter — or reach an adapter-management / host-exec
# route — gets cross-tenant compromise. Only a *remote gateway* adapter is safe:
# the agent runs in the tenant's own OpenClaw container, never on this host.
#
# This proxy is the SOLE gate for the direct create+hire routes. The deployed
# fork's create/hire handlers call assertKnownAdapterType, which only checks the
# adapter is *registered* (`process` is) — NOT that it is allowlisted. (The
# invite/join paths DO enforce openclaw_gateway upstream; we still filter them
# defensively.) The audit doc's §3.1 SSRF (a browser-supplied adapterConfig.url
# reaching Paperclip's outbound gateway connect, regardless of adapterType) is
# now closed below: no legitimate browser write ever needs to carry
# adapterConfig at all, since the backend mints it server-side, pinned to our
# own facade, via a direct Paperclip admin-API call that never touches this
# proxy — so it is rejected outright rather than URL-validated. Still open:
# §4 adapter-plugin-store pre-disable, and §3.2-3.5's other routes (issue
# patches, project workspace commands) — out of scope for this fix.
# ---------------------------------------------------------------------------

# Only the deployed remote runtime adapter is exposed; local/process/HTTP
# execution and adapter management remain blocked below.
_ALLOWED_ADAPTER_TYPES = frozenset({"hermes_gateway"})

# Each entry is (methods, pattern); methods=None matches every method. `path`
# is matched WITHOUT its leading slash (e.g. "api/companies/x/agents").
_BLOCKED_ROUTES: tuple[tuple[frozenset[str] | None, re.Pattern[str]], ...] = (
    # Adapter registry: npm install / uninstall / reload / toggle — host mutation
    # + inventory disclosure of the dangerous adapters. Isol8 owns agent-create.
    (None, re.compile(r"^api/adapters(?:/|$)")),
    # Plugin system: install (npm on host), config eval, tool exec, info leak.
    (None, re.compile(r"^api/plugins(?:/|$)")),
    (None, re.compile(r"^_plugins/")),  # plugin-served static JS (XSS to loader)
    # Environments: SSH driver + probes are outbound-from-host SSRF. Reads pass.
    ({"POST", "PATCH", "DELETE"}, re.compile(r"^api/(?:companies/[^/]+/)?environments(?:/|$)")),
    # Unauthenticated tenant-controlled outbound HEAD. Trailing-slash-safe:
    # Express non-strict routing treats ``/foo`` and ``/foo/`` identically.
    (None, re.compile(r"^api/invites/[^/]+/test-resolution/?$")),
    # Paperclip's own auth surface — Isol8 injects a service-account cookie.
    (None, re.compile(r"^api/auth(?:/|$)")),
    # Instance-wide admin: settings, DB backups, cross-tenant scheduler list.
    (None, re.compile(r"^api/instance(?:/|$)")),
    (None, re.compile(r"^api/admin(?:/|$)")),
    # Company lifecycle is Isol8-owned; portability import carries agent bundles
    # (adapterType nested per-agent — blocked wholesale rather than walked).
    ({"POST"}, re.compile(r"^api/companies/?$")),
    ({"DELETE"}, re.compile(r"^api/companies/[^/]+/?$")),
    (None, re.compile(r"^api/companies/import")),
    (None, re.compile(r"^api/companies/[^/]+/imports(?:/|$)")),
    # Agent creation must pass through an Isol8-owned BFF so the owner-wide
    # budget cap and server-derived idempotency key are enforced. The raw
    # Paperclip route has neither control and would let any authenticated owner
    # bypass both simply by supplying the otherwise-safe gateway adapter.
    ({"POST"}, re.compile(r"^api/companies/[^/]+/(?:agents|agent-hires)/?$")),
    # claude_local-only subprocess login (trailing-slash-safe).
    (None, re.compile(r"^api/agents/[^/]+/claude-login/?$")),
    # Config-revision rollback restores a STORED adapterConfig snapshot with no
    # request-body adapterType. A revision predating this fence could carry a
    # non-gateway adapter (incl. one an attacker planted while the proxy was
    # open), and rollback would restore it unchanged. Block until the §3.6 data
    # migration has scrubbed historical revisions; unblock after.
    (None, re.compile(r"^api/agents/[^/]+/config-revisions/[^/]+/rollback/?$")),
    # Join-request approve creates an agent from the STORED join-request's
    # adapterType, falling back to "process" when it is null — a pre-fence row
    # is a host-exec path. Isol8 does not use the external-agent-join flow
    # (marketplace hire is the path), so block it; the §3.6 migration + the
    # invite-accept filter cover new/existing rows.
    (None, re.compile(r"^api/companies/[^/]+/join-requests/[^/]+/approve/?$")),
    # Adapter-inventory / docstring disclosure (mounted at root, not under /api).
    (None, re.compile(r"^llms/agent-configuration")),
    # Environment capability enumeration (info disclosure; creates/probes are
    # blocked above, reads pass, but capabilities leak the host's driver set).
    (None, re.compile(r"^api/(?:companies/[^/]+/)?environments/(?:[^/]+/)?capabilities")),
)

# `:type`-scoped adapter routes (models / detect-model / config-schema /
# ui-parser) — several shell out on the host. Allowed only for a gateway type.
_ADAPTER_TYPE_ROUTE = re.compile(r"^api/(?:companies/[^/]+/)?adapters/(?P<type>[^/]+)/")

# Routes that PERSIST a new agent's adapterType. A missing value defaults to
# "process" upstream, so these must carry an allowlisted type *explicitly*.
_ADAPTER_CREATE_ROUTES = (
    re.compile(r"^api/companies/[^/]+/agents/?$"),
    re.compile(r"^api/companies/[^/]+/agent-hires/?$"),
)

# Invite accept persists an *agent* join's adapterType; a missing one is stored
# as null and defaults to "process" when the join-request is later approved.
_INVITE_ACCEPT_ROUTES = (re.compile(r"^api/invites/[^/]+/accept/?$"),)

# Approval create/resubmit can mint an agent later when a hire approval is
# approved. Isol8-owned marketplace code files those approvals internally; the
# customer proxy must not expose a second, ungated create path.
_APPROVAL_CREATE_ROUTES = (re.compile(r"^api/companies/[^/]+/approvals/?$"),)
_APPROVAL_RESUBMIT_ROUTES = (re.compile(r"^api/approvals/[^/]+/resubmit/?$"),)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _contains_adapter_config(obj: object) -> bool:
    """True iff ``adapterConfig`` appears as a key ANYWHERE in ``obj``, at any
    nesting depth -- not just at the top level.

    Recursive for the same reason ``core.services.agent_redaction.strip_agent_secrets``
    is: a bare top-level check misses ``runtimeConfig.modelProfiles.<name>.adapterConfig``,
    a shape the audit doc's own route table (§2, ``POST .../agents`` /
    ``.../agent-hires`` / ``PATCH /api/agents/:id``) documents Paperclip actually
    accepts on exactly the routes this filter guards. A single-level ``"adapterConfig"
    in data`` check would let that nested form straight through.
    """
    if isinstance(obj, dict):
        return "adapterConfig" in obj or any(_contains_adapter_config(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_adapter_config(item) for item in obj)
    return False


def _assert_paperclip_request_allowed(
    method: str,
    path: str,
    body: bytes,
    *,
    content_type: str | None = None,
    content_encoding: str | None = None,
) -> None:
    """Reject a proxied request that could reach a non-gateway adapter.

    Raises ``HTTPException(403)`` on a disallowed route/body; returns ``None``
    otherwise. See the threat-model block above.
    """
    # Match routes case-INSENSITIVELY: the deployed Paperclip fork runs Express
    # with its default (case-insensitive) routing, so ``/API/Companies/.../Agents``
    # resolves to the same handler — a lowercase-only denylist would miss it. The
    # adapterType VALUE checks below stay case-sensitive (the upstream adapter
    # registry lookup is exact, so a case-variant type fails there anyway).
    method = method.upper()
    p = path.lower()

    # CANONICAL-PATH GUARD. The ASGI-decoded ``path`` must be exactly the path
    # httpx forwards to Paperclip. When httpx joins ``base_url`` + ``/{path}`` it
    # applies RFC-3986 resolution — percent-decoding, ``?``/``#`` splitting,
    # ``.``/``..`` dot-segment removal, and leading-``//`` authority handling —
    # so matching the *pre-join* path lets any of those transforms route the
    # request to a DIFFERENT upstream handler than we inspect (every such gap
    # defaulted the create route to the ``process`` adapter). Rather than chase
    # each transform, reject anything non-canonical up front: a leading slash, an
    # empty (``//``) / ``.`` / ``..`` segment, a delimiter, a stray percent, a
    # backslash, or a control byte. Legit Teams paths — a slash-free ``api/…``
    # with at most dotted file segments (``ui-parser.js``) — contain none.
    if (
        p.startswith("/")
        or "//" in p
        or any(c in p for c in ("?", "#", "%", "\\"))
        or any(seg in (".", "..") for seg in p.split("/"))
        or any(ord(ch) < 0x20 for ch in p)
    ):
        raise HTTPException(status_code=403, detail="Malformed path")

    for methods, pattern in _BLOCKED_ROUTES:
        if (methods is None or method in methods) and pattern.match(p):
            raise HTTPException(status_code=403, detail="Route not permitted")

    type_route = _ADAPTER_TYPE_ROUTE.match(p)
    if type_route and type_route.group("type") not in _ALLOWED_ADAPTER_TYPES:
        raise HTTPException(status_code=403, detail="Adapter not permitted")

    if method not in {"POST", "PUT", "PATCH"}:
        return

    # A compressed request body would hide adapterType from us while Paperclip's
    # express.json() (inflate=true by default) still decodes and processes it
    # upstream. No Isol8 client sends compressed request bodies — refuse them.
    if content_encoding and content_encoding.strip().lower() not in ("", "identity"):
        raise HTTPException(status_code=403, detail="Encoded request body not permitted")

    # Parse the body. Fail CLOSED on an unparseable ``application/json`` body:
    # our strict json.loads rejects e.g. invalid UTF-8 that Express's lenient
    # decoder would repair and process, so "can't inspect it" must not become
    # "allow it".
    content_is_json = (content_type or "").split(";", 1)[0].strip().lower() == "application/json"
    data: dict | None = None
    if body:
        try:
            parsed = json.loads(body)
            data = parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            if content_is_json:
                raise HTTPException(status_code=403, detail="Unparseable JSON body")
            data = None
    adapter_type = _str_or_none(data.get("adapterType")) if data else None

    # Direct create/hire: a missing adapterType defaults to "process" upstream.
    if adapter_type is None and any(r.match(p) for r in _ADAPTER_CREATE_ROUTES):
        raise HTTPException(status_code=403, detail="adapterType required")
    # Any write naming a non-allowlisted type at the top level.
    if adapter_type is not None and adapter_type not in _ALLOWED_ADAPTER_TYPES:
        raise HTTPException(status_code=403, detail="Adapter not permitted")

    # SSRF gap (audit doc §3.1): checking adapterType is "hermes_gateway" says
    # nothing about adapterConfig.apiBaseUrl -- the field Paperclip's gateway
    # adapter actually connects out to. A browser-supplied adapterConfig on an
    # otherwise-allowed agent is how an arbitrary outbound URL (or a header
    # blob riding along on it, §3.2) gets planted -- including on a PATCH that
    # edits ONLY adapterConfig and never re-sends adapterType, so this check
    # cannot be conditioned on adapter_type being present in THIS request. No
    # legitimate write through THIS proxy ever needs to carry adapterConfig at
    # all: the backend mints it server-side (build_hermes_gateway_adapter_config,
    # always pinned to our own /api/v1/internal/paperclip/hermes/{owner} facade)
    # via a direct Paperclip admin-API call, never through here. Reject the
    # whole key outright on every write rather than trying to validate a URL
    # shape or scope this to specific routes -- and check for it RECURSIVELY
    # (_contains_adapter_config), not just at the top level: the audit doc's
    # own route table for these exact endpoints documents Paperclip also
    # accepting it nested, at runtimeConfig.modelProfiles.<name>.adapterConfig.
    if data and _contains_adapter_config(data):
        raise HTTPException(status_code=403, detail="adapterConfig not permitted")

    if data is None:
        return

    # Invite accept: an agent join with no adapterType stores null -> "process".
    if data.get("requestType") == "agent" and any(r.match(p) for r in _INVITE_ACCEPT_ROUTES):
        if adapter_type not in _ALLOWED_ADAPTER_TYPES:
            raise HTTPException(status_code=403, detail="adapterType required")

    # Approval CREATE: approving a hire-shaped payload mints an agent without
    # Isol8's budget/idempotency boundary. Non-hire approvals still pass.
    if any(r.match(p) for r in _APPROVAL_CREATE_ROUTES):
        payload = data.get("payload")
        is_hire = data.get("type") == "hire_agent" or (
            isinstance(payload, dict) and ("adapterType" in payload or "adapterConfig" in payload)
        )
        if is_hire:
            raise HTTPException(status_code=403, detail="Route not permitted")

    # RESUBMIT omits the approval type from the request. A payload replacement
    # could turn a revision-requested hire into a fresh agent mint, so only a
    # state-only resubmit (which preserves the already-filtered payload) passes.
    if any(r.match(p) for r in _APPROVAL_RESUBMIT_ROUTES):
        payload = data.get("payload")
        if isinstance(payload, dict):
            raise HTTPException(status_code=403, detail="Route not permitted")


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Strip hop-by-hop headers from the upstream response.

    ``Content-Length`` is also dropped because the brand-rewrite step
    can change body length. ``httpx`` decodes compressed upstream bodies
    before exposing ``response.content``, so ``Content-Encoding`` must not
    describe those decoded bytes. FastAPI/Starlette will recompute the
    length when the Response is serialized. ``Set-Cookie`` is handled
    separately (we may rewrite the Domain= attribute) and re-attached by
    the caller, so we drop it here to avoid duplicates.
    """
    out: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in {"content-encoding", "content-length", "set-cookie"}:
            continue
        out[name] = value
    return out


def _brand_rewrite_html(body: bytes) -> bytes:
    """Replace Paperclip brand strings with Isol8 equivalents in HTML bytes.

    Only safe for ``text/html`` bodies — the patterns are anchored on
    HTML markup. Idempotent: the rewritten output never matches the
    original patterns again, so re-running the function is a no-op.
    """
    out = body
    for pattern, replacement in _BRAND_REWRITES:
        out = pattern.sub(replacement, out)
    return out


# This proxy fronts the NATIVE Paperclip agent routes, which echo the full
# agent record verbatim — including the openclaw_gateway adapterConfig (the
# operator device's signing private key + the container gateway token). Without
# stripping, that signing key reaches the browser/DOM. Reuse the SAME field set
# + stripper the /teams BFF uses (core.services.agent_redaction) so the two
# surfaces can never diverge on what's secret; the proxy only adds the route
# gate + the bytes<->JSON round-trip below.
_AGENT_ROUTE_RE = re.compile(r"(?:^|/)agents(?:/|$)")


# Agent writes the Paperclip UI makes through this proxy (create is blocked above; edits, pause/resume and
# delete pass). Mirrored into the super-agent registry like the admin client's own writes.
_AGENT_WRITE_RE = re.compile(r"^api/agents/(?P<id>[^/]+)(?:/(?:pause|resume|terminate))?/?$", re.IGNORECASE)


async def _mirror_agent_write(method: str, path: str, status_code: int, body: bytes) -> None:
    """Mirror a successful proxied agent write into ``agent_registry`` (never raises)."""
    if not 200 <= status_code < 300:
        return
    match = _AGENT_WRITE_RE.match(path)
    if match is None or method.upper() not in {"PATCH", "POST", "DELETE"}:
        return
    from core.services import agent_registry

    if method.upper() == "DELETE":
        await agent_registry.mirror_delete(match.group("id"))
        return
    try:
        agent = json.loads(body)
    except ValueError:
        agent = None
    if isinstance(agent, dict) and agent.get("id") and agent.get("name") and agent.get("companyId"):
        await agent_registry.mirror_from_paperclip(agent)
    else:  # not the full agent: --verify shows the drift, a backfill --apply heals it
        put_metric("agent_registry.mirror", dimensions={"result": "skipped"})


def _redact_agent_secrets_json(body: bytes) -> bytes:
    """Strip the agent secret fields from an agent-route JSON response body.

    Best-effort: a non-JSON / malformed body passes through unchanged so a
    parsing hiccup never breaks the page.
    """
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 — not JSON / malformed: pass through
        return body
    return json.dumps(strip_agent_secrets(data)).encode("utf-8")


def _rewrite_set_cookie_domain(set_cookie: str, target_domain: str | None = None) -> str:
    """Force the Domain attribute on a Set-Cookie header to ``target_domain``.

    ``target_domain`` defaults to a dot-prefixed ``settings.root_domain`` — the
    product apex derived from FRONTEND_URL (``.isol8.co``).

    Goal: the cookie should be sent by the browser on subsequent
    requests to any subdomain of that apex (chat., company., etc.)
    so Paperclip's in-page AJAX that addresses the company host
    directly carries the session without going through our
    bearer-injection path.

    Strategy:
      * If a ``Domain=`` attribute already exists, replace its value
        with ``target_domain``.
      * Otherwise append ``; Domain=<target_domain>``.

    We deliberately keep ``HttpOnly``, ``Secure``, ``SameSite``, and
    ``Path`` exactly as the upstream set them. The regex is
    case-insensitive on the attribute name (cookies are
    case-insensitive per RFC 6265 §5.2). Single-cookie input is
    expected — httpx stores multi-Set-Cookie responses as a list of
    headers we iterate separately at the call site.

    Edge case: a cookie with ``Domain=`` whose value contains a
    semicolon would confuse the simple regex, but real-world Domain
    values are restricted to host names per RFC 6265 §4.1.2.3, so
    the simple pattern is safe.
    """
    target_domain = target_domain or f".{settings.root_domain}"
    if re.search(r"(?i)domain=", set_cookie):
        return re.sub(
            r"(?i)(Domain=)[^;]+",
            r"\1" + target_domain,
            set_cookie,
            count=1,
        )
    return f"{set_cookie}; Domain={target_domain}"


# Clerk's default session-cookie name across their JS SDKs. Used by the
# WebSocket relay (``proxy_ws`` below). The HTTP path no longer reads it
# directly — Clerk dev instances scope ``__session`` to the application's
# exact host (e.g. ``dev.isol8.co``), so it never reaches
# ``company-dev.isol8.co``. The HTTP path uses our own session cookie
# (``isol8_paperclip``) issued by the bootstrap handshake.
_CLERK_SESSION_COOKIE = "__session"

# --- Proxy session cookie (issued by /__handshake__) ---
#
# Why we mint our own cookie instead of relying on Clerk's:
#
# Clerk's ``__session`` is set by Clerk's frontend API (or the application's
# clerkMiddleware) on the host where the user signed in — for dev that's
# ``dev.isol8.co`` only. Browsers don't send it on cross-subdomain navigation
# to ``company-dev.isol8.co``, so this proxy never sees it on top-level page
# loads. (Verified empirically: signed-in browser fetch to ``company-dev``
# with ``credentials: 'include'`` carried no ``__session`` cookie.)
#
# Goosetown solves the same problem implicitly — it serves its own SPA on
# ``dev.goosetown.isol8.co`` that loads the Clerk SDK, and the SDK recovers
# the user's session via third-party cookies on Clerk's frontend API host
# (``up-moth-55.clerk.accounts.dev``). Then it uses ``getToken()`` for API
# calls. Paperclip is a third-party app — we can't inject the Clerk SDK into
# its bundle. So we serve a tiny bootstrap HTML *in front* of Paperclip that
# does the same Clerk SDK dance, exchanges the JWT for a host-scoped cookie
# on ``company-dev.isol8.co``, and reloads. After that, every subsequent
# request (including Paperclip's own in-page navigation) carries the cookie
# and the proxy is happy.
#
# The cookie is an HS256 JWT signed with the existing
# ``PAPERCLIP_SERVICE_TOKEN_KEY`` (already loaded from Secrets Manager into
# the backend container — see ``core/services/service_token.py``). Different
# ``kind`` claim distinguishes proxy-session JWTs from agent service tokens
# so the two can never be cross-used.
_PAPERCLIP_SESSION_COOKIE = "isol8_paperclip"
_PAPERCLIP_SESSION_KIND = "paperclip_session"
_PAPERCLIP_SESSION_TTL_HOURS = 8


def _mint_paperclip_session(user_id: str, email: str | None, org_id: str | None = None) -> str:
    """HS256 JWT for the proxy session cookie. Same secret as service tokens
    but with a distinct ``kind`` claim so the verifier can refuse cross-use.

    ``org_id`` preserves the OWNER CONTEXT the handshake was validated
    under (from the Clerk JWT's org claims). Without it, every
    established-session request degrades to personal context, and the
    org-scoped readiness check can't tell a repaired row from a stale one
    — the user would be forwarded into the wrong workspace while the org
    join/repair is still in flight.
    """
    if not settings.PAPERCLIP_SERVICE_TOKEN_KEY:
        raise RuntimeError("PAPERCLIP_SERVICE_TOKEN_KEY not configured")
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": user_id,
        "kind": _PAPERCLIP_SESSION_KIND,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=_PAPERCLIP_SESSION_TTL_HOURS)).timestamp()),
    }
    if email:
        payload["email"] = email
    if org_id:
        payload["org_id"] = org_id
    return pyjwt.encode(payload, settings.PAPERCLIP_SERVICE_TOKEN_KEY, algorithm="HS256")


def _verify_paperclip_session(token: str) -> dict:
    """Verify a proxy session JWT. Returns claims on success; raises
    ``pyjwt.InvalidTokenError`` (or subclass) on any failure.
    """
    if not settings.PAPERCLIP_SERVICE_TOKEN_KEY:
        raise pyjwt.InvalidTokenError("PAPERCLIP_SERVICE_TOKEN_KEY not configured")
    claims = pyjwt.decode(
        token,
        settings.PAPERCLIP_SERVICE_TOKEN_KEY,
        algorithms=["HS256"],
    )
    if claims.get("kind") != _PAPERCLIP_SESSION_KIND:
        raise pyjwt.InvalidTokenError(f"Wrong kind: {claims.get('kind')!r}")
    if not claims.get("sub"):
        raise pyjwt.InvalidTokenError("Missing sub claim")
    return claims


def _wants_html(request: Request) -> bool:
    """Heuristic for "this is a top-level browser navigation."

    Used to decide whether a no-auth request should get the bootstrap
    HTML page (browser nav) or a JSON 401 (API client / fetch from JS).
    """
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept


async def _get_paperclip_user(request: Request) -> AuthContext:
    """Resolve the caller from the proxy session cookie OR a Bearer token.

    Order:
      1. ``?__t=`` handoff token, when present — an initial-handshake
         request. It must WIN over any existing session cookie: the token
         was just minted by the Teams entry point in the user's CURRENT
         Clerk context (including org claims), while a leftover cookie
         from a previous visit can encode a stale personal/other-org
         identity (the cookie carries no org claims at all). Honoring the
         stale cookie would run the whole handshake — the container
         guard, the org-scoped readiness check, and the autoprovision
         dispatch — under the WRONG owner context and 302 the user into
         the wrong workspace. An invalid/expired handoff token
         deliberately does NOT fall back to the cookie: for a browser
         nav, ``proxy()`` upgrades the 401 to a redirect that restarts
         the handshake with a fresh token.
      2. ``isol8_paperclip`` cookie — fastest, no network roundtrip, set by
         the handshake endpoint after a successful Clerk validation.
      3. ``Authorization: Bearer <clerk_jwt>`` — programmatic clients
         (Postman, curl, the handshake endpoint itself, post-deploy
         smoke tests). Validated via Clerk's JWKS.

    Browser navigation that bypasses the bootstrap (e.g. a stale tab whose
    cookie expired) gets a 401 here; ``proxy()`` upgrades that to a
    bootstrap HTML response when the request looks like a top-level nav.
    """
    handoff_token = request.query_params.get("__t") or None

    # 1. Proxy session cookie — skipped when an explicit handoff token is
    #    present (the token is the fresher context; see docstring).
    if not handoff_token:
        session_cookie = request.cookies.get(_PAPERCLIP_SESSION_COOKIE)
        if session_cookie:
            try:
                claims = _verify_paperclip_session(session_cookie)
                # org_id restores the owner context the handshake was
                # validated under — see _mint_paperclip_session.
                return AuthContext(
                    user_id=claims["sub"],
                    email=claims.get("email"),
                    org_id=claims.get("org_id"),
                )
            except pyjwt.InvalidTokenError as e:
                # Expired or tampered — fall through to Bearer/handoff;
                # caller gets a fresh cookie if either succeeds.
                logger.info("paperclip_proxy: session cookie rejected: %s", e)

    # 2. Bearer header (programmatic clients)
    # 3. ?__t= query-param (browser nav from dev.isol8.co Teams click — top-
    #    level navigation can't carry a Bearer header, so the frontend
    #    appends a fresh Clerk JWT here. proxy() strips it via 302 after
    #    minting the session cookie so it doesn't linger in the URL bar.)
    auth_header = request.headers.get("authorization", "")
    token: str | None = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip() or None
    if not token:
        token = handoff_token
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = await _decode_token(token)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        # JWKS fetch failed (Clerk down, network blip). Transient *service*
        # problem — surface 503 so the client can retry rather than treating
        # the user as logged out. Mirrors core.auth.get_current_user.
        logger.error("paperclip_proxy: JWKS fetch failed: %s", e)
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except Exception as e:  # noqa: BLE001
        logger.warning("paperclip_proxy: invalid Clerk token: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Clerk token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    # Extract org claims so the proxy can use resolve_owner_id() for the
    # container lookup (org users have org-owned OpenClaw containers, not
    # per-user). The paperclip-companies row itself stays per-user.
    org = _extract_org_claims(payload)
    return AuthContext(
        user_id=user_id,
        email=payload.get("email"),
        org_id=org["org_id"],
        org_role=org["org_role"],
        org_slug=org["org_slug"],
        org_permissions=org["org_permissions"],
    )


# --- Routes ---


@router.post("/__handshake__")
async def handshake(request: Request) -> Response:
    """Exchange a Clerk JWT for a host-scoped proxy session cookie.

    Called by the bootstrap HTML the moment Clerk SDK returns a session
    token. Validates the JWT via the standard Clerk JWKS path, mints an
    HS256 JWT signed with our service-token secret, and sets it as an
    HttpOnly Secure SameSite=Lax cookie scoped to the request's host. The
    cookie expires after ``_PAPERCLIP_SESSION_TTL_HOURS``; after that the
    bootstrap runs again (Clerk SDK can usually recover the session
    silently, so the user sees at most a brief loading flash).

    Auth path mirrors ``_get_paperclip_user``'s Bearer branch but does NOT
    accept the proxy session cookie — that would create a self-renewal
    loop where stale cookies could keep refreshing themselves. The point
    of the handshake is to get a *fresh* Clerk JWT.
    """
    auth_header = request.headers.get("authorization", "")
    token: str | None = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip() or None
    if not token:
        raise HTTPException(status_code=401, detail="Missing Clerk JWT")

    try:
        payload = await _decode_token(token)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.error("paperclip_proxy: handshake JWKS fetch failed: %s", e)
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except Exception as e:  # noqa: BLE001
        logger.warning("paperclip_proxy: handshake invalid Clerk token: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Clerk token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    email = payload.get("email")
    org = _extract_org_claims(payload)
    auth = AuthContext(
        user_id=user_id,
        email=email,
        org_id=org["org_id"],
        org_role=org["org_role"],
        org_slug=org["org_slug"],
        org_permissions=org["org_permissions"],
    )

    # Same org-scoped readiness dispatch as the ?__t= handoff. This route
    # is the OTHER cookie mint site (the bootstrap HTML's XHR), and the
    # cookie it mints carries the org context — so a user whose per-user
    # row is missing, non-active, or points at a different owner context
    # would land straight on the established-session repair stub, which
    # deliberately never re-spawns. Without dispatching here, an entry
    # through this path (direct nav, no ?__t=) would spin on the stub
    # with no repair in flight. Best-effort and idempotent, same as the
    # handoff; needs the email claim (Better Auth signup).
    #
    # Container guard, also mirroring the handoff: an owner with NO
    # container must not have a Paperclip company seeded from here — the
    # seeded agents' gateway adapter would point at a nonexistent
    # OpenClaw, and (worse) the now-active row would let the established-
    # session path forward into a dead Teams. This XHR can't redirect the
    # way the handoff does, so it simply declines to seed; the
    # unprovisioned-entry stub the user then sees is the pre-existing
    # behavior for this path, resolved once the workspace is provisioned
    # (its provision execution seeds Paperclip).
    if email:
        container = await container_repo.get_by_owner_id(resolve_owner_id(auth))
        if container is not None and not await _joined(resolve_owner_id(auth), user_id):
            from routers.container import _dispatch_watched

            _dispatch_watched(ensure_user_ready(auth))

    session_jwt = _mint_paperclip_session(
        user_id=user_id,
        email=email,
        org_id=auth.org_id,
    )
    response = Response(status_code=204)
    # No Domain= attribute → host-scoped to whatever public host this request
    # arrived at (e.g. company-dev.isol8.co). HttpOnly so JS can't exfiltrate;
    # Secure so it's HTTPS-only; SameSite=Lax so it rides on top-level
    # navigation (the Teams click) but not on cross-site sub-resource POSTs.
    response.set_cookie(
        key=_PAPERCLIP_SESSION_COOKIE,
        value=session_jwt,
        max_age=_PAPERCLIP_SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def _strip_handoff_param(request: Request) -> str:
    """Build the same URL the browser came from, minus ``?__t=``.

    Used for the post-handoff 302: we want the address bar to show the
    real URL the user was navigating to, with the one-shot Clerk JWT
    stripped. Preserves all other query params and the fragment.

    Strips the proxy mount prefix (``/__paperclip_proxy__``) from the
    path. The backend mounts this router at that prefix so the FastAPI
    request.url.path includes it, but the browser is at
    ``<public host>/foo`` (Vercel rewrote ``/foo`` to
    ``/__paperclip_proxy__/foo`` on the way in, or the legacy
    HostDispatcherMiddleware did the same). Either way, the 302 needs
    the bare ``/foo`` so the next request doesn't double-stack the
    prefix.
    """
    from urllib.parse import urlencode

    # urlencode handles reserved characters (&, =, %, spaces, …) correctly.
    # Naive f-string concat lets those through unescaped, which would
    # split or corrupt query state when the next request decoded the URL.
    items = [(k, v) for k, v in request.query_params.multi_items() if k != "__t"]
    qs = urlencode(items) if items else ""
    path = request.url.path
    prefix = "/__paperclip_proxy__"
    if path.startswith(prefix):
        path = path[len(prefix) :] or "/"
    if qs:
        path += "?" + qs
    if request.url.fragment:
        path += "#" + request.url.fragment
    return path


def _frontend_url() -> str:
    """Public URL of the dev/prod Isol8 frontend. Used for redirects when
    the user lands on the company host without prerequisite state.

    ``FRONTEND_URL`` carries its own non-empty default in ``core/config.py``
    and is CDK-set in every deployed env, so there is no second fallback to
    hard-code here.
    """
    return (settings.FRONTEND_URL or "").rstrip("/")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    """Forward ``request`` to the internal Paperclip server.

    Three auth + handshake states:

    1. **Initial handshake** (``?__t=<clerk_jwt>`` query param). The Teams
       button on dev.isol8.co appends a fresh Clerk JWT to the navigation
       URL because top-level browser nav can't carry an Authorization
       header. We validate it, check the user has an Isol8 container
       (302 to /chat if not), auto-provision their Paperclip company if
       missing, then mint a host-scoped session cookie and 302 to the
       same URL minus the token. The browser follows with the cookie and
       proxies through normally.
    2. **Established session** (``isol8_paperclip`` cookie). Skip the
       container/provision checks (they ran at handshake time); proceed
       to forward the request to Paperclip.
    3. **No auth, browser nav** — redirect to ``dev.isol8.co/chat`` with
       a hint so the user starts from the right place.
    """
    if _circuit_open():
        return _circuit_breaker_response()

    handoff_token = request.query_params.get("__t")
    is_initial_handshake = handoff_token is not None

    try:
        auth = await _get_paperclip_user(request)
    except HTTPException as e:
        # Browser nav with no auth → redirect to the Isol8 frontend.
        # company-dev.isol8.co isn't a sign-in destination on its own; the
        # canonical entry point is the Teams button on /chat.
        if e.status_code == 401 and _wants_html(request):
            from fastapi.responses import RedirectResponse

            return RedirectResponse(
                f"{_frontend_url()}/chat?from=teams",
                status_code=302,
            )
        raise

    if is_initial_handshake:
        # Container guard: if the user has no Isol8 container, Teams has
        # nothing to attach to (the seeded Main Agent's openclaw-gateway
        # adapter would point at a nonexistent target). Send them back to
        # /chat to provision one first. Use resolve_owner_id so org users
        # are matched against their org-owned container, not a per-user
        # one that may not exist.
        from fastapi.responses import RedirectResponse

        # Email guard: ensure_user_ready's paperclip seed step needs
        # auth.email to call Better Auth signup. If the Clerk JWT is
        # missing the email claim (older template, misconfigured org),
        # ensure_user_ready would best-effort no-op the seed (it logs
        # and returns rather than raising) — the row would stay None
        # forever and the user would loop in the setting-up stub.
        # Surface the error explicitly via /chat instead.
        if not auth.email:
            logger.warning(
                "paperclip_proxy: handoff blocked for user=%s — no email claim on JWT",
                auth.user_id,
            )
            return RedirectResponse(
                f"{_frontend_url()}/chat?from=teams&need=email",
                status_code=302,
            )

        owner_id = resolve_owner_id(auth)
        container = await container_repo.get_by_owner_id(owner_id)
        if container is None:
            logger.info(
                "paperclip_proxy: handoff blocked for user=%s — no container yet",
                auth.user_id,
            )
            return RedirectResponse(
                f"{_frontend_url()}/chat?from=teams&need=container",
                status_code=302,
            )

        # Auto-provision Paperclip company on first hit. Spawn as a
        # background task instead of awaiting — the user gets the 302
        # (and the JWT-stripped URL) within ms instead of waiting 5–10s
        # for Better Auth signup + company create + agent seed. The
        # subsequent request lands on a "setting up…" stub that
        # auto-refreshes until the row goes status="active". This keeps
        # the raw Clerk JWT in the address bar for only ~50ms instead of
        # the full provisioning duration. provision_org is idempotent so
        # a fast double-click is safe.
        #
        # Routed through the SAME canonical owner_id-keyed trigger
        # GET /container/status uses (ensure_user_ready) rather than a
        # second, proxy-local door that called provision_org directly
        # keyed on user_id — there is exactly one autoprovision trigger
        # family (INV-11). This also makes the dispatch org-aware for
        # free: an org member hitting Teams now correctly JOINS the
        # org's shared company instead of a personal-only provision_org
        # call attempting to CREATE one keyed on their own user_id. The
        # container already exists (checked above); ensure_user_ready never
        # dispatches a provision, so the running box is never force-redeployed
        # from here.
        # "Already provisioned" is org-scoped: this human JOINED this
        # workspace's active company. ensure_user_ready is idempotent.
        if not await _joined(owner_id, auth.user_id):
            # Watched dispatch (same wrapper as /container/status): a bare
            # create_task swallows exceptions raised before the coroutine's
            # own logging (gate/billing/DDB failures) as "Task exception
            # was never retrieved" — the watcher logs them and emits the
            # provision-error metric.
            from routers.container import _dispatch_watched

            _dispatch_watched(ensure_user_ready(auth))

        # Mint cookie + 302 to clean URL. The cookie carries the org
        # context the handshake was validated under, so the established-
        # session path below can keep applying the org-scoped readiness
        # check instead of degrading to personal context.
        session_jwt = _mint_paperclip_session(user_id=auth.user_id, email=auth.email, org_id=auth.org_id)
        response = RedirectResponse(_strip_handoff_param(request), status_code=302)
        response.set_cookie(
            key=_PAPERCLIP_SESSION_COOKIE,
            value=session_jwt,
            max_age=_PAPERCLIP_SESSION_TTL_HOURS * 3600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    # ---- Established-session path: forward request to Paperclip ----
    if auth.org_id is None:
        # A session with no org claim (a legacy cookie minted before org_id
        # rode the session) cannot name a workspace. Bounce out to /chat: a
        # fresh Teams entry re-handshakes with real Clerk claims.
        from fastapi.responses import RedirectResponse

        logger.info("paperclip_proxy: org-less session for user=%s; redirecting for re-handshake", auth.user_id)
        return RedirectResponse(
            f"{_frontend_url()}/chat?from=teams",
            status_code=302,
        )
    repo = PaperclipRepo(table_name="paperclip-companies")
    company = await repo.get_workspace(resolve_owner_id(auth))
    identity = await repo.get_identity(auth.user_id)
    # "seeding" = provision_org's pre-side-effect seed-claim row — an
    # in-flight provision exactly like "provisioning" for this check.
    if (
        company is None
        or company.status in ("seeding", "provisioning")
        or (company.status == "active" and auth.user_id not in company.member_user_ids)
        or identity is None
        or not identity.paperclip_user_id
    ):
        # Either the handshake just spawned ensure_user_ready and the rows
        # haven't landed yet (typical first visit), or it is in flight, or
        # this human has not joined THIS workspace's company yet (the
        # handshake dispatched the join). All converge via the auto-refresh
        # stub. Do NOT re-spawn from this path — the stub reloads every 2s
        # and a re-spawn each cycle would queue duplicate concurrent runs.
        return _provisioning_in_progress_response()
    if company.status != "active":
        # status="failed" or status="disabled". Both require user to
        # re-enter via /chat: failed needs an explicit retry trigger
        # (re-handshake spawns a fresh provision attempt with no
        # concurrent races), disabled requires admin/billing to clear.
        # Either way auto-refresh wouldn't help, so redirect out.
        from fastapi.responses import RedirectResponse

        logger.info(
            "paperclip_proxy: cookie path saw status=%s for user=%s; redirecting to /chat",
            company.status,
            auth.user_id,
        )
        return RedirectResponse(
            f"{_frontend_url()}/chat?from=teams&need=reprovision",
            status_code=302,
        )

    # ---- Resolve the upstream session cookie (reuse > mint) ----
    # The browser already holds the Paperclip ``.session_token`` cookie
    # (rewritten to ``.isol8.co`` on a prior sign-in) plus its
    # ``isol8_pc_bind`` companion. If that cookie is present AND its
    # binding cookie's ``sub`` matches the authenticated proxy user, we
    # forward it as-is and SKIP the sign-in — that eliminates the
    # per-request re-auth storm. Otherwise (cold start / cross-user /
    # unbound) we sign in fresh and seed both cookies on the response.
    # The binding check is the confused-deputy guard: ``is_trusted`` returns
    # the cookie only when its binding is for THIS user AND for THIS exact
    # session token, so a session cookie not provably bound to the caller is
    # never replayed on their behalf.
    reused_cookie = is_trusted(request.cookies, auth.user_id)

    # Per-request httpx.AsyncClient: simple lifecycle, no shared-state
    # gotchas. Cloud Map A-record TTL is 10s so DNS is re-resolved
    # often enough; v2 should hoist this into a long-lived shared
    # client to amortize TLS + connection-pool setup.
    async with httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=30.0,
    ) as client:
        admin = PaperclipAdminClient(http_client=client)

        async def _sign_in() -> str:
            # Cold-mint a fresh Better Auth session cookie for this user.
            # Email comes from the identity row (``paperclip_email``) — the
            # address Better Auth actually holds, written once at provision
            # time and never updated, so it survives a Clerk primary-email
            # change that would otherwise 401 every sign-in. The Clerk JWT's
            # ``email`` claim is the fallback for rows written before that
            # field existed; the Isol8 ``users`` row has no email column. The
            # password is the per-user random one we encrypted at
            # provisioning time.
            email = identity.paperclip_email or (auth.email or "").strip()
            if not email:
                logger.warning(
                    "paperclip_proxy: user %s has no email claim on JWT; cannot sign in to Paperclip",
                    auth.user_id,
                )
                raise HTTPException(status_code=400, detail="Email claim missing from auth token")
            try:
                password = decrypt(identity.paperclip_password_encrypted)
            except ValueError as e:
                # Encryption key rotated, or row was written under a
                # different key. The user needs reprovisioning — surface a
                # clear 500 rather than a confusing upstream auth failure.
                logger.error(
                    "paperclip_proxy: failed to decrypt password for user %s: %s",
                    auth.user_id,
                    e,
                )
                raise HTTPException(status_code=500, detail="Could not decrypt session credentials")
            try:
                signin = await admin.sign_in_user(email=email, password=password)
            except PaperclipApiError as e:
                logger.exception(
                    "paperclip_proxy: sign_in failed for user=%s status=%s body=%s",
                    auth.user_id,
                    e.status_code,
                    e.body,
                )
                _record_outcome(e.status_code)
                raise HTTPException(status_code=502, detail="Could not authenticate to Paperclip")
            fresh = signin.get("_session_cookie") or ""
            if not fresh:
                logger.error(
                    "paperclip_proxy: Better Auth response had no Set-Cookie for user %s",
                    auth.user_id,
                )
                raise HTTPException(status_code=502, detail="Paperclip auth response malformed")
            return fresh

        def _upstream_headers(session_cookie: str) -> dict[str, str]:
            # Build forwarding headers. We add X-Forwarded-* so Paperclip's
            # access logs reflect the real client identity (not the ALB IP).
            # Source the public hostname Paperclip should use when emitting
            # absolute callback/redirect URLs. Order matters:
            #   1. X-Forwarded-Host — set by Vercel for hosts on its rewrite
            #      path (dev.company.isol8.co, company.isol8.co). Holds the
            #      real public host the user typed; the only header that
            #      survives the Vercel→API-Gateway hop with the company host
            #      intact.
            #   2. X-Isol8-Public-Host — set by API Gateway parameter mapping
            #      to ``$context.domainName``. For Vercel-routed traffic this
            #      is the API Gateway hostname (``api-{env}.isol8.co``), NOT
            #      the company host — useless for upstream URL shaping. Kept
            #      as a fallback for legacy direct-to-API-Gateway paths.
            #   3. Host — last-ditch fallback (post-ALB this is the ALB DNS).
            forwarded_host_raw = (
                request.headers.get("x-forwarded-host")
                or request.headers.get("x-isol8-public-host")
                or request.headers.get("host", "")
            )
            # Multi-hop proxies append to X-Forwarded-Host comma-separated
            # ("hostA, hostB"); the LEFTMOST value is the original public
            # host. Without splitting, Paperclip would see an invalid host
            # token and emit malformed absolute URLs.
            forwarded_host = forwarded_host_raw.split(",", 1)[0].strip()
            client_host = request.client.host if request.client else ""
            # Auth via Cookie, NOT Authorization: Bearer — Paperclip's
            # Better Auth config does not enable the bearer() plugin, so a
            # bearer header is silently ignored and the upstream sees the
            # request as anonymous. ``_filter_request_headers`` already
            # stripped the inbound Cookie, so the only cookie reaching
            # upstream is the one we set here (reused or freshly minted).
            headers: dict[str, str] = {
                **_filter_request_headers(request),
                "Cookie": session_cookie,
                "X-Forwarded-Host": forwarded_host,
                "X-Forwarded-Proto": "https",
            }
            # Origin fallback. Same-origin browser GET/HEAD requests often
            # omit Origin entirely. Without it, Better Auth's internal
            # getSession derives a non-https request URL and looks for the
            # bare cookie name (no ``__Secure-`` prefix), so the session
            # is invisible upstream and privileged routes run as anonymous
            # → 401/403. Stamp PAPERCLIP_PUBLIC_URL when the inbound
            # request didn't carry an Origin.
            if "origin" not in {k.lower() for k in headers} and settings.PAPERCLIP_PUBLIC_URL:
                headers["Origin"] = settings.PAPERCLIP_PUBLIC_URL
            if client_host:
                headers["X-Forwarded-For"] = client_host
            return headers

        # httpx accepts a leading ``/`` in the path even when base_url
        # already has a trailing ``/`` — it normalizes correctly.
        upstream_url = f"/{path}"
        body_bytes = await request.body()

        # Adapter-surface gate: reject anything that could reach a non-gateway
        # adapter (host shell / SSRF) or an adapter-management route before it
        # leaves for the shared Paperclip task. Raises HTTPException(403).
        _assert_paperclip_request_allowed(
            request.method,
            path,
            body_bytes,
            content_type=request.headers.get("content-type"),
            content_encoding=request.headers.get("content-encoding"),
        )

        async def _forward(session_cookie: str) -> httpx.Response:
            return await client.request(
                method=request.method,
                url=upstream_url,
                params=request.query_params,
                content=body_bytes,
                headers=_upstream_headers(session_cookie),
            )

        # ``seed_cookie`` is set to the freshly-minted cookie whenever we
        # sign in (cold start or 401 fallback); the response builder below
        # then seeds both the rewritten Paperclip Set-Cookie AND the
        # ``isol8_pc_bind`` companion. A reused cookie is already in the
        # browser, so we leave ``seed_cookie`` None and reseed nothing.
        seed_cookie: str | None = None
        try:
            if reused_cookie is not None:
                upstream = await _forward(reused_cookie)
                if upstream.status_code == 401:
                    # The browser-held cookie expired/rotated upstream.
                    # Sign in once, retry, and reseed the fresh cookie.
                    logger.info(
                        "paperclip_proxy: reused cookie 401 for user=%s; re-minting",
                        auth.user_id,
                    )
                    seed_cookie = await _sign_in()
                    upstream = await _forward(seed_cookie)
            else:
                seed_cookie = await _sign_in()
                upstream = await _forward(seed_cookie)
        except httpx.HTTPError as e:
            logger.exception(
                "paperclip_proxy: upstream request failed user=%s method=%s path=%s: %s",
                auth.user_id,
                request.method,
                path,
                e,
            )
            _record_outcome(502)
            # Best-effort signal for the W63 paperclip-down alarm. ``kind`` is a
            # bounded dim ("unreachable"); a metrics failure must never mask the
            # original upstream error or change the 502 we return.
            try:
                put_metric("paperclip.upstream.error", dimensions={"kind": "unreachable"})
            except Exception:  # noqa: BLE001 — telemetry must not break the proxy
                logger.debug("paperclip.upstream.error metric emission failed", exc_info=True)
            return Response(
                content=b"Bad gateway",
                status_code=502,
                media_type="text/plain",
            )

    _record_outcome(upstream.status_code)
    await _mirror_agent_write(request.method, path, upstream.status_code, upstream.content)

    # ---- Build the response back to the browser ----
    response_body = upstream.content
    content_type = upstream.headers.get("content-type", "")
    if "text/html" in content_type and response_body:
        try:
            response_body = _brand_rewrite_html(response_body)
        except Exception as e:  # noqa: BLE001 - never break the page
            logger.warning(
                "paperclip_proxy: brand-rewrite raised (passing through): %s",
                e,
            )
    elif response_body and "application/json" in content_type and _AGENT_ROUTE_RE.search(path):
        # Agent-route JSON echoes the full record incl. the openclaw_gateway
        # adapterConfig (device signing key + gateway token). Strip it before it
        # reaches the browser — Content-Length is dropped by
        # _filter_response_headers, so a length change here is safe.
        try:
            response_body = _redact_agent_secrets_json(response_body)
        except Exception as e:  # noqa: BLE001 - never break the page
            logger.warning(
                "paperclip_proxy: agent-secret redaction raised (passing through): %s",
                e,
            )

    response_headers = _filter_response_headers(upstream.headers)

    # Re-attach Set-Cookie (potentially many) with rewritten Domain.
    # httpx preserves the original ordering of repeated headers via
    # ``get_list``; we round-trip each through the rewriter and
    # carry them across as a list. FastAPI's ``Response`` accepts a
    # mapping so we serialize multiple Set-Cookie via raw mutation
    # of ``response.headers`` after construction.
    set_cookies = upstream.headers.get_list("set-cookie") if hasattr(upstream.headers, "get_list") else []
    if not set_cookies:
        # httpx.Headers may not expose get_list in some versions; fall
        # back to the single-value accessor so we degrade gracefully.
        single = upstream.headers.get("set-cookie")
        if single:
            set_cookies = [single]

    rewritten_cookies = [_rewrite_set_cookie_domain(sc) for sc in set_cookies]

    response = Response(
        content=response_body,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type or None,
    )
    for cookie in rewritten_cookies:
        # raw_headers append preserves multi-Set-Cookie semantics —
        # ``response.headers["set-cookie"] = ...`` would only retain
        # the last value.
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))

    # If we cold-minted (or re-minted on 401) a session cookie this
    # request, seed the browser with it (domain-broadened to .isol8.co)
    # plus the ``isol8_pc_bind`` companion, so the NEXT request reuses it
    # and we skip the sign-in. The minted cookie came from a separate
    # ``sign_in_user`` call, not the forwarded request, so the upstream
    # rotation Set-Cookies above don't necessarily carry it — we must set
    # it explicitly here. On the reused-cookie path ``seed_cookie`` is
    # None and the cookie already lives in the browser, so we touch
    # nothing (rotation, if any, was relayed above).
    if seed_cookie is not None:
        set_session_on_response(response, seed_cookie, auth.user_id)

    return response


# ---------------------------------------------------------------------------
# WebSocket relay — paired upstream WS for Paperclip live-events
# ---------------------------------------------------------------------------
#
# Why a separate route from the HTTP one?
#
# 1. **Auth source.** Browser-issued WebSocket upgrades cannot carry an
#    ``Authorization: Bearer`` header (the WebSocket JS API has no hook
#    for it), so the only credential we get is the Clerk session cookie
#    that already rides along on cross-origin requests to ``*.isol8.co``.
#    That means we can't use ``Depends(get_current_user)`` here — we
#    have to read ``websocket.cookies`` and validate the JWT manually
#    via ``_decode_token``.
# 2. **Different transport.** httpx can't proxy a WebSocket upgrade,
#    and FastAPI dispatches WS upgrades to ``@router.websocket(...)``
#    routes, not ``@router.api_route(...)`` ones. Both can register the
#    same ``{path:path}`` because Starlette's matcher checks the
#    ``websocket.scope["type"]`` and routes accordingly.
#
# Auth flow (mirrors the HTTP path's sign-in):
#   1. Read ``__session`` cookie (Clerk's default JWT cookie name).
#   2. Decode + validate with ``_decode_token`` → extract ``sub`` + ``email``.
#   3. Look up the user's Paperclip identity row, decrypt the password.
#   4. Sign in to Paperclip (Better Auth) → session token.
#   5. Open paired upstream WS with ``Authorization: Bearer <token>``.
#   6. asyncio.gather two relay coroutines (browser→upstream and
#      upstream→browser) until either side closes.
#
# Close codes follow RFC 6455 + the "private application" range
# (4000-4999): 4401 unauthenticated, 4400 missing email claim, 4502
# Paperclip auth failed, 4503 not provisioned. They're informational
# only — the browser usually just sees an immediate close — but they
# make CloudWatch traces readable.

# Max WebSocket frame size we accept from either side. 10 MB matches
# Paperclip's own server limit; bigger frames are almost certainly a
# bug or abuse, so we let the websockets library reject them upstream
# of the relay loop.
_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024


@router.websocket("/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str) -> None:
    """Bidirectional WebSocket relay for Paperclip live-events.

    Auth via Clerk session cookie on the upgrade frame (browsers cannot
    attach an Authorization header to a JS-initiated WebSocket open, so
    cookie auth is the only viable channel). Same DB lookup + Better
    Auth sign-in dance as the HTTP path; differs in that the upstream
    leg is also a WebSocket and we relay frames bidirectionally instead
    of round-tripping a single request/response.
    """
    # Defence-in-depth: the adapter-management / host-exec routes are HTTP, not
    # WS, but never let a WS upgrade reach one either. GET semantics — the body
    # and create-route rules don't apply to an upgrade, only the route denylist.
    try:
        _assert_paperclip_request_allowed("GET", path, b"")
    except HTTPException:
        await websocket.close(code=4403, reason="Route not permitted")
        return

    # ---- Step 1: Extract + validate session ----
    # Prefer the proxy session cookie (``isol8_paperclip``) over Clerk's
    # ``__session``. The HTTP bootstrap establishes the proxy cookie before
    # Paperclip's UI ever loads, so by the time live-events WS connects the
    # cookie is always there. Clerk ``__session`` falls through as a
    # belt-and-braces fallback for the day Clerk's parent-domain cookie
    # config changes (prod with ``clerk.isol8.co``) — the WS handler stays
    # functional in either world without code changes.
    user_id: str | None = None
    email: str = ""
    proxy_session = websocket.cookies.get(_PAPERCLIP_SESSION_COOKIE)
    if proxy_session:
        try:
            claims = _verify_paperclip_session(proxy_session)
            user_id = claims["sub"]
            email = (claims.get("email") or "").strip()
        except pyjwt.InvalidTokenError as e:
            logger.info("paperclip_proxy_ws: session cookie rejected: %s", e)
            # Fall through to Clerk cookie path.

    if not user_id:
        clerk_token = websocket.cookies.get(_CLERK_SESSION_COOKIE)
        if not clerk_token:
            await websocket.close(code=4401, reason="Missing session credential")
            return
        try:
            payload = await _decode_token(clerk_token)
        except Exception as e:  # noqa: BLE001 - opaque to caller; we close.
            logger.warning("paperclip_proxy_ws: invalid Clerk token: %s", e)
            await websocket.close(code=4401, reason="Invalid Clerk token")
            return
        user_id = payload.get("sub")
        email = (payload.get("email") or "").strip()

    if not user_id:
        await websocket.close(code=4401, reason="Token missing subject")
        return
    if not email:
        # Same constraint as the HTTP path — Better Auth needs email +
        # password to sign the user in, and the Isol8 ``users`` row has
        # no email column to fall back on.
        logger.warning(
            "paperclip_proxy_ws: user %s has no email claim on JWT; cannot sign in",
            user_id,
        )
        await websocket.close(code=4400, reason="Email claim missing")
        return

    # ---- Step 2: Look up the human's Paperclip identity + decrypt password ----
    # Identity only: Paperclip's own membership scopes what this session sees.
    repo = PaperclipRepo(table_name="paperclip-companies")
    company = await repo.get_identity(user_id)
    if company is None or not company.paperclip_user_id:
        await websocket.close(code=4503, reason="Paperclip not provisioned")
        return

    # The email extracted above is the user's LIVE address; Better Auth's is
    # frozen at sign-up. Prefer what the identity row records.
    email = company.paperclip_email or email

    try:
        password = decrypt(company.paperclip_password_encrypted)
    except ValueError as e:
        logger.error(
            "paperclip_proxy_ws: failed to decrypt password for user %s: %s",
            user_id,
            e,
        )
        await websocket.close(code=4500, reason="Credential decrypt failed")
        return

    # ---- Step 3: Sign in to Paperclip via Better Auth ----
    async with httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    ) as client:
        admin = PaperclipAdminClient(http_client=client)
        try:
            signin = await admin.sign_in_user(email=email, password=password)
        except PaperclipApiError as e:
            logger.exception(
                "paperclip_proxy_ws: sign_in failed for user=%s status=%s body=%s",
                user_id,
                e.status_code,
                e.body,
            )
            await websocket.close(code=4502, reason="Paperclip auth failed")
            return

        session_cookie = signin.get("_session_cookie") or ""
        if not session_cookie:
            logger.error(
                "paperclip_proxy_ws: Better Auth response had no Set-Cookie for user %s",
                user_id,
            )
            await websocket.close(code=4502, reason="Paperclip auth response malformed")
            return

    # ---- Step 4: Open paired upstream WS ----
    # Translate the http(s)://host base URL into ws(s)://host so we
    # connect on the WebSocket transport. Paperclip's live-events
    # endpoint lives under the same host as its HTTP API.
    upstream_base = settings.PAPERCLIP_INTERNAL_URL.replace("https://", "wss://").replace("http://", "ws://")
    # Ensure exactly one slash between base and path. Paperclip is
    # forgiving about double slashes, but a clean URL keeps logs sane.
    upstream_url = f"{upstream_base.rstrip('/')}/{path.lstrip('/')}"

    await websocket.accept()

    # Origin must match PAPERCLIP_PUBLIC_URL so Better Auth's internal
    # getSession derives the right request URL and looks for the
    # __Secure- cookie prefix (otherwise it falls back to the bare
    # name and the session is not recognized — see admin_client._headers).
    ws_headers = {"Cookie": session_cookie}
    if settings.PAPERCLIP_PUBLIC_URL:
        ws_headers["Origin"] = settings.PAPERCLIP_PUBLIC_URL

    try:
        async with ws_connect(
            upstream_url,
            additional_headers=ws_headers,
            max_size=_WS_MAX_FRAME_SIZE,
            open_timeout=15,
            close_timeout=5,
        ) as upstream:
            # ---- Step 5: Bidirectional relay ----
            #
            # We use ``asyncio.gather(..., return_exceptions=True)``
            # rather than ``asyncio.wait(FIRST_COMPLETED)`` so that an
            # exception in one leg doesn't tear down the other leg
            # mid-frame — gather collects results, the outer ``finally``
            # closes both sides cleanly.

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        msg_type = msg.get("type")
                        if msg_type == "websocket.disconnect":
                            return
                        # FastAPI's ``receive()`` returns the raw ASGI
                        # event dict; either ``text`` or ``bytes`` is
                        # populated for a ``websocket.receive`` event.
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    return
                except ConnectionClosed:
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning(
                        "paperclip_proxy_ws: client→upstream relay error user=%s: %s",
                        user_id,
                        e,
                    )

            async def upstream_to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except ConnectionClosed:
                    return
                except WebSocketDisconnect:
                    return
                except Exception as e:  # noqa: BLE001 - log + bail
                    logger.warning(
                        "paperclip_proxy_ws: upstream→client relay error user=%s: %s",
                        user_id,
                        e,
                    )

            await asyncio.gather(
                client_to_upstream(),
                upstream_to_client(),
                return_exceptions=True,
            )
    except Exception as e:  # noqa: BLE001 - top-level guard
        logger.exception(
            "paperclip_proxy_ws: connection failed user=%s path=%s: %s",
            user_id,
            path,
            e,
        )
    finally:
        # ``websocket.close()`` is idempotent in Starlette — safe to call
        # even if the relay loop already saw a disconnect.
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


async def _joined(owner_id: str, user_id: str) -> bool:
    """Whether ``user_id`` has joined workspace ``owner_id``'s ACTIVE company."""
    ws = await PaperclipRepo(table_name="paperclip-companies").get_workspace(owner_id)
    return ws is not None and ws.status == "active" and user_id in ws.member_user_ids
