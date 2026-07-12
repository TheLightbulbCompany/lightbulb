"""Tests for the Teams Work BFF — Routines + Goals + Projects."""

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
        "create_routine_trigger",
        "patch_routine",
        "delete_routine",
        "run_routine",
        "list_routine_runs",
        "list_goals",
        "get_goal",
        "create_goal",
        "patch_goal",
        "delete_goal",
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
    """Create body mirrors upstream ``createRoutineSchema``: a titled,
    assignable routine row — cron is NOT here (it's on a trigger)."""
    r = client.post(
        "/api/v1/teams/routines",
        json={
            "title": "nightly",
            "assignee_agent_id": "a1",
            "description": "Run nightly checks",
            "status": "active",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_routine.await_args.kwargs["body"]
    # camelCase emitted on the wire; cron must NOT leak into the routine body.
    assert body["title"] == "nightly"
    assert body["assigneeAgentId"] == "a1"
    assert body["description"] == "Run nightly checks"
    assert body["status"] == "active"
    assert "cron" not in body
    assert "name" not in body


def test_create_routine_rejects_extra(client):
    """``CreateRoutineBody`` is extra="forbid" — unknown keys (and the
    old ``cron``/``name`` flat fields) must 422."""
    r = client.post(
        "/api/v1/teams/routines",
        json={
            "title": "x",
            "cron": "0 0 * * *",  # cron is no longer a routine field
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_create_routine_trigger(client, admin):
    """POST /teams/routines/{id}/triggers forwards a schedule trigger
    (cron lives here, not on the routine row)."""
    r = client.post(
        "/api/v1/teams/routines/r1/triggers",
        json={"kind": "schedule", "cron_expression": "0 9 * * *", "timezone": "UTC"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.create_routine_trigger.assert_awaited_once()
    kwargs = admin.create_routine_trigger.await_args.kwargs
    assert kwargs["routine_id"] == "r1"
    body = kwargs["body"]
    assert body["kind"] == "schedule"
    assert body["cronExpression"] == "0 9 * * *"
    assert body["timezone"] == "UTC"


def test_create_routine_trigger_rejects_non_schedule(client, admin):
    """The trigger body is schedule-only — a webhook kind 422s."""
    r = client.post(
        "/api/v1/teams/routines/r1/triggers",
        json={"kind": "webhook", "cron_expression": "0 9 * * *"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_run_routine_proxies_upstream(client, monkeypatch):
    """POST /teams/routines/{id}/run proxies upstream + forwards body."""
    from routers.teams import agents as agents_mod
    from routers.teams import _guards as guards_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(guards_mod.trial_cap, "is_over_daily_cap", AsyncMock(return_value=False))

    r = client.post(
        "/api/v1/teams/routines/r1/run",
        json={"source": "manual"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200
    assert r.json()["runId"] == "run_test_123"
    admin.run_routine.assert_awaited_once_with(
        routine_id="r1",
        body={"source": "manual"},
        session_cookie="cookie",
    )


def test_run_routine_blocked_over_cap(client, monkeypatch):
    """A trial_local owner over the free-tier $3/day Meta cap gets a 402
    with the structured meta_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (manually firing a routine dispatches a run
    immediately server-side, so the check must run ahead of the proxy
    call to actually stop the spend)."""
    from routers.teams import agents as agents_mod
    from routers.teams import _guards as guards_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(guards_mod.trial_cap, "is_over_daily_cap", AsyncMock(return_value=True))

    r = client.post(
        "/api/v1/teams/routines/r1/run",
        json={"source": "manual"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "meta_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.run_routine.assert_not_awaited()


def test_list_routine_runs_returns_envelope(client, monkeypatch):
    """GET /teams/routines/{id}/runs proxies upstream + wraps in {runs: [...]} envelope."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.list_routine_runs = AsyncMock(
        return_value=[
            {"id": "run_1", "status": "completed", "startedAt": "2026-05-07T10:00:00Z"},
            {"id": "run_2", "status": "running", "startedAt": "2026-05-07T11:00:00Z"},
        ]
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/routines/r1/runs",
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
    assert len(body["runs"]) == 2
    admin.list_routine_runs.assert_awaited_once_with(
        routine_id="r1",
        session_cookie="cookie",
    )


def test_list_goals(client, admin):
    r = client.get("/api/v1/teams/goals", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_goals.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_get_goal(client, admin):
    """GET /teams/goals/{id} proxies upstream GET /api/goals/{id}."""
    admin.get_goal.return_value = {
        "id": "g_1",
        "title": "Ship v1",
        "status": "active",
        "level": "company",
    }
    r = client.get(
        "/api/v1/teams/goals/g_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == "g_1"
    admin.get_goal.assert_awaited_once_with(
        goal_id="g_1",
        session_cookie="cookie",
    )


def test_get_goal_does_not_collide_with_list(client, admin):
    """The static ``/goals`` list route and the param ``/goals/{id}`` detail
    route are distinct — fetching by id must hit get_goal, not list_goals."""
    client.get("/api/v1/teams/goals/g_42", headers={"Authorization": "Bearer x"})
    admin.get_goal.assert_awaited_once_with(
        goal_id="g_42",
        session_cookie="cookie",
    )
    admin.list_goals.assert_not_awaited()


def test_delete_goal(client, admin):
    """DELETE /teams/goals/{id} proxies upstream DELETE /api/goals/{id}."""
    r = client.delete(
        "/api/v1/teams/goals/g_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.delete_goal.assert_awaited_once_with(
        goal_id="g_1",
        session_cookie="cookie",
    )


def test_patch_goal_forwards_level(client, admin):
    """PatchGoalBody accepts ``level`` (the goal-detail Level picker)."""
    r = client.patch(
        "/api/v1/teams/goals/g_1",
        json={"level": "team", "status": "active"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.patch_goal.await_args.kwargs["body"]
    assert body == {"level": "team", "status": "active"}


def test_patch_goal_rejects_extra(client):
    """``PatchGoalBody`` is extra="forbid" — unknown keys must 422."""
    r = client.patch(
        "/api/v1/teams/goals/g_1",
        json={"title": "x", "evil": 1},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_patch_goal_rejects_invalid_status(client):
    """``PatchGoalBody.status`` is constrained to GOAL_STATUSES."""
    r = client.patch(
        "/api/v1/teams/goals/g_1",
        json={"status": "in_progress"},  # not a GOAL_STATUS
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_patch_goal_rejects_invalid_level(client):
    """``PatchGoalBody.level`` is constrained to GOAL_LEVELS."""
    r = client.patch(
        "/api/v1/teams/goals/g_1",
        json={"level": "department"},  # not a GOAL_LEVEL
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


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
