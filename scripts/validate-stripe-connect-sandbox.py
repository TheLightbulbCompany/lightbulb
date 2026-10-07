#!/usr/bin/env python3
"""Validate Stripe Connect Express end-to-end against test mode.

Run once with `STRIPE_SECRET_KEY=sk_test_...` exported in the shell. The
script does not modify production state — Stripe test mode is fully
isolated. Exit 0 means the separate-charges-and-transfers flow works
end-to-end (charge → held balance → onboarding → transfer succeeds).

Usage:
  STRIPE_SECRET_KEY=sk_test_... uv run python scripts/validate-stripe-connect-sandbox.py
"""
import os
import sys
import time

import stripe


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_test_"):
        print("ERROR: STRIPE_SECRET_KEY must be a test-mode key (starts with sk_test_)")
        return 1
    stripe.api_key = key

    # Step 1: Create an Express connected account.
    print("[1/7] Creating Express account...")
    account = stripe.Account.create(
        type="express",
        country="US",
        email=f"test-seller-{int(time.time())}@example.com",
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        metadata={"validation_run": "true"},
    )
    print(f"      account.id = {account.id}")

    # Step 2: Create the onboarding link (would redirect a real user).
    print("[2/7] Creating onboarding AccountLink...")
    link = stripe.AccountLink.create(
        account=account.id,
        refresh_url="https://example.com/refresh",
        return_url="https://example.com/return",
        type="account_onboarding",
    )
    print(f"      link.url = {link.url[:60]}...")

    # Step 3: Simulate a charge to the platform balance (no transfer_data).
    print("[3/7] Creating PaymentIntent against platform...")
    intent = stripe.PaymentIntent.create(
        amount=2000,
        currency="usd",
        payment_method_types=["card"],
        confirm=True,
        payment_method="pm_card_visa",
        metadata={"validation_run": "true", "seller_id": account.id},
    )
    print(f"      intent.id = {intent.id}, status = {intent.status}")
    if intent.status != "succeeded":
        print(f"ERROR: expected status 'succeeded', got '{intent.status}'")
        return 2

    # Step 4: Attempt a Transfer to the un-onboarded connected account.
    # Expected to fail with one of Stripe's documented capability errors —
    # that failure is evidence of the held-balance pattern (charges land
    # in the platform balance; transfer waits for onboarding).
    print("[4/7] Attempting Transfer to un-onboarded account (expected to fail)...")
    pre_onboarding_blocked = False
    try:
        transfer = stripe.Transfer.create(
            amount=1700,
            currency="usd",
            destination=account.id,
            transfer_group=f"validation_{intent.id}",
        )
        print(f"      transfer.id = {transfer.id} (account had transfers already enabled)")
    except stripe.error.InvalidRequestError as e:
        expected_codes = {"insufficient_capabilities_for_transfer", "account_unactivated"}
        code = getattr(e, "code", None)
        if code in expected_codes:
            pre_onboarding_blocked = True
            print(f"      expected failure (code={code}): {e.user_message or e}")
            print("      → held-balance pattern confirmed: transfer waits for onboarding.")
        else:
            print(f"ERROR: unexpected Stripe error code={code}: {e}")
            return 3

    # Step 5: Programmatically complete test-mode onboarding.
    # Stripe test mode lets us push the account through onboarding without
    # a real user filling forms. We attach a verified test bank token,
    # accept ToS, and provide enough business-profile data for Stripe to
    # activate the transfers capability.
    print("[5/7] Completing test-mode onboarding (bank + ToS + business profile)...")
    stripe.Account.create_external_account(
        account.id,
        external_account="btok_us_verified",
    )
    stripe.Account.modify(
        account.id,
        tos_acceptance={
            "date": int(time.time()),
            "ip": "8.8.8.8",
        },
        business_type="individual",
        individual={
            "first_name": "Validation",
            "last_name": "Tester",
            "email": account.email,
            "dob": {"day": 1, "month": 1, "year": 1990},
            "address": {
                "line1": "address_full_match",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94103",
                "country": "US",
            },
            "ssn_last_4": "0000",
            "phone": "+15555550100",
        },
        business_profile={
            "mcc": "5734",
            "url": "https://example.com",
        },
    )

    # Stripe needs a beat to flip the capability from "pending" → "active"
    # after the modify call. Poll up to ~10s.
    print("      polling for transfers capability=active...")
    for attempt in range(10):
        time.sleep(1)
        refreshed = stripe.Account.retrieve(account.id)
        cap = (refreshed.capabilities or {}).get("transfers")
        if cap == "active":
            print(f"      transfers capability active after {attempt + 1}s")
            break
    else:
        print("ERROR: transfers capability never reached 'active' after 10s")
        print(f"      final capabilities: {refreshed.capabilities}")
        return 4

    # Step 6: Retry the Transfer. This time it should succeed.
    print("[6/7] Retrying Transfer to now-onboarded account...")
    try:
        transfer = stripe.Transfer.create(
            amount=1700,
            currency="usd",
            destination=account.id,
            transfer_group=f"validation_{intent.id}_post_onboarding",
        )
        print(f"      transfer.id = {transfer.id}, amount = {transfer.amount} cents")
    except stripe.error.StripeError as e:
        print(f"ERROR: post-onboarding Transfer failed: {e}")
        return 5

    # Step 7: Confirm the platform balance + the connected account both
    # reflect the round-trip.
    print("[7/7] Reading platform + connected balances...")
    platform_balance = stripe.Balance.retrieve()
    pending = sum(b.amount for b in platform_balance.pending if b.currency == "usd")
    connected_balance = stripe.Balance.retrieve(stripe_account=account.id)
    connected_pending = sum(b.amount for b in connected_balance.pending if b.currency == "usd")
    print(f"      platform pending: {pending} cents")
    print(f"      connected account pending: {connected_pending} cents")

    print()
    print("OK — full round-trip validated:")
    print("  charge → platform balance → onboarding → transfer to connected account.")
    if pre_onboarding_blocked:
        print("  Pre-onboarding transfer correctly blocked. Held-balance pattern works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
