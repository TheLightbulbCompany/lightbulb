# Load environment variables FIRST, before any other imports
from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
from contextlib import asynccontextmanager

from core.observability.logging import configure_logging

configure_logging(level="INFO")

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from core.auth import get_current_user
from core.config import settings
from core.containers import (
    get_gateway_pool,
    startup_containers,
    shutdown_containers,
)
from core.observability.e2e_correlation import E2ECorrelationMiddleware
from core.repositories import container_repo
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.update_service import run_scheduled_worker
from routers import (
    agent_builder,
    agent_mail_relay,
    agent_mail_send,
    apple_iap,
    automations,
    billing,
    browser_view,
    channels,
    config,
    connections,
    container,
    container_files,
    container_recover,
    debug,
    desktop_auth,
    desktop_pair,
    devices,
    google_accounts,
    hooks_credentials,
    hooks_proxy,
    integrations,
    meta_webhook,
    metronome_webhook,
    oauth,
    onboarding,
    ops,
    orgs,
    paperclip_events,
    paperclip_proxy,
    paperclip_trigger_proxy,
    platform_info,
    ses_inbound,
    settings_keys,
    support_tickets,
    updates,
    users,
    webhooks,
    websocket_chat,
    workspace_files,
)
from routers.teams import router as teams_router
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Liveness gate for the ALB /health probe. Flipped True at the very end of the
# lifespan startup block (just before ``yield``). While False, /health returns
# 503 so the ALB does not route to a not-yet-ready task.
_started = False


# ---------------------------------------------------------------
# Paperclip host dispatch
# ---------------------------------------------------------------
#
# When a request arrives at ``company.isol8.co`` (or the env-suffixed
# variant ``company-{env}.isol8.co``) we dispatch the entire request to
# the Paperclip reverse-proxy router instead of the standard ``/api/v1``
# surface. The dispatch key is ``X-Isol8-Public-Host``, NOT ``Host``:
# API Gateway HTTP API rewrites the upstream ``Host`` header to the
# integration target's DNS (the internal ALB), so by the time we see the
# request the original public hostname is gone from ``Host``. The
# parameter mapping in ``api-stack.ts`` copies ``$context.domainName``
# into ``X-Isol8-Public-Host`` so the dispatch key survives the integration
# hop. (We can't use ``X-Forwarded-Host`` for this — API Gateway HTTP
# API restricts parameter mapping on ``x-forwarded-*`` headers.)
#
# Implementation: rewrite the scope path with a sentinel prefix and
# mount the paperclip_proxy router at that prefix. The standard FastAPI
# routing system handles the dispatch from there. The double-underscore
# prefix is a deliberate choice — it's not a valid path segment for any
# user-facing route and can never be hit from a normal browser URL.
#
# Note on middleware ordering: Starlette runs middleware in LIFO source
# order on the way IN (the last ``add_middleware`` call wraps outermost
# and runs first). We register ``HostDispatcherMiddleware`` BEFORE
# ``ProxyHeadersMiddleware`` so the dispatcher runs AFTER proxy-headers
# on inbound requests — though we only read X-Isol8-Public-Host directly
# from the ASGI scope, so the ordering doesn't matter for correctness.
# What does matter is that we run before any auth/CORS/etc. middleware
# we want to skip for proxied traffic, which the placement here gives
# us automatically.

PAPERCLIP_PROXY_PREFIX = "/__paperclip_proxy__"


def _paperclip_dispatch_hosts() -> set[str]:
    """Hostnames that should be routed to the paperclip_proxy router.

    Built from ``settings.PAPERCLIP_PUBLIC_URL`` (set by CDK in deployed
    envs to ``https://company.isol8.co`` or ``https://company-{env}.isol8.co``)
    plus a small allow-list of local-dev hostnames so a backend running on
    localhost dispatches company.* without extra config.
    """
    hosts: set[str] = {
        "company.localhost",
        "company.local",
    }
    public = (settings.PAPERCLIP_PUBLIC_URL or "").strip()
    if public:
        # Strip scheme + path; we only want the host[:port] part, then
        # drop the port for a normalized comparison key.
        from urllib.parse import urlparse

        parsed = urlparse(public if "://" in public else f"https://{public}")
        if parsed.hostname:
            hosts.add(parsed.hostname.lower())
    return hosts


