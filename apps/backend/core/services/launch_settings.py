"""Launch Competition settings — validation, defaults, projections of the
launch-settings DynamoDB row, the admin write path, and the one-click Stripe
free-month coupon mint.

``update`` and ``create_stripe_coupon`` are consumed by ``routers/ops.py``,
which stays thin: it catches ``SettingsInvalid`` -> 422
``{"detail": {"errors": ...}}`` (the codebase's one 422 convention —
FastAPI's default ``HTTPException`` envelope), ``CouponAlreadyConfigured`` ->
409, ``StripeMintError`` -> 502. Every write here clears the three caches
that read the settings row (``launch_phase``'s 30s cache, the public
``/launch`` response cache, the admin ops sheet's 60s cache) — a saved row
that doesn't take effect until an old cache expires is a launch-day footgun.

``mint_nonce`` (server-only) makes every Stripe call ``create_stripe_coupon``
makes a pure function of the row: the coupon idempotency key is derived from
``(env, nonce)``, not a freshly-randomized value per call. Without this a
retried mint (or a ``force`` re-mint) would send the same idempotency key
with different params — Stripe 400s on that, not de-dupes it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import stripe

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import launch_settings_repo
from core.repositories.launch_settings_repo import DEFAULTS
from core.services import launch_phase, launch_sheet_email
from core.services.launch_phase import parse_iso

logger = logging.getLogger(__name__)

# ISO-8601 with a required offset (bare `Z` or `+HH:MM`/`-HH:MM`) — a naive
# timestamp with no zone is rejected here before it ever reaches parse_iso.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")

_DATE_FIELDS = ("sheet_closes_at", "competition_start_at", "competition_end_at")
_POSITIVE_INT_FIELDS = ("comp_slots",)
_NONNEG_INT_FIELDS = ("pot_front_cents", "pot_per_entrant_cents", "pot_per_seat_cents", "referral_jump")

# The one launch free-month coupon: 100% off, once, redeemable through the end
# of October ET (spec: winners have the whole first competition month to
# subscribe on it). ponytail: a hardcoded instant for a one-shot competition;
# make it a setting if this ever runs twice.
COUPON_REDEEM_BY = datetime(2026, 10, 31, 23, 59, tzinfo=ZoneInfo("America/New_York"))


class SettingsInvalid(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(f"launch settings invalid: {errors}")


class CouponAlreadyConfigured(Exception):
    """The row already carries a ``stripe_coupon_id`` and the caller didn't
    pass ``force`` — refuse to mint a second coupon and orphan the first."""


class StripeMintError(Exception):
    """Coupon creation failed at Stripe. The router turns this into a bare
    502 — the real Stripe error is logged server-side by
    ``create_stripe_coupon``, never carried onto this exception or echoed to
    the client."""


def _validate_date(field: str, value, errors: dict[str, str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _ISO_RE.match(value):
        errors[field] = "must be an ISO-8601 timestamp with an offset, e.g. 2026-09-30T12:00:00-04:00"
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

    for field in _DATE_FIELDS:
        value = payload.get(field, DEFAULTS[field])
        _validate_date(field, value, errors)
        row[field] = value

    closes, start, end = (row[f] for f in _DATE_FIELDS)
    if closes and start and not errors.get("sheet_closes_at") and not errors.get("competition_start_at"):
        if parse_iso(start) <= parse_iso(closes):
            errors["competition_start_at"] = "must be after sheet_closes_at"
    if start and end and not errors.get("competition_start_at") and not errors.get("competition_end_at"):
        if parse_iso(end) <= parse_iso(start):
            errors["competition_end_at"] = "must be after competition_start_at"

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

    if row["enabled"] and not (closes and start and end):
        errors["enabled"] = "requires sheet_closes_at, competition_start_at and competition_end_at to be set"

    if errors:
        raise SettingsInvalid(errors)
    return row


async def effective() -> dict:
    """DEFAULTS ⊕ the stored row — the row that governs the running app,
    whether or not an admin has ever saved one."""
    row = await launch_settings_repo.get()
    return {**DEFAULTS, **(row or {})}


def to_admin(row: dict) -> dict:
    """The row shape the platform-admin console sees — everything but
    ``mint_nonce`` (an internal Stripe idempotency counter, not an editable
    or displayable setting). ADMIN-ONLY: this projection carries the pot
    fields, which never appear in any public payload (see
    ``routers/launch.launch_info`` for the public shape)."""
    return {k: v for k, v in row.items() if k != "mint_nonce"}


def _clear_caches() -> None:
    """Every settings write drops the three in-process caches that read the
    row, so a saved change takes effect on the next request instead of
    waiting out a stale cache: ``launch_phase``'s 30s cache, the public
    ``/launch`` + ``/launch/leaderboard`` response cache, and the admin ops
    sheet's 60s cache. Lazy imports — ``routers.launch`` imports services
    this module doesn't need at import time, and importing
    ``core.services.ops.launch`` up front would be a real cycle (it imports
    this module back, to serve ``settings`` while the feature is disabled)."""
    from core.services.ops import launch as ops_launch
    from routers.launch import clear_response_cache

    launch_phase.invalidate()
    clear_response_cache()
    ops_launch.clear_cache()


# Managed exclusively by the server (coupon mint + the close action) — the
# admin settings PUT is a full replace, so these are carried forward from the
# stored row on every save rather than trusted from the payload. Without the
# carry-forward a settings save after the close would silently blank the
# frozen pot/roster (or reset the coupon idempotency nonce). The still-unset
# ones carry forward as None and are DROPPED by `launch_settings_repo.put`
# rather than written as DynamoDB NULLs — see the comment there; a persisted
# NULL made the attribute exist and killed the pot freeze's write-once
# condition.
_SERVER_MANAGED_FIELDS = (
    "mint_nonce",
    # Not server-managed so much as edited through its OWN endpoint — the
    # settings PUT's eight-field contract does not carry it, so without the
    # carry-forward every schedule save would wipe the admin's email copy.
    "email_copy",
    "stripe_coupon_id",
    "closed_at",
    "final_pot_cents",
    "final_roster_count",
    "close_started_at",
    "close_result",
)


async def update(auth: AuthContext, payload: dict) -> dict:
    """Validate + persist an admin-submitted settings payload, stamping who
    wrote it, and drop every cache that reads the row. Raises
    ``SettingsInvalid`` on bad input — ``routers/ops.py`` turns that into a
    422 with the field errors."""
    row = validate(payload)
    current = await effective()
    for field in _SERVER_MANAGED_FIELDS:
        row[field] = current[field]
    saved = await launch_settings_repo.put({**row, "updated_by": auth.user_id})
    _clear_caches()
    # DEFAULTS ⊕ saved, like `effective()`: the put drops the still-unset
    # close-state fields rather than storing NULLs, and the admin form expects
    # the whole shape back.
    return to_admin({**DEFAULTS, **saved})


MAX_COPY_LEN = 2000


def validate_email_copy(variant: str, payload: dict) -> dict:
    """Normalize + validate one variant's copy override. A blank (or absent)
    field is DROPPED, which is how "reset to default" is expressed — the
    shipped copy in ``launch_sheet_email`` is always the fallback. Raises
    ``SettingsInvalid`` (router -> 422 ``{"detail": {"errors": ...}}``)."""
    if variant not in launch_sheet_email.VARIANTS:
        raise SettingsInvalid({"variant": f"must be one of {', '.join(launch_sheet_email.VARIANTS)}"})
    allowed = set(launch_sheet_email.PLACEHOLDERS[variant])
    errors: dict[str, str] = {}
    out: dict[str, str] = {}
    for field in launch_sheet_email.FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors[field] = "must be text"
            continue
        value = value.strip()
        if not value:
            continue  # blank -> fall back to the shipped default
        if len(value) > MAX_COPY_LEN:
            errors[field] = f"must be at most {MAX_COPY_LEN} characters"
            continue
        if field == "subject" and ("\r" in value or "\n" in value):
            # EmailMessage refuses a header with a newline, so the send would
            # RAISE — every row of that cohort released and dumped into
            # email_failures with nothing saying why. Refuse it here instead.
            errors[field] = "must be a single line — no line breaks"
            continue
        try:
            used = launch_sheet_email.placeholders_in(value)
        except ValueError as e:
            errors[field] = str(e)
            continue
        unknown = sorted(n for n in used if n not in allowed)
        if unknown:
            supported = ", ".join(f"{{{p}}}" for p in sorted(allowed)) or "none"
            errors[field] = (
                f"unsupported placeholder(s): {', '.join('{' + (n or '') + '}' for n in unknown)}; "
                f"this email supports {supported}"
            )
            continue
        out[field] = value
    if errors:
        raise SettingsInvalid(errors)
    return out


async def update_email_copy(auth: AuthContext, variant: str, payload: dict) -> dict:
    """Persist one variant's copy override (all-blank = reset to default) and
    drop the caches, so the next email uses it. Partial write of the single
    ``email_copy`` map, so a schedule save in flight can't clobber it.

    ponytail: read-modify-write of the map, so two admins editing DIFFERENT
    variants in the same instant can lose one edit. One console, one admin at
    a time — make it a per-variant attribute if that ever stops being true."""
    fields = validate_email_copy(variant, payload)
    current = await effective()
    copy = dict(current.get("email_copy") or {})
    if fields:
        copy[variant] = fields
    else:
        copy.pop(variant, None)
    await launch_settings_repo.update_fields({"email_copy": copy, "updated_by": auth.user_id})
    _clear_caches()
    return copy


def email_copy_admin(row: dict) -> list[dict]:
    """Every variant's default, override, effective copy, supported
    placeholders and a rendered preview — everything the console's email
    editor needs, in one read."""
    s = launch_phase.schedule()
    values = launch_sheet_email.sample_values(
        comp_slots=row["comp_slots"],
        referral_jump=row["referral_jump"],
        sheet_closes_at=s.sheet_closes_at if s else None,
    )
    overrides = row.get("email_copy") or {}
    out = []
    for variant in launch_sheet_email.VARIANTS:
        body = launch_sheet_email.render(variant, copy=overrides, values=values)
        out.append(
            {
                "variant": variant,
                "placeholders": list(launch_sheet_email.PLACEHOLDERS[variant]),
                "defaults": dict(launch_sheet_email.DEFAULT_COPY[variant]),
                "override": dict(overrides.get(variant) or {}),
                "preview_subject": body["subject"],
                "preview_html": launch_sheet_email.preview_document(body["html"]),
            }
        )
    return out


async def record_close_state(
    fields: dict, *, condition: str | None = None, condition_values: dict | None = None
) -> bool:
    """Atomic partial write of the server-managed close-state fields
    (``closed_at``, the pot freeze, the async-close bookkeeping) — a single
    UpdateExpression SET of ONLY the supplied fields (no read), used by
    ``core.services.launch_close`` only. The admin PUT carries these fields
    forward (``_SERVER_MANAGED_FIELDS``), so neither writer can clobber the
    other. ``condition`` (+ ``condition_values`` for any placeholders it
    names) makes the write conditional (write-once fields — the pot freeze);
    a lost condition returns False and writes nothing. CALLERS MUST CHECK the
    return of a conditional write: a discarded freeze that nobody looked at is
    exactly how the pot stayed unset through every close."""
    wrote = await launch_settings_repo.update_fields(fields, condition=condition, condition_values=condition_values)
    _clear_caches()
    return wrote


async def create_stripe_coupon(auth: AuthContext, *, force: bool = False) -> dict:
    """Mint the launch free-month coupon: 100% off, ``duration="once"``,
    redeemable through ``COUPON_REDEEM_BY``, and RESTRICTED to the flat
    (Workspace) subscription product via ``applies_to.products`` — the free
    month is a Workspace month, never a discount on the bundled starter-credits
    line item (a 100%-off coupon on the whole session would zero up to
    $1,000 of credits the webhook still grants in full). The product id is
    resolved from ``STRIPE_WORKSPACE_PRICE_ID`` at mint time. There are NO
    promotion codes — the coupon is attached server-side (to paying winners'
    subscriptions by the close action, and pre-applied at checkout for
    unpaid winners), never typed by a customer. Refuses a second mint unless
    ``force`` — orphaning the first coupon in Stripe is an admin decision,
    never an accident (the close action forces a re-mint itself when the
    stored coupon predates the applies_to restriction).

    The Stripe call is a pure function of ``(env, product, nonce)``: a
    non-``force`` mint always computes ``nonce = max(stored_nonce, 1)`` —
    either a genuine first mint or a retry of one that failed before the id
    was persisted — so byte-identical params land on the same idempotency
    key and Stripe hands back the cached coupon. ``force`` bumps the nonce
    for a genuinely fresh coupon; the product id riding the key means the
    restricted mint can never replay a cached UNrestricted v1 coupon."""
    row = await effective()
    if row["stripe_coupon_id"] and not force:
        raise CouponAlreadyConfigured()
    if COUPON_REDEEM_BY <= datetime.now(timezone.utc):
        # Stripe rejects a past `redeem_by` outright; say why instead of 502ing.
        raise SettingsInvalid({"stripe_coupon_id": "the coupon redeem-by instant (Oct 31 ET) is already past"})
    if not settings.STRIPE_WORKSPACE_PRICE_ID:
        raise SettingsInvalid({"stripe_coupon_id": "STRIPE_WORKSPACE_PRICE_ID is not configured for this env"})

    nonce = row["mint_nonce"] + 1 if force else max(row["mint_nonce"], 1)
    env = settings.ENVIRONMENT
    try:
        price = await run_in_thread(stripe.Price.retrieve, settings.STRIPE_WORKSPACE_PRICE_ID)
        product = price["product"]
        coupon = await run_in_thread(
            stripe.Coupon.create,
            percent_off=100,
            duration="once",
            name="Lightbulb launch free month",
            redeem_by=int(COUPON_REDEEM_BY.timestamp()),
            applies_to={"products": [product]},
            idempotency_key=f"launch-coupon:{env}:{product}:{nonce}",
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
            "updated_by": auth.user_id,
        }
    )
    _clear_caches()
    return {"stripe_coupon_id": saved["stripe_coupon_id"]}
