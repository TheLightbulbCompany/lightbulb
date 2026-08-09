"""Tests for the @audit_admin_action decorator.

Covers the CEO S1 fail-closed contract:
- Synchronous DDB write BEFORE response is returned.
- On handler success: audit row written, response unchanged plus
  audit_status="written".
- On handler exception: audit row written with result="error", original
  exception re-raised.
- On DDB write failure: handler result preserved, response annotated with
  audit_status="panic", CRITICAL log emitted, no exception raised from
  the audit layer (don't double-fail the request).
- payload redaction via redact_paths param.
- elapsed_ms captured.
- user_agent + ip captured from request headers.
"""

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def fake_auth():
    from core.auth import AuthContext

    return AuthContext(user_id="admin_alice", email="alice@isol8.co")


@pytest.fixture
def fake_request():
    """Mock FastAPI Request with headers + client.host."""
    request = MagicMock()
    # Production REST chain: attacker-spoofed hop, then the true client IP
    # (appended by API Gateway), then the VPC-link ENI (appended by the ALB).
    request.headers = {
        "user-agent": "Mozilla/5.0 (test)",
        "x-forwarded-for": "203.0.113.1, 198.51.100.7, 10.0.0.1",
    }
    request.client = MagicMock(host="10.0.0.1")
    return request


@pytest.fixture
def repo_mock():
    """Patch admin_actions_repo.create to a mock so we can assert calls without DDB."""
    with patch("core.services.admin_audit.admin_actions_repo.create", new=AsyncMock(return_value={})) as mock:
        yield mock


