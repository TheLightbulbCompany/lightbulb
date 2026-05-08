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
2. Look up the user's ``PaperclipCompany`` row, decrypt the stored
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
import base64
import binascii
import html
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
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError

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
        b"<title>Isol8 Teams</title>",
    ),
    (
        re.compile(
            rb'(<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\'])Paperclip',
            re.IGNORECASE,
        ),
        rb"\1Isol8",
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
    now = time.time()
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
    now = time.time()
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
    case: handshake just spawned a background ``_autoprovision`` task
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


def _provisioning_response() -> Response:
    """Returned when the user has no Paperclip company yet (or it's not active).

    The page POSTs to ``/__provision__`` to trigger personal-company
    provisioning (mirrors what the Clerk org.created webhook would do
    for org users), then auto-polls until the row goes ``status=active``
    and reloads. Manual refresh also works — backend will return either
    the same stub or the actual Paperclip UI depending on row state.
    """
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setting up Teams…</title>
<style>{_STUB_BASE_CSS}</style>
</head><body>
<div class="card">
  <h1 id="title">Set up your team workspace</h1>
  <p id="msg">This takes a few seconds. Click below to provision your workspace.</p>
  <button id="go" class="btn">Set up workspace</button>
  <div id="err" class="err" hidden></div>
</div>
<script>
(() => {{
  const $ = (id) => document.getElementById(id);
  const setBusy = (msg) => {{
    $('title').innerHTML = '<span class="spinner"></span>Provisioning…';
    $('msg').textContent = msg || 'This usually takes 5–10 seconds.';
    $('go').hidden = true;
    $('err').hidden = true;
  }};
  const setError = (msg) => {{
    $('err').textContent = msg;
    $('err').hidden = false;
    $('go').disabled = false;
    $('go').textContent = 'Try again';
    $('title').textContent = 'Set up your team workspace';
    $('msg').textContent = '';
  }};

  async function poll(deadlineMs) {{
    // After provisioning succeeds, the next request to / should proxy
    // straight through. We poll by issuing a HEAD with no follow so we
    // can read the status code without re-rendering this page.
    const start = Date.now();
    while (Date.now() - start < deadlineMs) {{
      try {{
        const r = await fetch('/', {{ method: 'HEAD', credentials: 'include' }});
        if (r.status !== 503) {{
          location.replace('/');
          return;
        }}
      }} catch (e) {{ /* keep polling */ }}
      await new Promise(r => setTimeout(r, 2000));
    }}
    setError('Provisioning taking longer than expected. Refresh to retry.');
  }}

  $('go').addEventListener('click', async () => {{
    $('go').disabled = true;
    setBusy('');
    try {{
      const r = await fetch('/__provision__', {{ method: 'POST', credentials: 'include' }});
      if (r.ok) {{
        await poll(60000);
      }} else {{
        const text = await r.text().catch(() => '');
        setError('Provisioning failed (' + r.status + ')' + (text ? ': ' + text.slice(0, 200) : ''));
      }}
    }} catch (e) {{
      setError('Network error: ' + (e && e.message || e));
    }}
  }});
}})();
</script>
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
        out[name] = value
    return out


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Strip hop-by-hop headers from the upstream response.

    ``Content-Length`` is also dropped because the brand-rewrite step
    can change body length. FastAPI/Starlette will recompute it from
    the new body when the Response is serialized. ``Set-Cookie`` is
    handled separately (we may rewrite the Domain= attribute) and
    re-attached by the caller, so we drop it here to avoid duplicates.
    """
    out: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in {"content-length", "set-cookie"}:
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


def _rewrite_set_cookie_domain(set_cookie: str, target_domain: str = ".isol8.co") -> str:
    """Force the Domain attribute on a Set-Cookie header to ``target_domain``.

    Goal: the cookie should be sent by the browser on subsequent
    requests to any ``*.isol8.co`` subdomain (chat.isol8.co,
    company.isol8.co, etc.) so Paperclip's in-page AJAX that
    addresses ``company.isol8.co`` directly carries the session
    without going through our bearer-injection path.

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


def _decode_clerk_publishable_key(pk: str) -> str | None:
    """Extract the Clerk Frontend API hostname from a publishable key.

    Clerk publishable keys encode the frontend API URL: the format is
    ``pk_{test|live}_<base64(<frontend_api_host>$)>``. Returns the bare
    hostname (no scheme) on success, ``None`` on parse failure. Used by
    the bootstrap HTML to load the right Clerk SDK build per environment.
    """
    if not pk:
        return None
    parts = pk.split("_", 2)
    if len(parts) != 3 or parts[0] != "pk":
        return None
    encoded = parts[2]
    # base64.b64decode requires correct padding; pad up to a multiple of 4.
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded.rstrip("$").rstrip("/") or None


def _mint_paperclip_session(user_id: str, email: str | None) -> str:
    """HS256 JWT for the proxy session cookie. Same secret as service tokens
    but with a distinct ``kind`` claim so the verifier can refuse cross-use.
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


def _bootstrap_html() -> bytes:
    """Render the Clerk sign-in bootstrap page.

    Served when a browser navigates to a Paperclip URL with no auth.
    Mirrors Goosetown's pattern: a tiny Clerk-aware page that mounts
    Clerk's ``<SignIn>`` component inline. After the user signs in (or
    one-clicks "Continue as ..." if Clerk recognizes the browser),
    Clerk establishes a session on company-dev.isol8.co, the page POSTs
    the JWT to ``/__handshake__`` to mint a host-scoped proxy cookie,
    then reloads — the next request carries the cookie and proxies
    through to Paperclip normally.

    Why we mount the sign-in inline instead of redirecting to a Clerk-
    hosted sign-in page: ``Clerk.redirectToSignIn`` defaults to a
    relative ``/sign-in`` URL on the *current* host, which is this same
    bootstrap page — that creates an infinite redirect loop. Mounting
    inline avoids the loop entirely and matches the Goosetown UX (no
    domain-bouncing during sign-in).

    Why we don't use ``__client``-cookie session sharing: Clerk's dev
    tier scopes session cookies per-application-host. The user has to
    establish a session on this host directly. Clerk dev does still
    recognize the email and offers "Continue as ..." with one click on
    re-visit, so the UX is close to "shared auth" without paying for
    Clerk Pro's cross-domain SSO.

    The publishable key is *public* (Clerk literally serves it in HTML)
    so rendering it inline is fine. The Clerk SDK script URL is derived
    from the key (Clerk encodes the frontend API host inside it).
    """
    pk = (settings.CLERK_PUBLISHABLE_KEY or "").strip()
    clerk_host = _decode_clerk_publishable_key(pk)
    if not pk or not clerk_host:
        # Misconfiguration — render a static error rather than broken JS.
        return (
            b"<!doctype html><html><body>"
            b"<h1>Teams unavailable</h1>"
            b"<p>Auth bootstrap is not configured. Contact support.</p>"
            b"</body></html>"
        )
    pk_attr = html.escape(pk, quote=True)
    sdk_src = f"https://{html.escape(clerk_host, quote=True)}/npm/@clerk/clerk-js@5/dist/clerk.browser.js"
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to Teams</title>
<style>{_STUB_BASE_CSS}
  body{{flex-direction:column;gap:16px}}
  #status{{font-size:13px;color:#6b7280}}
  #signin-root{{display:flex;justify-content:center;width:100%;max-width:480px}}
</style>
</head><body>
<div id="status">Loading…</div>
<div id="signin-root"></div>
<script async crossorigin="anonymous" data-clerk-publishable-key="{pk_attr}" src="{sdk_src}" type="text/javascript"></script>
<script>
(async () => {{
  const status = document.getElementById('status');
  const root = document.getElementById('signin-root');
  const setStatus = (msg) => {{ status.textContent = msg; }};

  // Wait for Clerk SDK to attach
  const start = Date.now();
  while (!window.Clerk && Date.now() - start < 10000) {{
    await new Promise(r => setTimeout(r, 50));
  }}
  if (!window.Clerk) {{
    setStatus('Failed to load Clerk SDK. Refresh to try again.');
    return;
  }}

  try {{
    await window.Clerk.load();
  }} catch (e) {{
    setStatus('Auth failed to initialize: ' + (e && e.message || e));
    return;
  }}

  async function finishHandshake() {{
    setStatus('Setting up your session…');
    try {{
      const token = await window.Clerk.session.getToken();
      const r = await fetch('/__handshake__', {{
        method: 'POST',
        headers: {{ 'Authorization': 'Bearer ' + token }},
        credentials: 'include',
      }});
      if (r.ok) {{
        location.replace(location.pathname + location.search + location.hash);
      }} else {{
        setStatus('Auth handshake failed (' + r.status + '). Refresh to retry.');
      }}
    }} catch (e) {{
      setStatus('Handshake error: ' + (e && e.message || e));
    }}
  }}

  if (window.Clerk.session) {{
    // Already signed in on this host — go straight to handshake.
    return finishHandshake();
  }}

  // Mount Clerk's sign-in component inline. Clerk fires its session-change
  // listener once sign-in completes; we then run the handshake + reload.
  setStatus('');
  window.Clerk.mountSignIn(root, {{
    routing: 'virtual',
    appearance: {{ elements: {{ rootBox: {{ width: '100%' }} }} }},
  }});
  window.Clerk.addListener(({{ session }}) => {{
    if (session) finishHandshake();
  }});
}})();
</script>
</body></html>
"""
    return body.encode("utf-8")


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
      1. ``isol8_paperclip`` cookie — fastest, no network roundtrip, set by
         the handshake endpoint after a successful Clerk validation.
      2. ``Authorization: Bearer <clerk_jwt>`` — programmatic clients
         (Postman, curl, the handshake endpoint itself, post-deploy
         smoke tests). Validated via Clerk's JWKS.

    Browser navigation that bypasses the bootstrap (e.g. a stale tab whose
    cookie expired) gets a 401 here; ``proxy()`` upgrades that to a
    bootstrap HTML response when the request looks like a top-level nav.
    """
    # 1. Proxy session cookie
    session_cookie = request.cookies.get(_PAPERCLIP_SESSION_COOKIE)
    if session_cookie:
        try:
            claims = _verify_paperclip_session(session_cookie)
            return AuthContext(user_id=claims["sub"], email=claims.get("email"))
        except pyjwt.InvalidTokenError as e:
            # Expired or tampered — fall through to Bearer/handoff; caller
            # gets a fresh cookie if either succeeds.
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
        token = request.query_params.get("__t") or None
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

    session_jwt = _mint_paperclip_session(user_id=user_id, email=email)
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


@router.post("/__provision__")
async def provision_self(request: Request) -> Response:
    """Synchronously provision a personal Paperclip company for the caller.

    The Clerk webhooks (``organization.created`` / ``organizationMembership.created``)
    only provision Paperclip rows for users who go through Clerk's org-creation
    flow. Users who reach the Teams button on a personal account would otherwise
    see the "workspace being set up" stub forever. This endpoint is the
    self-service equivalent: it runs the same provisioning chain (sign up via
    Better Auth, create company, mint service token, seed Main Agent, persist
    row) but uses ``user_id`` as the org_id sentinel since the row is keyed
    per-user anyway.

    Idempotent: returns 204 immediately if the row already exists with
    ``status="active"``. Otherwise blocks until provisioning completes
    (typically 5–10s — Better Auth signup + Paperclip company create + agent
    seed). On failure, returns 502 with the underlying error so the
    provisioning stub can surface it to the user.

    Auth: same as the proxy itself (cookie or Bearer). The bootstrap sets the
    cookie before the user ever sees the provisioning stub, so by the time
    the stub's "Set up workspace" button POSTs here, auth is already in place.
    """
    auth = await _get_paperclip_user(request)
    if not auth.email:
        raise HTTPException(
            status_code=400,
            detail="Email claim missing from auth token; cannot create Paperclip account",
        )

    repo = PaperclipRepo(table_name="paperclip-companies")
    existing = await repo.get(auth.user_id)
    if existing is not None and existing.status == "active":
        return Response(status_code=204)

    # Build provisioning chain inline (same shape as the
    # core.services.paperclip_provisioning.build_paperclip_provisioning
    # helper). Per-request httpx.AsyncClient mirrors the proxy's pattern.
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_provisioning import PaperclipProvisioning

    async with httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=30.0,
    ) as http:
        admin = PaperclipAdminClient(http_client=http)
        provisioning = PaperclipProvisioning(admin, repo, env_name=settings.ENVIRONMENT)
        try:
            # Personal context: pass user_id as the owner_id — for
            # personal owners ``owner_id == clerk_user_id`` and the
            # by-org-id GSI partitions cleanly because each personal
            # user is its own one-member "org" on the storage side.
            await provisioning.provision_org(
                owner_id=auth.user_id,
                owner_user_id=auth.user_id,
                owner_email=auth.email,
            )
        except Exception as e:  # noqa: BLE001 — surface to caller for stub UI.
            logger.exception(
                "paperclip_proxy: self-provision failed for user=%s: %s",
                auth.user_id,
                e,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Provisioning failed: {type(e).__name__}",
            )
    return Response(status_code=204)


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
    # The earlier f-string concat let those through unescaped, which would
    # split or corrupt query state when the next request decoded the URL
    # (Codex P2 on PR #495).
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
    the user lands on company.isol8.co without prerequisite state.
    """
    return (settings.FRONTEND_URL or "").rstrip("/") or "https://dev.isol8.co"


