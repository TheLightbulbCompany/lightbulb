"""Tests for the marketplace seller profile router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


CALLER_ID = "user_caller"


@pytest.fixture
def client_with_auth():
    """TestClient with auth dependency stubbed to a fixed CALLER_ID."""
    from main import app

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=CALLER_ID)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    from main import app

    app.dependency_overrides.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_me_unauthenticated_returns_401_or_403(unauth_client):
    resp = unauth_client.get("/api/v1/marketplace/seller/me")
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_sellers.seller_profile.get_profile", new_callable=AsyncMock)
def test_get_me_returns_404_when_no_profile(mock_get, client_with_auth):
    mock_get.return_value = None
    resp = client_with_auth.get("/api/v1/marketplace/seller/me")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "seller_profile_required"


@patch("routers.marketplace_sellers.seller_profile.get_profile", new_callable=AsyncMock)
def test_get_me_returns_profile(mock_get, client_with_auth):
    mock_get.return_value = {
        "seller_id": CALLER_ID,
        "handle": "aden",
        "display_name": "Aden Kim",
        "bio": "builds agents",
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }
    resp = client_with_auth.get("/api/v1/marketplace/seller/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["handle"] == "aden"
    assert body["display_name"] == "Aden Kim"


@patch("routers.marketplace_sellers.seller_profile.upsert_profile", new_callable=AsyncMock)
def test_put_me_creates_profile(mock_upsert, client_with_auth):
    mock_upsert.return_value = {
        "seller_id": CALLER_ID,
        "handle": "aden",
        "display_name": None,
        "bio": None,
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }
    resp = client_with_auth.put(
        "/api/v1/marketplace/seller/me",
        json={"handle": "aden"},
    )
    assert resp.status_code == 200
    mock_upsert.assert_called_once()
    kwargs = mock_upsert.call_args.kwargs
    assert kwargs["seller_id"] == CALLER_ID
    assert kwargs["handle"] == "aden"


@patch("routers.marketplace_sellers.seller_profile.upsert_profile", new_callable=AsyncMock)
def test_put_me_translates_format_to_400(mock_upsert, client_with_auth):
    from core.services import seller_profile

    mock_upsert.side_effect = seller_profile.HandleFormatError("bad format")
    resp = client_with_auth.put(
        "/api/v1/marketplace/seller/me",
        json={"handle": "ade"},  # passes pydantic min_length but raises service-side
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "handle_format"


@patch("routers.marketplace_sellers.seller_profile.upsert_profile", new_callable=AsyncMock)
def test_put_me_translates_reserved_to_409(mock_upsert, client_with_auth):
    from core.services import seller_profile

    mock_upsert.side_effect = seller_profile.HandleReservedError("reserved")
    resp = client_with_auth.put(
        "/api/v1/marketplace/seller/me",
        json={"handle": "isol8"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "handle_reserved"


@patch("routers.marketplace_sellers.seller_profile.upsert_profile", new_callable=AsyncMock)
def test_put_me_translates_taken_to_409(mock_upsert, client_with_auth):
    from core.services import seller_profile

    mock_upsert.side_effect = seller_profile.HandleTakenError("taken")
    resp = client_with_auth.put(
        "/api/v1/marketplace/seller/me",
        json={"handle": "aden"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "handle_taken"


@patch("routers.marketplace_sellers.seller_profile.get_profile_by_handle", new_callable=AsyncMock)
def test_check_handle_returns_available_when_free(mock_query, client_with_auth):
    mock_query.return_value = None
    resp = client_with_auth.get("/api/v1/marketplace/seller/check-handle?h=aden")
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "reason": None}


@patch("routers.marketplace_sellers.seller_profile.get_profile_by_handle", new_callable=AsyncMock)
def test_check_handle_returns_taken_when_other_owns_it(mock_query, client_with_auth):
    mock_query.return_value = {"seller_id": "user_other", "handle": "aden"}
    resp = client_with_auth.get("/api/v1/marketplace/seller/check-handle?h=aden")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "taken"}


@patch("routers.marketplace_sellers.seller_profile.get_profile_by_handle", new_callable=AsyncMock)
def test_check_handle_returns_available_for_self_owner(mock_query, client_with_auth):
    mock_query.return_value = {"seller_id": CALLER_ID, "handle": "aden"}
    resp = client_with_auth.get("/api/v1/marketplace/seller/check-handle?h=aden")
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "reason": None}


def test_check_handle_format_rejected_without_ddb_call(client_with_auth):
    """A bad format should short-circuit before any DDB lookup runs."""
    with patch(
        "routers.marketplace_sellers.seller_profile.get_profile_by_handle",
        new_callable=AsyncMock,
    ) as q:
        resp = client_with_auth.get("/api/v1/marketplace/seller/check-handle?h=AB")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "format"}
    q.assert_not_called()


def test_check_handle_reserved(client_with_auth):
    with patch(
        "routers.marketplace_sellers.seller_profile.get_profile_by_handle",
        new_callable=AsyncMock,
    ):
        resp = client_with_auth.get("/api/v1/marketplace/seller/check-handle?h=isol8")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "reserved"}


@patch("routers.marketplace_sellers.seller_profile.get_profile_by_handle", new_callable=AsyncMock)
def test_public_lookup_by_handle_returns_no_seller_id(mock_query, unauth_client):
    mock_query.return_value = {
        "seller_id": CALLER_ID,
        "handle": "aden",
        "display_name": "Aden Kim",
        "bio": "builds agents",
        "created_at": "2026-05-17T00:00:00Z",
    }
    resp = unauth_client.get("/api/v1/marketplace/seller/handle/aden")
    assert resp.status_code == 200
    body = resp.json()
    assert "seller_id" not in body
    assert body["handle"] == "aden"
    assert body["display_name"] == "Aden Kim"


@patch("routers.marketplace_sellers.seller_profile.get_profile_by_handle", new_callable=AsyncMock)
def test_public_lookup_404(mock_query, unauth_client):
    mock_query.return_value = None
    resp = unauth_client.get("/api/v1/marketplace/seller/handle/aden")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# require_org_admin gate on seller-handle upsert
# ---------------------------------------------------------------------------


@pytest.fixture
def org_member_client():
    """TestClient authed as a non-admin org member.

    The seller handle is org-shared (one org → one marketplace identity),
    so a non-admin org member cannot claim it.
    """
    from main import app

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_test",
        org_id="org_test",
        org_role="org:basic_member",
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("routers.marketplace_sellers.seller_profile.upsert_profile", new_callable=AsyncMock)
def test_put_me_non_admin_org_member_403(mock_upsert, org_member_client):
    resp = org_member_client.put(
        "/api/v1/marketplace/seller/me",
        json={"handle": "aden"},
    )
    assert resp.status_code == 403
    mock_upsert.assert_not_called()
