"""Teams BFF — issue recovery actions (the ported IssueRecoveryActionCard's two routes)."""

from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

import core.auth as auth_mod
from core.auth import AuthContext
from core.services.paperclip_admin_client import PaperclipApiError
from main import app
from routers.teams.deps import TeamsContext

ISSUE = "11111111-1111-1111-1111-111111111111"
ACTION = "22222222-2222-2222-2222-222222222222"
RUN = "33333333-3333-3333-3333-333333333333"


def _bearer(claims: dict, headers: dict | None = None) -> dict:
    # get_current_user (signature) is stubbed below; get_human_session reads only the shape.
    raw = jwt.encode({"sub": "u1", **claims}, "k" * 32, algorithm="HS256", headers=headers)
    return {"Authorization": f"Bearer {raw}"}


H = _bearer({"sid": "sess_1"})  # a person's Clerk session token


@pytest.fixture
def admin(monkeypatch):
    from routers.teams import agents as agents_mod

    ctx = TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu",
        session_cookie="cookie",
    )

    async def fake_ctx():
        return ctx

    fake = MagicMock()
    fake.get_issue = AsyncMock(return_value={"id": ISSUE, "companyId": "co_abc"})
    fake.list_issue_recovery_actions = AsyncMock(return_value={"active": {"id": ACTION}, "actions": [{"id": ACTION}]})
    fake.resolve_issue_recovery_action = AsyncMock(
        return_value={"issue": {"id": ISSUE}, "recoveryAction": {"id": ACTION}}
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)

    async def verified(credentials):
        return AuthContext(user_id="u1", org_id="o1")

    monkeypatch.setattr(auth_mod, "get_current_user", verified)
    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


client = TestClient(app, raise_server_exceptions=False)


def _resolve(body, headers=H):
    return client.post(f"/api/v1/teams/issues/{ISSUE}/recovery-actions/resolve", json=body, headers=headers)


RECONCILE = {
    "runId": RUN,
    "providerStopped": True,
    "actionOutcome": "mixed",
    "outcomeEvidence": "Stopped by me partway through; it wrote half the file.",
}


def test_get_forwards_with_member_session(admin):
    r = client.get(f"/api/v1/teams/issues/{ISSUE}/recovery-actions", headers=H)
    assert r.status_code == 200
    assert r.json()["active"]["id"] == ACTION
    admin.list_issue_recovery_actions.assert_awaited_once_with(issue_id=ISSUE, session_cookie="cookie")


def test_resolve_forwards_camelcase_body(admin):
    r = _resolve({"actionId": ACTION, "outcome": "restored", "sourceIssueStatus": "todo"})
    assert r.status_code == 200
    admin.resolve_issue_recovery_action.assert_awaited_once_with(
        issue_id=ISSUE,
        body={"actionId": ACTION, "outcome": "restored", "sourceIssueStatus": "todo"},
        session_cookie="cookie",
    )


@pytest.mark.real_fences
@pytest.mark.parametrize("method", ["get", "resolve"])
def test_another_companys_issue_is_404_and_never_forwarded(admin, method):
    admin.get_issue.return_value = {"id": ISSUE, "companyId": "co_OTHER"}
    if method == "get":
        r = client.get(f"/api/v1/teams/issues/{ISSUE}/recovery-actions", headers=H)
    else:
        r = _resolve({"outcome": "restored", "sourceIssueStatus": "done"})
    assert r.status_code == 404
    admin.list_issue_recovery_actions.assert_not_awaited()
    admin.resolve_issue_recovery_action.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "restored", "sourceIssueStatus": "blocked"},  # stock superRefine pairing
        {"outcome": "false_positive", "sourceIssueStatus": "todo"},
        {"outcome": "restored", "sourceIssueStatus": "todo", "executionReconciliation": {}},
        {
            "outcome": "restored",
            "sourceIssueStatus": "todo",
            "executionReconciliation": {**RECONCILE, "providerStopped": False},
        },
        {
            "outcome": "restored",
            "sourceIssueStatus": "todo",
            "executionReconciliation": {**RECONCILE, "actionOutcome": "maybe"},
        },
        {
            "outcome": "restored",
            "sourceIssueStatus": "todo",
            "executionReconciliation": {**RECONCILE, "outcomeEvidence": "  too short  "},
        },
        {"outcome": "restored", "sourceIssueStatus": "todo", "executionReconciliation": {**RECONCILE, "extra": 1}},
        {"outcome": "restored", "sourceIssueStatus": "todo", "actionId": "not-a-uuid"},
    ],
)
def test_resolve_rejects_off_schema_bodies(admin, body):
    assert _resolve(body).status_code == 422
    admin.resolve_issue_recovery_action.assert_not_awaited()


def test_resolve_surfaces_paperclips_4xx_reason(admin):
    admin.resolve_issue_recovery_action.side_effect = PaperclipApiError(
        "POST /api/issues/x/recovery-actions/resolve -> 409",
        409,
        {"error": "Recovery action is no longer active"},
    )
    r = _resolve({"actionId": ACTION, "outcome": "restored", "sourceIssueStatus": "todo"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Recovery action is no longer active"


def test_resolve_forwards_a_persons_execution_reconciliation(admin):
    body = {
        "actionId": ACTION,
        "outcome": "restored",
        "sourceIssueStatus": "todo",
        "executionReconciliation": RECONCILE,
    }
    r = _resolve(body)
    assert r.status_code == 200
    admin.resolve_issue_recovery_action.assert_awaited_once_with(issue_id=ISSUE, body=body, session_cookie="cookie")


@pytest.mark.parametrize(
    "headers",
    [
        _bearer({}),  # a JWT-template token: no sid
        _bearer({"sid": "sess_1"}, {"typ": "at+jwt"}),  # an OAuth access token (an MCP tool caller)
        _bearer({"sid": "sess_1", "client_id": "oauth_app"}),
    ],
)
def test_only_a_persons_session_may_resolve(admin, headers):
    body = {
        "actionId": ACTION,
        "outcome": "restored",
        "sourceIssueStatus": "todo",
        "executionReconciliation": RECONCILE,
    }
    r = _resolve(body, headers=headers)
    assert r.status_code == 403
    admin.resolve_issue_recovery_action.assert_not_awaited()
