"""/api/v1/company/context -- the "paste your context" door."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_context as mod  # noqa: E402


def _client(role):
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_x", org_id="org_a", org_role=role)
    return TestClient(app)


@pytest.fixture
def admin():
    yield _client("org:admin")
    app.dependency_overrides.clear()


@pytest.fixture
def member():
    yield _client("org:member")
    app.dependency_overrides.clear()


def _company(starter=None, seeded=None):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(starter_agent_ids=starter, seeded_agent_ids=seeded))
    return patch.object(mod, "PaperclipRepo", return_value=repo)


def test_post_merges_into_the_ceo_profile(admin):
    merge = MagicMock(return_value={"merged": 12, "overflowed": True})
    with _company(starter={"ceo": "ceo_1", "growth": "g_1"}), patch.object(mod.context_import, "merge_context", merge):
        resp = admin.post("/api/v1/company/context", json={"text": "We sell bread"})
    assert resp.status_code == 200
    assert resp.json() == {"merged": 12, "overflowed": True}
    merge.assert_called_once_with("org_a", "ceo_1", "We sell bread")


def test_post_falls_back_to_the_first_seeded_agent(admin):
    merge = MagicMock(return_value={"merged": 1, "overflowed": False})
    with _company(seeded=["friday_1", "atlas_1"]), patch.object(mod.context_import, "merge_context", merge):
        admin.post("/api/v1/company/context", json={"text": "x"})
    assert merge.call_args.args[1] == "friday_1"


def test_post_without_a_ceo_is_409(admin):
    with _company(), patch.object(mod.context_import, "merge_context") as merge:
        resp = admin.post("/api/v1/company/context", json={"text": "x"})
    assert resp.status_code == 409
    assert "Lightbulb" in resp.json()["detail"]
    merge.assert_not_called()


def test_post_is_admin_only(member):
    with _company(starter={"ceo": "ceo_1"}), patch.object(mod.context_import, "merge_context") as merge:
        resp = member.post("/api/v1/company/context", json={"text": "x"})
    assert resp.status_code == 403
    merge.assert_not_called()


@pytest.mark.parametrize("text", ["", "   \n "])
def test_post_rejects_empty(admin, text):
    with _company(starter={"ceo": "ceo_1"}), patch.object(mod.context_import, "merge_context") as merge:
        resp = admin.post("/api/v1/company/context", json={"text": text})
    assert resp.status_code == 422
    merge.assert_not_called()


def test_post_caps_the_request_size(admin):
    with _company(starter={"ceo": "ceo_1"}), patch.object(mod.context_import, "merge_context") as merge:
        resp = admin.post("/api/v1/company/context", json={"text": "x" * (mod.MAX_CONTEXT_CHARS + 1)})
    assert resp.status_code == 422
    merge.assert_not_called()


def test_get_reports_the_marker_to_any_member(member):
    with (
        _company(starter={"ceo": "ceo_1"}),
        patch.object(mod.context_import, "has_imported_context", MagicMock(return_value=True)) as has,
    ):
        resp = member.get("/api/v1/company/context")
    assert resp.status_code == 200
    assert resp.json() == {"imported": True}
    has.assert_called_once_with("org_a", "ceo_1")
