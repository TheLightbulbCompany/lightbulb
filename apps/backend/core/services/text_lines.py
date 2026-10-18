"""The texting line behind the Front desk add-on: bought on the subscription webhook, released with it."""

from __future__ import annotations

import logging

import stripe

from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import sendblue
from core.services.billing_service import BillingServiceError, _is_front_desk_item
from core.services.hermes_provisioning import refresh_agent_soul

logger = logging.getLogger(__name__)

# The line costs money the moment it exists, so only a paid subscription buys it; past_due keeps an
# existing line until the item goes or the subscription is canceled.
FRONT_DESK_LIVE_STATUSES = frozenset({"active"})


class _RowExists(RuntimeError):
    pass


async def ensure_line(owner_id: str) -> str:
    if await agent_email_repo.text_line_for_owner(owner_id):
        return "unchanged"
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    agent_id = (getattr(row, "starter_agent_ids", None) or {}).get("customer_operations")
    if not agent_id:
        raise BillingServiceError("no_front_desk_agent")
    # ponytail: a process death between the claim and the row write leaves front_desk=True with no row (hand reset); the window is one HTTP call.
    if not await billing_repo.claim_front_desk(owner_id):
        return "unchanged"
    number = None
    try:
        number = await sendblue.provision_line()
        if await agent_email_repo.put_text_line(number=number, owner_id=owner_id, agent_id=agent_id) is None:
            raise _RowExists(f"text line {number} already recorded")
    except Exception as e:
        if number:
            try:
                await sendblue.deprovision_line(number)
            except Exception:
                logger.warning(
                    "Could not release unrecorded line %s (owner %s): it is billing, release it by hand",
                    number,
                    owner_id,
                    exc_info=True,
                )
        try:
            await billing_repo.set_front_desk(owner_id, False)
        except Exception:
            logger.exception("Could not release the front desk claim for owner %s", owner_id)
        code = "row_exists" if isinstance(e, _RowExists) else str(getattr(e, "code", type(e).__name__))
        put_metric("text_line.provision_failed", dimensions={"code": code})
        raise
    try:
        await refresh_agent_soul(owner_id, agent_id)
    except Exception:
        logger.warning("Soul refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    return "provisioned"


async def release_line(owner_id: str) -> str:
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if not line:
        try:
            await billing_repo.set_front_desk(owner_id, False)  # unsticks a lost claim
        except Exception:
            logger.warning("Could not clear the front desk flag for owner %s", owner_id, exc_info=True)
        return "absent"
    number = line["address"]
    try:
        await sendblue.deprovision_line(number)
    except Exception:
        logger.warning(
            "Sendblue deprovision failed for %s (owner %s); release it by hand", number, owner_id, exc_info=True
        )
    await agent_email_repo.delete_text_line(number)
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
        return await ensure_line(owner_id)
    if not has or status == "canceled":
        return await release_line(owner_id)
    return "skipped"
