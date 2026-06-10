"""Read-only reconciliation: AWS Bedrock vs the usage-counters ledger.

A safety net for the cost-metering pipeline. Isol8's ``usage-counters``
ledger once captured ~3% of real Bedrock spend (cache tokens recorded as
0) and the ~30x undercount went unnoticed for weeks because nothing ever
compared the ledger to what AWS actually metered. This script is that
missing comparison. It runs two independent reconciliations:

1. **Tokens** — the four ``AWS/Bedrock`` CloudWatch token metrics (Sum)
   vs the SUM of the matching ``usage-counters`` token columns. Catches a
   dropped token class (the original incident).

2. **Dollars** — AWS Cost Explorer's actual unblended Bedrock cost vs the
   SUM of the ledger's recorded RAW spend. Catches a *wrong rate* or an
   AWS price change that the token side cannot see: tokens can reconcile
   perfectly while the dollar conversion is stale. Cost Explorer is the
   only source that reflects the marketplace-billed Claude 4.x models
   ("… (Amazon Bedrock Edition)") — the Price List and per-token APIs do
   not cover the actual invoice (incl. any negotiated discount).

A divergence beyond ``--threshold-pct`` in *either* reconciliation raises
an ALERT and exits non-zero.

Run via::

    cd apps/backend
    uv run python -m scripts.reconcile_bedrock_vs_ledger \\
        --env prod --days 30
    uv run python -m scripts.reconcile_bedrock_vs_ledger \\
        --env prod --start 2026-05-01 --end 2026-06-01 --threshold-pct 15

READ-ONLY: pulls CloudWatch statistics + Cost Explorer + scans the
ledger; writes nothing. The operator runs it with real AWS credentials;
nothing here makes live calls during development or tests.

Scope notes
-----------

* **Account-wide, not per-owner.** Bedrock's CloudWatch metrics AND Cost
  Explorer's Bedrock cost are emitted for the whole AWS account — Bedrock
  does not tag invocations with an Isol8 owner unless
  model-invocation-logging with custom dimensions is enabled (it is NOT).
  So the only meaningful comparison is account-wide AWS totals vs the sum
  of *all* owners' ledger rows. Per-owner attribution would require
  Bedrock invocation logging — a future enhancement, deliberately not
  built here.

* **Cost Explorer is ~24h delayed.** Bedrock cost lands in Cost Explorer
  roughly a day after the invocation, so the dollar reconciliation is
  only meaningful for windows that ended at least a day ago. A fresh
  partial day will undercount the AWS side.

* **Period rows only.** ``usage-counters`` has three SK shapes (see
  ``core/services/usage_service.record_usage``):
    - ``YYYY-MM``                 — the per-owner period rollup (summed)
    - ``lifetime``                — a running total (SKIPPED: would double-count)
    - ``member:{user_id}:{period}`` — per-member breakdown (SKIPPED: a
      strict subset of the period rollup; including it double-counts)
  We sum only the rows whose SK exactly equals one of the ``YYYY-MM``
  period strings the window spans.

* **Token side is Bedrock-owner-scoped.** Since ``record_usage`` records
  non-Bedrock model VOLUME (ChatGPT OAuth / OpenAI / BYO) into the same
  ``YYYY-MM`` rows with spend 0, and those rows carry no provider
  dimension, the token reconciliation filters to owners whose
  ``billing-accounts`` ``provider_choice == "bedrock_claude"`` (resolved
  by ``fetch_bedrock_owner_ids``). Otherwise the ledger token columns get
  inflated by non-Bedrock traffic while the account-wide ``AWS/Bedrock``
  CloudWatch totals stay correct → false token-divergence alerts after a
  provider migration. The dollar side needs no such filter: non-Bedrock
  sessions already record spend 0.

* **RAW spend, not the customer price.** The ledger's
  ``total_spend_microdollars`` column (a misnomer — it stores microcents;
  1 USD = 1e6 microcents) holds Isol8's RAW, un-marked-up cost: in
  ``record_usage`` it is taken verbatim from OpenClaw's
  ``estimatedCostUsd`` (or recomputed pre-markup from the Bedrock pricing
  table). The 1.4× customer markup is applied only by Metronome's rate
  card. The AWS bill is itself un-marked-up, so the dollar reconciliation
  compares AWS $ against this RAW ledger spend — NOT the marked-up
  customer price.

* **The "ledger" here is the analytics counters, not the biller.** The
  biller is Metronome (the meter Lambda ingests every invocation
  directly); its per-customer balances/invoices have no cheap account-wide
  total in our thin client, so the AWS-bill-vs-BILLED comparison is a
  Metronome-console check for now. This script reconciles AWS truth
  against the ``usage-counters`` analytics writes — the layer whose
  cache-token blindness caused the original incident.

* **Month granularity.** The ledger buckets by calendar month, so the
  comparison is only apples-to-apples when the window aligns to whole
  months. For a partial month the ledger total covers the *entire*
  month while CloudWatch / Cost Explorer cover only the requested days —
  the script prints a warning when ``--start``/``--end`` are not month
  boundaries.
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

# The ledger column holding RAW (un-marked-up) spend. Misnamed for legacy
# reasons — values are microcents, not microdollars (see usage_repo).
_LEDGER_SPEND_COLUMN = "total_spend_microdollars"

# 1 USD = 100 cents = 1_000_000 microcents (1 cent = 10_000 microcents).
_MICROCENTS_PER_USD = 1_000_000


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


@dataclass
class DollarResult:
    """Dollar reconciliation outcome: AWS bill vs the ledger's RAW spend.

    Both figures are USD. ``aws_usd`` is Cost Explorer's account-wide
    unblended Bedrock cost; ``ledger_usd`` is the sum of the ledger's RAW
    (un-marked-up) recorded spend across every owner's period rows.
    """

    aws_usd: float
    ledger_usd: float

    @property
    def abs_diff(self) -> float:
        return abs(self.aws_usd - self.ledger_usd)

    @property
    def divergence_pct(self) -> float:
        """Percent divergence of the ledger from the AWS bill.

        AWS is the denominator because the bill is the real cost. When the
        bill is zero, divergence is 0 if the ledger also reads zero, else
        100 (the ledger recorded spend the bill never charged).
        """
        if self.aws_usd == 0:
            return 0.0 if self.ledger_usd == 0 else 100.0
        return self.abs_diff / self.aws_usd * 100.0


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


async def fetch_bedrock_owner_ids() -> set[str]:
    """Scan ``billing-accounts`` → the set of owner_ids on the Bedrock path.

    An owner's LLM provider lives on the per-owner ``billing-accounts`` row
    as ``provider_choice`` (set by ``billing_repo.set_provider_choice``); the
    ``usage-counters`` rows carry no provider dimension. This builds the
    owner→Bedrock filter the token reconciliation needs (see
    :func:`fetch_ledger_period_totals`). Read-only paginated scan.

    Rows with no ``provider_choice`` (phantom / pre-onboarding rows) are
    excluded — they have no Bedrock usage to reconcile.
    """
    from core.dynamodb import get_table, run_in_thread

    owners: set[str] = set()
    table = get_table("billing-accounts")
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            if item.get("provider_choice") == "bedrock_claude":
                owner_id = item.get("owner_id")
                if owner_id:
                    owners.add(owner_id)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return owners


async def fetch_ledger_period_totals(
    periods: list[str],
    *,
    bedrock_owner_ids: set[str] | None = None,
) -> dict[str, int]:
    """Scan ``usage-counters`` and sum the PERIOD rollup rows for ``periods``.

    Returns ``{column: total}`` for the four token columns. Read-only —
    a paginated table scan, no writes. Only rows whose SK exactly equals
    one of ``periods`` (the ``YYYY-MM`` rollup shape) are counted;
    ``lifetime`` and ``member:*`` rows are skipped to avoid double-count.

    ``bedrock_owner_ids`` (when provided) restricts the sum to owners on the
    Bedrock provider path. This is REQUIRED to compare apples-to-apples
    against the account-wide ``AWS/Bedrock`` CloudWatch token metrics: after
    D6, ``record_usage`` writes non-Bedrock model VOLUME (ChatGPT OAuth /
    OpenAI / BYO) into these same ``YYYY-MM`` rows with spend 0, and those
    rows have no provider dimension. Without the filter the ledger token side
    is inflated by non-Bedrock traffic while CloudWatch stays correct →
    false token-divergence alerts after a provider migration. ``None`` keeps
    the un-filtered behavior (every period row counted); ``main`` always
    resolves and passes the Bedrock-only set.
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
            if bedrock_owner_ids is not None and item.get("owner_id") not in bedrock_owner_ids:
                # Non-Bedrock owner: its token volume (spend 0) would inflate
                # the comparison against the AWS/Bedrock CloudWatch totals.
                continue
            for c in columns:
                totals[c] += int(item.get(c, 0))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return totals


