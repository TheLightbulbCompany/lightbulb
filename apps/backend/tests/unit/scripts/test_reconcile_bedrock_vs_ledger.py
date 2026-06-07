"""Unit tests for the Bedrock-vs-ledger reconciliation script.

All AWS/DDB access is mocked — no live calls. The headline case mirrors
the real incident: AWS metered ~510M cache-read tokens while the ledger
recorded 0 (cache tokens dropped), which must surface as ~100% divergence
and an ALERT / non-zero exit.
"""

from __future__ import annotations

import argparse
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from scripts.reconcile_bedrock_vs_ledger import (
    ClassResult,
    DollarResult,
    build_arg_parser,
    fetch_bedrock_actual_cost_usd,
    fetch_bedrock_owner_ids,
    fetch_ledger_period_totals,
    fetch_ledger_spend_usd,
    format_dollar_report,
    format_report,
    main,
    reconcile,
    resolve_window,
    sum_ce_bedrock_cost_usd,
    sum_metric_datapoints,
    _is_bedrock_service,
    _is_month_aligned,
    _period_strings,
)


# --------------------------------------------------------------------------- #
# Divergence math
# --------------------------------------------------------------------------- #


def test_class_result_exact_match_is_zero_divergence():
    r = ClassResult(name="input", aws_total=1_000_000, ledger_total=1_000_000)
    assert r.abs_diff == 0
    assert r.divergence_pct == 0.0


def test_class_result_cache_undercount_is_full_divergence():
    """The incident: AWS metered 510M cache tokens, ledger recorded 0."""
    r = ClassResult(name="cache_read", aws_total=510_000_000, ledger_total=0)
    assert r.abs_diff == 510_000_000
    assert r.divergence_pct == 100.0


def test_class_result_partial_undercount():
    # Ledger captured a quarter of what AWS metered → 75% divergence.
    r = ClassResult(name="input", aws_total=400, ledger_total=100)
    assert r.divergence_pct == pytest.approx(75.0)


def test_class_result_overcount_uses_aws_denominator():
    # Ledger reports more than AWS billed → still measured against AWS.
    r = ClassResult(name="output", aws_total=100, ledger_total=150)
    assert r.abs_diff == 50
    assert r.divergence_pct == pytest.approx(50.0)


def test_class_result_zero_aws_zero_ledger_is_zero():
    r = ClassResult(name="cache_write", aws_total=0, ledger_total=0)
    assert r.divergence_pct == 0.0


def test_class_result_zero_aws_nonzero_ledger_is_full_divergence():
    # AWS billed nothing but the ledger invented tokens — maximal divergence.
    r = ClassResult(name="cache_write", aws_total=0, ledger_total=42)
    assert r.divergence_pct == 100.0


# --------------------------------------------------------------------------- #
# reconcile() pairing
# --------------------------------------------------------------------------- #


def test_reconcile_pairs_metrics_to_columns():
    aws_metrics = {
        "InputTokenCount": 1000,
        "OutputTokenCount": 200,
        "CacheReadInputTokenCount": 510_000_000,
        "CacheWriteInputTokenCount": 30,
    }
    ledger_totals = {
        "total_input_tokens": 1000,
        "total_output_tokens": 200,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
    }
    results = {r.name: r for r in reconcile(aws_metrics, ledger_totals)}

    assert results["input"].aws_total == 1000
    assert results["input"].ledger_total == 1000
    assert results["cache_read"].aws_total == 510_000_000
    assert results["cache_read"].ledger_total == 0
    assert results["cache_read"].divergence_pct == 100.0
    assert results["cache_write"].ledger_total == 0


def test_reconcile_defaults_missing_keys_to_zero():
    results = {r.name: r for r in reconcile({}, {})}
    assert set(results) == {"input", "output", "cache_read", "cache_write"}
    for r in results.values():
        assert r.aws_total == 0
        assert r.ledger_total == 0
        assert r.divergence_pct == 0.0


# --------------------------------------------------------------------------- #
# format_report() + threshold alerting
# --------------------------------------------------------------------------- #


def test_format_report_alerts_on_cache_undercount():
    results = reconcile(
        {"CacheReadInputTokenCount": 510_000_000, "InputTokenCount": 1000},
        {"total_input_tokens": 1000, "total_cache_read_tokens": 0},
    )
    text, alerted = format_report(results, threshold_pct=20.0)
    assert alerted is True
    assert "OVER THRESHOLD" in text
    assert "cache_read" in text
    assert "OVERALL" in text


