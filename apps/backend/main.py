# Load environment variables FIRST, before any other imports
from dotenv import load_dotenv

load_dotenv()

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from core.observability.logging import configure_logging

configure_logging(level="INFO")

from fastapi import Depends, FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse

from core.auth import get_current_user
from core.config import settings
from core.observability.e2e_correlation import E2ECorrelationMiddleware
from core.repositories import container_repo
from core.services.messaging import ApiProblem
from core.services import hosted_mcp
from core.services.a2a_auth import READ_SCOPE_A2A, RESOURCE_A2A, SEND_SCOPE
from core.services.hosted_mcp import READ_SCOPE, RESOURCE, WRITE_SCOPE
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.update_service import run_scheduled_worker
from routers import (
    agent_mail,
    agent_text,
    apple_iap,
    automations,
    billing,
    config,
    connections,
    container,
    container_files,
    container_recover,
    debug,
    desktop_auth,
    desktop_pair,
    devices,
    gateway_relay,
    huddles,
    desktop_execution,
    desktop_sessions,
    desktop_session_pickup,
    desktop_session_resume,
    google_accounts,
    integrations,
    launch,
    mac_cdp,
    meet_audio,
    meet_bot,
    meetings,
    messaging,
    meta_webhook,
    metronome_webhook,
    oauth,
    ops,
    orgs,
    paperclip_events,
    paperclip_proxy,
    paperclip_trigger_proxy,
    photon_inbound,
    platform_info,
    runtime_rpc,
    sendblue_inbound,
    ses_inbound,
    agent_keys as agent_keys_router,
    network as network_router,
    network_route as network_route_router,
    settings_keys,
    setup_requests,
    slack_app,
    support_tickets,
    users,
    webhooks,
    website_logins,
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


# LOAD-BEARING, do not delete (owner ruling 2026-09-24): Paperclip's Better Auth session on
# company[-env] depends on this dispatch -- removing it broke Better Auth before, low traffic or not.
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
    """Emit gateway.running.count every 60 s: the P12 backend-liveness canary expects a steady stream.

    P12 binds the fleet {env, service} identity only -- the emitting ECS task id is
    NOT a metric dimension: it is a new CloudWatch metric identity per task per
    deploy, the same unbounded-cardinality class core/observability/metrics.py's
    _DENIED_DIMENSIONS already refuses for container_id. It rides a structured log
    field instead, so a dead loop is still attributable in Logs Insights without
    minting a fresh metric identity on every deploy. This loop deliberately stays
    UNADMITTED (no pass-admission lease): it is the liveness canary, and a canary
    admitted once per window only proves that *some* instance is alive, not that
    this one is.

    Also piggybacks the OpenRouter account-balance check here (throttled to
    every ~15 min via a monotonic timestamp) rather than adding a second
    scheduler -- see core.services.openrouter_keys.check_account_balance --
    and the TeamsEventBroker idle-client sweep (release_idle).
    """
    from core.observability.metrics import gauge
    from core.services.openrouter_keys import check_account_balance

    instance = os.getenv("HOSTNAME") or "local"
    balance_check_interval_s = 900
    last_balance_check = time.monotonic()  # first check after 15 min of uptime, never at boot (tests boot the app)
    while True:
        try:
            rows = await container_repo.get_by_status("running")
            gauge("gateway.running.count", len(rows))
            logger.info("running_count", extra={"instance": instance, "count": len(rows)})
        except Exception:
            logger.warning("running_gauges loop iteration failed", exc_info=True)
        if time.monotonic() - last_balance_check >= balance_check_interval_s:
            last_balance_check = time.monotonic()
            await check_account_balance()
        try:
            from core.services.teams_event_broker import get_broker

            broker = get_broker()
            if broker is not None:
                await broker.release_idle()
        except Exception:
            logger.warning("teams broker idle sweep failed", exc_info=True)
        await asyncio.sleep(60)


async def _build_teams_event_broker():
    """Construct the singleton TeamsEventBroker used by /ws/message.

    Returns None when the dependencies aren't configured (e.g. local
    dev without Paperclip running) — the broker is best-effort and
    /teams continues to work via REST without realtime updates.
    """
    try:
        from core.repositories.paperclip_repo import PaperclipRepo
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

        from core.services.clerk_admin import get_primary_email
        from core.services.paperclip_admin_client import shared_admin_client

        async def resolve_company_id(owner_id: str) -> str:
            company = await repo.resolve_owner_row(owner_id)
            if company is None:
                raise RuntimeError(f"no active paperclip company for owner {owner_id}")
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

    from core.services.daily_digest import run_digest_sweeper
    from core.services.needs_you_email import run_needs_you_sweeper
    from core.services.durable_activity import run_trial_idle_sweeper
    from core.services.run_ledger import run_ledger_sweeper
    from core.services.browser_control import run_autosave_sweeper
    from core.services.session_pickup import run_pickup_sweeper
    from core.services.memory_api import run_memory_sweeper
    from core.services.voice_calls import voice_call_sweeper
    from core.services.apps_sweeper import run_apps_sweeper

    scheduled_worker_task = asyncio.create_task(run_scheduled_worker())
    idle_sweeper_stop = asyncio.Event()
    idle_sweeper_task = asyncio.create_task(run_trial_idle_sweeper(idle_sweeper_stop))
    run_sweeper_stop = asyncio.Event()
    run_sweeper_task = asyncio.create_task(run_ledger_sweeper(run_sweeper_stop))
    browser_autosave_stop = asyncio.Event()
    browser_autosave_task = asyncio.create_task(run_autosave_sweeper(browser_autosave_stop))
    voice_sweeper_stop = asyncio.Event()
    voice_sweeper_task = asyncio.create_task(voice_call_sweeper(voice_sweeper_stop))
    pickup_sweeper_stop = asyncio.Event()
    pickup_sweeper_task = asyncio.create_task(run_pickup_sweeper(pickup_sweeper_stop))
    memory_sweeper_stop = asyncio.Event()
    memory_sweeper_task = asyncio.create_task(run_memory_sweeper(memory_sweeper_stop))
    digest_stop = asyncio.Event()
    digest_task = asyncio.create_task(run_digest_sweeper(digest_stop))
    away_stop = asyncio.Event()
    away_task = asyncio.create_task(run_needs_you_sweeper(away_stop))
    apps_sweeper_stop = asyncio.Event()
    apps_sweeper_task = asyncio.create_task(run_apps_sweeper(apps_sweeper_stop))

    # Teams event broker — projects approved Paperclip events onto each user's
    # private AppSync channel. REST remains available when it is unconfigured.
    broker = await _build_teams_event_broker()
    if broker is not None:
        from core.services.teams_event_broker import set_broker

        set_broker(broker)

    gauge_task = asyncio.create_task(_running_count_gauge_loop())

    # Register background tasks so /admin/system/health can surface their state.
    from core.services import system_health

    system_health.BACKGROUND_TASKS.update(
        {
            "scheduled_worker": scheduled_worker_task,
            "trial_idle_sweeper": idle_sweeper_task,
            "run_ledger_sweeper": run_sweeper_task,
            "browser_autosave_sweeper": browser_autosave_task,
            "voice_call_sweeper": voice_sweeper_task,
            "session_pickup_sweeper": pickup_sweeper_task,
            "memory_sweeper": memory_sweeper_task,
            "daily_digest": digest_task,
            "needs_you_email": away_task,
            "apps_sweeper": apps_sweeper_task,
            "running_gauges": gauge_task,
        }
    )

    # Converge Paperclip's instance flags onto the repo's desired state, so a
    # deploy applies them. A one-shot, so it is NOT in BACKGROUND_TASKS (a
    # finished task reads "stopped" there). Best-effort: it never blocks or
    # fails startup. Deployed envs only: local and test have no cluster.
    flags_task = None
    if settings.ENVIRONMENT in ("dev", "prod"):
        from core.services.paperclip_instance_flags import converge_at_startup

        flags_task = asyncio.create_task(converge_at_startup(settings.ENVIRONMENT))

    # One MCP app + session manager PER lifespan entry (hosted_mcp.running): the
    # SDK's manager runs once per instance and the test client re-enters this lifespan.
    mcp_context = hosted_mcp.running()
    await mcp_context.__aenter__()

    # Temporal client (super-agent, dev only), connected in the background so startup never waits on it:
    # until it lands, or if it fails (bad key, Cloud outage), get_client() raises TemporalUnavailable and
    # every existing route still serves. temporalio is imported only in this branch.
    from core.services.super_agent_flags import env_enabled

    temporal_task = None
    if env_enabled():
        from core.temporal.client import connect_and_set

        temporal_task = asyncio.create_task(connect_and_set())

    # Startup complete — flip the liveness gate so /health returns 200.
    global _started
    _started = True

    yield

    # Shutdown — request every producer's stop before draining. (The openclaw-work
    # FIFO is drained by its own Lambda, scripts/work_lambda.py, not by this process.)
    logger.info("Shutting down application...")
    if temporal_task is not None:
        from core.temporal.client import set_client as set_temporal_client

        temporal_task.cancel()
        set_temporal_client(None)
    set_shutting_down(True)  # 503 new business requests; /health stays green for ALB drain
    from core.services.update_service import request_worker_stop

    request_worker_stop()
    idle_sweeper_stop.set()
    run_sweeper_stop.set()
    browser_autosave_stop.set()
    voice_sweeper_stop.set()
    pickup_sweeper_stop.set()
    memory_sweeper_stop.set()
    digest_stop.set()
    away_stop.set()
    apps_sweeper_stop.set()
    gauge_task.cancel()
    if flags_task is not None:
        flags_task.cancel()
    try:
        async with asyncio.timeout(20):
            # No provision-dispatch handoff: dispatch holds no per-owner
            # state (Phase 1 deleted the inline runner) — a roll mid-dispatch
            # just drops that poll's submit, and the next poll retries.
            cleanup = [
                scheduled_worker_task,
                idle_sweeper_task,
                run_sweeper_task,
                browser_autosave_task,
                voice_sweeper_task,
                pickup_sweeper_task,
                memory_sweeper_task,
                digest_task,
                away_task,
                apps_sweeper_task,
                gauge_task,
            ]
            if broker is not None:
                cleanup.append(broker.shutdown())
            await asyncio.gather(*cleanup, return_exceptions=True)
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown exceeded 20s budget; cancelling process-local workers")
        for task in (
            scheduled_worker_task,
            idle_sweeper_task,
            run_sweeper_task,
            browser_autosave_task,
            voice_sweeper_task,
            pickup_sweeper_task,
            memory_sweeper_task,
            digest_task,
            away_task,
            apps_sweeper_task,
            gauge_task,
        ):
            task.cancel()
        await asyncio.gather(
            scheduled_worker_task,
            idle_sweeper_task,
            run_sweeper_task,
            browser_autosave_task,
            voice_sweeper_task,
            pickup_sweeper_task,
            memory_sweeper_task,
            digest_task,
            away_task,
            apps_sweeper_task,
            gauge_task,
            return_exceptions=True,
        )
    finally:
        await mcp_context.__aexit__(None, None, None)
        system_health.BACKGROUND_TASKS.clear()
        if broker is not None:
            from core.services.teams_event_broker import set_broker

            set_broker(None)


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
        "name": "integrations",
        "description": "MCP server and ClawHub integration management.",
    },
    {
        "name": "messaging",
        "description": "Canonical multiplayer rooms, messages, and runtime commands.",
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
    title="Lightbulb API",
    description=(
        "AI agent platform powered by OpenClaw. Personal agents with persistent memory and streaming LLM inference."
    ),
    version="2.0.0",
    # Schema off in prod like /docs — nothing consumes it there, and it maps
    # the full route inventory (admin/debug/webhook shapes) for free.
    openapi_url=None if settings.ENVIRONMENT == "prod" else f"{settings.API_V1_STR}/openapi.json",
    docs_url=None if settings.ENVIRONMENT == "prod" else "/docs",
    redoc_url=None if settings.ENVIRONMENT == "prod" else "/redoc",
    # PUBLIC_API_BASE is the public origin of THIS API, wired per env by CDK
    # (https://api-dev.<apex> / https://api.<apex>, localhost otherwise).
    servers=[
        {
            "url": settings.PUBLIC_API_BASE,
            "description": {"dev": "Development", "prod": "Production"}.get(settings.ENVIRONMENT, "Local development"),
        }
    ],
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)


