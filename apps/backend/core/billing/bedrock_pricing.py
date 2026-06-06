"""Bedrock Claude pricing constants - input/output/cache token rates per model.

Rates sourced from AWS Bedrock list price (us-east-1, 2026-04). Update
this file when AWS changes pricing; no other code should hardcode rates.
Per spec §6.3.

Microcents arithmetic is used everywhere in the credit ledger so we
avoid float drift on deduction. 1 dollar = 100 cents = 1,000,000
microcents. ``cost_microcents`` returns microcents directly; the
``get_all_rates`` helper returns USD-per-token floats for the
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


# Rates in USD per million tokens. Cache rates per Anthropic prompt-caching
# pricing on Bedrock: cache_write = 1.25× input rate, cache_read = 0.1× input
# rate. Reference: https://aws.amazon.com/bedrock/pricing/
_RATES: dict[str, ModelRate] = {
    "anthropic.claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.3,
    },
    "anthropic.claude-opus-4-6-v1": {
        # Opus 4.6 carries our card-3 traffic while AWS holds Opus 4.7
        # at applied=0 TPM (Service Quotas L-5DB28B7B). Add 4.7 back
        # here when the quota lifts — pricing is identical so the
        # ledger math is unchanged.
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.5,
    },
    "anthropic.claude-haiku-4-5": {
        # Cheap aux-task tier — compaction / heartbeat / memory-flush.
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    },
}


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

    Markup is applied separately by ``credit_ledger.deduct``. Cache tokens
    default to 0 so older callers that don't pass them keep working —
    new callers should pass them so cache-write surcharge and cache-read
    discount land in the ledger.

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
