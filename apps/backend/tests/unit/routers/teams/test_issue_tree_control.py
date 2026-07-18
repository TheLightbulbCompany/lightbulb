# apps/backend/tests/unit/routers/teams/test_issue_tree_control.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from main import app
from routers.teams.deps import TeamsContext

_HOLD_ID = "11111111-2222-3333-4444-555555555555"


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


def _mock_admin(monkeypatch):
    admin = MagicMock()
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    return admin


def test_preview_forwards_body_aliased_and_returns_verbatim(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.preview_issue_tree_control = AsyncMock(
        return_value={"mode": "pause", "totals": {"affectedIssues": 3, "activeRuns": 1}}
    )
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-control/preview",
        json={"mode": "pause"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"mode": "pause", "totals": {"affectedIssues": 3, "activeRuns": 1}}
    admin.preview_issue_tree_control.assert_awaited_once_with(
        session_cookie="cookie", issue_id="iss1", body={"mode": "pause"}
    )


def test_preview_forwards_nested_release_policy_camelcased(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.preview_issue_tree_control = AsyncMock(return_value={"mode": "pause"})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-control/preview",
        json={"mode": "pause", "releasePolicy": {"strategy": "after_active_runs_finish", "note": "cooldown"}},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.preview_issue_tree_control.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        body={"mode": "pause", "releasePolicy": {"strategy": "after_active_runs_finish", "note": "cooldown"}},
    )


def test_preview_rejects_bad_mode_422(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.preview_issue_tree_control = AsyncMock(return_value={})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-control/preview",
        json={"mode": "nuke"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.preview_issue_tree_control.assert_not_awaited()


def test_preview_rejects_smuggled_field_422(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.preview_issue_tree_control = AsyncMock(return_value={})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-control/preview",
        json={"mode": "pause", "adapterType": "openclaw_gateway"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.preview_issue_tree_control.assert_not_awaited()


def test_create_hold_forwards_and_returns_verbatim(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.create_issue_tree_hold = AsyncMock(
        return_value={"hold": {"id": _HOLD_ID, "mode": "cancel"}, "preview": {"totals": {}}}
    )
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-holds",
        json={"mode": "cancel", "reason": "runaway loop"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["hold"]["id"] == _HOLD_ID
    admin.create_issue_tree_hold.assert_awaited_once_with(
        session_cookie="cookie", issue_id="iss1", body={"mode": "cancel", "reason": "runaway loop"}
    )


def test_create_hold_rejects_restore_mode_422(client, monkeypatch):
    # ``restore`` + ``metadata.wakeAgents`` spawns heartbeat.wakeup agent runs
    # (inference spend) that the BFF forwards ungated. The mode whitelist drops
    # ``restore`` so it 422s at the FastAPI boundary before reaching upstream.
    admin = _mock_admin(monkeypatch)
    admin.create_issue_tree_hold = AsyncMock(return_value={})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-holds",
        json={"mode": "restore"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.create_issue_tree_hold.assert_not_awaited()


def test_create_hold_rejects_metadata_wakeagents_422(client, monkeypatch):
    # ``metadata`` is a forbidden extra field on CreateTreeHoldBody — no
    # ``wakeAgents`` can reach upstream even paired with an accepted mode.
    admin = _mock_admin(monkeypatch)
    admin.create_issue_tree_hold = AsyncMock(return_value={})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-holds",
        json={"mode": "pause", "metadata": {"wakeAgents": True}},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.create_issue_tree_hold.assert_not_awaited()


def test_preview_rejects_restore_mode_422(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.preview_issue_tree_control = AsyncMock(return_value={})
    r = client.post(
        "/api/v1/teams/issues/iss1/tree-control/preview",
        json={"mode": "restore"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.preview_issue_tree_control.assert_not_awaited()


def test_list_holds_forwards_query_and_envelopes(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.list_issue_tree_holds = AsyncMock(return_value=[{"id": _HOLD_ID, "mode": "pause"}])
    r = client.get(
        "/api/v1/teams/issues/iss1/tree-holds?status=active&mode=pause&includeMembers=true",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"holds": [{"id": _HOLD_ID, "mode": "pause"}]}
    admin.list_issue_tree_holds.assert_awaited_once_with(
        session_cookie="cookie",
        issue_id="iss1",
        params={"status": "active", "mode": "pause", "includeMembers": "true"},
    )


def test_get_hold_rejects_bad_uuid_400(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.get_issue_tree_hold = AsyncMock(return_value={})
    r = client.get(
        "/api/v1/teams/issues/iss1/tree-holds/not-a-uuid",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400
    admin.get_issue_tree_hold.assert_not_awaited()


def test_release_hold_forwards(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.release_issue_tree_hold = AsyncMock(return_value={"id": _HOLD_ID, "status": "released"})
    r = client.post(
        f"/api/v1/teams/issues/iss1/tree-holds/{_HOLD_ID}/release",
        json={"reason": "resolved"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.release_issue_tree_hold.assert_awaited_once_with(
        session_cookie="cookie", issue_id="iss1", hold_id=_HOLD_ID, body={"reason": "resolved"}
    )


def test_state_forwards_and_returns_verbatim(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.get_issue_tree_control_state = AsyncMock(
        return_value={"activePauseHold": {"holdId": _HOLD_ID, "isRoot": True}}
    )
    r = client.get(
        "/api/v1/teams/issues/iss1/tree-control/state",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"activePauseHold": {"holdId": _HOLD_ID, "isRoot": True}}
    admin.get_issue_tree_control_state.assert_awaited_once_with(session_cookie="cookie", issue_id="iss1")


def test_subtree_diagnostics_forwards(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.get_issue_subtree_diagnostics = AsyncMock(return_value={"nodeCount": 5, "edges": []})
    r = client.get(
        "/api/v1/teams/issues/iss1/diagnostics/subtree",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"nodeCount": 5, "edges": []}
    admin.get_issue_subtree_diagnostics.assert_awaited_once_with(session_cookie="cookie", issue_id="iss1")


def test_blocker_diagnostics_forwards(client, monkeypatch):
    admin = _mock_admin(monkeypatch)
    admin.get_issue_blocker_diagnostics = AsyncMock(return_value={"blockers": [], "readiness": "ready"})
    r = client.get(
        "/api/v1/teams/issues/iss1/diagnostics/blockers",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json() == {"blockers": [], "readiness": "ready"}
    admin.get_issue_blocker_diagnostics.assert_awaited_once_with(session_cookie="cookie", issue_id="iss1")