class HostDispatcherMiddleware:
    """Pure ASGI middleware that rewrites the request scope path with a
    sentinel prefix when the request arrives at a Paperclip-proxy host.

    Reads ``X-Isol8-Public-Host`` directly off the ASGI scope headers.
    We can't use ``X-Forwarded-Host`` because API Gateway HTTP API
    restricts parameter mapping on ``x-forwarded-*`` headers ("Operations
    on header x-forwarded-host are restricted"); ``api-stack.ts`` sets
    our custom header via ``overwrite:header.X-Isol8-Public-Host`` =
    ``$context.domainName`` on the integration. Reading it off the raw
    scope sidesteps Starlette's ``ProxyHeadersMiddleware`` which only
    promotes ``X-Forwarded-For`` / ``X-Forwarded-Proto``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Compute dispatch hosts once; settings are immutable for the
        # lifetime of the process (CDK redeploys restart the task).
        self._hosts = _paperclip_dispatch_hosts()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Walk the raw header list (case-insensitive bytes keys) rather
        # than building a dict — duplicate header collapse via dict()
        # would silently drop the second value of any repeated header.
        xph_raw: bytes | None = None
        for name, value in scope.get("headers", ()):
            if name == b"x-isol8-public-host":
                xph_raw = value
                break
        if xph_raw is None:
            await self.app(scope, receive, send)
            return

        # X-Isol8-Public-Host is set by API Gateway parameter mapping to
        # exactly $context.domainName, so it shouldn't carry comma chains
        # or ports — but defensively normalize anyway in case some other
        # caller (local dev, tests) sets it manually.
        xph_full = xph_raw.decode("latin-1", errors="replace")
        xph = xph_full.split(",", 1)[0].split(":", 1)[0].strip().lower()
        if xph not in self._hosts:
            await self.app(scope, receive, send)
            return

        # Dispatch path-rewrite. Preserve query string, headers, and
        # client. Both ``path`` (decoded str) and ``raw_path`` (bytes)
        # need updating — Starlette routing reads ``path`` but some
        # ASGI servers / WS handlers prefer ``raw_path``.
        new_scope = dict(scope)
        original_path = scope.get("path", "")
        new_scope["path"] = PAPERCLIP_PROXY_PREFIX + original_path
        raw_path = scope.get("raw_path")
        if raw_path is None:
            raw_path = original_path.encode("latin-1", errors="replace")
        new_scope["raw_path"] = PAPERCLIP_PROXY_PREFIX.encode("ascii") + raw_path
        await self.app(new_scope, receive, send)


class SecurityHeadersMiddleware:
    """Add baseline security response headers to every HTTP response.

    - ``X-Content-Type-Options: nosniff`` — stop MIME-sniffing of API responses.
    - ``X-Frame-Options: SAMEORIGIN`` — clickjacking guard (the backend also
      reverse-proxies the Paperclip SPA HTML, for which framing isn't expected).
    - ``Referrer-Policy: strict-origin-when-cross-origin`` — limit referrer leak.

    ``Strict-Transport-Security`` is set here too because the REST API path is
    ALB -> ECS with no CloudFront in front, and an ALB can't add headers to
    *forwarded* responses — so the app is the practical place to assert HSTS for
    api.isol8.co + the proxied company.isol8.co. (Browsers ignore HSTS over
    plain HTTP, so it's a no-op for local dev / internal health probes.)

    Deliberately NO blanket Content-Security-Policy here: a strict CSP would
    break the proxied Paperclip SPA, so CSP belongs on those responses
    specifically (the Next.js frontend sets the app's CSP). Only adds a header
    if the downstream response didn't already set it.
    """

    _HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"strict-transport-security", b"max-age=31536000"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in self._HEADERS:
                    if name not in present:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _running_count_gauge_loop():
    """Periodically emit the gateway running-count gauges.

    Replaces the old _safe_idle_checker — flat-fee cutover removed
    scale-to-zero, but the W5 alarm on gateway.connection.open still
    expects a steady metric stream.
    """
    while True:
        try:
            pool = get_gateway_pool()
            await pool.emit_running_gauges()
        except Exception:
            logger.warning("running_gauges loop iteration failed", exc_info=True)
        await asyncio.sleep(60)


async def _resume_wake_resize_transitions() -> None:
    """Resume the provisioning -> running poller for containers that were
    mid-transition on a NON-provisioning path (wake-from-stopped, resize)
    when the backend last shut down.

    SFN-owned rows are explicitly EXCLUDED (``provision_claim_id`` present):
    the PROVISION Step Functions executor (spec §7, PR-10) survives backend
    process death natively — bounded per-attempt + total timeout, its own
    retries — so a "provisioning" row created via ``dispatch_provision`` no
    longer needs an in-process resume at all; that used to be this function's
    main job (the old boot-reconciler re-arm) and is now dead weight AT BEST
    and UNSAFE at worst — resuming a bare, unfenced ``_await_running_transition``
    (claim_id=None -> plain ``update_fields``, no condition) over a row an
    SFN execution still owns could race the runner's own FENCED write with
    an unconditioned one.

    ``start_user_service`` (wake) and ``resize_user_container`` never claim a
    provision lease — their "provisioning" writes carry no
    ``provision_claim_id`` — so their in-process pollers dying mid-poll (a
    deploy or crash) still leaves a real gap this covers: on boot, find
    provisioning rows with NO stamped claim and re-kick a plain (unfenced)
    poller for them, exactly as before. Idempotent (a poller finding an
    already-healthy container transitions it on its first iteration and
    exits).
    """
    from core.containers import get_ecs_manager

    try:
        rows = await container_repo.get_by_status("provisioning")
    except Exception:
        logger.warning("Could not resume wake/resize transitions on startup", exc_info=True)
        return

    ecs = get_ecs_manager()
    for row in rows:
        if row.get("provision_claim_id"):
            # SFN-owned — see docstring. Not ours to resume.
            continue
        owner_id = row["owner_id"]
        asyncio.create_task(ecs._await_running_transition(owner_id))
        logger.info("Resumed provisioning -> running poller for %s (wake/resize, unclaimed)", owner_id)


async def _reconcile_trial_idle_containers() -> None:
    """One-time boot sweep that stops trial containers leaked across a restart.

    Task 12's extended-idle scale-to-zero (``GatewayConnectionPool._grace_close``
    / ``_maybe_stop_trial_container``) is in-process: the grace timer that
    watches a ``trial_local`` owner after their last chat tab closes is a plain
    ``asyncio.Task`` living on a single backend instance. A deploy or crash
    kills that task mid-grace, and nothing else re-arms the stop check — the
    container it was watching keeps running (and billing Bedrock credits)
    forever. This is the one-time recovery for exactly that leak: on boot,
    read every ``status="running"`` container row and dispatch its owner
    straight to ``pool._maybe_stop_trial_container``.

    Deliberately does NOT re-derive the trial_local / active-run / live-chat
    pre-filters here. ``_maybe_stop_trial_container`` already re-runs its full
    veto ladder from scratch on every call (billing_repo re-read, has_active_runs,
    is_parked, cross-task live-chat count via connection_service) — every row is
    dispatched unconditionally and the method itself decides who is actually
    safe to stop. Duplicating those reads in this pass would double the DDB
    traffic for zero extra safety and risk drifting out of sync with the one
    place (Task 12) that already owns "who never gets stopped."

    ONE-TIME — no loop, no interval. This codebase deliberately runs no
    sweepers: whatever leaked from a previous process's death is caught at
    the next restart, and Task 12's event-driven grace covers every
    subsequent connect/disconnect without polling. A steady-state interval
    here would just be a slower, wasteful version of the same veto ladder
    this function already dispatches to once.
    """
    try:
        rows = await container_repo.get_by_status("running")
    except Exception:
        logger.warning("Could not reconcile trial idle containers on startup", exc_info=True)
        return

    pool = get_gateway_pool()
    for row in rows:
        owner_id = row["owner_id"]
        asyncio.create_task(pool._maybe_stop_trial_container(owner_id, trigger="boot_reconcile"))
        logger.info("Boot reconcile: dispatched trial-stop check for %s", owner_id)


async def _build_teams_event_broker():
    """Construct the singleton TeamsEventBroker used by /ws/message.

    Returns None when the dependencies aren't configured (e.g. local
    dev without Paperclip running) — the broker is best-effort and
    /teams continues to work via REST without realtime updates.
    """
    try:
        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.connection_service import ConnectionService
        from core.services.management_api_client import ManagementApiClient
        from core.services.paperclip_event_client import PaperclipEventClient
        from core.services.paperclip_user_session import get_user_session_cookie
        from core.services.teams_event_broker import TeamsEventBroker

        # Best-effort: if the Paperclip env isn't configured (no internal
        # URL), don't try to start the broker — /teams REST still works,
        # just without live updates.
        paperclip_url = (settings.PAPERCLIP_INTERNAL_URL or "").strip()
        if not paperclip_url:
            logger.info("teams broker: PAPERCLIP_INTERNAL_URL unset; skipping startup")
            return None

        # Resolve ws:// / wss:// from the http(s):// internal URL.
        if paperclip_url.startswith("https://"):
            ws_base = "wss://" + paperclip_url[len("https://") :]
        elif paperclip_url.startswith("http://"):
            ws_base = "ws://" + paperclip_url[len("http://") :]
        else:
            ws_base = paperclip_url
        ws_base = ws_base.rstrip("/")

        repo = PaperclipRepo(table_name="paperclip-companies")
        conn_svc = ConnectionService()
        mgmt_api = ManagementApiClient()

        from core.services.clerk_admin import get_primary_email
        from core.services.paperclip_admin_client import shared_admin_client

        async def resolve_company_id(user_id: str) -> str:
            company = await repo.get(user_id)
            if company is None:
                raise RuntimeError(f"no paperclip company for user {user_id}")
            return company.company_id

        async def resolve_session_cookie(user_id: str) -> str:
            return await get_user_session_cookie(
                user_id=user_id,
                repo=repo,
                admin_client=shared_admin_client(),
                clerk_email_resolver=get_primary_email,
            )

        def client_factory(*, user_id: str, company_id: str, cookie: str, on_event):
            return PaperclipEventClient(
                url=f"{ws_base}/api/companies/{company_id}/events/ws",
                cookie=cookie,
                on_event=on_event,
                user_id=user_id,
                company_id=company_id,
            )

        broker = TeamsEventBroker(
            client_factory=client_factory,
            management_api=mgmt_api,
            connection_service=conn_svc,
            resolve_company_id=resolve_company_id,
            resolve_session_cookie=resolve_session_cookie,
        )
        logger.info("teams broker: started; ws_base=%s", ws_base)
        return broker
    except Exception:
        logger.exception("teams broker: startup failed; /teams will run without live updates")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    logger.info("Starting application...")
    # A starting app instance is not shutting down. Resetting here (not only at
    # shutdown) keeps the flag scoped to this instance's lifecycle, so a reused-app
    # lifespan (e.g. one ASGI test client per hypothesis example) never inherits a
    # stale "shutting down" from a prior shutdown.
    set_shutting_down(False)
    await startup_containers()
    await _resume_wake_resize_transitions()
    await _reconcile_trial_idle_containers()
    worker_task = asyncio.create_task(run_scheduled_worker())

    # Teams event broker — proxies Paperclip's per-company live-events WS
    # to subscribed browser tabs via API Gateway Management API. Started
    # after the gateway pool (so management_api singleton is initialized)
    # but before anything that depends on its API.
    broker = await _build_teams_event_broker()
    if broker is not None:
        from core.services import teams_event_broker_singleton

        teams_event_broker_singleton.set_broker(broker)

    gauge_task = asyncio.create_task(_running_count_gauge_loop())

    # Register background tasks so /admin/system/health can surface their state.
    # See core/services/system_health.py — admin dashboard reads this dict.
    from core.services import system_health

    system_health.BACKGROUND_TASKS["scheduled_worker"] = worker_task
    system_health.BACKGROUND_TASKS["running_gauges"] = gauge_task

    # Startup complete — flip the liveness gate so /health returns 200.
    global _started
    _started = True

    yield

    # Shutdown — bounded so no step can ride into the ECS SIGKILL window.
    # Ladder: this timeout(20) < uvicorn --timeout-graceful-shutdown 25 < ECS stopTimeout 30.
    logger.info("Shutting down application...")
    set_shutting_down(True)  # 503 new business requests; /health stays green for ALB drain
    from core.services.update_service import request_worker_stop

    try:
        async with asyncio.timeout(20):
            # First: terminate any in-flight inline provision child. It must
            # land before the SIGKILL window so no runner outlives this
            # instance; everything below is process-local cleanup. No
            # StartExecution — the state machine has been watching each claim
            # since it was minted and takes the ECS leg over on its own clock.
            from core.services.provision_dispatch import handoff_inflight_provisions

            await handoff_inflight_provisions()
            system_health.BACKGROUND_TASKS.clear()
            # Worker: graceful stop (finish current iteration); gauge: stateless hard-cancel.
            request_worker_stop()
            gauge_task.cancel()
            try:
                await gauge_task
            except asyncio.CancelledError:
                pass
            # Give the worker a short window to honor the stop event and finish
            # its current iteration cleanly. wait_for hard-cancels it on timeout,
            # so a wedged iteration can't consume the whole shutdown budget.
            try:
                await asyncio.wait_for(worker_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            if broker is not None:
                from core.services import teams_event_broker_singleton

                await broker.shutdown()
                teams_event_broker_singleton.set_broker(None)
            await shutdown_containers()
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown exceeded 20s budget; exiting to let the platform reclaim the task")


openapi_tags = [
    {
        "name": "users",
        "description": "User registration and sync.",
    },
    {
        "name": "websocket",
        "description": "WebSocket connect, disconnect, and message endpoints (API Gateway integration).",
    },
    {
        "name": "billing",
        "description": "Billing, usage tracking, and subscription management.",
    },
    {
        "name": "container",
        "description": "OpenClaw container RPC proxy for the control panel.",
    },
    {
        "name": "channels",
        "description": "Messaging channel management (Telegram, Discord, WhatsApp).",
    },
    {
        "name": "integrations",
        "description": "MCP server and ClawHub integration management.",
    },
    {
        "name": "debug",
        "description": "Dev-only container provisioning.",
    },
    {
        "name": "health",
        "description": "Root and health check endpoints for verifying API availability.",
    },
]

app = FastAPI(
    title="Isol8 API",
    description=(
        "AI agent platform powered by OpenClaw. Personal agents with persistent memory and streaming LLM inference."
    ),
    version="2.0.0",
    # Schema off in prod like /docs — nothing consumes it there, and it maps
    # the full route inventory (admin/debug/webhook shapes) for free.
    openapi_url=None if settings.ENVIRONMENT == "prod" else f"{settings.API_V1_STR}/openapi.json",
    docs_url=None if settings.ENVIRONMENT == "prod" else "/docs",
    redoc_url=None if settings.ENVIRONMENT == "prod" else "/redoc",
    servers=(
        [{"url": "https://api-dev.isol8.co", "description": "Development"}]
        if settings.ENVIRONMENT == "dev"
        else [{"url": "https://api.isol8.co", "description": "Production"}]
        if settings.ENVIRONMENT == "prod"
        else [
            {"url": "http://localhost:8000", "description": "Local development"},
            {"url": "https://api-dev.isol8.co", "description": "Development"},
        ]
    ),
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)


# PaperclipApiError handler — translate upstream Paperclip non-2xx responses into
# matching HTTPExceptions so the BFF returns a faithful status to the browser
# instead of a generic 500. ``PaperclipApiError`` is raised by every method on
# ``PaperclipAdminClient`` (see ``_post`` / ``_get`` / ``_patch`` / ``_delete``)
# whenever Paperclip responds with a non-2xx; without this handler the Teams
# BFF (``routers/teams/*.py``) would propagate it as an unhandled exception
# and FastAPI would return 500 with a useless body. We forward Paperclip's
# original status code (e.g. 403 / 404) and surface the raw upstream body
# under ``upstream_status`` so panel-side error handling has something to
# branch on. The ``502`` fallback is for the (currently-impossible) case
# where ``status_code`` is outside the standard HTTP range — we can't
# legitimately return that to the client, so map to "bad gateway" which
# is exactly what an out-of-range upstream status implies.
@app.exception_handler(PaperclipApiError)
async def paperclip_api_error_handler(request, exc: PaperclipApiError):
    # 401 self-heal (Teams BFF only). A Teams request forwards the browser's
    # Paperclip session cookie upstream; once that cookie expires/rotates the
    # upstream returns 401. Without intervention the stale cookie keeps being
    # forwarded and the user loops 401 forever (the cookie is HttpOnly, so the
    # frontend can't clear it). So: catch the upstream 401, FORCE a fresh
    # per-user sign-in, overwrite the browser cookie on the response, and
    # return a 401 with ``{"detail":"session refreshed, retry"}``.
    #
    # Contract with the frontend (wired in a later step): on a 401 from a
    # Teams call, retry the request EXACTLY ONCE — the retry now carries the
    # freshly reseeded cookie and succeeds. We do this here, in ONE exception
    # handler, rather than wrapping each of the ~40 Teams handlers.
    #
    # ``request.state.teams_user_id`` is stamped by ``_ctx`` before any
    # upstream call. Its presence is what scopes this self-heal to Teams BFF
    # requests; the board proxy handles its own 401 fallback inline and never
    # sets this, so it falls through to the faithful-status path below.
    teams_user_id = getattr(request.state, "teams_user_id", None)
    if exc.status_code == 401 and teams_user_id:
        from core.services.paperclip_session_cookie import resolve_session_cookie
        from routers.teams.agents import get_user_session_cookie_for

        refresh = JSONResponse(
            status_code=401,
            content={"detail": "session refreshed, retry"},
        )
        try:
            await resolve_session_cookie(
                cookies=request.cookies,
                response=refresh,
                user_id=teams_user_id,
                mint=lambda: get_user_session_cookie_for(teams_user_id),
                force=True,
            )
        except Exception:
            # The re-mint (a fresh per-user sign-in) can fail for many reasons:
            # SessionCookieError (missing key), PaperclipApiError (sign-in 4xx/5xx),
            # UserSessionError (workspace no longer active), Clerk lookup errors.
            # Any of them must degrade to a plain 401 — never escape this handler
            # (which would 500) or loop the client.
            logger.warning("teams 401 self-heal: re-mint failed for user %s", teams_user_id, exc_info=True)
            return JSONResponse(
                status_code=401,
                content={"detail": str(exc), "upstream_status": exc.status_code},
            )
        return refresh

    status = exc.status_code if 400 <= exc.status_code < 600 else 502
    if status >= 500:
        # str(exc) is "GET /api/companies/<company_uuid>/... -> 5xx" — it embeds
        # the internal Paperclip path + company UUID, and an upstream 5xx carries
        # nothing client-actionable. Log the real detail server-side and return a
        # generic body so we don't leak backend architecture to the caller.
        logger.warning("paperclip upstream error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"detail": "upstream error", "upstream_status": 502},
        )
    return JSONResponse(
        status_code=status,
        content={"detail": str(exc), "upstream_status": exc.status_code},
    )


# Request-ID middleware — generates/propagates X-Request-ID for log correlation.
# Added first so it runs innermost (after CORS and ProxyHeaders).
from core.observability.middleware import RequestContextMiddleware

app.add_middleware(RequestContextMiddleware)

# Shutdown-aware middleware — returns 503 to new business requests once the
# lifespan shutdown sets the process flag, while /health stays green for ALB
# drain. See core/middleware/shutdown_aware.py.
from core.middleware.shutdown_aware import ShutdownAwareMiddleware, set_shutting_down

app.add_middleware(ShutdownAwareMiddleware)

# Host-dispatch middleware (Paperclip) — rewrites the ASGI scope path to
# the paperclip_proxy mount prefix when X-Isol8-Public-Host names a
# Paperclip domain. Added before ProxyHeadersMiddleware so it sits INSIDE
# the proxy-headers wrapper on the inbound path (Starlette runs middleware
# in LIFO source order — last added runs first). The dispatcher reads
# X-Isol8-Public-Host directly off the scope, so it doesn't actually depend
# on ProxyHeadersMiddleware running first; the placement here is about
# letting CORS/E2E/admin-metrics still see all traffic uniformly.
app.add_middleware(HostDispatcherMiddleware)

# Proxy-headers middleware — ALB terminates TLS and forwards X-Forwarded-Proto.
# Without this, FastAPI generates http:// URLs in redirects (e.g. redirect_slashes),
# which breaks clients behind HTTPS.
#
# trusted_hosts is scoped to the VPC CIDR (10.0.0.0/16) plus loopback. This
# matters because uvicorn's ProxyHeadersMiddleware ALSO promotes inbound
# X-Forwarded-For into request.client.host whenever it trusts the immediate
# peer. The previous "*" form let any caller spoof request.client.host on
# every route that consumes it (audit logs, rate-limit keys, etc.). Scoping
# to the VPC CIDR means:
#   - Production: the immediate TCP peer is always the ALB (private IP in
#     the 10.0.0.0/16 VPC), so the middleware trusts it and X-Forwarded-Proto
#     promotion still works — HTTPS redirect URLs continue to render.
#   - Test/dev: TestClient's peer is the literal string "testclient" (not
#     in any CIDR), so middleware does NOT promote — request.client.host
#     stays as the synthetic peer and any spoofed XFF in test inputs is
#     correctly ignored.
# Requires uvicorn >= 0.31 for ipaddress.ip_network matching in trusted_hosts
# (older versions only do exact-string set membership).
#
# RESIDUAL CAVEAT (not fixed here, tracked as follow-up): in production, the
# AWS ALB by default APPENDS the real client IP to any inbound XFF rather
# than stripping it. The backend then sees `XFF: <attacker-spoofed>, <real>`
# and uvicorn's middleware reads the LEFTMOST entry, which is the spoofed
# one. So request.client.host (and audit-log source IPs derived from it) are
# still attacker-influenced in production. The fix is one of:
#   - enable XFF stripping at the ALB (AWS-side attribute / WAF rule), or
#   - swap to a middleware that reads the rightmost XFF (trust N hops from
#     the right).
# The audit-log half of this gap is closed: audit rows now derive their IP
# from core/services/admin_audit.extract_client_ip, which walks XFF from the
# RIGHT skipping our own infrastructure hops (VPC CIDR / loopback) and takes
# the first untrusted entry — the client hop API Gateway established — and
# ignores the promoted request.client.host. Other request.client.host
# consumers (rate-limit keys etc.) still see the leftmost hop until an
# edge-side fix lands.
# Both per-env VPC CIDRs (network-stack.ts ENV_CONFIG): dev 10.0.0.0/16,
# prod 10.2.0.0/16. The list previously carried only the dev CIDR, so in
# prod the ALB peer was never trusted and X-Forwarded-Proto promotion
# silently didn't happen there.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=["10.0.0.0/16", "10.2.0.0/16", "127.0.0.1", "::1"],
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-E2E-Run-Id"],
    # Expose X-Request-ID (set on every response by RequestContextMiddleware) so
    # cross-origin browser JS can read it. Without this, response.headers.get(
    # "X-Request-ID") is null cross-origin (it works same-origin, which masks the
    # gap in local dev). The frontend attaches recent request IDs to support
    # tickets for log correlation. Named explicitly rather than "*" as the
    # mode-agnostic, future-proof choice — unrelated to credentials.
    expose_headers=["X-Request-ID"],
)

# E2E correlation middleware — binds X-E2E-Run-Id header to log context so
# every log line emitted while handling an e2e harness request carries the
# same e2e_run_id field. No-op for traffic that doesn't send the header.
app.add_middleware(E2ECorrelationMiddleware)

# Admin metrics middleware (CEO O1) — emits admin_api.* CloudWatch metrics
# for every /api/v1/admin/* request. No-op for non-admin paths.
from core.middleware.admin_metrics import AdminMetricsMiddleware  # noqa: E402

app.add_middleware(AdminMetricsMiddleware)

# Security response headers (nosniff / frame-options / referrer-policy). Added
# last so it wraps outermost and stamps every response, including ones a
# downstream middleware short-circuits.
app.add_middleware(SecurityHeadersMiddleware)


def custom_openapi():
    """Override OpenAPI schema generation to inject BearerAuth security scheme."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
    )
    schema["components"] = schema.get("components", {})
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

