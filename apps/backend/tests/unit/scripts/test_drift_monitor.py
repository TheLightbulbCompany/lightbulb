"""Drift monitor: sum credit-transactions deduct rows (the real money
post-cutover) to USD, and the alert-threshold predicate.

Mirrors the WORKING fetch-test pattern in
tests/unit/scripts/test_reconcile_bedrock_vs_ledger.py: patch
core.dynamodb.get_table with a _FakeTable stub + core.dynamodb.run_in_thread
with a passthrough. Do NOT use moto + DYNAMODB_TABLE_PREFIX here — the prefix
is read once at import (core/dynamodb.py:12), so setenv is a no-op and
get_table("credit-transactions") resolves to the default isol8-dev- prefix,
which a moto table never created.
"""

import pytest


class _FakeTable:
    """Minimal DDB Table stub: yields preset scan pages in order."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.scan_calls = 0

    def scan(self, **kwargs):  # noqa: ARG002 - signature parity with boto3
        page = self._pages[self.scan_calls]
        self.scan_calls += 1
        return page


async def _passthrough_run_in_thread(func, *args, **kwargs):
    """Call the function directly instead of offloading to a thread."""
    return func(*args, **kwargs)


def _deduct_row(owner_id, tx_id, raw_microcents, created_at, *, row_type="deduct"):
    """A credit-transactions row as boto3 resource-level (un-marshalled) dict
    — i.e. plain Python values, matching what table.scan() returns when the
    DDB *resource* (not the low-level client) is used by get_table."""
    return {
        "user_id": owner_id,
        "tx_id": tx_id,
        "type": row_type,
        "raw_cost_microcents": raw_microcents,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_sums_only_deduct_rows_in_window():
    from unittest.mock import patch

    from scripts.reconcile_bedrock_vs_ledger import fetch_ledger_deduct_spend_usd

    items = [
        # In-window deducts (June 2026): 1_000_000 + 500_000 microcents = $1.50.
        _deduct_row("org_A", "deduct#r1", 1_000_000, "2026-06-03T10:00:00Z"),
        _deduct_row("org_B", "deduct#r2", 500_000, "2026-06-09T11:00:00Z"),
        # Out-of-window deduct (May) — excluded by the created_at prefix filter.
        _deduct_row("org_A", "deduct#r0", 9_000_000, "2026-05-31T23:00:00Z"),
        # Non-deduct row (a top-up) — excluded by the type filter.
        _deduct_row("org_A", "topup#t1", 9_000_000, "2026-06-05T10:00:00Z", row_type="top_up"),
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        total = await fetch_ledger_deduct_spend_usd(["2026-06"])

    assert total == pytest.approx(1.50)


@pytest.mark.asyncio
async def test_empty_table_is_zero():
    from unittest.mock import patch

    from scripts.reconcile_bedrock_vs_ledger import fetch_ledger_deduct_spend_usd

    fake_table = _FakeTable(pages=[{"Items": []}])
    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        assert await fetch_ledger_deduct_spend_usd(["2026-06"]) == 0.0


def test_drift_exceeds_threshold_predicate():
    from scripts.drift_monitor import drift_alerted

    # 30% drift on a $100 AWS bill vs $70 ledger.
    assert drift_alerted(aws_usd=100.0, ledger_usd=70.0, threshold_pct=20.0) is True


def test_drift_within_threshold_predicate():
    from scripts.drift_monitor import drift_alerted

    # 5% drift is under the 20% threshold.
    assert drift_alerted(aws_usd=100.0, ledger_usd=95.0, threshold_pct=20.0) is False


def test_drift_zero_aws_zero_ledger_not_alerted():
    from scripts.drift_monitor import drift_alerted

    assert drift_alerted(aws_usd=0.0, ledger_usd=0.0, threshold_pct=20.0) is False


def test_drift_zero_aws_nonzero_ledger_alerted():
    from scripts.drift_monitor import drift_alerted

    # AWS reported $0 but the ledger deducted — a real discrepancy.
    assert drift_alerted(aws_usd=0.0, ledger_usd=5.0, threshold_pct=20.0) is True