def test_format_report_no_alert_when_within_threshold():
    # Every class within 10% of AWS.
    results = reconcile(
        {
            "InputTokenCount": 1000,
            "OutputTokenCount": 1000,
            "CacheReadInputTokenCount": 1000,
            "CacheWriteInputTokenCount": 1000,
        },
        {
            "total_input_tokens": 950,
            "total_output_tokens": 1050,
            "total_cache_read_tokens": 1000,
            "total_cache_write_tokens": 980,
        },
    )
    text, alerted = format_report(results, threshold_pct=20.0)
    assert alerted is False
    assert "OVER THRESHOLD" not in text


def test_format_report_threshold_is_strict_greater_than():
    # Exactly at threshold (20%) must NOT alert; just over must.
    at = reconcile({"InputTokenCount": 100}, {"total_input_tokens": 80})
    _, at_alerted = format_report(at, threshold_pct=20.0)
    assert at_alerted is False

    over = reconcile({"InputTokenCount": 100}, {"total_input_tokens": 79})
    _, over_alerted = format_report(over, threshold_pct=20.0)
    assert over_alerted is True


def test_format_report_overall_row_is_its_own_check():
    # The OVERALL aggregate is evaluated against the threshold independently
    # of the per-class flags — here one heavily-skewed class drives the
    # aggregate over 20%.
    results = reconcile(
        {
            "InputTokenCount": 1000,
            "OutputTokenCount": 10,
            "CacheReadInputTokenCount": 10,
            "CacheWriteInputTokenCount": 10,
        },
        {
            "total_input_tokens": 700,
            "total_output_tokens": 10,
            "total_cache_read_tokens": 10,
            "total_cache_write_tokens": 10,
        },
    )
    text, alerted = format_report(results, threshold_pct=20.0)
    assert alerted is True


# --------------------------------------------------------------------------- #
# sum_metric_datapoints()
# --------------------------------------------------------------------------- #


def test_sum_metric_datapoints_folds_sum_field():
    resp = {"Datapoints": [{"Sum": 100.0}, {"Sum": 250.0}, {"Sum": 1.4}]}
    assert sum_metric_datapoints(resp) == 351


def test_sum_metric_datapoints_empty():
    assert sum_metric_datapoints({"Datapoints": []}) == 0
    assert sum_metric_datapoints({}) == 0


# --------------------------------------------------------------------------- #
# Period-string generation + month alignment
# --------------------------------------------------------------------------- #


def test_period_strings_single_month():
    assert _period_strings(date(2026, 6, 1), date(2026, 7, 1)) == ["2026-06"]


def test_period_strings_spans_two_months():
    assert _period_strings(date(2026, 5, 15), date(2026, 6, 10)) == ["2026-05", "2026-06"]


def test_period_strings_crosses_year_boundary():
    assert _period_strings(date(2025, 12, 20), date(2026, 2, 5)) == [
        "2025-12",
        "2026-01",
        "2026-02",
    ]


def test_period_strings_end_before_start_is_empty():
    assert _period_strings(date(2026, 6, 1), date(2026, 6, 1)) == []
    assert _period_strings(date(2026, 6, 2), date(2026, 6, 1)) == []


def test_period_strings_end_exclusive_does_not_pull_extra_month():
    # End is the 1st of July (exclusive) → only June, not July.
    assert _period_strings(date(2026, 6, 1), date(2026, 7, 1)) == ["2026-06"]


def test_is_month_aligned():
    assert _is_month_aligned(date(2026, 6, 1), date(2026, 7, 1)) is True
    assert _is_month_aligned(date(2026, 6, 1), date(2026, 6, 15)) is False
    assert _is_month_aligned(date(2026, 6, 3), date(2026, 7, 1)) is False


# --------------------------------------------------------------------------- #
# fetch_ledger_period_totals() — skips lifetime + member:* rows
# --------------------------------------------------------------------------- #