# Routes
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(devices.router, prefix="/api/v1/devices", tags=["devices"])

# Clerk webhook events (user lifecycle)
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# SES-inbound agent-email ingest (provisioned agent email, inbound leg).
# A Lambda parses raw SES mail and POSTs the clean JSON payload here.
app.include_router(ses_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# Paperclip isol8.notifications plugin events (approvals + run lifecycle →
# push notifications). Bearer-secret auth; disabled when the secret is unset.
app.include_router(paperclip_events.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# WebSocket routes (API Gateway WebSocket -> HTTP POST)
app.include_router(websocket_chat.router, prefix="/api/v1/ws")

# Container browser live-view relay (direct browser WS via the ALB, like the
# paperclip_proxy relay — NOT the API Gateway WebSocket path above)
app.include_router(browser_view.router, prefix="/api/v1/browser-view", tags=["browser-view"])

# Billing routes
app.include_router(billing.router, prefix="/api/v1/billing", tags=["billing"])

# Apple In-App Purchase: the app's link endpoint + Apple's notification
# receiver. The router carries its own /billing and /webhooks paths because the
# two live on different auth models (Clerk token vs JWS signature), so it is
# mounted at the API root rather than under either prefix.
app.include_router(apple_iap.router, prefix=settings.API_V1_STR, tags=["billing"])

# Support ticket capture
app.include_router(support_tickets.router, prefix="/api/v1/support", tags=["support"])

# Metronome ledger webhook (low-balance -> local fast-gate flag, Phase 2).
# Route is /webhooks/metronome; mounting under API_V1_STR yields
# /api/v1/webhooks/metronome alongside the Clerk/Stripe webhook surface.
app.include_router(metronome_webhook.router, prefix=settings.API_V1_STR, tags=["billing"])

# Organization management (Gate A — invite-creation tenancy invariant)
app.include_router(orgs.router, prefix="/api/v1/orgs", tags=["orgs"])

# First-run onboarding brief (stages BRIEF.md/BOOTSTRAP.md + Set-up issue)
app.include_router(onboarding.router, prefix="/api/v1/onboarding", tags=["onboarding"])

# Container lifecycle management (GET /status, POST /retry)
app.include_router(container.router, prefix="/api/v1/container", tags=["container"])

# Workspace file uploads (POST /files)
app.include_router(container_files.router, prefix="/api/v1/container", tags=["container"])

# Container recovery + gateway control (POST /recover, POST /gateway/restart)
app.include_router(container_recover.router, prefix="/api/v1/container", tags=["container"])

# Container updates (pending updates, apply/schedule)
app.include_router(updates.router, prefix=f"{settings.API_V1_STR}/container", tags=["container"])

# Channel management (Telegram, Discord, WhatsApp)
app.include_router(channels.router, prefix="/api/v1/channels", tags=["channels"])

# Config patching (unified EFS write endpoint)
app.include_router(config.router, prefix="/api/v1/config", tags=["config"])

app.include_router(settings_keys.router, prefix="/api/v1/settings/keys", tags=["settings"])

# Integrations (MCP servers, ClawHub)
app.include_router(integrations.router, prefix="/api/v1", tags=["integrations"])

# Connections (Bucket A paste-key integrations: list / connect / disconnect)
app.include_router(connections.router, prefix="/api/v1", tags=["connections"])
# Public OAuth callback surface (provider redirects the unauthenticated browser here)
app.include_router(connections.oauth_router, prefix="/api/v1", tags=["connections"])

# Per-agent Google-account assignment (list accounts + bind/unbind an agent)
app.include_router(google_accounts.router, prefix="/api/v1", tags=["google-accounts"])

# 1-click Automations trigger seeding (connect an app, pick an event, get a
# pre-filled webhook-triggered routine) + the Stripe guided-paste whsec_
# write. See routers/automations.py.
app.include_router(automations.router, prefix="/api/v1", tags=["automations"])

# Workspace file browser (EFS agent workspace)
app.include_router(workspace_files.router, prefix="/api/v1/container", tags=["container"])

# Debug routes (dev-only container provisioning)
app.include_router(debug.router, prefix="/api/v1/debug", tags=["debug"])

app.include_router(desktop_auth.router, prefix="/api/v1/auth", tags=["desktop"])

# Desktop two-node pair / revoke (two-node architecture). Pair is idempotent;
# revoke force-closes the live WS session keyed by deviceId.
app.include_router(desktop_pair.router, prefix="/api/v1", tags=["desktop"])

# ChatGPT OAuth (device-code flow for BYO ChatGPT-token billing)
app.include_router(oauth.router, prefix="/api/v1")

# Public webhook ingress proxy. Bearer-token auth (NOT Clerk) — external
# senders (Zapier/Stripe/GitHub) hit /api/v1/hooks/u/{owner_id}/{hook_path}
# and we forward to the per-user OpenClaw container. See routers/hooks_proxy.py
# for the auth model and forwarded-header rewriting rationale.
app.include_router(hooks_proxy.router, prefix="/api/v1/hooks", tags=["hooks"])
# Tier-2 ingress: signed Paperclip routine-trigger fires at
# /api/v1/hooks/t/{owner_id}/{public_id}. No edge auth — Paperclip verifies
# the per-trigger signature upstream. See routers/paperclip_trigger_proxy.py.
app.include_router(paperclip_trigger_proxy.router, prefix="/api/v1/hooks", tags=["hooks"])
# App-LEVEL Meta ingress at /api/v1/hooks/meta — deliberately ownerless, because
# a Meta app has ONE callback URL for every user. Routed by the asset id in the
# payload, authenticated by the app-secret signature. See routers/meta_webhook.py.
app.include_router(meta_webhook.router, prefix="/api/v1/hooks", tags=["hooks"])
# Human operator diagnostics; every route requires a Clerk platform admin.
app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])
# Agent-callable bridge (hook_token auth, no Clerk) — Atlas registers built
# agents into the owner's container. See routers/agent_builder.py.
app.include_router(agent_builder.router, prefix="/api/v1/agent-builder", tags=["agent-builder"])

