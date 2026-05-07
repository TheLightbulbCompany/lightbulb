"""Tests for paperclip_admin_client using httpx MockTransport.

``asyncio_mode = "auto"`` in pyproject.toml means async test functions
are auto-marked, so no ``@pytest.mark.asyncio`` decorator is needed.
"""

from __future__ import annotations

import json as _json
from typing import Callable

import httpx
import pytest

from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> PaperclipAdminClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://paperclip.test")
    return PaperclipAdminClient(http_client=http)


@pytest.fixture
async def client_factory():
    """Yields a factory that builds a client wired to the given response handler.

    The fixture tracks every ``httpx.AsyncClient`` it creates and
    closes them all on teardown so we don't leak sockets between
    tests.
    """
    clients: list[httpx.AsyncClient] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> PaperclipAdminClient:
        c = _make_client(handler)
        clients.append(c._http)
        return c

    yield factory
    for h in clients:
        await h.aclose()


async def test_create_company_sends_session_cookie_and_idempotency_key(client_factory):
    """``create_company`` carries the org-owner's session as a
    ``Cookie:`` header (Better Auth has no bearer plugin) and forwards
    the caller-supplied ``Idempotency-Key``.

    This is the production path: T11 signs the org-owner up via
    Better Auth and passes the resulting session cookie here so the
    company gets owner-membership for the right user.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["cookie"] = req.headers.get("cookie")
        captured["idem"] = req.headers.get("idempotency-key")
        captured["body"] = req.read().decode()
        return httpx.Response(201, json={"id": "co_abc", "name": "u@example.com"})

    client = client_factory(handler)
    company = await client.create_company(
        session_cookie="sess_owner",
        name="u@example.com",
        description="provisioned by Isol8",
        idempotency_key="user_123",
    )
    assert captured["cookie"] == "sess_owner"
    assert captured["idem"] == "user_123"
    body = _json.loads(captured["body"])
    assert body["name"] == "u@example.com"
    assert body["description"] == "provisioned by Isol8"
    # Default budget_monthly_cents=0 should not be sent (kept off the
    # wire so server uses its own default behavior).
    assert "budgetMonthlyCents" not in body
    assert company["id"] == "co_abc"


async def test_5xx_raises_paperclip_api_error(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client = client_factory(handler)
    with pytest.raises(PaperclipApiError) as exc:
        await client.create_company(session_cookie="sess", name="x")
    assert exc.value.status_code == 503


async def test_4xx_raises_paperclip_api_error(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid"})

    client = client_factory(handler)
    with pytest.raises(PaperclipApiError) as exc:
        await client.create_company(session_cookie="sess", name="")
    assert exc.value.status_code == 400


def test_paperclip_api_error_retryable_classification():
    """PaperclipApiError sets ``retryable`` from ``status_code`` directly
    in __init__. T12's webhook handler dispatches retries based on this
    attribute without inspecting the status code itself.

    Classification:
      - 5xx (server error)   -> retryable=True  (transient)
      - 429 (rate limited)   -> retryable=True  (back off + retry)
      - everything else      -> retryable=False (4xx state error)
    """
    # 5xx — transient server errors, retry.
    assert PaperclipApiError("server-down", 500, "").retryable is True
    assert PaperclipApiError("bad-gateway", 502, "").retryable is True
    assert PaperclipApiError("svc-unavail", 503, "").retryable is True
    # 429 — rate-limited, retry with backoff.
    assert PaperclipApiError("rate-limited", 429, "").retryable is True
    # 4xx (excluding 429) — permanent state errors, do not retry.
    assert PaperclipApiError("bad-request", 400, "").retryable is False
    assert PaperclipApiError("not-found", 404, "").retryable is False
    assert PaperclipApiError("conflict", 409, "").retryable is False


async def test_create_agent_api_key_returns_token(client_factory):
    """``POST /api/agents/{id}/keys`` returns ``{id, name, token, createdAt}``.

    Verified against ``server/src/services/agents.ts:createApiKey``.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(
            201,
            json={
                "id": "key_1",
                "name": "primary",
                "token": "secret-token-value",
                "createdAt": "2026-04-27T00:00:00Z",
            },
        )

    client = client_factory(handler)
    result = await client.create_agent_api_key(
        session_cookie="sess_owner",
        agent_id="agent_42",
        name="primary",
    )
    assert result["token"] == "secret-token-value"
    assert "/api/agents/agent_42/keys" in captured["url"]
    body = _json.loads(captured["body"])
    assert body == {"name": "primary"}