@app.exception_handler(ApiProblem)
async def messaging_api_problem_handler(request, exc: ApiProblem):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.exception_handler(RequestValidationError)
async def messaging_validation_error_handler(request, exc: RequestValidationError):
    # website-logins too: FastAPI's default 422 echoes the rejected input -- a password.
    if request.url.path.startswith(
        (
            "/api/v1/messaging",
            "/api/v1/runtime",
            "/api/v1/internal/messaging",
            "/api/v1/website-logins",
            # huddles too: a rejected turn would echo a live transcript.
            "/api/v1/internal/huddles",
            # session links: a rejected body carries a link token or a PKCE verifier.
            "/api/v1/public",
            "/api/v1/session-links",
        )
    ):
        problem = ApiProblem("INVALID_REQUEST", "Request validation failed.")
        return JSONResponse(status_code=problem.status_code, content=problem.body())
    if request.url.path.startswith("/api/v1/sessions"):
        # uploaded sessions: FastAPI's default 422 would echo tool-call summaries.
        return JSONResponse(status_code=422, content={"detail": "Invalid sessions upload."})
    return await request_validation_exception_handler(request, exc)


# PaperclipApiError handler — translate upstream Paperclip non-2xx responses into
# matching HTTPExceptions so the BFF returns a faithful status to the browser
# instead of a generic 500. ``PaperclipApiError`` is raised by every method on
# ``PaperclipAdminClient`` (see ``_post`` / ``_get`` / ``_patch`` / ``_delete``)
# whenever Paperclip responds with a non-2xx; without this handler the Teams
# BFF (``routers/teams/*.py``) would propagate it as an unhandled exception
# and FastAPI would return 500 with a useless body. We forward Paperclip's
# original status code (e.g. 403 / 404) under ``upstream_status`` so panel-side
# error handling has something to branch on, and ``exc.message`` — NOT
# ``str(exc)``, which now carries the upstream response body — as the detail, so
# an upstream error payload never reaches the browser. The ``502`` fallback is
# for the (currently-impossible) case where ``status_code`` is outside the
# standard HTTP range — we can't legitimately return that to the client, so map
# to "bad gateway" which is exactly what an out-of-range upstream status implies.
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
                content={"detail": exc.message, "upstream_status": exc.status_code},
            )
        return refresh

    status = exc.status_code if 400 <= exc.status_code < 600 else 502
    if status >= 500:
        # str(exc) is "GET /api/companies/<company_uuid>/... -> 5xx: <body>" — it
        # embeds the internal Paperclip path + company UUID, and an upstream 5xx
        # carries nothing client-actionable. Log the real detail server-side
        # (the body is the only place upstream says why) and return a
        # generic body so we don't leak backend architecture to the caller.
        logger.warning("paperclip upstream error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"detail": "upstream error", "upstream_status": 502},
        )
    return JSONResponse(
        status_code=status,
        content={"detail": _paperclip_reason(exc), "upstream_status": exc.status_code},
    )


