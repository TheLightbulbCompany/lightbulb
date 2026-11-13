# Load environment variables FIRST, before any other imports
from dotenv import load_dotenv

load_dotenv()

import asyncio
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
from core.services.update_service import run_scheduled_worker
from routers import (
    agent_mail,
    agent_text,
    apple_iap,
    automations,
    internal_automations,
    voice_meet,
    voice_owner,
    voice_phone,
    billing,
    config,
    connections,
    container,
    debug,
    desktop_auth,
    desktop_pair,
    devices,
    gateway_relay,
    hooks,
    huddles,
    desktop_execution,
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
    photon_inbound,
    platform_info,
    runtime_rpc,
    ses_inbound,
    agent_keys as agent_keys_router,
    network as network_router,
    network_route as network_route_router,
    setup_requests,
    support_tickets,
    users,
    webhooks,
    website_logins,
    workspace_files,
)
from routers.teams import router as teams_router
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Liveness gate for the ALB /health probe. Flipped True at the very end of the
# lifespan startup block (just before ``yield``). While False, /health returns
# 503 so the ALB does not route to a not-yet-ready task.
_started = False


class SecurityHeadersMiddleware:
    """Add baseline security response headers to every HTTP response.

    - ``X-Content-Type-Options: nosniff`` — stop MIME-sniffing of API responses.
    - ``X-Frame-Options: SAMEORIGIN`` — clickjacking guard.
    - ``Referrer-Policy: strict-origin-when-cross-origin`` — limit referrer leak.

    ``Strict-Transport-Security`` is set here too because the REST API path is
    ALB -> ECS with no CloudFront in front, and an ALB can't add headers to
    *forwarded* responses — so the app is the practical place to assert HSTS for
    api.isol8.co. (Browsers ignore HSTS over
    plain HTTP, so it's a no-op for local dev / internal health probes.)

    No Content-Security-Policy here: the Next.js frontend sets the app's CSP. Only adds a header
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
    scheduler -- see core.services.openrouter_keys.check_account_balance.
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
        await asyncio.sleep(60)


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
    from core.services.memory_api import run_memory_sweeper
    from core.services.voice_calls import voice_call_sweeper

    scheduled_worker_task = asyncio.create_task(run_scheduled_worker())
    idle_sweeper_stop = asyncio.Event()
    idle_sweeper_task = asyncio.create_task(run_trial_idle_sweeper(idle_sweeper_stop))
    run_sweeper_stop = asyncio.Event()
    run_sweeper_task = asyncio.create_task(run_ledger_sweeper(run_sweeper_stop))
    browser_autosave_stop = asyncio.Event()
    browser_autosave_task = asyncio.create_task(run_autosave_sweeper(browser_autosave_stop))
    voice_sweeper_stop = asyncio.Event()
    voice_sweeper_task = asyncio.create_task(voice_call_sweeper(voice_sweeper_stop))
    memory_sweeper_stop = asyncio.Event()
    memory_sweeper_task = asyncio.create_task(run_memory_sweeper(memory_sweeper_stop))
    digest_stop = asyncio.Event()
    digest_task = asyncio.create_task(run_digest_sweeper(digest_stop))
    away_stop = asyncio.Event()
    away_task = asyncio.create_task(run_needs_you_sweeper(away_stop))

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
            "memory_sweeper": memory_sweeper_task,
            "daily_digest": digest_task,
            "needs_you_email": away_task,
            "running_gauges": gauge_task,
        }
    )

    # One MCP app + session manager PER lifespan entry (hosted_mcp.running): the
    # SDK's manager runs once per instance and the test client re-enters this lifespan.
    mcp_context = hosted_mcp.running()
    await mcp_context.__aenter__()

    # Temporal client, connected in the background so startup never waits on it:
    # until it lands, or if it fails (bad key, Cloud outage), get_client() raises TemporalUnavailable and
    # every existing route still serves. temporalio is imported only in this branch.
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
    memory_sweeper_stop.set()
    digest_stop.set()
    away_stop.set()
    gauge_task.cancel()
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
                memory_sweeper_task,
                digest_task,
                away_task,
                gauge_task,
            ]
            await asyncio.gather(*cleanup, return_exceptions=True)
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown exceeded 20s budget; cancelling process-local workers")
        for task in (
            scheduled_worker_task,
            idle_sweeper_task,
            run_sweeper_task,
            browser_autosave_task,
            voice_sweeper_task,
            memory_sweeper_task,
            digest_task,
            away_task,
            gauge_task,
        ):
            task.cancel()
        await asyncio.gather(
            scheduled_worker_task,
            idle_sweeper_task,
            run_sweeper_task,
            browser_autosave_task,
            voice_sweeper_task,
            memory_sweeper_task,
            digest_task,
            away_task,
            gauge_task,
            return_exceptions=True,
        )
    finally:
        await mcp_context.__aexit__(None, None, None)
        system_health.BACKGROUND_TASKS.clear()


openapi_tags = [
    {
        "name": "users",
        "description": "User registration and sync.",
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
    if request.url.path.startswith(
        (
            "/api/v1/messaging",
            "/api/v1/runtime",
            "/api/v1/internal/messaging",
            # huddles too: a rejected turn would echo a live transcript.
            "/api/v1/internal/huddles",
            "/api/v1/internal/voice",
        )
    ):
        problem = ApiProblem("INVALID_REQUEST", "Request validation failed.")
        return JSONResponse(status_code=problem.status_code, content=problem.body())
    if request.url.path.startswith("/api/v1/sessions"):
        # uploaded sessions: FastAPI's default 422 would echo tool-call summaries.
        return JSONResponse(status_code=422, content={"detail": "Invalid sessions upload."})
    return await request_validation_exception_handler(request, exc)


# Request-ID middleware — generates/propagates X-Request-ID for log correlation.
# Added first so it runs innermost (after CORS and ProxyHeaders).
from core.observability.middleware import RequestContextMiddleware

app.add_middleware(RequestContextMiddleware)

# Shutdown-aware middleware — returns 503 to new business requests once the
# lifespan shutdown sets the process flag, while /health stays green for ALB
# drain. See core/middleware/shutdown_aware.py.
from core.middleware.shutdown_aware import ShutdownAwareMiddleware, set_shutting_down

app.add_middleware(ShutdownAwareMiddleware)

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
app.include_router(photon_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(ses_inbound.router, prefix="/api/v1/webhooks", tags=["webhooks"])

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
app.include_router(mac_cdp.router, prefix="/api/v1/internal/mac-cdp", tags=["mac-cdp"])

# Workspace website logins (the store behind every agent's browser vault)
app.include_router(website_logins.internal_router, prefix="/api/v1/internal/website-logins", tags=["browser"])

# Config read/patch over the owner's roster profiles
app.include_router(config.router, prefix="/api/v1/config", tags=["config"])

app.include_router(agent_keys_router.router, prefix="/api/v1/settings/agent-keys", tags=["settings"])
# Under /internal/messaging: the bridge's `_post_backend` prefixes every callback with it.
app.include_router(network_router.internal_router, prefix="/api/v1/internal/messaging/network", tags=["settings"])
# `lightbulb login`/`logout`/`invite --agents` and the doorbell. The CLI's routes carry no Clerk
# token (a signed challenge, or the doorbell's bearer, is the credential); the confirm is Clerk.
app.include_router(network_router.router, prefix="/api/v1/network", tags=["network"])
# Hosted apps on the free network: the computer route link (on the bv door's /gateway/ path),
# its cross-task door, the wake-up page and the bridge's app-mail notice.
app.include_router(network_route_router.router, prefix="/api/v1", tags=["network"])

# Connections (Bucket A paste-key integrations: list / connect / disconnect)
app.include_router(connections.router, prefix="/api/v1", tags=["connections"])
# Public OAuth callback surface (provider redirects the unauthenticated browser here)
app.include_router(connections.oauth_router, prefix="/api/v1", tags=["connections"])

# 1-click Automations trigger seeding (connect an app, pick an event, get a
# pre-filled webhook-triggered routine) + the Stripe guided-paste whsec_
# write. See routers/automations.py.
app.include_router(automations.router, prefix="/api/v1", tags=["automations"])
app.include_router(internal_automations.router, prefix="/api/v1", tags=["super-agent"])  # lane A: Router proposals
app.include_router(voice_phone.router, prefix="/api/v1", tags=["voice"])  # the phone receptionist worker
app.include_router(voice_meet.router, prefix="/api/v1", tags=["voice"])  # the meeting agent worker
app.include_router(voice_owner.router, prefix="/api/v1", tags=["voice"])  # owner mode: Live in the DM is the Router

from routers import runtime_internal  # noqa: E402

app.include_router(runtime_internal.router, prefix="/api/v1/internal/runtime", tags=["super-agent"])

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

# Webhook automation fires at /api/v1/hooks/t/{owner_id}/{public_id}: no edge auth, the
# backend verifies each delivery against its HOOK# row. See routers/hooks.py.
app.include_router(hooks.router, prefix="/api/v1/hooks", tags=["hooks"])
# App-LEVEL Meta ingress at /api/v1/hooks/meta — deliberately ownerless, because
# a Meta app has ONE callback URL for every user. Routed by the asset id in the
# payload, authenticated by the app-secret signature. See routers/meta_webhook.py.
app.include_router(meta_webhook.router, prefix="/api/v1/hooks", tags=["hooks"])
# Human operator diagnostics; every route requires a Clerk platform admin.
app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])
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

# Stripe Link Agent Wallet (every route 404s until Link's OAuth client is configured).
from routers import company_link_wallet  # noqa: E402

app.include_router(company_link_wallet.router, prefix="/api/v1/company/link-wallet", tags=["company-link-wallet"])
app.include_router(
    company_link_wallet.internal_router, prefix="/api/v1/internal/link-wallet", tags=["company-link-wallet"]
)

from routers import company_slug  # noqa: E402

app.include_router(company_slug.router, prefix="/api/v1/company", tags=["company-slug"])

# Super-agent Questions (wave2-Q): /needs-you, /tasks/{id}/questions. Mounted before routers/tasks.py.
from routers import questions as questions_router  # noqa: E402

app.include_router(questions_router.router, prefix="/api/v1", tags=["questions"])

# The owner's voice surfaces (spec 2026-11-07 §5): /voice/timeline, /voice/agent.
from routers import voice as voice_router  # noqa: E402

app.include_router(voice_router.router, prefix="/api/v1", tags=["voice"])

# Super-agent task API (wave2-O Task 8, C15). After questions so /tasks/{id}/questions stays theirs.
from routers import tasks as tasks_router  # noqa: E402

app.include_router(tasks_router.router, prefix="/api/v1", tags=["tasks"])

# A person reviews (lists, deletes) what the workspace's agents remember.
from routers import memory as memory_router  # noqa: E402

app.include_router(memory_router.router, prefix="/api/v1/memory", tags=["memory"])
app.include_router(memory_router.internal_router, prefix="/api/v1/internal/memory", tags=["memory"])

# Sessions a person uploads from the CLI, by choice (metadata or full).
from routers import sessions as sessions_router  # noqa: E402

app.include_router(sessions_router.router, prefix="/api/v1/sessions", tags=["sessions"])


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
