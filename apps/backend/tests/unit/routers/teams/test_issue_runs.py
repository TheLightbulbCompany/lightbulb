# apps/backend/tests/unit/routers/teams/test_issue_runs.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc", paperclip_user_id="pcu", session_cookie="cookie"
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


def test_list_issue_runs_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_runs = AsyncMock(return_value=[{"runId": "r1"}, {"runId": "r2"}])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"runs": [{"runId": "r1"}, {"runId": "r2"}]}
    admin.list_issue_runs.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")
