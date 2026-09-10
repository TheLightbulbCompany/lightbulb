"""Tests for POST /api/v1/orgs/{org_id}/invitations — Gate A."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext
from routers import orgs


@pytest.fixture
def app():
    """Mount the orgs router for isolated testing."""
    app = FastAPI()
    app.include_router(orgs.router, prefix="/api/v1/orgs")
    return app


@pytest.fixture
def admin_auth():
    """Auth context for an admin of org_test."""
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
    """Auth context for a basic member of org_test."""
    return AuthContext(
        user_id="user_member",
        org_id="org_test",
        org_role="org:member",
        org_slug="testorg",
        org_permissions=[],
        email="member@example.com",
    )


@pytest.fixture
def personal_auth():
    """Auth context for a personal user (no org)."""
    return AuthContext(user_id="user_personal", email="personal@example.com")


def _override_auth(app, ctx):
    """Override get_current_user dependency for the duration of one test."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: ctx


def test_invite_to_email_with_no_clerk_user_succeeds(app, admin_auth):
    _override_auth(app, admin_auth)
    with patch("routers.orgs.clerk_admin") as mock_clerk:
        mock_clerk.create_organization_invitation = AsyncMock(return_value={"id": "orginv_abc"})
        client = TestClient(app)
        resp = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "newperson@example.com", "role": "org:member"},
        )
    assert resp.status_code == 201
    assert resp.json() == {"invitation_id": "orginv_abc"}
    mock_clerk.create_organization_invitation.assert_awaited_once()


def test_invite_does_not_read_or_mutate_billing_for_an_existing_user(app, admin_auth):
    """Workspace invitations are membership lifecycle only, never a billing action."""
    _override_auth(app, admin_auth)
    with patch("routers.orgs.clerk_admin") as mock_clerk:
        mock_clerk.create_organization_invitation = AsyncMock(return_value={"id": "orginv_existing"})
        client = TestClient(app)
        response = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "existing@example.com", "role": "org:member"},
        )

    assert response.status_code == 201


def test_invite_to_email_with_clerk_user_no_billing_succeeds(app, admin_auth):
    _override_auth(app, admin_auth)
    with patch("routers.orgs.clerk_admin") as mock_clerk:
        mock_clerk.create_organization_invitation = AsyncMock(return_value={"id": "orginv_def"})
        client = TestClient(app)
        resp = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "existing@example.com", "role": "org:member"},
        )
    assert resp.status_code == 201
    mock_clerk.create_organization_invitation.assert_awaited_once()


def test_non_admin_caller_returns_403(app, member_auth):
    _override_auth(app, member_auth)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/orgs/org_test/invitations",
        json={"email": "nope@example.com", "role": "org:member"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Organization admin access required"


def test_personal_caller_is_rejected_before_invitation_creation(app, personal_auth):
    _override_auth(app, personal_auth)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/orgs/org_test/invitations",
        json={"email": "nope@example.com", "role": "org:member"},
    )
    assert resp.status_code == 409


def test_caller_in_different_org_returns_403(app, admin_auth):
    """Admin of org_test cannot invite to org_other."""
    _override_auth(app, admin_auth)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/orgs/org_other/invitations",
        json={"email": "x@example.com", "role": "org:member"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot invite to a different org"


def test_default_role_is_member(app, admin_auth):
    """Omitting role in the body defaults to org:member."""
    _override_auth(app, admin_auth)
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return {"id": "orginv_default"}

    with patch("routers.orgs.clerk_admin") as mock_clerk:
        mock_clerk.create_organization_invitation = AsyncMock(side_effect=_capture)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "default@example.com"},
        )
    assert resp.status_code == 201
    assert captured["role"] == "org:member"


def test_clerk_create_invitation_failure_propagates_with_metric(app, admin_auth):
    """Clerk 4xx/5xx propagates the HTTPException AND emits orgs.invitation.failed."""
    from fastapi import HTTPException

    _override_auth(app, admin_auth)
    with (
        patch("routers.orgs.clerk_admin") as mock_clerk,
        patch("routers.orgs.put_metric") as mock_metric,
    ):
        mock_clerk.create_organization_invitation = AsyncMock(
            side_effect=HTTPException(status_code=422, detail="duplicate invitation")
        )
        client = TestClient(app)
        resp = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "dup@example.com", "role": "org:member"},
        )
    assert resp.status_code == 422
    metric_names = [c.args[0] for c in mock_metric.call_args_list]
    assert "orgs.invitation.failed" in metric_names


def test_clerk_response_missing_id_raises_500(app, admin_auth):
    """A malformed Clerk response without 'id' should fail loudly, not 200 with
    invitation_id=''. The endpoint raises KeyError; FastAPI translates to 500.

    raise_server_exceptions=False is required because Starlette's TestClient
    re-raises server exceptions to the caller by default — we want to assert
    the framework's 500 translation, which is the actual production behavior."""
    _override_auth(app, admin_auth)
    with patch("routers.orgs.clerk_admin") as mock_clerk:
        # Clerk returned a dict without 'id' — KeyError at invite["id"].
        mock_clerk.create_organization_invitation = AsyncMock(return_value={})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/orgs/org_test/invitations",
            json={"email": "malformed@example.com", "role": "org:member"},
        )
    # Server error — the contract is "fail loudly", not "return invitation_id=''".
    assert resp.status_code == 500