def _paperclip_reason(exc: PaperclipApiError) -> str:
    """Paperclip's own user-facing 4xx reason ("Issue is blocked by unresolved blockers",
    "Missing permission: tasks:assign.") from its ``{"error": ...}`` body; ``exc.message``
    only says WHICH call failed (Paperclip audit 2026-09-24 F5). 5xx never reaches here."""
    try:
        body = exc.body if isinstance(exc.body, dict) else json.loads(exc.body or "{}")
        reason = body.get("error")
    except (ValueError, AttributeError):
        reason = None
    return reason[:300] if isinstance(reason, str) and reason.strip() else exc.message


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
# letting CORS/E2E still see all traffic uniformly.
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
# XFF is walked from the RIGHT (uvicorn >= 0.31): the client host is the first entry that is
# not one of the trusted hosts below, so a caller-supplied leftmost XFF entry is never promoted.
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
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "X-E2E-Run-Id",
        "Mcp-Session-Id",
        "Mcp-Protocol-Version",
    ],
    # Expose X-Request-ID (set on every response by RequestContextMiddleware) so
    # cross-origin browser JS can read it. Without this, response.headers.get(
    # "X-Request-ID") is null cross-origin (it works same-origin, which masks the
    # gap in local dev). The frontend attaches recent request IDs to support
    # tickets for log correlation. Named explicitly rather than "*" as the
    # mode-agnostic, future-proof choice — unrelated to credentials.
    expose_headers=["X-Request-ID", "Mcp-Session-Id", "WWW-Authenticate"],
)

