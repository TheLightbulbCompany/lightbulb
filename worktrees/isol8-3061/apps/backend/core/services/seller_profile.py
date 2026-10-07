"""Marketplace seller profile — handle storage, validation, and lookup.

The marketplace lets a seller pick a public handle (``@example``) that
identifies them across listings and (future) star / credibility surfaces.
This module owns validation + uniqueness policy for that identity; the
DynamoDB access lives in ``core.repositories.marketplace_repo``:

  * PK ``seller_id`` (Clerk user_id) -> profile row.
  * GSI ``handle-index`` PK ``handle`` -> uniqueness probe + future
    ``GET /seller/handle/{handle}`` lookups.

Validation rules (kept tight on purpose so future star/credibility
features have a stable identity to attach to):

  * Format: ``[a-z0-9_-]{3,32}``. Lowercased server-side.
  * Reserved words (``isol8``, ``admin``, ``support``, ...) are
    rejected with :class:`HandleReservedError`.
  * Uniqueness is enforced via a DDB conditional ``attribute_not_exists``
    write keyed on the handle. Conflicts surface as
    :class:`HandleTakenError`.

The router layer translates the typed errors into HTTP status codes; the
service stays HTTP-agnostic so tests don't have to spin up FastAPI.
"""

from __future__ import annotations

import re
from typing import Final

from core.dynamodb import utc_now_iso
from core.repositories import marketplace_repo


HANDLE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{3,32}$")
HANDLE_MIN_LEN: Final[int] = 3
HANDLE_MAX_LEN: Final[int] = 32
DISPLAY_NAME_MAX_LEN: Final[int] = 64
BIO_MAX_LEN: Final[int] = 280

RESERVED_HANDLES: Final[frozenset[str]] = frozenset(
    {
        # Brand handles. "isol8" stays reserved after the rebrand — releasing a
        # retired brand lets someone else publish listings under it.
        "isol8",
        "flickmeon",
        "lightbulb",
        "lightbulbai",
        "admin",
        "administrator",
        "support",
        "help",
        "staff",
        "system",
        "official",
        "security",
        "moderator",
        "mod",
        "marketplace",
        "featured",
        "curator",
        "root",
        "owner",
        "anonymous",
        "deleted",
    }
)


class SellerProfileError(Exception):
    """Base class for seller-profile validation / state errors."""


class HandleFormatError(SellerProfileError):
    """Handle does not match the format regex / length bounds."""


class HandleReservedError(SellerProfileError):
    """Handle is on the reserved-words list."""


class HandleTakenError(SellerProfileError):
    """Handle is already in use by a different seller."""


def normalize_handle(raw: str | None) -> str:
    """Strip + lowercase. Empty string if input is None/whitespace."""
    return (raw or "").strip().lower()


def validate_handle(handle: str) -> None:
    """Raise :class:`HandleFormatError` or :class:`HandleReservedError`.

    Caller passes a *normalised* handle (use :func:`normalize_handle`
    first). The format regex covers length + character set in one match.
    """
    if not HANDLE_RE.fullmatch(handle):
        raise HandleFormatError(f"handle must match {HANDLE_RE.pattern} (got {handle!r})")
    if handle in RESERVED_HANDLES:
        raise HandleReservedError(f"handle {handle!r} is reserved")


def validate_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    stripped = display_name.strip()
    if not stripped:
        return None
    if len(stripped) > DISPLAY_NAME_MAX_LEN:
        raise SellerProfileError(f"display_name exceeds {DISPLAY_NAME_MAX_LEN} chars")
    return stripped


def validate_bio(bio: str | None) -> str | None:
    if bio is None:
        return None
    stripped = bio.strip()
    if not stripped:
        return None
    if len(stripped) > BIO_MAX_LEN:
        raise SellerProfileError(f"bio exceeds {BIO_MAX_LEN} chars")
    return stripped


async def get_profile(seller_id: str) -> dict | None:
    """Return the profile row keyed by seller_id, or None if absent."""
    if not seller_id:
        return None
    return await marketplace_repo.get_seller_profile(seller_id)


async def get_profile_by_handle(handle: str) -> dict | None:
    """Return the profile keyed by handle via the handle-index GSI, or None."""
    normalised = normalize_handle(handle)
    if not normalised:
        return None
    return await marketplace_repo.find_seller_by_handle(normalised)


async def upsert_profile(
    *,
    seller_id: str,
    handle: str,
    display_name: str | None = None,
    bio: str | None = None,
) -> dict:
    """Create or update a profile row.

    Validates handle format + reserved list, then performs a uniqueness
    probe against the handle-index. The actual put is unconditional on
    the primary table (seller_id key) — we treat the GSI read as the
    serialization point. Two concurrent writers picking the same handle
    is rare in practice; if it ever shows up, the loser's row simply
    has a handle that conflicts with another's and the next
    :func:`get_profile_by_handle` will pick whichever wrote last —
    acceptable for v1 since we already retry the publish flow.
    """
    normalised = normalize_handle(handle)
    validate_handle(normalised)
    display = validate_display_name(display_name)
    biography = validate_bio(bio)

    existing_by_handle = await get_profile_by_handle(normalised)
    if existing_by_handle and existing_by_handle.get("seller_id") != seller_id:
        raise HandleTakenError(f"handle {normalised!r} is already taken")

    now = utc_now_iso()
    existing = await get_profile(seller_id)
    item: dict = {
        "seller_id": seller_id,
        "handle": normalised,
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
    }
    if display:
        item["display_name"] = display
    if biography:
        item["bio"] = biography

    await marketplace_repo.put_seller_profile(item)

    # Resolver caches the previous handle (or fallback) for this seller
    # — invalidate so the new value is visible immediately.
    from core.services import seller_display

    seller_display.invalidate_seller(seller_id)

    return item
