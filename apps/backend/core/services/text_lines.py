"""The texting line behind the Front desk add-on. Buying it is retired (no add-on price, no checkout); what is left
settles a pending Photon request, releases a line, and serves the line read."""

from __future__ import annotations

import logging


from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.services import onboarding_brief, photon
from core.services.agent_email import mail_changed
from core.services.billing_service import BillingServiceError
from core.services.hermes_provisioning import refresh_agent_soul

logger = logging.getLogger(__name__)


async def _clear_claim(owner_id: str) -> None:
    try:
        await billing_repo.release_front_desk_claim(owner_id)
    except Exception:
        logger.exception("Could not release the front desk claim for owner %s", owner_id)


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