# Public connectable catalog (integrations + channels) read live by the baked
# isol8 skill from inside per-owner containers. No auth — non-sensitive catalog
# data. See routers/platform_info.py.
app.include_router(platform_info.router, prefix="/api/v1/platform-info", tags=["platform-info"])

# Clerk-authed read of the calling user's hook credentials. Backs the
# Settings → Webhooks panel; the user's own browser fetches their own
# webhook URL + token. See routers/hooks_credentials.py.
app.include_router(hooks_credentials.router, prefix="/api/v1/hooks", tags=["hooks"])

# Authenticated outbound agent-email reply relay (hook_token auth, no Clerk)
# — a per-user container calls this to send a threaded email reply on
# behalf of one of its agents. See routers/agent_mail_relay.py for the auth
# model (reply_token names the owner; hook_token proves the caller IS that
# owner's container).
app.include_router(agent_mail_relay.router, prefix="/api/v1/agent-mail", tags=["agent-mail"])

# Agent-INITIATED owner email (hook_token auth, no Clerk) — the same container
# calling out unprompted rather than replying. Same auth shape minus the
# reply_token, and with no recipient parameter at all: the owner is named in
# the path and the recipient is resolved server-side, so nothing a caller
# supplies can redirect the message. See routers/agent_mail_send.py.
app.include_router(agent_mail_send.router, prefix="/api/v1/agent-mail", tags=["agent-mail"])

