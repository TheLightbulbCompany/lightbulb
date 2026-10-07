"""Bind X-E2E-Run-Id header to per-request log context.

When the e2e harness issues a request with X-E2E-Run-Id, every log line
emitted while handling that request includes the same `e2e_run_id` field.
This lets ops grep CloudWatch by a single ID and see the full trace of
a failed e2e run.
"""

import contextvars
import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Per-request context. Reset to None at request boundaries so requests
# without the header don't inherit the previous request's run_id.
_e2e_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("e2e_run_id", default=None)

# Module-level flag so we only wrap the LogRecord factory once, even if
# the middleware is instantiated multiple times (e.g. across tests).
_factory_installed = False


def _install_log_record_factory() -> None:
    """Wrap the active LogRecord factory to stamp e2e_run_id on every record.

    We use a factory (not a Filter on the root logger) because filters
    attached to a logger are only consulted for that logger's own handlers
    — records that propagate to ancestors or are captured by alternate
    handlers (e.g. pytest's caplog) would otherwise skip the filter. The
    factory runs at record creation, so the attribute is present on the
    record everywhere downstream.
    """
    global _factory_installed
    if _factory_installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        run_id = _e2e_run_id_var.get()
        if run_id is not None:
            record.e2e_run_id = run_id
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class E2ECorrelationMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware: read X-E2E-Run-Id header, bind to log context.

    The contextvar is the binding mechanism so async work spawned inside
    the request handler inherits the value automatically.
    """

    def __init__(self, app):
        super().__init__(app)
        _install_log_record_factory()

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        run_id = request.headers.get("X-E2E-Run-Id")
        token = _e2e_run_id_var.set(run_id) if run_id else None
        try:
            return await call_next(request)
        finally:
            if token is not None:
                _e2e_run_id_var.reset(token)
