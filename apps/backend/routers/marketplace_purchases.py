"""Purchases, Stripe webhook, refunds.

Isol8-internal scope: buyers are signed-in Isol8 users who deploy
listings into their existing OpenClaw container via the deploy endpoint
in marketplace_listings. There is no CLI installer.
"""

import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo, marketplace_repo
from core.services import license_service, payout_service, rollup_service
from core.services.owner_facts import write_owner_facts
from core.services import webhook_dedup
from core.services.webhook_dedup import WebhookDedupResult
from schemas import marketplace as schemas


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace-purchases"])

# Fire-and-forget rollup refreshes spawned by the money-event webhook branch.
# The loop holds tasks weakly, so without a strong reference here a refresh
# could be garbage-collected mid-flight. Discard-on-done keeps the set small.
_rollup_refresh_tasks: set[asyncio.Task] = set()


def _validate_return_url(url: str, field: str) -> str:
    """Restrict Stripe Checkout return URLs to Isol8-owned origins.

    These are client-supplied; without the check an org admin could mint a
    legitimate Stripe session that redirects buyers to an arbitrary domain
    after payment (phishing with our checkout as the lure).
    """
    from urllib.parse import urlparse

    apex = settings.root_domain  # product apex, derived from FRONTEND_URL
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # `apex` is our own config, never attacker-controlled, and the suffix check
    # keeps the leading dot so `evil-isol8.co` cannot pass as a subdomain.
    if apex and parsed.scheme == "https" and (host == apex or host.endswith(f".{apex}")):
        return url
    if parsed.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return url  # local dev
    raise HTTPException(status_code=400, detail=f"{field} must be an https URL on {apex}")


