"""Referral free months, paid out from Stripe ``invoice.paid``.

Rule (spec): the referrer earns a free month when the referred person's FIRST
PAID invoice lands. The once-only guard is ``launch_waitlist_repo.mark_rewarded``
on the referred row (a conditional write). The grant is a 100%-off-once coupon
applied to the referrer's subscription; when it cannot be applied it is banked
on the referrer's row and drained one per ``invoice.paid`` of their own.

Personal owners only. Never raises — this runs inside the webhook handler.
"""

from __future__ import annotations

import hashlib
import logging

import stripe

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.product_analytics import capture_product_event
from core.repositories import billing_repo, launch_waitlist_repo, user_repo
from core.services import launch_phase

logger = logging.getLogger(__name__)


def subscription_has_discount(sub: dict) -> bool:
    return bool(sub.get("discount")) or bool(sub.get("discounts"))


async def grant_free_month(owner_id: str, *, idempotency_key: str) -> bool:
    """Apply the free-month coupon to the owner's live subscription.
    False ⇒ caller must bank it (no sub, already discounted, or no coupon)."""
    coupon = settings.STRIPE_FREE_MONTH_COUPON_ID
    if not coupon:
        return False
    billing = await billing_repo.get_by_owner_id(owner_id)
    sub_id = (billing or {}).get("stripe_subscription_id")
    if not sub_id or (billing or {}).get("subscription_status") not in ("active", "trialing"):
        return False
    sub = await run_in_thread(stripe.Subscription.retrieve, sub_id)
    if subscription_has_discount(sub):
        return False
    try:
        await run_in_thread(
            stripe.Subscription.modify, sub_id, discounts=[{"coupon": coupon}], idempotency_key=idempotency_key
        )
    except stripe.InvalidRequestError as e:
        # Older API versions reject `discounts` on subscription update; `coupon` is the legacy spelling.
        if "discounts" not in str(e):
            raise
        await run_in_thread(
            stripe.Subscription.modify, sub_id, coupon=coupon, idempotency_key=f"{idempotency_key}:legacy"
        )
    capture_product_event(
        distinct_id=owner_id, event="launch_free_month_applied", properties={"subscription_id": sub_id}
    )
    return True


async def _email_for_owner(owner_id: str) -> str | None:
    row = await user_repo.get(owner_id)
    email = (row or {}).get("email")
    return email.strip().lower() if email else None


async def _safe_counter(
    email: str,
    field: launch_waitlist_repo.CounterField,
    delta: int,
    *,
    code: str | None = None,
    owner_id: str | None = None,
) -> None:
    """A counter write that carries money. Retried once; a second failure is logged
    at ERROR so it can be reconciled by hand — identified by referral code and
    owner id, NEVER by the email."""
    for attempt in (1, 2):
        try:
            await launch_waitlist_repo.add_counter(email, field, delta)
            return
        except Exception:  # noqa: BLE001 — losing a month is worse than a noisy log line
            if attempt == 2:
                logger.error(
                    "launch_referral: counter write failed twice — reconcile by hand "
                    "(field=%s delta=%s referral_code=%s owner_id=%s)",
                    field,
                    delta,
                    code,
                    owner_id,
                    exc_info=True,
                )


