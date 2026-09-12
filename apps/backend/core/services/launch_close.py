"""The admin close action for the competition sign-up sheet, plus the
checkout-side coupon pre-apply for unpaid winners.

``close()`` runs the Sept-30 sequence: stamp ``closed_at`` (which freezes
joins and referral jumps — the phase leaves ``open``), flag the top
``comp_slots`` sheet rows by effective position as winners, mark
``compete_eligible`` (winners ∪ paying subscribers; unpaid non-winners are
out), freeze the pot (``pot_front + per_entrant × roster``) onto the settings
row, ensure the ONE Stripe free-month coupon exists and attach it to every
PAYING winner's subscription, and email EVERYONE on the sheet — in their own
words: winners (paid/unpaid coupon variants), paying non-winners (still in
the competition, just without the free month), and everyone else (out of the
competition, keeps the product).

Every step is idempotent and stamps per-row flags (``winner``,
``compete_eligible``, ``coupon_attached_at``, ``winner_email_sent_at``), so a
rerun after a partial failure (``force=True`` past the already-closed guard)
converges: done work is skipped, missed work is retried.
"""

from __future__ import annotations

import asyncio
import logging

import stripe

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import billing_repo, launch_settings_repo, launch_waitlist_repo, user_repo
from core.services import launch_phase, launch_service, launch_settings
from core.services.launch_sheet_email import (
    send_not_selected_email,
    send_still_competing_email,
    send_winner_paid_email,
    send_winner_unpaid_email,
)
from core.services.provision_gate import is_paid_subscription

logger = logging.getLogger(__name__)


class CloseError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AlreadyClosed(Exception):
    """``closed_at`` is already stamped and the caller didn't pass ``force``."""


class CloseRunning(Exception):
    """A close background task was started and has not written its result yet
    (``close_started_at`` set, ``close_result`` unset) — refuse a second
    start unless ``force`` (which is also how a crashed task is recovered)."""


def _discount_coupon_id(d) -> str | None:
    if not isinstance(d, dict):
        return None
    c = d.get("coupon")
    return c.get("id") if isinstance(c, dict) else c


async def _attach_coupon(billing: dict, coupon: str) -> str:
    """Attach the free-month coupon to a paying winner's live subscription.

    Returns one of:
      - ``"attached"`` — ours is on the subscription (attached now, or
        already there); the caller stamps the row.
      - ``"no_sub"`` — no ``stripe_subscription_id`` on the billing row
        (Apple-rail/legacy shape); a visible failure, retried on force.
      - ``"nonflat"`` — the subscription does not carry the Workspace
        price, so the product-restricted coupon would apply NOTHING (Stripe
        attaches it without erroring and discounts $0). Skipped: the winner
        keeps the unpaid-style entitlement instead of a lie.

    An UNRELATED existing discount (e.g. a legacy referral free month) does
    not count as done: ours is added ALONGSIDE it (Stripe's discounts array
    stacks), or the winner's month would be silently eaten. A Stripe failure
    propagates so the row is NOT stamped and a rerun retries."""
    sub_id = billing.get("stripe_subscription_id")
    if not sub_id:
        return "no_sub"
    sub = await run_in_thread(stripe.Subscription.retrieve, sub_id, expand=["discounts"])
    from core.services.billing_service import subscription_item_on_price

    items = (sub.get("items") or {}).get("data") or []
    if not subscription_item_on_price(items, settings.STRIPE_WORKSPACE_PRICE_ID):
        return "nonflat"
    existing = list(sub.get("discounts") or [])
    if not existing and sub.get("discount"):
        existing = [sub["discount"]]  # legacy single-discount shape
    if any(_discount_coupon_id(d) == coupon for d in existing):
        return "attached"
    keep = [{"discount": d["id"] if isinstance(d, dict) else d} for d in existing]
    await run_in_thread(
        stripe.Subscription.modify,
        sub_id,
        discounts=[*keep, {"coupon": coupon}],
        # Stable per subscription: however many times the close is rerun, one
        # subscription gets the coupon attached exactly once.
        idempotency_key=f"launch-close-coupon:{sub_id}",
    )
    return "attached"