# Admin dashboard — every endpoint gated by Depends(require_platform_admin).
from routers import admin as admin_router  # noqa: E402

app.include_router(admin_router.router, prefix="/api/v1")

# Teams BFF — native UI for Paperclip. The router itself carries the
# /teams prefix; mounting under /api/v1 yields /api/v1/teams/*.
app.include_router(teams_router, prefix="/api/v1")

# Pre-run allowance gate for the openclaw_gateway adapter (free-tier cap).
# Router carries the /paperclip prefix; mounting under /api/v1 yields

# Paperclip proxy router — mounted at the sentinel prefix that
# HostDispatcherMiddleware rewrites paths to. Never reachable directly
# from a normal browser URL: API Gateway never sets X-Isol8-Public-Host
# to a Paperclip domain unless the request actually arrived at one, and
# the prefix has a leading double-underscore that no real route uses.
#
# include_in_schema=False keeps the sentinel out of /openapi.json. The
# proxy is an internal dispatch implementation detail — exposing it in
# the public OpenAPI schema would (a) confuse SDK generators and (b)
# trip schemathesis contract fuzzing in CI (the fuzzer would hit the
# real proxy code which needs Paperclip infrastructure that isn't
# mocked at the contract layer).
app.include_router(
    paperclip_proxy.router,
    prefix=PAPERCLIP_PROXY_PREFIX,
    include_in_schema=False,
)

