"""Voice-call billing primitives: who pays, how long a call may run, what a second costs.

Credits mode mirrors the openrouter-settlement Lambda: provider cost is ingested to Metronome
and priced there at the managed-credit markup; the fast-gate row gets the same provisional
debit settlement applies (the next balance refresh overwrites it from Metronome). Free mode is
a per-workspace daily seconds counter in the containers table (``__voice__#<owner>``).
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services import metronome_gate, trial_cap

FREE_SECONDS_PER_DAY = 600
MAX_CALL_SECONDS = 1800
MIN_CALL_SECONDS = 60
FREE_KEY_PREFIX = "__voice__#"
VOICE_PROVIDER_USD_PER_MINUTE = Decimal("0.05")
_NANODOLLARS_PER_USD = Decimal(10**9)
_LIVE_STATUSES = {"active", "trialing", "trial_local"}


def _containers():
    return get_table("containers")


def billing_mode(account: dict | None) -> Literal["credits", "free"] | None:
    """None = no voice (no row, lapsed, expired trial). Stored fields only — never
    provider_choice_from_row, which reports openrouter_managed for trial_local."""
    if not account or account.get("subscription_status") not in _LIVE_STATUSES:
        return None
    if account.get("subscription_status") == "trial_local" and trial_cap.is_trial_expired(account):
        return None
    if account.get("provider_choice") == "openrouter_managed" and not trial_cap.is_managed_capped(account):
        return "credits"
    return "free"


def provider_nanodollars(seconds: int) -> int:
    return int(
        (Decimal(seconds) * VOICE_PROVIDER_USD_PER_MINUTE * _NANODOLLARS_PER_USD / 60).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def charge_microcents(seconds: int) -> int:
    """Same formula as openrouter-settlement ``debit_microcents``: cost × markup, rounded up."""
    markup = Decimal(settings.OPENROUTER_MARKUP or "0")
    if not markup.is_finite() or markup < 1:
        raise ValueError("OPENROUTER_MARKUP must be a decimal >= 1")
    return int((Decimal(provider_nanodollars(seconds)) * markup / 1000).to_integral_value(rounding=ROUND_CEILING))


async def budget_seconds(owner_id: str, mode: str) -> int:
    if mode == "free":
        item = (
            await run_in_thread(
                _containers().get_item, Key={"owner_id": f"{FREE_KEY_PREFIX}{owner_id}"}, ConsistentRead=True
            )
        ).get("Item") or {}
        used = int(item.get("usedSeconds", 0)) if item.get("day") == trial_cap.utc_today() else 0
        return max(0, min(FREE_SECONDS_PER_DAY - used, MAX_CALL_SECONDS))
    if await metronome_gate.is_owner_blocked(owner_id):
        return 0
    row = await MetronomeFastGate().get_row(owner_id)
    if not row or row.get("remaining_microcents") is None:
        return 0
    headroom = (
        int(row["remaining_microcents"])
        - int(row.get("reserved_microcents", 0))
        - int(row.get("openrouter_reserved_microcents", 0))
        - metronome_gate.RESERVE_ESTIMATE_MICROCENTS
    )
    per_minute = charge_microcents(60)
    return max(0, min(math.floor(headroom * 60 / per_minute), MAX_CALL_SECONDS))
