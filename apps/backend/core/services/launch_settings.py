"""Launch Competition settings — validation, defaults, and the public
projection of the launch-settings DynamoDB row.

Writing (``update``) and the Stripe-coupon mint land in a later task
(``core.services.ops.launch`` / ``routers/ops.py``); this module owns the
shape and the rules so that task, and ``core.services.launch_phase``'s
read-side cache, both build on the same normalized row.
"""

from __future__ import annotations

import re

from core.repositories import launch_settings_repo
from core.repositories.launch_settings_repo import DEFAULTS
from core.services.launch_phase import parse_iso

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
