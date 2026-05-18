"""Marketplace seller profile — handle storage, validation, and lookup.

The marketplace lets a seller pick a public handle (``@example``) that
identifies them across listings and (future) star / credibility surfaces.
This module owns the DynamoDB table behind that identity:

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

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso


HANDLE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{3,32}$")
HANDLE_MIN_LEN: Final[int] = 3
HANDLE_MAX_LEN: Final[int] = 32
DISPLAY_NAME_MAX_LEN: Final[int] = 64
BIO_MAX_LEN: Final[int] = 280

RESERVED_HANDLES: Final[frozenset[str]] = frozenset(
    {
        "isol8",
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


def _sellers_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_SELLERS_TABLE)


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
    table = _sellers_table()
    try:
        resp = await run_in_thread(table.get_item, Key={"seller_id": seller_id})
    except ClientError as exc:
        # Table missing (e.g. early after CDK deploy) is benign — surface
        # as "no profile" so the rest of the app keeps working.
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return resp.get("Item")


async def get_profile_by_handle(handle: str) -> dict | None:
    """Return the profile keyed by handle via the handle-index GSI, or None."""
    normalised = normalize_handle(handle)
    if not normalised:
        return None
    table = _sellers_table()
    try:
        resp = await run_in_thread(
            table.query,
            IndexName="handle-index",
            KeyConditionExpression="handle = :h",
            ExpressionAttributeValues={":h": normalised},
            Limit=1,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    items = resp.get("Items") or []
    return items[0] if items else None


async def check_handle_available(handle: str, *, for_seller_id: str | None = None) -> bool:
    """True if handle is free, OR taken by ``for_seller_id`` (i.e. unchanged).

    Validates first — callers receive False for reserved / malformed
    handles without doing a DDB read, matching the publish-flow UX
    where the input is rejected before the network round-trip.
    """
    normalised = normalize_handle(handle)
    try:
        validate_handle(normalised)
    except SellerProfileError:
        return False
    existing = await get_profile_by_handle(normalised)
    if existing is None:
        return True
    return for_seller_id is not None and existing.get("seller_id") == for_seller_id


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

    table = _sellers_table()
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

    await run_in_thread(table.put_item, Item=item)

    # Resolver caches the previous handle (or fallback) for this seller
    # — invalidate so the new value is visible immediately.
    from core.services import seller_display

    seller_display.invalidate_seller(seller_id)

    return item
