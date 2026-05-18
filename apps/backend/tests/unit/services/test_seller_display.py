"""Tests for the seller-display-name resolver."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import seller_display  # noqa: E402


CURATOR_ID = "user_curator123"
OTHER_SELLER_ID = "user_seller456"


@pytest.fixture(autouse=True)
def _reset_cache():
    seller_display._reset_cache_for_tests()
    yield
    seller_display._reset_cache_for_tests()


@pytest.mark.asyncio
async def test_curator_resolves_to_isol8():
    with patch.object(
        seller_display.curator,
        "get_curator_user_id",
        new=AsyncMock(return_value=CURATOR_ID),
    ):
        name = await seller_display.resolve_display_name(CURATOR_ID)
    assert name == "isol8"


@pytest.mark.asyncio
async def test_other_seller_uses_first_last_name():
    with (
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
        name = await seller_display.resolve_display_name(OTHER_SELLER_ID)
    assert name == "Aden Kim"


@pytest.mark.asyncio
async def test_falls_back_to_email_local_part_when_name_missing():
    user = {
        "first_name": "",
        "last_name": "",
        "primary_email_address_id": "idn_1",
        "email_addresses": [{"id": "idn_1", "email_address": "jordan@acme.com"}],
    }
    with (
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(
            seller_display.clerk_admin,
            "get_user",
            new=AsyncMock(return_value=user),
        ),
    ):
        name = await seller_display.resolve_display_name(OTHER_SELLER_ID)
    assert name == "jordan"


@pytest.mark.asyncio
async def test_falls_back_to_first_email_when_primary_id_missing():
    user = {
        "first_name": "",
        "last_name": "",
        "email_addresses": [{"id": "idn_x", "email_address": "ada@example.com"}],
    }
    with (
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            seller_display.clerk_admin,
            "get_user",
            new=AsyncMock(return_value=user),
        ),
    ):
        name = await seller_display.resolve_display_name(OTHER_SELLER_ID)
    assert name == "ada"


@pytest.mark.asyncio
async def test_unknown_user_returns_fallback():
    with (
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(
            seller_display.clerk_admin,
            "get_user",
            new=AsyncMock(return_value=None),
        ),
    ):
        name = await seller_display.resolve_display_name(OTHER_SELLER_ID)
    assert name == seller_display.FALLBACK_NAME


@pytest.mark.asyncio
async def test_empty_seller_id_returns_fallback_without_clerk_call():
    get_user = AsyncMock(return_value=None)
    with patch.object(seller_display.clerk_admin, "get_user", new=get_user):
        name = await seller_display.resolve_display_name("")
    assert name == seller_display.FALLBACK_NAME
    get_user.assert_not_called()


@pytest.mark.asyncio
async def test_cache_avoids_repeat_clerk_calls():
    get_user = AsyncMock(return_value={"first_name": "Aden", "last_name": "Kim"})
    with (
        patch.object(
            seller_display.curator,
            "get_curator_user_id",
            new=AsyncMock(return_value=CURATOR_ID),
        ),
        patch.object(seller_display.clerk_admin, "get_user", new=get_user),
    ):
        await seller_display.resolve_display_name(OTHER_SELLER_ID)
        await seller_display.resolve_display_name(OTHER_SELLER_ID)
        await seller_display.resolve_display_name(OTHER_SELLER_ID)

    assert get_user.call_count == 1


@pytest.mark.asyncio
async def test_attach_seller_names_to_list_mixes_curator_and_other():
    items = [
        {"listing_id": "a", "seller_id": CURATOR_ID},
        {"listing_id": "b", "seller_id": OTHER_SELLER_ID},
        {"listing_id": "c", "seller_id": OTHER_SELLER_ID},  # cache hit
    ]
    with (
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
        result = await seller_display.attach_seller_names(items)

    assert result[0]["seller_display_name"] == "isol8"
    assert result[1]["seller_display_name"] == "Aden Kim"
    assert result[2]["seller_display_name"] == "Aden Kim"


@pytest.mark.asyncio
async def test_attach_seller_name_single_item_skips_when_present():
    item = {"listing_id": "x", "seller_id": OTHER_SELLER_ID, "seller_display_name": "Override"}
    get_user = AsyncMock(return_value={"first_name": "Should", "last_name": "NotRun"})
    with patch.object(seller_display.clerk_admin, "get_user", new=get_user):
        await seller_display.attach_seller_name(item)

    assert item["seller_display_name"] == "Override"
    get_user.assert_not_called()
