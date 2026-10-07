"""FastAPI middleware for X-Request-ID injection.

Generates a unique request ID for every inbound request (or uses the
one from the X-Request-ID header if the caller provides it, e.g., from
the ALB or API Gateway). Binds it to the contextvar so all downstream
log lines and metric calls carry the same ID.
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.observability.logging import bind_request_context

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Injects X-Request-ID into the request context and response headers."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        bind_request_context(request_id)

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
