"""The welcome email, sent by the workspace's CEO agent from the team address (D10).

Two versions (08 §5), plain text, no emoji:

* **Owner** -- once, when the workspace exists: the provision executor sends it right after
  #general is created (``user_provision``), through ``send_to_owner`` as the CEO's first
  contact, so it goes out once and the agent's own first contact is not a second hello.
* **Teammate** -- on ``organizationMembership.created`` for anyone but the creator, through
  ``send_to_member``. A member who joins before the workspace finishes provisioning gets none
  (``incomplete_provisioning``): the room they land in says the same.

It replaced a platform welcome sent on ``user.created`` from ``hello@``, which went to every
account (invitees included) before any workspace existed and promised things that were not true.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.services import clerk_admin

logger = logging.getLogger(__name__)


def general_url(owner_id: str) -> str:
    from core.services.messaging import default_room_id

    return room_url(default_room_id(owner_id))


def room_url(room_id: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=chat&room={room_id}"


async def owner_start_url(owner_id: str, owner_user_id: str) -> tuple[str, bool]:
    """Where the owner starts, and whether it is their chat with Lightbulb (super-agent: the first-run DM)."""
    from core.services import messaging

    front = await messaging._front_agent_id(owner_id)
    if not front:
        return general_url(owner_id), False
    return room_url(await messaging._agent_dm_room_id(owner_id, owner_user_id, front)), True


def _greeting(first_name: str | None) -> str:
    name = (first_name or "").strip()
    return f"Hi {name}," if name else "Hi,"


def owner_welcome(*, first_name: str | None, workspace: str, url: str, lightbulb_chat: bool = False) -> tuple[str, str]:
    """(subject, text) for the person who created the workspace. ``lightbulb_chat``: the super-agent start, their
    chat with Lightbulb instead of the general project."""
    if lightbulb_chat:
        return (
            f"Your team at {workspace} is ready",
            f"""{_greeting(first_name)}

I'm Lightbulb. Your workspace is set up. I hand work to the specialists (Engineering, Finance, Growth, Analytics and Customer Operations) and show you each task.

Tell me what the business is (a website is enough). I'll write a short plan in our chat and ask you for one decision. While I work, invite the person who should see it with you. They can approve it too.

Your chat with Lightbulb: {url}

We run in your workspace's own cloud, so routines and email keep going when your laptop is closed. Spend, sends and deploys wait for a person. Your 30 days are free.

Reply to this email if anything is unclear.

— Lightbulb, {workspace}
""",
        )
    return (
        f"Your team at {workspace} is ready",
        f"""{_greeting(first_name)}

I'm Lightbulb. Your workspace is set up. I hand work to the specialists (Engineering, Finance, Growth, Analytics and Customer Operations) and show you each task.

Tell me what the business is (a website is enough). I'll write a short plan, post it in #general, and ask you for one decision. While I work, invite the person who should see it with you. They can approve it too.

Open #general: {url}

We run in your workspace's own cloud, so routines and email keep going when your laptop is closed. Spend, sends and deploys wait for a person. Your 30 days are free.

Reply to this email if anything is unclear.

— Lightbulb, {workspace}
""",
    )


def teammate_welcome(*, first_name: str | None, inviter: str | None, workspace: str, url: str) -> tuple[str, str]:
    """(subject, text) for someone who joined an existing workspace."""
    added = f"{inviter} added you to {workspace}." if inviter else f"You've been added to {workspace}."
    return (
        f"You're in {workspace} on Lightbulb",
        f"""{_greeting(first_name)}

{added} Lightbulb hands work to the specialists, and their results and approvals show up in the channels for everyone.

Start in #general: you'll see what's been asked and what's waiting on a decision. Mention an agent by name to hand it something.

Open #general: {url}

— Lightbulb, {workspace}
""",
    )


async def _workspace_name(owner_id: str) -> str:
    org = await clerk_admin.get_organization(owner_id) or {}
    return " ".join((org.get("name") or "").split()) or "your workspace"


async def send_owner_welcome(owner_id: str, owner_user_id: str) -> str:
    """The owner version, as Lightbulb's first contact. Returns ``send_to_owner``'s result."""
    from core.services.agent_mail_owner_send import send_to_owner

    user = await clerk_admin.get_user(owner_user_id) or {}
    url, lightbulb_chat = await owner_start_url(owner_id, owner_user_id)
    subject, text = owner_welcome(
        first_name=user.get("first_name"),
        workspace=await _workspace_name(owner_id),
        url=url,
        lightbulb_chat=lightbulb_chat,
    )
    from core.services.agent_mail_tenant import ensure_tenant

    return await send_to_owner(
        owner_id=owner_id,
        purpose="first_contact",
        subject=subject,
        body_text=text,
        tenant_name=await ensure_tenant(owner_id),  # the owner's SES tenant, like every agent send
    )


async def send_teammate_welcome(
    owner_id: str, *, user_id: str, email: str, first_name: str | None, inviter: str | None = None
) -> str:
    """The teammate version. Returns ``send_to_member``'s result."""
    from core.services.agent_mail_owner_send import send_to_member

    if inviter is None:
        # The join door's marker (T02) says who let them in: an invitation's sender or the
        # join link's creator. Unknown -> "You've been added to …".
        from core.services import join_analytics

        marker = await join_analytics._read_join_via(owner_id, user_id, email)
        invited_by = marker.get("invited_by")
        inviter = ((await clerk_admin.get_user(invited_by) or {}).get("first_name") or None) if invited_by else None
    subject, text = teammate_welcome(
        first_name=first_name, inviter=inviter, workspace=await _workspace_name(owner_id), url=general_url(owner_id)
    )
    return await send_to_member(owner_id=owner_id, user_id=user_id, email=email, subject=subject, body_text=text)
