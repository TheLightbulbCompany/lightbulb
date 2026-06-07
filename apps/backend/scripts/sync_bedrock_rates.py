"""Source Bedrock per-token rates from the AWS agreement-offer rate card.

The hardcoded rate table in ``core/billing/bedrock_pricing._RATES`` (mirrored
by the cost blocks in ``core/containers/config.py``) was wrong in three ways
that went unnoticed for weeks: Opus was ~2.7x too high, Sonnet/Haiku used the
``global.`` rates instead of the ``us.`` Regional ones, and one Haiku id was
not even a valid Bedrock profile. AWS exposes our exact contracted rate card
programmatically, so this script fetches it and tells us whether the committed
table still matches AWS truth.

What it does, per catalog model:

1. **Fetch** the agreement-offer rate card
   (``list_foundation_model_agreement_offers``) and select the four
   dimensions for our profile: ``us.`` Regional (CRIS) in us-east-1, with
   ``cacheRetention:"long"`` so cacheWrite reads the **1-hour** tier.
2. **Validate ids** via ``list_inference_profiles`` — assert each catalog id
   has a corresponding ACTIVE ``us.`` profile (the check that would have
   caught the invalid-Haiku-id bug).
3. **Compare** the AWS values to the committed ``_RATES`` and flag drift
   beyond a small tolerance.

Run via::

    cd apps/backend
    uv run python -m scripts.sync_bedrock_rates              # report + drift
    uv run python -m scripts.sync_bedrock_rates --emit       # + paste-ready literals

Exit codes: ``0`` clean, ``1`` drift or an invalid/missing id, ``2`` a fetch
error. Suitable for a scheduled / CI drift check.

READ-ONLY w.r.t. AWS — only ``list_foundation_model_agreement_offers`` and
``list_inference_profiles`` (both Describe/List). The operator runs it with
real credentials; nothing here makes a live call at import time or in tests
(boto3 is imported inside the fetch helpers, which the tests mock).

This script does NOT rewrite source files — auto-editing pricing constants is
too risky to automate. ``--emit`` prints the corrected literals to stdout for
a human to review and paste.

Dimension naming reference (from the AWS rate card)::

    <REGIONPREFIX>_<TokenType>[_1h][_LCtx][_Global][_Batch]

    USE1_InputTokenCount               us-east-1, on-demand, Regional/CRIS, std ctx  <- ours
    USE1_OutputTokenCount
    USE1_CacheReadInputTokenCount
    USE1_CacheWrite1hInputTokenCount   the 1h cache-write tier (cacheRetention=long) <- ours
    USE1_CacheWriteInputTokenCount     the 5-minute cache-write tier (NOT ours)
    USE1_*_Global                      the global. profile variant (excluded)
    USE1_*_LCtx                        long-context variant (excluded)
    USE1_*_Batch                       batch variant (excluded)
    USW2_* / EUC1_* ...                other regions (excluded)

``price`` is USD per **Million** tokens, which is exactly the unit our rate
table and the config cost blocks use, so no scaling is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

# Token-type → the bare dimension name (region prefix + cache TTL are appended
# at lookup). ``cache_write`` resolves to the 1-HOUR dimension because
# agents.defaults.params.cacheRetention is pinned to "long" — every write bills
# at the 1h rate, never the 5-minute one. Keep this aligned with the four
# fields of bedrock_pricing.ModelRate.
_DIMENSION_SUFFIX: dict[str, str] = {
    "input": "InputTokenCount",
    "output": "OutputTokenCount",
    "cache_read": "CacheReadInputTokenCount",
    "cache_write": "CacheWrite1hInputTokenCount",
}

# Mapping of snake_case rate field → the camelCase key used in the config.py
# cost block, for the --emit literal.
_COST_BLOCK_KEY: dict[str, str] = {
    "input": "input",
    "output": "output",
    "cache_read": "cacheRead",
    "cache_write": "cacheWrite",
}

# The `us.` Regional inference-profile prefix. We run CRIS in us-east-1; the
# bare foundation id becomes the invokable profile id by prepending this.
_PROFILE_PREFIX = "us."

# AWS-verified values (this session) for the three catalog models, kept here
# as an in-repo guard so tests and a human reader can sanity-check the parser
# output against the contracted card without a live call. NOT used as a
# fallback for the live fetch — the script always reads AWS.
AWS_RATES_EXPECTED: dict[str, dict[str, float]] = {
    "anthropic.claude-opus-4-6-v1": {"input": 5.5, "output": 27.5, "cache_read": 0.55, "cache_write": 11.0},
    "anthropic.claude-sonnet-4-6": {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6},
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.1,
        "output": 5.5,
        "cache_read": 0.11,
        "cache_write": 2.2,
    },
}


# --------------------------------------------------------------------------- #
# Catalog access — sourced from bedrock_pricing so we never re-list models.
# --------------------------------------------------------------------------- #


def catalog_model_ids() -> list[str]:
    """The bare foundation-model ids we run, read from the pricing catalog.

    Reads ``bedrock_pricing._RATES`` keys so the script stays in lockstep with
    the catalog instead of hardcoding the model list a second time.
    """
    from core.billing.bedrock_pricing import get_all_rates

    return list(get_all_rates().keys())


def committed_rate(model_id: str) -> dict[str, float]:
    """Return the currently-committed rate block for ``model_id`` (snake_case).

    Pulls from ``bedrock_pricing.get_all_rates`` so drift is measured against
    the literal that ships in the credit ledger.
    """
    from core.billing.bedrock_pricing import get_all_rates

    return dict(get_all_rates()[model_id])


def profile_id_for(bare_model_id: str) -> str:
    """Map a bare foundation-model id to its ``us.`` inference-profile id."""
    return f"{_PROFILE_PREFIX}{bare_model_id}"


# --------------------------------------------------------------------------- #
# AWS access — behind small functions so tests mock them; boto3 imported lazily.
# --------------------------------------------------------------------------- #


async def fetch_agreement_rate_card(model_id: str, *, region: str) -> list[dict]:
    """Fetch the usage-based-pricing rateCard for ``model_id``.

    ``model_id`` is the BARE foundation id (the agreement-offers API keys on
    the foundation id, not the inference-profile id). Returns the raw
    ``rateCard`` list of ``{dimension, price, unit, description}`` dicts.

    Read-only: ``list_foundation_model_agreement_offers`` is a List call.
    """
    import boto3

    client = boto3.client("bedrock", region_name=region)
    resp = await asyncio.to_thread(
        client.list_foundation_model_agreement_offers,
        modelId=model_id,
    )
    return extract_rate_card(resp)


def extract_rate_card(response: dict) -> list[dict]:
    """Pull ``offers[0].termDetails.usageBasedPricingTerm.rateCard`` out.

    Pure helper so the response-shape navigation is testable without boto3.
    Raises ``ValueError`` if the offer has no usage-based pricing term.
    """
    offers = response.get("offers") or []
    if not offers:
        raise ValueError("agreement-offers response has no offers")
    term = offers[0].get("termDetails", {}).get("usageBasedPricingTerm", {})
    rate_card = term.get("rateCard")
    if not rate_card:
        raise ValueError("offer has no usageBasedPricingTerm.rateCard")
    return rate_card


async def fetch_inference_profiles(*, region: str) -> list[dict]:
    """List the account's inference profiles (paginated).

    Returns the raw ``inferenceProfileSummaries`` items
    (``{inferenceProfileId, status, ...}``). Read-only.
    """
    import boto3

    client = boto3.client("bedrock", region_name=region)
    profiles: list[dict] = []
    next_token: str | None = None
    while True:
        kwargs: dict = {}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = await asyncio.to_thread(client.list_inference_profiles, **kwargs)
        profiles.extend(resp.get("inferenceProfileSummaries", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return profiles


# --------------------------------------------------------------------------- #
# Rate-card parsing (pure)
# --------------------------------------------------------------------------- #


def select_dimension(rate_card: list[dict], dimension: str) -> float:
    """Return the price for the EXACT ``dimension`` name as a float.

    Exact equality only — ``USE1_InputTokenCount`` must never match
    ``USE1_InputTokenCount_Global`` / ``_LCtx`` / ``_Batch``. That precision
    is the whole point: every excluded variant lives on the same card.

    Raises ``KeyError`` if the dimension is absent.
    """
    for entry in rate_card:
        if entry.get("dimension") == dimension:
            return float(entry["price"])
    raise KeyError(dimension)


def parse_rate_card(rate_card: list[dict], *, region_prefix: str) -> dict[str, float]:
    """Build the four-field snake_case rate block for our profile.

    Selects ``<region_prefix>_<suffix>`` for each of input / output /
    cache_read / cache_write, where cache_write is the 1-HOUR tier. All
    ``_Global`` / ``_LCtx`` / ``_Batch`` / other-region dims are excluded by
    construction — we only ask for the exact four dimension names.
    """
    return {
        field_name: select_dimension(rate_card, f"{region_prefix}_{suffix}")
        for field_name, suffix in _DIMENSION_SUFFIX.items()
    }


# --------------------------------------------------------------------------- #
# Inference-profile id validation (pure)
# --------------------------------------------------------------------------- #


def validate_profile_ids(catalog: list[str], profiles: list[dict]) -> list[str]:
    """Return the catalog ids that have NO ACTIVE ``us.`` inference profile.

    This is the guard that would have caught the invalid-Haiku-id bug: a
    catalog model whose ``us.`` profile id does not exist (or is not ACTIVE)
    can never be invoked, so pricing it is meaningless. An empty return means
    every catalog id is invokable.
    """
    active_ids = {p.get("inferenceProfileId") for p in profiles if str(p.get("status", "")).upper() == "ACTIVE"}
    return [m for m in catalog if profile_id_for(m) not in active_ids]


# --------------------------------------------------------------------------- #
# Drift comparison (pure)
# --------------------------------------------------------------------------- #


@dataclass
class FieldDrift:
    """One rate field that diverges between committed and AWS."""

    field: str
    committed: float | None
    aws: float


@dataclass
class ModelDrift:
    """Per-model drift result — the list of fields that diverge."""

    model_id: str
    fields: list[FieldDrift] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.fields)


def compare_rates(
    model_id: str,
    *,
    committed: dict[str, float],
    aws: dict[str, float],
    tolerance: float,
) -> ModelDrift:
    """Compare committed vs AWS rates field-by-field; collect any drift.

    A field drifts when it is missing from ``committed`` or differs from the
    AWS value by more than ``tolerance`` (absolute USD/Mtok). ``aws`` is the
    source of truth (the contracted card), so all four fields are checked.
    """
    drifts: list[FieldDrift] = []
    for field_name, aws_value in aws.items():
        committed_value = committed.get(field_name)
        if committed_value is None or abs(committed_value - aws_value) > tolerance:
            drifts.append(FieldDrift(field=field_name, committed=committed_value, aws=aws_value))
    return ModelDrift(model_id=model_id, fields=drifts)


# --------------------------------------------------------------------------- #
# Reporting + literal emission
# --------------------------------------------------------------------------- #


def format_rate_table(model_id: str, committed: dict[str, float], aws: dict[str, float]) -> str:
    """Render an AWS-vs-committed table for one model."""
    lines = [f"  {model_id}"]
    header = f"    {'field':<12} {'committed':>12} {'aws':>12} {'delta':>12}"
    lines.append(header)
    lines.append("    " + "-" * (len(header) - 4))
    for field_name in _DIMENSION_SUFFIX:
        aws_value = aws.get(field_name)
        committed_value = committed.get(field_name)
        delta = "" if (aws_value is None or committed_value is None) else f"{committed_value - aws_value:+.4f}"
        committed_str = "—" if committed_value is None else f"{committed_value:.4f}"
        aws_str = "—" if aws_value is None else f"{aws_value:.4f}"
        lines.append(f"    {field_name:<12} {committed_str:>12} {aws_str:>12} {delta:>12}")
    return "\n".join(lines)


def emit_literals(aws_rates: dict[str, dict[str, float]]) -> str:
    """Print paste-ready Python literals for both rate surfaces.

    Emits the snake_case ``_RATES`` block (for bedrock_pricing.py) and the
    camelCase ``cost`` block (for the config.py catalog) so a human can paste
    the corrected values after reviewing the drift report. We deliberately do
    NOT rewrite the source files — that's left to human review.
    """
    lines: list[str] = []
    lines.append("# --- paste into core/billing/bedrock_pricing._RATES ---")
    lines.append("_RATES = {")
    for model_id, rates in aws_rates.items():
        lines.append(f"    {model_id!r}: {{")
        for field_name in _DIMENSION_SUFFIX:
            lines.append(f'        "{field_name}": {rates[field_name]},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# --- cost blocks for core/containers/config.py catalog ---")
    for model_id, rates in aws_rates.items():
        parts = ", ".join(f'"{_COST_BLOCK_KEY[field_name]}": {rates[field_name]}' for field_name in _DIMENSION_SUFFIX)
        lines.append(f"# {model_id}")
        lines.append(f'"cost": {{{parts}}},')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Arg parsing + entrypoint
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Source Bedrock per-token rates from the AWS agreement-offer rate "
            "card, validate inference-profile ids, and flag drift vs the "
            "committed rate table. Read-only."
        ),
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region for the Bedrock control-plane calls (default: us-east-1).",
    )
    parser.add_argument(
        "--region-prefix",
        default="USE1",
        help="Rate-card region prefix selecting the dimension family (default: USE1 = us-east-1).",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.01,
        help="Absolute USD/Mtok difference tolerated before flagging drift (default: 0.01).",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="Also print paste-ready _RATES / cost-block literals to stdout (does NOT rewrite files).",
    )
    return parser


async def main(args: argparse.Namespace) -> int:
    catalog = catalog_model_ids()

    print("Sourcing Bedrock rates from the AWS agreement-offer rate card")
    print(f"  region:        {args.region}")
    print(f"  region prefix: {args.region_prefix}")
    print("  cacheWrite:    1h tier (cacheRetention=long)")
    print(f"  tolerance:     ±{args.tolerance} USD/Mtok")
    print(f"  catalog:       {', '.join(catalog)}")
    print()

    # 1. Validate that every catalog id is an active us. inference profile.
    try:
        profiles = await fetch_inference_profiles(region=args.region)
    except Exception as e:  # pragma: no cover - surfaced only with live creds
        print(f"ERROR: failed to list inference profiles: {e}")
        return 2
    missing = validate_profile_ids(catalog, profiles)
    if missing:
        print("INVALID IDS: the following catalog ids have no active us. inference profile:")
        for m in missing:
            print(f"  - {m}  (expected profile {profile_id_for(m)})")
        print()

    # 2. Fetch + parse each model's rate card.
    aws_rates: dict[str, dict[str, float]] = {}
    fetch_failed = False
    for model_id in catalog:
        try:
            rate_card = await fetch_agreement_rate_card(model_id, region=args.region)
            aws_rates[model_id] = parse_rate_card(rate_card, region_prefix=args.region_prefix)
        except (KeyError, ValueError) as e:
            print(f"ERROR: could not source rates for {model_id}: {e}")
            fetch_failed = True

    # 3. Compare to committed + render the per-model table.
    print("AWS rate card vs committed _RATES:")
    print()
    any_drift = False
    for model_id in catalog:
        if model_id not in aws_rates:
            continue
        committed = committed_rate(model_id)
        aws = aws_rates[model_id]
        print(format_rate_table(model_id, committed, aws))
        drift = compare_rates(model_id, committed=committed, aws=aws, tolerance=args.tolerance)
        if drift.has_drift:
            any_drift = True
            drifted = ", ".join(f"{f.field}" for f in drift.fields)
            print(f"    DRIFT: {drifted}")
        print()

    if args.emit and aws_rates:
        print(emit_literals(aws_rates))
        print()

    # 4. Exit status.
    if missing or fetch_failed:
        print("FAIL: at least one catalog id is invalid or its rate card could not be sourced.")
        return 1
    if any_drift:
        print(
            "DRIFT: the committed rate table no longer matches the AWS card — "
            "review the table above and re-run with --emit to get paste-ready literals."
        )
        return 1
    print("OK: committed rate table matches the AWS agreement-offer card.")
    return 0


if __name__ == "__main__":
    parsed = build_arg_parser().parse_args()
    sys.exit(asyncio.run(main(parsed)))
