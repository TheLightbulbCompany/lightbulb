"""Tests for the seller-display-name resolver layered over the new
``marketplace-sellers`` profile table."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import seller_display  # noqa: E402


CURATOR_ID = "user_curator123"
SELLER_ID = "user_seller456"


@pytest.fixture(autouse=True)
def _reset_cache():
    seller_display._reset_cache_for_tests()
    yield
    seller_display._reset_cache_for_tests()


@pytest.mark.asyncio
async def test_profile_handle_wins_over_clerk_fallback():
    profile = {"seller_id": SELLER_ID, "handle": "aden"}
    with (
        patch("core.services.seller_profile.get_profile", new=AsyncMock(return_value=profile)),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(seller_display.clerk_admin, "get_user", new=AsyncMock(return_value=None)) as clerk,
    ):
        name = await seller_display.resolve_display_name(SELLER_ID)

    assert name == "@aden"
    clerk.assert_not_called()


@pytest.mark.asyncio
async def test_curator_resolves_to_at_isol8_without_profile():
    with (
        patch("core.services.seller_profile.get_profile", new=AsyncMock(return_value=None)),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
    ):
        name = await seller_display.resolve_display_name(CURATOR_ID)
    assert name == "@isol8"


@pytest.mark.asyncio
async def test_clerk_first_last_name_fallback():
    with (
        patch("core.services.seller_profile.get_profile", new=AsyncMock(return_value=None)),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(
            seller_display.clerk_admin,
            "get_user",
            new=AsyncMock(return_value={"first_name": "Aden", "last_name": "Kim"}),
        ),
    ):
        name = await seller_display.resolve_display_name(SELLER_ID)
    assert name == "Aden Kim"


@pytest.mark.asyncio
async def test_unknown_seller_returns_community_member():
    with (
        patch("core.services.seller_profile.get_profile", new=AsyncMock(return_value=None)),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(seller_display.clerk_admin, "get_user", new=AsyncMock(return_value=None)),
    ):
        name = await seller_display.resolve_display_name(SELLER_ID)
    assert name == "Community member"


@pytest.mark.asyncio
async def test_attach_seller_names_mixes_handle_curator_and_fallback():
    profile = {"seller_id": SELLER_ID, "handle": "aden"}

    async def fake_get_profile(sid):
        return profile if sid == SELLER_ID else None

    items = [
        {"listing_id": "a", "seller_id": CURATOR_ID},
        {"listing_id": "b", "seller_id": SELLER_ID},
        {"listing_id": "c", "seller_id": SELLER_ID},  # cache hit
        {"listing_id": "d", "seller_id": "user_unknown"},
    ]

    with (
        patch("core.services.seller_profile.get_profile", new=AsyncMock(side_effect=fake_get_profile)),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(seller_display.clerk_admin, "get_user", new=AsyncMock(return_value=None)),
    ):
        result = await seller_display.attach_seller_names(items)

    assert result[0]["seller_display_name"] == "@isol8"
    assert result[1]["seller_display_name"] == "@aden"
    assert result[2]["seller_display_name"] == "@aden"
    assert result[3]["seller_display_name"] == "Community member"


@pytest.mark.asyncio
async def test_invalidate_seller_drops_cache_entry():
    profile_v1 = {"seller_id": SELLER_ID, "handle": "old"}
    profile_v2 = {"seller_id": SELLER_ID, "handle": "new"}

    fake = AsyncMock(side_effect=[profile_v1, profile_v2])
    with (
        patch("core.services.seller_profile.get_profile", new=fake),
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
    ):
        first = await seller_display.resolve_display_name(SELLER_ID)
        seller_display.invalidate_seller(SELLER_ID)
        second = await seller_display.resolve_display_name(SELLER_ID)

    assert first == "@old"
    assert second == "@new"
