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


def _is_paying(account: dict) -> bool:
    """Whether this owner has a live or recoverable subscription.

    NOT the same set as ``provision_gate.is_subscription_active`` — do not
    "restore" parity between them, they answer different questions:

      is_subscription_active  {active, trialing, trial_local}
          "may use the platform right now"
      _PAID_STATUSES          {active, trialing, past_due, unpaid}
          "has a live or recoverable subscription"

    The differences are deliberate. ``trial_local`` is excluded because
    free-tier owners must never get a ledger at all. ``past_due``/``unpaid``
    are included because they still have a live subscription and can recover
    — a ledger they cannot yet spend from blocks nothing, and having it ready
    means recovery does not also need a provisioning round trip.

    What IS borrowed from ``is_subscription_active`` is the legacy fallback: a
    row with ``subscription_status is None`` and a ``stripe_subscription_id``
    is a real paid subscription whose status was never backfilled by
    ``customer.subscription.updated``, and that function treats it as active.
    Skipping those here would leave exactly the owners the gates consider paid
    without a ledger — the lockout this script exists to prevent. The None
    guard is strict, matching it: a NON-None status outside the allowlist
    (canceled, incomplete) stays excluded even with a stale
    ``stripe_subscription_id`` on the row.
    """
    status = account.get("subscription_status")
    if status in _PAID_STATUSES:
        return True
    return status is None and bool(account.get("stripe_subscription_id"))


def _needs_backfill(account: dict) -> bool:
    """A paid Bedrock owner whose ledger is absent OR half-built.

    Both ids are required. When the Stripe-link step fails,
    ``metronome_provisioning._provision`` deliberately persists only
    ``metronome_customer_id`` and defers the contract — and without a contract
    there are no prepaid commits, so no balance can exist and the fences treat
    it as unprovisioned. Re-running ensure on that owner completes the link.
    """
    if not _is_paying(account):
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

    from core.dynamodb import get_table, run_in_thread, table_name

    resolved = table_name("billing-accounts")
    # Log the table we ACTUALLY read. Table naming comes from
    # DYNAMODB_TABLE_PREFIX (default "isol8-dev-"), NOT from ENVIRONMENT, so an
    # --env prod run that failed to set the prefix would quietly scan dev and
    # report "0 need a ledger" — which reads as "prod is clean, safe to deploy
    # the fences" and would lock out every real paid owner on the flip. main()
    # sets the prefix from --env and asserts it; this line is the operator's
    # own confirmation, so a zero is never ambiguous.
    logger.info("Scanning table: %s", resolved)

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

    from core.repositories import billing_repo

    failed: list[str] = []
    for a in targets:
        owner_id = a["owner_id"]
        try:
            await ensure_metronome_customer(owner_id=owner_id)
        except Exception:
            # Keep going: one owner's Metronome failure must not strand the
            # rest of the fleet behind it. Re-run to retry just these.
            failed.append(owner_id)
            logger.exception("FAILED owner=%s", owner_id)
            continue

        # VERIFY, do not assume. `ensure_metronome_customer` returns NORMALLY
        # on the deferred-contract path: when the Stripe link is unavailable
        # (or the row has no stripe_customer_id) `_provision` persists only
        # metronome_customer_id and returns the id, because a contract's
        # billing config is create-time-only and creating it unlinked would
        # strand the owner's top-ups permanently.
        #
        # So "no exception" is NOT "provisioned". Trusting it would log a
        # success, exit 0, and hand the operator a clean bill of health for an
        # owner both fences still classify as unprovisioned — who is then
        # locked out the moment the fences deploy, which is the exact outcome
        # this script exists to prevent. Same silent-success shape as the
        # table-prefix bug: the dangerous failure is the one that looks fine.
        #
        # Strongly consistent: we are re-reading ids written moments ago and
        # deciding a deploy-gating exit code on them.
        after = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        customer_id = after.get("metronome_customer_id")
        contract_id = after.get("metronome_contract_id")
        if not (customer_id and contract_id):
            failed.append(owner_id)
            logger.error(
                "INCOMPLETE owner=%s customer=%s contract=%s — ensure returned without raising, "
                "but the ledger is half-built (deferred contract). Re-run after fixing the "
                "Stripe link; both fences treat this owner as unprovisioned.",
                owner_id,
                customer_id or "-",
                contract_id or "-",
            )
            continue

        logger.info("provisioned owner=%s customer=%s contract=%s", owner_id, customer_id, contract_id)

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

    # Table naming reads DYNAMODB_TABLE_PREFIX, which DEFAULTS TO "isol8-dev-"
    # — ENVIRONMENT does not affect it. Setting only ENVIRONMENT made an
    # `--env prod` run scan the DEV table and report "0 need a ledger", i.e. a
    # clean bill of health for a prod fleet it never looked at. Set the prefix
    # explicitly, and OVERWRITE rather than setdefault so an inherited dev
    # value in the operator's shell cannot silently win.
    #
    # This must happen before core.config is imported (settings is built at
    # import time and core.dynamodb snapshots the prefix at ITS import). Every
    # import in this module is function-local for exactly that reason — do not
    # hoist them to the top.
    os.environ["ENVIRONMENT"] = args.env
    os.environ["DYNAMODB_TABLE_PREFIX"] = f"isol8-{args.env}-"

    from core.dynamodb import table_name

    resolved = table_name("billing-accounts")
    expected = f"isol8-{args.env}-billing-accounts"
    if resolved != expected:
        # Belt and braces: if the prefix did not take (a stale import order, a
        # future refactor), refuse rather than report a confident zero about
        # the wrong table.
        logger.error("Table resolution mismatch: got %s, expected %s — refusing to run", resolved, expected)
        return 2

    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
