"""Payout service: Stripe Connect Express onboarding + Transfer creation.

v1 launches with US sellers only. International support is post-v1.

Connect flow uses 'separate charges and transfers':
  1. Buyer's purchase -> Stripe Charge to the platform balance.
  2. Seller onboards via Express -> Connect account exists.
  3. Held balance flushed via stripe.Transfer.create() to the connected account.

This module owns steps 2 and 3. Step 1 (Charge) lives in marketplace_service
(Plan 2). The webhook handler (also Plan 2) calls back into this module on
account.updated to flush held balances.
"""

import time
import uuid
from dataclasses import dataclass

import stripe

from core.config import settings


SUPPORTED_COUNTRIES = {"US"}


class UnsupportedCountryError(Exception):
    """Raised when seller's country is not supported in v1."""


async def create_connect_account(*, seller_id: str, email: str, country: str) -> str:
    """Create a Stripe Connect Express account for a seller. Returns account_id.

    Idempotency: a fresh uuid4 per call. Stable-key idempotency
    (e.g. `connect_account_create:{seller_id}`) is dangerous here —
    Stripe's 24h cache would return a now-deleted account_id if the
    seller's Stripe account was wiped out-of-band (admin reset, dev-env
    cleanup, abuse takedown). De-dup is the caller's job: marketplace_payouts
    /onboard reads marketplace-payout-accounts first and skips this
    function when an account_id is already on file. The remaining race
    (two concurrent first-time onboarders for the same seller) creates
    one orphan account at most; Stripe's empty-account inventory is
    a small operational cost we accept v0.
    """
    if country not in SUPPORTED_COUNTRIES:
        raise UnsupportedCountryError(f"v1 supports only {SUPPORTED_COUNTRIES}; got {country}")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    account = stripe.Account.create(
        type="express",
        country=country,
        email=email,
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        metadata={"seller_id": seller_id},
        idempotency_key=f"connect_account_create:{uuid.uuid4()}",
    )
    return account.id


async def create_onboarding_link(*, connect_account_id: str, refresh_url: str, return_url: str) -> str:
    """Create a one-time Stripe Express onboarding link. Returns the URL."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    link = stripe.AccountLink.create(
        account=connect_account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url


async def find_transfer_id_by_group(*, transfer_group: str) -> str | None:
    """The existing Transfer for this purchase's transfer_group, or None.

    Transfer groups are unique per purchase (checkout embeds a timestamp), so
    at most one legitimate Transfer exists per group. The flush checks this
    BEFORE creating a transfer and adopts a match instead — closing the
    crash-before-stamp window generally: a transfer that succeeded but never
    got recorded on the purchase row is found and stamped on retry rather
    than duplicated, regardless of which idempotency-key bucket the retry
    lands in. Same lookup refund_purchase already relies on for reversals.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    transfers = stripe.Transfer.list(transfer_group=transfer_group, limit=1)
    return transfers.data[0].id if transfers.data else None


async def transfer_held_balance(*, connect_account_id: str, amount_cents: int, transfer_group: str) -> str:
    """Create a Transfer from platform balance to the connected account.

    transfer_group: groups Transfers logically per purchase batch; useful for
    Reversals on refund.
    Returns the Stripe transfer_id.

    The idempotency key is DAY-BUCKETED (UTC): Stripe caches error responses
    under an idempotency key for 24h, so the old static key let one
    balance_insufficient failure — the NORMAL first-attempt outcome at
    fulfillment time, since the buyer's charge is still pending — poison
    every webhook retry for a day even after funds became available (proven
    live in dev 2026-08-14). Same-day concurrency still dedups on the shared
    key; day rollover matches the timescale on which card charges actually
    settle. The purchase row's set-once seller_transfer_id stamp (checked
    against a consistent read before every transfer) remains the primary
    double-pay guard across buckets.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    day_bucket = time.strftime("%Y%m%d", time.gmtime())
    transfer = stripe.Transfer.create(
        amount=amount_cents,
        currency="usd",
        destination=connect_account_id,
        transfer_group=transfer_group,
        idempotency_key=f"transfer:{connect_account_id}:{transfer_group}:{day_bucket}",
    )
    return transfer.id


@dataclass
class RefundResult:
    refund_id: str
    reversal_id: str | None


async def refund_purchase(*, payment_intent_id: str, transfer_group: str, full_amount_cents: int) -> RefundResult:
    """Refund a buyer's PaymentIntent. If a Transfer to the seller has happened,
    reverse it first to claw back the funds.

    Per design doc separate-charges-and-transfers: the original charge is on
    the platform balance. If the seller hasn't received a Transfer yet (still
    in held balance), refund alone is sufficient. If a Transfer has happened,
    we must reverse it before refunding — otherwise the platform eats the cost.

    Args:
        payment_intent_id: Stripe PaymentIntent ID (`pi_…`). The marketplace
            stores PaymentIntent IDs on purchase rows (not Charge IDs); Stripe's
            Refund.create accepts either via `payment_intent=` or `charge=`.
        transfer_group: The exact transfer_group used at checkout. Caller MUST
            persist this on the purchase row at checkout-completion time and
            read it back here — checkout writes a unique-per-purchase value
            (includes a timestamp suffix) and any reconstruction here would
            miss the seller transfer.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    transfers = stripe.Transfer.list(transfer_group=transfer_group, limit=1)
    reversal_id: str | None = None
    if transfers.data:
        transfer = transfers.data[0]
        reversal = stripe.Transfer.create_reversal(
            transfer.id,
            amount=transfer.amount,
            idempotency_key=f"reversal:{transfer.id}",
        )
        reversal_id = reversal.id

    refund = stripe.Refund.create(
        payment_intent=payment_intent_id,
        amount=full_amount_cents,
        idempotency_key=f"refund:{payment_intent_id}",
    )
    return RefundResult(refund_id=refund.id, reversal_id=reversal_id)