# Marketplace listings (Plan 2) — router carries its own /api/v1/marketplace prefix.
from routers import marketplace_listings  # noqa: E402

app.include_router(marketplace_listings.router)

# Marketplace purchases (Plan 2) — checkout, Stripe webhook, refunds.
from routers import marketplace_purchases  # noqa: E402

app.include_router(marketplace_purchases.router)

# Marketplace payouts (Plan 2) — Stripe Connect Express onboarding + dashboard.
from routers import marketplace_payouts  # noqa: E402

app.include_router(marketplace_payouts.router)

# Marketplace admin (Plan 2) — moderation queue, approve/reject, takedown.
from routers import marketplace_admin  # noqa: E402

app.include_router(marketplace_admin.router)

# Marketplace seller profiles — handle picker + public profile lookups.
from routers import marketplace_sellers  # noqa: E402

app.include_router(marketplace_sellers.router)


@app.get(
    "/",
    summary="API root",
    description="Returns a welcome message. Useful for verifying the API is reachable.",
    operation_id="root",
    tags=["health"],
)
async def root():
    return {"message": "Welcome to Isol8 API"}


@app.get(
    "/health",
    summary="Health check",
    description=(
        "Cheap, local liveness check for the ALB. Returns HTTP 200 once startup has "
        "completed, HTTP 503 while still booting. Deliberately does NOT call DynamoDB or "
        "any upstream, so liveness is not tied to external availability."
    ),
    operation_id="health_check",
    tags=["health"],
    responses={
        503: {"description": "Still starting up"},
    },
)
async def health_check():
    """ALB liveness — cheap + local. 200 once startup completed; 503 while still
    booting so ALB doesn't route to a not-ready task. Deliberately does NOT call
    DynamoDB or any upstream (that would tie liveness to external availability and
    flap the task)."""
    if not _started:
        return JSONResponse(status_code=503, content={"status": "starting"})
    return {"status": "healthy"}


@app.get(
    "/protected",
    summary="Test authentication",
    description="Returns the authenticated user's ID. Useful for verifying that Clerk JWT authentication is working.",
    operation_id="protected_route",
    tags=["health"],
    responses={
        401: {"description": "Missing or invalid Clerk JWT token"},
    },
)
async def protected_route(auth=Depends(get_current_user)):
    return {"message": "You are authenticated", "user_id": auth.user_id}
