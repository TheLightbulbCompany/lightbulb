"""What is left of the agent-mail send door: the per-owner daily send cap and the owner's address.

Agent-authored mail (replying to a stored inbound, ``--to`` a stranger) is retired with the agent product. The
cap is still spent by the RSVP mail a Meetings invite gets (`core/voice/meetings/meeting_rsvp.py`), and the
owner lookup is still how the team address is minted.
"""

from datetime import datetime, timezone

from core.repositories import container_repo
from core.repositories.workspace_repo import WorkspaceRepo
from core.services.agent_mail_owner_send import DAILY_SEND_CAP
from core.services.owner_email import lookup_owner_email


async def consume_cap(owner_id: str) -> bool:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP)


async def _owner_email(owner_id: str) -> str | None:
    onboarding = await WorkspaceRepo().get_onboarding_state(owner_id) or {}
    return await lookup_owner_email(onboarding.get("user_id") or owner_id)
