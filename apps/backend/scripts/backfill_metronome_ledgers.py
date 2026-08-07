"""One-shot: provision a Metronome ledger for every paid Bedrock owner missing one.

WHY THIS EXISTS
---------------
The credit fences (``metronome_gate.is_owner_blocked`` and the bedrock-gate
Lambda's ``paidReserve``) now fail CLOSED when a ``bedrock_claude`` owner has no
complete Metronome ledger — because "no ledger" means nothing can be debited, so
admitting is unmetered Bedrock spend on our account rather than leniency.

Existing rows do not get a ledger on their own. ``ensure_metronome_customer`` is
called from ``ensure_user_ready`` (which a healthy personal owner with a running
container does not re-enter — ``routers/container.py`` re-dispatches it only for
org context or a failed Teams seat) and from the ``entering_live`` webhook
transition (which an owner who has been ``active`` for weeks never re-enters).
So without this backfill, flipping the fences closed locks out every
already-converted paid owner at once: no container start, no chat, and a 429 on
every in-container Bedrock call including cron and channel runs that have no UI
to show a card.

RUN THIS BEFORE DEPLOYING THE FENCES, not after.

Usage::

    cd apps/backend
    uv run python -m scripts.backfill_metronome_ledgers --env prod --dry-run
    uv run python -m scripts.backfill_metronome_ledgers --env prod

``--dry-run`` (default off) reports what would be provisioned and writes
nothing. Idempotent: ``ensure_metronome_customer`` short-circuits on the
persisted ids, so re-running only picks up owners it previously failed on.

DELETE THIS SCRIPT once it has run against dev + prod — per CLAUDE.md, a
one-shot backfill whose data is produced at creation time afterwards is git
history, not a file to keep.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("backfill_metronome_ledgers")

# Owners whose inference we bill. chatgpt_oauth / byo_key owners pay their own
# provider, so a ledger for them is pure pollution — and the fences never
# consult it for them (every call site checks provider_choice first).
_BEDROCK_PROVIDER = "bedrock_claude"

# Statuses that mean "this owner is paying us right now". trial_local is
# deliberately absent: free-tier owners are governed by the $3/day meter cap and
# must never be given a ledger (ensure_metronome_customer short-circuits them,
# and a zero balance would read as "out of credits" to the low-balance
# enforcer). past_due/unpaid are included — they still have a live subscription
# and can recover, and a ledger they cannot spend from blocks nothing.
_PAID_STATUSES = {"active", "trialing", "past_due", "unpaid"}


def _needs_backfill(account: dict) -> bool:
    """A paid Bedrock owner whose ledger is absent OR half-built.

    Both ids are required. When the Stripe-link step fails,
    ``metronome_provisioning._provision`` deliberately persists only
    ``metronome_customer_id`` and defers the contract — and without a contract
    there are no prepaid commits, so no balance can exist and the fences treat
    it as unprovisioned. Re-running ensure on that owner completes the link.
    """
    if account.get("subscription_status") not in _PAID_STATUSES:
        return False
    if account.get("provider_choice") != _BEDROCK_PROVIDER:
        return False
    return not (account.get("metronome_customer_id") and account.get("metronome_contract_id"))


async def _scan_billing_accounts() -> list[dict]:
    """Every billing row, paginated. There is no GSI on
    (subscription_status, provider_choice) and this runs once, so a full scan
    is the right tool — matching the other one-shot backfills in this
    directory."""
    from typing import Any

    from core.dynamodb import get_table, run_in_thread

    table = get_table("billing-accounts")
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        items.extend(resp.get("Items", []))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def _run(dry_run: bool) -> int:
    from core.billing.metronome_provisioning import ensure_metronome_customer

    accounts = await _scan_billing_accounts()
    targets = [a for a in accounts if _needs_backfill(a)]

    logger.info(
        "Scanned %d billing rows; %d need a Metronome ledger",
        len(accounts),
        len(targets),
    )
    for a in targets:
        logger.info(
            "  %s status=%s customer=%s contract=%s",
            a.get("owner_id"),
            a.get("subscription_status"),
            a.get("metronome_customer_id") or "-",
            a.get("metronome_contract_id") or "-",
        )

    if dry_run:
        logger.info("DRY RUN — nothing written")
        return 0

    failed: list[str] = []
    for a in targets:
        owner_id = a["owner_id"]
        try:
            customer_id = await ensure_metronome_customer(owner_id=owner_id)
            logger.info("provisioned owner=%s customer=%s", owner_id, customer_id)
        except Exception:
            # Keep going: one owner's Metronome failure must not strand the
            # rest of the fleet behind it. Re-run to retry just these.
            failed.append(owner_id)
            logger.exception("FAILED owner=%s", owner_id)

    logger.info("Done: %d provisioned, %d failed", len(targets) - len(failed), len(failed))
    if failed:
        # Non-zero so an operator cannot mistake a partial run for a clean one
        # and deploy the fences on top of it.
        logger.error("Owners still without a ledger: %s", ", ".join(failed))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.environ.setdefault("ENVIRONMENT", args.env)

    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
