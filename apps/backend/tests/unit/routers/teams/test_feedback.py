# apps/backend/tests/unit/routers/teams/test_feedback.py
"""Teams BFF — agent feedback votes (thumbs up/down on agent output).

Covers the cast/list vote proxy plus the consent-gated feedback-traces
listing. Mirrors the fixture/mocking style of the sibling teams tests
(test_issue_documents.py etc.).
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
        paperclip_user_id="pcu",
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


def _mock_admin(monkeypatch, admin):
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)


def test_list_feedback_votes_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_feedback_votes = AsyncMock(
        return_value=[
            {"id": "v1", "targetType": "issue_comment", "targetId": "cmt1", "vote": "up"},
            {"id": "v2", "targetType": "issue_comment", "targetId": "cmt2", "vote": "down"},
        ]
    )
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/issues/iss1/feedback-votes", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {
        "votes": [
            {"id": "v1", "targetType": "issue_comment", "targetId": "cmt1", "vote": "up"},
            {"id": "v2", "targetType": "issue_comment", "targetId": "cmt2", "vote": "down"},
        ]
    }
    admin.list_issue_feedback_votes.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")


def test_cast_feedback_vote_forwards_whitelisted_body(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={"id": "v1", "vote": "up"})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={"targetType": "issue_comment", "targetId": "cmt1", "vote": "up"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "v1", "vote": "up"}
    admin.upsert_issue_feedback_vote.assert_awaited_once()
    kwargs = admin.upsert_issue_feedback_vote.await_args.kwargs
    assert kwargs["issue_id"] == "iss1"
    assert kwargs["session_cookie"] == "cookie"
    # camelCase wire shape; None fields excluded.
    assert kwargs["body"] == {"targetType": "issue_comment", "targetId": "cmt1", "vote": "up"}


def test_cast_feedback_vote_with_reason_and_sharing(client, monkeypatch):
    # Consent ENABLED -> allowSharing:true is forwarded through unchanged.
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"id": "co_abc", "feedbackDataSharingEnabled": True})
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={"id": "v1", "vote": "down"})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={
            "targetType": "issue_comment",
            "targetId": "cmt1",
            "vote": "down",
            "reason": "missed the ask",
            "allowSharing": True,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_company.assert_awaited_once_with(company_id="co_abc", session_cookie="cookie")
    body = admin.upsert_issue_feedback_vote.await_args.kwargs["body"]
    assert body["reason"] == "missed the ask"
    assert body["allowSharing"] is True


def test_cast_feedback_vote_forces_sharing_false_when_consent_disabled(client, monkeypatch):
    # Consent DISABLED -> a direct POST with allowSharing:true must not share.
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"id": "co_abc", "feedbackDataSharingEnabled": False})
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={"id": "v1", "vote": "up"})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={
            "targetType": "issue_comment",
            "targetId": "cmt1",
            "vote": "up",
            "allowSharing": True,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_company.assert_awaited_once_with(company_id="co_abc", session_cookie="cookie")
    body = admin.upsert_issue_feedback_vote.await_args.kwargs["body"]
    assert body["allowSharing"] is False


def test_cast_feedback_vote_fails_closed_when_company_missing(client, monkeypatch):
    # Fail closed: a non-dict/missing company is treated as sharing-disabled.
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value=None)
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={"id": "v1", "vote": "up"})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={
            "targetType": "issue_comment",
            "targetId": "cmt1",
            "vote": "up",
            "allowSharing": True,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.upsert_issue_feedback_vote.await_args.kwargs["body"]
    assert body["allowSharing"] is False


def test_cast_feedback_vote_skips_consent_check_when_not_sharing(client, monkeypatch):
    # No allowSharing requested -> no consent lookup on the common path.
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"feedbackDataSharingEnabled": True})
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={"id": "v1", "vote": "up"})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={"targetType": "issue_comment", "targetId": "cmt1", "vote": "up"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_company.assert_not_awaited()
    body = admin.upsert_issue_feedback_vote.await_args.kwargs["body"]
    assert "allowSharing" not in body


def test_cast_feedback_vote_rejects_bad_vote_value(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={"targetType": "issue_comment", "targetId": "cmt1", "vote": "sideways"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.upsert_issue_feedback_vote.assert_not_awaited()


def test_cast_feedback_vote_rejects_smuggled_field(client, monkeypatch):
    admin = MagicMock()
    admin.upsert_issue_feedback_vote = AsyncMock(return_value={})
    _mock_admin(monkeypatch, admin)

    r = client.post(
        "/api/v1/teams/issues/iss1/feedback-votes",
        json={
            "targetType": "issue_comment",
            "targetId": "cmt1",
            "vote": "up",
            "adapterType": "openclaw_gateway",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.upsert_issue_feedback_vote.assert_not_awaited()


def test_feedback_traces_gated_when_sharing_disabled(client, monkeypatch):
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"id": "co_abc", "feedbackDataSharingEnabled": False})
    admin.list_issue_feedback_traces = AsyncMock(return_value=[{"id": "t1"}])
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/issues/iss1/feedback-traces", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    # Consent gate: sharing off -> no traces exposed, upstream traces call skipped.
    assert r.json() == {"traces": []}
    admin.list_issue_feedback_traces.assert_not_awaited()


def test_feedback_traces_proxied_when_sharing_enabled(client, monkeypatch):
    admin = MagicMock()
    admin.get_company = AsyncMock(return_value={"id": "co_abc", "feedbackDataSharingEnabled": True})
    admin.list_issue_feedback_traces = AsyncMock(
        return_value=[{"id": "t1", "vote": "up"}, {"id": "t2", "vote": "down"}]
    )
    _mock_admin(monkeypatch, admin)

    r = client.get("/api/v1/teams/issues/iss1/feedback-traces", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"traces": [{"id": "t1", "vote": "up"}, {"id": "t2", "vote": "down"}]}
    admin.get_company.assert_awaited_once_with(company_id="co_abc", session_cookie="cookie")
    # Traces live on the COMPANY-scoped upstream route with issueId as a query
    # filter — the handler must forward company_id (from ctx) + issue_id so the
    # admin client hits /api/companies/{id}/feedback-traces?issueId=..., NOT the
    # non-existent /api/issues/{id}/feedback-traces route.
    admin.list_issue_feedback_traces.assert_awaited_once_with(
        company_id="co_abc", issue_id="iss1", session_cookie="cookie"
    )