async def _autoprovision(auth: AuthContext) -> None:
    """Synchronously create a personal Paperclip company for ``auth``.

    Mirrors what the Clerk org.created webhook does, but passes
    ``user_id`` as the ``owner_id`` since for personal owners
    ``owner_id == clerk_user_id``. Idempotent — provision_org
    short-circuits if a row already exists with status="active".
    Raises HTTPException(502) on failure.
    """
    if not auth.email:
        raise HTTPException(
            status_code=400,
            detail="Email claim missing from auth token; cannot create Paperclip account",
        )
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_provisioning import PaperclipProvisioning

    repo = PaperclipRepo(table_name="paperclip-companies")
    async with httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=30.0,
    ) as http:
        admin = PaperclipAdminClient(http_client=http)
        provisioning = PaperclipProvisioning(admin, repo, env_name=settings.ENVIRONMENT)
        try:
            await provisioning.provision_org(
                owner_id=auth.user_id,
                owner_user_id=auth.user_id,
                owner_email=auth.email,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "paperclip_proxy: autoprovision failed for user=%s: %s",
                auth.user_id,
                e,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Provisioning failed: {type(e).__name__}",
            )


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

        # Email guard: _autoprovision needs auth.email to call Better
        # Auth signup. If the Clerk JWT is missing the email claim
        # (older template, misconfigured org), spawning the task would
        # raise immediately and asyncio would swallow the exception —
        # the row would stay None forever and the user would loop in
        # the setting-up stub. Surface the error explicitly via /chat
        # instead.
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
        # auto-refreshes until the row goes status="active". Codex P1
        # on PR #495 — keeps the raw Clerk JWT in the address bar for
        # only ~50ms instead of the full provisioning duration.
        # provision_org is idempotent so a fast double-click is safe.
        repo = PaperclipRepo(table_name="paperclip-companies")
        existing = await repo.get(auth.user_id)
        if existing is None or existing.status != "active":
            asyncio.create_task(_autoprovision(auth))

        # Mint cookie + 302 to clean URL.
        session_jwt = _mint_paperclip_session(user_id=auth.user_id, email=auth.email)
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
    repo = PaperclipRepo(table_name="paperclip-companies")
    company = await repo.get(auth.user_id)
    if company is None or company.status == "provisioning":
        # Either: (a) handshake just spawned _autoprovision and the row
        # hasn't been written yet (typical first visit), or (b) it's
        # actively in-flight. Show the auto-refresh stub; do NOT
        # re-spawn from this path — the stub reloads every 2s and a
        # re-spawn each cycle would queue duplicate concurrent provision
        # runs that race against each other (provision_org is only
        # safely idempotent for already-active rows; concurrent failed
        # retries can flip a successful row back to "failed"). Codex P1
        # on PR #499. If the initial spawn somehow died silently, the
        # user will eventually close the tab and re-handshake — that's
        # the recovery path, NOT auto-respawn.
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

    # Email comes from the Clerk JWT (``email`` claim). The Isol8
    # ``users`` DynamoDB row only stores ``user_id`` + ``created_at``
    # — there is no email column to fall back on, so if the JWT lacks
    # the claim we cannot sign the user in.
    email = (auth.email or "").strip()
    if not email:
        logger.warning(
            "paperclip_proxy: user %s has no email claim on JWT; cannot sign in to Paperclip",
            auth.user_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Email claim missing from auth token",
        )

    try:
        password = decrypt(company.paperclip_password_encrypted)
    except ValueError as e:
        # Encryption key rotated, or row was written under a different
        # key. The user needs to be reprovisioned — surface a clear 500
        # rather than a confusing upstream auth failure.
        logger.error(
            "paperclip_proxy: failed to decrypt password for user %s: %s",
            auth.user_id,
            e,
        )
        raise HTTPException(status_code=500, detail="Could not decrypt session credentials")

    # ---- Sign in to Paperclip + forward the request ----
    # Per-request httpx.AsyncClient: simple lifecycle, no shared-state
    # gotchas. Cloud Map A-record TTL is 10s so DNS is re-resolved
    # often enough; v2 should hoist this into a long-lived shared
    # client to amortize TLS + connection-pool setup.
    async with httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=30.0,
    ) as client:
        admin = PaperclipAdminClient(http_client=client)
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

        session_cookie = signin.get("_session_cookie") or ""
        if not session_cookie:
            logger.error(
                "paperclip_proxy: Better Auth response had no Set-Cookie for user %s",
                auth.user_id,
            )
            raise HTTPException(status_code=502, detail="Paperclip auth response malformed")

        # Build forwarding headers. We add X-Forwarded-* so Paperclip's
        # access logs reflect the real client identity (not the ALB IP).
        # Source the public hostname Paperclip should use when emitting
        # absolute callback/redirect URLs. Order matters:
        #   1. X-Forwarded-Host — set by Vercel for hosts on its rewrite
        #      path (dev.company.isol8.co, company.isol8.co). Holds the
        #      real public host the user typed; the only header that
        #      survives the Vercel→API-Gateway hop with the company host
        #      intact. Codex P1 on PR #497.
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
        # token and emit malformed absolute URLs. Codex P2 on PR #499.
        forwarded_host = forwarded_host_raw.split(",", 1)[0].strip()
        client_host = request.client.host if request.client else ""
        # Auth via Cookie, NOT Authorization: Bearer — Paperclip's
        # Better Auth config does not enable the bearer() plugin, so a
        # bearer header is silently ignored and the upstream sees the
        # request as anonymous. session_cookie is the bare ``name=value``
        # captured from Better Auth's Set-Cookie on sign-in.
        upstream_headers: dict[str, str] = {
            **_filter_request_headers(request),
            "Cookie": session_cookie,
            "X-Forwarded-Host": forwarded_host,
            "X-Forwarded-Proto": "https",
        }
        # Origin fallback. Same-origin browser GET/HEAD requests often
        # omit Origin entirely. Without it, Better Auth's internal
        # getSession derives a non-https request URL and looks for the
        # bare cookie name (no ``__Secure-`` prefix), so the session we
        # just signed in for is invisible upstream and privileged routes
        # run as anonymous → 401/403. Stamp PAPERCLIP_PUBLIC_URL when
        # the inbound request didn't carry an Origin. Codex P1 on PR #507.
        if "origin" not in {k.lower() for k in upstream_headers} and settings.PAPERCLIP_PUBLIC_URL:
            upstream_headers["Origin"] = settings.PAPERCLIP_PUBLIC_URL
        if client_host:
            upstream_headers["X-Forwarded-For"] = client_host

        # httpx accepts a leading ``/`` in the path even when base_url
        # already has a trailing ``/`` — it normalizes correctly.
        upstream_url = f"/{path}"
        body_bytes = await request.body()
        try:
            upstream = await client.request(
                method=request.method,
                url=upstream_url,
                params=request.query_params,
                content=body_bytes,
                headers=upstream_headers,
            )
        except httpx.HTTPError as e:
            logger.exception(
                "paperclip_proxy: upstream request failed user=%s method=%s path=%s: %s",
                auth.user_id,
                request.method,
                path,
                e,
            )
            _record_outcome(502)
            return Response(
                content=b"Bad gateway",
                status_code=502,
                media_type="text/plain",
            )

    _record_outcome(upstream.status_code)

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
#   3. Look up the user's PaperclipCompany row, decrypt the password.
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

    # ---- Step 2: Look up Paperclip company + decrypt password ----
    # The paperclip-companies table is keyed per-USER (not per-org): every
    # member, including org owners, gets their own row with their own
    # Better Auth password. The HTTP path keys lookup on
    # ``auth.user_id`` for the same reason. An earlier draft of this WS
    # handler tried to resolve an ``owner_id`` from the org claim and
    # query that — that key never exists in the table, so any user in an
    # org context was getting closed with 4503 on every WS upgrade.
    # Pass the short name; ``get_table`` adds the env prefix exactly once.
    repo = PaperclipRepo(table_name="paperclip-companies")
    company = await repo.get(user_id)
    if company is None or company.status != "active":
        await websocket.close(code=4503, reason="Paperclip not provisioned")
        return

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
