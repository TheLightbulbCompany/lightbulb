"""Tests for core/observability/middleware.py — request-id injection."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.observability.logging import request_id_var
from core.observability.middleware import REQUEST_ID_HEADER, RequestContextMiddleware


def _create_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/test")
    async def test_endpoint():
        return {"request_id": request_id_var.get()}

    return app


def test_generates_request_id_when_none_provided():
    """Middleware should generate a UUID when no header is sent."""
    client = TestClient(_create_app())
    resp = client.get("/test")
    assert resp.status_code == 200
    assert REQUEST_ID_HEADER in resp.headers
    rid = resp.headers[REQUEST_ID_HEADER]
    assert len(rid) == 36  # UUID format
    assert resp.json()["request_id"] == rid


def test_honors_incoming_request_id():
    """When X-Request-ID is provided, middleware uses it instead of generating."""
    client = TestClient(_create_app())
    resp = client.get("/test", headers={REQUEST_ID_HEADER: "my-custom-id"})
    assert resp.headers[REQUEST_ID_HEADER] == "my-custom-id"
    assert resp.json()["request_id"] == "my-custom-id"


def test_each_request_gets_unique_id():
    """Two requests without headers should get different IDs."""
    client = TestClient(_create_app())
    r1 = client.get("/test")
    r2 = client.get("/test")
    assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]
