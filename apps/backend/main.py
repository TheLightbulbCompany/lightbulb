# Load environment variables FIRST, before any other imports
from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
from contextlib import asynccontextmanager

from core.observability.logging import configure_logging

configure_logging(level="INFO")

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from core.auth import get_current_user
from core.config import settings
from routers import (
    admin,
    billing,
    ops,
    os as os_router,
    os_billing as os_billing_router,
    webhooks,
)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Starting application...")
    # A starting app instance is not shutting down. Resetting here (not only at
    # shutdown) keeps the flag scoped to this instance's lifecycle.
    set_shutting_down(False)

    from core.voice.os_voice import os_voice_sweeper
    from core.observability.spark_store_jobs import run as spark_store_jobs

    os_voice_stop = asyncio.Event()
    os_voice_task = asyncio.create_task(os_voice_sweeper(os_voice_stop))
    spark_store_stop = asyncio.Event()
    spark_store_task = asyncio.create_task(spark_store_jobs(spark_store_stop))

    # Startup complete — flip the liveness gate so /health returns 200.
    global _started
    _started = True

    yield

    logger.info("Shutting down application...")
    set_shutting_down(True)  # 503 new business requests; /health stays green for ALB drain
    os_voice_stop.set()
    spark_store_stop.set()
    try:
        async with asyncio.timeout(20):
            await asyncio.gather(os_voice_task, spark_store_task, return_exceptions=True)
    except asyncio.TimeoutError:
        logger.error("Graceful shutdown exceeded 20s budget; cancelling process-local workers")
        os_voice_task.cancel()
        spark_store_task.cancel()
        await asyncio.gather(os_voice_task, spark_store_task, return_exceptions=True)


openapi_tags = [
    {
        "name": "health",
        "description": "Root and health check endpoints for verifying API availability.",
    },
]

app = FastAPI(
    title="Lightbulb API",
    description=("Backend for Spark, the Mac voice assistant."),
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
    ],
    # Expose X-Request-ID (set on every response by RequestContextMiddleware) so
    # cross-origin browser JS can read it. Without this, response.headers.get(
    # "X-Request-ID") is null cross-origin (it works same-origin, which masks the
    # gap in local dev). The frontend attaches recent request IDs to support
    # tickets for log correlation. Named explicitly rather than "*" as the
    # mode-agnostic, future-proof choice — unrelated to credentials.
    expose_headers=["X-Request-ID", "WWW-Authenticate"],
)

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
app.include_router(os_router.router, prefix="/api/v1/os", tags=["os"])
app.include_router(os_billing_router.router, prefix="/api/v1/os", tags=["os"])
# Clerk webhook (user.deleted ends the person's Spark account)
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
# Stripe webhook (Spark subscriptions) at /api/v1/billing/webhooks/stripe
app.include_router(billing.router, prefix="/api/v1/billing", tags=["billing"])
# Platform-admin console: /admin/me and the Spark metrics tab (/ops/spark).
app.include_router(admin.router, prefix="/api/v1")
app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])


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
