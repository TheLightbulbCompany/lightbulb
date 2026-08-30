"""Launch Competition settings — validation, defaults, the public projection
of the launch-settings DynamoDB row, the admin write path, and the one-click
Stripe free-month coupon + promotion-code mint.

``update`` and ``create_stripe_coupon`` are consumed by ``routers/ops.py``,
which stays thin: ``SettingsInvalid`` propagates uncaught to main.py's
``settings_invalid_handler`` (a bare 422 ``{"errors": ...}``, no
``{"detail": ...}`` wrapper — the admin form walks it straight onto fields);
the router itself only maps ``CouponAlreadyConfigured`` -> 409 and
``StripeMintError`` -> 502. Every write here clears the three caches that
read the settings row (``launch_phase``'s 30s poll-path cache, the public
``/launch`` response cache, the admin ops sheet's 60s cache) — a saved row
that doesn't take effect until an old cache expires is a launch-day footgun.
"""

from __future__ import annotations

import logging
import re
import secrets
import string

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
    # A plain attribute, not a raise-site HTTPException — main.py's
    # ``settings_invalid_handler`` renders the errors dict as the BARE
    # response body (no ``{"detail": ...}`` wrapper, unlike the two
    # exceptions below); ``status_code`` here only makes
    # ``@audit_admin_action``'s http_status column accurate.
    status_code = 422

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
    see — never ``updated_by``."""
    return {k: v for k, v in row.items() if k != "updated_by"}


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


async def update(auth: AuthContext, payload: dict) -> dict:
    """Validate + persist an admin-submitted settings payload, stamping who
    wrote it, and drop every cache that reads the row. Raises
    ``SettingsInvalid`` on bad input — main.py's exception handler turns that
    into a 422 with the field errors."""
    row = validate(payload)
    saved = await launch_settings_repo.put({**row, "updated_by": auth.user_id})
    _clear_caches()
    return to_public(saved)


_PROMO_CODE_ALPHABET = string.ascii_uppercase + string.digits
_PROMO_CODE_LEN = 6


async def create_stripe_coupon(auth: AuthContext, *, force: bool = False) -> dict:
    """Mint the launch free-month coupon plus a single promotion code sized
    to ``comp_slots`` redemptions, expiring at ``competition_start_at`` (once
    the competition starts, joining on the comped code is over). Refuses a
    second mint unless ``force`` — orphaning the first coupon in Stripe is an
    admin decision, never an accident. Raises ``CouponAlreadyConfigured``
    (409, mapped by ``routers/ops.py``), ``SettingsInvalid`` (missing
    ``competition_start_at``; 422, mapped by main.py's handler), or
    ``StripeMintError`` (502, mapped by ``routers/ops.py``)."""
    row = await effective()
    if row["stripe_coupon_id"] and not force:
        raise CouponAlreadyConfigured()
    start_at = row["competition_start_at"]
    if not start_at:
        raise SettingsInvalid({"competition_start_at": "must be set before minting the coupon"})

    code = "LIGHTBULB-" + "".join(secrets.choice(_PROMO_CODE_ALPHABET) for _ in range(_PROMO_CODE_LEN))
    try:
        coupon = await run_in_thread(
            stripe.Coupon.create,
            percent_off=100,
            duration="once",
            name="Lightbulb launch free month",
            idempotency_key=f"launch-coupon:{settings.ENVIRONMENT}:{start_at}",
        )
        promo = await run_in_thread(
            stripe.PromotionCode.create,
            coupon=coupon.id,
            code=code,
            max_redemptions=row["comp_slots"],
            expires_at=int(parse_iso(start_at).timestamp()),
            idempotency_key=f"launch-promo:{settings.ENVIRONMENT}:{coupon.id}",
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
