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

    # RE-CHECK, immediately before the destructive call. The entitlement read at
    # the top of this function is already several awaits old by now — an owner
    # facts write, and whatever the caller did before us. Two ways that gap
    # bites, both of which end with a PAYING owner's container destroyed:
    #
    #   * they resubscribe while this is in flight, or
    #   * a cancellation webhook is redelivered after they resubscribed (Stripe
    #     and Apple both retry on 5xx).
    #
    # Destroying the container self-heals — the resubscribe's durable reconcile
    # reprovisions a workspace with no running runtime. The Bedrock resources `delete_user_service` reaps behind it
    # do NOT: the resubscribe's provision has already returned success, so
    # nothing retries, and the owner's chat just hard-fails on Bedrock. Cheap
    # strongly-consistent read against that.
    current_account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if still_entitled(current_account):
        logger.warning(
            "entitlement.teardown ABORTED for owner %s trigger=%s — became entitled again "
            "mid-teardown (resubscribe or redelivered webhook); leaving the runtime alone",
            owner_id,
            trigger,
        )
        put_metric("entitlement.teardown", dimensions={"trigger": trigger, "outcome": "aborted_raced"})
        return False

    try:
        from core.services.hermes_provisioning import delete_workspace_runtime

        # Reaps the owner's Bedrock footprint (gate-token rows, AIPs, IAM role)
        # internally — that is the seam every teardown path shares, so there is
        # nothing to duplicate here. See reap_owner_bedrock_resources.
        # NOT delete_profiles: a lapsed or past-due subscription must not destroy the
        # owner's memory/skills/SOUL.md (M6-R42).
        await delete_workspace_runtime(owner_id)
    except Exception:
        logger.exception("entitlement.teardown container delete failed for owner %s", owner_id)

    # A workspace can have an old managed key even if it currently uses BYOK or
    # ChatGPT. Retire by key existence/state, not the current provider choice.
    # The gate already denies a canceled subscription; the settlement worker
    # keeps the key usable for pending generation lookups until the outbox drains.
    try:
        from core.repositories.openrouter_key_repo import mark_retiring

        await mark_retiring(owner_id)
    except Exception:
        logger.exception("entitlement.teardown OpenRouter key retirement failed for owner %s", owner_id)

    # No Paperclip disable here: the company belongs to the workspace, and a
    # lapsed entitlement is not a deleted workspace (``organization.deleted``
    # disables the workspace row). The old per-user ``disable(owner_id)`` found
    # no row under an org id and was a no-op for every workspace.

    return True
