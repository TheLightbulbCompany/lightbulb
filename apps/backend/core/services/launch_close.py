"""The admin close action for the competition sign-up sheet, plus the
checkout-side coupon pre-apply for unpaid winners.

``close()`` runs the Sept-30 sequence: stamp ``closed_at`` (which freezes
joins and referral jumps — the phase leaves ``open``), flag the top
``comp_slots`` sheet rows by effective position as winners, mark
``compete_eligible`` (winners ∪ paying subscribers; unpaid non-winners are
out), freeze the pot (``pot_front + per_entrant × roster``) onto the settings
row, ensure the ONE Stripe free-month coupon exists and attach it to every
PAYING winner's subscription, and email every winner (paid/unpaid variants).

Every step is idempotent and stamps per-row flags (``winner``,
``compete_eligible``, ``coupon_attached_at``, ``winner_email_sent_at``), so a
rerun after a partial failure (``force=True`` past the already-closed guard)
converges: done work is skipped, missed work is retried.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe

from core.auth import AuthContext
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import billing_repo, launch_waitlist_repo, user_repo
from core.services import launch_phase, launch_service, launch_settings
from core.services.launch_sheet_email import send_winner_paid_email, send_winner_unpaid_email
from core.services.provision_gate import is_paid_subscription

logger = logging.getLogger(__name__)


class CloseError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AlreadyClosed(Exception):
    """``closed_at`` is already stamped and the caller didn't pass ``force``."""


def _discount_coupon_id(d) -> str | None:
    if not isinstance(d, dict):
        return None
    c = d.get("coupon")
    return c.get("id") if isinstance(c, dict) else c


async def _attach_coupon(billing: dict, coupon: str) -> bool:
    """Attach the free-month coupon to a paying winner's live subscription.
    True = ours is on the subscription (attached now, or already there) —
    the caller stamps the row. An UNRELATED existing discount (e.g. a legacy
    referral free month) does not count as done: ours is added ALONGSIDE it
    (Stripe's discounts array stacks), or the winner's month would be
    silently eaten by whatever was already there. A Stripe failure
    propagates so the row is NOT stamped and a rerun retries."""
    sub_id = billing.get("stripe_subscription_id")
    if not sub_id:
        return False
    sub = await run_in_thread(stripe.Subscription.retrieve, sub_id, expand=["discounts"])
    existing = list(sub.get("discounts") or [])
    if not existing and sub.get("discount"):
        existing = [sub["discount"]]  # legacy single-discount shape
    if any(_discount_coupon_id(d) == coupon for d in existing):
        return True
    keep = [{"discount": d["id"] if isinstance(d, dict) else d} for d in existing]
    await run_in_thread(
        stripe.Subscription.modify,
        sub_id,
        discounts=[*keep, {"coupon": coupon}],
        # Stable per subscription: however many times the close is rerun, one
        # subscription gets the coupon attached exactly once.
        idempotency_key=f"launch-close-coupon:{sub_id}",
    )
    return True


