"""Tests for the public Paperclip routine-trigger ingress proxy.

Like hooks_proxy, this router deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have Clerk
tokens. Unlike hooks_proxy there is no edge token at all: the real auth is
Paperclip's per-trigger signing, verified upstream. The owner_id segment
only drives the entitlement gate, rate limit, and audit row.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

UPSTREAM = "http://paperclip.internal:3100"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate-limit buckets live in hooks_proxy module state (shared
    helper); clear them so the 60-POST test doesn't pollute neighbours."""
    from routers import hooks_proxy

    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()
    yield
    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()


@pytest.fixture(autouse=True)
def _paperclip_url(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", UPSTREAM)


@pytest.fixture
def client():
    return TestClient(app)


def _mock_billing(status: str | None):
    """None = no billing row; otherwise a row with that subscription status."""
    return patch(
        "routers.paperclip_trigger_proxy.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


class _StubTransport(httpx.AsyncBaseTransport):
    def __init__(self, captured: dict, status: int = 202, content: bytes = b'{"status":"queued"}'):
        self.captured = captured
        self.status = status
        self.content = content

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured["url"] = str(request.url)
        self.captured["body"] = request.content
        self.captured["headers"] = dict(request.headers)
        return httpx.Response(self.status, content=self.content)


def _stub_client(captured: dict, **kw):
    transport = _StubTransport(captured, **kw)
    return patch(
        "routers.paperclip_trigger_proxy._client_factory",
        lambda: httpx.AsyncClient(transport=transport),
    )


def test_trial_local_owner_returns_402(client):
    """Triggers are a paid always-on feature — trial owners get the
    structured 402 before anything is forwarded."""
    captured: dict = {}
    with _mock_billing("trial_local"), _stub_client(captured):
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["blocked"] is True
    assert detail["code"] == "always_on_requires_subscription"
    assert captured == {}  # nothing reached upstream


def test_unknown_owner_fails_closed_with_402(client):
    """No billing row (unknown / fabricated owner_id) = not entitled."""
    with _mock_billing(None):
        r = client.post("/api/v1/hooks/t/u_404/abc123def456abc123def456", json={})
    assert r.status_code == 402


async def test_invalid_public_id_rejected_with_400(client):
    """publicIds are hex upstream; non-alphanumerics are URL-injection
    attempts, rejected before any lookup work.

    The ``..`` case calls the handler directly — TestClient/httpx
    URL-normalize dot segments before the request leaves the client, but a
    raw HTTP client can send them verbatim (same rationale as the
    hooks_proxy traversal test)."""
    from fastapi import HTTPException

    from routers.paperclip_trigger_proxy import proxy_trigger

    for bad in ("..", "abc/def", "abc def", ""):
        with pytest.raises(HTTPException) as exc:
            await proxy_trigger(owner_id="u_1", public_id=bad, request=None)  # type: ignore[arg-type]
        assert exc.value.status_code == 400
        assert exc.value.detail == "invalid_public_id"

    # Percent-encoded slash through the real stack: starlette decodes it
    # BEFORE routing, so "abc%2Fdef" becomes two path segments and never
    # matches the single-segment route — 404 at the router, equally safe.
    with _mock_billing("active"):
        r = client.post("/api/v1/hooks/t/u_1/abc%2Fdef", json={})
        assert r.status_code == 404


def test_authless_forward_passes_raw_body_and_whitelisted_headers(client):
    """The signed request forwards byte-verbatim with ONLY the signing
    headers + Content-Type; everything else (cookies, spoofed XFF) is
    dropped, and X-Forwarded-For is re-asserted with the true caller IP."""
    captured: dict = {}
    raw_body = b'{"ref": "main",\n  "x":  1}'  # odd spacing — must survive untouched

    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        r = client.post(
            "/api/v1/hooks/t/u_1/abc123def456abc123def456?source=github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer trigger-secret",
                "X-Paperclip-Signature": "sig-value",
                "X-Paperclip-Timestamp": "1723200000",
                "X-Hub-Signature-256": "sha256=deadbeef",
                "Idempotency-Key": "idem-1",
                "Cookie": "clerk=should-not-forward",
                "X-Forwarded-For": "1.2.3.4",  # spoof — must be overwritten
                "X-Custom-Junk": "nope",
            },
        )

    assert r.status_code == 202
    assert r.json() == {"status": "queued"}
    assert captured["url"] == f"{UPSTREAM}/api/routine-triggers/public/abc123def456abc123def456/fire?source=github"
    assert captured["body"] == raw_body  # exact bytes — HMAC-load-bearing
    h = captured["headers"]
    assert h["authorization"] == "Bearer trigger-secret"
    assert h["x-paperclip-signature"] == "sig-value"
    assert h["x-paperclip-timestamp"] == "1723200000"
    assert h["x-hub-signature-256"] == "sha256=deadbeef"
    assert h["idempotency-key"] == "idem-1"
    assert h["content-type"] == "application/json"
    assert "cookie" not in h
    assert "x-custom-junk" not in h
    assert h["x-forwarded-for"] != "1.2.3.4"  # rewritten, not passed through


def test_upstream_status_passes_through_verbatim(client):
    """Paperclip's own signature rejection (401) must reach the sender."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _stub_client(captured, status=401, content=b'{"error":"bad signature"}'),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 401
    assert r.json() == {"error": "bad signature"}


def test_upstream_unreachable_returns_502(client):
    class FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("boom")

    with (
        _mock_billing("active"),
        patch(
            "routers.paperclip_trigger_proxy._client_factory",
            lambda: httpx.AsyncClient(transport=FailTransport()),
        ),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 502


def test_missing_paperclip_url_returns_503(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    with _mock_billing("active"):
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 503


def test_audit_row_written_with_trigger_kind(client):
    captured: dict = {}
    audit_writes = []
    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch(
            "routers.paperclip_trigger_proxy.hook_event_repo.write_event",
            side_effect=lambda **kw: audit_writes.append(kw),
        ),
    ):
        r = client.post(
            "/api/v1/hooks/t/u_1/abc123def456abc123def456",
            json={},
            headers={"User-Agent": "GitHub-Hookshot/1.0"},
        )
    assert r.status_code == 202
    assert len(audit_writes) == 1
    assert audit_writes[0]["kind"] == "paperclip_trigger"
    assert audit_writes[0]["hook_path"] == "abc123def456abc123def456"
    assert audit_writes[0]["status_code"] == 202
    assert audit_writes[0]["user_agent"] == "GitHub-Hookshot/1.0"


def test_audit_write_failure_does_not_drop_proxied_call(client):
    captured: dict = {}
    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch(
            "routers.paperclip_trigger_proxy.hook_event_repo.write_event",
            side_effect=Exception("DDB down"),
        ),
    ):
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 202


def test_rate_limiter_returns_429_after_60_per_minute(client):
    """Separate bucket from container hooks — 60/min per owner."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        for _ in range(60):
            client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
        r = client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_trigger_bucket_is_independent_of_container_hook_bucket(client):
    """Exhausting the trigger bucket must not consume the container-hook
    bucket for the same owner (and vice versa) — keys are prefixed."""
    from routers import hooks_proxy

    captured: dict = {}
    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        client.post("/api/v1/hooks/t/u_1/abc123def456abc123def456", json={})
    assert len(hooks_proxy._RATE_BUCKETS_PER_OWNER["trigger:u_1"]) == 1
    assert "u_1" not in hooks_proxy._RATE_BUCKETS_PER_OWNER