async def fetch_ledger_spend_usd(periods: list[str]) -> float:
    """Sum the ledger's RAW spend (``total_spend_microdollars``) → USD.

    Scans ``usage-counters`` and sums only the ``YYYY-MM`` period rollup
    rows for ``periods`` (skipping ``lifetime`` and ``member:*`` to avoid
    double-count, same as the token side). The column stores microcents
    (the name is a legacy misnomer); ``microcents / 1e6 = USD``.

    This is Isol8's RAW, un-marked-up cost — comparable to the AWS bill.
    See the module docstring's "RAW spend" note.
    """
    from core.dynamodb import get_table, run_in_thread

    wanted = set(periods)
    total_microcents = 0

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
            total_microcents += int(item.get(_LEDGER_SPEND_COLUMN, 0))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return total_microcents / _MICROCENTS_PER_USD


# --------------------------------------------------------------------------- #
# AWS Cost Explorer — actual Bedrock $ (the only marketplace-billed truth).
# --------------------------------------------------------------------------- #


def _is_bedrock_service(service_name: str) -> bool:
    """True when a Cost Explorer SERVICE value is a Bedrock cost line.

    Matches both the base ``Amazon Bedrock`` service and the
    per-model marketplace-billed lines ``… (Amazon Bedrock Edition)``.
    Matched by substring rather than an exact list because the model
    version strings (e.g. "Claude Opus 4.6") change as models ship.
    Case- and whitespace-tolerant — Cost Explorer occasionally pads keys.
    """
    normalized = service_name.strip().lower()
    return "bedrock edition" in normalized or normalized == "amazon bedrock"


