"""Tests for the Teams Approvals BFF.

Audit §3: ``payload.adapterType`` is an indirect smuggling carrier;
the body whitelist on ``ApproveApprovalBody`` / ``RejectApprovalBody``
must reject any non-whitelisted field with 422.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie",
    )


@pytest.fixture
def client(teams_ctx):
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_approvals_proxies_with_session(client, monkeypatch):
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value={"approvals": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/approvals",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.list_approvals.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_approve_with_note(client, monkeypatch):
    admin = MagicMock()
    admin.approve_approval = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/approvals/ap_1/approve",
        json={"note": "lgtm"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.approve_approval.assert_awaited_once_with(
        approval_id="ap_1",
        note="lgtm",
        session_cookie="cookie",
    )


def test_approve_rejects_smuggled_adapter_type(client):
    """A client attempting to smuggle ``adapterType`` through the
    approval payload must be rejected with 422 — body schema forbids
    extras."""
    r = client.post(
        "/api/v1/teams/approvals/ap_1/approve",
        json={"note": "lgtm", "adapterType": "process"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_reject_requires_reason(client):
    """``reason`` is required by ``RejectApprovalBody``; missing it must 422."""
    r = client.post(
        "/api/v1/teams/approvals/ap_1/reject",
        json={},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_get_approval_detail(client, monkeypatch):
    """`GET /teams/approvals/{id}` returns upstream's full approval detail
    shape verbatim."""
    admin = MagicMock()
    admin.get_approval = AsyncMock(
        return_value={
            "id": "apv_1",
            "companyId": "co_abc",
            "status": "pending",
            "type": "tool_call",
            "payload": {"tool": "shell", "args": "ls -la"},
            "createdAt": "2026-05-04T01:00:00Z",
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/approvals/apv_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "apv_1"
    assert body["status"] == "pending"
    admin.get_approval.assert_awaited_once_with(
        approval_id="apv_1",
        session_cookie="cookie",
    )


# --- approval detail pass-throughs (ported stock ApprovalDetail) --------------

APV = "11111111-1111-1111-1111-111111111111"
H = {"Authorization": "Bearer x"}


@pytest.fixture
def admin(client, monkeypatch):
    from routers.teams import agents as agents_mod

    fake = MagicMock()
    fake.get_approval = AsyncMock(return_value={"id": APV, "companyId": "co_abc", "status": "pending"})
    fake.list_approval_comments = AsyncMock(return_value=[{"id": "c1", "body": "hi"}])
    fake.add_approval_comment = AsyncMock(return_value={"id": "c2", "body": "ok"})
    fake.request_approval_revision = AsyncMock(return_value={"id": APV, "status": "revision_requested"})
    fake.resubmit_approval = AsyncMock(return_value={"id": APV, "status": "pending"})
    fake.list_approval_issues = AsyncMock(return_value=[{"id": "iss_1", "title": "T"}])
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    return fake


_CALLS = {
    "get": ("get", f"/api/v1/teams/approvals/{APV}", None),
    "comments": ("get", f"/api/v1/teams/approvals/{APV}/comments", None),
    "comment": ("post", f"/api/v1/teams/approvals/{APV}/comments", {"body": "ok"}),
    "revision": ("post", f"/api/v1/teams/approvals/{APV}/request-revision", {"decisionNote": "fix"}),
    "resubmit": ("post", f"/api/v1/teams/approvals/{APV}/resubmit", {}),
    "issues": ("get", f"/api/v1/teams/approvals/{APV}/issues", None),
}


def _call(client, name, url=None):
    method, default_url, body = _CALLS[name]
    url = url or default_url
    if method == "get":
        return client.get(url, headers=H)
    return client.post(url, json=body, headers=H)


def test_detail_routes_forward_with_member_session(client, admin):
    assert _call(client, "comments").json() == [{"id": "c1", "body": "hi"}]
    admin.list_approval_comments.assert_awaited_once_with(approval_id=APV, session_cookie="cookie")

    r = _call(client, "comment")
    assert r.status_code == 201
    admin.add_approval_comment.assert_awaited_once_with(approval_id=APV, body="ok", session_cookie="cookie")

    assert _call(client, "revision").json()["status"] == "revision_requested"
    admin.request_approval_revision.assert_awaited_once_with(
        approval_id=APV, decision_note="fix", session_cookie="cookie"
    )

    assert _call(client, "resubmit").status_code == 200
    admin.resubmit_approval.assert_awaited_once_with(approval_id=APV, session_cookie="cookie")

    assert _call(client, "issues").json() == [{"id": "iss_1", "title": "T"}]
    admin.list_approval_issues.assert_awaited_once_with(approval_id=APV, session_cookie="cookie")


@pytest.mark.real_fences
@pytest.mark.parametrize("name", list(_CALLS))
def test_another_companys_approval_is_404_and_never_forwarded(client, admin, name):
    admin.get_approval.return_value = {"id": APV, "companyId": "co_OTHER"}
    assert _call(client, name).status_code == 404
    for m in (
        "list_approval_comments",
        "add_approval_comment",
        "request_approval_revision",
        "resubmit_approval",
        "list_approval_issues",
    ):
        getattr(admin, m).assert_not_awaited()


@pytest.mark.parametrize("name", list(_CALLS))
def test_bad_approval_id_is_422(client, admin, name):
    url = _CALLS[name][1].replace(APV, "ap.x")
    assert _call(client, name, url).status_code == 422
    admin.get_approval.assert_not_awaited()


@pytest.mark.parametrize(
    "name,body",
    [
        ("comment", {}),
        ("comment", {"body": ""}),
        ("comment", {"body": "x", "authorAgentId": "a"}),
        ("revision", {"decisionNote": "x", "status": "approved"}),
        ("resubmit", {"payload": {"adapterType": "process"}}),
    ],
)
def test_off_schema_bodies_are_422(client, admin, name, body):
    r = client.post(_CALLS[name][1], json=body, headers=H)
    assert r.status_code == 422
    admin.get_approval.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "body", "admin_call", "decision"),
    [
        ("approve", {"note": "ok"}, "approve_approval", "approved"),
        ("reject", {"reason": "not this"}, "reject_approval", "rejected"),
    ],
)
def test_answering_an_approval_is_a_handoff(client, monkeypatch, action, body, admin_call, decision):
    """T03: approve AND reject (D12) emit approval_answered with the decision."""
    admin = MagicMock()
    setattr(admin, admin_call, AsyncMock(return_value={"ok": True}))
    from routers.teams import agents as agents_mod
    from routers.teams import approvals as approvals_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    answered = MagicMock()
    monkeypatch.setattr(approvals_mod.handoff_analytics, "approval_answered", answered)
    r = client.post(f"/api/v1/teams/approvals/ap_1/{action}", json=body, headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    answered.assert_called_once_with(workspace_id="o1", user_id="u1", approval_id="ap_1", decision=decision)
