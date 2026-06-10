"""Drift monitor: the alert-threshold predicate over the shared dollar
machinery. The monitor's spend source is the reconciler's
``fetch_ledger_spend_usd`` (usage-counters analytics RAW spend), which is
covered in tests/unit/scripts/test_reconcile_bedrock_vs_ledger.py —
Metronome owns the billed side and is reconciled via its console."""


def test_monitor_uses_analytics_spend_source():
    """The monitor must read the analytics counters (the surviving ledger);
    the credit-transactions deduct sum died with the DDB credit ledger."""
    import scripts.drift_monitor as drift_monitor

    assert drift_monitor.fetch_ledger_spend_usd is not None
    assert not hasattr(drift_monitor, "fetch_ledger_deduct_spend_usd")


# The drift monitor reuses the reconciler's pure dollar machinery: the
# threshold predicate IS the `alerted` flag returned by `format_dollar_report`
# (driven by `DollarResult.divergence_pct`). These tests assert against that
# shared helper directly — no monitor-local reimplementation.


def test_drift_exceeds_threshold_predicate():
    from scripts.reconcile_bedrock_vs_ledger import DollarResult, format_dollar_report

    # 30% drift on a $100 AWS bill vs $70 ledger.
    result = DollarResult(aws_usd=100.0, ledger_usd=70.0)
    _, alerted = format_dollar_report(result, threshold_pct=20.0)
    assert alerted is True


def test_drift_within_threshold_predicate():
    from scripts.reconcile_bedrock_vs_ledger import DollarResult, format_dollar_report

    # 5% drift is under the 20% threshold.
    result = DollarResult(aws_usd=100.0, ledger_usd=95.0)
    _, alerted = format_dollar_report(result, threshold_pct=20.0)
    assert alerted is False


def test_drift_zero_aws_zero_ledger_not_alerted():
    from scripts.reconcile_bedrock_vs_ledger import DollarResult, format_dollar_report

    result = DollarResult(aws_usd=0.0, ledger_usd=0.0)
    _, alerted = format_dollar_report(result, threshold_pct=20.0)
    assert alerted is False


def test_drift_zero_aws_nonzero_ledger_alerted():
    from scripts.reconcile_bedrock_vs_ledger import DollarResult, format_dollar_report

    # AWS reported $0 but the ledger deducted — a real discrepancy.
    result = DollarResult(aws_usd=0.0, ledger_usd=5.0)
    _, alerted = format_dollar_report(result, threshold_pct=20.0)
    assert alerted is True
