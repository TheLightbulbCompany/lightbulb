# apps/backend/tests/unit/routers/teams/test_issue_children.py
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


def test_list_children_forwards_descendantOf_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[{"id": "c1"}, {"id": "c2"}])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/children", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"issues": [{"id": "c1"}, {"id": "c2"}]}
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc", session_cookie="cookie", params={"descendantOf": "iss1"}
    )


def test_create_child_whitelists_and_calls_admin(client, monkeypatch):
    admin = MagicMock()
    admin.create_child_issue = AsyncMock(return_value={"id": "child1", "title": "Sub"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/issues/iss1/children",
        json={"title": "Sub", "assigneeAgentId": "ag1"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == "child1"
    call = admin.create_child_issue.await_args
    assert call.kwargs["issue_id"] == "iss1"
    assert call.kwargs["body"] == {"title": "Sub", "assigneeAgentId": "ag1"}
    assert "company_id" not in call.kwargs


def test_create_child_rejects_extra_keys(client, monkeypatch):
    admin = MagicMock()
    admin.create_child_issue = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/issues/iss1/children",
        json={"title": "Sub", "adapterType": "x"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.create_child_issue.assert_not_awaited()
