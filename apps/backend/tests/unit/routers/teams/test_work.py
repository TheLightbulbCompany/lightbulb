"""Tests for the Teams Work BFF — Routines + Goals + Projects."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.services import trial_cap

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
        "get_routine",
        "run_routine",
        "list_routine_runs",
        # A2 — editable routines
        "patch_routine_trigger",
        "delete_routine_trigger",
        "rotate_routine_trigger_secret",
        "list_routine_revisions",
        "restore_routine_revision",
        "list_goals",
        "get_goal",
        "create_goal",
        "patch_goal",
        "delete_goal",
        "list_projects",
        "get_project",
        "create_project",
        "patch_project",
        "delete_project",
    ):
        setattr(a, name, AsyncMock(return_value={"ok": True}))
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: a)
    # Routine create/trigger are always-on-entitled (paid). Default these tests
    # to an entitled owner; the trial cases override this stub explicitly.
    # Without it the guard does a real DDB read.
    monkeypatch.setattr(
        "routers.teams._guards.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )
    return a


def test_list_routines(client, admin):
    r = client.get("/api/v1/teams/routines", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.list_routines.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie",
    )


def test_list_routines_hides_archived(client, admin):
    """Archived routines must not surface — delete_routine archives rather
    than deletes, so an unfiltered list would resurrect a "deleted" routine
    in the panel with controls that cannot work on it."""
    admin.list_routines = AsyncMock(
        return_value=[
            {"id": "rt_live", "title": "Live", "status": "active"},
            {"id": "rt_gone", "title": "Deleted", "status": "archived"},
            {"id": "rt_paused", "title": "Paused", "status": "paused"},
        ]
    )
    r = client.get("/api/v1/teams/routines", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["routines"]]
    assert ids == ["rt_live", "rt_paused"]


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


def test_create_routine_forwards_the_execution_policy(client, admin):
    """A routine's reviewer policy must reach Paperclip verbatim.

    ``extra="forbid"`` means an unlisted field is a 422, so the reviewer
    stages would never have reached upstream without the schema field.
    """
    policy = {
        "stages": [
            {"type": "review", "participants": [{"type": "agent", "agentId": "a2"}]},
        ],
    }
    r = client.post(
        "/api/v1/teams/routines",
        json={"title": "nightly", "assignee_agent_id": "a1", "execution_policy": policy},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.create_routine.await_args.kwargs["body"]
    assert body["executionPolicy"] == policy


def test_patch_routine_can_clear_the_execution_policy(client, admin, monkeypatch):
    """An explicit null must survive to Paperclip, or the reviewer cannot be removed."""
    r = client.patch(
        "/api/v1/teams/routines/rt_1",
        json={"execution_policy": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.patch_routine.await_args.kwargs["body"]
    assert body == {"executionPolicy": None}


def test_patch_routine_forwards_the_full_execution_policy(client, admin, monkeypatch):
    """Reviewer edits are read/merge/write in the UI, so every unrelated
    policy field in that full desired value must survive the BFF unchanged."""
    policy = {
        "mode": "normal",
        "commentRequired": False,
        "stages": [
            {"type": "review", "participants": [{"type": "agent", "agentId": "a2"}]},
            {"type": "approval", "participants": [{"type": "agent", "agentId": "a3"}]},
        ],
        "authorizationPolicy": {"mode": "restricted"},
    }
    r = client.patch(
        "/api/v1/teams/routines/rt_1",
        json={"execution_policy": policy},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = admin.patch_routine.await_args.kwargs["body"]
    assert body == {"executionPolicy": policy}


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


def test_create_trigger_body_accepts_webhook_signing():
    """``CreateRoutineTriggerBody`` un-defers the webhook kind: signing_mode /
    replay_window_sec forward to Paperclip's camelCase wire shape."""
    from routers.teams.schemas import CreateRoutineTriggerBody

    body = CreateRoutineTriggerBody(kind="webhook", signing_mode="hmac_sha256", replay_window_sec=300)
    dumped = body.model_dump(exclude_none=True, by_alias=True)
    assert dumped["kind"] == "webhook"
    assert dumped["signingMode"] == "hmac_sha256"
    assert dumped["replayWindowSec"] == 300


