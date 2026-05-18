"""Purchases, Stripe webhook, refunds.

Isol8-internal scope: buyers are signed-in Isol8 users who deploy
listings into their existing OpenClaw container via the deploy endpoint
in marketplace_listings. There is no CLI installer.
"""

import time
from typing import Annotated

import boto3
import stripe
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from core.auth import AuthContext, get_current_user, require_org_admin
from core.config import settings
from core.services import license_service, payout_service
from core.services import webhook_dedup
from core.services.webhook_dedup import WebhookDedupResult
from schemas import marketplace as schemas


router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace-purchases"])


def _purchases_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PURCHASES_TABLE)


def _payout_accounts_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PAYOUT_ACCOUNTS_TABLE)


@router.post("/checkout")
async def checkout(
    payload: schemas.CheckoutRequest,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create a Stripe Checkout session against the platform account."""
    require_org_admin(auth)
    from core.services import marketplace_service

    listing = await marketplace_service.get_by_slug(slug=payload.listing_slug)
    if not listing or listing["status"] != "published":
        raise HTTPException(status_code=404, detail="listing not available")
    if listing["price_cents"] == 0:
        raise HTTPException(status_code=400, detail="listing is free")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=payload.success_url,
        cancel_url=payload.cancel_url,
        customer_email=payload.email or None,
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": listing["name"]},
                    "unit_amount": listing["price_cents"],
                },
                "quantity": 1,
            }
        ],
        metadata={
            "listing_id": listing["listing_id"],
            "listing_slug": listing["slug"],
            "version": str(listing["version"]),
            "buyer_id": auth.user_id,
            "seller_id": listing["seller_id"],
        },
        payment_intent_data={
            "transfer_group": f"purchase_{listing['listing_id']}_{auth.user_id}_{int(time.time())}",
        },
        idempotency_key=f"checkout:{auth.user_id}:{listing['listing_id']}:{int(time.time() // 60)}",
    )
    return {"checkout_url": session.url, "session_id": session.id}


@router.post(
    "/webhooks/stripe-marketplace",
    summary="Stripe Connect webhook receiver for marketplace events",
    description=(
        "Receives Stripe Connect webhook events for the marketplace. Verifies "
        "the signature against STRIPE_CONNECT_WEBHOOK_SECRET (set in the task "
        "definition). Handles checkout.session.completed (records purchase + "
        "license + held balance), charge.refunded (revokes license), and "
        "account.updated (flushes held balance to seller's Connect account). "
        "Other event types are acknowledged 200 but not acted on. Idempotent "
        "via webhook_dedup."
    ),
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
):
    """Stripe Connect webhook receiver for marketplace events."""
    body = await request.body()
    try:
        event = stripe.Webhook.construct_event(body, stripe_signature, settings.STRIPE_CONNECT_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid signature: {e}")

    # Claim-then-rollback dedup. Record the event first (so two concurrent
    # deliveries can't both run the handler), but if the handler raises
    # transiently, drop the dedup row so Stripe's retry can re-process.
    # Without the rollback, a transient DDB/Stripe error surfaces as 500
    # once and the retry sees ALREADY_SEEN and silently drops the event,
    # losing the purchase / license grant / held-balance update.
    dedup = await webhook_dedup.record_event_or_skip(event_id=event["id"], source="stripe-marketplace")
    if dedup == WebhookDedupResult.ALREADY_SEEN:
        return {"status": "replay-acked"}

    event_type = event["type"]
    obj = event["data"]["object"]
    try:
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            # Both events flow through the same fulfillment path.
            # checkout.session.completed fires once payment is initiated;
            # for delayed-payment methods (ACH, BNPL, bank transfer) it
            # carries payment_status="unpaid" and the actual settlement
            # comes later as async_payment_succeeded. _handle_checkout_completed
            # gates on payment_status="paid" so unpaid sessions are no-ops
            # until the async event arrives.
            await _handle_checkout_completed(obj)
        elif event_type == "charge.refunded":
            await _handle_charge_refunded(obj)
        elif event_type == "account.updated":
            await _handle_account_updated(obj)
        # Other events (transfer.failed, payout.paid, payout.failed,
        # checkout.session.async_payment_failed) are logged via Stripe;
        # async_payment_failed in particular is a no-op because we never
        # granted the license in the first place (gated above).
    except Exception:
        await webhook_dedup.delete_event(event_id=event["id"])
        raise
    return {"status": "ok"}


async def _handle_checkout_completed(session: dict) -> None:
    """Grant license + record purchase + bump seller's held balance.

    Captures the PaymentIntent's transfer_group on the purchase row so that
    /refund can pass the EXACT same key into payout_service.refund_purchase
    when looking up the seller transfer for reversal. Reconstructing the
    key client-side would miss because checkout includes a timestamp suffix.

    Gates on payment_status="paid". For delayed-payment methods (ACH,
    BNPL, bank transfer) Stripe emits checkout.session.completed with
    payment_status="unpaid" before funds settle, then follows up with
    async_payment_succeeded once paid (or async_payment_failed). Without
    this gate the buyer would receive entitlement on a payment that may
    later fail.
    """
    if session.get("payment_status") != "paid":
        return  # delayed payment not yet settled; wait for async_payment_succeeded.

    metadata = session.get("metadata", {})
    listing_id = metadata.get("listing_id")
    buyer_id = metadata.get("buyer_id")
    seller_id = metadata.get("seller_id")
    version = int(metadata.get("version", "1"))
    amount = session.get("amount_total", 0)
    if not (listing_id and buyer_id and seller_id):
        return  # invalid session metadata, ignore.

    payment_intent_id = session.get("payment_intent")
    transfer_group = ""
    if payment_intent_id:
        # Re-raise StripeError so the webhook dedup rollback fires and Stripe
        # retries the event — silently persisting an empty transfer_group
        # would permanently mark this event "processed" while leaving the
        # purchase row missing the linkage refund_purchase needs to find and
        # reverse the seller transfer (Codex P1, round 9).
        stripe.api_key = settings.STRIPE_SECRET_KEY
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        transfer_group = pi.get("transfer_group") or ""

    # Atomic fulfillment via TransactWriteItems: the purchase row write
    # AND the seller balance credit happen in a single ACID transaction,
    # so partial-failure replay can never leave the system in a state
    # where the buyer has a license but the seller's accounting is missing
    # the credit (or vice versa).
    #
    # Idempotency: purchase_id is deterministic from the Stripe checkout
    # session_id (one buy = one session), and the Put is conditional on
    # attribute_not_exists. A Stripe retry (after webhook_dedup rollback)
    # re-runs the whole transaction; the conditional fails, the entire
    # transaction is canceled, and NEITHER the purchase nor the balance
    # are written a second time. Net: exactly one purchase row + exactly
    # one balance credit per checkout session.
    session_id = session.get("id")
    if not session_id:
        return  # Stripe always sends id on completed sessions; defensive.
    purchase_id = f"cs_{session_id}"
    license_key = license_service.generate()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    client = boto3.client("dynamodb")
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": settings.MARKETPLACE_PURCHASES_TABLE,
                        "Item": {
                            "buyer_id": {"S": buyer_id},
                            "purchase_id": {"S": purchase_id},
                            "listing_id": {"S": listing_id},
                            "seller_id": {"S": seller_id},
                            "listing_version_at_purchase": {"N": str(version)},
                            "entitlement_version_floor": {"N": str(version)},
                            "price_paid_cents": {"N": str(amount)},
                            "stripe_payment_intent_id": {"S": payment_intent_id or ""},
                            "stripe_checkout_session_id": {"S": session_id},
                            "stripe_transfer_group": {"S": transfer_group},
                            "license_key": {"S": license_key},
                            "license_key_revoked": {"BOOL": False},
                            "status": {"S": "paid"},
                            "install_count": {"N": "0"},
                            "created_at": {"S": now_iso},
                        },
                        "ConditionExpression": "attribute_not_exists(purchase_id)",
                    },
                },
                {
                    "Update": {
                        "TableName": settings.MARKETPLACE_PAYOUT_ACCOUNTS_TABLE,
                        "Key": {"seller_id": {"S": seller_id}},
                        "UpdateExpression": (
                            "SET balance_held_cents = if_not_exists(balance_held_cents, :zero) + :amt, "
                            "    last_balance_update_at = :now"
                        ),
                        "ExpressionAttributeValues": {
                            ":zero": {"N": "0"},
                            ":amt": {"N": str(amount)},
                            ":now": {"S": now_iso},
                        },
                    },
                },
            ]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            return  # Already fulfilled on a prior delivery; idempotent no-op.
        raise


async def _handle_charge_refunded(charge: dict) -> None:
    """Revoke license on refund. Looks up the purchase via the
    payment-intent GSI — single Query, not a full-table scan.
    """
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return
    table = _purchases_table()
    resp = table.query(
        IndexName="payment-intent-index",
        KeyConditionExpression="stripe_payment_intent_id = :pi",
        ExpressionAttributeValues={":pi": payment_intent_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return
    purchase = items[0]
    await license_service.revoke(
        purchase_id=purchase["purchase_id"],
        buyer_id=purchase["buyer_id"],
        reason="refunded",
    )
    # Reconcile held balance for pre-payout refunds. The original
    # checkout added price_paid_cents to balance_held_cents but nothing
    # subtracts on refund — combined with R11's skip-refunded-in-flush,
    # the seller's held balance would stay permanently inflated. Only
    # decrement when seller_transfer_id is absent: if the transfer
    # already happened, refund_purchase already triggered a Transfer
    # Reversal which separately clawed back the money on the seller
    # ledger, so the held balance was already drained at flush time.
    seller_id = purchase.get("seller_id") or (purchase.get("metadata") or {}).get("seller_id")
    if not seller_id:
        # Older purchase rows wrote seller_id into checkout metadata only;
        # look it up from the listing as a fallback. New rows store it
        # directly (post-R10 atomic write).
        listing_id = purchase.get("listing_id")
        if listing_id:
            listings_table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
            v_at_purchase = int(purchase.get("listing_version_at_purchase", 1)) or 1
            li = listings_table.get_item(Key={"listing_id": listing_id, "version": v_at_purchase}).get("Item")
            seller_id = (li or {}).get("seller_id")
    if not purchase.get("seller_transfer_id") and seller_id:
        amount = int(purchase.get("price_paid_cents", 0))
        if amount > 0:
            try:
                _payout_accounts_table().update_item(
                    Key={"seller_id": seller_id},
                    UpdateExpression="SET balance_held_cents = balance_held_cents - :amt",
                    ConditionExpression="balance_held_cents >= :amt",
                    ExpressionAttributeValues={":amt": amount},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # Held balance is already short of this amount — likely a
                # concurrent flush+refund race or a manual ops correction.
                # Leave the row alone rather than going negative.


async def _handle_account_updated(account: dict) -> None:
    """When seller onboarding completes, flush held balance via per-purchase
    Transfers — each one keyed by the purchase's stored transfer_group.

    Per-purchase rather than batched: refund flow looks up the seller
    transfer for reversal via ``stripe.Transfer.list(transfer_group=…)``
    using the purchase row's stored ``stripe_transfer_group``. A single
    aggregated ``flush_{seller}_{ts}`` transfer would not be findable that
    way, so refunds would issue to the buyer without clawing back the
    seller's funds (platform eats the cost). Iterating per-purchase keeps
    refund reversal correct.

    Idempotency: each purchase row carries ``seller_transfer_id`` once
    transferred; subsequent calls (Stripe replays, repeat onboarding
    events) skip already-paid rows. Stripe's idempotency_key on
    Transfer.create (keyed off transfer_group) is a defense-in-depth
    second layer.

    Race tolerance: balance_held_cents is decremented (not zeroed) by the
    transferred amount, so a new purchase arriving mid-flush still has
    its held balance preserved for the next onboarding/flush event.
    """
    if not account.get("payouts_enabled"):
        return
    seller_id = (account.get("metadata") or {}).get("seller_id")
    if not seller_id:
        return
    pa_table = _payout_accounts_table()
    resp = pa_table.get_item(Key={"seller_id": seller_id})
    pa = resp.get("Item", {})
    held = pa.get("balance_held_cents", 0)
    connect_account_id = pa.get("stripe_connect_account_id") or account.get("id")
    if held <= 0 or not connect_account_id:
        return

    listings_table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
    purchases_table = _purchases_table()
    # Page through all of the seller's listings — DynamoDB Query caps each
    # page at 1MB, so a prolific seller could otherwise have purchases on
    # later listings silently skipped during payout flush.
    seller_listings: list[dict] = []
    listings_kwargs: dict = {
        "IndexName": "seller-created-index",
        "KeyConditionExpression": "seller_id = :s",
        "ExpressionAttributeValues": {":s": seller_id},
    }
    while True:
        page = listings_table.query(**listings_kwargs)
        seller_listings.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        listings_kwargs["ExclusiveStartKey"] = last

    transferred_total = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for listing in seller_listings:
        listing_id = listing["listing_id"]
        kwargs: dict = {
            "IndexName": "listing-created-index",
            "KeyConditionExpression": "listing_id = :l",
            "ExpressionAttributeValues": {":l": listing_id},
        }
        while True:
            page = purchases_table.query(**kwargs)
            for purchase in page.get("Items", []):
                if purchase.get("seller_transfer_id"):
                    continue  # already transferred on a prior flush
                # Skip refunded purchases — license_service.revoke leaves
                # price_paid_cents intact (so the buyer's history still
                # shows what they paid), so without an explicit refund
                # check the flush would Transfer to the seller for a
                # purchase the buyer was already refunded for. That's a
                # direct accounting loss: buyer refunded AND seller paid.
                if purchase.get("license_key_revoked") or purchase.get("status") == "refunded":
                    continue
                amount = int(purchase.get("price_paid_cents", 0))
                tg = purchase.get("stripe_transfer_group")
                if amount <= 0 or not tg:
                    continue  # legacy / malformed row
                # listing-created-index is eventually consistent: a replayed
                # account.updated (or a concurrent run) can re-read the same
                # purchase before the previous run's seller_transfer_id is
                # visible. Stripe's idempotency_key on transfer_held_balance
                # catches the duplicate transfer (returns the existing
                # transfer_id), but without an atomic guard here we'd
                # double-count the amount in transferred_total and the held
                # balance would be decremented twice for one transfer.
                # The conditional update on attribute_not_exists is the
                # source of truth for "this run wrote the seller_transfer_id"
                # — only successful update_item calls increment the running
                # total.
                transfer_id = await payout_service.transfer_held_balance(
                    connect_account_id=connect_account_id,
                    amount_cents=amount,
                    transfer_group=tg,
                )
                try:
                    purchases_table.update_item(
                        Key={"buyer_id": purchase["buyer_id"], "purchase_id": purchase["purchase_id"]},
                        UpdateExpression="SET seller_transfer_id = :t, transferred_at = :now",
                        ConditionExpression="attribute_not_exists(seller_transfer_id)",
                        ExpressionAttributeValues={":t": transfer_id, ":now": now_iso},
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                        # Another run got there first — Stripe Transfer was
                        # idempotent so no money moved twice; just don't
                        # count this amount toward our held-balance decrement.
                        continue
                    raise
                transferred_total += amount
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last

    if transferred_total > 0:
        pa_table.update_item(
            Key={"seller_id": seller_id},
            UpdateExpression=(
                "SET balance_held_cents = balance_held_cents - :t, "
                "    onboarding_status = :done, "
                "    lifetime_earned_cents = if_not_exists(lifetime_earned_cents, :zero) + :t"
            ),
            ExpressionAttributeValues={":zero": 0, ":done": "completed", ":t": transferred_total},
        )


@router.post("/refund")
async def refund(
    purchase_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """7-day refund window for buyers."""
    require_org_admin(auth)
    table = _purchases_table()
    resp = table.get_item(Key={"buyer_id": auth.user_id, "purchase_id": purchase_id})
    purchase = resp.get("Item")
    if not purchase:
        raise HTTPException(status_code=404, detail="purchase not found")
    created_iso = purchase["created_at"]
    age_seconds = time.time() - time.mktime(time.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ"))
    if age_seconds > 7 * 24 * 60 * 60:
        raise HTTPException(status_code=403, detail="refund window expired (7 days)")

    result = await payout_service.refund_purchase(
        payment_intent_id=purchase["stripe_payment_intent_id"],
        transfer_group=purchase.get("stripe_transfer_group", ""),
        full_amount_cents=purchase["price_paid_cents"],
    )
    await license_service.revoke(purchase_id=purchase_id, buyer_id=auth.user_id, reason="refunded")
    return {"refund_id": result.refund_id, "reversal_id": result.reversal_id}


# ---------------------------------------------------------------------------
# Buyer purchase history surfacing
# ---------------------------------------------------------------------------


@router.get("/my-purchases", response_model=schemas.MyPurchasesResponse)
async def my_purchases(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """List the caller's marketplace purchases.

    Queries marketplace-purchases by buyer_id (the table's PK). Joins the
    listing slug per row by reading marketplace-listings v1; small N
    (buyers don't buy thousands of items) makes the per-row read fine for v0.
    """
    # The table's sort key is purchase_id (UUID), so ScanIndexForward=False
    # would return reverse-lexical UUID order, not newest-first. Page
    # through all rows for this buyer (v0 volumes are tiny) and sort by
    # created_at in Python. Limit-then-cap-at-100 after sorting so a buyer
    # with >100 purchases sees the genuinely-newest 100, not an arbitrary
    # subset.
    table = _purchases_table()
    raws: list[dict] = []
    kwargs: dict = {
        "KeyConditionExpression": "buyer_id = :b",
        "ExpressionAttributeValues": {":b": auth.user_id},
    }
    while True:
        page = table.query(**kwargs)
        raws.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    raws.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    raws = raws[:100]

    items: list[schemas.PurchaseSummary] = []
    listings_table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
    slug_cache: dict[str, str] = {}
    for raw in raws:
        listing_id = raw.get("listing_id", "")
        if listing_id and listing_id not in slug_cache:
            # Use the listing_version_at_purchase row if available, otherwise
            # fall back to v=1. Avoids 404s on multi-version listings where
            # v=1 was retired before the buyer reads their history.
            v_at_purchase = int(raw.get("listing_version_at_purchase", 1)) or 1
            li = listings_table.get_item(Key={"listing_id": listing_id, "version": v_at_purchase}).get("Item")
            if not li:
                li = listings_table.get_item(Key={"listing_id": listing_id, "version": 1}).get("Item")
            slug_cache[listing_id] = (li or {}).get("slug", "") if li else ""
        items.append(
            schemas.PurchaseSummary(
                purchase_id=raw.get("purchase_id", ""),
                listing_id=listing_id,
                listing_slug=slug_cache.get(listing_id) or None,
                license_key=raw.get("license_key", ""),
                price_paid_cents=int(raw.get("price_paid_cents", 0)),
                status=raw.get("status", "paid"),
                created_at=raw.get("created_at", ""),
            )
        )
    return schemas.MyPurchasesResponse(items=items)
