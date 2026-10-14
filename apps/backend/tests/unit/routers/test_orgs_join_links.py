"""Workspace join links — /api/v1/orgs/{org_id}/join-link and /join.

Email invitations (back since 2026-10-08) are covered in test_orgs_invitations.py.

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

    async def put_link(
        self, org_id: str, *, token_hash: str, token_enc: str, created_by: str, only_if_absent: bool = False
    ) -> dict | None:
        if only_if_absent and org_id in self.rows:
            return None
        row = {
            "org_id": org_id,
            "token_hash": token_hash,
            "token_enc": token_enc,
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


def test_status_shows_the_current_link_but_never_the_hash(app, admin_auth, repo):
    """An admin can re-copy the live link any time — re-sharing must not require
    a regenerate (MKT385: seven mints in two days, nine stale-link 404s)."""
    _override_auth(app, admin_auth)
    client = TestClient(app)

    before = client.get("/api/v1/orgs/org_test/join-link")
    assert before.status_code == 200
    assert before.json()["exists"] is False
    assert before.json().get("join_url") is None

    minted = client.post("/api/v1/orgs/org_test/join-link").json()
    after = client.get("/api/v1/orgs/org_test/join-link").json()

    assert after["exists"] is True
    assert after["created_by"] == "user_admin"
    assert after["join_url"] == minted["join_url"]
    assert "token_hash" not in after
    assert "token_enc" not in after


def test_stored_row_holds_ciphertext_not_the_token(app, admin_auth, repo):
    """A leaked table row must still not be a usable link on its own."""
    _override_auth(app, admin_auth)
    token = _token_from(TestClient(app).post("/api/v1/orgs/org_test/join-link").json()["join_url"])

    row = repo.rows["org_test"]
    assert row["token_enc"]
    assert row["token_enc"] != token
    assert token not in str(row)


def test_status_of_a_link_minted_before_redisplay_has_no_url(app, admin_auth, repo):
    """Rows written by the hash-only code have no ciphertext: exists, not copyable."""
    _override_auth(app, admin_auth)
    repo.rows["org_test"] = {
        "org_id": "org_test",
        "token_hash": workspace_join._hash_token("legacy-token"),
        "created_by": "user_admin",
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    after = TestClient(app).get("/api/v1/orgs/org_test/join-link").json()
    assert after["exists"] is True
    assert after.get("join_url") is None


def test_status_survives_an_undecryptable_row(app, admin_auth, repo):
    """A rotated ENCRYPTION_KEY must degrade to "regenerate once", not a 500."""
    _override_auth(app, admin_auth)
    repo.rows["org_test"] = {
        "org_id": "org_test",
        "token_hash": workspace_join._hash_token("t"),
        "token_enc": "bm90LWEtZmVybmV0LXRva2Vu",
        "created_by": "user_admin",
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    resp = TestClient(app).get("/api/v1/orgs/org_test/join-link")
    assert resp.status_code == 200
    assert resp.json()["exists"] is True
    assert resp.json().get("join_url") is None


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


def test_status_survives_a_missing_encryption_key(app, admin_auth, repo):
    """No key at all (a partial local config) is a misconfiguration, not a 500 on read."""
    from core import encryption

    _override_auth(app, admin_auth)
    repo.rows["org_test"] = {
        "org_id": "org_test",
        "token_hash": workspace_join._hash_token("t"),
        "token_enc": "bm90LWEtZmVybmV0LXRva2Vu",
        "created_by": "user_admin",
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    with patch.object(encryption.settings, "ENCRYPTION_KEY", ""):
        resp = TestClient(app).get("/api/v1/orgs/org_test/join-link")
    assert resp.status_code == 200
    assert resp.json()["exists"] is True
    assert resp.json().get("join_url") is None


# --- Admin gating ------------------------------------------------------------


def test_a_member_may_copy_and_mint_the_first_link_but_not_replace_or_revoke_it(app, member_auth, repo):
    """D5: members copy the link. Replacing one revokes it for everyone holding it, so that stays admin-only."""
    _override_auth(app, member_auth)
    client = TestClient(app)

    assert client.get("/api/v1/orgs/org_test/join-link").json()["exists"] is False
    first = client.post("/api/v1/orgs/org_test/join-link")
    assert first.status_code == 201
    assert client.get("/api/v1/orgs/org_test/join-link").json()["join_url"] == first.json()["join_url"]

    # A second mint (another member racing, or a stale "no link" read) never replaces: it hands
    # back the live link.
    again = client.post("/api/v1/orgs/org_test/join-link")
    assert again.status_code == 201
    assert again.json()["join_url"] == first.json()["join_url"]
    assert client.delete("/api/v1/orgs/org_test/join-link").status_code == 403
    assert client.get("/api/v1/orgs/org_test/join-link").json()["join_url"] == first.json()["join_url"]


def test_a_member_cannot_replace_a_link_that_cannot_be_shown(app, member_auth, repo):
    _override_auth(app, member_auth)
    repo.rows["org_test"] = {"org_id": "org_test", "token_hash": "h", "created_by": "user_admin", "created_at": "x"}
    resp = TestClient(app).post("/api/v1/orgs/org_test/join-link")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only a workspace admin can replace the join link"
    assert repo.rows["org_test"]["token_hash"] == "h"


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


def test_redeem_lands_join_link_redeemed_on_the_new_member(app, admin_auth, repo, clerk):
    """The one PostHog fact the growth funnel needs: who joined, invited by whom, and how
    many people the workspace now has. Keyed on the joiner, never the org."""
    _override_auth(app, admin_auth)
    client = TestClient(app)
    token = _token_from(client.post("/api/v1/orgs/org_test/join-link").json()["join_url"])
    clerk.get_organization = AsyncMock(return_value={"name": "Acme", "slug": "acme", "members_count": 2})
    _override_auth(app, AuthContext(user_id="user_joiner"))
    with patch("core.services.workspace_join.capture_product_event", create=True) as capture:
        assert client.post("/api/v1/orgs/org_test/join", json={"token": token}).status_code == 200
    clerk.get_organization.assert_awaited_once_with("org_test", include_members_count=True)
    capture.assert_called_once()
    kwargs = capture.call_args.kwargs
    assert kwargs["distinct_id"] == "user_joiner"
    assert kwargs["event"] == "join_link_redeemed"
    assert kwargs["properties"] == {
        "workspace_id": "org_test",
        "status": "joined",
        "invited_by": "user_admin",
        "members_count": 2,
    }
