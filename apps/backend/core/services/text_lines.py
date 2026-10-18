"""The texting line behind the Front desk add-on: bought on the subscription webhook, released with it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import stripe

from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.repositories.paperclip_repo import customer_ops_agent_id
from core.services import onboarding_brief, photon
from core.services.agent_email import mail_changed
from core.services.billing_service import BillingServiceError, _is_front_desk_item
from core.services.hermes_provisioning import refresh_agent_soul

logger = logging.getLogger(__name__)

# A subscription in one of these is still the workspace's, whatever id is stored: never a stale cancel.
SUBSCRIPTION_LIVE_STATUSES = frozenset({"active", "trialing", "past_due"})
STALE_CLAIM_S = 120  # a claim with no rows behind it is a crash only after this long


async def _clear_claim(owner_id: str) -> None:
    try:
        await billing_repo.release_front_desk_claim(owner_id)
    except Exception:
        logger.exception("Could not release the front desk claim for owner %s", owner_id)


async def _abandon(owner_id: str, op: dict | None, code: str) -> None:
    """A request that left no pending row: cancel its operation (it may bill) and release the claim."""
    if op:
        try:
            await photon.cancel_operation(op["operationId"])
        except Exception:
            logger.warning(
                "Could not cancel line operation %s (owner %s): it may bill, cancel it by hand",
                op.get("operationId"),
                owner_id,
                exc_info=True,
            )
    await _clear_claim(owner_id)
    put_metric("text_line.provision_failed", dimensions={"code": code})


async def _settle(owner_id: str) -> str:
    """The webhook's one look at the operation: a failure raises so Stripe redelivers."""
    outcome = await complete_pending(owner_id)
    if outcome == "failed":
        raise BillingServiceError("line_provision_failed")
    return outcome


async def ensure_line(owner_id: str) -> str:
    """claim -> request the Photon operation -> one look -> else a pending row the panel's poll completes."""
    if await agent_email_repo.text_line_for_owner(owner_id):
        return "unchanged"
    if await agent_email_repo.pending_line_for_owner(owner_id):
        done = await _settle(owner_id)
        if done != "absent":
            return done
    agent_id = await customer_ops_agent_id(owner_id)
    if not agent_id:
        put_metric("text_line.no_front_desk_agent")
        return "no_agent"
    # The generation claimed under is the request key: stable until a release bumps it, so a redelivery after
    # a crash between the request and the pending row gets the same Photon operation back.
    gen = await billing_repo.claim_front_desk(owner_id)
    if gen is None:
        if await agent_email_repo.text_line_for_owner(owner_id) or await agent_email_repo.pending_line_for_owner(
            owner_id
        ):
            return "unchanged"
        # The flag is set with nothing behind it. A claim younger than STALE_CLAIM_S may still be inside its
        # Photon request; an older one is a crash, retaken in place so the generation (the request key) is kept.
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_S)).isoformat()
        gen = await billing_repo.claim_front_desk(owner_id, stale_before=stale_before)
        if gen is None:
            return "unchanged"
    op = None
    try:
        op = await photon.request_dedicated_line(idempotency_key=f"front-desk:{owner_id}:{gen}")
        stored = await agent_email_repo.put_pending_line(
            owner_id=owner_id, agent_id=agent_id, operation_id=op["operationId"]
        )
    except Exception as e:
        await _abandon(owner_id, op, str(getattr(e, "code", type(e).__name__)))
        raise
    if not stored:
        await _abandon(owner_id, op, "row_exists")
        raise RuntimeError(f"pending line already recorded for {owner_id}")
    return await _settle(owner_id)