async def _ensure_restricted_coupon(auth: AuthContext) -> str:
    """The free-month coupon id, guaranteed to carry the applies_to product
    restriction. A stored coupon WITHOUT it is a stale v1 mint (dev/prod
    settings rows can carry one) — attaching or pre-applying it would zero
    the bundled credits line item, so it is replaced with a fresh restricted
    mint (``force=True``: the nonce bumps and the product id rides the
    idempotency key, so Stripe can never replay the old coupon). Stripe
    verification failures surface as StripeMintError (router → 502)."""
    coupon = launch_phase.coupon_id()
    if coupon:
        try:
            stored = await run_in_thread(stripe.Coupon.retrieve, coupon, expand=["applies_to"])
            price = await run_in_thread(stripe.Price.retrieve, settings.STRIPE_WORKSPACE_PRICE_ID)
        except stripe.StripeError as e:
            logger.error("launch_close: coupon verification failed for %s: %s", coupon, e)
            raise launch_settings.StripeMintError() from None
        # The restriction must name the CURRENT flat product — merely being
        # restricted to something isn't enough (a rotated flat price, or a
        # coupon minted against another env's product, discounts nothing).
        if price["product"] in ((stored.get("applies_to") or {}).get("products") or []):
            return coupon
        logger.warning(
            "launch_close: stored coupon %s does not apply to the flat product — re-minting restricted", coupon
        )
    await launch_settings.create_stripe_coupon(auth, force=bool(coupon))
    return (await launch_settings.effective())["stripe_coupon_id"]


async def mark_winner_coupon_consumed(owner_id: str) -> None:
    """Called by handle_checkout_session when a completed session carried the
    winner discount (the ``launch_winner_coupon`` metadata marker): stamp
    ``coupon_attached_at`` on the sheet row so a forced close rerun does not
    attach a SECOND free month to the subscription the checkout just minted.
    Best-effort — never raises on the webhook path."""
    try:
        user = await user_repo.get(owner_id)
        email = ((user or {}).get("email") or "").strip().lower()
        if not email:
            return
        await launch_waitlist_repo.update_fields(email, {"coupon_attached_at": utc_now_iso()})
    except Exception:  # noqa: BLE001 — a launch stamp must never fail the billing webhook
        logger.warning("launch_close: coupon-consumed stamp failed for %s", owner_id, exc_info=True)


def _is_running(row: dict) -> bool:
    return bool(row.get("close_started_at")) and row.get("close_result") is None


async def start_close(auth: AuthContext, *, force: bool = False) -> None:
    """Run the close as a background task — 200 winners means ~400 sequential
    Stripe calls plus 200 SES sends, far past the API gateway's 30s timeout,
    so the router returns 202 and the admin sheet polls ``close_result``.

    Refusals happen HERE, synchronously (an exception inside the task would
    vanish): already-closed and already-running both 409 unless ``force``.
    ``close_started_at`` is stamped before the task starts; the finished
    task's summary (or its error) lands on the settings row as
    ``close_result``. A crash that writes neither is recovered by a force
    rerun, exactly like any partial failure — the per-row stamps make the
    sequence convergent."""
    row = await launch_settings.effective()
    if row.get("closed_at") and not force:
        raise AlreadyClosed()
    if _is_running(row) and not force:
        raise CloseRunning()
    if launch_phase.schedule() is None:
        raise CloseError("launch_disabled")
    await launch_settings.record_close_state({"close_started_at": utc_now_iso(), "close_result": None})
    asyncio.create_task(_run_close(auth, force=force))


