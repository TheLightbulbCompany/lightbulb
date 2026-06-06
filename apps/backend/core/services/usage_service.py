"""Usage tracking — counter writes for analytics. Billing flows through credit_ledger."""

import logging
from datetime import datetime, timezone

from core.billing.bedrock_pricing import UnknownModelError, cost_microcents
from core.observability.metrics import put_metric
from core.repositories import usage_repo

logger = logging.getLogger(__name__)


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _strip_provider_prefix(model: str) -> str:
    """Strip the provider prefix (e.g. ``amazon-bedrock/``) from the model id.

    The pricing table is keyed on the bare Bedrock model id; the model
    string the gateway emits in ``chat.final`` carries the provider
    prefix. Strip it once here so callers don't have to.
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


async def record_usage(
    owner_id: str,
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> None:
    """Record a single LLM usage event for analytics.

    Writes per-period + lifetime + per-member counters to DynamoDB. The
    credit ledger (core/services/credit_ledger.py) handles authoritative
    billing for card-3 (bedrock_claude) users; this is a counter-only
    side-write surfaced via the admin /usage endpoint.

    Microcents are stored in the same ``total_spend_microdollars``
    column the legacy code wrote to (DDB schema unchanged for backward
    compatibility — column name is a misnomer; values are microcents).
    """
    bare_model = _strip_provider_prefix(model)
    try:
        spend_microcents = cost_microcents(
            model_id=bare_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
    except UnknownModelError:
        # Non-Bedrock models (OpenAI/Codex) have no Bedrock pricing entry. We
        # still record token + request VOLUME (with spend 0) so usage is never
        # invisible — a runaway loop on the Codex path must remain visible.
        # spend 0 is correct for Isol8's books: byo_key is user-paid, Codex is
        # flat-rate, so Isol8's per-token $ cost there is ~0. The metric still
        # fires for genuinely-mispriced Bedrock models (and, expectedly, for
        # non-Bedrock models).
        put_metric("billing.pricing.missing_model")
        logger.warning(
            "No pricing for model %s — recording volume with spend 0 for owner %s",
            model,
            owner_id,
        )
        spend_microcents = 0

    # Skip only a completely-empty event (no token volume AND no spend);
    # otherwise record so spend-0 unknown-model volume is preserved.
    if spend_microcents <= 0 and not (input_tokens or output_tokens or cache_read or cache_write):
        return

    period = _current_period()

    await usage_repo.increment(
        owner_id,
        period,
        spend_microcents,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
    )
    await usage_repo.increment(
        owner_id,
        "lifetime",
        spend_microcents,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
    )
    await usage_repo.increment(
        owner_id,
        f"member:{user_id}:{period}",
        spend_microcents,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
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
