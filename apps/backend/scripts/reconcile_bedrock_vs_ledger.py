"""Read-only reconciliation: AWS Bedrock metered tokens vs the usage-counters ledger.

A safety net for the cost-metering pipeline. Isol8's ``usage-counters``
ledger once captured ~3% of real Bedrock spend (cache tokens recorded as
0) and the ~30x undercount went unnoticed for weeks because nothing ever
compared the ledger to what AWS actually metered. This script is that
missing comparison.

It pulls the four ``AWS/Bedrock`` CloudWatch token metrics (Sum) for a
window and compares them against the SUM of the matching
``usage-counters`` PERIOD rows across every owner. A per-token-class
divergence beyond ``--threshold-pct`` raises an ALERT and exits non-zero.

Run via::

    cd apps/backend
    uv run python -m scripts.reconcile_bedrock_vs_ledger \\
        --env prod --days 30
    uv run python -m scripts.reconcile_bedrock_vs_ledger \\
        --env prod --start 2026-05-01 --end 2026-06-01 --threshold-pct 15

READ-ONLY: pulls CloudWatch statistics + scans the ledger; writes
nothing. The operator runs it with real AWS credentials; nothing here
makes live calls during development or tests.

Scope notes
-----------

* **Account-wide, not per-owner.** Bedrock's CloudWatch metrics are
  emitted for the whole AWS account — Bedrock does not tag invocations
  with an Isol8 owner unless model-invocation-logging with custom
  dimensions is enabled (it is NOT). So the only meaningful comparison
  is account-wide AWS totals vs the sum of *all* owners' ledger rows.
  Per-owner attribution would require Bedrock invocation logging — a
  future enhancement, deliberately not built here.

* **Period rows only.** ``usage-counters`` has three SK shapes (see
  ``core/services/usage_service.record_usage``):
    - ``YYYY-MM``                 — the per-owner period rollup (summed)
    - ``lifetime``                — a running total (SKIPPED: would double-count)
    - ``member:{user_id}:{period}`` — per-member breakdown (SKIPPED: a
      strict subset of the period rollup; including it double-counts)
  We sum only the rows whose SK exactly equals one of the ``YYYY-MM``
  period strings the window spans.

* **Month granularity.** The ledger buckets by calendar month, so the
  comparison is only apples-to-apples when the window aligns to whole
  months. For a partial month the ledger total covers the *entire*
  month while CloudWatch covers only the requested days — the script
  prints a warning when ``--start``/``--end`` are not month boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

# AWS/Bedrock CloudWatch metric name → the usage-counters column it maps to.
# All four are emitted account-wide with the ``Sum`` statistic.
_METRIC_TO_COLUMN: dict[str, str] = {
    "InputTokenCount": "total_input_tokens",
    "OutputTokenCount": "total_output_tokens",
    "CacheReadInputTokenCount": "total_cache_read_tokens",
    "CacheWriteInputTokenCount": "total_cache_write_tokens",
}

# Display label per token class, ordered for the report table.
_CLASS_ORDER: list[tuple[str, str, str]] = [
    ("input", "InputTokenCount", "total_input_tokens"),
    ("output", "OutputTokenCount", "total_output_tokens"),
    ("cache_read", "CacheReadInputTokenCount", "total_cache_read_tokens"),
    ("cache_write", "CacheWriteInputTokenCount", "total_cache_write_tokens"),
]

_BEDROCK_NAMESPACE = "AWS/Bedrock"


@dataclass
class ClassResult:
    """Reconciliation outcome for one token class."""

    name: str
    aws_total: int
    ledger_total: int

    @property
    def abs_diff(self) -> int:
        return abs(self.aws_total - self.ledger_total)

    @property
    def divergence_pct(self) -> float:
        """Percent divergence of the ledger from AWS (the source of truth).

        Uses AWS as the denominator because AWS is the real bill. When AWS
        reports zero for a class, divergence is 0 if the ledger also reads
        zero, else 100 (the ledger invented tokens the bill never saw).
        """
        if self.aws_total == 0:
            return 0.0 if self.ledger_total == 0 else 100.0
        return self.abs_diff / self.aws_total * 100.0


def _period_strings(start: date, end: date) -> list[str]:
    """Return the ``YYYY-MM`` strings the half-open ``[start, end)`` window spans.

    The ledger buckets by calendar month, so any month the window touches
    must be summed. ``end`` is exclusive (it is the day after the last day
    of interest), matching how CloudWatch windows are expressed.
    """
    if end <= start:
        return []
    periods: list[str] = []
    # Walk month-by-month from start's month through the month containing
    # the last in-window day (end - 1).
    last_day = end - timedelta(days=1)
    y, m = start.year, start.month
    while (y, m) <= (last_day.year, last_day.month):
        periods.append(f"{y:04d}-{m:02d}")
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return periods


def _is_month_aligned(start: date, end: date) -> bool:
    """True when the window is whole calendar months (1st-to-1st)."""
    return start.day == 1 and end.day == 1


# --------------------------------------------------------------------------- #
# AWS / DDB access — kept behind small functions so tests can mock them.
# --------------------------------------------------------------------------- #


async def fetch_bedrock_metrics(*, start: datetime, end: datetime, region: str) -> dict[str, int]:
    """Sum each AWS/Bedrock token metric over ``[start, end)``.

    Returns ``{metric_name: total_tokens}`` for the four token metrics.
    Uses a single period that spans the whole window so CloudWatch returns
    one datapoint per metric; we still sum defensively in case it splits.
    """
    import boto3

    client = boto3.client("cloudwatch", region_name=region)
    # CloudWatch caps Period at 1 day (86400s) for windows beyond ~63 days
    # of high resolution; daily buckets are always safe and we sum them.
    window_seconds = max(int((end - start).total_seconds()), 60)
    period = min(window_seconds, 86400)

    totals: dict[str, int] = {}
    for metric_name in _METRIC_TO_COLUMN:
        resp = await asyncio.to_thread(
            client.get_metric_statistics,
            Namespace=_BEDROCK_NAMESPACE,
            MetricName=metric_name,
            StartTime=start,
            EndTime=end,
            Period=period,
            Statistics=["Sum"],
        )
        totals[metric_name] = sum_metric_datapoints(resp)
    return totals


def sum_metric_datapoints(response: dict[str, Any]) -> int:
    """Sum the ``Sum`` field across a get_metric_statistics response.

    Pulled out as a pure function so the datapoint-folding is testable
    without a boto3 client.
    """
    return int(sum(dp.get("Sum", 0) for dp in response.get("Datapoints", [])))


async def fetch_ledger_period_totals(periods: list[str]) -> dict[str, int]:
    """Scan ``usage-counters`` and sum the PERIOD rollup rows for ``periods``.

    Returns ``{column: total}`` for the four token columns. Read-only —
    a paginated table scan, no writes. Only rows whose SK exactly equals
    one of ``periods`` (the ``YYYY-MM`` rollup shape) are counted;
    ``lifetime`` and ``member:*`` rows are skipped to avoid double-count.
    """
    from core.dynamodb import get_table, run_in_thread

    wanted = set(periods)
    columns = (
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_cache_write_tokens",
    )
    totals = {c: 0 for c in columns}

    table = get_table("usage-counters")
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            if item.get("period") not in wanted:
                continue
            for c in columns:
                totals[c] += int(item.get(c, 0))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return totals


# --------------------------------------------------------------------------- #
# Reconciliation + reporting (pure)
# --------------------------------------------------------------------------- #


def reconcile(
    aws_metrics: dict[str, int],
    ledger_totals: dict[str, int],
) -> list[ClassResult]:
    """Pair AWS metric totals with ledger column totals, per token class."""
    results: list[ClassResult] = []
    for name, metric_name, column in _CLASS_ORDER:
        results.append(
            ClassResult(
                name=name,
                aws_total=int(aws_metrics.get(metric_name, 0)),
                ledger_total=int(ledger_totals.get(column, 0)),
            )
        )
    return results


def format_report(results: list[ClassResult], *, threshold_pct: float) -> tuple[str, bool]:
    """Render the comparison table. Returns ``(text, alerted)``.

    ``alerted`` is True when any class diverges beyond ``threshold_pct``.
    An overall row aggregates all four classes.
    """
    lines: list[str] = []
    header = f"{'class':<12} {'aws':>16} {'ledger':>16} {'abs_diff':>16} {'divergence%':>12}"
    lines.append(header)
    lines.append("-" * len(header))

    alerted = False
    aws_sum = ledger_sum = 0
    for r in results:
        aws_sum += r.aws_total
        ledger_sum += r.ledger_total
        flag = ""
        if r.divergence_pct > threshold_pct:
            alerted = True
            flag = "  <== OVER THRESHOLD"
        lines.append(
            f"{r.name:<12} {r.aws_total:>16,} {r.ledger_total:>16,} {r.abs_diff:>16,} {r.divergence_pct:>11.1f}%{flag}"
        )

    overall = ClassResult(name="OVERALL", aws_total=aws_sum, ledger_total=ledger_sum)
    if overall.divergence_pct > threshold_pct:
        alerted = True
    lines.append("-" * len(header))
    lines.append(
        f"{overall.name:<12} {overall.aws_total:>16,} {overall.ledger_total:>16,} "
        f"{overall.abs_diff:>16,} {overall.divergence_pct:>11.1f}%"
    )
    return "\n".join(lines), alerted


# --------------------------------------------------------------------------- #
# Arg parsing + entrypoint
# --------------------------------------------------------------------------- #


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:  # pragma: no cover - argparse surfaces the message
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {value!r}") from e


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile account-wide AWS/Bedrock metered tokens against the "
            "sum of usage-counters period rows. Read-only."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help="Environment label (informational; DDB targeting comes from DYNAMODB_TABLE_PREFIX).",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--days",
        type=int,
        help="Window size: the last N days ending today (UTC). Mutually exclusive with --start/--end.",
    )
    parser.add_argument(
        "--start",
        type=_parse_iso_date,
        help="Window start (ISO date, inclusive). Use with --end.",
    )
    parser.add_argument(
        "--end",
        type=_parse_iso_date,
        help="Window end (ISO date, exclusive). Use with --start.",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=20.0,
        help="Per-class divergence percent that triggers an ALERT + non-zero exit (default: 20).",
    )
    return parser


def resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    """Resolve ``--days`` or ``--start/--end`` to a half-open ``[start, end)`` date window.

    Raises ``SystemExit`` (via argparse error) on an invalid combination.
    """
    if args.days is not None:
        if args.start or args.end:
            raise ValueError("--days is mutually exclusive with --start/--end")
        if args.days <= 0:
            raise ValueError("--days must be positive")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=args.days)
        return start, end
    if args.start and args.end:
        if args.end <= args.start:
            raise ValueError("--end must be after --start")
        return args.start, args.end
    raise ValueError("provide either --days or both --start and --end")


async def main(args: argparse.Namespace) -> int:
    from core.config import settings

    try:
        start, end = resolve_window(args)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2

    region = getattr(settings, "AWS_REGION", "us-east-1")
    periods = _period_strings(start, end)

    print(f"[{args.env}] Reconciling AWS/Bedrock vs usage-counters ledger")
    print(f"  window:    {start.isoformat()} → {end.isoformat()} (end exclusive)")
    print(f"  region:    {region}")
    print(f"  periods:   {', '.join(periods) if periods else '(none)'}")
    print(f"  threshold: {args.threshold_pct:.1f}%")
    if not _is_month_aligned(start, end):
        print(
            "  WARNING: window is not month-aligned. The ledger buckets by "
            "calendar month, so its totals cover whole months while CloudWatch "
            "covers only the requested days — expect inflated ledger divergence."
        )
    print()

    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

    aws_metrics = await fetch_bedrock_metrics(start=start_dt, end=end_dt, region=region)
    ledger_totals = await fetch_ledger_period_totals(periods)

    results = reconcile(aws_metrics, ledger_totals)
    report, alerted = format_report(results, threshold_pct=args.threshold_pct)
    print(report)
    print()

    if alerted:
        print(
            f"ALERT: at least one token class diverges beyond {args.threshold_pct:.1f}% — "
            "the ledger may be under/over-recording Bedrock usage."
        )
        return 1
    print("OK: all token classes within threshold.")
    return 0


if __name__ == "__main__":
    parsed = build_arg_parser().parse_args()
    sys.exit(asyncio.run(main(parsed)))