@router.post("/checkout")
async def checkout(
    payload: schemas.CheckoutRequest,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create a Stripe Checkout session against the platform account."""
    require_org_admin(auth)
    from core.services import marketplace_service

    listing = await marketplace_service.get_published_by_id(listing_id=payload.listing_id)
    if not listing or listing["status"] != "published":
        raise HTTPException(status_code=404, detail="listing not available")
    if listing["price_cents"] == 0:
        raise HTTPException(status_code=400, detail="listing is free")
    # Purchase identity is the RESOLVED owner (org id for org workspaces, user
    # id for personal): the install is org-scoped, so keying purchases to the
    # individual admin would let a second org admin be charged again for a
    # listing the org already owns. Deploy's entitlement check uses the same
    # identity.
    buyer_identity = resolve_owner_id(auth)
    # A seller is never charged for their own listing — they deploy it free
    # through the seller bypass. A listing's seller_id is the publishing user
    # or, for workspace-published listings, the workspace id.
    if listing.get("seller_id") in {auth.user_id, buyer_identity}:
        raise HTTPException(status_code=409, detail="This is your own listing — you can't buy it.")
    # An entitled buyer must never reach Stripe again — the storefront's only
    # recovery path after a broken paid deploy used to re-prompt payment.
    # Consumers branch on this shape and deploy instead.
    if await marketplace_repo.has_unrevoked_purchase(buyer_id=buyer_identity, listing_id=listing["listing_id"]):
        raise HTTPException(status_code=409, detail={"error": "already_entitled"})

    stripe.api_key = settings.STRIPE_SECRET_KEY
    checkout_ts = int(time.time())
    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=_validate_return_url(payload.success_url, "success_url"),
        cancel_url=_validate_return_url(payload.cancel_url, "cancel_url"),
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
            "buyer_id": buyer_identity,
            "seller_id": listing["seller_id"],
        },
        payment_intent_data={
            "transfer_group": f"purchase_{listing['listing_id']}_{buyer_identity}_{checkout_ts}",
        },
        # Same timestamp as transfer_group: a second click in the same second
        # replays the identical request (Stripe returns the same session); a
        # later click mints a fresh key. A coarser key with a finer-grained
        # body (the old minute-bucket) made Stripe reject the retry with
        # idempotency_error — same key, different params — a 500 to the buyer.
        idempotency_key=f"checkout:{buyer_identity}:{listing['listing_id']}:{checkout_ts}",
    )
    return {"checkout_url": session.url, "session_id": session.id}


@router.post(
    "/webhooks/stripe-marketplace",
    summary="Stripe webhook receiver for marketplace events",
    description=(
        "Receives Stripe webhook events for the marketplace from TWO Stripe "
        "endpoint registrations pointing at this URL: a platform-scoped one "
        "(checkout.session.completed / async_payment_succeeded / "
        "charge.refunded — checkout runs on the platform balance, so Stripe "
        "only delivers these to 'events on your account' endpoints) and a "
        "Connect-scoped one (account.updated for connected accounts). Each "
        "registration has its own signing secret, so verification tries "
        "STRIPE_MARKETPLACE_WEBHOOK_SECRET then STRIPE_CONNECT_WEBHOOK_SECRET. "
        "Other event types are acknowledged 200 but not acted on. Idempotent "
        "via webhook_dedup."
    ),
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
):
    """Stripe webhook receiver for marketplace events (platform + Connect)."""
    body = await request.body()
    event = None
    last_error: Exception | None = None
    # Two Stripe endpoint registrations deliver to this URL (platform-scoped
    # and Connect-scoped), each with its own signing secret. A signature
    # matches exactly one of them.
    for secret in (settings.STRIPE_MARKETPLACE_WEBHOOK_SECRET, settings.STRIPE_CONNECT_WEBHOOK_SECRET):
        if not secret:
            continue
        try:
            event = stripe.Webhook.construct_event(body, stripe_signature, secret)
            break
        except Exception as e:
            last_error = e
    if event is None:
        raise HTTPException(
            status_code=400, detail=f"invalid signature: {last_error or 'no webhook secret configured'}"
        )

    # Claim-then-rollback dedup. Record the event first (so two concurrent
    # deliveries can't both run the handler), but if the handler raises
    # transiently, drop the dedup row so Stripe's retry can re-process.
    # Without the rollback, a transient DDB/Stripe error surfaces as 500
    # once and the retry sees ALREADY_SEEN and silently drops the event,
    # losing the purchase / license grant / held-balance update.
    #
    # The dedup key is SOURCE-PREFIXED: Stripe mints ONE event id per
    # occurrence and delivers it to every subscribed endpoint, and the
    # billing webhook (/billing/webhooks/stripe) subscribes to
    # checkout.session.completed too. Bare event-id keying in the shared
    # dedup table let whichever endpoint claimed first silently swallow
    # the other's processing (lost purchase or lost credit grant).
    dedup = await webhook_dedup.record_event_or_skip(
        event_id=f"stripe-marketplace:{event['id']}", source="stripe-marketplace"
    )
    if dedup == WebhookDedupResult.ALREADY_SEEN:
        return {"status": "replay-acked"}

    event_type = event["type"]
    obj = event["data"]["object"]
    try:
        if event_type in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "charge.refunded",
        ) and event.get("account"):
            # Marketplace checkout and refunds run on the PLATFORM balance, so
            # their events never carry `account`. One that does happened on a
            # connected account (a seller's, or an owner's own business
            # account) — money the platform never received. Treating it as a
            # purchase would grant entitlement and credit a seller from it.
            logger.warning(
                "marketplace webhook: ignoring %s from connected account %s", event_type, event.get("account")
            )
            put_metric("marketplace.webhook.connected_account_ignored", dimensions={"event_type": event_type})
        elif event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
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
        elif event_type == "account.application.deauthorized":
            # The connected account id rides on the event envelope, not the
            # data object (which is the Application).
            await _handle_account_deauthorized(event.get("account"))
        elif event_type in (
            "payment_intent.succeeded",
            "charge.succeeded",
            "invoice.paid",
            "customer.created",
            "payout.paid",
            "payout.failed",
        ):
            # Money motion on a COMPANY account (the owner's own business
            # account) marks the owner-day rollup dirty: recompute today,
            # fire-and-forget — the webhook 200s regardless, and the hourly
            # rollup pass in update_service is the backstop. Same
            # company-vs-seller discrimination as account.updated above.
            #
            # invoice.paid collision: the platform billing endpoint
            # (/billing/webhooks/stripe) also handles invoice.paid — a
            # DIFFERENT Stripe endpoint registration, and the source-prefixed
            # dedup key above ("stripe-marketplace:") keeps the two from
            # swallowing each other's processing.
            #
            # Non-company accounts (sellers) fall through to the ack below,
            # exactly as these events did before this branch existed.
            company_row = await billing_repo.get_by_company_stripe_account_id(event.get("account") or "")
            if company_row is not None:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                task = asyncio.create_task(rollup_service.refresh_owner_day(company_row["owner_id"], today))
                _rollup_refresh_tasks.add(task)

                def _rollup_done(t: asyncio.Task, owner_id: str = company_row["owner_id"], day: str = today) -> None:
                    _rollup_refresh_tasks.discard(t)
                    if not t.cancelled() and t.exception() is not None:
                        logger.warning(
                            "rollup refresh failed owner_id=%s day=%s", owner_id, day, exc_info=t.exception()
                        )

                task.add_done_callback(_rollup_done)
        # Other events (transfer.failed,
        # checkout.session.async_payment_failed) are logged via Stripe;
        # async_payment_failed in particular is a no-op because we never
        # granted the license in the first place (gated above).
    except Exception:
        await webhook_dedup.delete_event(event_id=f"stripe-marketplace:{event['id']}")
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
    # Metadata is the only link from the payment to the listing, so check it
    # against the listing row before crediting anyone: the seller must be the
    # listing's seller and the buyer must have paid its price. A published
    # row's price is immutable, so a mismatch is never a legitimate sale.
    #
    # Strongly consistent, and a MISSING row is retryable, not a mismatch: a
    # checkout can complete moments after its listing version is written, and
    # acking that delivery would leave a paid buyer with no entitlement. The
    # raise rolls back the dedup row so Stripe redelivers.
    listing = await marketplace_repo.get_listing(listing_id, version, consistent=True)
    if not listing:
        raise HTTPException(status_code=503, detail="listing not readable yet; retry")
    if listing.get("seller_id") != seller_id or int(listing.get("price_cents", -1)) != amount:
        logger.warning(
            "marketplace checkout %s does not match listing %s v%s; not fulfilled",
            session.get("id"),
            listing_id,
            version,
        )
        put_metric("marketplace.webhook.checkout_mismatch")
        return

    payment_intent_id = session.get("payment_intent")
    transfer_group = ""
    if payment_intent_id:
        # Re-raise StripeError so the webhook dedup rollback fires and Stripe
        # retries the event — silently persisting an empty transfer_group
        # would permanently mark this event "processed" while leaving the
        # purchase row missing the linkage refund_purchase needs to find and
        # reverse the seller transfer.
        stripe.api_key = settings.STRIPE_SECRET_KEY
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        transfer_group = pi.get("transfer_group") or ""

    # Atomic fulfillment: the purchase row write AND the seller balance
    # credit happen in a single ACID transaction (see the repo function).
    # A Stripe retry (after webhook_dedup rollback) re-runs the whole
    # transaction; the conditional fails and the repo returns False —
    # already fulfilled on a prior delivery, idempotent no-op.
    session_id = session.get("id")
    if not session_id:
        return  # Stripe always sends id on completed sessions; defensive.
    purchase_id = f"cs_{session_id}"
    license_key = license_service.generate()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Platform fee: the buyer pays price_paid_cents; the seller's held-balance
    # credit (and later Transfer) is the post-fee share. We charge on the
    # platform account and transfer separately, so the fee is simply the
    # credit delta — no application_fee_amount involved.
    seller_share = amount * (100 - settings.MARKETPLACE_PLATFORM_FEE_PERCENT) // 100
    await marketplace_repo.transact_record_purchase_and_credit_seller(
        purchase_id=purchase_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        listing_id=listing_id,
        version=version,
        amount_cents=amount,
        seller_share_cents=seller_share,
        payment_intent_id=payment_intent_id or "",
        session_id=session_id,
        transfer_group=transfer_group,
        license_key=license_key,
        now_iso=now_iso,
    )

    # Flush at fulfillment, not just on account.updated: a seller who
    # onboarded BEFORE this sale never gets another account.updated, so
    # without this the money sits in balance_held_cents forever. Failures
    # PROPAGATE deliberately: the webhook's dedup rollback fires and Stripe
    # retries the whole event — fulfillment above is idempotent (the
    # transaction no-ops on replay) and a swallowed flush error would leave a
    # transferred-but-unstamped purchase with no prompt retry, reopening the
    # >24h Stripe-idempotency double-pay window.
    #
    # THIS sale's purchase is passed explicitly: the listing-created-index the
    # flush loop pages is eventually consistent and may not show the row the
    # transaction just wrote, which would make the flush no-op "successfully"
    # for exactly the seller it was added for.
    pa = await marketplace_repo.get_payout_account(seller_id, consistent=True) or {}
    connect_account_id = pa.get("stripe_connect_account_id")
    if connect_account_id:
        acct = stripe.Account.retrieve(connect_account_id)
        if acct.get("payouts_enabled"):
            await _flush_seller_held_balance(
                seller_id=seller_id,
                connect_account_id=connect_account_id,
                known_purchase_key=(buyer_id, purchase_id),
            )


async def _handle_charge_refunded(charge: dict) -> None:
    """Revoke license on refund. Looks up the purchase via the
    payment-intent GSI — single Query, not a full-table scan.
    """
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return
    # charge.refunded also fires on PARTIAL refunds; `refunded` is Stripe's
    # fully-refunded flag. Revoking the license (and clawing back the whole
    # seller share) on a partial refund would punish a purchase the buyer
    # still holds — skip and let a later full refund settle it.
    #
    # ponytail: the seller share is deliberately NOT clawed back
    # proportionally on partials — the platform eats that slice. Partial
    # refunds have no user-facing path (the /refund route is full-price
    # only); they can only be issued by the platform operator from the
    # Stripe dashboard, so the cost is an operator decision, surfaced by the
    # warning below. Build proportional clawback (cumulative amount_refunded
    # tracking on the purchase row + partial transfer reversals) only if
    # partial refunds become a real support pattern.
    if not charge.get("refunded"):
        logging.getLogger(__name__).warning(
            "partial refund on payment_intent=%s (amount_refunded=%s): entitlement kept, "
            "seller share NOT clawed back — platform absorbs this slice",
            payment_intent_id,
            charge.get("amount_refunded"),
        )
        return
    purchase = await marketplace_repo.find_purchase_by_payment_intent(payment_intent_id)
    if purchase is None:
        return
    await license_service.revoke(
        purchase_id=purchase["purchase_id"],
        buyer_id=purchase["buyer_id"],
        reason="refunded",
    )
    seller_id = purchase.get("seller_id") or (purchase.get("metadata") or {}).get("seller_id")
    if not seller_id:
        # Older purchase rows wrote seller_id into checkout metadata only;
        # look it up from the listing as a fallback. New rows store it
        # directly (post-R10 atomic write).
        listing_id = purchase.get("listing_id")
        if listing_id:
            v_at_purchase = int(purchase.get("listing_version_at_purchase", 1)) or 1
            li = await marketplace_repo.get_listing(listing_id, v_at_purchase)
            seller_id = (li or {}).get("seller_id")

    # Branch off a POST-revoke consistent re-read, not the row read above: a
    # flush can stamp seller_transfer_id between that read and here. Whichever
    # of revoke/stamp commits second, its actor's re-read sees the other —
    # the flush's post-stamp re-check catches revoke-after-stamp, this one
    # catches stamp-before-revoke.
    fresh = (
        await marketplace_repo.get_purchase(purchase["buyer_id"], purchase["purchase_id"], consistent=True) or purchase
    )
    if seller_id:
        await _settle_refunded_purchase(fresh, seller_id=seller_id)


async def _settle_refunded_purchase(fresh: dict, *, seller_id: str) -> None:
    """Settle the seller side of a refunded purchase — callable from the
    refund handler AND the flush's refund-race branch, in any interleaving.

    The invariant: held-balance settlement for a purchase happens exactly
    once, owned by the payout_accounted claim; transfer reversals are
    idempotent by transfer id, so they may be re-issued freely.

    Cases off the fresh row:
      - transferred + accounted   → the flush's booking already moved the
        share out of held balance; only the transfer needs reversing.
      - transferred + unaccounted → the refund won a race with the flush (or
        a prior run crashed mid-settle): reverse, claim, debit the share.
      - untransferred             → claim, then debit (no transfer to
        reverse; the flush skips revoked rows so none will be created).
    Whichever of the refund handler / flush claims first performs the debit
    exactly once; the loser's claim fails and it skips — the cross-races
    converge. Reversal-before-claim: a crash between them re-reverses on
    replay (Stripe no-op) instead of losing the reversal.
    """
    amount = int(fresh.get("seller_share_cents", fresh.get("price_paid_cents", 0)))
    transfer_id = fresh.get("seller_transfer_id")
    if transfer_id:
        # Refunds issued from the Stripe DASHBOARD (the realistic support
        # path) arrive with no reversal — without this the buyer gets their
        # money back and the seller keeps theirs, platform eats 100%. Same
        # idempotency key as /refund's refund_purchase, so a double reversal
        # is a Stripe-side no-op.
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.Transfer.create_reversal(
            transfer_id,
            amount=amount,
            idempotency_key=f"reversal:{transfer_id}",
        )
    if fresh.get("payout_accounted"):
        return  # booking already settled the held balance for this purchase
    claimed = await marketplace_repo.mark_payout_accounted(buyer_id=fresh["buyer_id"], purchase_id=fresh["purchase_id"])
    if claimed and amount > 0:
        # Debit the SELLER SHARE (what fulfillment credited), not the
        # buyer-paid price. A False return (balance already short — manual
        # ops correction) leaves the row alone rather than going negative.
        await marketplace_repo.decrement_held_balance(seller_id=seller_id, amount_cents=amount)


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
    # Company accounts (the owner's own business account, created by us or
    # connected via Standard OAuth) mirror their state onto the billing row
    # and never touch the seller flush. Looked up by account id, not by
    # metadata: an OAuth-connected account carries no metadata of ours.
    account_id = account.get("id") or ""
    company_row = await billing_repo.get_by_company_stripe_account_id(account_id)
    if company_row is not None:
        await billing_repo.set_company_stripe_status(
            company_row["owner_id"],
            account_id=account_id,
            details_submitted=bool(account.get("details_submitted")),
            charges_enabled=bool(account.get("charges_enabled")),
            payouts_enabled=bool(account.get("payouts_enabled")),
        )
        # The container reads `company_stripe` off the facts file; without this
        # it keeps asking the owner to set up an account that just went live.
        await write_owner_facts(company_row["owner_id"])
        return

    if not account.get("payouts_enabled"):
        return
    seller_id = (account.get("metadata") or {}).get("seller_id")
    if not seller_id:
        return
    pa = await marketplace_repo.get_payout_account(seller_id) or {}
    connect_account_id = pa.get("stripe_connect_account_id") or account.get("id")
    if not connect_account_id:
        return
    await _flush_seller_held_balance(seller_id=seller_id, connect_account_id=connect_account_id)


async def _handle_account_deauthorized(account_id: str | None) -> None:
    """The owner revoked the platform from their Stripe Dashboard (or we did
    via /company/stripe/disconnect). Unbind; never delete anything."""
    if not account_id:
        return
    row = await billing_repo.get_by_company_stripe_account_id(account_id)
    if row is None:
        return
    if row.get("company_stripe_source") == "api_key":
        # A Connect deauthorization says nothing about a key-bound row: the
        # owner may have disconnected the grant and re-bound the SAME account
        # by key while this event was in flight. Clearing it would strand the
        # key behind no row.
        return
    await billing_repo.clear_company_stripe_account(row["owner_id"], account_id=account_id)
    await write_owner_facts(row["owner_id"])


async def _flush_seller_held_balance(
    *,
    seller_id: str,
    connect_account_id: str,
    known_purchase_key: tuple[str, str] | None = None,
) -> None:
    """Transfer every untransferred, unrefunded purchase's seller share.

    Called from account.updated (seller onboards after selling) AND from
    checkout fulfillment (seller onboarded before selling) — the two
    orderings between "sale happened" and "payouts enabled".

    known_purchase_key (buyer_id, purchase_id): a purchase the caller KNOWS
    exists (fulfillment just wrote it) — processed via a consistent base-table
    read even when the eventually-consistent listing-created-index hasn't
    surfaced it yet, so the fulfillment-time flush can't no-op on exactly the
    sale it was invoked for.

    Settlement is PER-PURCHASE and replayable: each purchase's transfer stamp
    is followed by its own bookkeeping (apply_flush_totals) and a
    payout_accounted marker. A crash between stamp and bookkeeping is healed
    on the next flush — stamped-but-unaccounted rows get their bookkeeping
    applied then. (No pre-rollout cohort to worry about: the purchases table
    has never been written by a real checkout — the webhook was mis-scoped in
    dev and absent in prod.)
    """
    # No zero-balance early exit: recovery work (stamped-but-unaccounted rows,
    # unreversed revoked transfers) must run even when the held balance reads
    # zero — a refund race can drain the balance while leaving a transfer
    # that still needs its compensating reversal. Volumes make the extra
    # pagination free.
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    processed: set[str] = set()

    async def book_settlement(fresh: dict, *, transfer_id: str, amount: int) -> None:
        """Book one stamped transfer exactly once.

        Revoked rows: reverse FIRST (idempotent by transfer id — replay-safe
        if we crash before the claim), then claim. Active rows: CLAIM first
        (two overlapping flushes can both see stamped-but-unaccounted, and
        apply_flush_totals is a non-idempotent decrement — ownership must be
        decided before booking), then apply totals. A crash between claim and
        totals loses at most this purchase's display bookkeeping — logged,
        and strictly better than double-booking money.
        """
        revoked = fresh.get("license_key_revoked") or fresh.get("status") == "refunded"
        if revoked:
            # Same settlement helper the refund handler uses — reverse the
            # transfer (idempotent) and let the accounting claim decide who
            # debits the held share exactly once.
            await _settle_refunded_purchase(dict(fresh, seller_transfer_id=transfer_id), seller_id=seller_id)
            return
        claimed = await marketplace_repo.mark_payout_accounted(
            buyer_id=fresh["buyer_id"], purchase_id=fresh["purchase_id"]
        )
        if not claimed:
            return  # another run booked it
        try:
            await marketplace_repo.apply_flush_totals(seller_id=seller_id, transferred_cents=amount)
        except Exception:
            # The claim is durable but the totals write was lost — the held
            # balance will read HIGH by this share until an operator corrects
            # it. Never re-raised into the webhook: a retry cannot recover it
            # (the claim is taken) and failing the whole flush would strand
            # the seller's other purchases too.
            logging.getLogger(__name__).exception(
                "payout bookkeeping lost for purchase=%s seller=%s amount=%s — balance will read high",
                fresh["purchase_id"],
                seller_id,
                amount,
            )

    async def settle_one(purchase: dict) -> None:
        # Seller share is what fulfillment credited post-fee; legacy rows
        # (pre-fee) carry only price_paid_cents, which WAS their credited
        # amount.
        amount = int(purchase.get("seller_share_cents", purchase.get("price_paid_cents", 0)))
        if purchase.get("seller_transfer_id"):
            # Already transferred. A prior run crashed between the stamp and
            # its bookkeeping (or between a compensating reversal and its
            # claim) — book_settlement re-inspects the revoked state, so a
            # reversed transfer is re-reversed (Stripe no-op) rather than
            # booked as proceeds.
            if not purchase.get("payout_accounted") and amount > 0:
                await book_settlement(purchase, transfer_id=purchase["seller_transfer_id"], amount=amount)
            return
        # Skip refunded purchases — license_service.revoke leaves
        # price_paid_cents intact (so the buyer's history still shows what
        # they paid), so without an explicit refund check the flush would
        # Transfer to the seller for a purchase the buyer was already
        # refunded for. That's a direct accounting loss: buyer refunded AND
        # seller paid.
        if purchase.get("license_key_revoked") or purchase.get("status") == "refunded":
            return
        tg = purchase.get("stripe_transfer_group")
        if amount <= 0 or not tg:
            return  # legacy / malformed row
        # ADOPT before creating: a prior run's transfer may have succeeded
        # without its stamp landing (crash between the Stripe call and the
        # conditional write, or a retry straddling the idempotency-key day
        # boundary). Transfer groups are unique per purchase, so a match IS
        # this purchase's transfer — stamp it instead of minting a second
        # one. This, not the idempotency key, is what makes retries
        # duplicate-safe across key buckets; the key covers same-bucket
        # concurrency, and the set-once conditional write decides which run
        # owns the bookkeeping.
        transfer_id = await payout_service.find_transfer_id_by_group(transfer_group=tg)
        if transfer_id is None:
            try:
                transfer_id = await payout_service.transfer_held_balance(
                    connect_account_id=connect_account_id,
                    amount_cents=amount,
                    transfer_group=tg,
                )
            except stripe.InvalidRequestError as exc:
                if getattr(exc, "code", None) == "balance_insufficient":
                    # The NORMAL first-attempt outcome at fulfillment time: the
                    # buyer's charge is still pending, and transfers draw from
                    # AVAILABLE balance. Re-raised so the webhook 500s and
                    # Stripe's multi-day retry schedule re-runs the flush — the
                    # day-bucketed idempotency key lets a later retry succeed
                    # once the charge settles. WARNING, not exception: expected.
                    logging.getLogger(__name__).warning(
                        "payout flush deferred: platform balance insufficient for %s cents to %s "
                        "(charge likely still pending; Stripe retries converge after settlement)",
                        amount,
                        connect_account_id,
                    )
                raise
        stamped = await marketplace_repo.set_seller_transfer_if_absent(
            buyer_id=purchase["buyer_id"],
            purchase_id=purchase["purchase_id"],
            transfer_id=transfer_id,
            now_iso=now_iso,
        )
        if not stamped:
            # Another run got there first — Stripe Transfer was idempotent so
            # no money moved twice; the stamping run owns the bookkeeping.
            return
        # A refund can race this transfer: charge.refunded saw the row
        # UNstamped, revoked it, and debited only the held balance — with the
        # transfer completing after, the seller would stay paid and no later
        # event would ever reverse it. Re-read post-stamp and let
        # book_settlement pick reversal vs proceeds off the fresh state.
        fresh = (
            await marketplace_repo.get_purchase(purchase["buyer_id"], purchase["purchase_id"], consistent=True) or {}
        )
        await book_settlement(fresh or purchase, transfer_id=transfer_id, amount=amount)

    if known_purchase_key is not None:
        known = await marketplace_repo.get_purchase(*known_purchase_key, consistent=True)
        if known:
            processed.add(known["purchase_id"])
            await settle_one(known)

    # The repo pages through all of the seller's listings — a prolific
    # seller could otherwise have purchases on later listings silently
    # skipped during payout flush.
    for listing in await marketplace_repo.list_listings_by_seller(seller_id):
        for purchase in await marketplace_repo.list_purchases_by_listing(listing["listing_id"]):
            if purchase["purchase_id"] in processed:
                continue
            if purchase.get("seller_transfer_id") and purchase.get("payout_accounted"):
                continue  # fully settled on a prior flush
            # Fresh strongly-consistent re-read before moving money: the
            # listing-created-index feeding this loop is eventually
            # consistent, and Stripe's idempotency keys expire after 24h
            # while webhook retries run for days — a stale "untransferred"
            # row here plus an expired key would double-pay.
            fresh = (
                await marketplace_repo.get_purchase(purchase["buyer_id"], purchase["purchase_id"], consistent=True)
                or {}
            )
            processed.add(purchase["purchase_id"])
            await settle_one(fresh or purchase)


@router.post("/refund")
async def refund(
    purchase_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """7-day refund window for buyers."""
    require_org_admin(auth)
    purchase = await marketplace_repo.get_purchase(resolve_owner_id(auth), purchase_id)
    if not purchase:
        raise HTTPException(status_code=404, detail="purchase not found")
    created_iso = purchase["created_at"]
    # created_at is stored UTC ("...Z"); calendar.timegm reads the struct as
    # UTC. time.mktime would read it as LOCAL time, skewing the window by the
    # host's UTC offset on any non-UTC host.
    age_seconds = time.time() - calendar.timegm(time.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ"))
    if age_seconds > 7 * 24 * 60 * 60:
        raise HTTPException(status_code=403, detail="refund window expired (7 days)")

    result = await payout_service.refund_purchase(
        payment_intent_id=purchase["stripe_payment_intent_id"],
        transfer_group=purchase.get("stripe_transfer_group", ""),
        full_amount_cents=purchase["price_paid_cents"],
    )
    await license_service.revoke(purchase_id=purchase_id, buyer_id=resolve_owner_id(auth), reason="refunded")
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
    raws = await marketplace_repo.list_purchases_by_buyer(resolve_owner_id(auth))
    raws.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    raws = raws[:100]

    items: list[schemas.PurchaseSummary] = []
    slug_cache: dict[str, str] = {}
    for raw in raws:
        listing_id = raw.get("listing_id", "")
        if listing_id and listing_id not in slug_cache:
            # Use the listing_version_at_purchase row if available, otherwise
            # fall back to v=1. Avoids 404s on multi-version listings where
            # v=1 was retired before the buyer reads their history.
            v_at_purchase = int(raw.get("listing_version_at_purchase", 1)) or 1
            li = await marketplace_repo.get_listing(listing_id, v_at_purchase)
            if not li:
                li = await marketplace_repo.get_listing(listing_id, 1)
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
