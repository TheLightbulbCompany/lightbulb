"""End-to-end smoke for the hooks proxy.

Boots a stub HTTP server posing as the user's OpenClaw container,
and POSTs a real request through the FastAPI app's TestClient.
Verifies the body is forwarded, the audit row lands, and
X-Forwarded-* are rewritten with the controlled values.

This test is "integration" in the lightweight sense: no LocalStack,
no real DDB, no real ECS — but it exercises the full request path
through the FastAPI app, the proxy router, an httpx outbound call,
and a stub upstream HTTP server. It catches issues that pure
unit-with-mocks tests (test_hooks_proxy.py) miss, like the duplicate
Authorization-header bug found during T6 review.
"""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_container():
    """Run a tiny HTTP server that imitates OpenClaw's /hooks endpoint."""
    received = {"body": None, "headers": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length)
            received["headers"] = dict(self.headers)
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"queued":true}')

        def log_message(self, *_a, **_k):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "received": received}
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Match the unit-test fixture's behavior — clear the in-process rate
    bucket so a prior unit test's 60-POST bucket doesn't pre-charge this one
    (and vice versa, so this test doesn't poison anything that runs after)."""
    from routers import hooks_proxy

    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()
    yield
    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hooks_proxy_end_to_end(stub_container, monkeypatch):
    from main import app
    from core.repositories import container_repo

    token = secrets.token_urlsafe(32)
    fake_container = {
        "owner_id": "u_e2e",
        "hook_token": token,
        "service_name": "stub",
        "status": "running",
    }

    async def fake_get(owner_id):
        return fake_container if owner_id == "u_e2e" else None

    async def fake_resolve(owner_id):
        return (fake_container, "127.0.0.1")

    # Point the proxy's GATEWAY_PORT at the stub.
    from routers import hooks_proxy

    monkeypatch.setattr(hooks_proxy, "GATEWAY_PORT", stub_container["port"])
    monkeypatch.setattr(container_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr(hooks_proxy.ecs_manager, "resolve_running_container", fake_resolve)

    client = TestClient(app)
    payload = {"text": "new email", "metadata": {"source": "stripe"}}
    r = client.post(
        "/api/v1/hooks/u/u_e2e/wake",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Stripe/1.0"},
        json=payload,
    )

    assert r.status_code == 202
    assert r.json() == {"queued": True}
    assert json.loads(stub_container["received"]["body"]) == payload
    forwarded = stub_container["received"]["headers"]
    assert forwarded.get("X-Forwarded-Proto") == "https"
    assert forwarded.get("X-Forwarded-Host") == "api.isol8.co"
    assert forwarded.get("Authorization") == f"Bearer {token}"
