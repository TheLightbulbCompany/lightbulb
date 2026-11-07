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
    PaperclipAgentCreateReplayMiss,
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
        captured["idempotency_key"] = req.headers.get("idempotency-key")
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
        idempotency_key="agent-create-1",
    )
    assert "/api/companies/co_abc/agents" in captured["url"]
    body = _json.loads(captured["body"])
    assert body["adapterType"] == "openclaw_gateway"
    assert body["adapterConfig"]["authToken"] == "svc_token_xyz"
    assert body["adapterConfig"]["sessionKey"] == "user_123"
    assert body["role"] == "ceo"
    assert "idempotencyKey" not in body
    assert "idempotencyReplayOnly" not in body
    assert captured["idempotency_key"] == "agent-create-1"
    assert "runtimeConfig" not in body  # no cap for a non-engineer


async def test_create_engineer_is_born_with_the_run_cap(client_factory):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(_json.loads(req.read().decode()))
        return httpx.Response(201, json={"id": "agent_1"})

    client = client_factory(handler)
    for runtime_config in (None, {"heartbeat": {"enabled": True}}):
        await client.create_agent(
            session_cookie="s",
            company_id="co",
            name="Forge",
            role="engineer",
            adapter_type="hermes_gateway",
            adapter_config={},
            runtime_config=runtime_config,
        )
    assert [b["runtimeConfig"] for b in bodies] == [
        {"heartbeat": {"maxConcurrentRuns": 2}},
        {"heartbeat": {"enabled": True, "maxConcurrentRuns": 2}},
    ]


async def test_create_agent_replay_only_reuses_the_same_header_key(client_factory):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["key"] = req.headers.get("idempotency-key")
        captured["replay_only"] = req.headers.get("idempotency-replay-only")
        captured["body"] = req.read().decode()
        return httpx.Response(200, json={"id": "agent_1"})

    client = client_factory(handler)
    await client.create_agent(
        session_cookie="sess_owner",
        company_id="co_abc",
        name="Main Agent",
        role="ceo",
        adapter_type="openclaw_gateway",
        adapter_config={},
        idempotency_key="agent-create-1",
        idempotency_replay_only=True,
    )

    assert captured["key"] == "agent-create-1"
    assert captured["replay_only"] == "true"
    body = _json.loads(captured["body"])
    assert "idempotencyKey" not in body
    assert "idempotencyReplayOnly" not in body


async def test_create_agent_maps_only_the_structured_replay_miss_code(client_factory):
    def replay_miss(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "agent_create_idempotency_replay_miss"})

    client = client_factory(replay_miss)
    with pytest.raises(PaperclipAgentCreateReplayMiss):
        await client.create_agent(
            session_cookie="sess_owner",
            company_id="co_abc",
            name="Main Agent",
            role="ceo",
            adapter_type="openclaw_gateway",
            adapter_config={},
            idempotency_key="agent-create-1",
            idempotency_replay_only=True,
        )

    def unrelated_conflict(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": "text mentions agent_create_idempotency_replay_miss but has no code"},
        )

    client = client_factory(unrelated_conflict)
    with pytest.raises(PaperclipApiError) as exc:
        await client.create_agent(
            session_cookie="sess_owner",
            company_id="co_abc",
            name="Main Agent",
            role="ceo",
            adapter_type="openclaw_gateway",
            adapter_config={},
            idempotency_key="agent-create-1",
            idempotency_replay_only=True,
        )
    assert type(exc.value) is PaperclipApiError


