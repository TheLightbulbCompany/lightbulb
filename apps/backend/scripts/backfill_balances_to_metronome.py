"""One-time DDB-balance -> Metronome-credit migration (Phase 3 cutover).

Reads every owner's balance from the legacy ``credits`` DDB table and grants an
equal Metronome prepaid credit (NON-payment-gated — the owner already paid for
that balance in the old ledger; re-gating on Stripe would double-charge). Run
ONCE per environment BEFORE the credits/credit-transactions tables are dropped
(spec §3.5: "Backfill existing balances into Metronome prepaid credits;
idempotency-keyed per owner; do not grandfather two money systems").

Idempotent: each owner's ``backfill_done_at`` marker on the fast-gate row is
claimed with a conditional ``attribute_not_exists`` write BEFORE the grant, then
released if the grant fails — so the marker is the at-most-once gate and the
grant is the retryable step. This matters because Metronome's
``/contracts/edit add_commits`` does NOT dedup (no ``Idempotency-Key`` header,
and metadata is traceability-only, not a server-side no-op key), so a
grant-then-mark order would re-grant on a crash between the two. The granted
commit still carries a deterministic ``metadata.backfill_key`` for audit/trace.

Negative balances (overdrafts left by the legacy ledger's deduct path) are
intentionally NOT carried over — there is no negative prepaid credit in
Metronome, so the (small) debt is forgiven at cutover. The dry-run/apply summary
reports ``negative_skipped`` + ``negative_microcents`` so the operator can see
the forgiven amount.

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
from botocore.exceptions import ClientError

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


def _claim_backfill_marker(owner_id: str) -> bool:
    """Atomically claim the backfill marker BEFORE granting. Returns True if the
    claim succeeded (this run owns the grant), False if another run/prior run
    already claimed it.

    Marker-before-grant + a conditional ``attribute_not_exists`` claim is the
    sole double-grant guard: Metronome's ``/contracts/edit add_commits`` does
    NOT dedup on commit metadata (no ``Idempotency-Key`` header on the POST), so
    if we granted first and stamped the marker second a crash in between would
    re-grant on the next run. By claiming first, the conditional write is the
    at-most-once gate; the grant is the retryable step (we roll the marker back
    if it fails, so a re-run retries instead of skipping a never-granted owner).
    """
    try:
        _fast_gate_table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="SET backfill_done_at = :t",
            ConditionExpression="attribute_not_exists(backfill_done_at)",
            ExpressionAttributeValues={":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _release_backfill_marker(owner_id: str) -> None:
    """Roll back a claimed-but-not-granted marker so a re-run retries the grant."""
    _fast_gate_table().update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="REMOVE backfill_done_at",
    )


async def backfill_owner(owner_id: str, balance_microcents: int, *, apply: bool, credits_table: str) -> bool:
    """Grant one owner's balance as a Metronome credit. Returns True if a grant
    was made (or would be, in dry-run), False if skipped (zero/negative balance,
    already done, or no Stripe customer)."""
    # Defensive guard for direct callers. run_backfill already filters zero and
    # negative balances upstream (and counts overdrafts as `negative_skipped` —
    # debt the cutover intentionally forgives, since Metronome has no negative
    # prepaid credit). Nothing to grant for a non-positive balance.
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

    # Claim the marker FIRST (conditional, atomic) so the grant is at-most-once
    # even across crashes/concurrent runs. If the grant then fails, release the
    # marker so a re-run retries instead of skipping a never-granted owner.
    if not _claim_backfill_marker(owner_id):
        logger.info("SKIP %s — marker already claimed (concurrent or prior run)", owner_id)
        return False
    try:
        await ensure_metronome_customer(owner_id=owner_id, stripe_customer_id=stripe_customer_id)
        await fund_backfill_credit(
            owner_id=owner_id,
            amount_microcents=balance_microcents,
            contract_id=settings.METRONOME_CREDITS_CONTRACT_ID,
            product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
        )
    except Exception:
        _release_backfill_marker(owner_id)
        logger.exception("FAILED %s — grant errored; marker released for retry", owner_id)
        raise
    logger.info("GRANTED %s: %d microcents", owner_id, balance_microcents)
    return True


async def run_backfill(*, env: str, apply: bool, credits_table: str) -> dict:
    table = _credits_table(credits_table)
    # `candidates` counts owners that still NEED a backfill (positive balance,
    # marker not yet set) — NOT every positive-balance owner — so on a re-run
    # the summary reflects remaining work, not total positive balances. Already-
    # done owners are tracked separately (`skipped_already_done`) and overdrafts
    # in `negative_skipped` (debt forgiven at cutover — see backfill_owner).
    candidates = 0
    granted = 0
    skipped_already_done = 0
    negative_skipped = 0
    negative_microcents = 0
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("user_id")
            balance = int(item.get("balance_microcents", 0))
            if balance < 0:
                negative_skipped += 1
                negative_microcents += balance
                logger.warning("SKIP %s — negative balance %d microcents (debt forgiven)", owner_id, balance)
                continue
            if balance == 0:
                continue
            if _already_backfilled(owner_id):
                skipped_already_done += 1
                continue
            candidates += 1
            if await backfill_owner(owner_id, balance, apply=apply, credits_table=credits_table):
                if apply:
                    granted += 1
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    summary = {
        "candidates": candidates,
        "granted": granted,
        "skipped_already_done": skipped_already_done,
        "negative_skipped": negative_skipped,
        "negative_microcents": negative_microcents,
        "apply": apply,
    }
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
