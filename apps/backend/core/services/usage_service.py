"""Usage tracking — counter writes for analytics. Billing flows through Metronome."""

import logging
from datetime import datetime, timezone

from core.billing.bedrock_pricing import UnknownModelError, cost_microcents
from core.observability.metrics import put_metric
from core.repositories import usage_repo

logger = logging.getLogger(__name__)


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


_BEDROCK_PROVIDER_PREFIX = "amazon-bedrock/"


def _strip_provider_prefix(model: str) -> str:
    """Strip the provider prefix (e.g. ``amazon-bedrock/``) from the model id.

    The pricing table is keyed on the bare Bedrock model id; the model
    string the gateway emits in ``chat.final`` carries the provider
    prefix. Strip it once here so callers don't have to.
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _is_bedrock_model(model: str) -> bool:
    """True for a Bedrock provider ref or Hermes application-inference-profile ARN.

    Pricing only knows Bedrock models, so a pricing miss is a genuine,
    page-worthy gap only for Bedrock refs. OpenAI (``openai/``), Codex
    (``openai/``), and BYO-direct Anthropic (``anthropic/``) refs
    are expected to miss and must not page. The check runs on the raw
    model ref (before any prefix stripping) where the provider is still
    visible.
    """
    return model.startswith(_BEDROCK_PROVIDER_PREFIX) or (
        model.startswith("arn:aws:bedrock:") and ":application-inference-profile/" in model
    )


async def record_usage(
    owner_id: str,
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    raw_cost_microcents: int | None = None,
    room_turn: bool = False,
    event_kind: str | None = None,
) -> None:
    """Record a single LLM usage event for analytics.

    Writes per-period + lifetime + per-member counters to DynamoDB.
    Authoritative billing lives in Metronome (the meter Lambda ingests
    every Bedrock invocation directly); this is a counter-only side-write
    surfaced via the usage panels + the CE reconciliation script.

    Microcents are stored in the same ``total_spend_microdollars``
    column the legacy code wrote to (DDB schema unchanged for backward
    compatibility — column name is a misnomer; values are microcents).

    When ``raw_cost_microcents`` is supplied AND the model is a
    ``amazon-bedrock/`` ref, it is recorded verbatim as spend instead of
    recomputing from token counts. The caller sources it from OpenClaw's
    ``estimatedCostUsd`` (cache-inclusive), which is the authoritative cost:
    ``sessions.list`` omits cache token counts, and cache tokens are ~99.99%
    of volume, so the token-based path captures only ~3% of real cost. The
    override is gated to Bedrock because it is the only Isol8-billed,
    metered path; non-Bedrock sessions (ChatGPT OAuth / OpenAI / BYO) are
    user-paid / flat-rate, so recording their ``estimatedCostUsd`` as Isol8
    spend would contradict the books — they record spend 0 even when the
    override is present. Token counts are still recorded for analytics in
    all cases. ``None`` (the default — no ``estimatedCostUsd`` on the row)
    falls back to the token-based path unchanged.

    ``room_turn`` marks a turn dispatched from a messaging room, and
    ``event_kind`` is that dispatch's kind (``user_request`` for an addressed
    message or DM, ``room_event`` for ambient traffic — see
    ``messaging.select_dispatch_targets``). A room turn writes one extra
    counter row, ``room:{event_kind}:{period}``, so ambient spend can be read
    apart from addressed spend; the owner/lifetime/member rollups above are
    unchanged and still carry every turn exactly once.
    """
    bare_model = _strip_provider_prefix(model)
    bedrock_model = model.removeprefix(_BEDROCK_PROVIDER_PREFIX)
    is_aip = bedrock_model.startswith("arn:aws:bedrock:") and ":application-inference-profile/" in bedrock_model
    if is_aip and not (raw_cost_microcents is not None and raw_cost_microcents > 0):
        from core.billing.bedrock_aip import TIER_TO_FM, read_owner_aips

        # Hermes reports the owner's opaque AIP ARN. Resolve against the persisted
        # owner map, never a Bedrock control-plane lookup or another tenant's map.
        arns = await read_owner_aips(owner_id) or {}
        bare_model = next(
            (TIER_TO_FM[tier] for tier, arn in arns.items() if arn == bedrock_model and tier in TIER_TO_FM), model
        )
    # The raw-cost override (OpenClaw's cache-inclusive ``estimatedCostUsd``)
    # is only authoritative Isol8 spend on the Isol8-billed path: Bedrock.
    # Non-Bedrock sessions (ChatGPT OAuth / OpenAI / BYO) can carry
    # ``estimatedCostUsd`` too, but those are user-paid / flat-rate, so
    # recording that cost as Isol8 spend would contradict the books. Gate the
    # override to Bedrock; anything else falls through to the token path →
    # UnknownModelError → spend 0, even when the override is present.
    use_raw_cost = raw_cost_microcents is not None and raw_cost_microcents > 0 and _is_bedrock_model(model)
    if use_raw_cost:
        spend_microcents = raw_cost_microcents
    else:
        try:
            spend_microcents = cost_microcents(
                model_id=bare_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
        except UnknownModelError:
            # Non-Bedrock models (OpenAI/Codex/BYO Anthropic) have no Bedrock
            # pricing entry. We still record token + request VOLUME (with spend 0)
            # so usage is never invisible — a runaway loop on the Codex path must
            # remain visible. spend 0 is correct for Isol8's books: byo_key is
            # user-paid, Codex is flat-rate, so Isol8's per-token $ cost there is ~0.
            #
            # The page-tier metric fires ONLY for Bedrock refs, where a pricing
            # miss is a genuine gap (a Bedrock model we forgot to price). Pricing
            # only knows Bedrock, so emitting it for the expected non-Bedrock misses
            # would spam the page-tier alarm on every non-Bedrock chat.
            if _is_bedrock_model(model):
                put_metric("billing.pricing.missing_model")
            else:
                put_metric("billing.usage.non_bedrock")
            logger.warning(
                "No pricing for model %s — recording volume with spend 0 for owner %s",
                model,
                owner_id,
            )
            spend_microcents = 0

    # Skip only a completely-empty event (no token volume AND no spend);
    # otherwise record so spend-0 unknown-model volume is preserved. A
    # cache-heavy turn can report ~0 input/output tokens with a positive
    # ``raw_cost_microcents`` — the spend check keeps it from being dropped.
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
    if room_turn:
        await usage_repo.increment(
            owner_id,
            f"room:{event_kind or 'unknown'}:{period}",
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
