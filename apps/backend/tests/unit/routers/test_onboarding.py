"""Router tests for POST /api/v1/onboarding/brief.

Pattern mirrors tests/unit/routers/teams/test_work.py: override the
TeamsContext dependency, monkeypatch _admin() with a MagicMock carrying
AsyncMock methods, and monkeypatch get_workspace with a MagicMock.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext, get_current_user
from core.services.paperclip_admin_client import PaperclipApiError
import routers.onboarding as onboarding_mod
from routers.teams import agents as agents_mod

FRIDAY = {"id": "friday-uuid", "role": "ceo", "name": "Friday"}
ATLAS = {"id": "atlas-uuid", "role": "recruiter", "name": "Atlas"}


def _personal_auth():
    return AuthContext(user_id="user_abc", org_id=None, email="max@acme.com")


def _org_auth():
    return AuthContext(user_id="user_abc", org_id="org_xyz", org_role="admin", email="max@acme.com")


@pytest.fixture
def ctx():
    return SimpleNamespace(
        company_id="comp-1",
        session_cookie="cookie",
        owner_id="user_abc",
    )


@pytest.fixture
def client(ctx, monkeypatch):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[FRIDAY, ATLAS])
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=True)
    ws.write_file = MagicMock()
    monkeypatch.setattr(onboarding_mod, "get_workspace", lambda: ws)

    get_billing = AsyncMock(return_value={"subscription_status": "trial_local"})
    monkeypatch.setattr(onboarding_mod.billing_repo, "get_by_owner_id", get_billing)

    app.dependency_overrides[get_current_user] = _personal_auth
    app.dependency_overrides[onboarding_mod._ctx] = lambda: ctx
    with TestClient(app) as c:
        c.admin = admin
        c.ws = ws
        c.billing = get_billing
        yield c
    app.dependency_overrides.clear()


def test_fresh_submit_stages_files_without_seeding_a_board_issue(client):
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com", "detail": "Austin builds"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "staged", "friday_agent_id": "friday-uuid"}

    brief_call = client.ws.write_file_if_missing.call_args
    assert brief_call.args[0] == "user_abc"
    assert brief_call.args[1] == "workspaces/friday-uuid/BRIEF.md"
    assert "https://acme.com" in brief_call.args[2]

    # The board is no longer seeded on submit — Friday fills it at the END of
    # her research session with the three next-move tasks she proposes.
    client.admin.create_issue.assert_not_called()


def test_replay_returns_already_submitted(client):
    client.ws.write_file_if_missing.return_value = False
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_submitted"}
    client.ws.write_file.assert_not_called()


def test_org_context_rejected(client):
    app.dependency_overrides[get_current_user] = _org_auth
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "personal_only"


def test_non_trial_billing_rejected(client):
    client.billing.return_value = {"subscription_status": "active"}
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "trial_only"
    client.admin.list_agents.assert_not_awaited()
    client.ws.write_file_if_missing.assert_not_called()


def test_no_friday_yet_returns_409(client):
    client.admin.list_agents.return_value = [ATLAS]
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.ws.write_file_if_missing.assert_not_called()


def test_paperclip_error_returns_retryable_409(client):
    client.admin.list_agents.side_effect = PaperclipApiError("paperclip down", status_code=503, body=None)
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "new", "input": "an agency"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_ready"
    client.ws.write_file_if_missing.assert_not_called()


def test_submit_captures_analytics_event(client, monkeypatch):
    capture = MagicMock()
    monkeypatch.setattr(onboarding_mod, "capture_product_event", capture)
    resp = client.post(
        "/api/v1/onboarding/brief",
        json={"path": "grow", "input": "https://acme.com"},
    )
    assert resp.status_code == 200
    capture.assert_called_once()
    kwargs = capture.call_args.kwargs
    assert kwargs["event"] == "first_run_brief_submitted"
    assert kwargs["properties"] == {"path": "grow", "no_idea": False}


# --- "I don't know yet — you pick": submit with no input ---------------------


def test_new_path_without_input_stages_a_pick_one_brief(client):
    """The absent idea IS the signal — Friday is told to choose one."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "staged"

    brief = client.ws.write_file_if_missing.call_args.args[2]
    assert "don't have an\n  idea yet" in brief
    assert "job zero" in brief
    # Her only lead is who they are — the brief must still carry it.
    assert "max@acme.com" in brief


def test_new_path_with_blank_input_is_treated_as_no_idea(client):
    """A client posting "" must not produce a brief quoting an empty string."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "   "})
    assert resp.status_code == 200

    brief = client.ws.write_file_if_missing.call_args.args[2]
    assert "job zero" in brief


def test_no_idea_submit_is_flagged_in_analytics(client, monkeypatch):
    capture = MagicMock()
    monkeypatch.setattr(onboarding_mod, "capture_product_event", capture)
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new"})
    assert resp.status_code == 200
    assert capture.call_args.kwargs["properties"] == {"path": "new", "no_idea": True}


def test_grow_path_still_requires_input(client):
    """Only `new` may omit the idea — a grow brief with no website is useless."""
    resp = client.post("/api/v1/onboarding/brief", json={"path": "grow"})
    assert resp.status_code == 422
    client.ws.write_file_if_missing.assert_not_called()


def test_too_short_input_still_rejected(client):
    resp = client.post("/api/v1/onboarding/brief", json={"path": "new", "input": "ab"})
    assert resp.status_code == 422
    client.ws.write_file_if_missing.assert_not_called()
