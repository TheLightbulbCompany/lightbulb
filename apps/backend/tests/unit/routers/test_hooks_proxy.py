"""Tests for the public webhook ingress proxy.

This router is the ONLY isol8 router that deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have
Clerk tokens. Auth is timing-safe bearer-token compare against
``containers.hook_token`` only.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts with an empty rate-limit bucket. Shared in-process
    state otherwise causes cross-test pollution (the 60-POST rate-limit test
    leaves 60 entries in _RATE._buckets['u_1'] which would
    pre-charge any new test that hits the same owner)."""
    from routers import hooks_proxy

    hooks_proxy._RATE.clear()
    yield
    hooks_proxy._RATE.clear()


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


def _mock_billing(status: str | None):
    """Mock the billing-account lookup behind the always-on entitlement gate.
    Pass `None` for the no-billing-row case; pass "active"/"trial_local"/etc.
    to simulate the corresponding subscription state."""
    return patch(
        "routers.hooks_proxy.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


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


def test_trial_local_owner_returns_402(client, fake_container):
    """A trial owner's hook token is real (every container mints one), but
    webhooks are a paid always-on feature — the proxy must refuse with the
    structured 402 before any rate-limit or container-resolution work."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        _mock_billing("trial_local"),
        patch("routers.hooks_proxy.ecs_manager.resolve_running_container", new=AsyncMock()) as resolve,
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["blocked"] is True
    assert detail["code"] == "always_on_requires_subscription"
    resolve.assert_not_awaited()


def test_missing_billing_row_returns_402(client, fake_container):
    """Fail-closed: no billing row at all = not entitled."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        _mock_billing(None),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "always_on_requires_subscription"


def test_active_owner_passes_entitlement_gate(client, fake_container):
    """An active (paid) owner sails through the 402 gate to container-IP
    resolution — no-IP 503 here proves the request got past entitlement."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        _mock_billing("active"),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(fake_container, None)),
        ) as resolve,
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
    assert r.status_code == 503
    resolve.assert_awaited_once()


def test_no_running_task_returns_503(client, fake_container):
    """When EcsManager.resolve_running_container returns no IP, 503 + Retry-After."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_container)),
        _mock_billing("active"),
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
    """The header-stripping guarantee (module docstring: every external webhook was DEAD on
    every 8.1 container until X-Forwarded-*/Forwarded/X-Real-IP came out) is built once in
    `_build_forwarded_headers` and handed to whichever transport forwards the call -- ECS's
    raw httpx client is dead now that `host_for` is pinned to "agentcore" fleet-wide, so this
    drives the SAME assertions through `runtime_transport.request` instead."""
    from core.services import runtime_transport

    agentcore_container = {**fake_container, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["path"] = path
        captured["content"] = content
        captured["headers"] = headers
        return runtime_transport.RuntimeResponse(200, '{"ok": true}', {"content-type": "application/json"})

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=agentcore_container)),
        _mock_billing("active"),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(agentcore_container, runtime_transport.AGENTCORE_ADDRESS)),
        ),
        patch("routers.hooks_proxy.runtime_transport.request", fake_request),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={
                "Authorization": "Bearer " + fake_container["hook_token"],
                "User-Agent": "Stripe/1.0",
                # Spoofed XFF — the proxy must overwrite, not append.
                "X-Forwarded-For": "1.2.3.4",
                "X-Real-IP": "1.2.3.4",
            },
            json={"text": "new email"},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured["path"] == "/hooks/wake"
    assert captured["content"] == r.request.content
    # Headers we control:
    assert captured["headers"]["Authorization"] == "Bearer " + fake_container["hook_token"]
    # NO X-Forwarded-* may reach the container. 2026.8.1 ingress attribution
    # (gateway/ingress-attribution.ts) fails CLOSED -- 403 proxy_attribution_required,
    # before the hooks handler runs -- for any request carrying forwarded headers
    # from an address outside gateway.trustedProxies, and our container config
    # deliberately trusts only 127.0.0.1/::1 (the backend's 10.x subnet is NOT
    # trusted, by design, after the WS-path 403 incident). Verified live on a
    # rolled dev container 2026-09-02: same request, same token -- 400
    # "message required" without the headers, 403 with them. Every external
    # webhook through this proxy was dead on every 8.1 container until this.
    # The caller's spoofed value must still not pass through either.
    for h in captured["headers"]:
        assert not h.lower().startswith("x-forwarded-"), h
    assert "forwarded" not in {h.lower() for h in captured["headers"]}
    assert "x-real-ip" not in {h.lower() for h in captured["headers"]}