async def test_create_agent_replay_only_requires_the_operation_key(client_factory):
    def must_not_send(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    client = client_factory(must_not_send)
    with pytest.raises(ValueError, match="requires idempotency_key"):
        await client.create_agent(
            session_cookie="sess_owner",
            company_id="co_abc",
            name="Main Agent",
            role="ceo",
            adapter_type="openclaw_gateway",
            adapter_config={},
            idempotency_replay_only=True,
        )


async def test_create_child_issue_posts_to_issue_scoped_path(client_factory):
    """``create_child_issue`` POSTs to the ISSUE-scoped
    ``/api/issues/{issueId}/children`` — NOT a company-scoped path.

    Upstream's child-create route is mounted issue-scoped
    (``router.post("/issues/:id/children")``); company is derived
    server-side from the parent. A company-scoped path 404s. The
    router test mocks this admin method, so this is the only place the
    exact wire path is exercised — pinning it here prevents a silent
    regression back to ``/api/companies/{id}/issues/{id}/children``.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = req.read().decode()
        return httpx.Response(201, json={"id": "child1", "title": "x"})

    client = client_factory(handler)
    out = await client.create_child_issue(
        session_cookie="sess_owner",
        issue_id="iss1",
        body={"title": "x"},
    )
    assert captured["method"] == "POST"
    assert "/api/issues/iss1/children" in captured["url"]
    assert "/companies/" not in captured["url"]
    body = _json.loads(captured["body"])
    assert body == {"title": "x"}
    assert out["id"] == "child1"


async def test_delete_company_swallows_404(client_factory):
    """404 on DELETE means already gone — must not raise so cleanup
    cron retries are idempotent.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = client_factory(handler)
    # Should NOT raise.
    await client.delete_company(session_cookie="sess_owner", company_id="co_gone")


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


async def test_approve_approval_remaps_note_to_decision_note(client_factory):
    """Upstream ``resolveApprovalSchema`` accepts only ``decisionNote``, so
    the BFF's ``note`` input must land on the wire as ``decisionNote`` —
    otherwise the reviewer note is silently stripped (data loss)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = _json.loads(req.read().decode())
        return httpx.Response(200, json={"ok": True})

    client = client_factory(handler)
    await client.approve_approval(session_cookie="sess", approval_id="ap_1", note="looks good")
    assert "/api/approvals/ap_1/approve" in captured["url"]
    assert captured["body"] == {"decisionNote": "looks good"}


async def test_approve_approval_empty_note_sends_empty_body(client_factory):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.read().decode()
        return httpx.Response(200, json={"ok": True})

    client = client_factory(handler)
    await client.approve_approval(session_cookie="sess", approval_id="ap_1", note=None)
    assert captured["body"] == "{}"


async def test_reject_approval_remaps_reason_to_decision_note(client_factory):
    """``reject_approval``'s ``reason`` maps to upstream ``decisionNote``."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = _json.loads(req.read().decode())
        return httpx.Response(200, json={"ok": True})

    client = client_factory(handler)
    await client.reject_approval(session_cookie="sess", approval_id="ap_1", reason="not safe")
    assert "/api/approvals/ap_1/reject" in captured["url"]
    assert captured["body"] == {"decisionNote": "not safe"}


async def test_create_routine_trigger_posts_to_triggers_endpoint(client_factory):
    """Cron schedule lives on a trigger (POST /routines/{id}/triggers), not
    the routine row."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = _json.loads(req.read().decode())
        return httpx.Response(201, json={"trigger": {"id": "tr_1"}, "secretMaterial": None})

    client = client_factory(handler)
    out = await client.create_routine_trigger(
        session_cookie="sess",
        routine_id="r1",
        body={"kind": "schedule", "cronExpression": "0 9 * * *", "timezone": "UTC"},
    )
    assert "/api/routines/r1/triggers" in captured["url"]
    assert captured["body"]["kind"] == "schedule"
    assert captured["body"]["cronExpression"] == "0 9 * * *"
    assert out["trigger"]["id"] == "tr_1"


# ---------------------------------------------------------------------------
# A2 — editable routines: trigger patch/delete/rotate + revisions list/restore
# ---------------------------------------------------------------------------


async def test_patch_routine_trigger_maps_to_routine_triggers_endpoint(client_factory):
    """Trigger edit is keyed by the global trigger id upstream, NOT nested."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["body"] = _json.loads(req.read().decode())
        return httpx.Response(200, json={"id": "tr_1", "cronExpression": "0 6 * * *"})

    client = client_factory(handler)
    out = await client.patch_routine_trigger(
        session_cookie="sess",
        trigger_id="tr_1",
        body={"cronExpression": "0 6 * * *", "enabled": True},
    )
    assert captured["method"] == "PATCH"
    assert "/api/routine-triggers/tr_1" in captured["url"]
    assert captured["body"] == {"cronExpression": "0 6 * * *", "enabled": True}
    assert out["cronExpression"] == "0 6 * * *"


async def test_delete_routine_trigger_swallows_404(client_factory):
    """A double-delete of a trigger is a no-op, not an error (idempotent)."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        assert "/api/routine-triggers/tr_1" in str(req.url)
        return httpx.Response(404, json={"error": "Routine trigger not found"})

    client = client_factory(handler)
    out = await client.delete_routine_trigger(session_cookie="sess", trigger_id="tr_1")
    assert out == {}


async def test_delete_routine_archives_via_patch(client_factory):
    """delete_routine must PATCH status=archived — upstream has no DELETE
    /api/routines/{id} route, and the old DELETE 404ed silently (the routine
    survived and kept firing)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = _json.loads(req.content)
        return httpx.Response(200, json={"id": "rt_1", "status": "archived"})

    client = client_factory(handler)
    out = await client.delete_routine(session_cookie="sess", routine_id="rt_1")
    assert captured["method"] == "PATCH"
    assert "/api/routines/rt_1" in captured["url"]
    assert captured["body"] == {"status": "archived"}
    assert out["status"] == "archived"


async def test_delete_routine_swallows_404(client_factory):
    """Deleting an already-gone routine stays an idempotent no-op — the
    PATCH-based archive must preserve the old DELETE contract for retries
    and double-deletes."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Routine not found"})

    client = client_factory(handler)
    out = await client.delete_routine(session_cookie="sess", routine_id="rt_gone")
    assert out == {}


async def test_rotate_routine_trigger_secret_posts_empty_body(client_factory):
    """Rotate posts an empty object and returns the fresh secret material."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["body"] = req.read().decode()
        return httpx.Response(200, json={"trigger": {"id": "tr_1"}, "secretMaterial": "whsec_new"})

    client = client_factory(handler)
    out = await client.rotate_routine_trigger_secret(session_cookie="sess", trigger_id="tr_1")
    assert captured["method"] == "POST"
    assert "/api/routine-triggers/tr_1/rotate-secret" in captured["url"]
    assert captured["body"] == "{}"
    assert out["secretMaterial"] == "whsec_new"


async def test_list_routine_revisions_returns_list(client_factory):
    """Revisions come back as a bare upstream array."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/api/routines/r1/revisions" in str(req.url)
        return httpx.Response(200, json=[{"id": "rev_2"}, {"id": "rev_1"}])

    client = client_factory(handler)
    out = await client.list_routine_revisions(session_cookie="sess", routine_id="r1")
    assert [r["id"] for r in out] == ["rev_2", "rev_1"]


async def test_list_routine_revisions_tolerates_non_list(client_factory):
    """A non-list upstream body degrades to [] instead of raising."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = client_factory(handler)
    out = await client.list_routine_revisions(session_cookie="sess", routine_id="r1")
    assert out == []


async def test_restore_routine_revision_posts_to_restore_endpoint(client_factory):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        return httpx.Response(200, json={"revision": {"id": "rev_1", "revisionNumber": 1}})

    client = client_factory(handler)
    out = await client.restore_routine_revision(session_cookie="sess", routine_id="r1", revision_id="rev_1")
    assert captured["method"] == "POST"
    assert "/api/routines/r1/revisions/rev_1/restore" in captured["url"]
    assert out["revision"]["id"] == "rev_1"


# ---------------------------------------------------------------------------
# Retry-After: two emitters, two spellings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["Retry-After", "X-Retry-After"])
async def test_sign_in_429_carries_the_backoff_hint_in_either_spelling(client_factory, header):
    """Better Auth's sign-in limiter emits ``X-Retry-After`` while Paperclip's
    own routes emit the standard ``Retry-After``. Both are real, so reading
    only one silently ignores the other's hint — which is how a caller ends up
    inventing a backoff the server already told it."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too many requests", headers={header: "42"})

    client = client_factory(handler)
    with pytest.raises(PaperclipApiError) as err:
        await client.sign_in_user(email="a@b.test", password="pw")

    assert err.value.status_code == 429
    assert err.value.retryable is True
    assert err.value.retry_after == 42


async def test_a_429_with_no_hint_leaves_retry_after_unset(client_factory):
    """None means "no hint" — the caller uses its own backoff rather than
    treating a missing header as zero seconds."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too many requests")

    client = client_factory(handler)
    with pytest.raises(PaperclipApiError) as err:
        await client.sign_in_user(email="a@b.test", password="pw")

    assert err.value.retry_after is None