async def test_fetch_ledger_period_totals_sums_only_period_rows():
    """Lifetime and member:* rows must be excluded to avoid double-count."""
    items = [
        # Two owners' June period rollups — summed.
        {
            "owner_id": "user_a",
            "period": "2026-06",
            "total_input_tokens": 100,
            "total_output_tokens": 10,
            "total_cache_read_tokens": 1000,
            "total_cache_write_tokens": 5,
        },
        {
            "owner_id": "org_b",
            "period": "2026-06",
            "total_input_tokens": 200,
            "total_output_tokens": 20,
            "total_cache_read_tokens": 2000,
            "total_cache_write_tokens": 10,
        },
        # Lifetime row — SKIPPED.
        {
            "owner_id": "user_a",
            "period": "lifetime",
            "total_input_tokens": 999999,
            "total_output_tokens": 999999,
            "total_cache_read_tokens": 999999,
            "total_cache_write_tokens": 999999,
        },
        # Member row — SKIPPED (subset of org_b's period rollup).
        {
            "owner_id": "org_b",
            "period": "member:user_m:2026-06",
            "total_input_tokens": 150,
            "total_output_tokens": 15,
            "total_cache_read_tokens": 1500,
            "total_cache_write_tokens": 7,
        },
        # A different month — SKIPPED (not in the requested window).
        {
            "owner_id": "user_a",
            "period": "2026-05",
            "total_input_tokens": 500,
            "total_output_tokens": 50,
            "total_cache_read_tokens": 5000,
            "total_cache_write_tokens": 25,
        },
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        totals = await fetch_ledger_period_totals(["2026-06"])

    assert totals == {
        "total_input_tokens": 300,
        "total_output_tokens": 30,
        "total_cache_read_tokens": 3000,
        "total_cache_write_tokens": 15,
    }


async def test_fetch_ledger_period_totals_paginates():
    page1 = {
        "Items": [
            {"owner_id": "u1", "period": "2026-06", "total_input_tokens": 1},
        ],
        "LastEvaluatedKey": {"owner_id": "u1", "period": "2026-06"},
    }
    page2 = {
        "Items": [
            {"owner_id": "u2", "period": "2026-06", "total_input_tokens": 4},
        ],
    }
    fake_table = _FakeTable(pages=[page1, page2])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        totals = await fetch_ledger_period_totals(["2026-06"])

    assert totals["total_input_tokens"] == 5
    assert fake_table.scan_calls == 2


async def test_fetch_ledger_period_totals_sums_multiple_periods():
    items = [
        {"owner_id": "u1", "period": "2026-05", "total_input_tokens": 10},
        {"owner_id": "u1", "period": "2026-06", "total_input_tokens": 20},
        {"owner_id": "u1", "period": "2026-07", "total_input_tokens": 999},
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        totals = await fetch_ledger_period_totals(["2026-05", "2026-06"])

    assert totals["total_input_tokens"] == 30  # 2026-07 excluded


# --------------------------------------------------------------------------- #
# Bedrock-only token reconciliation — exclude non-Bedrock owners.
#
# After D6 (record_usage records non-Bedrock model VOLUME into the same
# YYYY-MM period rows with spend 0), the token side must filter to
# Bedrock-only owners or it false-alarms after a provider migration: the
# AWS/Bedrock CloudWatch totals stay correct while the ledger token columns
# get inflated by ChatGPT-OAuth / OpenAI / BYO traffic.
# --------------------------------------------------------------------------- #


async def test_fetch_bedrock_owner_ids_returns_only_bedrock_owners():
    """Scan billing-accounts → set of owner_ids on the Bedrock path only."""
    items = [
        {"owner_id": "user_a", "provider_choice": "bedrock_claude"},
        {"owner_id": "org_b", "provider_choice": "bedrock_claude"},
        {"owner_id": "user_c", "provider_choice": "chatgpt_oauth"},
        {"owner_id": "org_d", "provider_choice": "byo_key", "byo_provider": "anthropic"},
        # Phantom / pre-onboarding row with no provider_choice yet — excluded.
        {"owner_id": "user_e"},
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        owners = await fetch_bedrock_owner_ids()

    assert owners == {"user_a", "org_b"}


async def test_fetch_bedrock_owner_ids_paginates():
    page1 = {
        "Items": [{"owner_id": "u1", "provider_choice": "bedrock_claude"}],
        "LastEvaluatedKey": {"owner_id": "u1"},
    }
    page2 = {
        "Items": [
            {"owner_id": "u2", "provider_choice": "chatgpt_oauth"},
            {"owner_id": "u3", "provider_choice": "bedrock_claude"},
        ],
    }
    fake_table = _FakeTable(pages=[page1, page2])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        owners = await fetch_bedrock_owner_ids()

    assert owners == {"u1", "u3"}
    assert fake_table.scan_calls == 2


async def test_fetch_ledger_period_totals_filters_to_bedrock_owners():
    """A period with mixed Bedrock + non-Bedrock owner rows must count ONLY
    the Bedrock owners' tokens — non-Bedrock volume (spend 0) would inflate
    the comparison against the AWS/Bedrock CloudWatch totals."""
    items = [
        # Bedrock owner — COUNTED.
        {
            "owner_id": "user_bedrock",
            "period": "2026-06",
            "total_input_tokens": 100,
            "total_output_tokens": 10,
            "total_cache_read_tokens": 1000,
            "total_cache_write_tokens": 5,
        },
        # Non-Bedrock owner with token VOLUME (spend 0) — EXCLUDED.
        {
            "owner_id": "user_chatgpt",
            "period": "2026-06",
            "total_input_tokens": 999_999,
            "total_output_tokens": 999_999,
            "total_cache_read_tokens": 999_999,
            "total_cache_write_tokens": 999_999,
        },
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        totals = await fetch_ledger_period_totals(["2026-06"], bedrock_owner_ids={"user_bedrock"})

    assert totals == {
        "total_input_tokens": 100,
        "total_output_tokens": 10,
        "total_cache_read_tokens": 1000,
        "total_cache_write_tokens": 5,
    }


async def test_fetch_ledger_period_totals_none_filter_counts_all():
    """``bedrock_owner_ids=None`` keeps the un-filtered behavior (every
    period row counted) — used only by tests; ``main`` always passes a set."""
    items = [
        {"owner_id": "a", "period": "2026-06", "total_input_tokens": 1},
        {"owner_id": "b", "period": "2026-06", "total_input_tokens": 2},
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        totals = await fetch_ledger_period_totals(["2026-06"], bedrock_owner_ids=None)

    assert totals["total_input_tokens"] == 3


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def test_argparse_requires_env():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_argparse_rejects_unknown_env():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--env", "staging"])


def test_argparse_days_window():
    parser = build_arg_parser()
    args = parser.parse_args(["--env", "prod", "--days", "30"])
    assert args.env == "prod"
    assert args.days == 30
    assert args.threshold_pct == 20.0


def test_argparse_start_end_window_and_threshold():
    parser = build_arg_parser()
    args = parser.parse_args(["--env", "dev", "--start", "2026-05-01", "--end", "2026-06-01", "--threshold-pct", "15"])
    assert args.start == date(2026, 5, 1)
    assert args.end == date(2026, 6, 1)
    assert args.threshold_pct == 15.0


def test_resolve_window_rejects_days_with_start():
    # argparse can't express "either --days OR (--start AND --end)", so the
    # combination is caught at resolve time, not parse time.
    parser = build_arg_parser()
    args = parser.parse_args(["--env", "prod", "--days", "7", "--start", "2026-05-01"])
    with pytest.raises(ValueError):
        resolve_window(args)


def test_argparse_rejects_bad_iso_date():
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--env", "prod", "--start", "not-a-date", "--end", "2026-06-01"])


# --------------------------------------------------------------------------- #
# resolve_window()
# --------------------------------------------------------------------------- #


def test_resolve_window_days():
    args = argparse.Namespace(days=10, start=None, end=None)
    start, end = resolve_window(args)
    assert (end - start).days == 10


def test_resolve_window_explicit_dates():
    args = argparse.Namespace(days=None, start=date(2026, 5, 1), end=date(2026, 6, 1))
    assert resolve_window(args) == (date(2026, 5, 1), date(2026, 6, 1))


def test_resolve_window_rejects_end_before_start():
    args = argparse.Namespace(days=None, start=date(2026, 6, 1), end=date(2026, 5, 1))
    with pytest.raises(ValueError):
        resolve_window(args)


def test_resolve_window_rejects_nonpositive_days():
    args = argparse.Namespace(days=0, start=None, end=None)
    with pytest.raises(ValueError):
        resolve_window(args)


def test_resolve_window_requires_a_window():
    args = argparse.Namespace(days=None, start=None, end=None)
    with pytest.raises(ValueError):
        resolve_window(args)


# --------------------------------------------------------------------------- #
# main() — end-to-end with both AWS + DDB mocked
# --------------------------------------------------------------------------- #


async def test_main_alerts_and_exits_nonzero_on_cache_undercount(capsys):
    """The incident, end-to-end: AWS=510M cache, ledger=0 → exit 1 + ALERT."""
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    aws_metrics = {
        "InputTokenCount": 1_000_000,
        "OutputTokenCount": 200_000,
        "CacheReadInputTokenCount": 510_000_000,
        "CacheWriteInputTokenCount": 20_000_000,
    }
    ledger_totals = {
        "total_input_tokens": 1_000_000,
        "total_output_tokens": 200_000,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
    }

    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value=aws_metrics),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value=ledger_totals),
        ),
        # Dollar side clean so the alert comes purely from the token undercount.
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=100.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=100.0),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "ALERT" in out
    assert "cache_read" in out


async def test_main_clean_run_exits_zero(capsys):
    args = argparse.Namespace(
        env="dev",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    matched = {
        "InputTokenCount": 1000,
        "OutputTokenCount": 1000,
        "CacheReadInputTokenCount": 1000,
        "CacheWriteInputTokenCount": 1000,
    }
    ledger = {
        "total_input_tokens": 1000,
        "total_output_tokens": 1000,
        "total_cache_read_tokens": 1000,
        "total_cache_write_tokens": 1000,
    }
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value=matched),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value=ledger),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=100.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=100.0),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out
    assert "ALERT" not in out


