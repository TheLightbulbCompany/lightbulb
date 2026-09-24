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
    policy = {
        "mode": "normal",
        "stages": [
            {"type": "review", "participants": [{"type": "agent", "agentId": "ag_2"}]},
        ],
    }
    admin.get_routine = AsyncMock(
        return_value={
            "id": "r1",
            "title": "Nightly digest",
            "status": "active",
            "executionPolicy": policy,
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {
        "id": "r1",
        "title": "Nightly digest",
        "status": "active",
        "executionPolicy": policy,
    }
    admin.get_routine.assert_awaited_once_with(routine_id="r1", session_cookie="cookie")


def test_get_routine_projects_fire_url_on_webhook_triggers(client, monkeypatch):
    """Webhook triggers carry the PUBLIC fire URL; schedules carry none.

    Arming an existing trigger means pasting this URL into the provider's
    dashboard, and its shape is server-owned — upstream's own ``webhookUrl``
    points at the in-VPC Paperclip host, so the panel can only get a correct
    URL from here.
    """
    from core.config import settings

    admin = MagicMock()
    admin.get_routine = AsyncMock(
        return_value={
            "id": "r1",
            "triggers": [
                {"id": "t1", "kind": "webhook", "publicId": "pub_abc", "signingMode": "hmac_sha256"},
                {"id": "t2", "kind": "schedule", "cronExpression": "0 9 * * *", "publicId": None},
            ],
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    webhook, schedule = r.json()["triggers"]
    assert webhook["fireUrl"] == f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/o1/pub_abc"
    assert "fireUrl" not in schedule


def test_rotate_secret_replaces_upstream_in_vpc_webhook_url(client, monkeypatch):
    """The one-time ``secretMaterial.webhookUrl`` is shown to the owner as the
    value to paste into the provider — upstream mints it from the in-VPC
    PAPERCLIP_API_URL, which no provider can reach, so it is swapped here."""
    from core.config import settings

    admin = MagicMock()
    admin.rotate_routine_trigger_secret = AsyncMock(
        return_value={
            "trigger": {"id": "t1", "kind": "webhook", "publicId": "pub_abc"},
            "secretMaterial": {
                "webhookUrl": "http://paperclip.internal/api/routine-triggers/public/pub_abc/fire",
                "webhookSecret": "whsec_new",
            },
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/routines/r1/triggers/t1/rotate-secret",
        headers={"Authorization": "Bearer x"},
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["secretMaterial"]["webhookUrl"] == f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/o1/pub_abc"
    assert body["secretMaterial"]["webhookSecret"] == "whsec_new"
    assert body["trigger"]["fireUrl"] == f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/o1/pub_abc"


def test_get_routine_skips_webhook_trigger_with_no_public_id(client, monkeypatch):
    """No publicId → no fire URL. The URL is keyed by that id, so a webhook row
    without one (mid-create, or an upstream projection that omits it) has no
    address to arm; emitting a truncated one would be a silently-dead paste."""
    admin = MagicMock()
    admin.get_routine = AsyncMock(
        return_value={"id": "r1", "triggers": [{"id": "t1", "kind": "webhook", "publicId": None}]}
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert "fireUrl" not in r.json()["triggers"][0]


# ---- GET /teams/routines/{id}/activity (ported stock routinesApi.activity) ----


def _activity_admin(routine):
    admin = MagicMock()
    admin.get_routine = AsyncMock(return_value=routine)
    admin.list_routine_runs = AsyncMock(return_value=[{"id": "run_1"}, {"id": "run_2"}])
    pages = {
        ("routine", "r1"): [{"id": "e1", "createdAt": "2026-01-01T00:00:00Z"}],
        ("routine_trigger", "t1"): [
            {"id": "e2", "createdAt": "2026-01-03T00:00:00Z"},
            {"id": "e1", "createdAt": "2026-01-01T00:00:00Z"},
        ],
        ("routine_run", "run_1"): {"events": [{"id": "e3", "createdAt": "2026-01-02T00:00:00Z"}]},
        ("routine_run", "run_2"): [],
    }

    async def list_activity(*, session_cookie, company_id, entity_type, entity_id):
        return pages[(entity_type, entity_id)]

    admin.list_activity = AsyncMock(side_effect=list_activity)
    return admin


def test_routine_activity_fans_out_dedupes_and_sorts(client, monkeypatch):
    admin = _activity_admin({"id": "r1", "companyId": "co_abc", "triggers": [{"id": "t1"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert [e["id"] for e in r.json()["events"]] == ["e2", "e3", "e1"]
    # Every activity call is pinned to the caller's company.
    assert {c.kwargs["company_id"] for c in admin.list_activity.await_args_list} == {"co_abc"}
    assert {(c.kwargs["entity_type"], c.kwargs["entity_id"]) for c in admin.list_activity.await_args_list} == {
        ("routine", "r1"),
        ("routine_trigger", "t1"),
        ("routine_run", "run_1"),
        ("routine_run", "run_2"),
    }
    admin.get_routine.assert_awaited_once_with(routine_id="r1", session_cookie="cookie")


def test_routine_activity_404s_foreign_company(client, monkeypatch):
    admin = _activity_admin({"id": "r1", "companyId": "co_other", "triggers": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404
    admin.list_activity.assert_not_awaited()
    admin.list_routine_runs.assert_not_awaited()


def test_routine_activity_rejects_bad_id(client, monkeypatch):
    admin = _activity_admin({"id": "r1", "companyId": "co_abc", "triggers": []})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1.x/activity", headers={"Authorization": "Bearer x"})
    assert r.status_code == 422
    admin.get_routine.assert_not_awaited()