def sum_ce_bedrock_cost_usd(response: dict[str, Any]) -> float:
    """Sum the Bedrock UnblendedCost across a SERVICE-grouped CE response.

    Pure function (no boto3) so the filter + fold is testable. Walks every
    ``ResultsByTime[].Groups[]`` entry, keeps groups whose SERVICE key is a
    Bedrock line (``_is_bedrock_service``), and sums their
    ``UnblendedCost.Amount`` (a USD string). Non-Bedrock services (S3,
    EC2, …) are excluded.
    """
    total = 0.0
    for bucket in response.get("ResultsByTime", []):
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            if not keys or not _is_bedrock_service(keys[0]):
                continue
            amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0")
            total += float(amount)
    return total


async def fetch_bedrock_actual_cost_usd(*, start: date, end: date, region: str) -> float:
    """Account-wide actual Bedrock $ from Cost Explorer over ``[start, end)``.

    Uses ``ce.get_cost_and_usage`` grouped by SERVICE on the UnblendedCost
    metric, then sums the Bedrock-line groups. ``end`` is exclusive,
    matching Cost Explorer's TimePeriod semantics. MONTHLY granularity is
    used because the ledger buckets by calendar month; the parser folds
    however many time buckets CE returns.

    Cost Explorer's Bedrock data is account-wide and ~24h delayed — see
    the module docstring's scope notes.
    """
    import boto3

    # Cost Explorer is a global service; the endpoint lives in us-east-1.
    # We still pass the configured region for parity / explicitness.
    client = boto3.client("ce", region_name=region)
    resp = await asyncio.to_thread(
        client.get_cost_and_usage,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    return sum_ce_bedrock_cost_usd(resp)


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


def format_dollar_report(result: DollarResult, *, threshold_pct: float) -> tuple[str, bool]:
    """Render the dollar comparison. Returns ``(text, alerted)``.

    ``alerted`` is True when the divergence exceeds ``threshold_pct``.
    Compares Cost Explorer's account-wide Bedrock $ against the ledger's
    RAW (un-marked-up) recorded spend — see the module docstring.
    """
    alerted = result.divergence_pct > threshold_pct
    flag = "  <== OVER THRESHOLD" if alerted else ""
    lines = [
        f"{'source':<28} {'amount (USD)':>16}",
        "-" * 45,
        f"{'AWS Bedrock (Cost Explorer)':<28} {result.aws_usd:>16,.2f}",
        f"{'ledger RAW spend':<28} {result.ledger_usd:>16,.2f}",
        "-" * 45,
        f"{'abs diff':<28} ${result.abs_diff:>15,.2f}",
        f"{'divergence':<28} {result.divergence_pct:>15.1f}%{flag}",
    ]
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
            "Reconcile account-wide AWS Bedrock usage against the ledger on "
            "two axes: CloudWatch token counts vs ledger tokens, and Cost "
            "Explorer Bedrock $ vs ledger RAW spend $. Read-only."
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
        help="Divergence percent (per token class OR total $) that triggers an ALERT + non-zero exit (default: 20).",
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

    # --- Token reconciliation: CloudWatch counts vs ledger token columns ---
    # Resolve the Bedrock-only owner set first: the ledger's YYYY-MM rows hold
    # non-Bedrock token VOLUME (spend 0) since D6, with no provider dimension,
    # so the token side must be scoped to Bedrock owners to match the
    # account-wide AWS/Bedrock CloudWatch totals (non-Bedrock owners
    # contribute nothing to CloudWatch).
    bedrock_owner_ids = await fetch_bedrock_owner_ids()
    aws_metrics = await fetch_bedrock_metrics(start=start_dt, end=end_dt, region=region)
    ledger_totals = await fetch_ledger_period_totals(periods, bedrock_owner_ids=bedrock_owner_ids)
    print(f"  bedrock owners: {len(bedrock_owner_ids)} (token side scoped to these)")

    results = reconcile(aws_metrics, ledger_totals)
    token_report, token_alerted = format_report(results, threshold_pct=args.threshold_pct)
    print("TOKEN reconciliation (CloudWatch counts vs ledger):")
    print(token_report)
    print()

    # --- Dollar reconciliation: Cost Explorer $ vs ledger RAW spend $ ---
    # No Bedrock-owner filter needed here: record_usage records non-Bedrock
    # sessions with spend 0 (only Bedrock is Isol8-billed), so non-Bedrock
    # owners contribute $0 to the ledger spend column already.
    aws_usd = await fetch_bedrock_actual_cost_usd(start=start, end=end, region=region)
    ledger_usd = await fetch_ledger_spend_usd(periods)

    dollar_result = DollarResult(aws_usd=aws_usd, ledger_usd=ledger_usd)
    dollar_report, dollar_alerted = format_dollar_report(dollar_result, threshold_pct=args.threshold_pct)
    print("DOLLAR reconciliation (Cost Explorer Bedrock $ vs ledger RAW spend $):")
    print(dollar_report)
    print()

    alerted = token_alerted or dollar_alerted
    if alerted:
        if token_alerted:
            print(
                f"ALERT: at least one token class diverges beyond {args.threshold_pct:.1f}% — "
                "the ledger may be under/over-recording Bedrock usage."
            )
        if dollar_alerted:
            print(
                f"ALERT: ledger spend diverges from the AWS bill beyond "
                f"{args.threshold_pct:.1f}% — the dollar rate may be wrong or "
                "AWS pricing may have changed."
            )
        return 1
    print("OK: token classes and dollar spend within threshold.")
    return 0


if __name__ == "__main__":
    parsed = build_arg_parser().parse_args()
    sys.exit(asyncio.run(main(parsed)))
