# apps/backend/tests/unit/routers/teams/test_routine_detail.py
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


def test_get_routine_forwards(client, monkeypatch):
    admin = MagicMock()
    admin.get_routine = AsyncMock(return_value={"id": "r1", "title": "Nightly digest", "status": "active"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"id": "r1", "title": "Nightly digest", "status": "active"}
    admin.get_routine.assert_awaited_once_with(routine_id="r1", session_cookie="cookie")