# R78/R83: app origins may call the invite swap only (no credentials); added after CORSMiddleware so it runs first.
from routers.apps import InviteCors  # noqa: E402

app.add_middleware(InviteCors)

# E2E correlation middleware — binds X-E2E-Run-Id header to log context so
# every log line emitted while handling an e2e harness request carries the
# same e2e_run_id field. No-op for traffic that doesn't send the header.
app.add_middleware(E2ECorrelationMiddleware)

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
app.include_router(sendblue_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(photon_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(ses_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# Paperclip isol8.notifications plugin events (approvals + run lifecycle →
# push notifications). Bearer-secret auth; disabled when the secret is unset.
app.include_router(paperclip_events.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# Machine-only customer lifecycle feed + idempotent send ledger. Uses the
# Paperclip service bearer credential and never impersonates an admin user.
from routers import lifecycle_operations  # noqa: E402

app.include_router(lifecycle_operations.router, prefix="/api/v1/lifecycle", tags=["lifecycle"])

# WebSocket routes (API Gateway WebSocket -> HTTP POST)
app.include_router(websocket_chat.router, prefix="/api/v1/ws")

# Browser/mobile canonical messaging and the guarded HTTP runtime bridge.
app.include_router(messaging.router, prefix="/api/v1", tags=["messaging"])
app.include_router(runtime_rpc.router, prefix="/api/v1/runtime", tags=["messaging"])

# Raw WebSocket relay for machine clients (desktop node hosts, Paperclip
# operators) — a direct browser-facing WS door on the ALB, not the API Gateway
# WebSocket path.
app.include_router(gateway_relay.router, prefix="/api/v1/gateway", tags=["gateway-relay"])
# Recall's per-person Meet audio: the same door, since it is the only raw-WS path the bv- edge forwards.
app.include_router(meet_audio.router, prefix="/api/v1/gateway", tags=["meetings"])
app.include_router(desktop_execution.router, prefix="/api/v1/desktop-execution", tags=["desktop"])
app.include_router(desktop_sessions.router, prefix="/api/v1", tags=["desktop"])
app.include_router(desktop_session_pickup.router, prefix="/api/v1", tags=["desktop"])
app.include_router(desktop_session_resume.router, prefix="/api/v1", tags=["desktop"])

# Billing routes
app.include_router(billing.router, prefix="/api/v1/billing", tags=["billing"])
app.include_router(huddles.router, prefix="/api/v1", tags=["huddles"])
app.include_router(meetings.router, prefix="/api/v1", tags=["meetings"])
app.include_router(meet_bot.router, prefix="/api/v1", tags=["meetings"])

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

# Container lifecycle management (GET /status, POST /retry)
app.include_router(container.router, prefix="/api/v1/container", tags=["container"])

# AgentCore Browser live view (GET /browser/live-view) -- lives in routers/container.py but
# mounts on its own prefix, since the panel's URL is /api/v1/browser/live-view.
app.include_router(container.browser_router, prefix="/api/v1/browser", tags=["browser"])
app.include_router(container.browser_internal_router, prefix="/api/v1/internal/browser", tags=["browser"])
app.include_router(container.provision_events_router, prefix="/api/v1/internal/provisioning", tags=["container"])
from routers import paperclip_cost as paperclip_cost_router  # noqa: E402

app.include_router(paperclip_cost_router.router, prefix="/api/v1/internal/paperclip", tags=["paperclip"])
app.include_router(mac_cdp.router, prefix="/api/v1/internal/mac-cdp", tags=["mac-cdp"])

# Workspace website logins (the store behind every agent's browser vault)
app.include_router(website_logins.router, prefix="/api/v1/website-logins", tags=["browser"])
app.include_router(website_logins.internal_router, prefix="/api/v1/internal/website-logins", tags=["browser"])

# Workspace file uploads (POST /files)
app.include_router(container_files.router, prefix="/api/v1/container", tags=["container"])

# Container recovery (POST /recover)
app.include_router(container_recover.router, prefix="/api/v1/container", tags=["container"])

# Config read/patch over the owner's roster profiles
app.include_router(config.router, prefix="/api/v1/config", tags=["config"])

app.include_router(agent_keys_router.router, prefix="/api/v1/settings/agent-keys", tags=["settings"])
app.include_router(settings_keys.router, prefix="/api/v1/settings/keys", tags=["settings"])
# Under /internal/messaging: the bridge's `_post_backend` prefixes every callback with it.
app.include_router(network_router.internal_router, prefix="/api/v1/internal/messaging/network", tags=["settings"])
# `lightbulb login`/`logout`/`invite --agents` and the doorbell. The CLI's routes carry no Clerk
# token (a signed challenge, or the doorbell's bearer, is the credential); the confirm is Clerk.
app.include_router(network_router.router, prefix="/api/v1/network", tags=["network"])
# Hosted apps on the free network: the computer route link (on the bv door's /gateway/ path),
# its cross-task door, the wake-up page and the bridge's app-mail notice.
app.include_router(network_route_router.router, prefix="/api/v1", tags=["network"])

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

# Workspace file browser (the agent's S3 Hermes profile)
app.include_router(workspace_files.router, prefix="/api/v1/container", tags=["container"])

# Debug routes (dev-only container provisioning)
app.include_router(debug.router, prefix="/api/v1/debug", tags=["debug"])

app.include_router(desktop_auth.router, prefix="/api/v1/auth", tags=["desktop"])

# Desktop two-node pair / revoke (two-node architecture). Pair is idempotent;
# revoke force-closes the live WS session keyed by deviceId.
app.include_router(desktop_pair.router, prefix="/api/v1", tags=["desktop"])

# ChatGPT OAuth (device-code flow for BYO ChatGPT-token billing)
app.include_router(oauth.router, prefix="/api/v1")

# Tier-2 ingress: signed Paperclip routine-trigger fires at
# /api/v1/hooks/t/{owner_id}/{public_id}. No edge auth — Paperclip verifies
# the per-trigger signature upstream. See routers/paperclip_trigger_proxy.py.
app.include_router(paperclip_trigger_proxy.router, prefix="/api/v1/hooks", tags=["hooks"])
# App-LEVEL Meta ingress at /api/v1/hooks/meta — deliberately ownerless, because
# a Meta app has ONE callback URL for every user. Routed by the asset id in the
# payload, authenticated by the app-secret signature. See routers/meta_webhook.py.
app.include_router(meta_webhook.router, prefix="/api/v1/hooks", tags=["hooks"])
# The platform Slack app: one Request URL for every workspace (events,
# interactivity, slash) + the OAuth install rail. See routers/slack_app.py.
app.include_router(slack_app.router, prefix="/api/v1/slack", tags=["slack"])
# Human operator diagnostics; every route requires a Clerk platform admin.
app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])
app.include_router(launch.router, prefix="/api/v1/launch", tags=["launch"])
# Public /request-setup form (unauthenticated, rate-limited per IP).
app.include_router(setup_requests.router, prefix="/api/v1/setup-requests", tags=["setup-requests"])

# Public connectable catalog (integrations + channels) read live by the baked
# isol8 skill from inside per-owner containers. No auth — non-sensitive catalog
# data. See routers/platform_info.py.
app.include_router(platform_info.router, prefix="/api/v1/platform-info", tags=["platform-info"])

# Agent mail (container routes under /u/{owner_id}/… use hook-token auth;
# /addresses routes use Clerk). Policy lives in core/services/agent_mail_send.
app.include_router(agent_mail.router, prefix="/api/v1/agent-mail", tags=["agent-mail"])
app.include_router(agent_text.router, prefix="/api/v1/agent-text", tags=["agent-text"])

# Admin dashboard — every endpoint gated by Depends(require_platform_admin).
from routers import admin as admin_router  # noqa: E402

app.include_router(admin_router.router, prefix="/api/v1")

# Teams BFF — native UI for Paperclip. The router itself carries the
# /teams prefix; mounting under /api/v1 yields /api/v1/teams/*.
app.include_router(teams_router, prefix="/api/v1")

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


# Claude Code context import — same /api/v1/marketplace prefix, own subtree.
from routers import marketplace_import  # noqa: E402

app.include_router(marketplace_import.router)


# Marketplace purchases (Plan 2) — checkout, Stripe webhook, refunds.
from routers import marketplace_purchases  # noqa: E402

app.include_router(marketplace_purchases.router)

# Marketplace payouts (Plan 2) — Stripe Connect Express onboarding + dashboard.
from routers import marketplace_payouts  # noqa: E402

app.include_router(marketplace_payouts.router)

# Company Stripe account — status, create + hosted onboarding, connect existing, disconnect.
from routers import company_stripe  # noqa: E402

app.include_router(company_stripe.router, prefix="/api/v1/company/stripe", tags=["company-stripe"])

# Provisioning rail (Stripe Projects) — Friday's ask routes, the owner's
# approvals, and the provisioned-resource surface in Settings.
from routers import company_provisioning  # noqa: E402

app.include_router(company_provisioning.router, prefix="/api/v1/company/provisioning", tags=["company-provisioning"])

# Apps on the provisioning rail: builders under /build, Canvas and the admin on the rest.
from routers import apps as apps_routes  # noqa: E402

app.include_router(apps_routes.router, prefix="/api/v1/apps", tags=["apps"])

# Capability policy + revoke-all (the P0d slice — row + surface only;
# enforcement in the rails is P1).
from routers import company_policy  # noqa: E402

app.include_router(company_policy.router, prefix="/api/v1/company", tags=["company-policy"])

# Stripe Link Agent Wallet (every route 404s until Link's OAuth client is configured).
from routers import company_link_wallet  # noqa: E402

app.include_router(company_link_wallet.router, prefix="/api/v1/company/link-wallet", tags=["company-link-wallet"])
app.include_router(
    company_link_wallet.internal_router, prefix="/api/v1/internal/link-wallet", tags=["company-link-wallet"]
)

# Money reads — the stored day rollup plus live Stripe payments/customers/products.
from routers import company_money  # noqa: E402

app.include_router(company_money.router, prefix="/api/v1/company", tags=["company-money"])

# The "paste your context" door: merges into the CEO's Hermes memories/USER.md.
from routers import company_context  # noqa: E402
from routers import company_slug  # noqa: E402

app.include_router(company_context.router, prefix="/api/v1/company", tags=["company-context"])
app.include_router(company_slug.router, prefix="/api/v1/company", tags=["company-slug"])

# Ideas: the catalog of agency-style jobs, Start and dismiss (spec 2026-10-14 §4.4).
from routers import ideas as ideas_router  # noqa: E402

app.include_router(ideas_router.router, prefix="/api/v1", tags=["ideas"])

# Super-agent Questions (wave2-Q): /needs-you, /tasks/{id}/questions. Mounted before routers/tasks.py.
from routers import questions as questions_router  # noqa: E402

app.include_router(questions_router.router, prefix="/api/v1", tags=["questions"])

# Proactive rules (wave2-S): /automations/{id}/mode, /automations/{id}/matches. 404 unless the switch is on.
from routers import rules as rules_router  # noqa: E402

app.include_router(rules_router.router, prefix="/api/v1", tags=["rules"])

# A person reviews (lists, deletes) what the workspace's agents remember.
from routers import memory as memory_router  # noqa: E402

app.include_router(memory_router.router, prefix="/api/v1/memory", tags=["memory"])
app.include_router(memory_router.internal_router, prefix="/api/v1/internal/memory", tags=["memory"])

# Sessions a person uploads from the CLI, by choice (metadata or full).
from routers import sessions as sessions_router  # noqa: E402

app.include_router(sessions_router.router, prefix="/api/v1/sessions", tags=["sessions"])
app.include_router(sessions_router.shared_router, prefix="/api/v1/shared", tags=["sessions"])

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
    return {"message": "Welcome to Lightbulb API"}


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


@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def mcp_protected_resource_metadata():
    # The SDK gates basic read access; write is a separate consented tool scope.
    return {
        "resource": RESOURCE,
        "authorization_servers": [settings.CLERK_ISSUER],
        "scopes_supported": ["user:org:read", READ_SCOPE, WRITE_SCOPE],
        "bearer_methods_supported": ["header"],
    }


@app.get("/.well-known/oauth-protected-resource/a2a", include_in_schema=False)
async def a2a_protected_resource_metadata():
    return {
        "resource": RESOURCE_A2A,
        "authorization_servers": [settings.CLERK_ISSUER],
        "scopes_supported": ["user:org:read", READ_SCOPE_A2A, SEND_SCOPE],
        "bearer_methods_supported": ["header"],
    }


@app.get("/.well-known/openai-apps-challenge", include_in_schema=False)
async def openai_apps_challenge():
    if not settings.OPENAI_APPS_CHALLENGE_TOKEN:
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse(settings.OPENAI_APPS_CHALLENGE_TOKEN)


from routers import a2a  # noqa: E402

# The A2A door; before the MCP mount, which 404s every path it does not serve.
app.include_router(a2a.router)

# Keep this last: the SDK serves /mcp and its root well-known OAuth metadata.
app.mount("/", hosted_mcp.mount)
