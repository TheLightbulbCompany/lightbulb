"""/api/v1/ideas routes: Clerk auth, wired to core.services.ideas."""

import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import ideas as mod  # noqa: E402


def test_routes_need_a_signed_in_user():
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get("/api/v1/ideas").status_code in (401, 403)
    assert client.post("/api/v1/ideas/books-cleanup/start").status_code in (401, 403)


def test_routes_call_the_service_as_the_member():
    auth = AuthContext(user_id="user_x", org_id="org_a", org_role="org:member")
    app.dependency_overrides[get_current_user] = lambda: auth
    try:
        client = TestClient(app)
        with (
            patch.object(mod.ideas, "list_ideas", AsyncMock(return_value={"ideas": [], "running": []})),
            patch.object(mod.ideas, "start", AsyncMock(return_value={"roomId": "r", "projectId": "p"})) as start,
            patch.object(mod.ideas, "dismiss", AsyncMock()) as dismiss,
        ):
            assert client.get("/api/v1/ideas").json() == {"ideas": [], "running": []}
            assert client.post("/api/v1/ideas/books-cleanup/start").json() == {"roomId": "r", "projectId": "p"}
            assert client.post("/api/v1/ideas/books-cleanup/dismiss").status_code == 204
        start.assert_awaited_once_with(auth, "books-cleanup")
        dismiss.assert_awaited_once_with(auth, "books-cleanup")
    finally:
        app.dependency_overrides.clear()


SUGGESTION = {
    "agentId": "agent-g",
    "title": "Answer the open reviews",
    "why": "Fourteen reviews have no reply.",
    "deliverables": ["Every review answered"],
    "size": "task",
}


def test_suggest_idea_needs_this_owners_hook_token():
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.post("/api/v1/internal/ideas/suggestions/org_a", json=SUGGESTION).status_code == 401
    with patch(
        "core.services.container_auth.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "t-b"})
    ):
        res = client.post(
            "/api/v1/internal/ideas/suggestions/org_a", json=SUGGESTION, headers={"Authorization": "Bearer t-a"}
        )
    assert res.status_code == 401


def test_suggest_idea_stores_through_the_service():
    client = TestClient(app)
    with (
        patch("routers.ideas._require_hook_token", AsyncMock()) as guard,
        patch.object(mod.ideas, "suggest", AsyncMock(return_value={"status": "added", "id": "s-1"})) as suggest,
    ):
        res = client.post(
            "/api/v1/internal/ideas/suggestions/org_a", json=SUGGESTION, headers={"Authorization": "Bearer t"}
        )
    assert res.json() == {"status": "added", "id": "s-1"}
    guard.assert_awaited_once()
    owner, body = suggest.await_args.args
    assert owner == "org_a" and body.agent_id == "agent-g" and body.size == "task"
    bad = {**SUGGESTION, "deliverables": ["a", "b", "c", "d"]}
    with patch("routers.ideas._require_hook_token", AsyncMock()):
        assert client.post("/api/v1/internal/ideas/suggestions/org_a", json=bad).status_code == 422
