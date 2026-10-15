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
            patch.object(mod.ideas, "list_ideas", AsyncMock(return_value={"ideas": [], "totalLow": 0})),
            patch.object(mod.ideas, "start", AsyncMock(return_value={"roomId": "r", "projectId": "p"})) as start,
            patch.object(mod.ideas, "dismiss", AsyncMock()) as dismiss,
        ):
            assert client.get("/api/v1/ideas").json() == {"ideas": [], "totalLow": 0}
            assert client.post("/api/v1/ideas/books-cleanup/start").json() == {"roomId": "r", "projectId": "p"}
            assert client.post("/api/v1/ideas/books-cleanup/dismiss").status_code == 204
        start.assert_awaited_once_with(auth, "books-cleanup")
        dismiss.assert_awaited_once_with(auth, "books-cleanup")
    finally:
        app.dependency_overrides.clear()