@pytest.mark.asyncio
async def test_happy_path_writes_audit_and_tags_response(repo_mock, fake_auth, fake_request):
    from core.services.admin_audit import audit_admin_action

    @audit_admin_action("container.reprovision")
    async def handler(*, user_id, request, auth):
        return {"status": "started", "run_id": "abc"}

    response = await handler(user_id="user_target", request=fake_request, auth=fake_auth)

    assert response["status"] == "started"
    assert response["run_id"] == "abc"
    assert response["audit_status"] == "written"

    repo_mock.assert_awaited_once()
    call_kwargs = repo_mock.await_args.kwargs
    assert call_kwargs["admin_user_id"] == "admin_alice"
    assert call_kwargs["target_user_id"] == "user_target"
    assert call_kwargs["action"] == "container.reprovision"
    assert call_kwargs["result"] == "success"
    assert call_kwargs["audit_status"] == "written"
    assert call_kwargs["http_status"] == 200
    assert call_kwargs["error_message"] is None
    assert call_kwargs["user_agent"] == "Mozilla/5.0 (test)"
    # Rightmost UNTRUSTED hop: the ALB-appended VPC-link ENI (10.0.0.1) is
    # skipped, the API-Gateway-established client IP is taken, and the
    # client-supplied spoofable leftmost hop is never reached.
    assert call_kwargs["ip"] == "198.51.100.7"
    assert call_kwargs["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_handler_exception_audits_then_reraises(repo_mock, fake_auth, fake_request):
    from fastapi import HTTPException

    from core.services.admin_audit import audit_admin_action

    @audit_admin_action("container.reprovision")
    async def handler(*, user_id, request, auth):
        raise HTTPException(status_code=409, detail="container_not_running")

    with pytest.raises(HTTPException) as exc_info:
        await handler(user_id="user_target", request=fake_request, auth=fake_auth)

    assert exc_info.value.status_code == 409
    repo_mock.assert_awaited_once()
    call_kwargs = repo_mock.await_args.kwargs
    assert call_kwargs["result"] == "error"
    assert call_kwargs["http_status"] == 409
    assert "container_not_running" in (call_kwargs["error_message"] or "")


@pytest.mark.asyncio
async def test_audit_write_failure_returns_panic_status(fake_auth, fake_request, caplog):
    """CEO S1: DDB write fails → response annotated audit_status='panic',
    CRITICAL log emitted, original handler result preserved."""
    from core.services.admin_audit import audit_admin_action

    with patch(
        "core.services.admin_audit.admin_actions_repo.create",
        new=AsyncMock(side_effect=RuntimeError("ddb_unreachable")),
    ):

        @audit_admin_action("billing.cancel_subscription")
        async def handler(*, user_id, request, auth):
            return {"status": "ok", "subscription": "sub_123"}

        with caplog.at_level(logging.CRITICAL):
            response = await handler(user_id="user_target", request=fake_request, auth=fake_auth)

    assert response["status"] == "ok"
    assert response["subscription"] == "sub_123"
    assert response["audit_status"] == "panic"

    # CRITICAL log includes the action + admin + target for forensics.
    panic_logs = [r for r in caplog.records if "ADMIN_AUDIT_PANIC" in r.message]
    assert len(panic_logs) == 1
    rec = panic_logs[0]
    assert rec.levelno == logging.CRITICAL
    assert "billing.cancel_subscription" in rec.message
    assert "admin_alice" in rec.message
    assert "user_target" in rec.message


@pytest.mark.asyncio
async def test_payload_redact_paths_strip_sensitive_keys(repo_mock, fake_auth, fake_request):
    from core.services.admin_audit import audit_admin_action

    class FakeBody:
        def model_dump(self):
            return {"patch": {"providers": {"anthropic_api_key": "sk-ant-xyz"}}, "note": "ok"}

    @audit_admin_action("config.patch", redact_paths=["patch"])
    async def handler(*, user_id, request, auth, body):
        return {"status": "patched"}

    await handler(user_id="user_target", request=fake_request, auth=fake_auth, body=FakeBody())

    call_kwargs = repo_mock.await_args.kwargs
    payload = call_kwargs["payload"]
    assert payload["patch"] == "***redacted***"
    assert payload["note"] == "ok"


@pytest.mark.asyncio
async def test_payload_default_when_no_body(repo_mock, fake_auth, fake_request):
    """GET endpoints don't have a body — payload becomes empty dict, not error."""
    from core.services.admin_audit import audit_admin_action

    @audit_admin_action("user.view")
    async def handler(*, user_id, request, auth):
        return {"identity": "redacted"}

    await handler(user_id="user_target", request=fake_request, auth=fake_auth)

    call_kwargs = repo_mock.await_args.kwargs
    assert call_kwargs["payload"] == {}


@pytest.mark.asyncio
async def test_target_param_default_is_user_id(repo_mock, fake_auth, fake_request):
    from core.services.admin_audit import audit_admin_action

    @audit_admin_action("container.reprovision")
    async def handler(*, user_id, request, auth):
        return {"ok": True}

    await handler(user_id="user_target_xyz", request=fake_request, auth=fake_auth)

    assert repo_mock.await_args.kwargs["target_user_id"] == "user_target_xyz"


@pytest.mark.asyncio
async def test_target_param_can_be_overridden(repo_mock, fake_auth, fake_request):
    from core.services.admin_audit import audit_admin_action

    @audit_admin_action("agent.delete", target_param="owner_id")
    async def handler(*, owner_id, request, auth):
        return {"ok": True}

    await handler(owner_id="user_other", request=fake_request, auth=fake_auth)

    assert repo_mock.await_args.kwargs["target_user_id"] == "user_other"


@pytest.mark.asyncio
async def test_ip_falls_back_to_client_host_when_no_xff(repo_mock, fake_auth):
    from core.services.admin_audit import audit_admin_action

    request = MagicMock()
    request.headers = {"user-agent": "curl/8"}
    request.client = MagicMock(host="192.0.2.5")

    @audit_admin_action("user.view")
    async def handler(*, user_id, request, auth):
        return {"ok": True}

    await handler(user_id="user_target", request=request, auth=fake_auth)

    assert repo_mock.await_args.kwargs["ip"] == "192.0.2.5"
    assert repo_mock.await_args.kwargs["user_agent"] == "curl/8"


class TestExtractClientIp:
    """Rightmost-untrusted XFF walk: skip our own infra hops from the right,
    take the client hop the innermost trusted proxy established."""

    def _req(self, xff, host="10.0.0.1"):
        request = MagicMock()
        request.headers = {"x-forwarded-for": xff} if xff is not None else {}
        request.client = MagicMock(host=host)
        return request

    def test_skips_vpc_hop_and_takes_apigw_client(self):
        from core.services.admin_audit import extract_client_ip

        assert extract_client_ip(self._req("203.0.113.1, 198.51.100.7, 10.0.0.1")) == "198.51.100.7"

    def test_direct_chain_takes_rightmost_public(self):
        from core.services.admin_audit import extract_client_ip

        assert extract_client_ip(self._req("203.0.113.1, 198.51.100.7")) == "198.51.100.7"

    def test_all_trusted_hops_falls_back_to_client_host(self):
        from core.services.admin_audit import extract_client_ip

        assert extract_client_ip(self._req("10.0.0.9, 10.0.0.1", host="10.0.0.2")) == "10.0.0.2"

    def test_no_xff_falls_back_to_client_host(self):
        from core.services.admin_audit import extract_client_ip

        assert extract_client_ip(self._req(None, host="192.0.2.5")) == "192.0.2.5"

    def test_garbage_hop_is_not_treated_as_trusted(self):
        from core.services.admin_audit import extract_client_ip

        # A non-IP token can't be classified as infra — surfaced as-is rather
        # than silently skipped (better a weird audit value than a wrong one).
        assert extract_client_ip(self._req("203.0.113.1, not-an-ip, 10.0.0.1")) == "not-an-ip"

    def test_prod_vpc_hop_is_also_trusted(self):
        from core.services.admin_audit import extract_client_ip

        # Prod's VPC is 10.2.0.0/16 (network-stack ENV_CONFIG), not the dev
        # 10.0.0.0/16 — the prod VPC-link ENI must be skipped the same way.
        assert extract_client_ip(self._req("203.0.113.1, 198.51.100.7, 10.2.0.14")) == "198.51.100.7"
