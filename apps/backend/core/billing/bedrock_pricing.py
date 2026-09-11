"""Bedrock Claude pricing constants - input/output/cache token rates per model.

DISPLAY + RECONCILIATION ONLY — never the money path. The billing rates
live in Metronome's rate card (AWS rate × markup, configured per env in
the console); this table exists for the ``/billing/pricing`` UI surface,
the analytics counters' cost estimates, and the CE-vs-ledger
reconciliation script. Rates are the AWS-verified `us.` Regional (CRIS)
rate card (list-foundation-model-agreement-offers); keep it in lockstep
with the Metronome console when AWS changes pricing.

Microcents arithmetic avoids float drift. 1 dollar = 100 cents =
1,000,000 microcents. ``cost_microcents`` returns microcents directly;
the ``get_all_rates`` helper returns USD-per-token floats for the
``/billing/pricing`` UI surface.

Rate keys are bare foundation-model ids (e.g. ``anthropic.claude-sonnet-4-6``).
Lookups normalize the input first to strip:
  * the ``amazon-bedrock/`` provider prefix (added by the OpenClaw gateway
    when it emits ``chat.final``), and
  * inference-profile region prefixes (``us.``/``global.``/``eu.`` etc. —
    every Claude 4.x model on Bedrock is INFERENCE_PROFILE-only, so the
    invokable id always carries one of these prefixes).
"""

from __future__ import annotations

import re
from typing import TypedDict

from core.billing.bedrock_aip import TIER_TO_FM


class UnknownModelError(Exception):
    """Raised when a model id has no entry in the rate table.

    Deliberately NOT a KeyError subclass — pricing failures should never
    be silently swallowed by upstream `except KeyError:` clauses around
    dict access.
    """


class ModelRate(TypedDict):
    """Per-model token rates expressed in USD per million tokens.

    Used both by the cost helper (which collapses to microcents) and the
    pricing-UI endpoint (which surfaces raw USD per token).
    """

    input: float
    output: float
    cache_read: float
    cache_write: float


# Rates in USD per million tokens — the AWS-verified `us.` Regional (CRIS)
# rate card. cache_read is the 0.1× input cacheRead rate; cache_write is the
# 1-HOUR cacheWrite tier, because agents.defaults.params.cacheRetention is
# pinned to "long" (1h TTL) — so every write bills at the 1h rate, not the
# 5-minute one. Reference: AWS list-foundation-model-agreement-offers.
# Task D7 will source these from the AWS rate card automatically; keep in
# lockstep with the config.py catalog cost blocks until then.
_RATES: dict[str, ModelRate] = {
    "anthropic.claude-sonnet-4-6": {
        "input": 3.3,
        "output": 16.5,
        "cache_write": 6.6,
        "cache_read": 0.33,
    },
    "anthropic.claude-opus-4-6-v1": {
        # Opus 4.6 carries our card-3 traffic while AWS holds Opus 4.7
        # at applied=0 TPM (Service Quotas L-5DB28B7B). Add 4.7 back
        # here when the quota lifts — pricing is identical so the
        # ledger math is unchanged.
        "input": 5.5,
        "output": 27.5,
        "cache_write": 11.0,
        "cache_read": 0.55,
    },
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        # Cheap aux-task tier — compaction / heartbeat / memory-flush.
        # Keyed on the date-stamped foundation id: the bare
        # anthropic.claude-haiku-4-5 is NOT a valid Bedrock profile.
        "input": 1.1,
        "output": 5.5,
        "cache_write": 2.2,
        "cache_read": 0.11,
    },
}


# Tier (the AIP "lane" Plan 2 assigns per owner) -> canonical foundation-model
# id used as the rate-table key. The meter receives an AIP ARN (modelId in the
# invocation log), which normalize_model_id cannot turn into a foundation id;
# this map closes that gap. SINGLE SOURCE: imported from core.billing.bedrock_aip
# (TIER_TO_FM) — the IAM-policy FM ARNs and these rate keys never drift.


# Region prefixes used by Bedrock cross-region inference profiles. Mirrors
# the upstream OpenClaw stripper at extensions/amazon-bedrock/discovery.ts
# (resolveKnownContextWindow). When AWS adds a new region group, extend this
# pattern instead of duplicating rate-table entries.
_INFERENCE_PROFILE_PREFIX_RE = re.compile(r"^(?:us|eu|ap|apac|au|jp|global)\.")


def normalize_model_id(model_id: str) -> str:
    """Strip provider + inference-profile prefixes to the bare foundation-model id.

    Idempotent — callers can pass already-bare ids and get them back unchanged.
    Centralized here so the credit ledger and usage tracker share one stripper
    and the rate table stays keyed on a single canonical form.
    """
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    return _INFERENCE_PROFILE_PREFIX_RE.sub("", bare)


def get_rate(model_id: str) -> ModelRate:
    """Return the per-token rate entry for *model_id*.

    Accepts any of:
      * bare foundation-model id (``anthropic.claude-sonnet-4-6``)
      * inference-profile id (``us.anthropic.claude-sonnet-4-6``)
      * full openclaw model ref (``amazon-bedrock/us.anthropic.claude-sonnet-4-6``)

    Raises:
        UnknownModelError: model_id has no rate entry.
    """
    bare = normalize_model_id(model_id)
    try:
        return _RATES[bare]
    except KeyError:
        raise UnknownModelError(model_id) from None


def get_all_rates() -> dict[str, ModelRate]:
    """Return all known model rates.

    Used by the ``/billing/pricing`` endpoint. Returned dict is a
    defensive copy so callers can't mutate the module-level constants.
    """
    return {model_id: dict(rate) for model_id, rate in _RATES.items()}


def cost_microcents(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Compute the un-marked-up cost in microcents for an inference call.

    A RAW-cost ESTIMATE for analytics/reconciliation — the billed price
    (markup included) is computed by Metronome's rate card. Cache tokens
    default to 0 so older callers that don't pass them keep working —
    new callers should pass them so cache-write surcharge and cache-read
    discount land in the estimate.

    Args:
        model_id: bare Bedrock model id (e.g. "anthropic.claude-sonnet-4-6").
            Pass without the "amazon-bedrock/" prefix.
        input_tokens: prompt tokens consumed.
        output_tokens: completion tokens produced.
        cache_read_tokens: tokens served from prompt cache (0.1× input rate).
        cache_write_tokens: tokens written to prompt cache (1.25× input rate).

    Returns:
        Integer microcents.

    Raises:
        UnknownModelError: model_id has no rate entry.
    """
    rate = get_rate(model_id)
    return (
        int(input_tokens * rate["input"])
        + int(output_tokens * rate["output"])
        + int(cache_read_tokens * rate["cache_read"])
        + int(cache_write_tokens * rate["cache_write"])
    )


def price_from_tier(
    tier: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Price an invocation from an already-resolved per-owner tier.

    Maps the tier ("opus"/"sonnet"/"haiku") to its canonical foundation-model id
    (TIER_TO_FM) and prices that. PURE — no DynamoDB read. The meter resolves the
    tier once (alongside the owner) and passes it here, so there is exactly one
    reverse-table read per metered record.

    Raises:
        UnknownModelError: the tier maps to no known foundation model.
    """
    fm_id = TIER_TO_FM.get(tier) if tier else None
    if fm_id is None:
        raise UnknownModelError(tier)
    return cost_microcents(
        model_id=fm_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
