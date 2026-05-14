"""Unit tests for the marketplace seller-identity service.

Covers:
    * ``validate_format`` rejects too-short, too-long, illegal chars,
      uppercase, leading/trailing punctuation.
    * ``check_reserved`` blocks each reserved name.
    * ``claim_username`` on a free name writes the reservation row.
    * ``claim_username`` on a taken name raises ``SellerUsernameError('taken')``.
    * ``claim_username`` within cooldown raises ``cooldown_active``.
    * ``get_for_owner`` returns None when unclaimed, dict when claimed.
    * ``resolve_seller_display`` / ``enrich_listings_with_seller_display``.

Storage model: the seller-usernames table is the single source of truth
(no users-row mirror). The ``by-owner-id`` GSI is the reverse-lookup index;
``seller_username_repo.get_current_for_owner`` queries it. Tests mock the
repo directly.
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
    get_for_owner,
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
    async def test_happy_path_writes_reservation(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(return_value=None)
            mock_repo.claim = AsyncMock(return_value={"username_lower": "ledger"})

            result = await claim_username("user_abc", "Ledger")

        assert result["username"] == "ledger"
        assert result["last_changed_at"]
        assert result["cooldown_ends_at"]
        # Reservation row written with owner_id (not user_id).
        mock_repo.claim.assert_awaited_once()
        kwargs = mock_repo.claim.await_args.kwargs
        assert kwargs["username_lower"] == "ledger"
        assert kwargs["owner_id"] == "user_abc"

    async def test_org_owner_id_passed_through(self) -> None:
        """Codex feedback: handles attach to owner, not user. An org_*
        owner_id must be stored as the claimant on the reservation row."""
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(return_value=None)
            mock_repo.claim = AsyncMock(return_value={"username_lower": "acmeco"})

            await claim_username("org_acme", "acmeco")

        assert mock_repo.claim.await_args.kwargs["owner_id"] == "org_acme"

    async def test_taken_username_raises_taken(self) -> None:
        from core.repositories import seller_username_repo

        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(return_value=None)
            mock_repo.claim = AsyncMock(
                side_effect=seller_username_repo.UsernameTakenError("ledger"),
            )
            # Re-attach the real exception class so the service's
            # isinstance check works through the mock module.
            mock_repo.UsernameTakenError = seller_username_repo.UsernameTakenError

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "ledger")

        assert exc.value.code == "taken"

    async def test_cooldown_active_rejects(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={
                    "username_lower": "oldhandle",
                    "owner_id": "user_abc",
                    "claimed_at": recent,
                },
            )
            mock_repo.claim = AsyncMock()

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "newhandle")

        assert exc.value.code == "cooldown_active"
        assert "cooldown_ends_at" in exc.value.extra
        # Reservation must NOT have been attempted.
        mock_repo.claim.assert_not_awaited()

    async def test_idempotent_reclaim_of_same_handle(self) -> None:
        """Re-claiming the EXACT same handle (in cooldown) is a no-op,
        not a cooldown_active error -- it just returns the current
        state."""
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={
                    "username_lower": "ledger",
                    "owner_id": "user_abc",
                    "claimed_at": recent,
                },
            )
            mock_repo.claim = AsyncMock()

            result = await claim_username("user_abc", "Ledger")

        assert result["username"] == "ledger"
        assert result["last_changed_at"] == recent
        # No reservation write.
        mock_repo.claim.assert_not_awaited()

    async def test_invalid_format_rejected_before_reservation(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock()
            mock_repo.claim = AsyncMock()

            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "ab")
            assert exc.value.code == "invalid_format"

            mock_repo.claim.assert_not_awaited()
            mock_repo.get_current_for_owner.assert_not_called()  # not even read

    async def test_reserved_name_rejected_before_reservation(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.claim = AsyncMock()
            mock_repo.get_current_for_owner = AsyncMock()
            with pytest.raises(SellerUsernameError) as exc:
                await claim_username("user_abc", "isol8")
            assert exc.value.code == "reserved"
            mock_repo.claim.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_for_owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetForOwner:
    async def test_returns_none_when_unclaimed(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(return_value=None)
            result = await get_for_owner("user_abc")
        assert result is None

    async def test_returns_state_when_claimed(self) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={
                    "username_lower": "ledger",
                    "owner_id": "user_abc",
                    "claimed_at": ts,
                },
            )
            result = await get_for_owner("user_abc")
        assert result is not None
        assert result["username"] == "ledger"
        assert result["last_changed_at"] == ts
        assert result["cooldown_ends_at"]

    async def test_works_for_org_owner(self) -> None:
        """Codex feedback: org owners (no users-row) must still be able to
        own and read a handle. The GSI on owner_id makes this work without
        any user_repo dependency."""
        ts = datetime.now(timezone.utc).isoformat()
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={
                    "username_lower": "acmeco",
                    "owner_id": "org_acme",
                    "claimed_at": ts,
                },
            )
            result = await get_for_owner("org_acme")
        assert result is not None
        assert result["username"] == "acmeco"


# ---------------------------------------------------------------------------
# resolve_seller_display + enrich helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResolveSellerDisplay:
    async def test_returns_handle_when_owner_has_one(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={"username_lower": "ledger", "owner_id": "user_abc"},
            )
            assert await seller_identity.resolve_seller_display("user_abc") == "@ledger"

    async def test_returns_handle_for_org_owner(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                return_value={"username_lower": "acmeco", "owner_id": "org_acme"},
            )
            assert await seller_identity.resolve_seller_display("org_acme") == "@acmeco"

    async def test_falls_back_to_isol8_when_no_handle(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(return_value=None)
            assert (
                await seller_identity.resolve_seller_display("user_abc") == "@isol8"
            )

    async def test_falls_back_to_isol8_when_seller_id_missing(self) -> None:
        assert await seller_identity.resolve_seller_display(None) == "@isol8"

    async def test_enrich_listings_writes_field(self) -> None:
        with patch(
            "core.services.seller_identity.seller_username_repo"
        ) as mock_repo:
            mock_repo.get_current_for_owner = AsyncMock(
                side_effect=lambda owner_id: {
                    "user_a": {"username_lower": "ledger", "owner_id": "user_a"},
                    "user_b": None,
                }.get(owner_id),
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