async def test_create_agent_passes_adapter_type_and_config(client_factory):
    """``adapterType`` and ``adapterConfig`` are sibling fields per
    ``createAgentSchema`` in
    ``packages/shared/src/validators/agent.ts``.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(201, json={"id": "agent_1"})

    client = client_factory(handler)
    await client.create_agent(
        session_cookie="sess_owner",
        company_id="co_abc",
        name="Main Agent",
        role="ceo",
        adapter_type="openclaw_gateway",
        adapter_config={
            "url": "wss://ws-dev.isol8.co",
            "authToken": "svc_token_xyz",
            "sessionKeyStrategy": "issue",
            "sessionKey": "user_123",
        },
    )
    assert "/api/companies/co_abc/agents" in captured["url"]
    body = _json.loads(captured["body"])
    assert body["adapterType"] == "openclaw_gateway"
    assert body["adapterConfig"]["authToken"] == "svc_token_xyz"
    assert body["adapterConfig"]["sessionKey"] == "user_123"
    assert body["role"] == "ceo"


async def test_delete_company_swallows_404(client_factory):
    """404 on DELETE means already gone — must not raise so cleanup
    cron retries are idempotent.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = client_factory(handler)
    # Should NOT raise.
    await client.delete_company(session_cookie="sess_owner", company_id="co_gone")


async def test_disable_company_sends_archive_post(client_factory):
    """``disable_company`` is mapped onto Paperclip's
    ``POST /api/companies/{companyId}/archive`` since there is no
    dedicated ``disable`` endpoint upstream.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"id": "co_abc", "status": "archived"})

    client = client_factory(handler)
    await client.disable_company(session_cookie="sess_owner", company_id="co_abc")
    assert captured["method"] == "POST"
    assert "/archive" in captured["url"]


# ---------------------------------------------------------------------
# Better Auth (per-user accounts)
# ---------------------------------------------------------------------


async def test_sign_up_user_returns_user_and_session_cookie(client_factory):
    """``POST /api/auth/sign-up/email`` accepts ``{email, password, name?}``
    and returns ``{user, token}`` plus a ``Set-Cookie`` we surface as
    ``_session_cookie``. Sign-up MUST NOT carry any auth — Better
    Auth's ``disableSignUp`` flag is the only gate.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["cookie"] = req.headers.get("cookie")
        captured["body"] = req.read().decode()
        return httpx.Response(
            200,
            json={
                "user": {"id": "pc_user_1", "email": "alice@isol8.co", "name": "Alice"},
                "token": "sess_token_abc",
            },
            headers={
                "Set-Cookie": ("paperclip-default.session_token=cookie-val-abc; Path=/; HttpOnly; Secure"),
            },
        )

    client = client_factory(handler)
    out = await client.sign_up_user(
        email="alice@isol8.co",
        password="random-pass-xyz",
        name="Alice",
    )
    assert captured["method"] == "POST"
    assert "/api/auth/sign-up/email" in captured["url"]
    assert captured["cookie"] is None
    body = _json.loads(captured["body"])
    assert body == {
        "email": "alice@isol8.co",
        "password": "random-pass-xyz",
        "name": "Alice",
    }
    assert out["user"]["id"] == "pc_user_1"
    assert out["_session_cookie"] == "paperclip-default.session_token=cookie-val-abc"


async def test_sign_up_user_defaults_name_to_email(client_factory):
    """If ``name`` is omitted, fall back to ``email`` so the Better
    Auth payload is always complete (the underlying schema requires
    a non-empty name)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.read().decode()
        return httpx.Response(200, json={"user": {"id": "u"}, "token": "t"})

    client = client_factory(handler)
    await client.sign_up_user(email="bob@isol8.co", password="pw")
    body = _json.loads(captured["body"])
    assert body["name"] == "bob@isol8.co"


async def test_sign_in_user_extracts_session_cookie(client_factory):
    """``POST /api/auth/sign-in/email`` returns ``{user, token}`` AND
    sets the Better Auth session cookie via ``Set-Cookie``. We surface
    the bare ``name=value`` of that cookie as ``_session_cookie`` on
    the response dict — Bearer auth is silently ignored upstream so
    this is the only handle callers can use to authenticate.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(
            200,
            json={
                "user": {"id": "pc_user_1", "email": "alice@isol8.co"},
                "token": "sess_signed_in",
            },
            headers={
                "Set-Cookie": ("paperclip-default.session_token=cookie-val-xyz; Path=/; HttpOnly; Secure"),
            },
        )

    client = client_factory(handler)
    out = await client.sign_in_user(
        email="alice@isol8.co",
        password="random-pass-xyz",
    )
    assert "/api/auth/sign-in/email" in captured["url"]
    body = _json.loads(captured["body"])
    assert body == {"email": "alice@isol8.co", "password": "random-pass-xyz"}
    # Cookie is what subsequent admin calls actually need.
    assert out["_session_cookie"] == "paperclip-default.session_token=cookie-val-xyz"
    # Token is still surfaced for any caller logic that wants it.
    assert out["token"] == "sess_signed_in"


