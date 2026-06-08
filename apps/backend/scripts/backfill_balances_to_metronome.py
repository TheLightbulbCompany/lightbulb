"""One-time DDB-balance -> Metronome-credit migration (Phase 3 cutover).

Reads every owner's balance from the legacy ``credits`` DDB table and grants an
equal Metronome prepaid credit (NON-payment-gated — the owner already paid for
that balance in the old ledger; re-gating on Stripe would double-charge). Run
ONCE per environment BEFORE the credits/credit-transactions tables are dropped
(spec §3.5: "Backfill existing balances into Metronome prepaid credits;
idempotency-keyed per owner; do not grandfather two money systems").

Idempotent: each granted owner gets a ``backfill_done_at`` marker on the
fast-gate row; re-running skips marked owners, so a partial run resumes without
double-granting. The granted commit also carries a deterministic
``metadata.backfill_key`` so the grant is traceable in Metronome.

Run::

    cd apps/backend
    uv run python -m scripts.backfill_balances_to_metronome --env prod
    uv run python -m scripts.backfill_balances_to_metronome --env prod --apply

Defaults to dry-run; pass ``--apply`` to actually grant. The credits table name
defaults to ``isol8-{env}-credits`` (override with ``--credits-table``).

DO NOT run this after the credits table is dropped — the source data is gone.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

import boto3

from core.billing.metronome_funding import fund_backfill_credit
from core.billing.metronome_provisioning import ensure_metronome_customer
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger("backfill_balances_to_metronome")


def _fast_gate_table():
    # Env-first (matches metronome_funding/_fast_gate_table_name +
    # metronome_provisioning): the pydantic `settings` singleton freezes env at
    # import time, so a test/runtime `METRONOME_FAST_GATE_TABLE` env override
    # would be invisible if we read `settings.` only. Reading os.environ first
    # makes the override (and the unit test's monkeypatch.setenv) take effect.
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — backfill is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(name)


def _credits_table(name: str):
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(name)


def _already_backfilled(owner_id: str) -> bool:
    resp = _fast_gate_table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
    return bool((resp.get("Item") or {}).get("backfill_done_at"))


def _mark_backfilled(owner_id: str) -> None:
    _fast_gate_table().update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="SET backfill_done_at = :t",
        ExpressionAttributeValues={":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    )


async def backfill_owner(owner_id: str, balance_microcents: int, *, apply: bool, credits_table: str) -> bool:
    """Grant one owner's balance as a Metronome credit. Returns True if a grant
    was made (or would be, in dry-run), False if skipped (already done / no
    Stripe customer)."""
    if balance_microcents <= 0:
        return False
    if _already_backfilled(owner_id):
        logger.info("SKIP %s — already backfilled", owner_id)
        return False

    if not apply:
        logger.info("DRY-RUN would grant %s: %d microcents", owner_id, balance_microcents)
        return True

    account = await billing_repo.get_by_owner_id(owner_id)
    stripe_customer_id = (account or {}).get("stripe_customer_id")
    if not stripe_customer_id:
        logger.warning("SKIP %s — no stripe_customer_id; provision manually", owner_id)
        return False

    await ensure_metronome_customer(owner_id=owner_id, stripe_customer_id=stripe_customer_id)
    await fund_backfill_credit(
        owner_id=owner_id,
        amount_microcents=balance_microcents,
        contract_id=settings.METRONOME_CREDITS_CONTRACT_ID,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
    )
    _mark_backfilled(owner_id)
    logger.info("GRANTED %s: %d microcents", owner_id, balance_microcents)
    return True


async def run_backfill(*, env: str, apply: bool, credits_table: str) -> dict:
    table = _credits_table(credits_table)
    candidates = 0
    granted = 0
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("user_id")
            balance = int(item.get("balance_microcents", 0))
            if balance <= 0:
                continue
            candidates += 1
            if await backfill_owner(owner_id, balance, apply=apply, credits_table=credits_table):
                if apply:
                    granted += 1
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    summary = {"candidates": candidates, "granted": granted, "apply": apply}
    logger.info("backfill summary: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Backfill DDB credit balances into Metronome prepaid credits.")
    parser.add_argument("--env", required=True, help="Environment name (dev/prod) — used to default the table name.")
    parser.add_argument("--apply", action="store_true", help="Actually grant credits (default: dry-run).")
    parser.add_argument("--credits-table", default=None, help="Override the credits table name.")
    args = parser.parse_args()
    credits_table = args.credits_table or f"isol8-{args.env}-credits"
    asyncio.run(run_backfill(env=args.env, apply=args.apply, credits_table=credits_table))


if __name__ == "__main__":
    main()
