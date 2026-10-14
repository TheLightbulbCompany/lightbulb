"""Workspace email invitations — /api/v1/orgs/{org_id}/invitations.

Back since 2026-10-08, next to the join link (see ``test_orgs_join_links.py``,
whose auth fixtures this mirrors). Clerk is faked at the HTTP layer with an
``httpx.MockTransport`` so the real request shapes, error mapping and the
revoke-is-idempotent confirmation all run, not a mock of the helpers.
"""

import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.services import clerk_admin, workspace_invitations
from routers import orgs

_REAL_CLIENT = httpx.AsyncClient


class FakeClerk:
    """Just enough of Clerk's org members + invitations API, in memory."""

    def __init__(self):
        self.members = [{"public_user_data": {"user_id": "user_admin", "identifier": "admin@example.com"}}]
        self.invitations: dict[str, dict] = {}
        self.fail_with: int | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"errors": [{"code": "internal_error"}]})
        if request.url.path == "/v1/users/user_admin":
            return httpx.Response(200, json={"id": "user_admin", "first_name": "Sam"})
        if request.url.path == "/v1/organizations/org_test":
            return httpx.Response(200, json={"id": "org_test", "name": "Acme & Co"})
        path = request.url.path.removeprefix("/v1/organizations/org_test")
        if path == "/memberships":
            return httpx.Response(200, json={"data": self.members, "total_count": len(self.members)})
        if path == "/invitations" and request.method == "GET":
            pending = [i for i in self.invitations.values() if i["status"] == "pending"]
            return httpx.Response(200, json={"data": pending, "total_count": len(pending)})
        if path == "/invitations" and request.method == "POST":
            body = json.loads(request.content)
            inv = {
                "id": f"orginv_{len(self.invitations) + 1}",
                "email_address": body["email_address"],
                "role": body["role"],
                "status": "pending",
                "created_at": 1_759_900_000_000 + len(self.invitations),
                "expires_at": None,
            }
            self.invitations[inv["id"]] = inv
            return httpx.Response(200, json=inv)
        inv_id = path.removeprefix("/invitations/").removesuffix("/revoke")
        inv = self.invitations.get(inv_id)
        if inv is None:
            return httpx.Response(404, json={"errors": [{"code": "resource_not_found"}]})
        if request.method == "GET":
            return httpx.Response(200, json=inv)
        if inv["status"] != "pending":
            return httpx.Response(400, json={"errors": [{"code": "organization_invitation_not_pending"}]})
        inv["status"] = "revoked"
        return httpx.Response(200, json=inv)


@pytest.fixture
def clerk():
    fake = FakeClerk()
    transport = httpx.MockTransport(fake.handler)
    with (
        patch.object(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test_xxx"),
        patch.object(clerk_admin.settings, "FRONTEND_URL", "https://app.example.com/"),
        patch.object(clerk_admin.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw)),
    ):
        yield fake


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(orgs.router, prefix="/api/v1/orgs")
    return app


@pytest.fixture
def admin_auth():
    return AuthContext(
        user_id="user_admin",
        org_id="org_test",
        org_role="org:admin",
        org_slug="testorg",
        org_permissions=["org:sys_memberships:manage"],
        email="admin@example.com",
    )


def _as(app, ctx) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: ctx
    return TestClient(app)


