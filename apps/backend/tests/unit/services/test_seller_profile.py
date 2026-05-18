"""Tests for the marketplace seller profile service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import seller_profile  # noqa: E402


SELLER_ID = "user_alpha"


def test_normalize_handle_strips_and_lowercases():
    assert seller_profile.normalize_handle("  Adenkim ") == "adenkim"
    assert seller_profile.normalize_handle(None) == ""
    assert seller_profile.normalize_handle("") == ""


def test_validate_handle_accepts_canonical():
    seller_profile.validate_handle("aden-kim")
    seller_profile.validate_handle("aden_kim_99")


@pytest.mark.parametrize("bad", ["", "ab", "a" * 33, "Aden", "with space", "punct!", "no.dots"])
def test_validate_handle_rejects_format(bad):
    with pytest.raises(seller_profile.HandleFormatError):
        seller_profile.validate_handle(bad)


@pytest.mark.parametrize("reserved", ["isol8", "admin", "support", "curator", "moderator"])
def test_validate_handle_rejects_reserved(reserved):
    with pytest.raises(seller_profile.HandleReservedError):
        seller_profile.validate_handle(reserved)


def test_validate_display_name_strips_or_returns_none():
    assert seller_profile.validate_display_name(None) is None
    assert seller_profile.validate_display_name("   ") is None
    assert seller_profile.validate_display_name("  Aden  ") == "Aden"


def test_validate_display_name_rejects_overlong():
    with pytest.raises(seller_profile.SellerProfileError):
        seller_profile.validate_display_name("x" * 100)


def test_validate_bio_caps_at_280():
    assert seller_profile.validate_bio(None) is None
    assert seller_profile.validate_bio("a" * 280) == "a" * 280
    with pytest.raises(seller_profile.SellerProfileError):
        seller_profile.validate_bio("a" * 281)


def _stub_table_with_existing(item: dict | None):
    """Build a Table mock whose get_item returns {Item: item} and whose
    query (handle-index) returns the same shape under Items."""
    table = MagicMock()
    table.get_item.return_value = {"Item": item} if item else {}
    table.query.return_value = {"Items": [item] if item else []}
    table.put_item.return_value = {}
    return table


@pytest.mark.asyncio
async def test_get_profile_returns_none_for_unknown():
    table = _stub_table_with_existing(None)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        result = await seller_profile.get_profile(SELLER_ID)
    assert result is None
    table.get_item.assert_called_once_with(Key={"seller_id": SELLER_ID})


@pytest.mark.asyncio
async def test_get_profile_by_handle_uses_gsi():
    profile = {"seller_id": SELLER_ID, "handle": "aden"}
    table = _stub_table_with_existing(profile)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        result = await seller_profile.get_profile_by_handle(" ADEN ")
    assert result == profile
    args = table.query.call_args
    assert args.kwargs["IndexName"] == "handle-index"
    assert args.kwargs["ExpressionAttributeValues"] == {":h": "aden"}


@pytest.mark.asyncio
async def test_check_handle_available_true_when_free():
    table = _stub_table_with_existing(None)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        assert await seller_profile.check_handle_available("aden")


@pytest.mark.asyncio
async def test_check_handle_available_false_for_other_owner():
    other = {"seller_id": "user_other", "handle": "aden"}
    table = _stub_table_with_existing(other)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        assert not await seller_profile.check_handle_available("aden", for_seller_id=SELLER_ID)


@pytest.mark.asyncio
async def test_check_handle_available_true_for_same_owner():
    mine = {"seller_id": SELLER_ID, "handle": "aden"}
    table = _stub_table_with_existing(mine)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        assert await seller_profile.check_handle_available("aden", for_seller_id=SELLER_ID)


@pytest.mark.asyncio
async def test_check_handle_available_false_for_reserved():
    table = _stub_table_with_existing(None)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        assert not await seller_profile.check_handle_available("isol8")


@pytest.mark.asyncio
async def test_upsert_profile_creates_when_handle_free():
    table = _stub_table_with_existing(None)
    with (
        patch.object(seller_profile, "_sellers_table", return_value=table),
        patch("core.services.seller_display.invalidate_seller") as invalidate,
    ):
        result = await seller_profile.upsert_profile(
            seller_id=SELLER_ID,
            handle="aden",
            display_name="Aden Kim",
            bio="builds agents",
        )

    assert result["seller_id"] == SELLER_ID
    assert result["handle"] == "aden"
    assert result["display_name"] == "Aden Kim"
    assert result["bio"] == "builds agents"
    invalidate.assert_called_once_with(SELLER_ID)
    table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_profile_rejects_taken_handle():
    other = {"seller_id": "user_other", "handle": "aden"}
    table = _stub_table_with_existing(other)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        with pytest.raises(seller_profile.HandleTakenError):
            await seller_profile.upsert_profile(seller_id=SELLER_ID, handle="aden")


@pytest.mark.asyncio
async def test_upsert_profile_allows_same_handle_for_owner():
    mine = {
        "seller_id": SELLER_ID,
        "handle": "aden",
        "created_at": "2026-05-01T00:00:00Z",
    }
    table = _stub_table_with_existing(mine)
    with (
        patch.object(seller_profile, "_sellers_table", return_value=table),
        patch("core.services.seller_display.invalidate_seller"),
    ):
        result = await seller_profile.upsert_profile(
            seller_id=SELLER_ID,
            handle="aden",
            display_name="Aden K.",
        )

    # created_at is preserved from the existing row.
    assert result["created_at"] == "2026-05-01T00:00:00Z"
    assert result["display_name"] == "Aden K."


@pytest.mark.asyncio
async def test_upsert_profile_validates_handle():
    table = _stub_table_with_existing(None)
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        with pytest.raises(seller_profile.HandleFormatError):
            await seller_profile.upsert_profile(seller_id=SELLER_ID, handle="bad space")
        with pytest.raises(seller_profile.HandleReservedError):
            await seller_profile.upsert_profile(seller_id=SELLER_ID, handle="admin")


@pytest.mark.asyncio
async def test_get_profile_missing_table_returns_none():
    table = MagicMock()
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetItem")
    table.get_item.side_effect = err
    with patch.object(seller_profile, "_sellers_table", return_value=table):
        assert await seller_profile.get_profile(SELLER_ID) is None


@pytest.mark.asyncio
async def test_upsert_profile_uses_async_thread_for_put():
    """Ensures put_item is awaited via run_in_thread so we don't block the event loop."""
    table = _stub_table_with_existing(None)
    with (
        patch.object(seller_profile, "_sellers_table", return_value=table),
        patch("core.services.seller_display.invalidate_seller"),
        patch(
            "core.services.seller_profile.run_in_thread", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
        ) as wrapped,
    ):
        await seller_profile.upsert_profile(seller_id=SELLER_ID, handle="aden")
    # get_profile (get_item), get_profile_by_handle (query), put_item.
    assert wrapped.call_count >= 3