async def close(auth: AuthContext, *, force: bool = False) -> dict:
    row = await launch_settings.effective()
    already_closed = bool(row.get("closed_at"))
    if already_closed and not force:
        raise AlreadyClosed()
    s = launch_phase.schedule()
    if s is None:
        raise CloseError("launch_disabled")

    # (a) Stamp closed_at FIRST — the phase leaves "open" immediately (the
    # settings caches are cleared by the write), so joins refuse and referral
    # jumps freeze before the roster below is ordered.
    if not already_closed:
        await launch_settings.record_close_state({"closed_at": utc_now_iso()})

    sheet = [r for r in await launch_waitlist_repo.list_all() if r.get("joined_at")]
    jump = launch_phase.constants()["referral_jump"]

    paying: dict[str, dict] = {}
    # final_pot_cents is stamped strictly AFTER winner flagging completes, so
    # a None here means the (b)-(d) sequence from the last attempt never
    # finished (a crash, possibly mid-loop with a partial winner set). In
    # that case the recovery path is simply to run the full sequence again —
    # it is deterministic post-close (closed_at freezes joins and referral
    # counting the moment it stamps). `is not None`: pot_front_cents=0 with
    # zero entrants legitimately freezes the pot at 0.
    if already_closed and row.get("final_pot_cents") is not None:
        # Force rerun = CONVERGENCE ONLY. The winner set and the frozen pot
        # are immutable after the first close — a later comp_slots edit or a
        # billing change must never reshuffle who won or what the pot froze
        # at. Only (e) coupon attaches and (f) emails are retried, off the
        # per-row stamps; billing is read for winners only (variant + sub id).
        winners = [r for r in sheet if r.get("winner")]
        for r in winners:
            uid = r.get("user_id")
            billing = (await billing_repo.get_by_owner_id(uid)) if uid else None
            if is_paid_subscription(billing):
                paying[r["email"]] = billing or {}
        roster_count = row.get("final_roster_count")
        final_pot = row.get("final_pot_cents")
    else:
        # (b) Winners: the top comp_slots by effective position, ties by raw
        # position (earlier signup wins). Jumps are frozen by (a).
        ordered = sorted(sheet, key=lambda r: (launch_service.effective_position(r, jump), int(r["position"])))
        winners = ordered[: s.comp_slots]
        winner_emails = {r["email"] for r in winners}
        for r in winners:
            await launch_waitlist_repo.update_fields(r["email"], {"winner": True})

        # (c) Eligibility: winners ∪ paying subscribers compete; unpaid
        # non-winners are out. "Paying" is a real Stripe subscription
        # (is_paid_subscription — trial_local is not paying).
        for r in sheet:
            uid = r.get("user_id")
            billing = (await billing_repo.get_by_owner_id(uid)) if uid else None
            if is_paid_subscription(billing):
                paying[r["email"]] = billing or {}
            eligible = r["email"] in winner_emails or r["email"] in paying
            await launch_waitlist_repo.update_fields(r["email"], {"compete_eligible": eligible})

        # (d) Freeze the pot: front + per-entrant × final roster. First write
        # only — these values are immutable once stamped.
        roster_count = sum(1 for r in sheet if r["email"] in winner_emails or r["email"] in paying)
        c = launch_phase.constants()
        final_pot = c["pot_front_cents"] + c["pot_per_entrant_cents"] * roster_count
        await launch_settings.record_close_state({"final_roster_count": roster_count, "final_pot_cents": final_pot})

    # (e) The coupon: mint if missing (idempotent — see create_stripe_coupon),
    # then attach to every PAYING winner's subscription. Attach failures are
    # logged and counted, never fatal — rerun with force to converge.
    coupon = launch_phase.coupon_id()
    if not coupon:
        await launch_settings.create_stripe_coupon(auth)
        coupon = (await launch_settings.effective())["stripe_coupon_id"]
    coupons_attached = 0
    coupon_failures = 0
    for r in winners:
        billing = paying.get(r["email"])
        if not billing or r.get("coupon_attached_at"):
            continue
        try:
            if await _attach_coupon(billing, coupon):
                await launch_waitlist_repo.update_fields(r["email"], {"coupon_attached_at": utc_now_iso()})
                coupons_attached += 1
        except Exception:  # noqa: BLE001 — one bad subscription must not abort the rest of the close
            coupon_failures += 1
            logger.exception("launch_close: coupon attach failed for %s", r.get("user_id"))

    # (f) Winner emails — paid vs unpaid variant, stamped per row only on a
    # confirmed send so a rerun retries the misses.
    emails_sent = 0
    email_failures = 0
    for r in winners:
        if r.get("winner_email_sent_at"):
            continue
        send = send_winner_paid_email if r["email"] in paying else send_winner_unpaid_email
        message_id = await send(to_email=r["email"], first_name=None)
        if message_id:
            await launch_waitlist_repo.update_fields(r["email"], {"winner_email_sent_at": utc_now_iso()})
            emails_sent += 1
        else:
            email_failures += 1

    summary = {
        "closed_at": (await launch_settings.effective())["closed_at"],
        "sheet_count": len(sheet),
        "winners": len(winners),
        "paying": len(paying),
        "final_roster_count": roster_count,
        "final_pot_cents": final_pot,
        "coupons_attached": coupons_attached,
        "coupon_failures": coupon_failures,
        "emails_sent": emails_sent,
        "email_failures": email_failures,
    }
    logger.info("launch_close: %s", summary)
    return summary


async def winner_checkout_discount(owner_id: str) -> list[dict] | None:
    """``[{"coupon": id}]`` when this owner's sheet row is a winner whose free
    month hasn't been attached yet — pre-applied by
    ``billing_service._build_subscription_checkout`` so an unpaid winner's
    checkout is $0 with no code to type. None otherwise. Never raises — a
    launch read must never break checkout."""
    try:
        if launch_phase.schedule() is None:
            return None
        if launch_settings.COUPON_REDEEM_BY <= datetime.now(timezone.utc):
            # An expired coupon on a Checkout Session is a Stripe
            # InvalidRequestError at Session.create — a winner subscribing
            # after the redeem-by instant gets a normal full-price checkout.
            return None
        coupon = launch_phase.coupon_id()
        if not coupon:
            return None
        user = await user_repo.get(owner_id)
        email = ((user or {}).get("email") or "").strip().lower()
        if not email:
            return None
        row = await launch_waitlist_repo.get(email)
        if row and row.get("winner") and not row.get("coupon_attached_at"):
            return [{"coupon": coupon}]
    except Exception:  # noqa: BLE001 — never let a launch read break checkout
        logger.warning("launch_close: winner discount lookup failed for %s", owner_id, exc_info=True)
    return None
