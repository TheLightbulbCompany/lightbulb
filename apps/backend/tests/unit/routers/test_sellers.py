"""Integration-style tests for the /api/v1/sellers/me router.

Uses the standard ``async_client`` fixture (Clerk-mocked auth, personal
mode user_test_123). Service-layer DDB interactions are patched so these
tests are pure-Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# GET /api/v1/sellers/me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_404_when_unclaimed(async_client) -> None:
    with patch("core.services.seller_identity.user_repo") as mock_user_repo:
        mock_user_repo.get_seller_username = AsyncMock(return_value=None)
        resp = await async_client.get("/api/v1/sellers/me")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "seller_username_not_set"


@pytest.mark.asyncio
async def test_get_me_returns_state_when_claimed(async_client) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with patch("core.services.seller_identity.user_repo") as mock_user_repo:
        mock_user_repo.get_seller_username = AsyncMock(
            return_value={
                "seller_username": "ledger",
                "seller_username_changed_at": ts,
            },
        )
        resp = await async_client.get("/api/v1/sellers/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "ledger"
    assert body["last_changed_at"] == ts
    assert body["cooldown_ends_at"]


# ---------------------------------------------------------------------------
# POST /api/v1/sellers/me/username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_claim_succeeds_on_free_handle(async_client) -> None:
    with (
        patch("core.services.seller_identity.user_repo") as mock_user_repo,
        patch("core.services.seller_identity.seller_username_repo") as mock_repo,
    ):
        mock_user_repo.get_seller_username = AsyncMock(return_value=None)
        mock_user_repo.set_seller_username = AsyncMock()
        mock_repo.claim = AsyncMock(return_value={"username_lower": "ledger"})

        resp = await async_client.post(
            "/api/v1/sellers/me/username",
            json={"username": "Ledger"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "ledger"


@pytest.mark.asyncio
async def test_post_returns_409_on_taken(async_client) -> None:
    from core.repositories import seller_username_repo

    with (
        patch("core.services.seller_identity.user_repo") as mock_user_repo,
        patch("core.services.seller_identity.seller_username_repo") as mock_repo,
    ):
        mock_user_repo.get_seller_username = AsyncMock(return_value=None)
        mock_repo.claim = AsyncMock(
            side_effect=seller_username_repo.UsernameTakenError("ledger"),
        )
        mock_repo.UsernameTakenError = seller_username_repo.UsernameTakenError

        resp = await async_client.post(
            "/api/v1/sellers/me/username",
            json={"username": "ledger"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "taken"


@pytest.mark.asyncio
async def test_post_returns_409_cooldown_with_ends_at(async_client) -> None:
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with patch("core.services.seller_identity.user_repo") as mock_user_repo:
        mock_user_repo.get_seller_username = AsyncMock(
            return_value={
                "seller_username": "oldhandle",
                "seller_username_changed_at": recent,
            },
        )

        resp = await async_client.post(
            "/api/v1/sellers/me/username",
            json={"username": "newhandle"},
        )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "cooldown_active"
    assert detail["cooldown_ends_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_handle", ["ab", "x" * 31, "has space", "B@d"])
async def test_post_returns_400_on_invalid_format(async_client, bad_handle: str) -> None:
    resp = await async_client.post(
        "/api/v1/sellers/me/username",
        json={"username": bad_handle},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_format"


@pytest.mark.asyncio
async def test_post_returns_400_on_reserved(async_client) -> None:
    resp = await async_client.post(
        "/api/v1/sellers/me/username",
        json={"username": "isol8"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "reserved"
