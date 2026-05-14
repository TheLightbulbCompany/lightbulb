"""Marketplace seller-username claim service.

The marketplace shows ``by @ledger`` on every listing card. The handle is
claimed at first publish, is unique platform-wide (case-insensitive), and
can only be changed every 90 days. This module is the single source of
truth for those rules.

**Owner-scoped, not user-scoped.** Handles attach to the *owner* of the
marketplace seller account, which is:

  * For personal sellers: ``owner_id == clerk user_id``
  * For org sellers:      ``owner_id == clerk org_id``

This matches how every other surface keys ownership (containers, billing,
Paperclip company). All call sites resolve ``owner_id`` via
``core.auth.resolve_owner_id(auth)`` and pass it in.

Storage shape (single table, single source of truth):

  * ``seller_usernames`` keyed by ``username_lower`` (PK) — the conditional
    ``PutItem`` is the uniqueness oracle.
  * GSI ``by-owner-id`` (PK ``owner_id``, sort key ``claimed_at``) — fast
    reverse lookup ("what handle does this owner currently hold?") without
    a separate mirror table. The latest row per owner is the partition's
    tail.
  * Old rows from prior handle changes stay in the table so the previous
    handle can't be immediately re-claimed by someone else (impersonation
    guard).

Public surface (referenced by ``routers/sellers.py`` and the marketplace
publish gate):

  * ``SellerUsernameError`` — typed errors raised from this module.
  * ``validate_format(username)``
  * ``check_reserved(username)``
  * ``claim_username(owner_id, username)`` — atomic single-write claim.
  * ``get_for_owner(owner_id)``
  * ``resolve_seller_display(seller_id)`` — listing-card display lookup.
  * ``enrich_listings_with_seller_display(listings)`` — batched form.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from core.dynamodb import utc_now_iso
from core.repositories import seller_username_repo


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
        - ``"cooldown_active"`` -- owner changed handle within 90 days.

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
    failure so a corrupt cell can't permanently lock an owner out of a
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


async def claim_username(owner_id: str, username: str) -> dict:
    """Claim ``username`` for ``owner_id``.

    Validates format + reserved list, enforces the 90-day cooldown if the
    owner has changed handles before, then atomically writes the
    seller-usernames reservation row. The reservation row's conditional
    ``PutItem`` is the uniqueness oracle -- if a row already exists for
    ``username_lower`` we raise ``taken``.

    ``owner_id`` is the value returned by ``core.auth.resolve_owner_id(auth)``
    — Clerk org_id for org owners, Clerk user_id for personal owners.

    Returns the post-claim state shape consumed by ``routers/sellers.py``.
    """
    canonical = validate_format(username)
    check_reserved(canonical)

    # Cooldown gate -- check the owner's current handle (latest row by
    # claimed_at on the by-owner-id GSI). If they hold this exact handle
    # already, the call is idempotent. Otherwise enforce the cooldown
    # window before we touch the reservation table.
    existing = await seller_username_repo.get_current_for_owner(owner_id)
    if existing and existing.get("username_lower") == canonical:
        # Idempotent re-claim of the same handle is a no-op. Return the
        # current state so the route can return 200 + the same body the
        # GET endpoint would. We deliberately don't bump
        # ``claimed_at`` here -- the cooldown clock should track the last
        # *actual* change.
        prev_at = existing.get("claimed_at")
        return {
            "username": canonical,
            "last_changed_at": prev_at,
            "cooldown_ends_at": _cooldown_ends_at(prev_at),
        }
    if existing and existing.get("claimed_at"):
        active, ends_at = _cooldown_active(existing.get("claimed_at"))
        if active:
            raise SellerUsernameError(
                "cooldown_active",
                extra={"cooldown_ends_at": ends_at},
            )

    now_iso = utc_now_iso()
    try:
        await seller_username_repo.claim(
            username_lower=canonical,
            owner_id=owner_id,
            claimed_at=now_iso,
        )
    except seller_username_repo.UsernameTakenError as exc:
        raise SellerUsernameError("taken") from exc

    return {
        "username": canonical,
        "last_changed_at": now_iso,
        "cooldown_ends_at": _cooldown_ends_at(now_iso),
    }


async def get_for_owner(owner_id: str) -> dict | None:
    """Return the owner's current claimed-handle state, or None when unclaimed.

    Shape::

        {
            "username":           "ledger",
            "last_changed_at":    "<ISO-8601>",
            "cooldown_ends_at":   "<ISO-8601>",
        }
    """
    row = await seller_username_repo.get_current_for_owner(owner_id)
    if not row:
        return None
    claimed_at = row.get("claimed_at")
    return {
        "username": row["username_lower"],
        "last_changed_at": claimed_at,
        "cooldown_ends_at": _cooldown_ends_at(claimed_at),
    }


# In-house listing fallback. The platform's own listings use the reserved
# ``isol8`` handle. The actual handle reservation should be seeded post-deploy
# by an operator so the row exists in the seller_usernames table for the
# admin user_id — but we want the ``by @isol8`` display to work even before
# that seeding happens.
ISOL8_DISPLAY_HANDLE = "isol8"


async def enrich_listings_with_seller_display(
    listings: list[dict],
) -> list[dict]:
    """Annotate a list of listing dicts with a ``seller_username`` field.

    The frontend ListingCard renders ``by {listing.seller_username}``
    (e.g. ``by @ledger`` or ``by @isol8``). This helper resolves each
    unique ``seller_id`` (== ``owner_id`` for the seller) to its current
    handle once, then writes the ``@<handle>`` form back onto every
    listing dict.

    Mutates and returns the same list -- the marketplace_search cache
    holds these dicts, but the cache snapshot is reset on every
    refresh (no concern about handle drift sticking around). Per-id
    lookup failures fall back to ``@isol8`` so a transient DDB issue
    can't break the listings grid.
    """
    # Resolve each unique owner_id exactly once -- catalog reads can
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

    ``seller_id`` is the listing's owner — Clerk org_id for org sellers,
    Clerk user_id for personal sellers. We query the seller_usernames
    GSI by owner_id and return whatever handle is current.

    Resolution order:
        1. If the owner has a claimed handle, return ``@<handle>``.
        2. Otherwise return ``@isol8`` as the safe fallback (covers
           in-house listings whose handle row may not be seeded yet
           plus any edge case where a listing somehow lacks a claimed
           seller).

    This function never raises -- DDB read failures fall through to the
    in-house fallback so a single misbehaving row doesn't break the
    listings grid.
    """
    if seller_id:
        try:
            row = await seller_username_repo.get_current_for_owner(seller_id)
            if row and row.get("username_lower"):
                return f"@{row['username_lower']}"
        except Exception:
            # Defensive: DDB throttling / encryption errors must not break
            # the public listing page. Fall through to the in-house
            # fallback below.
            pass
    return f"@{ISOL8_DISPLAY_HANDLE}"