async def _reward_referrer(payer_email: str, payer_row: dict, invoice_id: str) -> None:
    code = payer_row.get("referred_by")
    if not code:
        return
    if not await launch_waitlist_repo.mark_rewarded(payer_email):
        return  # already paid out for this referred person
    # Past this line the once-only credit is spent. NOTHING may raise before the
    # bank decision below, or the referrer silently loses their free month.
    referrer: dict | None = None
    referrer_owner: str | None = None
    applied = False
    try:
        referrer = await launch_waitlist_repo.get_by_referral_code(code)
        if referrer:
            referrer_owner = referrer.get("user_id")
            if referrer_owner:
                billing = await billing_repo.get_by_owner_id(referrer_owner)
                sub_id = (billing or {}).get("stripe_subscription_id")
                applied = await grant_free_month(
                    referrer_owner, idempotency_key=f"referral_reward:{sub_id}:{invoice_id}"
                )
    except Exception:  # noqa: BLE001 — any failure after the CAS banks the month instead
        logger.warning("launch_referral: referral grant failed after the CAS", exc_info=True)
        applied = False
    if referrer is None:
        # The referrer row could not be resolved, so there is no row to credit.
        logger.error("launch_referral: no referrer row for referral_code=%s after the CAS", code)
        return
    if not applied:
        await _safe_counter(referrer["email"], "free_months_banked", 1, code=code, owner_id=referrer_owner)
    await _safe_counter(referrer["email"], "referrals_paid", 1, code=code, owner_id=referrer_owner)
    capture_product_event(
        distinct_id=referrer_owner or None,
        event="launch_referral_rewarded",
        properties={
            "applied": applied,
            "referred_owner_email_hash": hashlib.sha256(payer_email.encode()).hexdigest()[:16],
        },
    )


async def _drain_banked(payer_owner: str, payer_email: str, payer_row: dict, invoice_id: str) -> None:
    if int(payer_row.get("free_months_banked", 0)) <= 0:
        return
    remaining = await launch_waitlist_repo.add_counter(payer_email, "free_months_banked", -1)
    if remaining is None:
        return  # the decrement was refused — there is no month to spend, so nothing to put back
    sub_id = ((await billing_repo.get_by_owner_id(payer_owner)) or {}).get("stripe_subscription_id")
    try:
        applied = await grant_free_month(payer_owner, idempotency_key=f"banked_month:{sub_id}:{invoice_id}")
    except Exception:  # noqa: BLE001 — a Stripe failure after the decrement must still put the month back
        logger.warning("launch_referral: grant_free_month failed while draining banked month", exc_info=True)
        applied = False
    if applied:
        return
    await _safe_counter(
        payer_email, "free_months_banked", 1, code=payer_row.get("referral_code"), owner_id=payer_owner
    )  # put it back
    logger.info("launch: banked month not applicable yet for %s (remaining=%s)", payer_owner, remaining + 1)


async def on_invoice_paid(invoice: dict) -> None:
    try:
        # Inside the try on purpose: schedule() raises on a malformed
        # launch-schedule.json, and this handler's contract is that a launch
        # bug never fails the billing webhook. Fail-loud lives at synth
        # (service-stack.ts validateLaunchSchedule) and at the poll door
        # (launch_hold.pre_drop_gate), not here.
        if launch_phase.schedule() is None:
            return  # the competition is not configured — the webhook does no launch reads at all
        customer = invoice.get("customer")
        if not customer:
            return
        rows = await billing_repo.list_by_stripe_customer_id(customer)
        if not rows:
            return
        sub_id = invoice.get("subscription")
        matches = [r for r in rows if r.get("stripe_subscription_id") == sub_id] if sub_id else rows
        owner_id = (matches or rows)[0]["owner_id"]
        if owner_id.startswith("org_"):
            return
        payer_email = await _email_for_owner(owner_id)
        if not payer_email:
            return
        # The funded pot counts the FIRST Stripe invoice of ANY amount (redemption
        # counts, not just paid amounts) — CAS makes duplicate invoices inert. This
        # must never abort the referral reward path below, so it gets its own try/except.
        try:
            await launch_waitlist_repo.mark_pot_counted(payer_email)
        except Exception:  # noqa: BLE001
            logger.warning("launch_referral: mark_pot_counted failed for owner=%s", owner_id, exc_info=True)
        if int(invoice.get("amount_paid") or 0) <= 0:
            return
        payer_row = await launch_waitlist_repo.get(payer_email)
        if not payer_row:
            return
        invoice_id = invoice.get("id") or "unknown"
        await _reward_referrer(payer_email, payer_row, invoice_id)
        await _drain_banked(owner_id, payer_email, payer_row, invoice_id)
    except Exception:  # noqa: BLE001 — a launch bug must never fail the billing webhook
        logger.warning("launch_referral.on_invoice_paid failed", exc_info=True)
