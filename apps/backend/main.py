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
    get_ecs_manager,
    get_gateway_pool,
    startup_containers,
    shutdown_containers,
)
from core.observability.e2e_correlation import E2ECorrelationMiddleware
from core.repositories import container_repo
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.update_service import run_scheduled_worker
from routers import (
    billing,
    channels,
    config,
    container,
    container_recover,
    container_rpc,
    control_ui_proxy,
    debug,
    desktop_auth,
    hooks_credentials,
    hooks_proxy,
    integrations,
    oauth,
    orgs,
    paperclip_proxy,
    settings_keys,
    updates,
    users,
    webhooks,
    websocket_chat,
    workspace_files,
)
from routers.teams import router as teams_router
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


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
    plus a small allow-list of local-dev hostnames so ``./scripts/local-dev.sh``
    works without extra config.
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


async def _resume_provisioning_transitions() -> None:
    """Resume the provisioning -> running poller for any containers that were
    mid-transition when the backend last shut down.

    ``_await_running_transition`` is an in-process asyncio task — a deploy or
    crash kills it mid-poll. DDB ``status="provisioning"`` is the durable
    marker that the transition hasn't completed; on startup we find those
    rows and re-kick the poller. The poller itself is idempotent (if the
    container is already healthy, the first iteration transitions it to
    running and exits).
    """
    try:
        rows = await container_repo.get_by_status("provisioning")
    except Exception:
        logger.warning("Could not resume provisioning transitions on startup", exc_info=True)
        return

    ecs = get_ecs_manager()
    for row in rows:
        owner_id = row["owner_id"]
        asyncio.create_task(ecs._await_running_transition(owner_id))
        logger.info("Resumed provisioning -> running poller for %s", owner_id)


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

        # Per-user dependencies share the existing helpers from agents.py.
        from routers.teams.agents import _admin, _resolve_user_email

        async def resolve_company_id(user_id: str) -> str:
            company = await repo.get(user_id)
            if company is None:
                raise RuntimeError(f"no paperclip company for user {user_id}")
            return company.company_id

        async def resolve_session_cookie(user_id: str) -> str:
            return await get_user_session_cookie(
                user_id=user_id,
                repo=repo,
                admin_client=_admin(),
                clerk_email_resolver=_resolve_user_email,
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
    await startup_containers()
    await _resume_provisioning_transitions()
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

    yield

    # Shutdown
    logger.info("Shutting down application...")
    system_health.BACKGROUND_TASKS.clear()
    gauge_task.cancel()
    worker_task.cancel()
    try:
        await gauge_task
    except asyncio.CancelledError:
        pass
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    if broker is not None:
        from core.services import teams_event_broker_singleton

        await broker.shutdown()
        teams_event_broker_singleton.set_broker(None)

    await shutdown_containers()


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
        "name": "control-ui",
        "description": "Proxy for the embedded OpenClaw control UI SPA.",
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
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
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
    status = exc.status_code if 400 <= exc.status_code < 600 else 502
    return JSONResponse(
        status_code=status,
        content={"detail": str(exc), "upstream_status": exc.status_code},
    )


# Request-ID middleware — generates/propagates X-Request-ID for log correlation.
# Added first so it runs innermost (after CORS and ProxyHeaders).
from core.observability.middleware import RequestContextMiddleware

app.add_middleware(RequestContextMiddleware)

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
# Out of scope for this commit; the route-level fix uses request.client.host
# directly so a future ALB-side change immediately closes the gap.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=["10.0.0.0/16", "127.0.0.1", "::1"],
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-E2E-Run-Id"],
)

# E2E correlation middleware — binds X-E2E-Run-Id header to log context so
# every log line emitted while handling an e2e harness request carries the
# same e2e_run_id field. No-op for traffic that doesn't send the header.
app.add_middleware(E2ECorrelationMiddleware)

# Admin metrics middleware (CEO O1) — emits admin_api.* CloudWatch metrics
# for every /api/v1/admin/* request. No-op for non-admin paths.
from core.middleware.admin_metrics import AdminMetricsMiddleware  # noqa: E402

app.add_middleware(AdminMetricsMiddleware)


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

# Clerk webhook events (user lifecycle)
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])

# WebSocket routes (API Gateway WebSocket -> HTTP POST)
app.include_router(websocket_chat.router, prefix="/api/v1/ws")

# Billing routes
app.include_router(billing.router, prefix="/api/v1/billing", tags=["billing"])