def test_query_string_is_forwarded_verbatim(client, fake_container):
    """Webhook senders carry verification/signature material in the query
    string (Slack URL-verification ``?challenge=``, custom ``?routing_key=``,
    etc.). The proxy must forward it verbatim — dropping it silently breaks
    those handshakes. The ECS raw-httpx forward is dead now (`host_for` is
    pinned to "agentcore" fleet-wide), so this drives the same assertion
    through `runtime_transport.request` instead -- the query-string append is
    shared code (``path = f"/hooks/{hook_path}" + (f"?{query}" if query else "")``)."""
    from core.services import runtime_transport

    agentcore_container = {**fake_container, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["path"] = path
        return runtime_transport.RuntimeResponse(200, '{"ok": true}', {})

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=agentcore_container)),
        _mock_billing("active"),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(agentcore_container, runtime_transport.AGENTCORE_ADDRESS)),
        ),
        patch("routers.hooks_proxy.runtime_transport.request", fake_request),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake?challenge=abc&signature=xyz",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )

    assert r.status_code == 200
    assert captured["path"].endswith("wake?challenge=abc&signature=xyz")


@pytest.mark.asyncio
async def test_path_traversal_rejected_with_400():
    """Defense-in-depth: hook_path containing ``..`` or ``\\`` is rejected
    before any auth/lookup work. A valid-token holder must not be able to
    escape ``/hooks/*`` containment via path traversal.

    Tested by calling the handler directly — TestClient/httpx URL-normalize
    ``foo/../bar`` to ``bar`` before the request leaves the client, but a
    raw HTTP client (curl, a webhook source) can send ``..`` segments
    verbatim, which is the threat we're guarding against.
    """
    from fastapi import HTTPException

    from routers.hooks_proxy import proxy_hook

    # `..` as a path segment
    with pytest.raises(HTTPException) as exc:
        await proxy_hook(owner_id="u_1", hook_path="foo/../bar", request=None)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_hook_path"

    # Bare `..` segment (no surrounding path)
    with pytest.raises(HTTPException) as exc:
        await proxy_hook(owner_id="u_1", hook_path="..", request=None)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_hook_path"

    # Backslash anywhere in the path
    with pytest.raises(HTTPException) as exc:
        await proxy_hook(owner_id="u_1", hook_path="foo\\bar", request=None)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_hook_path"


def test_agentcore_row_forwards_through_the_runtime_transport_seam(client, fake_container):
    """An owner on AgentCore never opens the ECS httpx client -- the request
    goes through ``runtime_transport.request`` instead, addressed by the
    sentinel ip and the container row (not a dialled task IP)."""
    from core.services import runtime_transport

    agentcore_container = {**fake_container, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["container"] = container
        captured["path"] = path
        captured["ip"] = ip
        captured["content"] = content
        captured["headers"] = headers
        captured["timeout_s"] = timeout_s
        return runtime_transport.RuntimeResponse(200, '{"ok": true}', {"content-type": "application/json"})

    def _boom_client_factory():
        raise AssertionError("ECS client must not be built for an agentcore row")

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=agentcore_container)),
        _mock_billing("active"),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(agentcore_container, runtime_transport.AGENTCORE_ADDRESS)),
        ),
        patch("routers.hooks_proxy._client_factory", _boom_client_factory),
        patch("routers.hooks_proxy.runtime_transport.request", fake_request),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake?challenge=abc",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={"text": "hi"},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured["container"] is agentcore_container
    assert captured["path"] == "/hooks/wake?challenge=abc"
    assert captured["ip"] == runtime_transport.AGENTCORE_ADDRESS
    assert captured["headers"]["Authorization"] == "Bearer " + fake_container["hook_token"]
    assert captured["content"] == r.request.content
    assert captured["timeout_s"] == 30.0


def test_rate_limiter_returns_429_after_60_per_minute(client, fake_container):
    """In-process token bucket caps at 60/min per owner. The limiter check runs before IP
    resolution/forwarding either way, but the loop below still needs each of the 60 calls to
    complete cleanly rather than raising on the (now dead) ECS forward path -- `host_for` is
    pinned to "agentcore" fleet-wide, so the container is agentcore-shaped here too."""
    from core.services import runtime_transport

    agentcore_container = {**fake_container, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}

    async def fake_request(*_a, **_kw):
        return runtime_transport.RuntimeResponse(200, "{}", {})

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id", new=AsyncMock(return_value=agentcore_container)),
        _mock_billing("active"),
        patch(
            "routers.hooks_proxy.ecs_manager.resolve_running_container",
            new=AsyncMock(return_value=(agentcore_container, runtime_transport.AGENTCORE_ADDRESS)),
        ),
        patch("routers.hooks_proxy.runtime_transport.request", fake_request),
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
