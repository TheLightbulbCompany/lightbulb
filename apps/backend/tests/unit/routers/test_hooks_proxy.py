"""Tests for the public webhook ingress proxy.

This router is the ONLY isol8 router that deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have
Clerk tokens. Auth is timing-safe bearer-token compare against
``containers.hook_token`` only.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts with an empty rate-limit bucket. Shared in-process
    state otherwise causes cross-test pollution (the 60-POST rate-limit test
    leaves 60 entries in _RATE_BUCKETS_PER_OWNER['u_1'] which would
    pre-charge any new test that hits the same owner)."""
    from routers import hooks_proxy

    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()
    yield
    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_container():
    return {
        "owner_id": "u_1",
        "hook_token": "secret-token-32-bytes-of-randomness",
        "service_name": "openclaw-u-1-abc",
        "status": "running",
    }


def test_missing_authorization_returns_401(client, fake_container):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)):
        r = client.post("/api/v1/hooks/u/u_1/wake", json={})
        assert r.status_code == 401


def test_wrong_token_returns_401(client, fake_container):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer wrong"},
            json={},
        )
        assert r.status_code == 401


def test_unknown_owner_returns_404(client):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)):
        r = client.post(
            "/api/v1/hooks/u/u_404/wake",
            headers={"Authorization": "Bearer anything"},
            json={},
        )
        assert r.status_code == 404


def test_no_running_task_returns_503(client, fake_container):
    """When EcsManager.resolve_running_container returns no IP, 503 + Retry-After."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(fake_container, None)),
        ),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 503
        assert "Retry-After" in r.headers


def test_successful_forward_passes_body_and_writes_audit(client, fake_container):
    forwarded_kwargs = {}

    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            forwarded_kwargs["url"] = str(request.url)
            forwarded_kwargs["body"] = request.content
            forwarded_kwargs["headers"] = dict(request.headers)
            return httpx.Response(200, content=b'{"ok": true}')

    audit_writes = []

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(fake_container, "10.0.42.17")),
        ),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event", side_effect=lambda **kw: audit_writes.append(kw)),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={
                "Authorization": "Bearer " + fake_container["hook_token"],
                "User-Agent": "Stripe/1.0",
                # Spoofed XFF — the proxy must overwrite, not append.
                "X-Forwarded-For": "1.2.3.4",
            },
            json={"text": "new email"},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert forwarded_kwargs["url"] == "http://10.0.42.17:18789/hooks/wake"
    # Headers we control:
    assert forwarded_kwargs["headers"]["authorization"] == "Bearer " + fake_container["hook_token"]
    assert forwarded_kwargs["headers"]["x-forwarded-for"] != "1.2.3.4"  # rewritten, not passed
    assert forwarded_kwargs["headers"]["x-forwarded-proto"] == "https"
    assert forwarded_kwargs["headers"]["x-forwarded-host"] == "api.isol8.co"
    # Audit row:
    assert len(audit_writes) == 1
    assert audit_writes[0]["status_code"] == 200
    assert audit_writes[0]["hook_path"] == "wake"
    assert audit_writes[0]["user_agent"] == "Stripe/1.0"


def test_audit_write_failure_does_not_drop_proxied_call(client, fake_container):
    """DDB outage must not turn a 200 into a 500."""

    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b'{"ok": true}')

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(fake_container, "10.0.42.17")),
        ),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event", side_effect=Exception("DDB down")),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 200


def test_rate_limiter_returns_429_after_60_per_minute(client, fake_container):
    """In-process token bucket caps at 60/min per owner."""

    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"{}")

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(fake_container, "10.0.42.17")),
        ),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event"),
    ):
        # Exhaust the bucket (60).
        for _ in range(60):
            client.post(
                "/api/v1/hooks/u/u_1/wake",
                headers={"Authorization": "Bearer " + fake_container["hook_token"]},
                json={},
            )
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers
