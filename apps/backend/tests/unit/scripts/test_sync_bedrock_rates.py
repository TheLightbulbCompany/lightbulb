"""Unit tests for the Bedrock rate-card sync/verify script.

All AWS access is mocked — no live calls. The headline cases verify the
rateCard parser picks exactly the four ``us.`` Regional (CRIS) dimensions
at the 1-hour cacheWrite tier and ignores the swarm of decoy dimensions
(``_Global``, ``_LCtx``, ``_Batch``, other regions) that share the card.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from scripts.sync_bedrock_rates import (
    AWS_RATES_EXPECTED,
    ModelDrift,
    build_arg_parser,
    compare_rates,
    extract_rate_card,
    main,
    parse_rate_card,
    profile_id_for,
    select_dimension,
    validate_profile_ids,
)


# --------------------------------------------------------------------------- #
# Fixtures: a realistic rateCard with the right dims + plenty of decoys.
# --------------------------------------------------------------------------- #


def _rate_card(
    *,
    input_price: str,
    output_price: str,
    cache_read_price: str,
    cache_write_1h_price: str,
    cache_write_5m_price: str,
) -> list[dict]:
    """Build a rateCard with the four wanted USE1 dims + decoys.

    Decoys: the 5-minute cacheWrite (must be ignored — we want 1h),
    the ``_Global`` profile variant, ``_LCtx`` long-context, ``_Batch``,
    and an entirely different region (USW2).
    """
    return [
        # --- the four we want (USE1, regional, standard ctx, on-demand) ---
        {"dimension": "USE1_InputTokenCount", "price": input_price, "unit": "Units"},
        {"dimension": "USE1_OutputTokenCount", "price": output_price, "unit": "Units"},
        {"dimension": "USE1_CacheReadInputTokenCount", "price": cache_read_price, "unit": "Units"},
        {"dimension": "USE1_CacheWrite1hInputTokenCount", "price": cache_write_1h_price, "unit": "Units"},
        # --- decoys that must NOT be picked ---
        # 5-minute cacheWrite (default tier; we run cacheRetention=long → 1h).
        {"dimension": "USE1_CacheWriteInputTokenCount", "price": cache_write_5m_price, "unit": "Units"},
        # global. profile variant.
        {"dimension": "USE1_InputTokenCount_Global", "price": "999.0", "unit": "Units"},
        {"dimension": "USE1_OutputTokenCount_Global", "price": "999.0", "unit": "Units"},
        {"dimension": "USE1_CacheWrite1hInputTokenCount_Global", "price": "999.0", "unit": "Units"},
        # long-context variant.
        {"dimension": "USE1_InputTokenCount_LCtx", "price": "888.0", "unit": "Units"},
        {"dimension": "USE1_OutputTokenCount_LCtx", "price": "888.0", "unit": "Units"},
        {"dimension": "USE1_CacheWrite1hInputTokenCount_LCtx", "price": "888.0", "unit": "Units"},
        # batch variant.
        {"dimension": "USE1_InputTokenCount_Batch", "price": "777.0", "unit": "Units"},
        {"dimension": "USE1_OutputTokenCount_Batch", "price": "777.0", "unit": "Units"},
        # long-context + global combined.
        {"dimension": "USE1_InputTokenCount_LCtx_Global", "price": "666.0", "unit": "Units"},
        # a different region entirely.
        {"dimension": "USW2_InputTokenCount", "price": "555.0", "unit": "Units"},
        {"dimension": "USW2_OutputTokenCount", "price": "555.0", "unit": "Units"},
        {"dimension": "USW2_CacheWrite1hInputTokenCount", "price": "555.0", "unit": "Units"},
        # eu region.
        {"dimension": "EUC1_InputTokenCount", "price": "444.0", "unit": "Units"},
    ]


def _offers_response(rate_card: list[dict]) -> dict:
    """Wrap a rateCard the way list_foundation_model_agreement_offers does."""
    return {
        "offers": [
            {
                "termDetails": {
                    "usageBasedPricingTerm": {"rateCard": rate_card},
                },
            },
        ],
    }


# Per-model expected rates, sourced from AWS this session.
_OPUS_CARD = _rate_card(
    input_price="5.5",
    output_price="27.5",
    cache_read_price="0.55",
    cache_write_1h_price="11.0",
    cache_write_5m_price="6.875",
)
_SONNET_CARD = _rate_card(
    input_price="3.3",
    output_price="16.5",
    cache_read_price="0.33",
    cache_write_1h_price="6.6",
    cache_write_5m_price="4.125",
)
_HAIKU_CARD = _rate_card(
    input_price="1.1",
    output_price="5.5",
    cache_read_price="0.11",
    cache_write_1h_price="2.2",
    cache_write_5m_price="1.375",
)


# --------------------------------------------------------------------------- #
# extract_rate_card() — response-shape navigation
# --------------------------------------------------------------------------- #


def test_extract_rate_card_pulls_nested_card():
    card = extract_rate_card(_offers_response(_OPUS_CARD))
    assert card is _OPUS_CARD


def test_extract_rate_card_raises_on_empty_offers():
    with pytest.raises(ValueError, match="no offers"):
        extract_rate_card({"offers": []})


def test_extract_rate_card_raises_on_missing_term():
    resp = {"offers": [{"termDetails": {}}]}
    with pytest.raises(ValueError, match="rateCard"):
        extract_rate_card(resp)


# --------------------------------------------------------------------------- #
# select_dimension() — exact-match selection, ignores decoy suffixes
# --------------------------------------------------------------------------- #


def test_select_dimension_picks_exact_use1_dim():
    card = _OPUS_CARD
    assert select_dimension(card, "USE1_InputTokenCount") == pytest.approx(5.5)
    assert select_dimension(card, "USE1_OutputTokenCount") == pytest.approx(27.5)


def test_select_dimension_is_exact_not_prefix():
    # "USE1_InputTokenCount" must NOT match "USE1_InputTokenCount_Global"
    # / "_LCtx" / "_Batch" — exact equality only.
    card = _OPUS_CARD
    # The non-suffixed dim is 5.5; the _Global decoy is 999. If selection
    # were a prefix/substring match it could grab the wrong one.
    assert select_dimension(card, "USE1_InputTokenCount") == pytest.approx(5.5)


def test_select_dimension_missing_dim_raises():
    with pytest.raises(KeyError):
        select_dimension(_OPUS_CARD, "USE1_NopeTokenCount")


# --------------------------------------------------------------------------- #
# parse_rate_card() — builds the 4-field block, 1h cacheWrite, excludes decoys
# --------------------------------------------------------------------------- #


def test_parse_rate_card_opus():
    rates = parse_rate_card(_OPUS_CARD, region_prefix="USE1")
    assert rates == {"input": 5.5, "output": 27.5, "cache_read": 0.55, "cache_write": 11.0}


def test_parse_rate_card_sonnet():
    rates = parse_rate_card(_SONNET_CARD, region_prefix="USE1")
    assert rates == {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}


def test_parse_rate_card_haiku():
    rates = parse_rate_card(_HAIKU_CARD, region_prefix="USE1")
    assert rates == {"input": 1.1, "output": 5.5, "cache_read": 0.11, "cache_write": 2.2}


def test_parse_rate_card_uses_1h_cache_write_not_5m():
    # The 5-minute dim is 6.875 for Opus; the 1h dim is 11.0. We pin
    # cacheRetention=long so cache_write MUST be the 1h value.
    rates = parse_rate_card(_OPUS_CARD, region_prefix="USE1")
    assert rates["cache_write"] == pytest.approx(11.0)
    assert rates["cache_write"] != pytest.approx(6.875)


def test_parse_rate_card_excludes_global_lctx_batch_and_other_regions():
    # The decoy prices (999/888/777/666/555/444) must never leak in. If any
    # exclusion were broken, one of the four fields would carry a decoy value.
    rates = parse_rate_card(_OPUS_CARD, region_prefix="USE1")
    for v in rates.values():
        assert v not in (999.0, 888.0, 777.0, 666.0, 555.0, 444.0)


def test_parse_rate_card_honors_region_prefix():
    # With region_prefix=USW2 the parser should read the USW2 dims (all 555
    # in the fixture for the dims that exist) — proving the prefix is wired,
    # not hardcoded to USE1.
    card = [
        {"dimension": "USW2_InputTokenCount", "price": "1.0", "unit": "Units"},
        {"dimension": "USW2_OutputTokenCount", "price": "2.0", "unit": "Units"},
        {"dimension": "USW2_CacheReadInputTokenCount", "price": "0.3", "unit": "Units"},
        {"dimension": "USW2_CacheWrite1hInputTokenCount", "price": "4.0", "unit": "Units"},
        {"dimension": "USE1_InputTokenCount", "price": "999.0", "unit": "Units"},
    ]
    rates = parse_rate_card(card, region_prefix="USW2")
    assert rates == {"input": 1.0, "output": 2.0, "cache_read": 0.3, "cache_write": 4.0}


# --------------------------------------------------------------------------- #
# profile_id_for() — bare foundation id → us. inference profile id
# --------------------------------------------------------------------------- #


def test_profile_id_for_prepends_region_profile_prefix():
    assert profile_id_for("anthropic.claude-opus-4-6-v1") == "us.anthropic.claude-opus-4-6-v1"
    assert profile_id_for("anthropic.claude-haiku-4-5-20251001-v1:0") == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# --------------------------------------------------------------------------- #
# validate_profile_ids() — every catalog id needs an ACTIVE us. profile
# --------------------------------------------------------------------------- #


def _profile(profile_id: str, status: str = "ACTIVE") -> dict:
    return {"inferenceProfileId": profile_id, "status": status}


def test_validate_profile_ids_all_present_and_active():
    catalog = ["anthropic.claude-opus-4-6-v1", "anthropic.claude-sonnet-4-6"]
    profiles = [
        _profile("us.anthropic.claude-opus-4-6-v1"),
        _profile("us.anthropic.claude-sonnet-4-6"),
        _profile("us.anthropic.claude-some-other-model"),
    ]
    missing = validate_profile_ids(catalog, profiles)
    assert missing == []


def test_validate_profile_ids_flags_bogus_id():
    # The invalid-Haiku-id bug: a catalog id with no matching active profile.
    catalog = ["anthropic.claude-opus-4-6-v1", "anthropic.claude-haiku-4-5"]
    profiles = [
        _profile("us.anthropic.claude-opus-4-6-v1"),
        _profile("us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ]
    missing = validate_profile_ids(catalog, profiles)
    assert missing == ["anthropic.claude-haiku-4-5"]


def test_validate_profile_ids_flags_inactive_profile():
    # A profile that exists but isn't ACTIVE doesn't count as available.
    catalog = ["anthropic.claude-opus-4-6-v1"]
    profiles = [_profile("us.anthropic.claude-opus-4-6-v1", status="INACTIVE")]
    missing = validate_profile_ids(catalog, profiles)
    assert missing == ["anthropic.claude-opus-4-6-v1"]


# --------------------------------------------------------------------------- #
# compare_rates() — drift detection vs committed _RATES
# --------------------------------------------------------------------------- #


def test_compare_rates_no_drift_when_equal():
    committed = {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    aws = {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    drift = compare_rates("anthropic.claude-sonnet-4-6", committed=committed, aws=aws, tolerance=0.001)
    assert drift.has_drift is False
    assert drift.fields == []


def test_compare_rates_flags_each_drifted_field():
    committed = {"input": 14.85, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    aws = {"input": 5.5, "output": 27.5, "cache_read": 0.33, "cache_write": 6.6}
    drift = compare_rates("anthropic.claude-opus-4-6-v1", committed=committed, aws=aws, tolerance=0.001)
    assert drift.has_drift is True
    drifted = {f.field for f in drift.fields}
    assert drifted == {"input", "output"}


def test_compare_rates_tolerance_absorbs_tiny_float_noise():
    committed = {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    aws = {"input": 3.3000004, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    drift = compare_rates("anthropic.claude-sonnet-4-6", committed=committed, aws=aws, tolerance=0.001)
    assert drift.has_drift is False


def test_compare_rates_missing_committed_field_is_drift():
    committed = {"input": 3.3, "output": 16.5, "cache_read": 0.33}  # no cache_write
    aws = {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 6.6}
    drift = compare_rates("anthropic.claude-sonnet-4-6", committed=committed, aws=aws, tolerance=0.001)
    assert drift.has_drift is True
    assert any(f.field == "cache_write" for f in drift.fields)


# --------------------------------------------------------------------------- #
# Expected-value sanity: the parser output must equal what C6 committed.
# --------------------------------------------------------------------------- #


def test_expected_constant_matches_parsed_cards():
    # AWS_RATES_EXPECTED documents the verified AWS values; the fixtures are
    # built to those, so the parser output must agree with the constant. This
    # is the in-repo guard that the fixtures didn't drift from spec.
    assert parse_rate_card(_OPUS_CARD, region_prefix="USE1") == AWS_RATES_EXPECTED["anthropic.claude-opus-4-6-v1"]
    assert parse_rate_card(_SONNET_CARD, region_prefix="USE1") == AWS_RATES_EXPECTED["anthropic.claude-sonnet-4-6"]
    assert (
        parse_rate_card(_HAIKU_CARD, region_prefix="USE1")
        == AWS_RATES_EXPECTED["anthropic.claude-haiku-4-5-20251001-v1:0"]
    )


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def test_argparse_defaults():
    args = build_arg_parser().parse_args([])
    assert args.region_prefix == "USE1"
    assert args.region == "us-east-1"
    assert args.emit is False
    assert args.tolerance == pytest.approx(0.01)


def test_argparse_emit_flag():
    args = build_arg_parser().parse_args(["--emit"])
    assert args.emit is True


def test_argparse_region_overrides():
    args = build_arg_parser().parse_args(["--region", "us-west-2", "--region-prefix", "USW2"])
    assert args.region == "us-west-2"
    assert args.region_prefix == "USW2"


# --------------------------------------------------------------------------- #
# main() — end-to-end with AWS mocked
# --------------------------------------------------------------------------- #


def _profiles_for_catalog(catalog: list[str]) -> list[dict]:
    return [_profile(profile_id_for(m)) for m in catalog]


def _fetch_offer_side_effect(card_by_model: dict[str, list[dict]]):
    async def _impl(model_id: str, *, region: str):  # noqa: ARG001
        return card_by_model[model_id]

    return AsyncMock(side_effect=_impl)


async def test_main_clean_run_exits_zero(capsys):
    args = argparse.Namespace(
        region="us-east-1",
        region_prefix="USE1",
        tolerance=0.01,
        emit=False,
    )
    catalog = [
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    ]
    cards = {
        "anthropic.claude-opus-4-6-v1": _OPUS_CARD,
        "anthropic.claude-sonnet-4-6": _SONNET_CARD,
        "anthropic.claude-haiku-4-5-20251001-v1:0": _HAIKU_CARD,
    }
    with (
        patch("scripts.sync_bedrock_rates.catalog_model_ids", return_value=catalog),
        patch("scripts.sync_bedrock_rates.committed_rate", side_effect=lambda m: AWS_RATES_EXPECTED[m]),
        patch("scripts.sync_bedrock_rates.fetch_agreement_rate_card", new=_fetch_offer_side_effect(cards)),
        patch(
            "scripts.sync_bedrock_rates.fetch_inference_profiles",
            new=AsyncMock(return_value=_profiles_for_catalog(catalog)),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out
    assert "DRIFT" not in out


async def test_main_flags_drift_and_exits_nonzero(capsys):
    args = argparse.Namespace(
        region="us-east-1",
        region_prefix="USE1",
        tolerance=0.01,
        emit=False,
    )
    catalog = ["anthropic.claude-opus-4-6-v1"]
    cards = {"anthropic.claude-opus-4-6-v1": _OPUS_CARD}
    # Committed input is the OLD wrong 2.7x value → drift.
    bad_committed = {"input": 14.85, "output": 27.5, "cache_read": 0.55, "cache_write": 11.0}
    with (
        patch("scripts.sync_bedrock_rates.catalog_model_ids", return_value=catalog),
        patch("scripts.sync_bedrock_rates.committed_rate", side_effect=lambda m: bad_committed),
        patch("scripts.sync_bedrock_rates.fetch_agreement_rate_card", new=_fetch_offer_side_effect(cards)),
        patch(
            "scripts.sync_bedrock_rates.fetch_inference_profiles",
            new=AsyncMock(return_value=_profiles_for_catalog(catalog)),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "DRIFT" in out
    assert "input" in out


async def test_main_flags_missing_profile_and_exits_nonzero(capsys):
    args = argparse.Namespace(
        region="us-east-1",
        region_prefix="USE1",
        tolerance=0.01,
        emit=False,
    )
    catalog = ["anthropic.claude-opus-4-6-v1"]
    cards = {"anthropic.claude-opus-4-6-v1": _OPUS_CARD}
    with (
        patch("scripts.sync_bedrock_rates.catalog_model_ids", return_value=catalog),
        patch("scripts.sync_bedrock_rates.committed_rate", side_effect=lambda m: AWS_RATES_EXPECTED[m]),
        patch("scripts.sync_bedrock_rates.fetch_agreement_rate_card", new=_fetch_offer_side_effect(cards)),
        # No active profile for the catalog id → invalid id detected.
        patch(
            "scripts.sync_bedrock_rates.fetch_inference_profiles",
            new=AsyncMock(return_value=[]),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "no active" in out.lower() or "missing" in out.lower()


async def test_main_emit_prints_python_literals(capsys):
    args = argparse.Namespace(
        region="us-east-1",
        region_prefix="USE1",
        tolerance=0.01,
        emit=True,
    )
    catalog = ["anthropic.claude-sonnet-4-6"]
    cards = {"anthropic.claude-sonnet-4-6": _SONNET_CARD}
    with (
        patch("scripts.sync_bedrock_rates.catalog_model_ids", return_value=catalog),
        patch("scripts.sync_bedrock_rates.committed_rate", side_effect=lambda m: AWS_RATES_EXPECTED[m]),
        patch("scripts.sync_bedrock_rates.fetch_agreement_rate_card", new=_fetch_offer_side_effect(cards)),
        patch(
            "scripts.sync_bedrock_rates.fetch_inference_profiles",
            new=AsyncMock(return_value=_profiles_for_catalog(catalog)),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 0
    # Emitted block carries the snake_case _RATES literal AND the camelCase
    # cost block, ready to paste into a human's review.
    assert "anthropic.claude-sonnet-4-6" in out
    assert '"cache_read"' in out
    assert '"cacheWrite"' in out


# --------------------------------------------------------------------------- #
# ModelDrift dataclass
# --------------------------------------------------------------------------- #


def test_model_drift_has_drift_false_when_empty():
    d = ModelDrift(model_id="m", fields=[])
    assert d.has_drift is False


def test_model_drift_has_drift_true_when_fields():
    from scripts.sync_bedrock_rates import FieldDrift

    d = ModelDrift(model_id="m", fields=[FieldDrift(field="input", committed=1.0, aws=2.0)])
    assert d.has_drift is True
