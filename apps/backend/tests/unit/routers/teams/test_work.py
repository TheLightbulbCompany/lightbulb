"""Tests for the Teams Work BFF (Task 10) — Routines + Goals + Projects."""

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


@pytest.fixture
def admin(monkeypatch):
    a = MagicMock()
    for name in (
        "list_routines",
        "create_routine",
        "patch_routine",
        "delete_routine",
        "list_goals",
        "create_goal",
        "patch_goal",
        "list_projects",
        "get_project",
        "create_project",
        "patch_project",
    ):
        setattr(a, name, AsyncMock(return_value={"ok": True}))
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: a)
    return a


def test_list_routines(client, admin):
    r = client.get("/api/v1/teams/routines", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_routines.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_create_routine(client, admin):
    r = client.post(
        "/api/v1/teams/routines",
        json={
            "name": "nightly",
            "cron": "0 0 * * *",
            "agent_id": "a1",
            "prompt": "Run nightly checks",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_routine.await_args.kwargs["body"]
    assert body["name"] == "nightly"
    assert body["cron"] == "0 0 * * *"


def test_create_routine_rejects_extra(client):
    """``CreateRoutineBody`` is extra="forbid" — unknown keys must 422."""
    r = client.post(
        "/api/v1/teams/routines",
        json={
            "name": "x",
            "cron": "x",
            "agent_id": "a",
            "prompt": "p",
            "evil": 1,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_list_goals(client, admin):
    r = client.get("/api/v1/teams/goals", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_goals.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


@pytest.mark.parametrize(
    "path,upstream_method,key",
    [
        ("/api/v1/teams/goals", "list_goals", "goals"),
        ("/api/v1/teams/routines", "list_routines", "routines"),
        ("/api/v1/teams/projects", "list_projects", "projects"),
    ],
)
def test_list_endpoints_wrap_bare_array_upstream(client, monkeypatch, path, upstream_method, key):
    """Upstream returns bare JSON arrays for goals / routines / projects;
    each frontend panel reads ``data?.<key>``. Without server-side
    wrapping the panel silently shows "No <key> yet" even when rows
    exist. Regression guard for the envelope contract.
    """
    a = MagicMock()
    setattr(
        a,
        upstream_method,
        AsyncMock(return_value=[{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}]),
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: a)

    r = client.get(path, headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == [key]
    assert len(body[key]) == 2
    assert body[key][0]["name"] == "alpha"


def test_create_project(client, admin):
    r = client.post(
        "/api/v1/teams/projects",
        json={"name": "Q3", "description": "Quarter"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_project.await_args.kwargs["body"]
    assert body == {"name": "Q3", "description": "Quarter"}


def test_get_project(client, admin):
    r = client.get(
        "/api/v1/teams/projects/pr_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_project.assert_awaited_once_with(
        project_id="pr_1",
        session_cookie="cookie",
    )
