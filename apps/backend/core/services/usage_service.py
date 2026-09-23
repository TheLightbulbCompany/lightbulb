"""Usage tracking — counter writes for analytics. Billing flows through Metronome."""

import logging
from datetime import datetime, timezone

from core.repositories import usage_repo

logger = logging.getLogger(__name__)


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def record_usage(
    owner_id: str,
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    room_turn: bool = False,
    event_kind: str | None = None,
) -> None:
    """Record a single LLM usage event's token VOLUME for analytics.

    Writes per-period + lifetime + per-member counters to DynamoDB. Spend is always 0 here:
    managed OpenRouter spend is written by the settlement Lambda from each generation's real
    cost (pricing it here would count it twice), and ChatGPT-OAuth / BYO-key inference is
    user-paid. ``model`` is kept for the call contract; the counters are not keyed on it.

    ``room_turn`` marks a turn dispatched from a messaging room, and ``event_kind`` is that
    dispatch's kind (``user_request`` for an addressed message or DM, ``room_event`` for ambient
    traffic — see ``messaging.select_dispatch_targets``). A room turn writes one extra counter
    row, ``room:{event_kind}:{period}``, so ambient volume can be read apart from addressed
    volume; the owner/lifetime/member rollups still carry every turn exactly once.
    """
    if not (input_tokens or output_tokens or cache_read or cache_write):
        return

    spend_microcents = 0
    period = _current_period()
    keys = [period, "lifetime", f"member:{user_id}:{period}"]
    if room_turn:
        keys.append(f"room:{event_kind or 'unknown'}:{period}")
    for key in keys:
        await usage_repo.increment(
            owner_id, key, spend_microcents, input_tokens, output_tokens, cache_read, cache_write
        )


async def get_usage_summary(owner_id: str) -> dict:
    """Get current period usage summary for the admin /usage endpoint.

    The DDB column is named ``total_spend_microdollars`` for legacy
    reasons; values are stored as microcents. We surface the field as
    USD here (microcents / 1e6 = USD).
    """
    period = _current_period()
    usage = await usage_repo.get_period_usage(owner_id, period)
    lifetime = await usage_repo.get_period_usage(owner_id, "lifetime")

    empty = {
        "total_spend_microdollars": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "request_count": 0,
    }
    usage = usage or empty

    return {
        "period": period,
        "total_spend": usage["total_spend_microdollars"] / 1_000_000,
        "total_input_tokens": usage["total_input_tokens"],
        "total_output_tokens": usage["total_output_tokens"],
        "total_cache_read_tokens": usage["total_cache_read_tokens"],
        "total_cache_write_tokens": usage["total_cache_write_tokens"],
        "request_count": usage["request_count"],
        "lifetime_spend": (lifetime["total_spend_microdollars"] if lifetime else 0) / 1_000_000,
    }
