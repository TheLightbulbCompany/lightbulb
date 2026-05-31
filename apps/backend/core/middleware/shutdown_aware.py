"""Process-wide graceful-shutdown gate.

During lifespan shutdown we set a process-global flag; this middleware then
returns 503 to NEW business requests (so the ALB/clients stop sending work)
while letting in-flight requests finish. The health check is whitelisted so
the task stays "healthy" and is drained via ECS deregistration, not flapped
out as unhealthy. The flag is a module-level bool (process-global state), not
a ContextVar (which is request-scoped).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_shutting_down = False

# Paths that MUST keep working during drain (ALB health check).
SHUTDOWN_WHITELIST = {"/health"}


def set_shutting_down(value: bool) -> None:
    global _shutting_down
    _shutting_down = value


def is_shutting_down() -> bool:
    return _shutting_down


class ShutdownAwareMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _shutting_down and request.url.path not in SHUTDOWN_WHITELIST:
            return JSONResponse(status_code=503, content={"detail": "Service shutting down"})
        return await call_next(request)