def test_create_routine_trigger_accepts_webhook(client, admin):
    """POST /teams/routines/{id}/triggers now creates a webhook trigger too —
    signing config forwards, and no cron/timezone leaks into the body."""
    r = client.post(
        "/api/v1/teams/routines/r1/triggers",
        json={"kind": "webhook", "signing_mode": "hmac_sha256", "replay_window_sec": 300},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert body["kind"] == "webhook"
    assert body["signingMode"] == "hmac_sha256"
    assert body["replayWindowSec"] == 300
    assert "cronExpression" not in body


def test_create_routine_trigger_rejects_invalid_kind(client, admin):
    """The trigger kind union is still closed — junk 422s locally instead of
    round-tripping to upstream."""
    r = client.post(
        "/api/v1/teams/routines/r1/triggers",
        json={"kind": "cron"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_create_routine_trigger_requires_cron_for_schedule(client, admin):
    """``kind`` defaults to schedule, so an empty body must still 422 rather
    than silently create a cron-less schedule trigger (cron_expression had to
    become Optional at the type level once ``kind`` could vary)."""
    r = client.post(
        "/api/v1/teams/routines/r1/triggers",
        json={"kind": "schedule"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_run_routine_proxies_upstream(client, monkeypatch):
    """POST /teams/routines/{id}/run proxies upstream + forwards body."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=None))

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
    """A trial_local owner over the free-tier $3/day $3/day trial cap gets a 402
    with the structured trial_daily_cap payload -- BEFORE the request ever
    reaches Paperclip (manually firing a routine dispatches a run
    immediately server-side, so the check must run ahead of the proxy
    call to actually stop the spend)."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))

    r = client.post(
        "/api/v1/teams/routines/r1/run",
        json={"source": "manual"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402
    body = r.json()["detail"]
    assert body == {
        "blocked": True,
        "code": "trial_daily_cap",
        "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    }
    admin.run_routine.assert_not_awaited()


def test_run_routine_blocked_when_the_trial_window_has_expired(client, monkeypatch):
    """The OTHER half of the trial predicate. This guard used to check only the
    daily cap, so an owner whose 3-day window had ENDED but who was under today's
    $3 could still fire a routine — dispatching a Paperclip run (and fresh Bedrock
    spend) that a chat message would have been refused."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.run_routine = AsyncMock(return_value={"runId": "run_test_123", "status": "queued"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(trial_cap, "trial_block_reason", AsyncMock(return_value=trial_cap.TRIAL_EXPIRED))

    r = client.post(
        "/api/v1/teams/routines/r1/run",
        json={"source": "manual"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "trial_expired"
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


def test_patch_project_forwards_null_description_to_clear_it(client, admin):
    r = client.patch(
        "/api/v1/teams/projects/pr_1",
        json={"description": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert admin.patch_project.await_args.kwargs["body"] == {"description": None}


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


# ---------------------------------------------------------------------------
# Routine CREATION is always-on-entitled (paid), not just routine RUN.
#
# has_always_on_entitlement's docstring has always listed "Paperclip routines"
# among the features it gates, but no routine path enforced it: a trial owner
# could create a routine and attach a cron trigger with zero checks.
#
# The reason this matters is NOT the paywall (spend was already fenced by the
# meter Lambda's $3/day cap). It is that a trial container scales to zero on tab
# close and NO Paperclip/operator dispatch path calls start_user_service, so a
# routine a trial owner schedules for 3am would silently never fire. We were
# letting them build a broken thing.


@pytest.mark.parametrize(
    "route,body",
    [
        ("/api/v1/teams/routines", {"title": "nightly report"}),
        ("/api/v1/teams/routines/r1/triggers", {"cronExpression": "0 3 * * *"}),
    ],
)
def test_trial_owner_cannot_create_a_routine_or_attach_a_schedule(client, monkeypatch, route, body):
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.create_routine = AsyncMock(return_value={"id": "r1"})
    admin.create_routine_trigger = AsyncMock(return_value={"id": "t1"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    # trial_local => has_always_on_entitlement is False
    monkeypatch.setattr(
        "routers.teams._guards.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "trial_local"}),
    )

    r = client.post(route, json=body, headers={"Authorization": "Bearer x"})

    assert r.status_code == 402
    body = r.json()["detail"]
    assert body["code"] == "routines_require_subscription"
    assert body["blocked"] is True
    assert isinstance(body["message"], str) and body["message"]
    admin.create_routine.assert_not_awaited()
    admin.create_routine_trigger.assert_not_awaited()


@pytest.mark.parametrize(
    "route,body,method",
    [
        ("/api/v1/teams/routines", {"title": "nightly report"}, "create_routine"),
        ("/api/v1/teams/routines/r1/triggers", {"cronExpression": "0 3 * * *"}, "create_routine_trigger"),
    ],
)
def test_paid_owner_can_still_create_routines(client, monkeypatch, route, body, method):
    """The gate must not break the paying customer it exists to protect."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.create_routine = AsyncMock(return_value={"id": "r1"})
    admin.create_routine_trigger = AsyncMock(return_value={"id": "t1"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        "routers.teams._guards.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )

    r = client.post(route, json=body, headers={"Authorization": "Bearer x"})

    assert r.status_code == 200, r.text
    getattr(admin, method).assert_awaited_once()


# ---------------------------------------------------------------------------
# Re-ACTIVATION is the same lever as creation.
#
# Gating only create left a walk-around: a trial owner with a pre-existing
# routine (seeded before the gate, or created through the Paperclip catch-all
# proxy) could just flip it back on. The enable toggle PATCHes exactly
# {status: "active"} (RoutinesPanel.toggle).
#
# But pause/rename/delete MUST stay open — refusing those would trap someone
# with automation they cannot turn off. Only activation reaches for autonomy.


def _trial(monkeypatch):
    monkeypatch.setattr(
        "routers.teams._guards.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "trial_local"}),
    )


def test_trial_owner_cannot_reactivate_a_routine(client, admin, monkeypatch):
    _trial(monkeypatch)

    r = client.patch(
        "/api/v1/teams/routines/r1",
        json={"status": "active"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.patch_routine.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"status": "paused"},
        {"title": "renamed"},
        {"status": "archived"},
    ],
)
def test_trial_owner_can_still_pause_rename_and_archive(client, admin, monkeypatch, body):
    """The escape hatch. Never trap a trial owner with automation they can't stop."""
    _trial(monkeypatch)

    r = client.patch(
        "/api/v1/teams/routines/r1",
        json=body,
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200, r.text
    admin.patch_routine.assert_awaited_once()


def test_trial_owner_can_still_delete_a_routine(client, admin, monkeypatch):
    _trial(monkeypatch)

    r = client.delete("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})

    assert r.status_code == 200
    admin.delete_routine.assert_awaited_once()


def test_paid_owner_can_still_reactivate(client, admin, monkeypatch):
    monkeypatch.setattr(
        "routers.teams._guards.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )

    r = client.patch(
        "/api/v1/teams/routines/r1",
        json={"status": "active"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200, r.text
    admin.patch_routine.assert_awaited_once()


# ---------------------------------------------------------------------------
# The CEO heartbeat is not a routine: stopping the retired Nightly CEO routine
# must never write the heartbeat opt-out (that is the agent page's toggle now).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# A2 — editable routines: trigger edit/delete/rotate + revisions list/restore
#
# These make RoutineDetailPanel editable. The trial-gate posture mirrors the
# routine-level gates: ENABLING a trigger and RESTORING a revision are gated
# (activation-adjacent); disable/delete/rotate stay open (the escape hatch).
# ---------------------------------------------------------------------------


def test_patch_routine_trigger_forwards_camelcase(client, admin):
    """PATCH /routines/{id}/triggers/{tid} maps to the admin client with the
    trigger id and camelCase body; the routine id is UI nesting only."""
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"cron_expression": "0 6 * * *", "timezone": "America/New_York"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    kwargs = admin.patch_routine_trigger.await_args.kwargs
    assert kwargs["trigger_id"] == "tr_1"
    body = kwargs["body"]
    assert body["cronExpression"] == "0 6 * * *"
    assert body["timezone"] == "America/New_York"


def test_patch_routine_trigger_disable_is_open_to_trial(client, admin, monkeypatch):
    """Disabling a trigger (enabled=false) is the escape hatch — never gated."""
    _trial(monkeypatch)
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"enabled": False},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.patch_routine_trigger.assert_awaited_once()


def test_patch_routine_trigger_enable_is_gated_for_trial(client, admin, monkeypatch):
    """Enabling a trigger (enabled=true) arms the schedule → gated like
    reactivating a routine."""
    _trial(monkeypatch)
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"enabled": True},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.patch_routine_trigger.assert_not_awaited()


def test_patch_routine_trigger_enable_allowed_for_paid(client, admin):
    """The paid owner the gate protects can still arm a trigger."""
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"enabled": True},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.patch_routine_trigger.assert_awaited_once()


def test_patch_routine_trigger_rejects_extra(client):
    """``UpdateRoutineTriggerBody`` is extra="forbid" — smuggled fields 422."""
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"adapterType": "openclaw_gateway"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P1 — any fireability-INCREASING trigger edit is gated, not just enabled=true.
#
# The old gate checked only ``enabled is True``. A trial owner with an
# ALREADY-enabled trigger could PATCH WITHOUT ``enabled`` — retime the cron to
# fire far more often, or set ``signing_mode="none"`` — and drive more
# autonomous / externally-triggered runs straight around the gate. Retiming
# (cron/timezone) and auth-loosening (signing_mode/replay_window) are now gated
# too; only a pure disable / relabel stays open.
# ---------------------------------------------------------------------------


def test_trial_owner_cannot_retime_an_enabled_trigger_cron(client, admin, monkeypatch):
    """Retiming the schedule of an already-enabled trigger (cron, no ``enabled``)
    is fireability-increasing → gated for trial owners."""
    _trial(monkeypatch)
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"cron_expression": "*/5 * * * *"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.patch_routine_trigger.assert_not_awaited()


def test_trial_owner_cannot_change_signing_mode(client, admin, monkeypatch):
    """Loosening webhook auth (signing_mode="none") is fireability-increasing →
    gated for trial owners even without ``enabled``."""
    _trial(monkeypatch)
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"signing_mode": "none"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.patch_routine_trigger.assert_not_awaited()


def test_paid_owner_can_retime_an_enabled_trigger(client, admin):
    """The paid owner the gate protects can still retime a schedule."""
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"cron_expression": "*/5 * * * *", "timezone": "America/New_York"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.patch_routine_trigger.assert_awaited_once()


def test_trial_owner_relabel_only_is_open(client, admin, monkeypatch):
    """A pure relabel does not increase fireability → stays open (escape hatch)."""
    _trial(monkeypatch)
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"label": "renamed trigger"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.patch_routine_trigger.assert_awaited_once()


def test_delete_routine_trigger_is_open_to_trial(client, admin, monkeypatch):
    """Deleting a trigger is ungated (escape hatch)."""
    _trial(monkeypatch)
    r = client.delete(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.delete_routine_trigger.assert_awaited_once_with(
        trigger_id="tr_1",
        session_cookie="cookie",
    )


def test_rotate_routine_trigger_secret_is_open_and_returns_material(client, admin):
    """Rotate is a management action, ungated, and passes the fresh secret back."""
    admin.rotate_routine_trigger_secret = AsyncMock(
        return_value={"trigger": {"id": "tr_1"}, "secretMaterial": "whsec_new"}
    )
    r = client.post(
        "/api/v1/teams/routines/r1/triggers/tr_1/rotate-secret",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["secretMaterial"] == "whsec_new"
    admin.rotate_routine_trigger_secret.assert_awaited_once_with(
        trigger_id="tr_1",
        session_cookie="cookie",
    )


def test_list_routine_revisions_returns_envelope(client, monkeypatch):
    """GET /routines/{id}/revisions wraps the bare upstream array in {revisions}."""
    from routers.teams import agents as agents_mod

    a = MagicMock()
    a.list_routine_revisions = AsyncMock(return_value=[{"id": "rev_2"}, {"id": "rev_1"}])
    monkeypatch.setattr(agents_mod, "_admin", lambda: a)

    r = client.get(
        "/api/v1/teams/routines/r1/revisions",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["revisions"]
    assert [rev["id"] for rev in body["revisions"]] == ["rev_2", "rev_1"]
    a.list_routine_revisions.assert_awaited_once_with(
        routine_id="r1",
        session_cookie="cookie",
    )


def test_restore_revision_gated_for_trial(client, admin, monkeypatch):
    """Restore can re-arm an active routine → gated for trial owners."""
    _trial(monkeypatch)
    r = client.post(
        "/api/v1/teams/routines/r1/revisions/rev_1/restore",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.restore_routine_revision.assert_not_awaited()


def test_restore_revision_allowed_for_paid(client, admin):
    r = client.post(
        "/api/v1/teams/routines/r1/revisions/rev_1/restore",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.restore_routine_revision.assert_awaited_once_with(
        routine_id="r1",
        revision_id="rev_1",
        session_cookie="cookie",
    )


def test_restore_revision_rewrites_recreated_webhook_urls(client, admin):
    """Restoring a snapshot naming a since-deleted webhook trigger makes upstream
    RECREATE it and return one-time material as ``secretMaterials`` — an ARRAY,
    a third shape distinct from create/rotate's ``{trigger, secretMaterial}``.

    Its ``webhookUrl`` is built from the in-VPC PAPERCLIP_API_URL like every
    other upstream-minted one, so it must be swapped here too. A restore entry
    names only ``triggerId``, so the fresh public id is read back out of
    upstream's own URL — the sole place it appears in the response.
    """
    from core.config import settings

    admin.restore_routine_revision = AsyncMock(
        return_value={
            "routine": {"id": "r1"},
            "restoredFromRevisionNumber": 3,
            "secretMaterials": [
                {
                    "triggerId": "trg_1",
                    "webhookUrl": "http://paperclip.internal/api/routine-triggers/public/pub_aaa/fire",
                    "webhookSecret": "whsec_one",
                },
                {
                    "triggerId": "trg_2",
                    "webhookUrl": "http://paperclip.internal/api/routine-triggers/public/pub_bbb/fire",
                    "webhookSecret": "whsec_two",
                },
            ],
        }
    )
    r = client.post(
        "/api/v1/teams/routines/r1/revisions/rev_1/restore",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    first, second = r.json()["secretMaterials"]
    assert first["webhookUrl"] == f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/o1/pub_aaa"
    assert second["webhookUrl"] == f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/o1/pub_bbb"
    # Secrets ride through untouched — only the URL is ours to rewrite.
    assert [m["webhookSecret"] for m in (first, second)] == ["whsec_one", "whsec_two"]


def test_restore_revision_leaves_unrecognized_webhook_url_alone(client, admin):
    """Fail CLOSED on an unparseable upstream URL: a guessed fire URL pasted into
    a provider fails silently at delivery time, which is worse than an obviously
    internal one. No match → leave the value exactly as upstream sent it."""
    admin.restore_routine_revision = AsyncMock(
        return_value={
            "secretMaterials": [
                {"triggerId": "trg_1", "webhookUrl": "http://paperclip.internal/some/other/shape"},
            ]
        }
    )
    r = client.post(
        "/api/v1/teams/routines/r1/revisions/rev_1/restore",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["secretMaterials"][0]["webhookUrl"] == "http://paperclip.internal/some/other/shape"


def test_patch_routine_accepts_widened_fields(client, admin):
    """The widened routine body carries policies + variables (incl. date type)
    + env through to upstream in camelCase."""
    r = client.patch(
        "/api/v1/teams/routines/r1",
        json={
            "concurrency_policy": "skip_if_active",
            "catch_up_policy": "enqueue_missed_with_cap",
            "auto_pause_enabled": True,
            "auto_pause_threshold": 5,
            "variables": [{"name": "asOf", "type": "date", "required": True}],
            "env": {"secrets": []},
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    body = admin.patch_routine.await_args.kwargs["body"]
    assert body["concurrencyPolicy"] == "skip_if_active"
    assert body["catchUpPolicy"] == "enqueue_missed_with_cap"
    assert body["autoPauseEnabled"] is True
    assert body["autoPauseThreshold"] == 5
    assert body["variables"][0] == {"name": "asOf", "type": "date", "required": True}
    assert body["env"] == {"secrets": []}


def test_patch_routine_forwards_explicit_null_description_clear(client, admin):
    """Clearing a routine's description sends ``description: null``. The forward must
    carry the explicit null through to Paperclip (exclude_UNSET, not exclude_none) —
    exclude_none would drop the null before Paperclip saw it, silently ignoring the
    clear so the field snapped back to the old text on refresh."""
    r = client.patch(
        "/api/v1/teams/routines/r1",
        json={"description": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    body = admin.patch_routine.await_args.kwargs["body"]
    assert "description" in body
    assert body["description"] is None


def test_patch_routine_omits_description_when_not_sent(client, admin):
    """A partial PATCH that never mentions description must NOT forward it — a field
    the client left untouched stays untouched upstream. exclude_unset distinguishes
    "client sent null" (forward it) from "client omitted it" (drop it)."""
    r = client.patch(
        "/api/v1/teams/routines/r1",
        json={"title": "Just a rename"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    body = admin.patch_routine.await_args.kwargs["body"]
    assert body == {"title": "Just a rename"}
    assert "description" not in body


# ---------------------------------------------------------------------------
# P1 — routine-POLICY fireability gate (mirrors the trigger-retiming gate).
#
# Gating only ``status=="active"`` left a walk-around: switching an ALREADY-active
# routine's ``concurrency_policy`` to ``always_enqueue`` or ``catch_up_policy`` to
# ``enqueue_missed_with_cap`` raises scheduled dispatch VOLUME without touching
# ``status``. Those more-dispatching targets are now gated like activation; a
# narrowing policy edit stays open (the escape hatch).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"concurrency_policy": "always_enqueue"},
        {"catch_up_policy": "enqueue_missed_with_cap"},
    ],
)
def test_trial_owner_cannot_raise_dispatch_policy(client, admin, monkeypatch, body):
    """A trial owner switching a policy to its more-dispatching value is gated 402,
    the same as flipping status to active."""
    _trial(monkeypatch)

    r = client.patch(
        "/api/v1/teams/routines/r1",
        json=body,
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "routines_require_subscription"
    admin.patch_routine.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"concurrency_policy": "always_enqueue"},
        {"catch_up_policy": "enqueue_missed_with_cap"},
    ],
)
def test_paid_owner_can_raise_dispatch_policy(client, admin, body):
    """The paid owner the gate protects can still raise dispatch policy."""
    r = client.patch(
        "/api/v1/teams/routines/r1",
        json=body,
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    admin.patch_routine.assert_awaited_once()


@pytest.mark.parametrize(
    "body",
    [
        {"concurrency_policy": "skip_if_active"},
        {"concurrency_policy": "coalesce_if_active"},
        {"catch_up_policy": "skip_missed"},
    ],
)
def test_trial_owner_can_narrow_dispatch_policy(client, admin, monkeypatch, body):
    """Narrowing a policy to a LESS-dispatching value can only reduce autonomy, so
    it stays open to trial owners — never falsely gated."""
    _trial(monkeypatch)

    r = client.patch(
        "/api/v1/teams/routines/r1",
        json=body,
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200, r.text
    admin.patch_routine.assert_awaited_once()


def test_patch_routine_trigger_forwards_explicit_null_label_clear(client, admin):
    """Blanking a previously-set trigger label sends ``label: null``. The forward must
    carry the explicit null through to Paperclip (exclude_UNSET, not exclude_none) —
    exclude_none would drop the null before Paperclip saw it, silently ignoring the
    clear so the label snapped back on refresh."""
    r = client.patch(
        "/api/v1/teams/routines/r1/triggers/tr_1",
        json={"label": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    body = admin.patch_routine_trigger.await_args.kwargs["body"]
    assert "label" in body
    assert body["label"] is None


def test_delete_project_calls_admin(client, admin):
    resp = client.delete("/api/v1/teams/projects/proj-1")

    assert resp.status_code == 200
    admin.delete_project.assert_awaited_once_with(
        project_id="proj-1",
        session_cookie="cookie",
    )