async def test_main_warns_on_non_month_aligned_window(capsys):
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 3),
        end=date(2026, 6, 20),
        threshold_pct=20.0,
    )
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=0.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await main(args)

    out = capsys.readouterr().out
    assert "not month-aligned" in out


async def test_main_returns_2_on_invalid_window(capsys):
    args = argparse.Namespace(env="prod", days=None, start=None, end=None, threshold_pct=20.0)
    rc = await main(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert "ERROR" in out


# --------------------------------------------------------------------------- #
# _is_bedrock_service() — SERVICE-dimension matching
# --------------------------------------------------------------------------- #


def test_is_bedrock_service_matches_bedrock_edition_models():
    assert _is_bedrock_service("Claude Opus 4.6 (Amazon Bedrock Edition)") is True
    assert _is_bedrock_service("Claude Sonnet 4.6 (Amazon Bedrock Edition)") is True
    assert _is_bedrock_service("Claude Haiku 4.5 (Amazon Bedrock Edition)") is True


def test_is_bedrock_service_matches_base_service():
    assert _is_bedrock_service("Amazon Bedrock") is True


def test_is_bedrock_service_excludes_unrelated_services():
    assert _is_bedrock_service("Amazon S3") is False
    assert _is_bedrock_service("Amazon Elastic Compute Cloud - Compute") is False
    assert _is_bedrock_service("AWS Cost Explorer") is False


def test_is_bedrock_service_is_case_and_whitespace_tolerant():
    # Cost Explorer occasionally returns leading/trailing whitespace; the
    # match should not be defeated by it, nor by minor case drift.
    assert _is_bedrock_service("  Amazon Bedrock  ") is True
    assert _is_bedrock_service("claude opus 4.6 (amazon bedrock edition)") is True


# --------------------------------------------------------------------------- #
# sum_ce_bedrock_cost_usd() — parse + filter the get_cost_and_usage response
# --------------------------------------------------------------------------- #


def _ce_grouped_response(service_costs: dict[str, str]) -> dict:
    """Build a get_cost_and_usage SERVICE-grouped response shape."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-06-01"},
                "Groups": [
                    {
                        "Keys": [service],
                        "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}},
                    }
                    for service, amount in service_costs.items()
                ],
                "Total": {},
            }
        ]
    }


def test_sum_ce_bedrock_cost_sums_only_bedrock_groups():
    resp = _ce_grouped_response(
        {
            "Claude Opus 4.6 (Amazon Bedrock Edition)": "1500.00",
            "Claude Sonnet 4.6 (Amazon Bedrock Edition)": "800.50",
            "Claude Haiku 4.5 (Amazon Bedrock Edition)": "99.50",
            "Amazon Bedrock": "12.00",
            "Amazon S3": "42.00",  # EXCLUDED
        }
    )
    total = sum_ce_bedrock_cost_usd(resp)
    # 1500 + 800.50 + 99.50 + 12 = 2412 ; S3's 42 must not count.
    assert total == pytest.approx(2412.00)


def test_sum_ce_bedrock_cost_excludes_all_non_bedrock():
    resp = _ce_grouped_response({"Amazon S3": "10.00", "AWS Lambda": "5.00"})
    assert sum_ce_bedrock_cost_usd(resp) == pytest.approx(0.0)


def test_sum_ce_bedrock_cost_folds_multiple_time_buckets():
    # DAILY/MONTHLY granularity can return several ResultsByTime entries.
    resp = {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["Amazon Bedrock"],
                        "Metrics": {"UnblendedCost": {"Amount": "100.00"}},
                    }
                ],
            },
            {
                "Groups": [
                    {
                        "Keys": ["Claude Opus 4.6 (Amazon Bedrock Edition)"],
                        "Metrics": {"UnblendedCost": {"Amount": "250.00"}},
                    },
                    {
                        "Keys": ["Amazon S3"],
                        "Metrics": {"UnblendedCost": {"Amount": "9.00"}},
                    },
                ],
            },
        ]
    }
    assert sum_ce_bedrock_cost_usd(resp) == pytest.approx(350.00)


def test_sum_ce_bedrock_cost_empty_response():
    assert sum_ce_bedrock_cost_usd({}) == pytest.approx(0.0)
    assert sum_ce_bedrock_cost_usd({"ResultsByTime": []}) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# fetch_bedrock_actual_cost_usd() — boto3 ce client mocked
# --------------------------------------------------------------------------- #


async def test_fetch_bedrock_actual_cost_usd_uses_mocked_ce_client():
    """No live AWS: a fake ce client returns a grouped response."""
    resp = _ce_grouped_response(
        {
            "Claude Opus 4.6 (Amazon Bedrock Edition)": "2000.00",
            "Amazon Bedrock": "400.00",
            "Amazon S3": "50.00",
        }
    )

    captured: dict = {}

    class _FakeCeClient:
        def get_cost_and_usage(self, **kwargs):
            captured.update(kwargs)
            return resp

    fake_boto3 = _FakeBoto3({"ce": _FakeCeClient()})

    with patch.dict("sys.modules", {"boto3": fake_boto3}):
        total = await fetch_bedrock_actual_cost_usd(start=date(2026, 5, 1), end=date(2026, 6, 1), region="us-east-1")

    assert total == pytest.approx(2400.00)  # 2000 + 400 ; S3 excluded
    # End-exclusive ISO dates and a SERVICE GroupBy on UnblendedCost.
    assert captured["TimePeriod"] == {"Start": "2026-05-01", "End": "2026-06-01"}
    assert captured["Metrics"] == ["UnblendedCost"]
    assert {"Type": "DIMENSION", "Key": "SERVICE"} in captured["GroupBy"]


# --------------------------------------------------------------------------- #
# fetch_ledger_spend_usd() — sum total_spend_microdollars (raw cost) → USD
# --------------------------------------------------------------------------- #


async def test_fetch_ledger_spend_usd_sums_only_period_rows_as_usd():
    """Raw spend is stored in total_spend_microdollars (microcents); 1 USD = 1e6.

    Lifetime + member:* rows are skipped, same as the token reconciliation.
    """
    items = [
        # Two owners' June rollups: 50_000_000 + 35_000_000 microcents = $85.
        {"owner_id": "user_a", "period": "2026-06", "total_spend_microdollars": 50_000_000},
        {"owner_id": "org_b", "period": "2026-06", "total_spend_microdollars": 35_000_000},
        # Lifetime — SKIPPED.
        {"owner_id": "user_a", "period": "lifetime", "total_spend_microdollars": 999_000_000},
        # Member — SKIPPED (subset of org_b's rollup).
        {
            "owner_id": "org_b",
            "period": "member:user_m:2026-06",
            "total_spend_microdollars": 20_000_000,
        },
        # Another month — SKIPPED (out of window).
        {"owner_id": "user_a", "period": "2026-05", "total_spend_microdollars": 7_000_000},
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        usd = await fetch_ledger_spend_usd(["2026-06"])

    assert usd == pytest.approx(85.0)


async def test_fetch_ledger_spend_usd_paginates_and_sums_periods():
    page1 = {
        "Items": [
            {"owner_id": "u1", "period": "2026-05", "total_spend_microdollars": 10_000_000},
        ],
        "LastEvaluatedKey": {"owner_id": "u1", "period": "2026-05"},
    }
    page2 = {
        "Items": [
            {"owner_id": "u2", "period": "2026-06", "total_spend_microdollars": 5_000_000},
            {"owner_id": "u3", "period": "2026-07", "total_spend_microdollars": 999_000_000},
        ],
    }
    fake_table = _FakeTable(pages=[page1, page2])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        usd = await fetch_ledger_spend_usd(["2026-05", "2026-06"])

    assert usd == pytest.approx(15.0)  # 2026-07 excluded
    assert fake_table.scan_calls == 2


# --------------------------------------------------------------------------- #
# DollarResult divergence math
# --------------------------------------------------------------------------- #


def test_dollar_result_exact_match_is_zero_divergence():
    r = DollarResult(aws_usd=2400.0, ledger_usd=2400.0)
    assert r.abs_diff == pytest.approx(0.0)
    assert r.divergence_pct == pytest.approx(0.0)


def test_dollar_result_historical_undercount_is_near_full_divergence():
    """The real case: AWS bill $2400, ledger recorded $85 → ~96% divergence."""
    r = DollarResult(aws_usd=2400.0, ledger_usd=85.0)
    assert r.abs_diff == pytest.approx(2315.0)
    assert r.divergence_pct == pytest.approx(96.458, abs=0.01)


def test_dollar_result_zero_aws_zero_ledger_is_zero():
    r = DollarResult(aws_usd=0.0, ledger_usd=0.0)
    assert r.divergence_pct == pytest.approx(0.0)


def test_dollar_result_zero_aws_nonzero_ledger_is_full_divergence():
    r = DollarResult(aws_usd=0.0, ledger_usd=10.0)
    assert r.divergence_pct == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# format_dollar_report() + threshold alerting
# --------------------------------------------------------------------------- #


def test_format_dollar_report_alerts_on_historical_divergence():
    text, alerted = format_dollar_report(DollarResult(aws_usd=2400.0, ledger_usd=85.0), threshold_pct=20.0)
    assert alerted is True
    assert "OVER THRESHOLD" in text
    assert "$" in text


def test_format_dollar_report_no_alert_when_within_threshold():
    text, alerted = format_dollar_report(DollarResult(aws_usd=100.0, ledger_usd=95.0), threshold_pct=20.0)
    assert alerted is False
    assert "OVER THRESHOLD" not in text


def test_format_dollar_report_threshold_is_strict_greater_than():
    at = format_dollar_report(DollarResult(aws_usd=100.0, ledger_usd=80.0), threshold_pct=20.0)
    assert at[1] is False
    over = format_dollar_report(DollarResult(aws_usd=100.0, ledger_usd=79.0), threshold_pct=20.0)
    assert over[1] is True


# --------------------------------------------------------------------------- #
# main() — dollar reconciliation feeds the exit code
# --------------------------------------------------------------------------- #


async def test_main_alerts_on_dollar_divergence_even_when_tokens_match(capsys):
    """Tokens reconcile but the $ side diverges (wrong rate / price change)
    → still ALERT + exit 1. This is the failure the token side can't catch."""
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    matched_tokens = {
        "InputTokenCount": 1000,
        "OutputTokenCount": 1000,
        "CacheReadInputTokenCount": 1000,
        "CacheWriteInputTokenCount": 1000,
    }
    matched_ledger_tokens = {
        "total_input_tokens": 1000,
        "total_output_tokens": 1000,
        "total_cache_read_tokens": 1000,
        "total_cache_write_tokens": 1000,
    }
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value=matched_tokens),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value=matched_ledger_tokens),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=2400.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=85.0),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "ALERT" in out
    # The dollar section should be visible in the report.
    assert "Cost Explorer" in out or "$" in out


