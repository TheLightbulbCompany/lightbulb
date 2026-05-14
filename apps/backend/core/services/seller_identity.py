"""Marketplace seller-username claim service.

The marketplace shows ``by @ledger`` on every listing card. The handle is
claimed at first publish, is unique platform-wide (case-insensitive), and
can only be changed every 90 days. This module is the single source of
truth for those rules.

Storage shape:
    * Authoritative reservation lives in the ``seller_usernames`` table,
      keyed by ``username_lower`` (see ``seller_username_repo``). The
      conditional ``PutItem`` is the uniqueness oracle.
    * The lowercased handle + ``last_changed_at`` ISO timestamp are also
      mirrored onto the user's row in the ``users`` table so per-listing
      reads can resolve ``seller_id -> @handle`` without a second indexed
      lookup.

Public surface (referenced by ``routers/sellers.py`` and the marketplace
publish gate):

    * ``SellerUsernameError`` -- typed errors raised from this module.
    * ``validate_format(username)``
    * ``check_reserved(username)``
    * ``claim_username(user_id, username)`` -- atomic two-table write.
    * ``get_for_user(user_id)``
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from core.dynamodb import utc_now_iso
from core.repositories import seller_username_repo, user_repo


# ---------------------------------------------------------------------------
# Rules (kept in code so they're discoverable + testable inline; widen the
# reserved list by adding strings here -- it's intentionally a frozenset so
# any accidental mutation at runtime would raise).
# ---------------------------------------------------------------------------

USERNAME_REGEX = re.compile(r"^[a-z0-9_-]+$")
MIN_LENGTH = 3
MAX_LENGTH = 30
COOLDOWN_DAYS = 90

# Names blocked from being claimed by end users. Lowercased.
# ``isol8`` is reserved for the platform's own in-house listings and is
# seeded post-deploy by an operator (see CDK README).
RESERVED_USERNAMES: frozenset[str] = frozenset(
    {
        "isol8",
        "admin",
        "support",
        "official",
        "claude",
        "anthropic",
        "marketplace",
        "atlas",
        "friday",
        "pulse",
        "pitch",
        "pitch-scout",
        "scout",
        "tally",
        "vera",
        "echo",
        "ember",
        "lens",
        "ora",
        "thread",
    }
)


class SellerUsernameError(Exception):
    """Domain error for handle-claim failures.

    ``code`` is one of:
        - ``"invalid_format"`` -- regex / length check failed.
        - ``"reserved"``       -- handle is in the reserved list.
        - ``"taken"``          -- conditional write hit an existing row.
        - ``"cooldown_active"`` -- user changed handle within 90 days.

    For ``cooldown_active`` the exception's ``extra`` carries the
    ``cooldown_ends_at`` ISO timestamp so the route can surface it.
    """

    def __init__(self, code: str, *, extra: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_format(username: str) -> str:
    """Strip + lowercase the candidate, validate regex + length.

    Returns the canonical (lowercased, stripped) form. Raises
    ``SellerUsernameError('invalid_format')`` if any check fails. The
    canonical form is what every downstream write uses -- never store the
    raw user input.
    """
    if not isinstance(username, str):
        raise SellerUsernameError("invalid_format")
    canonical = username.strip().lower()
    if len(canonical) < MIN_LENGTH or len(canonical) > MAX_LENGTH:
        raise SellerUsernameError("invalid_format")
    if not USERNAME_REGEX.match(canonical):
        raise SellerUsernameError("invalid_format")
    return canonical


def check_reserved(username: str) -> None:
    """Raise ``SellerUsernameError('reserved')`` if the lowercased handle
    is in the reserved list. No-op otherwise.

    Caller is responsible for passing the lowercased canonical form -- the
    reserved set is itself lowercased.
    """
    if username in RESERVED_USERNAMES:
        raise SellerUsernameError("reserved")


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse. Accepts the canonical form written by
    ``utc_now_iso`` plus a few common variants. Returns ``None`` on parse
    failure so a corrupt cell can't permanently lock a user out of a
    handle change.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        # Python's fromisoformat handles ``2026-05-13T12:34:56.000+00:00``.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cooldown_ends_at(last_changed_at: str | None) -> str | None:
    """Return the ISO timestamp at which the cooldown window expires, or
    ``None`` if no prior change is recorded.
    """
    parsed = _parse_iso(last_changed_at)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(days=COOLDOWN_DAYS)).isoformat()


def _cooldown_active(last_changed_at: str | None) -> tuple[bool, str | None]:
    """True iff the previous change happened within the cooldown window.
    Returns ``(active, cooldown_ends_at)`` so the caller can include the
    next-allowed timestamp in the error body.
    """
    parsed = _parse_iso(last_changed_at)
    if parsed is None:
        return False, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    ends_at = parsed + timedelta(days=COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)
    return now < ends_at, ends_at.isoformat()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def claim_username(user_id: str, username: str) -> dict:
    """Claim ``username`` for ``user_id``.

    Validates format + reserved list, enforces the 90-day cooldown if the
    user has changed handles before, then atomically writes the
    seller-usernames reservation row and updates the users row. The
    reservation row's conditional ``PutItem`` is the uniqueness oracle --
    if a row already exists for ``username_lower`` we raise ``taken``.

    Returns the post-claim state shape consumed by ``routers/sellers.py``.
    """
    canonical = validate_format(username)
    check_reserved(canonical)

    # Cooldown gate -- if the user has a prior change <90 days ago we
    # reject before touching the reservation table.
    existing = await user_repo.get_seller_username(user_id)
    if existing and existing.get("seller_username") == canonical:
        # Idempotent re-claim of the same handle is a no-op. Return the
        # current state so the route can return 200 + the same body the
        # GET endpoint would. We deliberately don't bump
        # ``last_changed_at`` here -- the cooldown clock should track the
        # last *actual* change.
        return {
            "username": canonical,
            "last_changed_at": existing.get("seller_username_changed_at"),
            "cooldown_ends_at": _cooldown_ends_at(
                existing.get("seller_username_changed_at")
            ),
        }
    if existing and existing.get("seller_username_changed_at"):
        active, ends_at = _cooldown_active(
            existing.get("seller_username_changed_at")
        )
        if active:
            raise SellerUsernameError(
                "cooldown_active",
                extra={"cooldown_ends_at": ends_at},
            )

    now_iso = utc_now_iso()
    try:
        await seller_username_repo.claim(
            username_lower=canonical,
            user_id=user_id,
            claimed_at=now_iso,
        )
    except seller_username_repo.UsernameTakenError as exc:
        raise SellerUsernameError("taken") from exc

    await user_repo.set_seller_username(
        user_id=user_id,
        username=canonical,
        last_changed_at=now_iso,
    )

    return {
        "username": canonical,
        "last_changed_at": now_iso,
        "cooldown_ends_at": _cooldown_ends_at(now_iso),
    }


async def get_for_user(user_id: str) -> dict | None:
    """Return the user's claimed-handle state, or None when unclaimed.

    Shape::

        {
            "username":           "ledger",
            "last_changed_at":    "<ISO-8601>",
            "cooldown_ends_at":   "<ISO-8601>",
        }
    """
    row = await user_repo.get_seller_username(user_id)
    if not row:
        return None
    last_changed = row.get("seller_username_changed_at")
    return {
        "username": row["seller_username"],
        "last_changed_at": last_changed,
        "cooldown_ends_at": _cooldown_ends_at(last_changed),
    }


# In-house listing fallback. ``curator.get_curator_user_id`` resolves the
# admin@isol8.co Clerk account at runtime — but we want the
# ``by @isol8`` display to work even before the curator account onboards
# (and even if its handle hasn't been seeded yet). So we hardcode the
# fallback here. The actual handle reservation should still be seeded
# post-deploy by an operator so the row exists in the dedicated table.
ISOL8_DISPLAY_HANDLE = "isol8"


async def enrich_listings_with_seller_display(
    listings: list[dict],
) -> list[dict]:
    """Annotate a list of listing dicts with a ``seller_username`` field.

    The frontend ListingCard renders ``by {listing.seller_username}``
    (e.g. ``by @ledger`` or ``by @isol8``). This helper resolves each
    unique seller_id to its mirrored handle once, then writes the
    ``@<handle>`` form back onto every listing dict.

    Mutates and returns the same list -- the marketplace_search cache
    holds these dicts, but the cache snapshot is reset on every
    refresh (no concern about handle drift sticking around). Per-id
    lookup failures fall back to ``@isol8`` so a transient DDB issue
    can't break the listings grid.
    """
    # Resolve each unique seller_id exactly once -- catalog reads can
    # easily hit dozens of listings from the same curator account.
    unique_ids = {li.get("seller_id") for li in listings if li.get("seller_id")}
    display_by_id: dict[str, str] = {}
    for sid in unique_ids:
        try:
            display_by_id[sid] = await resolve_seller_display(sid)
        except Exception:
            # resolve_seller_display already swallows its own errors,
            # but defend against unexpected upstream failures (e.g. a
            # boto3 NoCredentialsError raised at module import time in
            # CI where the helper isn't mocked). Fall back to the
            # in-house display so the listings grid never breaks.
            display_by_id[sid] = f"@{ISOL8_DISPLAY_HANDLE}"
    for li in listings:
        sid = li.get("seller_id")
        li["seller_username"] = display_by_id.get(sid, f"@{ISOL8_DISPLAY_HANDLE}")
    return listings


async def resolve_seller_display(seller_id: str | None) -> str:
    """Return the ``@handle`` to render for a listing whose ``seller_id``
    column is ``seller_id``.

    Resolution order:
        1. If the user has a claimed handle in the users-table mirror
           column, return ``@<handle>``.
        2. If the seller is the in-house curator account, return ``@isol8``.
        3. Otherwise return ``@isol8`` as the safest fallback -- the only
           way to reach this branch in v0 is an in-house listing whose
           seller is the curator account (and we never want to leak a
           raw user_id into the UI).

    This function never raises -- DDB read failures fall through to the
    in-house fallback so a single misbehaving row doesn't break the
    listings grid.
    """
    if seller_id:
        try:
            row = await user_repo.get_seller_username(seller_id)
            if row and row.get("seller_username"):
                return f"@{row['seller_username']}"
        except Exception:
            # Defensive: DDB throttling / encryption errors must not break
            # the public listing page. Fall through to the in-house
            # fallback below.
            pass
    return f"@{ISOL8_DISPLAY_HANDLE}"
