"""The texting line behind the Front desk add-on: bought on the subscription webhook, released with it."""

from __future__ import annotations

import logging

from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import sendblue
from core.services.billing_service import BillingServiceError, _is_front_desk_item
from core.services.hermes_provisioning import refresh_agent_soul
from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

logger = logging.getLogger(__name__)


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
    try:
        number = await sendblue.provision_line()
        await agent_email_repo.put_text_line(number=number, owner_id=owner_id, agent_id=agent_id)
    except Exception as e:
        try:
            await billing_repo.set_front_desk(owner_id, False)
        except Exception:
            logger.exception("Could not release the front desk claim for owner %s", owner_id)
        put_metric("text_line.provision_failed", dimensions={"code": str(getattr(e, "code", type(e).__name__))})
        raise
    try:
        await refresh_agent_soul(owner_id, agent_id)
    except Exception:
        logger.warning("Soul refresh failed after provisioning a line for owner %s", owner_id, exc_info=True)
    return "provisioned"


async def release_line(owner_id: str) -> str:
    line = await agent_email_repo.text_line_for_owner(owner_id)
    if not line:
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


async def sync_front_desk(owner_id: str, *, status: str, items: list[dict]) -> str:
    has = any(_is_front_desk_item(i) for i in items)
    if status in LIVE_SUBSCRIPTION_STATUSES and has:
        return await ensure_line(owner_id)
    if not has or status == "canceled":
        return await release_line(owner_id)
    return "skipped"