async def _run_close(auth: AuthContext, *, force: bool) -> None:
    try:
        result = await close(auth, force=force)
    except CloseError as e:
        result = {"error": e.reason}
    except launch_settings.StripeMintError:
        result = {"error": "stripe_error"}
    except launch_settings.SettingsInvalid:
        result = {"error": "settings_invalid"}
    except Exception:  # noqa: BLE001 — the summary write below is the only observer left
        logger.exception("launch_close: background close failed")
        result = {"error": "close_failed"}
    try:
        await launch_settings.record_close_state({"close_result": result})
    except Exception:  # noqa: BLE001 — leaves close_running true; the admin recovers with force
        logger.exception("launch_close: failed to record close_result")


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

    # Consistent scan: the close must see every committed join — an
    # eventually-consistent page dropping a row would deterministically
    # exclude a legitimate winner. (The 30s settings-cache instance race
    # stays accepted; this is about the roster itself.)
    sheet = [r for r in await launch_waitlist_repo.list_all(consistent=True) if r.get("joined_at")]
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
        # Eligibility is NOT recomputed on a rerun either — the stamp the
        # first close wrote is the truth, so a subscription started after the
        # close can't flip a "not this round" row into a "you're competing"
        # email that contradicts its own compete_eligible flag.
        eligible_emails = {r["email"] for r in sheet if r.get("compete_eligible")}
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

        # (d) Freeze the pot: front + per-entrant × final roster + one
        # (per-entrant + per-seat × seats) block per counted TEAM entry. Write-once
        # at the DB level — a concurrent recovery run losing the condition
        # keeps the first run's frozen values. The condition also accepts a
        # legacy NULL `final_pot_cents` (see POT_UNFROZEN), or a row saved by
        # the admin settings form before that fix would never freeze at all.
        eligible_emails = {r["email"] for r in sheet if r["email"] in winner_emails or r["email"] in paying}
        roster_count = len(eligible_emails)
        # Teams never reach the sheet (POST /launch/join refuses an org
        # context), so they are not in `roster_count` — they are folded into
        # the pot separately, and ONLY the ones the founders marked counted.
        team_seats = await launch_service.counted_team_seats()
        final_pot = launch_phase.pot_cents(roster_count, team_seats)
        froze = await launch_settings.record_close_state(
            {"final_roster_count": roster_count, "final_pot_cents": final_pot},
            condition=launch_settings_repo.POT_UNFROZEN,
            condition_values=launch_settings_repo.POT_UNFROZEN_VALUES,
        )
        if not froze:
            # The row was frozen by someone else between this run's read and
            # this write. Legitimate under a concurrent recovery run — but it
            # is also how the NULL-attribute defect hid, so it is never silent
            # again, and the summary reports what is actually ON the row.
            frozen = await launch_settings.effective()
            logger.warning(
                "launch_close: pot freeze discarded — the row already carries "
                "final_pot_cents=%r final_roster_count=%r; this run computed %r/%r",
                frozen.get("final_pot_cents"),
                frozen.get("final_roster_count"),
                final_pot,
                roster_count,
            )
            roster_count = frozen.get("final_roster_count")
            final_pot = frozen.get("final_pot_cents")

    winner_emails = {r["email"] for r in winners}

    # (e) The coupon: ensure a PRODUCT-RESTRICTED one exists (mint if
    # missing; force a re-mint over a stale coupon that predates the
    # applies_to restriction), then attach to
    # every PAYING winner's subscription. Attach failures are logged and
    # counted, never fatal — rerun with force to converge.
    coupon = await _ensure_restricted_coupon(auth)
    coupons_attached = 0
    coupon_failures = 0
    coupon_skipped_nonflat = 0
    nonflat_winners: set[str] = set()
    attach_blocked: set[str] = set()
    for r in winners:
        billing = paying.get(r["email"])
        if not billing or r.get("coupon_attached_at"):
            continue
        try:
            outcome = await _attach_coupon(billing, coupon)
        except Exception:  # noqa: BLE001 — one bad subscription must not abort the rest of the close
            coupon_failures += 1
            attach_blocked.add(r["email"])
            logger.exception("launch_close: coupon attach failed for %s", r.get("user_id"))
            continue
        if outcome == "attached":
            await launch_waitlist_repo.update_fields(r["email"], {"coupon_attached_at": utc_now_iso()})
            coupons_attached += 1
        elif outcome == "nonflat":
            # A paying winner NOT on the flat plan (Ultra): the restricted
            # coupon would discount $0 — attaching + stamping would be a
            # lie ("next invoice is $0"). Skip; they keep the winner flag,
            # get the unpaid-variant email below, and a future Workspace
            # checkout pre-applies the discount.
            coupon_skipped_nonflat += 1
            nonflat_winners.add(r["email"])
            logger.info(
                "launch_close: paying winner %s is not on the flat plan — coupon not attached "
                "(free month = Workspace month)",
                r.get("user_id"),
            )
        else:  # "no_sub"
            # A "paying" winner with no stripe_subscription_id on the row
            # (Apple-rail or legacy shape) — nothing to attach to. Count it
            # so the admin SEES it; the row stays unstamped, so a forced
            # rerun retries once the sub id exists.
            coupon_failures += 1
            attach_blocked.add(r["email"])
            logger.error(
                "launch_close: paying winner %s has no stripe_subscription_id — coupon not attached",
                r.get("user_id"),
            )

    # (f) Everyone on the sheet hears, in the words their outcome earns:
    # winners get the coupon copy (paid vs unpaid), paying non-winners are
    # told they are still competing (they are — compete_eligible stayed
    # true, they just pay their own way), and the rest are told the round
    # passed them by. Stamped per row only on a confirmed send so a rerun
    # retries the misses.
    sends = {"winner": 0, "competing": 0, "not_selected": 0}
    email_failures = 0
    # Admin copy overrides, read ONCE (cached on the settings row) rather
    # than per send.
    copy = launch_phase.email_copy()
    for r in sheet:
        if r.get("winner_email_sent_at"):
            continue
        if r["email"] in attach_blocked:
            # A winner whose attach FAILED (no sub id, or a Stripe error) —
            # neither winner variant is true for them yet. No email this run;
            # the row is unstamped on both counts, so a force rerun after the
            # attach problem is fixed sends the right one.
            continue
        # CLAIM-FIRST (at-most-once): stamp the row before sending, so a
        # concurrent run can never double-send; a failed send releases the
        # claim below and counts as a failure, so a force rerun retries. ONE
        # stamp for every variant (the winner-named field predates them), so
        # a row emailed once is never emailed again as something else.
        if not await launch_waitlist_repo.claim_winner_email(r["email"]):
            continue  # another run owns this row's email
        if r["email"] in winner_emails:
            # Paid variant only when the coupon actually lands on their sub —
            # a nonflat paying winner gets the unpaid variant, whose copy
            # ("first month of Workspace is free, applied automatically at
            # checkout") is literally accurate for them.
            is_paid_variant = r["email"] in paying and r["email"] not in nonflat_winners
            send = send_winner_paid_email if is_paid_variant else send_winner_unpaid_email
            variant, extra = "winner", {}
        elif r["email"] in eligible_emails:
            # len(winners), NOT s.comp_slots: the winner set is frozen at the
            # first close, so a comp_slots edit before a convergence rerun
            # would otherwise quote a not-yet-emailed row a number the data no
            # longer supports. The count of free months actually given is
            # exact and immune to later edits.
            variant, send, extra = "competing", send_still_competing_email, {"comp_slots": len(winners)}
        else:
            variant, send, extra = "not_selected", send_not_selected_email, {"comp_slots": len(winners)}
        message_id = await send(to_email=r["email"], first_name=None, copy=copy, **extra)
        if message_id:
            sends[variant] += 1
        else:
            email_failures += 1
            try:
                await launch_waitlist_repo.release_winner_email(r["email"])
            except Exception:  # noqa: BLE001 — a stuck claim means this row is never emailed; reconcile by hand
                logger.exception("launch_close: failed to release close-email claim for %s", r.get("user_id"))

    summary = {
        "closed_at": (await launch_settings.effective())["closed_at"],
        "sheet_count": len(sheet),
        "winners": len(winners),
        "paying": len(paying),
        "final_roster_count": roster_count,
        "final_pot_cents": final_pot,
        "coupons_attached": coupons_attached,
        "coupon_failures": coupon_failures,
        "coupon_skipped_nonflat": coupon_skipped_nonflat,
        "emails_sent": sum(sends.values()),
        "emails_winner": sends["winner"],
        "emails_competing": sends["competing"],
        "emails_not_selected": sends["not_selected"],
        "email_failures": email_failures,
    }
    logger.info("launch_close: %s", summary)
    return summary