async def complete_pending(owner_id: str) -> str:
    """One look at a requested line: ``provisioned``, ``pending`` (still running, or to be recorded on a later
    look), ``failed`` (the claim is released), or ``absent`` (nothing requested).

    Every pending-row delete is fenced on the operation this call read, so a slow reader never removes a newer
    request's row.
    """
    pending = await agent_email_repo.pending_line_for_owner(owner_id)
    if not pending:
        return "absent"
    op_id = pending["operation_id"]
    op = await photon.get_operation(op_id)
    state = op.get("state")
    if state in ("failed", "cancelled"):
        if not await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id):
            return "pending"  # another reader already settled this operation; its outcome stands
        await _clear_claim(owner_id)
        code = (op.get("error") or {}).get("code") or op.get("reason") or state
        put_metric("text_line.provision_failed", dimensions={"code": str(code)})
        return "failed"
    if state != "succeeded":
        return "pending"
    resource_id = op["resourceId"]
    agent_id = pending["agent_id"]
    line = recorded = None
    code = "record_failed"
    try:
        # Read first: another reader may have recorded a line already, and an owner holds one line only.
        line = await agent_email_repo.text_line_for_owner(owner_id)
        if not line:
            user = await photon.find_line_user(resource_id)
            if user and user.get("handle"):
                recorded = await agent_email_repo.put_text_line(
                    number=user["handle"],
                    owner_id=owner_id,
                    agent_id=agent_id,
                    photon_resource_id=resource_id,
                    photon_user_id=user["id"],
                )
    except Exception as e:
        code = str(getattr(e, "code", type(e).__name__))
    if not (line or recorded):
        try:
            line = await agent_email_repo.text_line_for_owner(owner_id)
        except Exception as e:
            code = str(getattr(e, "code", type(e).__name__))
        if not line:
            # Not recorded and nothing proves it is a conflict: keep the paid-for operation; a later look records it.
            put_metric("text_line.record_deferred", dimensions={"code": code})
            return "pending"
    if line and line.get("photon_resource_id") != resource_id:
        # The one definite conflict: the owner already has a different line, so this one is surplus.
        try:
            await photon.release_line(resource_id)
        except Exception:
            logger.warning(
                "Could not release surplus Photon line %s (owner %s): it is billing, release it by hand",
                resource_id,
                owner_id,
                exc_info=True,
            )
        await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id)
        put_metric("text_line.surplus_released")
        return "provisioned"
    # False: another reader already did; still provisioned
    await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id)
    try:
        await refresh_agent_soul(owner_id, agent_id)
    except Exception:
        logger.warning("Soul refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    try:
        await mail_changed(owner_id)
    except Exception:
        logger.warning("Mail refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    try:
        # The front desk's build prompt, for the website the workspace's brief named.
        await onboarding_brief.start_front_desk_build_for_line(owner_id)
    except Exception:
        logger.warning("Front desk build did not start for owner %s", owner_id, exc_info=True)
    put_metric("text_line.provisioned")
    return "provisioned"


async def release_line(owner_id: str) -> str:
    released = False
    pending = await agent_email_repo.pending_line_for_owner(owner_id)
    if pending:
        op_id = pending["operation_id"]
        await photon.cancel_operation(op_id)
        op = await photon.get_operation(op_id)
        state = op.get("state")
        if state in ("pending", "running"):
            raise BillingServiceError("line_release_pending")  # keep the row; Stripe redelivers
        if state == "succeeded":
            # The cancel lost the race: release the finished line by its resource, never through the record step.
            await photon.release_line(op["resourceId"])  # raises on anything but 404; Stripe redelivers
        await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id)
        released = True
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if not line:
        # Unsticks a lost claim; nothing to write when the flag is already clear.
        if (await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}).get("front_desk"):
            await _clear_claim(owner_id)
        return "released" if released else "absent"
    if line.get("photon_resource_id"):
        await photon.release_line(line["photon_resource_id"])  # raises on anything but 404; Stripe redelivers
    else:
        logger.warning("Line %s (owner %s) has no Photon resource id; release it by hand", line["address"], owner_id)
    await agent_email_repo.delete_text_line(line["address"])
    await billing_repo.release_front_desk_claim(owner_id)
    try:
        await refresh_agent_soul(owner_id, line["agent_id"])
    except Exception:
        logger.warning("Soul refresh failed after releasing a line for owner %s", owner_id, exc_info=True)
    return "released"


async def sync_front_desk(owner_id: str, *, subscription_id: str, previous_subscription_id: str | None = None) -> str:
    """Decide on the subscription as Stripe has it NOW, not as a possibly reordered event described it.

    A subscription that is no longer live and is not the workspace's current one (as stored now, or as stored
    before the event's own write, ``previous_subscription_id``) is a stale cancel: it never releases the line.
    A live subscription decides whatever id was stored. The line costs money the moment it exists, so only an
    ``active`` subscription buys it; past_due keeps an existing line until the item goes or the subscription
    is canceled.
    """
    subscription = stripe.Subscription.retrieve(subscription_id)
    status = subscription.get("status")
    has = any(_is_front_desk_item(i) for i in subscription["items"]["data"])
    if status not in SUBSCRIPTION_LIVE_STATUSES:
        current = (await billing_repo.get_by_owner_id(owner_id) or {}).get("stripe_subscription_id")
        if any(sid and sid != subscription_id for sid in (current, previous_subscription_id)):
            put_metric("text_line.sync_superseded")
            return "skipped_superseded"
    if status == "active" and has:
        return await ensure_line(owner_id)
    if not has or status == "canceled":
        return await release_line(owner_id)
    return "skipped"
