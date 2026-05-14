"""Unit tests for the marketplace seller-identity service.

Covers:
    * ``validate_format`` rejects too-short, too-long, illegal chars,
      uppercase, leading/trailing punctuation.
    * ``check_reserved`` blocks each reserved name.
    * ``claim_username`` on a free name writes both tables.
    * ``claim_username`` on a taken name raises ``SellerUsernameError('taken')``.
    * ``claim_username`` within cooldown raises ``cooldown_active``.
    * ``get_for_user`` returns None when unclaimed, dict when claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import seller_identity
from core.services.seller_identity import (
    RESERVED_USERNAMES,
    SellerUsernameError,
    check_reserved,
    claim_username,
    get_for_user,
    validate_format,
)


# ---------------------------------------------------------------------------
# validate_format
# ---------------------------------------------------------------------------


class TestValidateFormat:
    @pytest.mark.parametrize("good", ["abc", "ledger", "a-b_c", "user-123", "x" * 30])
    def test_accepts_valid_handles(self, good: str) -> None:
        assert validate_format(good) == good

    def test_lowercases_and_strips(self) -> None:
        assert validate_format("  Ledger  ") == "ledger"

    @pytest.mark.parametrize(
        "bad",
        [
            "",          # empty
            "ab",        # too short (< 3)
            "x" * 31,    # too long (> 30)
            "has space", # whitespace inside
            "café",     # non-ASCII
            "Bang!",     # punctuation outside [a-z0-9_-]
            ".leading",  # leading punctuation -- still illegal char
            "trailing.", # trailing punctuation
        ],
    )
    def test_rejects_bad_format(self, bad: str) -> None:
        with pytest.raises(SellerUsernameError) as exc:
            validate_format(bad)
        assert exc.value.code == "invalid_format"

    def test_mixed_case_normalizes_to_lower(self) -> None:
        # "Caps" -> "caps" is valid after canonicalization. Sanity check
        # that the function lowercases rather than rejecting outright.
        assert validate_format("Caps") == "caps"

    def test_rejects_non_string(self) -> None:
        with pytest.raises(SellerUsernameError) as exc:
            validate_format(None)  # type: ignore[arg-type]
        assert exc.value.code == "invalid_format"


# ---------------------------------------------------------------------------
# check_reserved
# ---------------------------------------------------------------------------


class TestCheckReserved:
    @pytest.mark.parametrize("name", sorted(RESERVED_USERNAMES))
    def test_each_reserved_name_blocked(self, name: str) -> None:
        with pytest.raises(SellerUsernameError) as exc:
            check_reserved(name)
        assert exc.value.code == "reserved"

    def test_non_reserved_passes(self) -> None:
        check_reserved("ledger")  # no raise


# ---------------------------------------------------------------------------
# claim_username -- happy path + collision + cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClaimUsername:
    async def test_happy_path_writes_both_tables(self) -> None:
        with (
            patch("core.services.seller_identity.user_repo") as mock_user_repo,
            patch("core.services.seller_identity.seller_username_repo") as mock_repo,
        ):
            mock_user_repo.get_seller_username = AsyncMock(return_value=None)
            mock_user_repo.set_seller_username = AsyncMock()
            mock_repo.claim = AsyncMock(return_value={"username_lower": "ledger"})

            result = await claim_username("user_abc", "Ledger")

        assert result["username"] == "ledger"
        assert result["last_changed_at"]
        assert result["cooldown_ends_at"]
        # Reservation row written
        mock_repo.claim.assert_awaited_once()
        kwargs = mock_repo.claim.await_args.kwargs
        assert kwargs["username_lower"] == "ledger"
        assert kwargs["user_id"] == "user_abc"
        # users-row mirror updated
        mock_user_repo.set_seller_username.assert_awaited_once()
        mirror_kwargs = mock_user_repo.set_seller_username.await_args.kwargs
        assert mirror_kwargs["username"] == "ledger"

    async def test_taken_username_raises_taken(self) -> None:
        from core.repositories import seller_username_repo

        with (
            patch("core.services.seller_identity.user_repo") as mock_user_repo,
            patch("core.services.seller_identity.seller_username_repo") as mock_repo,
        ):
            mock_user_repo.get_seller_username = AsyncMock(return_value=None)
            mock_user_repo.set_seller_username = AsyncMock()
            mock_repo.claim = AsyncMock(
                side_effect=seller_username_repo.UsernameTakenError("ledger"),
            )
            # Re-attach the real exception class so the service's
            # isinstance check works through the mock module.
            mock_repo.UsernameTakenError = seller_username_repo.UsernameTakenError

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "ledger")

        assert exc.value.code == "taken"
        # No mirror write on collision.
        mock_user_repo.set_seller_username.assert_not_awaited()

    async def test_cooldown_active_rejects(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with (
            patch("core.services.seller_identity.user_repo") as mock_user_repo,
            patch("core.services.seller_identity.seller_username_repo") as mock_repo,
        ):
            mock_user_repo.get_seller_username = AsyncMock(
                return_value={
                    "seller_username": "oldhandle",
                    "seller_username_changed_at": recent,
                },
            )
            mock_user_repo.set_seller_username = AsyncMock()
            mock_repo.claim = AsyncMock()

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "newhandle")

        assert exc.value.code == "cooldown_active"
        assert "cooldown_ends_at" in exc.value.extra
        # Reservation must NOT have been attempted.
        mock_repo.claim.assert_not_awaited()
        mock_user_repo.set_seller_username.assert_not_awaited()

    async def test_idempotent_reclaim_of_same_handle(self) -> None:
        """Re-claiming the EXACT same handle (in cooldown) is a no-op,
        not a cooldown_active error -- it just returns the current
        state."""
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with (
            patch("core.services.seller_identity.user_repo") as mock_user_repo,
            patch("core.services.seller_identity.seller_username_repo") as mock_repo,
        ):
            mock_user_repo.get_seller_username = AsyncMock(
                return_value={
                    "seller_username": "ledger",
                    "seller_username_changed_at": recent,
                },
            )
            mock_user_repo.set_seller_username = AsyncMock()
            mock_repo.claim = AsyncMock()

            result = await claim_username("user_abc", "Ledger")

        assert result["username"] == "ledger"
        # No reservation write; no mirror write.
        mock_repo.claim.assert_not_awaited()
        mock_user_repo.set_seller_username.assert_not_awaited()

    async def test_invalid_format_rejected_before_reservation(self) -> None:
        with (
            patch("core.services.seller_identity.user_repo") as mock_user_repo,
            patch("core.services.seller_identity.seller_username_repo") as mock_repo,
        ):
            mock_repo.claim = AsyncMock()

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "ab")
            assert exc.value.code == "invalid_format"

            mock_repo.claim.assert_not_awaited()
            mock_user_repo.get_seller_username.assert_not_called()  # not even read

    async def test_reserved_name_rejected_before_reservation(self) -> None:
        with patch("core.services.seller_identity.seller_username_repo") as mock_repo:
            mock_repo.claim = AsyncMock()
            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "isol8")
            assert exc.value.code == "reserved"
            mock_repo.claim.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetForUser:
    async def test_returns_none_when_unclaimed(self) -> None:
        with patch("core.services.seller_identity.user_repo") as mock_user_repo:
            mock_user_repo.get_seller_username = AsyncMock(return_value=None)
            result = await get_for_user("user_abc")
        assert result is None

    async def test_returns_state_when_claimed(self) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with patch("core.services.seller_identity.user_repo") as mock_user_repo:
            mock_user_repo.get_seller_username = AsyncMock(
                return_value={
                    "seller_username": "ledger",
                    "seller_username_changed_at": ts,
                },
            )
            result = await get_for_user("user_abc")
        assert result is not None
        assert result["username"] == "ledger"
        assert result["last_changed_at"] == ts
        assert result["cooldown_ends_at"]


# ---------------------------------------------------------------------------
# resolve_seller_display + enrich helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResolveSellerDisplay:
    async def test_returns_handle_when_user_has_one(self) -> None:
        with patch("core.services.seller_identity.user_repo") as mock_user_repo:
            mock_user_repo.get_seller_username = AsyncMock(
                return_value={"seller_username": "ledger"},
            )
            assert await seller_identity.resolve_seller_display("user_abc") == "@ledger"

    async def test_falls_back_to_isol8_when_no_handle(self) -> None:
        with patch("core.services.seller_identity.user_repo") as mock_user_repo:
            mock_user_repo.get_seller_username = AsyncMock(return_value=None)
            assert (
                await seller_identity.resolve_seller_display("user_abc") == "@isol8"
            )

    async def test_falls_back_to_isol8_when_seller_id_missing(self) -> None:
        assert await seller_identity.resolve_seller_display(None) == "@isol8"

    async def test_enrich_listings_writes_field(self) -> None:
        with patch("core.services.seller_identity.user_repo") as mock_user_repo:
            mock_user_repo.get_seller_username = AsyncMock(
                side_effect=lambda uid: {
                    "user_a": {"seller_username": "ledger"},
                    "user_b": None,
                }.get(uid),
            )
            listings = [
                {"listing_id": "l1", "seller_id": "user_a"},
                {"listing_id": "l2", "seller_id": "user_b"},
                {"listing_id": "l3", "seller_id": "user_a"},
            ]
            result = await seller_identity.enrich_listings_with_seller_display(listings)

        assert result[0]["seller_username"] == "@ledger"
        assert result[1]["seller_username"] == "@isol8"
        assert result[2]["seller_username"] == "@ledger"
