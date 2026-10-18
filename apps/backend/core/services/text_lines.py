"""The texting line behind the Front desk add-on: bought on the subscription webhook, released with it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time

import stripe

from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import photon
from core.services.agent_email import mail_changed
from core.services.billing_service import BillingServiceError, _is_front_desk_item
from core.services.hermes_provisioning import refresh_agent_soul

logger = logging.getLogger(__name__)

# The line costs money the moment it exists, so only a paid subscription buys it; past_due keeps an
# existing line until the item goes or the subscription is canceled.
FRONT_DESK_LIVE_STATUSES = frozenset({"active"})


class _RowExists(RuntimeError):
    pass


POLL_S = 2  # Photon operations are polled every 2 s while a caller waits
WAIT_S = 8  # wall-clock budget for a caller that waits on a fresh operation
STALE_CLAIM_S = 120  # a claim with no rows behind it is a crash only after this long


class _RecordFailed(RuntimeError):
    pass


async def _clear_claim(owner_id: str) -> None:
    try:
        await billing_repo.set_front_desk(owner_id, False)
    except Exception:
        logger.exception("Could not release the front desk claim for owner %s", owner_id)


async def _line_or_pending(owner_id: str) -> bool:
    return bool(
        await agent_email_repo.text_line_for_owner(owner_id) or await agent_email_repo.pending_line_for_owner(owner_id)
    )


async def ensure_line(owner_id: str, *, from_webhook: bool = False) -> str:
    """claim -> request the Photon operation -> short wait -> else leave a pending row for completion on read."""
    if await agent_email_repo.text_line_for_owner(owner_id):
        return "unchanged"
    if await agent_email_repo.pending_line_for_owner(owner_id):
        done = await complete_pending(owner_id, wait_s=WAIT_S, from_webhook=from_webhook, again=False)
        if done != "absent":
            return done
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    agent_id = (getattr(row, "starter_agent_ids", None) or {}).get("customer_operations")
    if not agent_id:
        put_metric("text_line.no_front_desk_agent")
        return "no_agent"
    if not await billing_repo.claim_front_desk(owner_id):
        if await _line_or_pending(owner_id):
            return "unchanged"
        # The flag is set with nothing behind it. A claim younger than STALE_CLAIM_S may still be inside its
        # Photon request; an older one is a crash, retaken in place so the generation (the request key) is kept.
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_S)).isoformat()
        if not await billing_repo.claim_front_desk(owner_id, stale_before=stale_before):
            return "unchanged"
    op = None
    try:
        # Stable until a release bumps the generation: a redelivery after a crash between the request and
        # the pending row gets the same Photon operation back instead of buying a second line.
        _, gen = await billing_repo.front_desk_meta(owner_id)
        op = await photon.request_dedicated_line(idempotency_key=f"front-desk:{owner_id}:{gen}")
        if not await agent_email_repo.put_pending_line(
            owner_id=owner_id, agent_id=agent_id, operation_id=op["operationId"]
        ):
            raise _RowExists(f"pending line already recorded for {owner_id}")
    except Exception as e:
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
        code = "row_exists" if isinstance(e, _RowExists) else str(getattr(e, "code", type(e).__name__))
        put_metric("text_line.provision_failed", dimensions={"code": code})
        raise
    return await complete_pending(owner_id, wait_s=WAIT_S, from_webhook=from_webhook, again=False)


async def _read_failed_retry(owner_id: str) -> str:
    try:
        again = await ensure_line(owner_id)
    except Exception:
        logger.warning("Re-requesting the line failed for owner %s", owner_id, exc_info=True)
        return "absent"
    return again if again in ("provisioned", "pending") else "absent"


async def complete_pending(owner_id: str, *, wait_s: float = 0, from_webhook: bool = False, again: bool = True) -> str:
    """Finish a requested line if Photon has: poll up to ``wait_s``, then record it, fail it, or leave it pending.

    A read that finds the operation failed asks for a new line once (``again``), so a failure is never a dead end.
    """
    pending = await agent_email_repo.pending_line_for_owner(owner_id)
    if not pending:
        return "absent"
    op = await photon.get_operation(pending["operation_id"])
    deadline = time.monotonic() + wait_s
    while op.get("state") in ("pending", "running") and time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        op = await photon.get_operation(pending["operation_id"])
    state = op.get("state")
    if state in ("failed", "cancelled"):
        await agent_email_repo.delete_pending_line(owner_id)
        await _clear_claim(owner_id)
        code = (op.get("error") or {}).get("code") or op.get("reason") or state
        put_metric("text_line.provision_failed", dimensions={"code": str(code)})
        if from_webhook:
            raise BillingServiceError("line_provision_failed")
        return await _read_failed_retry(owner_id) if again else "absent"
    if state != "succeeded":
        return "pending"
    resource_id = op["resourceId"]
    agent_id = pending["agent_id"]
    deferred: Exception | None = None
    try:
        user = await photon.find_line_user(resource_id)
        if not user:
            raise _RecordFailed(f"no Photon user for {resource_id}")
        recorded = await agent_email_repo.put_text_line(
            number=user["handle"],
            owner_id=owner_id,
            agent_id=agent_id,
            photon_resource_id=resource_id,
            photon_user_id=user["id"],
        )
    except Exception as e:
        recorded, deferred = None, e
    if recorded is None:
        try:
            line = await agent_email_repo.text_line_for_owner(owner_id)
        except Exception as e:
            line, deferred = None, deferred or e
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
            await agent_email_repo.delete_pending_line(owner_id)
            put_metric("text_line.surplus_released")
            return "provisioned"
        if not line:
            # Not recorded and nothing proves it is a conflict: keep the paid-for operation and retry later.
            code = "record_failed" if deferred is None else str(getattr(deferred, "code", type(deferred).__name__))
            put_metric("text_line.record_deferred", dimensions={"code": code})
            if from_webhook:
                raise BillingServiceError("line_record_pending") from deferred
            return "pending"
    await agent_email_repo.delete_pending_line(owner_id)  # False: another reader already did; still provisioned
    try:
        await refresh_agent_soul(owner_id, agent_id)
    except Exception:
        logger.warning("Soul refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    try:
        await mail_changed(owner_id)
    except Exception:
        logger.warning("Mail refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    put_metric("text_line.provisioned")
    return "provisioned"


async def release_line(owner_id: str) -> str:
    pending = await agent_email_repo.pending_line_for_owner(owner_id)
    if pending:
        await photon.cancel_operation(pending["operation_id"])
        op = await photon.get_operation(pending["operation_id"])
        if op.get("state") == "succeeded":
            # The cancel lost the race: record the line, then release it below. A deferred record can't be released yet.
            if await complete_pending(owner_id) == "pending":
                raise BillingServiceError("line_release_pending")
        elif op.get("state") in ("pending", "running"):
            raise BillingServiceError("line_release_pending")  # keep the row; Stripe redelivers
        else:
            await agent_email_repo.delete_pending_line(owner_id)
            await billing_repo.set_front_desk(owner_id, False)
            return "released"
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if not line:
        try:
            await billing_repo.set_front_desk(owner_id, False)  # unsticks a lost claim
        except Exception:
            logger.warning("Could not clear the front desk flag for owner %s", owner_id, exc_info=True)
        return "absent"
    if line.get("photon_resource_id"):
        await photon.release_line(line["photon_resource_id"])  # raises on anything but 404; Stripe redelivers
    else:
        logger.warning("Line %s (owner %s) has no Photon resource id; release it by hand", line["address"], owner_id)
    await agent_email_repo.delete_text_line(line["address"])
    await billing_repo.set_front_desk(owner_id, False)
    try:
        await refresh_agent_soul(owner_id, line["agent_id"])
    except Exception:
        logger.warning("Soul refresh failed after releasing a line for owner %s", owner_id, exc_info=True)
    return "released"


async def sync_front_desk(owner_id: str, *, subscription_id: str) -> str:
    """Decide on the subscription as Stripe has it NOW, not as a possibly reordered event described it."""
    subscription = stripe.Subscription.retrieve(subscription_id)
    status = subscription.get("status")
    has = any(_is_front_desk_item(i) for i in subscription["items"]["data"])
    if status in FRONT_DESK_LIVE_STATUSES and has:
        return await ensure_line(owner_id, from_webhook=True)
    if not has or status == "canceled":
        return await release_line(owner_id)
    return "skipped"
