"""Trial-end mail and the one pause (T19; docs/marketing/15 §A, §D, §F).

- Day 27 (3-4 days before ``trial_end``): one email to the owner from the CEO agent's address,
  with the team's numbers and the way to keep going.
- 90 days after an unpicked trial ended: the deletion notice, once. Nothing is deleted here;
  the notice is what comes before any deletion.
- One 30-day pause per company (:func:`pause_once`), offered by a founder for a timing lapse.

Each mail is guarded by a once-flag on the billing row, claimed before the send and released
if the send did not go out, so the 60-second trial sweep retries it.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import trial_recap

logger = logging.getLogger(__name__)

DAY = 86400
READING_ONLY_DAYS = 90
PAUSE_DAYS = 30


def _date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%B %-d")


async def _send_once(owner_id: str, field: str, purpose: str, subject: str, body: str, now: int) -> str:
    from core.services.agent_mail_owner_send import send_to_owner

    if not await billing_repo.claim_once(owner_id, field, now):
        return "already_sent"
    result = await send_to_owner(
        owner_id=owner_id, purpose=purpose, subject=subject, body_text=body, system_notice=True
    )
    if result in ("error", "rate_limited"):  # transient: the next sweep retries
        await billing_repo.release_once(owner_id, field)
    put_metric("trial.lifecycle_mail", dimensions={"purpose": purpose, "result": result})
    return result


def day27_body(recap: dict, price: int) -> str:
    lines = ["Here's what the team did with me this month."]
    if recap["people"] or recap["agents"]:
        lines.append(f"{recap['agents']} agents did work for {recap['people']} people in your rooms.")
    else:
        lines.append("We haven't finished anything together yet. Reply and I'll help set up one real task.")
    lines.append(
        f"Your free month ends {_date(recap['trial_end'])}. To keep going it's ${price} a month, "
        "and people are free up to five. After that date everything stays readable; I stop working "
        "until someone picks the plan."
    )
    lines.append(f"Keep the team: {settings.FRONTEND_URL.rstrip('/')}/settings?panel=billing")
    return "\n\n".join(lines) + "\n"


async def notices(owner_id: str, account: dict, now: int | None = None) -> None:
    """Called by the trial sweep for every trial_local row."""
    now = int(now if now is not None else time.time())
    end = account.get("trial_end")
    if end is None:
        return
    end = int(end)
    if 3 * DAY < end - now <= 4 * DAY and not account.get("trial_recap_emailed_at"):
        since = end - trial_recap.WINDOW_S
        people, agents = await trial_recap.room_activity(owner_id, since)
        body = day27_body({"trial_end": end, "people": people, "agents": agents}, settings.WORKSPACE_PRICE_USD)
        await _send_once(owner_id, "trial_recap_emailed_at", "trial_recap", "Our first month", body, now)
    elif now > end + READING_ONLY_DAYS * DAY and not account.get("deletion_notice_at"):
        body = (
            f"This workspace has been reading-only since {_date(end)}. Its record will be deleted "
            "unless an admin picks the plan. Nothing has been deleted yet. Reply to this email if you "
            "want an export or more time.\n\n"
            f"Keep the workspace: {settings.FRONTEND_URL.rstrip('/')}/settings?panel=billing\n"
        )
        await _send_once(
            owner_id, "deletion_notice_at", "deletion_notice", "Before we delete this workspace", body, now
        )


async def _rearm_automation(owner_id: str) -> None:
    """An extended trial retries cleanup of retired built-in schedules. User-created
    automations keep their own state; a durable reconcile retries cleanup on failure."""
    from core.services import proactive

    try:
        await proactive.ensure(owner_id)
    except Exception:
        logger.exception("pause: re-arming automation failed for %s; dispatching a reconcile", owner_id)
        from core.services.provision_dispatch import dispatch_reconcile

        await dispatch_reconcile(owner_id)


async def pause_once(owner_id: str, now: int | None = None) -> dict:
    """The one 30-day pause per company (A12). A trial moves its end 30 days out (from now, if it
    already ended); a live Stripe subscription has collection paused for 30 days."""
    import stripe

    from core.services.apple_iap import stripe_is_live

    now = int(now if now is not None else time.time())
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not account:
        return {"status": "no_billing_account"}
    trial = account.get("subscription_status") == "trial_local"
    if not trial and not stripe_is_live(account):
        return {"status": "no_subscription"}
    if not await billing_repo.claim_once(owner_id, "paused_at", now):
        return {"status": "pause_used"}
    try:
        if trial:
            end = max(int(account.get("trial_end") or now), now) + PAUSE_DAYS * DAY
            await billing_repo.extend_trial(owner_id, trial_end=end)
            await _rearm_automation(owner_id)
            return {"status": "paused", "until": end}
        until = now + PAUSE_DAYS * DAY
        stripe.Subscription.modify(
            account["stripe_subscription_id"],
            pause_collection={"behavior": "void", "resumes_at": until},
            idempotency_key=f"pause-once:{account['stripe_subscription_id']}",
        )
        return {"status": "paused", "until": until}
    except Exception:
        await billing_repo.release_once(owner_id, "paused_at")
        raise