def test_admin_sends_an_invitation_and_the_metric_and_event_fire(app, admin_auth, clerk, caplog):
    client = _as(app, admin_auth)
    with (
        patch.object(workspace_invitations, "put_metric") as metric,
        patch.object(workspace_invitations, "capture_product_event") as capture,
        caplog.at_level("INFO"),
    ):
        resp = client.post("/api/v1/orgs/org_test/invitations", json={"email": "Cofounder@Example.com"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "cofounder@example.com"
    assert body["status"] == "pending"
    assert body["id"] == "orginv_1"
    assert body["created_at"].endswith("+00:00")
    assert body["expires_at"] is None

    sent = json.loads(clerk.requests[-1].content)
    assert sent == {
        "email_address": "cofounder@example.com",
        "inviter_user_id": "user_admin",
        "role": "org:member",
        "redirect_url": "https://app.example.com/join/invite?workspace=Acme+%26+Co&inviter=Sam",
    }
    metric.assert_called_once_with("orgs.invitation.sent")
    capture.assert_called_once_with(
        distinct_id="user_admin",
        event="invitation_sent",
        properties={"workspace_id": "org_test", "by_admin": True, "source": "unknown"},
    )
    assert "cofounder@example.com" not in "\n".join(r.getMessage() for r in caplog.records)


def test_admin_lists_pending_newest_first(app, admin_auth, clerk):
    client = _as(app, admin_auth)
    client.post("/api/v1/orgs/org_test/invitations", json={"email": "a@example.com"})
    client.post("/api/v1/orgs/org_test/invitations", json={"email": "b@example.com"})
    clerk.invitations["orginv_1"]["status"] = "accepted"
    client.post("/api/v1/orgs/org_test/invitations", json={"email": "c@example.com"})

    resp = client.get("/api/v1/orgs/org_test/invitations")
    assert resp.status_code == 200
    assert [i["email"] for i in resp.json()["invitations"]] == ["c@example.com", "b@example.com"]


def test_a_member_may_invite_but_not_list_or_revoke(app, clerk):
    """D5: the person who wants a cofounder in is often not the admin. Taking access away stays admin-only."""
    member = AuthContext(user_id="user_member", org_id="org_test", org_role="org:member", org_slug="testorg")
    client = _as(app, member)
    assert client.get("/api/v1/orgs/org_test/invitations").status_code == 403
    assert client.delete("/api/v1/orgs/org_test/invitations/orginv_1").status_code == 403
    assert clerk.requests == []

    with patch.object(workspace_invitations, "capture_product_event") as capture:
        resp = client.post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"})
    assert resp.status_code == 201
    assert json.loads(clerk.requests[-1].content)["inviter_user_id"] == "user_member"
    assert capture.call_args.kwargs["properties"]["by_admin"] is False


def test_a_failed_name_lookup_still_sends_the_invitation(app, admin_auth, clerk):
    """The names are display-only; the ticket Clerk appends is what grants anything."""
    with (
        patch.object(workspace_invitations.clerk_admin, "get_organization", return_value=None),
        patch.object(workspace_invitations.clerk_admin, "get_user", return_value=None),
    ):
        resp = _as(app, admin_auth).post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"})
    assert resp.status_code == 201
    assert json.loads(clerk.requests[-1].content)["redirect_url"] == "https://app.example.com/join/invite"


def test_admin_of_another_workspace_is_403(app, admin_auth, clerk):
    resp = _as(app, admin_auth).post("/api/v1/orgs/org_other/invitations", json={"email": "x@example.com"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot manage a different workspace"
    assert clerk.requests == []


def test_invalid_email_is_422(app, admin_auth, clerk):
    resp = _as(app, admin_auth).post("/api/v1/orgs/org_test/invitations", json={"email": "not-an-email"})
    assert resp.status_code == 422


def test_inviting_a_current_member_is_409(app, admin_auth, clerk):
    resp = _as(app, admin_auth).post("/api/v1/orgs/org_test/invitations", json={"email": "ADMIN@example.com"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "That person is already in this workspace."}
    assert clerk.invitations == {}


def test_a_duplicate_pending_invitation_is_409(app, admin_auth, clerk):
    client = _as(app, admin_auth)
    assert client.post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"}).status_code == 201
    resp = client.post("/api/v1/orgs/org_test/invitations", json={"email": "X@example.com"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "That email already has a pending invitation."}


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("already_a_member_in_organization", "That person is already in this workspace."),
        ("duplicate_record", "That email already has a pending invitation."),
    ],
)
def test_clerks_own_conflict_codes_map_to_the_same_409s(app, admin_auth, clerk, code, detail):
    """The fallback for what our pre-checks cannot see (a secondary email, a race)."""
    real = clerk.handler

    def conflict(request):
        if request.method == "POST" and request.url.path.endswith("/invitations"):
            return httpx.Response(400, json={"errors": [{"code": code}]})
        return real(request)

    with patch.object(
        clerk_admin.httpx,
        "AsyncClient",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(conflict), **kw),
    ):
        resp = _as(app, admin_auth).post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": detail}


def test_a_clerk_5xx_is_a_502_never_a_fake_success(app, admin_auth, clerk):
    clerk.fail_with = 500
    client = _as(app, admin_auth)
    with patch.object(workspace_invitations, "put_metric") as metric:
        sent = client.post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"})
    assert sent.status_code == 502
    assert "x@example.com" not in sent.text
    metric.assert_not_called()
    assert client.get("/api/v1/orgs/org_test/invitations").status_code == 502


def test_revoke_is_204_and_idempotent(app, admin_auth, clerk):
    client = _as(app, admin_auth)
    inv_id = client.post("/api/v1/orgs/org_test/invitations", json={"email": "x@example.com"}).json()["id"]

    with (
        patch.object(workspace_invitations, "put_metric") as metric,
        patch.object(workspace_invitations, "capture_product_event") as capture,
    ):
        assert client.delete(f"/api/v1/orgs/org_test/invitations/{inv_id}").status_code == 204
        # Already revoked (Clerk 4xx, then GET says not pending) and never existed (404).
        assert client.delete(f"/api/v1/orgs/org_test/invitations/{inv_id}").status_code == 204
        assert client.delete("/api/v1/orgs/org_test/invitations/orginv_missing").status_code == 204

    assert clerk.invitations[inv_id]["status"] == "revoked"
    revokes = [r for r in clerk.requests if r.url.path.endswith("/revoke")]
    assert json.loads(revokes[0].content) == {"requesting_user_id": "user_admin"}
    metric.assert_called_with("orgs.invitation.revoked")
    capture.assert_called_with(
        distinct_id="user_admin", event="invitation_revoked", properties={"workspace_id": "org_test"}
    )
    assert client.get("/api/v1/orgs/org_test/invitations").json() == {"invitations": []}


def test_the_door_an_invitation_came_from_is_recorded(app, admin_auth, clerk):
    with patch.object(workspace_invitations, "capture_product_event") as capture:
        resp = _as(app, admin_auth).post(
            "/api/v1/orgs/org_test/invitations", json={"email": "s@x.com", "source": "chat"}
        )
    assert resp.status_code == 201
    assert capture.call_args.kwargs["properties"]["source"] == "chat"
    bad = _as(app, admin_auth).post(
        "/api/v1/orgs/org_test/invitations", json={"email": "s@x.com", "source": "Bad Label!"}
    )
    assert bad.status_code == 422
