"""Daily Bedrock drift monitor — Cost Explorer $ vs the analytics counters.

A daily comparison of Cost Explorer's account-wide Bedrock cost against
the SUM of the ``usage-counters`` RAW recorded spend (via
``fetch_ledger_spend_usd``). The BILLED side now lives in Metronome (the
meter Lambda ingests every invocation directly) and has no cheap
account-wide total in our thin client, so this monitor watches the
analytics layer — the layer whose cache-token blindness caused the
original undercount incident — while AWS-bill-vs-Metronome stays a
console check. Reuses the reconciler's pure dollar machinery
(``DollarResult`` / ``format_dollar_report``) and CE plumbing
(``fetch_bedrock_actual_cost_usd`` / ``_period_strings``) — no duplicated
boto3.

On drift beyond ``--threshold-pct`` it emits a PER-ENVIRONMENT CloudWatch
metric (``LightbulbAI/Billing`` / ``BedrockLedgerDriftPct``) and exits non-zero.
It deliberately does NOT create an AWS Cost Anomaly Detection monitor or any
other account-global resource — such a resource collides across the per-env
dev/prod stacks (the per-env-resource lesson; every Bedrock cost alarm in
``apps/infra/lib/stacks/observability-stack.ts`` — e.g. the ``W50``
bedrock-spend-anomaly alarm — is scoped per env via ``isol8-${envName}-`` in
its name, never account-global). A per-env CloudWatch alarm can be attached to
this metric without that collision.

Run via::

    cd apps/backend
    uv run python -m scripts.drift_monitor --env prod --days 2

READ-ONLY against the ledger + Cost Explorer; the only write is the
CloudWatch metric emit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from scripts.reconcile_bedrock_vs_ledger import (
    DollarResult,
    fetch_bedrock_actual_cost_usd,
    fetch_ledger_spend_usd,
    format_dollar_report,
    _period_strings,
)


def _emit_drift_metric(env: str, divergence_pct: float) -> None:
    """Emit the per-env drift gauge to CloudWatch (no account-global resource)."""
    import boto3

    from core.config import settings

    cw = boto3.client("cloudwatch", region_name=getattr(settings, "AWS_REGION", "us-east-1"))
    cw.put_metric_data(
        Namespace="LightbulbAI/Billing",
        MetricData=[
            {
                "MetricName": "BedrockLedgerDriftPct",
                "Dimensions": [{"Name": "env", "Value": env}],
                "Value": float(divergence_pct),
                "Unit": "Percent",
            }
        ],
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Daily Bedrock drift monitor: Cost Explorer Bedrock $ vs the "
            "usage-counters RAW spend sum. Emits a per-env CloudWatch "
            "metric and exits non-zero on drift."
        ),
    )
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Window: last N days ending today UTC (default 2; CE lags ~24h).",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=20.0,
        help="Divergence percent that triggers an ALERT + non-zero exit (default 20).",
    )
    return parser


async def main(args: argparse.Namespace) -> int:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days)
    periods = _period_strings(start, end)

    from core.config import settings

    region = getattr(settings, "AWS_REGION", "us-east-1")

    aws_usd = await fetch_bedrock_actual_cost_usd(start=start, end=end, region=region)
    ledger_usd = await fetch_ledger_spend_usd(periods)
    result = DollarResult(aws_usd=aws_usd, ledger_usd=ledger_usd)

    report, alerted = format_dollar_report(result, threshold_pct=args.threshold_pct)
    print(f"[{args.env}] Bedrock drift monitor")
    print(f"  window:  {start.isoformat()} → {end.isoformat()} (end exclusive)")
    print(f"  periods: {', '.join(periods) if periods else '(none)'}")
    print(report)

    _emit_drift_metric(args.env, result.divergence_pct)

    if alerted:
        print(
            f"ALERT: Bedrock ledger drift {result.divergence_pct:.1f}% exceeds "
            f"{args.threshold_pct:.1f}% — the analytics counters may be "
            "missing or over-counting spend vs the AWS bill."
        )
        return 1
    print("OK: analytics counters within threshold of the AWS bill.")
    return 0


if __name__ == "__main__":
    parsed = build_arg_parser().parse_args()
    sys.exit(asyncio.run(main(parsed)))