async def test_a_malformed_or_non_positive_hint_is_treated_as_absent(client_factory):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope", headers={"Retry-After": "not-a-number"})

    client = client_factory(handler)
    with pytest.raises(PaperclipApiError) as err:
        await client.sign_in_user(email="a@b.test", password="pw")

    assert err.value.retry_after is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({}, False), ({"can_create_agents": True}, True)],
)
async def test_create_agent_pins_can_create_agents(client_factory, kwargs, expected):
    """Paperclip v2026.916 defaults ``canCreateAgents`` to TRUE for a new
    agent, so the one funnel sends an explicit value: false unless a caller
    opts in (only the starter CEO does)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.read().decode())
        return httpx.Response(201, json={"id": "agent_1"})

    client = client_factory(handler)
    await client.create_agent(
        session_cookie="sess",
        company_id="co_abc",
        name="A",
        role="engineer",
        adapter_type="hermes_gateway",
        adapter_config={},
        **kwargs,
    )
    assert captured["body"]["permissions"] == {"canCreateAgents": expected}


@pytest.fixture(autouse=True)
def _no_registry_mirror(monkeypatch):
    """These pin the Paperclip calls; the registry mirror (always on since the switch retired) has its own suite."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services.paperclip_admin_client import PaperclipAdminClient as _Client

    monkeypatch.setattr(_Client, "_mirror", _AsyncMock())
