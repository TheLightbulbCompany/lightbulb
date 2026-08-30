"""Launch Competition settings — validation, defaults, the public projection
of the launch-settings DynamoDB row, the admin write path, and the one-click
Stripe free-month coupon + promotion-code mint.

``update`` and ``create_stripe_coupon`` are consumed by ``routers/ops.py``,
which stays thin: it catches ``SettingsInvalid`` -> 422
``{"detail": {"errors": ...}}`` (the codebase's one 422 convention —
FastAPI's default ``HTTPException`` envelope), ``CouponAlreadyConfigured`` ->
409, ``StripeMintError`` -> 502. Every write here clears the three caches
that read the settings row (``launch_phase``'s 30s poll-path cache, the
public ``/launch`` response cache, the admin ops sheet's 60s cache) — a
saved row that doesn't take effect until an old cache expires is a
launch-day footgun.

``mint_nonce`` (server-only — never in ``to_public`` or the admin PUT
schema) makes every Stripe call ``create_stripe_coupon`` makes a pure
function of the row: the coupon/promo idempotency keys and the promotion
code itself are derived from ``(env, competition_start_at, mint_nonce[,
comp_slots])``, not a freshly-randomized value per call. Without this a
retried mint (or a ``force`` re-mint) would send the same idempotency key
with different params — Stripe 400s on that, not de-dupes it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

import stripe

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import launch_settings_repo
from core.repositories.launch_settings_repo import DEFAULTS
from core.services import launch_phase
from core.services.launch_phase import parse_iso

logger = logging.getLogger(__name__)

# ISO-8601 with a required offset (bare `Z` or `+HH:MM`/`-HH:MM`) — a naive
# timestamp with no zone is rejected here before it ever reaches parse_iso.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")

_POSITIVE_INT_FIELDS = ("comp_slots", "competition_days")
_NONNEG_INT_FIELDS = ("pot_front_cents", "pot_per_entrant_cents")
_STR_FIELDS = ("stripe_coupon_id", "stripe_promotion_code_id", "stripe_promotion_code")


class SettingsInvalid(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(f"launch settings invalid: {errors}")


class CouponAlreadyConfigured(Exception):
    """The row already carries a ``stripe_coupon_id`` and the caller didn't
    pass ``force`` — refuse to mint a second coupon and orphan the first."""


class StripeMintError(Exception):
    """Coupon or promotion-code creation failed at Stripe. The router turns
    this into a bare 502 — the real Stripe error is logged server-side by
    ``create_stripe_coupon``, never carried onto this exception or echoed to
    the client."""


def _validate_date(field: str, value, errors: dict[str, str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _ISO_RE.match(value):
        errors[field] = "must be an ISO-8601 timestamp with an offset, e.g. 2026-09-18T00:00:00-04:00"
        return
    try:
        parse_iso(value)
    except ValueError:
        errors[field] = "not a valid date/time"


def validate(payload: dict) -> dict:
    """Normalize + validate an admin-submitted settings payload. Missing
    fields fall back to ``DEFAULTS``. Raises ``SettingsInvalid`` with one
    message per bad field; never partially raises."""
    errors: dict[str, str] = {}
    row: dict = {}

    enabled = payload.get("enabled", DEFAULTS["enabled"])
    if not isinstance(enabled, bool):
        errors["enabled"] = "must be true or false"
        enabled = bool(enabled)
    row["enabled"] = enabled

    drop_at = payload.get("drop_at", DEFAULTS["drop_at"])
    start_at = payload.get("competition_start_at", DEFAULTS["competition_start_at"])
    _validate_date("drop_at", drop_at, errors)
    _validate_date("competition_start_at", start_at, errors)
    row["drop_at"] = drop_at
    row["competition_start_at"] = start_at

    if drop_at and start_at and "drop_at" not in errors and "competition_start_at" not in errors:
        if parse_iso(start_at) <= parse_iso(drop_at):
            errors["competition_start_at"] = "must be after drop_at"

    for field in _POSITIVE_INT_FIELDS:
        value = payload.get(field, DEFAULTS[field])
        try:
            value = int(value)
        except (TypeError, ValueError):
            errors[field] = "must be a whole number"
            continue
        if value < 1:
            errors[field] = "must be at least 1"
        row[field] = value

    for field in _NONNEG_INT_FIELDS:
        value = payload.get(field, DEFAULTS[field])
        try:
            value = int(value)
        except (TypeError, ValueError):
            errors[field] = "must be a whole number"
            continue
        if value < 0:
            errors[field] = "must be at least 0"
        row[field] = value

    for field in _STR_FIELDS:
        value = payload.get(field, DEFAULTS[field])
        row[field] = "" if value is None else str(value)

    if row["enabled"] and (not row["drop_at"] or not row["competition_start_at"]):
        errors["enabled"] = "requires both drop_at and competition_start_at to be set"

    if errors:
        raise SettingsInvalid(errors)
    return row


async def effective() -> dict:
    """DEFAULTS ⊕ the stored row — the row that governs the running app,
    whether or not an admin has ever saved one."""
    row = await launch_settings_repo.get()
    return {**DEFAULTS, **(row or {})}


def to_public(row: dict) -> dict:
    """The row shape a non-admin surface (or the admin console form) may
    see — never ``updated_by``, never ``mint_nonce`` (an internal Stripe
    idempotency counter, not an editable or displayable setting)."""
    return {k: v for k, v in row.items() if k not in ("updated_by", "mint_nonce")}


def _clear_caches() -> None:
    """Every settings write drops the three in-process caches that read the
    row, so a saved change takes effect on the next request instead of
    waiting out a stale cache: ``launch_phase``'s 30s poll-path cache, the
    public ``/launch`` + ``/launch/leaderboard`` response cache, and the
    admin ops sheet's 60s cache. Lazy imports — ``routers.launch`` imports
    services this module doesn't need at import time, and importing
    ``core.services.ops.launch`` up front would be a real cycle (it imports
    this module back, to serve ``settings`` while the feature is disabled)."""
    from core.services.ops import launch as ops_launch
    from routers.launch import clear_response_cache

    launch_phase.invalidate()
    clear_response_cache()
    ops_launch.clear_cache()


_COUPON_MANAGED_FIELDS = ("mint_nonce", "stripe_coupon_id", "stripe_promotion_code_id", "stripe_promotion_code")


async def update(auth: AuthContext, payload: dict) -> dict:
    """Validate + persist an admin-submitted settings payload, stamping who
    wrote it, and drop every cache that reads the row. Raises
    ``SettingsInvalid`` on bad input — ``routers/ops.py`` turns that into a
    422 with the field errors.

    ``mint_nonce`` and the three ``stripe_*`` ids are exclusively managed by
    ``create_stripe_coupon`` — this is the settings *form*, and the coupon is
    minted by its own button, never hand-edited. ``put`` is a full replace,
    so those four fields are explicitly carried over from the current stored
    row here rather than trusted from ``payload``: ``LaunchSettingsUpdate``
    doesn't accept them at all, and ``validate`` fills the ``stripe_*`` ones
    from ``DEFAULTS`` (empty strings), so carrying them forward is the only
    way a settings save can never blank out (or silently reset the
    idempotency nonce on) an already-minted coupon."""
    row = validate(payload)
    current = await effective()
    for field in _COUPON_MANAGED_FIELDS:
        row[field] = current[field]
    saved = await launch_settings_repo.put({**row, "updated_by": auth.user_id})
    _clear_caches()
    return to_public(saved)


async def create_stripe_coupon(auth: AuthContext, *, force: bool = False) -> dict:
    """Mint the launch free-month coupon plus a single promotion code sized
    to ``comp_slots`` redemptions, expiring at ``competition_start_at`` (once
    the competition starts, joining on the comped code is over). Refuses a
    second mint unless ``force`` — orphaning the first coupon in Stripe is an
    admin decision, never an accident. Raises ``CouponAlreadyConfigured``
    (409), ``SettingsInvalid`` (missing ``competition_start_at``; 422), or
    ``StripeMintError`` (502) — all mapped by ``routers/ops.py``.

    Every Stripe call is a pure function of ``(env, start_at, nonce[,
    comp_slots])`` — see the module docstring. A non-``force`` mint always
    computes ``nonce = max(stored_nonce, 1)``: it's only reachable with an
    empty ``stripe_coupon_id`` (the conflict check above already refuses a
    configured row), so this is either a genuine first mint (stored nonce 0
    -> 1) or a RETRY of a mint that failed partway through Stripe before the
    ids/nonce were ever persisted (stored nonce still whatever it was) —
    either way, byte-identical params land on the same idempotency key and
    Stripe hands back the cached object instead of erroring. ``force`` always
    bumps the nonce, so a deliberate re-mint gets a fresh coupon + fresh
    code rather than colliding with the one being replaced."""
    row = await effective()
    if row["stripe_coupon_id"] and not force:
        raise CouponAlreadyConfigured()
    start_at = row["competition_start_at"]
    if not start_at:
        raise SettingsInvalid({"competition_start_at": "must be set before minting the coupon"})
    if parse_iso(start_at) <= datetime.now(timezone.utc):
        # The promo code expires AT competition start, so a start instant
        # already behind us mints a code nobody can redeem — and Stripe
        # rejects the past `expires_at` outright. A field error on the date
        # says which knob to move; a bare 502 doesn't.
        raise SettingsInvalid(
            {"competition_start_at": "must be in the future — the promo code expires at competition start"}
        )

    nonce = row["mint_nonce"] + 1 if force else max(row["mint_nonce"], 1)
    comp_slots = row["comp_slots"]
    env = settings.ENVIRONMENT
    code = "LIGHTBULB-" + hashlib.sha256(f"{env}:{start_at}:{nonce}".encode()).hexdigest()[:6].upper()
    try:
        coupon = await run_in_thread(
            stripe.Coupon.create,
            percent_off=100,
            duration="once",
            name="Lightbulb launch free month",
            idempotency_key=f"launch-coupon:{env}:{start_at}:{nonce}",
        )
        promo = await run_in_thread(
            stripe.PromotionCode.create,
            coupon=coupon.id,
            code=code,
            max_redemptions=comp_slots,
            expires_at=int(parse_iso(start_at).timestamp()),
            # comp_slots rides the key (not just the nonce) so a comp_slots
            # change picked up between a failed attempt and its retry gets a
            # fresh promo-code creation instead of a Stripe 400 for reusing
            # an idempotency key with different `max_redemptions`.
            # `code` above does NOT carry comp_slots, so that same retry asks
            # for a new promo code under the code string the lost attempt may
            # already have created — Stripe 400s on the duplicate. Re-mint
            # with `force` (fresh nonce ⇒ fresh code) to get past it.
            idempotency_key=f"launch-promo:{env}:{start_at}:{comp_slots}:{nonce}",
        )
    except stripe.StripeError as e:
        # Logged here, once, with whatever Stripe said — never the API key
        # (stripe.StripeError bodies don't carry it), and `from None` keeps
        # it out of StripeMintError's chain so nothing downstream echoes it.
        logger.error("launch coupon mint failed: %s", e)
        raise StripeMintError() from None

    saved = await launch_settings_repo.put(
        {
            **row,
            "mint_nonce": nonce,
            "stripe_coupon_id": coupon.id,
            "stripe_promotion_code_id": promo.id,
            "stripe_promotion_code": promo.code,
            "updated_by": auth.user_id,
        }
    )
    _clear_caches()
    return {
        "stripe_coupon_id": saved["stripe_coupon_id"],
        "stripe_promotion_code_id": saved["stripe_promotion_code_id"],
        "stripe_promotion_code": saved["stripe_promotion_code"],
    }
