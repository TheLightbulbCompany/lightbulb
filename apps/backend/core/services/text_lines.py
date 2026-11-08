"""The texting line behind the Front desk add-on: bought on the subscription webhook, released with it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import stripe

from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.repositories.workspace_repo import customer_ops_agent_id
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
    """A request that left no pending row: cancel its operation, release a line the cancel came too late for
    (it bills, and no row would ever find it), then release the claim."""
    if op:
        try:
            await photon.cancel_operation(op["operationId"])
            done = await photon.get_operation(op["operationId"])
            if done.get("state") == "succeeded":
                await _release_resource(done["resourceId"])
        except Exception:  # noqa: BLE001 — ponytail: no row to retry from here; text_line.orphan_line is the alarm
            logger.error(
                "Line operation %s (owner %s): not cancelled, or its line not released; it bills, release it by hand",
                op.get("operationId"),
                owner_id,
                exc_info=True,
            )
            put_metric("text_line.orphan_line")
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
        # A claim with no row yet is a request in flight (the window between the claim and the pending row):
        # still pending, so a reader keeps polling instead of stopping on a one-second gap.
        account = await billing_repo.get_by_owner_id(owner_id) or {}
        return "pending" if account.get("front_desk") else "absent"
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
            user, next_page = await photon.find_line_user(resource_id, page_token=pending.get("page_token"))
            if user is None and next_page != pending.get("page_token"):
                # A long user list: keep where the walk stopped, so the next look continues instead of restarting.
                await agent_email_repo.set_pending_page_token(owner_id, operation_id=op_id, page_token=next_page)
            if user and user.get("handle"):
                recorded = await agent_email_repo.record_text_line(
                    number=user["handle"],
                    owner_id=owner_id,
                    agent_id=agent_id,
                    photon_resource_id=resource_id,
                    photon_user_id=user["id"],
                    operation_id=op_id,
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
            await _release_resource(resource_id)
        except Exception:  # noqa: BLE001 — still billing; the row is the only record of it, so it stays
            # The next look retries: a panel open (get_line settles a pending row beside a line), a cancel, a wipe.
            logger.warning("Surplus Photon line %s (owner %s) not released yet", resource_id, owner_id, exc_info=True)
            put_metric("text_line.surplus_release_deferred")
            return "provisioned"
        await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id)
        put_metric("text_line.surplus_released")
        return "provisioned"
    # Recorded here (the same transaction removed the pending row) or by another reader: provisioned.
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


async def _release_resource(resource_id: str) -> None:
    """Released now, or raises so the caller's row survives for the retry (Stripe redelivers; the wipe reruns).
    A 202 release is an operation: only ``succeeded`` frees the row."""
    op = await photon.release_line(resource_id)
    if op is None:
        return
    state = op.get("state")
    if state in ("pending", "running"):
        raise BillingServiceError("line_release_pending")
    code = (op.get("error") or {}).get("code") or op.get("reason") or state
    put_metric("text_line.release_failed", dimensions={"code": str(code)})
    raise BillingServiceError("line_release_failed")


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
            await _release_resource(op["resourceId"])  # raises unless released; Stripe redelivers
        await agent_email_repo.delete_pending_line(owner_id, operation_id=op_id)
        released = True
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if not line:
        # Unsticks a lost claim; nothing to write when the flag is already clear.
        if (await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}).get("front_desk"):
            await _clear_claim(owner_id)
        return "released" if released else "absent"
    if line.get("photon_resource_id"):
        await _release_resource(line["photon_resource_id"])  # raises unless released; Stripe redelivers
    else:
        logger.warning("Line %s (owner %s) has no Photon resource id; release it by hand", line["address"], owner_id)
    await agent_email_repo.delete_text_line(line["address"])
    await billing_repo.release_front_desk_claim(owner_id)
    try:
        await refresh_agent_soul(owner_id, line["agent_id"])
    except Exception:
        logger.warning("Soul refresh failed after releasing a line for owner %s", owner_id, exc_info=True)
    return "released"


def _is_live(subscription_id: str) -> bool:
    """Whether Stripe has this subscription live NOW; an id Stripe no longer answers for is not."""
    try:
        return stripe.Subscription.retrieve(subscription_id).get("status") in SUBSCRIPTION_LIVE_STATUSES
    except stripe.error.StripeError:
        return False


async def sync_front_desk(owner_id: str, *, subscription_id: str, previous_subscription_id: str | None = None) -> str:
    """Decide on the subscription as Stripe has it NOW, not as a possibly reordered event described it.

    A subscription that is no longer live is a stale cancel, and never releases the line, only when the
    workspace holds ANOTHER subscription that is itself live (as stored now, or as stored before the event's
    own write, ``previous_subscription_id``). A stored id that is dead too (a reordered cancel of an older
    subscription overwrote the row) supersedes nothing: the cancel stands and the line goes. A live
    subscription decides whatever id was stored. The line costs money the moment it exists, so only an
    ``active`` subscription buys it; past_due keeps an existing line until the item goes or the subscription
    is canceled.
    """
    subscription = stripe.Subscription.retrieve(subscription_id)
    status = subscription.get("status")
    has = any(_is_front_desk_item(i) for i in subscription["items"]["data"])
    if status not in SUBSCRIPTION_LIVE_STATUSES:
        current = (await billing_repo.get_by_owner_id(owner_id) or {}).get("stripe_subscription_id")
        others = {sid for sid in (current, previous_subscription_id) if sid and sid != subscription_id}
        if any(_is_live(sid) for sid in others):
            put_metric("text_line.sync_superseded")
            return "skipped_superseded"
    if status == "active" and has:
        return await ensure_line(owner_id)
    if not has or status == "canceled":
        return await release_line(owner_id)
    return "skipped"


async def unlink_member(owner_id: str, user_id: str) -> None:
    """Drop every phone ``user_id`` linked on the workspace's line: a member who left texts as a customer."""
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if line:
        await agent_email_repo.drop_member_phones(line, user_id)