async def test_main_clean_run_passes_both_token_and_dollar(capsys):
    args = argparse.Namespace(
        env="dev",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    matched = {
        "InputTokenCount": 1000,
        "OutputTokenCount": 1000,
        "CacheReadInputTokenCount": 1000,
        "CacheWriteInputTokenCount": 1000,
    }
    ledger = {
        "total_input_tokens": 1000,
        "total_output_tokens": 1000,
        "total_cache_read_tokens": 1000,
        "total_cache_write_tokens": 1000,
    }
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value=matched),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value=ledger),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=100.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=98.0),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out
    assert "ALERT" not in out


async def test_main_passes_bedrock_owner_ids_to_token_scan(capsys):
    """main() must resolve the Bedrock-only owner set and hand it to the
    token-side ledger scan so non-Bedrock volume can't inflate the token
    comparison. The dollar side stays un-filtered (non-Bedrock spend is 0)."""
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    token_scan = AsyncMock(
        return_value={
            "total_input_tokens": 1000,
            "total_output_tokens": 1000,
            "total_cache_read_tokens": 1000,
            "total_cache_write_tokens": 1000,
        }
    )
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(
                return_value={
                    "InputTokenCount": 1000,
                    "OutputTokenCount": 1000,
                    "CacheReadInputTokenCount": 1000,
                    "CacheWriteInputTokenCount": 1000,
                }
            ),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value={"user_bedrock", "org_bedrock"}),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=token_scan,
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=100.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=100.0),
        ),
    ):
        rc = await main(args)

    assert rc == 0
    token_scan.assert_awaited_once()
    _, kwargs = token_scan.call_args
    assert kwargs["bedrock_owner_ids"] == {"user_bedrock", "org_bedrock"}


