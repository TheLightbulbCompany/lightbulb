"""Backfill Stripe customers' invoice default payment method.

Stripe Checkout attaches the collected payment method to the SUBSCRIPTION
only — the Customer's ``invoice_settings.default_payment_method`` stays
empty. Metronome's charge_automatically collection charges the CUSTOMER
default, so every customer onboarded before the subscription webhook
started promoting the PM has un-collectable Metronome invoices. This
script repairs the existing fleet: for each billing row with a
``stripe_customer_id``, if the customer has no invoice default AND exactly
one attached payment method, promote it. Zero or multiple attached PMs are
skipped — picking one would be a guess about which card the user wants
charged, so those are left for the webhook (next subscription event) or an
operator.

Dry-run by default; pass ``--apply`` to mutate Stripe. Requires AWS SSO
(``aws sso login --profile isol8-admin``) and the env's Stripe secret key:

    cd apps/backend && STRIPE_SECRET_KEY=sk_... AWS_PROFILE=isol8-admin \\
        uv run python -m scripts.backfill_default_payment_methods --env dev
    cd apps/backend && STRIPE_SECRET_KEY=sk_... AWS_PROFILE=isol8-admin \\
        uv run python -m scripts.backfill_default_payment_methods --env prod --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import boto3
import stripe

REGION = "us-east-1"


def _scan_stripe_customer_ids(env: str) -> list[str]:
    """Paginate the billing-accounts table and return the distinct
    stripe_customer_ids. Distinct because customers are keyed by email and
    multiple billing rows (personal + org) can share one Stripe customer —
    processing a customer twice would just waste API calls."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    table = f"isol8-{env}-billing-accounts"
    customer_ids: set[str] = set()
    scan_kwargs: dict = {"TableName": table}
    while True:
        resp = ddb.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            cid = item.get("stripe_customer_id", {}).get("S")
            if cid:
                customer_ids.add(cid)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return sorted(customer_ids)


def process_customer(customer_id: str, *, apply: bool) -> str:
    """Returns the outcome bucket for the summary."""
    customer = stripe.Customer.retrieve(customer_id)
    if customer.get("deleted"):
        print(f"  SKIP {customer_id}: deleted in Stripe")
        return "deleted"
    if (customer.get("invoice_settings") or {}).get("default_payment_method"):
        return "already_set"

    pms = stripe.Customer.list_payment_methods(customer_id).data
    if len(pms) == 0:
        print(f"  SKIP {customer_id}: no attached payment methods")
        return "no_pm"
    if len(pms) > 1:
        # Ambiguous — choosing the default card on the user's behalf is not
        # this script's call.
        print(f"  SKIP {customer_id}: {len(pms)} attached payment methods")
        return "multiple_pm"

    pm_id = pms[0].id
    if apply:
        stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": pm_id})
        print(f"  SET  {customer_id}: default payment method -> {pm_id}")
    else:
        print(f"  WOULD SET {customer_id}: default payment method -> {pm_id}")
    return "set"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Set Stripe customers' invoice default payment method where empty and "
            "exactly one PM is attached. Defaults to dry-run."
        ),
    )
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--apply", action="store_true", help="Actually modify Stripe (default: dry-run).")
    args = parser.parse_args()

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        sys.exit("STRIPE_SECRET_KEY must be set in the environment (the env's Stripe secret key).")

    customer_ids = _scan_stripe_customer_ids(args.env)
    print(f"[{args.env}] {len(customer_ids)} distinct Stripe customer(s) on billing rows")

    outcomes: Counter[str] = Counter()
    for customer_id in customer_ids:
        try:
            outcomes[process_customer(customer_id, apply=args.apply)] += 1
        except Exception as e:  # noqa: BLE001 — one bad customer must not stop the sweep
            print(f"  ERR  {customer_id}: {e}")
            outcomes["error"] += 1

    print("\nSummary:")
    for bucket in ("set", "already_set", "no_pm", "multiple_pm", "deleted", "error"):
        if outcomes[bucket]:
            print(f"  {bucket:12s} {outcomes[bucket]}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
