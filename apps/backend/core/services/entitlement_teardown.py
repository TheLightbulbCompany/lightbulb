"""Stop serving an owner once NOTHING is paying for them any more.

Teardown used to live inside `customer.subscription.deleted` and be keyed on
that event, which was correct while Stripe was the only thing that could pay.
With Apple IAP there are two providers and one shared entitlement, so the
question a cancellation asks is no longer "did Stripe end" but "is anything
still paying" — and both providers have to be able to ask it.

Keying on the event rather than the answer breaks in both directions:

- **Stripe cancels while Apple is live.** The old handler tore the container
  down unconditionally. The shared status correctly stayed active (see
  ``billing_repo._effective_status``), so the owner kept their entitlement and
  lost the service it entitles them to — with nothing to trigger a rebuild,
  because no further notification was coming.
- **Apple revokes and there is no Stripe.** The notification path wrote
  "canceled" and stopped there. Nothing deleted the ECS service or disabled
  Paperclip, so an owner whose last entitlement ended kept a container running
  — and running agents keep spending on inference.

Every step is best-effort and independently guarded. These run from webhook
handlers where a raised exception means a 5xx and a provider retry, and a retry
of teardown is not free: it re-enters ECS and Paperclip calls that have already
half-succeeded. Losing a teardown is recoverable (the next status change, or
the idle reaper, or the cleanup cron); 5xx-looping a provider is not.
"""

from __future__ import annotations

import logging
from typing import Any

from core.observability.metrics import put_metric
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

#: Statuses that still entitle an owner to a running container. Mirrors
#: provision_gate._ACTIVE_STATUSES plus past_due, which is deliberate: a failed
#: card and an Apple billing-grace period are both recoverable, and destroying
#: someone's workspace over a retryable payment is not a decision to take on
#: their behalf. Stripe's own dunning window and Apple's grace period both end
#: in an explicit revoke, which IS torn down.
_STILL_ENTITLED = frozenset({"active", "trialing", "trial_local", "past_due"})


def still_entitled(row: dict[str, Any] | None) -> bool:
    """Whether ANY provider still pays for this owner."""
    if not row:
        return False
    return (row.get("subscription_status") or "") in _STILL_ENTITLED


async def teardown_if_unentitled(owner_id: str, *, trigger: str) -> bool:
    """Tear the owner's runtime down, unless something still pays for them.

    Re-reads the row rather than trusting a caller's snapshot: the caller has
    just written one provider's status, and the shared value it must act on is
    derived from BOTH. A snapshot taken before that write says nothing about the
    combined answer.

    Returns True if teardown ran. ``trigger`` only labels metrics and logs.
    """
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if still_entitled(row):
        logger.info(
            "entitlement.teardown_skipped owner_id=%s trigger=%s status=%s — another provider still pays",
            owner_id,
            trigger,
            (row or {}).get("subscription_status"),
        )
        put_metric("entitlement.teardown", dimensions={"trigger": trigger, "outcome": "skipped"})
        return False

    logger.info("entitlement.teardown owner_id=%s trigger=%s", owner_id, trigger)
    put_metric("entitlement.teardown", dimensions={"trigger": trigger, "outcome": "ran"})

    # Owner facts FIRST. Teardown below is best-effort and may be delayed or
    # fail, and a still-running container's agents must not keep reporting an
    # active paid plan while it lingers.
    try:
        from core.services.owner_facts import write_owner_facts

        await write_owner_facts(owner_id, account_overrides={"subscription_status": "canceled"})
    except Exception:
        logger.exception("entitlement.teardown owner-facts refresh failed for owner %s", owner_id)

    row_at_death: dict[str, Any] | None = None
    ecs_deleted = False
    try:
        from core.containers import get_ecs_manager

        row_at_death = await get_ecs_manager().delete_user_service(owner_id)
        ecs_deleted = True
    except Exception:
        logger.exception("entitlement.teardown container delete failed for owner %s", owner_id)

    # The per-owner Bedrock AIPs and IAM role are account-wide-quota'd AWS
    # resources (IAM roles cap at 1,000), and `delete_user_service` destroys the
    # containers row that is the only handle on the AIP arns — so if they are not
    # reaped HERE, off that row's own ALL_OLD image, nothing can ever reach them
    # again. Until now only the admin account-wipe deleted them, so every
    # cancellation leaked a role plus its inference profiles. Both re-mint
    # idempotently (`ensure_owner_bedrock_role`) if the owner comes back.
    #
    # Gated on the ECS delete having SUCCEEDED: if the service survived, its task
    # may still be running under this role, and pulling the role out from under a
    # live container turns a retryable teardown into a confusing broken one. The
    # next trigger (or the idle reaper) re-runs this.
    if ecs_deleted:
        from core.billing.bedrock_aip import delete_owner_aips, delete_owner_bedrock_role

        # Anything that is not the documented `dict | None` is treated as "no
        # arns to reap" rather than allowed to raise. This runs from webhook
        # handlers where an exception means a 5xx and a provider retry, and
        # this extraction sits outside the per-call try blocks below — so an
        # unexpected row shape here would take the whole teardown down with it.
        row = row_at_death if isinstance(row_at_death, dict) else {}
        aip_arns = list((row.get("bedrock_aip_arns") or {}).values())
        if aip_arns:
            try:
                await delete_owner_aips(owner_id, aip_arns=aip_arns)
            except Exception:
                logger.exception("entitlement.teardown Bedrock AIP delete failed for owner %s", owner_id)

        # Unconditional and un-gated on the row: the role name derives from
        # owner_id alone, and the delete is idempotent (every step swallows
        # NoSuchEntity). Gating on a `bedrock_role_arn` field the way the
        # account-wipe does is exactly what makes a role unreachable once its
        # row is gone.
        try:
            await delete_owner_bedrock_role(owner_id)
        except Exception:
            logger.exception("entitlement.teardown Bedrock role delete failed for owner %s", owner_id)

    # Disable Paperclip with a 30-day grace window — the cleanup cron purges the
    # Paperclip-side artifacts after the grace elapses. Only THIS owner is
    # disabled; in a multi-member org the others keep access until their own
    # subscriptions end (or the org is deleted in Clerk).
    from core.services.paperclip_provisioning import (
        build_paperclip_provisioning,
        close_paperclip_provisioning,
    )

    provisioning = None
    try:
        provisioning = await build_paperclip_provisioning()
        await provisioning.disable(user_id=owner_id)
        put_metric("paperclip.webhook.disable", dimensions={"trigger": trigger})
    except Exception:
        logger.exception("entitlement.teardown Paperclip disable failed for owner %s", owner_id)
    finally:
        if provisioning is not None:
            await close_paperclip_provisioning(provisioning)

    return True