# Organization management (Gate A — invite-creation tenancy invariant)
app.include_router(orgs.router, prefix="/api/v1/orgs", tags=["orgs"])

# Container lifecycle management (GET /status, POST /retry)
app.include_router(container.router, prefix="/api/v1/container", tags=["container"])

# Container RPC proxy & file uploads (POST /rpc, POST /gateway/restart, POST /files)
app.include_router(container_rpc.router, prefix="/api/v1/container", tags=["container"])

# Container recovery (state-aware recover endpoint)
app.include_router(container_recover.router, prefix="/api/v1/container", tags=["container"])

# Container updates (pending updates, apply/schedule)
app.include_router(updates.router, prefix=f"{settings.API_V1_STR}/container", tags=["container"])

# Control UI proxy (embedded OpenClaw control UI SPA)
app.include_router(control_ui_proxy.router, prefix="/api/v1/control-ui", tags=["control-ui"])

# Channel management (Telegram, Discord, WhatsApp)
app.include_router(channels.router, prefix="/api/v1/channels", tags=["channels"])

# Config patching (unified EFS write endpoint)
app.include_router(config.router, prefix="/api/v1/config", tags=["config"])

app.include_router(settings_keys.router, prefix="/api/v1/settings/keys", tags=["settings"])

# Integrations (MCP servers, ClawHub)
app.include_router(integrations.router, prefix="/api/v1", tags=["integrations"])

# Workspace file browser (EFS agent workspace)
app.include_router(workspace_files.router, prefix="/api/v1/container", tags=["container"])

# Debug routes (dev-only container provisioning)
app.include_router(debug.router, prefix="/api/v1/debug", tags=["debug"])

app.include_router(desktop_auth.router, prefix="/api/v1/auth", tags=["desktop"])

# ChatGPT OAuth (device-code flow for BYO ChatGPT-token billing)
app.include_router(oauth.router, prefix="/api/v1")

# Public webhook ingress proxy. Bearer-token auth (NOT Clerk) — external
# senders (Zapier/Stripe/GitHub) hit /api/v1/hooks/u/{owner_id}/{hook_path}
# and we forward to the per-user OpenClaw container. See routers/hooks_proxy.py
# for the auth model and forwarded-header rewriting rationale.
app.include_router(hooks_proxy.router, prefix="/api/v1/hooks", tags=["hooks"])

# Clerk-authed read of the calling user's hook credentials. Backs the
# Settings → Webhooks panel; the user's own browser fetches their own
# webhook URL + token. See routers/hooks_credentials.py.
app.include_router(hooks_credentials.router, prefix="/api/v1/hooks", tags=["hooks"])

# Admin dashboard (#351) — every endpoint gated by Depends(require_platform_admin).
from routers import admin as admin_router  # noqa: E402

app.include_router(admin_router.router, prefix="/api/v1")

# Teams BFF — native UI for Paperclip (spec 2026-05-02). The router itself
# carries the /teams prefix; mounting under /api/v1 yields /api/v1/teams/*.
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

# Marketplace purchases (Plan 2) — checkout, Stripe webhook, refunds.
from routers import marketplace_purchases  # noqa: E402

app.include_router(marketplace_purchases.router)

# Marketplace payouts (Plan 2) — Stripe Connect Express onboarding + dashboard.
from routers import marketplace_payouts  # noqa: E402

app.include_router(marketplace_payouts.router)

# Marketplace admin (Plan 2) — moderation queue, approve/reject, takedown.
from routers import marketplace_admin  # noqa: E402

app.include_router(marketplace_admin.router)

# Seller identity (handle claim + cooldown). Router carries its own
# /api/v1/sellers prefix. The publish wizard preflights against
# GET /sellers/me to decide whether to show the ClaimUsernameStep.
from routers import sellers  # noqa: E402

app.include_router(sellers.router)


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
        "Validates DynamoDB connectivity. Returns HTTP 200 when healthy, HTTP 503 when unhealthy. "
        "Used by ALB health checks to determine whether to route traffic to this instance."
    ),
    operation_id="health_check",
    tags=["health"],
    responses={
        503: {"description": "DynamoDB connection failed"},
    },
)
async def health_check():
    """Health check for ALB — validates DynamoDB connectivity."""
    try:
        from core.dynamodb import get_table, run_in_thread

        table = get_table("users")
        await run_in_thread(table.load)  # DescribeTable API call
        return {"status": "healthy", "database": "dynamodb"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(status_code=503, content={"status": "unhealthy", "error": str(e)})


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
