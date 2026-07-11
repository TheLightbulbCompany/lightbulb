"""Meta Muse Spark pricing + free-tier cap constants (microcents).

1 USD = 1_000_000 microcents. A rate quoted as USD-per-million-tokens is
numerically identical to microcents-per-token, so cost = tokens * rate.
"""

from datetime import datetime, timezone

META_MODEL_REF = "meta/muse-spark-1.1"

# USD per 1M tokens. cacheRead/cacheWrite default to the input rate as a
# conservative upper bound until Meta's real cache rates are confirmed.
_INPUT = 1.25
_OUTPUT = 4.25
_CACHE_READ = 1.25
_CACHE_WRITE = 1.25

# For the in-container config (cosmetic + makes estimatedCostUsd correct).
META_COST_USD_PER_MILLION = {
    "input": _INPUT,
    "output": _OUTPUT,
    "cacheRead": _CACHE_READ,
    "cacheWrite": _CACHE_WRITE,
}

CAP_MICROCENTS = 3_000_000  # $3.00

# Phase-A-deferred clamps the cap relies on.
CONTEXT_TOKENS = 200_000
MAX_TOKENS = 32_000
EFFORT = "medium"


def is_meta_model(model: str) -> bool:
    return (model or "").startswith("meta/")


def meta_cost_microcents(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> int:
    return int(input_tokens * _INPUT + output_tokens * _OUTPUT + cache_read * _CACHE_READ + cache_write * _CACHE_WRITE)


# Worst-case one-turn cost for the pre-send reserve: the full context window
# re-read as cache plus a max-length output. Bounds parallel-burst overshoot.
RESERVE_MICROCENTS = meta_cost_microcents(
    input_tokens=0,
    output_tokens=MAX_TOKENS,
    cache_read=CONTEXT_TOKENS,
)


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
