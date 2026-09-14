"""Workspace join links — /api/v1/orgs/{org_id}/join-link and /join.

Replaces test_orgs_invitations.py, which covered the removed email-invitation
endpoint.

The DynamoDB layer is swapped for an in-memory dict that stores whatever the
service hands it, so the assertions that matter — "what reaches storage is a
hash, not the token" and "the previous token stops verifying" — are made against
the real hashing/compare code rather than a mock's call args.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.services import workspace_join
from routers import orgs


class FakeJoinRepo:
    """In-memory stand-in for workspace_join_repo — one row per org."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def get_link(self, org_id: str):
        return self.rows.get(org_id)

    async def put_link(self, org_id: str, *, token_hash: str, created_by: str) -> dict:
        row = {
            "org_id": org_id,
            "token_hash": token_hash,
            "created_by": created_by,
            "created_at": "2026-09-14T00:00:00+00:00",
        }
        self.rows[org_id] = row
        return row

    async def delete_link(self, org_id: str) -> None:
        self.rows.pop(org_id, None)


@pytest.fixture
def repo():
    fake = FakeJoinRepo()
    with patch.object(workspace_join, "workspace_join_repo", fake):
        yield fake


@pytest.fixture
def clerk():
    """Patched Clerk wrapper — joins succeed, org metadata resolves."""
    with patch.object(workspace_join, "clerk_admin") as mock:
        mock.create_organization_membership = AsyncMock(return_value={"status": "joined", "membership_id": "orgmem_1"})
        mock.get_organization = AsyncMock(return_value={"name": "Acme", "slug": "acme"})
        yield mock


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


@pytest.fixture
def member_auth():
    return AuthContext(
        user_id="user_member",
        org_id="org_test",
        org_role="org:member",
        org_slug="testorg",
        email="member@example.com",
    )


@pytest.fixture
def personal_auth():
    return AuthContext(user_id="user_personal", email="personal@example.com")


def _override_auth(app, ctx):
    app.dependency_overrides[get_current_user] = lambda: ctx


def _token_from(url: str) -> str:
    """The fragment-only secret from a minted join URL (/join/{org_id}#{token})."""
    return url.rsplit("#", 1)[-1]


# --- Minting -----------------------------------------------------------------


def test_mint_returns_a_link_and_stores_only_its_hash(app, admin_auth, repo):
    """The plaintext token must never reach storage."""
    _override_auth(app, admin_auth)
    resp = TestClient(app).post("/api/v1/orgs/org_test/join-link")

    assert resp.status_code == 201
    url = resp.json()["join_url"]
    token = _token_from(url)
    assert url.endswith(f"/join/org_test#{token}")
    # ~256 bits of CSPRNG entropy, url-safe.
    assert len(token) >= 43
    assert "." not in token

    row = repo.rows["org_test"]
    assert row["token_hash"] == workspace_join._hash_token(token)
    assert token not in str(row)
    assert row["created_by"] == "user_admin"


def test_status_never_leaks_the_token_or_its_hash(app, admin_auth, repo):
    _override_auth(app, admin_auth)
    client = TestClient(app)

    before = client.get("/api/v1/orgs/org_test/join-link")
    assert before.status_code == 200
    assert before.json()["exists"] is False

    minted = client.post("/api/v1/orgs/org_test/join-link").json()
    after = client.get("/api/v1/orgs/org_test/join-link").json()

    assert after["exists"] is True
    assert after["created_by"] == "user_admin"
    assert "join_url" not in after
    assert "token_hash" not in after
    assert _token_from(minted["join_url"]) not in str(after)


def test_regenerating_invalidates_the_previous_link(app, admin_auth, repo, clerk):
    _override_auth(app, admin_auth)
    client = TestClient(app)

    old = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])
    new = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])
    assert old != new

    # One row per org, so the old hash is simply gone.
    assert len(repo.rows) == 1

    _override_auth(app, AuthContext(user_id="user_joiner"))
    dead = client.post("/api/v1/orgs/org_test/join", json={"token": old})
    assert dead.status_code == 404
    clerk.create_organization_membership.assert_not_awaited()

    alive = client.post("/api/v1/orgs/org_test/join", json={"token": new})
    assert alive.status_code == 200


