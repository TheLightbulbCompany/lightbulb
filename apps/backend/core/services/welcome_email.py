"""The welcome email, sent by Lightbulb from the team address (D10).

Two versions (08 §5), plain text, no emoji:

* **Owner** -- once, when the workspace exists: the provision executor sends it
  (``user_provision``), through ``send_to_owner`` as Lightbulb's first contact, and points
  them at setting up their assistant (``/voice/setup``).
* **Teammate** -- on ``organizationMembership.created`` for anyone but the creator, through
  ``send_to_member``. A member who joins before the workspace finishes provisioning gets none
  (``incomplete_provisioning``).

It replaced a platform welcome sent on ``user.created`` from ``hello@``, which went to every
account (invitees included) before any workspace existed and promised things that were not true.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.services import clerk_admin

logger = logging.getLogger(__name__)


def setup_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/voice/setup"


def home_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/voice"


def _greeting(first_name: str | None) -> str:
    name = (first_name or "").strip()
    return f"Hi {name}," if name else "Hi,"


def owner_welcome(*, first_name: str | None, workspace: str, url: str) -> tuple[str, str]:
    """(subject, text) for the person who created the workspace."""
    return (
        f"Your assistant for {workspace} is ready to set up",
        f"""{_greeting(first_name)}

I'm Lightbulb. I answer your calls, book appointments and take messages.

To start, set up your assistant. Give me your website and I'll learn your hours, services and prices. Then pick where bookings go and how callers reach me, and try me out before you go live.

Set up your assistant: {url}

Reply to this email if anything is unclear.

Lightbulb, {workspace}
""",
    )


def teammate_welcome(*, first_name: str | None, inviter: str | None, workspace: str, url: str) -> tuple[str, str]:
    """(subject, text) for someone who joined an existing workspace."""
    added = f"{inviter} added you to {workspace}." if inviter else f"You've been added to {workspace}."
    return (
        f"You're in {workspace} on Lightbulb",
        f"""{_greeting(first_name)}

{added} Lightbulb answers the business's calls, books appointments and takes messages.

Open Lightbulb to see who called and what's waiting on you: {url}

Lightbulb, {workspace}
""",
    )


async def _workspace_name(owner_id: str) -> str:
    org = await clerk_admin.get_organization(owner_id) or {}
    return " ".join((org.get("name") or "").split()) or "your workspace"


async def send_owner_welcome(owner_id: str, owner_user_id: str) -> str:
    """The owner version, as Lightbulb's first contact. Returns ``send_to_owner``'s result."""
    from core.services.agent_mail_owner_send import send_to_owner

    user = await clerk_admin.get_user(owner_user_id) or {}
    subject, text = owner_welcome(
        first_name=user.get("first_name"), workspace=await _workspace_name(owner_id), url=setup_url()
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
        first_name=first_name, inviter=inviter, workspace=await _workspace_name(owner_id), url=home_url()
    )
    return await send_to_member(owner_id=owner_id, user_id=user_id, email=email, subject=subject, body_text=text)
