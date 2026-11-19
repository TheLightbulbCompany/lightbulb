"""The one 30-day pause per company (T19; docs/marketing/15 §F), offered by a founder for a
timing lapse (:func:`pause_once`, admin only). The old trial-end mails (day-27 recap, deletion
notice) described the retired team product and were removed.
"""

from __future__ import annotations

import logging
import time

from core.repositories import billing_repo

logger = logging.getLogger(__name__)

DAY = 86400
PAUSE_DAYS = 30


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
    if account.get("metronome_voice_contract_id"):
        # Extending a leftover local trial or pausing the Workspace Stripe item
        # would leave the separately collected voice plan renewing unchanged.
        return {
            "status": "voice_plan_managed",
            "message": "Manage voice-plan billing separately; no pause was applied.",
        }
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
