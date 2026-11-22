"""The engine service's app (engine-service plan Task 8; not deployed yet): the engine's operations for products
under `/engine/v1` (`routers/engine_api.py`), and the agent runtime's callbacks that land on engine code (task runs,
Router proposals, messaging). It calls the products back over HTTP only (`core.engine.http_hooks`) and imports no
product code: same image as the backend, `uvicorn engine_main:app`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.engine import http_hooks
from core.services.messaging import ApiProblem
from routers import engine_api, internal_automations, messaging_internal, runtime_internal

http_hooks.register()  # engine code in this process asks the products over HTTP


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from core.temporal.client import connect_and_set, set_client

    temporal = asyncio.create_task(connect_and_set())  # in the background: until it lands, Temporal calls 503
    yield
    temporal.cancel()
    set_client(None)


app = FastAPI(title="Lightbulb engine", lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
app.include_router(engine_api.router, prefix="/engine/v1")
app.include_router(runtime_internal.router, prefix="/api/v1/internal/runtime")
app.include_router(internal_automations.router, prefix="/api/v1")
app.include_router(messaging_internal.router, prefix="/api/v1")


@app.exception_handler(ApiProblem)
async def api_problem_handler(_request, exc: ApiProblem):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """The runtime's messaging callbacks answer a bad body as the backend does (main.py)."""
    if request.url.path.startswith("/api/v1/internal/messaging"):
        problem = ApiProblem("INVALID_REQUEST", "Request validation failed.")
        return JSONResponse(status_code=problem.status_code, content=problem.body())
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
async def health():
    return {"status": "ok"}
