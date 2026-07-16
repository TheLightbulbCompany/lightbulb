"""Lockstep: the bedrock-gate Lambda's rate card (rates.mjs) is the FOURTH copy
of these numbers (backend bedrock_pricing._RATES, the meter Lambda's index.py
_RATES, and the config.py catalog cost blocks are the other three). The gate has
no Python import path, so it hardcodes the table in JS — this test parses that JS
and asserts it is byte-equal to the backend source of truth, exactly as
test_index.py does for the meter copy. Without it the gate would reserve/settle
the wrong dollar amount the moment the rate card changes.
"""

import json
import re
from pathlib import Path

from core.billing import bedrock_aip, bedrock_pricing

# .../apps/backend/tests/unit/billing/test_x.py -> parents[4] == .../apps
RATES_MJS = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "bedrock-gate" / "rates.mjs"


def _extract_object(src: str, name: str) -> dict:
    """Pull `export const <name> = { ... };` and JSON-ify it (keys are already
    quoted or bare identifiers; values are numbers or quoted strings)."""
    m = re.search(rf"export const {name} = (\{{.*?\}});", src, re.DOTALL)
    assert m, f"{name} not found in rates.mjs"
    body = m.group(1)
    # Quote bare identifier keys: {input: 3.3} -> {"input": 3.3}. Keys are word
    # chars or already-quoted strings, followed by a colon.
    body = re.sub(r"([{,]\s*)([A-Za-z_][\w\-]*)\s*:", r'\1"\2":', body)
    # Drop trailing commas.
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


def _load_rates_mjs():
    src = RATES_MJS.read_text()
    return _extract_object(src, "RATES"), _extract_object(src, "TIER_TO_FM")


def test_gate_rate_table_matches_backend():
    rates, tier_to_fm = _load_rates_mjs()
    assert rates == bedrock_pricing._RATES, "bedrock-gate rates.mjs RATES drifted from bedrock_pricing._RATES"
    assert tier_to_fm == bedrock_aip.TIER_TO_FM, "bedrock-gate rates.mjs TIER_TO_FM drifted from bedrock_aip.TIER_TO_FM"


def test_gate_pricing_matches_backend_price_from_tier():
    """The gate's priceMicrocents is per-component Math.trunc(tokens*rate); the
    backend's price_from_tier is the same per-component int() truncation. Same
    table + same arithmetic => identical microcents. We verify the arithmetic
    shape here by reconstructing it from the parsed table and comparing to the
    backend for a representative usage vector per tier."""
    rates, tier_to_fm = _load_rates_mjs()
    usage = dict(input_tokens=123_457, output_tokens=8_191, cache_read_tokens=91_003, cache_write_tokens=25_311)
    for tier in tier_to_fm:
        rate = rates[tier_to_fm[tier]]
        gate_microcents = (
            int(usage["input_tokens"] * rate["input"])
            + int(usage["output_tokens"] * rate["output"])
            + int(usage["cache_read_tokens"] * rate["cache_read"])
            + int(usage["cache_write_tokens"] * rate["cache_write"])
        )
        backend_microcents = bedrock_pricing.price_from_tier(tier, **usage)
        assert gate_microcents == backend_microcents, f"gate pricing drift for tier {tier}"