# ---------------------------------------------------------------------
# Invite-flow chain
# ---------------------------------------------------------------------


async def test_create_company_as_user_uses_session_cookie(
    client_factory,
):
    """When provisioning the org owner's company, the Cookie header
    must carry the user's session so the new company is owned by them."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["cookie"] = req.headers.get("cookie")
        captured["url"] = str(req.url)
        return httpx.Response(201, json={"id": "co_owned_by_user"})

    client = client_factory(handler)
    out = await client.create_company(
        session_cookie="sess_owner",
        name="owner@isol8.co",
    )
    assert captured["cookie"] == "sess_owner"
    assert "/api/companies" in captured["url"]
    assert out["id"] == "co_owned_by_user"


async def test_create_invite_targets_company_and_email(client_factory):
    """``create_invite`` POSTs to ``/api/companies/{id}/invites`` with
    ``allowedJoinTypes: "human"`` and the admin session cookie.

    Note: Paperclip's invite is token-based, NOT email-targeted. The
    ``email`` parameter here is accepted for caller-side audit
    logging only — we don't put it on the wire because Paperclip
    doesn't store it on the invite. We DO assert the ``humanRole``
    field reaches the body when supplied."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["cookie"] = req.headers.get("cookie")
        captured["body"] = req.read().decode()
        return httpx.Response(
            201,
            json={
                "id": "inv_1",
                "token": "raw-invite-token",
                "companyId": "co_abc",
                "inviteType": "company_join",
                "allowedJoinTypes": "human",
            },
        )

    client = client_factory(handler)
    out = await client.create_invite(
        session_cookie="sess_admin",
        company_id="co_abc",
        email="newmember@isol8.co",
        human_role="member",
    )
    assert "/api/companies/co_abc/invites" in captured["url"]
    assert captured["cookie"] == "sess_admin"
    body = _json.loads(captured["body"])
    assert body["allowedJoinTypes"] == "human"
    assert body["humanRole"] == "member"
    # Email is not propagated to Paperclip (token-based invite).
    assert "email" not in body
    assert out["token"] == "raw-invite-token"


async def test_accept_invite_with_token(client_factory):
    """``accept_invite`` POSTs to ``/api/invites/{token}/accept`` with
    ``{requestType: "human"}`` and the new member's session token."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["cookie"] = req.headers.get("cookie")
        captured["body"] = req.read().decode()
        return httpx.Response(
            201,
            json={
                "id": "join_req_1",
                "status": "pending_approval",
                "companyId": "co_abc",
            },
        )

    client = client_factory(handler)
    out = await client.accept_invite(
        session_cookie="sess_new_member",
        invite_token="raw-invite-token",
    )
    assert "/api/invites/raw-invite-token/accept" in captured["url"]
    assert captured["cookie"] == "sess_new_member"
    body = _json.loads(captured["body"])
    assert body == {"requestType": "human"}
    assert out["status"] == "pending_approval"


async def test_approve_join_request_with_request_id(client_factory):
    """``approve_join_request`` POSTs to
    ``/api/companies/{companyId}/join-requests/{requestId}/approve``
    using a board-admin's session token. Empty body."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["cookie"] = req.headers.get("cookie")
        captured["body"] = req.read().decode()
        return httpx.Response(
            200,
            json={"id": "join_req_1", "status": "approved"},
        )

    client = client_factory(handler)
    out = await client.approve_join_request(
        session_cookie="sess_admin",
        company_id="co_abc",
        request_id="join_req_1",
    )
    assert "/api/companies/co_abc/join-requests/join_req_1/approve" in captured["url"]
    assert captured["cookie"] == "sess_admin"
    # Empty body but JSON encoded as "{}"
    assert captured["body"] == "{}"
    assert out["status"] == "approved"


async def test_archive_member_targets_company_and_member(client_factory):
    """``archive_member`` POSTs to
    ``/api/companies/{companyId}/members/{memberId}/archive`` with the
    admin's session cookie. Empty body."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["cookie"] = req.headers.get("cookie")
        captured["body"] = req.read().decode()
        return httpx.Response(
            200,
            json={
                "member": {
                    "id": "mem_1",
                    "principalId": "pap_user_1",
                    "status": "archived",
                },
                "reassignedIssueCount": 0,
            },
        )

    client = client_factory(handler)
    out = await client.archive_member(
        session_cookie="sess_admin",
        company_id="co_abc",
        member_id="mem_1",
    )
    assert "/api/companies/co_abc/members/mem_1/archive" in captured["url"]
    assert captured["cookie"] == "sess_admin"
    assert captured["body"] == "{}"
    assert out["member"]["status"] == "archived"