async def test_main_no_false_token_alert_after_provider_migration(capsys):
    """End-to-end: a ledger inflated by a migrated non-Bedrock owner must NOT
    raise a token-divergence alert. main() resolves the Bedrock-only owner
    set, the real fetch_ledger_period_totals filters by it, and the
    Bedrock-only ledger total then matches the AWS/Bedrock CloudWatch
    totals."""
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    # AWS metered exactly what the ONE Bedrock owner used.
    aws_metrics = {
        "InputTokenCount": 100,
        "OutputTokenCount": 10,
        "CacheReadInputTokenCount": 1000,
        "CacheWriteInputTokenCount": 5,
    }
    # The ledger carries that Bedrock owner PLUS a migrated ChatGPT owner
    # whose spend-0 token volume would (without the filter) ~10000x inflate
    # the ledger token side and trip a false alert.
    ledger_items = [
        {
            "owner_id": "user_bedrock",
            "period": "2026-06",
            "total_input_tokens": 100,
            "total_output_tokens": 10,
            "total_cache_read_tokens": 1000,
            "total_cache_write_tokens": 5,
        },
        {
            "owner_id": "user_migrated_chatgpt",
            "period": "2026-06",
            "total_input_tokens": 1_000_000,
            "total_output_tokens": 1_000_000,
            "total_cache_read_tokens": 1_000_000,
            "total_cache_write_tokens": 1_000_000,
        },
    ]
    fake_table = _FakeTable(pages=[{"Items": ledger_items}])

    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value=aws_metrics),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value={"user_bedrock"}),
        ),
        # The real token scan runs against the fake table so the owner filter
        # is exercised end-to-end (NOT a canned return).
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=0.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        rc = await main(args)

    out = capsys.readouterr().out
    assert rc == 0  # no false alert
    assert "ALERT" not in out