def test_revoke_kills_the_link(app, admin_auth, repo, clerk):
    _override_auth(app, admin_auth)
    client = TestClient(app)
    token = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])

    assert client.delete("/api/v1/orgs/org_test/join-link").status_code == 204
    assert repo.rows == {}
    # Revoking twice is fine.
    assert client.delete("/api/v1/orgs/org_test/join-link").status_code == 204

    _override_auth(app, AuthContext(user_id="user_joiner"))
    assert client.post("/api/v1/orgs/org_test/join", json={"token": token}).status_code == 404


# --- Admin gating ------------------------------------------------------------


def test_non_admin_member_cannot_see_or_mint_the_link(app, member_auth, repo):
    _override_auth(app, member_auth)
    client = TestClient(app)

    assert client.get("/api/v1/orgs/org_test/join-link").status_code == 403
    minted = client.post("/api/v1/orgs/org_test/join-link")
    assert minted.status_code == 403
    assert minted.json()["detail"] == "Organization admin access required"
    assert client.delete("/api/v1/orgs/org_test/join-link").status_code == 403
    assert repo.rows == {}


def test_personal_caller_is_rejected_before_any_write(app, personal_auth, repo):
    """No active org -> resolve_owner_id 409s inside require_org_admin."""
    _override_auth(app, personal_auth)
    resp = TestClient(app).post("/api/v1/orgs/org_test/join-link")
    assert resp.status_code == 409
    assert repo.rows == {}


def test_admin_of_another_workspace_cannot_mint_here(app, admin_auth, repo):
    _override_auth(app, admin_auth)
    resp = TestClient(app).post("/api/v1/orgs/org_other/join-link")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot manage a different workspace"
    assert repo.rows == {}


# --- Redeeming ---------------------------------------------------------------


def test_redeem_adds_the_caller_to_the_org_as_a_plain_member(app, admin_auth, repo, clerk):
    _override_auth(app, admin_auth)
    client = TestClient(app)
    token = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])

    _override_auth(app, AuthContext(user_id="user_joiner"))
    resp = client.post("/api/v1/orgs/org_test/join", json={"token": token})

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "joined",
        "org_id": "org_test",
        "org_name": "Acme",
        "org_slug": "acme",
    }
    clerk.create_organization_membership.assert_awaited_once_with(
        org_id="org_test",
        user_id="user_joiner",
        role="org:member",
    )


def test_redeem_when_already_a_member_is_an_idempotent_success(app, admin_auth, repo, clerk):
    """The owner clicking their own link must land in the workspace, not on an error."""
    _override_auth(app, admin_auth)
    client = TestClient(app)
    token = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])

    clerk.create_organization_membership = AsyncMock(return_value={"status": "already_member", "membership_id": None})
    resp = client.post("/api/v1/orgs/org_test/join", json={"token": token})

    assert resp.status_code == 200
    assert resp.json()["status"] == "already_member"


def test_redeem_with_an_unknown_or_wrong_token_is_a_uniform_404(app, admin_auth, repo, clerk):
    """Every rejection reads the same, so the endpoint is not an oracle."""
    _override_auth(app, admin_auth)
    client = TestClient(app)
    client.post("/api/v1/orgs/org_test/join-link")

    _override_auth(app, AuthContext(user_id="user_joiner"))
    wrong_token = client.post("/api/v1/orgs/org_test/join", json={"token": "not-the-token"})
    # org that never had a link at all
    never_minted = client.post("/api/v1/orgs/org_nothing/join", json={"token": "not-the-token"})

    assert wrong_token.status_code == never_minted.status_code == 404
    assert wrong_token.json()["detail"] == never_minted.json()["detail"]
    clerk.create_organization_membership.assert_not_awaited()


def test_redeem_requires_a_token_in_the_body(app, repo, clerk):
    """The token is never a query/path param on the API host — it would be logged."""
    _override_auth(app, AuthContext(user_id="user_joiner"))
    resp = TestClient(app).post("/api/v1/orgs/org_test/join", json={"token": ""})
    assert resp.status_code == 422


def test_token_is_never_logged_on_mint_or_rejection(app, admin_auth, repo, clerk, caplog):
    _override_auth(app, admin_auth)
    client = TestClient(app)
    with caplog.at_level("INFO"):
        token = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])
        client.post("/api/v1/orgs/org_test/join", json={"token": "wrong-token-value"})

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logged
    assert "wrong-token-value" not in logged
