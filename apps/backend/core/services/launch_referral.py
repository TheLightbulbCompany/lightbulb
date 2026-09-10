"""Referral counting + credit milestones, driven from subscription activation.

Rule (pivot spec): a referral COUNTS when the referred person's subscription
goes live for the first time — ``handle_subscription_state``'s entering-live
transition calls ``on_subscription_activated``. The once-only guard is
``launch_waitlist_repo.mark_referral_counted`` on the referee's row (a CAS),
so Stripe's unordered/duplicate deliveries and later re-subscribes are inert.
Counting freezes once the sheet is no longer ``open`` (the close action
stamps ``closed_at``); ``referred_by`` matters for nothing else.

Milestones, stamped on the REFERRER's row (``milestone_5_credits`` /
``milestone_20_credits``: "granted" | "pending_grant"): 5 counted referrals →
$50 of Bedrock credits, 20 → $200 more ($250 total). A grant replicates the
promotional-grant two-ledger sequence — ``grant_prepaid_credits`` (Metronome,
idempotent on a stable key) then the restore-only fast-gate refresh
(``read_net_balance_microcents`` + ``apply_fresh_balance``). A referrer with
no Metronome ledger yet (trial-only, or an unclaimed row) is stamped
``pending_grant`` and re-attempted when THEY subscribe — and on every later
counted referral, since ``check_milestones`` re-attempts any pending stamp.

Personal owners only. Never raises — this runs inside the webhook handler.
"""

from __future__ import annotations

import logging

from core.observability.product_analytics import capture_product_event
from core.repositories import launch_waitlist_repo, user_repo
from core.services import launch_phase

logger = logging.getLogger(__name__)

# (counted-referral threshold, credit grant in cents). The 20-milestone is a
# TOP-UP: $50 at 5 plus $200 at 20 = $250 total.
MILESTONES = ((5, 5_000), (20, 20_000))


def _milestone_field(threshold: int) -> str:
    return f"milestone_{threshold}_credits"


async def _email_for_owner(owner_id: str) -> str | None:
    """The users-table email, falling back to Clerk.

    The row is the fast path but is NOT guaranteed to carry an email: it is
    written email-less whenever /users/sync's heal fails, and every row
    created while the Clerk `user.created` webhook pointed at the retired
    domain has none at all. This runs inside a Stripe webhook, so there is no
    JWT to fall back on — and a missing email here drops the referral count
    for good, because ``mark_referral_counted`` is once-only and this is the
    single moment it fires. A Clerk round trip is cheap next to that.
    """
    row = await user_repo.get(owner_id)
    email = (row or {}).get("email")
    if email:
        return email.strip().lower()
    from core.services import clerk_admin

    try:
        return (await clerk_admin.get_primary_email(owner_id)).strip().lower()
    except Exception as e:  # noqa: BLE001 — no email anywhere; log and skip counting
        logger.warning("launch_referral: no email for %s in users or Clerk: %s", owner_id, e)
        return None


async def _grant_credits(owner_id: str, amount_cents: int, idempotency_key: str) -> bool:
    """The shared two-ledger grant sequence (see
    ``metronome_funding.grant_prepaid_credits_and_refresh_gate``). False ⇒
    the owner has no Metronome ledger yet (FundingError) — the caller stamps
    ``pending_grant``. Any other failure propagates so nothing is stamped and
    the next event retries."""
    from core.billing.metronome_funding import FundingError, grant_prepaid_credits_and_refresh_gate

    try:
        await grant_prepaid_credits_and_refresh_gate(owner_id, amount_cents, idempotency_key=idempotency_key)
    except FundingError:
        return False
    return True


async def check_milestones(referrer_row: dict) -> None:
    """Grant every milestone the referrer's counted total has reached and not
    yet been granted. Idempotent: "granted" stamps are final, the Metronome
    idempotency key is a pure function of (milestone, owner), and a
    ``pending_grant`` stamp is re-attempted on every call."""
    email = referrer_row["email"]
    owner_id = referrer_row.get("user_id")
    count = int(referrer_row.get("referrals_subscribed", 0))
    for threshold, cents in MILESTONES:
        if count < threshold:
            continue
        field = _milestone_field(threshold)
        if referrer_row.get(field) == "granted":
            continue
        granted = False
        if owner_id:
            granted = await _grant_credits(owner_id, cents, f"launch-milestone-{threshold}:{owner_id}")
        await launch_waitlist_repo.update_fields(email, {field: "granted" if granted else "pending_grant"})
        capture_product_event(
            distinct_id=owner_id or None,
            event="launch_milestone_credits",
            properties={"milestone": threshold, "amount_cents": cents, "granted": granted},
        )
        if granted:
            logger.info("launch_referral: milestone %s granted to %s ($%s)", threshold, owner_id, cents / 100)


async def on_subscription_activated(owner_id: str) -> None:
    """The referee's subscription just went live for the first time
    (``handle_subscription_state`` entering-live). Count them toward their
    referrer (once, while the sheet is open) and re-attempt any of their OWN
    pending milestone grants. Never raises."""
    try:
        s = launch_phase.schedule()
        if s is None:
            return  # the competition is not configured for this env
        if owner_id.startswith("org_"):
            return  # personal owners only
        email = await _email_for_owner(owner_id)
        if not email:
            return
        row = await launch_waitlist_repo.get(email)
        if not row:
            return
        # 1. Count this referee toward their referrer — jumps freeze the
        #    moment the sheet is no longer open (close stamps closed_at).
        if row.get("referred_by") and launch_phase.phase(s) == "open":
            if await launch_waitlist_repo.mark_referral_counted(email):
                referrer = await launch_waitlist_repo.get_by_referral_code(row["referred_by"])
                if referrer:
                    new_count = await launch_waitlist_repo.add_counter(referrer["email"], "referrals_subscribed", 1)
                    if new_count is not None:
                        await check_milestones({**referrer, "referrals_subscribed": new_count})
        # 2. This owner may themselves be a referrer whose milestone grant was
        #    pending on a missing Metronome ledger — their subscription is the
        #    moment one gets provisioned, so re-attempt.
        if any(row.get(_milestone_field(t)) == "pending_grant" for t, _ in MILESTONES):
            await check_milestones(row)
    except Exception:  # noqa: BLE001 — a launch bug must never fail the billing webhook
        logger.warning("launch_referral.on_subscription_activated failed for %s", owner_id, exc_info=True)
