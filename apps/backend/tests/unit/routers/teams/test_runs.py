"""Tests for the Teams Runs BFF."""

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


def test_get_run_detail(client, monkeypatch):
    """`GET /teams/heartbeat-runs/{id}` returns upstream's heartbeat-run detail."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(
        return_value={
            "id": "run_1",
            "agentId": "agt_1",
            "status": "failed",
            "startedAt": "2026-05-04T01:00:00Z",
            "stderrExcerpt": "boom",
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "run_1"
    assert body["status"] == "failed"
    admin.get_heartbeat_run.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
    )