async def test_main_token_match_but_dollar_clean_still_zero(capsys):
    """Sanity: when both sides are clean the dollar section doesn't false-alarm."""
    args = argparse.Namespace(
        env="prod",
        days=None,
        start=date(2026, 6, 1),
        end=date(2026, 7, 1),
        threshold_pct=20.0,
    )
    with (
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_metrics",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_owner_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_period_totals",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_bedrock_actual_cost_usd",
            new=AsyncMock(return_value=0.0),
        ),
        patch(
            "scripts.reconcile_bedrock_vs_ledger.fetch_ledger_spend_usd",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        rc = await main(args)

    assert rc == 0


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeTable:
    """Minimal DDB Table stub: yields preset scan pages in order."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.scan_calls = 0

    def scan(self, **kwargs):  # noqa: ARG002 - signature parity with boto3
        page = self._pages[self.scan_calls]
        self.scan_calls += 1
        return page


class _FakeBoto3:
    """Stand-in for the ``boto3`` module: ``client(name)`` returns a preset."""

    def __init__(self, clients: dict[str, object]):
        self._clients = clients

    def client(self, name, **kwargs):  # noqa: ARG002 - signature parity
        return self._clients[name]


async def _passthrough_run_in_thread(func, *args, **kwargs):
    """Call the function directly instead of offloading to a thread."""
    return func(*args, **kwargs)
