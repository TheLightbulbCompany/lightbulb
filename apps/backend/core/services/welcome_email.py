"""The welcome email, sent by Lightbulb from the team address (D10).

Plain text, no emoji. Sent once, when the workspace exists: the provision executor sends it
(``user_provision``), through ``send_to_owner`` as Lightbulb's first contact, and points the
person at setting up their assistant (``/voice/setup``).

It replaced a platform welcome sent on ``user.created`` from ``hello@``, which went to every
account before any workspace existed and promised things that were not true.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.services import clerk_admin

logger = logging.getLogger(__name__)


def setup_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/voice/setup"


def _greeting(first_name: str | None) -> str:
    name = (first_name or "").strip()
    return f"Hi {name}," if name else "Hi,"


def owner_welcome(*, first_name: str | None, workspace: str, url: str) -> tuple[str, str]:
    """(subject, text) for the person who created the workspace."""
    return (
        f"Your assistant for {workspace} is ready to set up",
        f"""{_greeting(first_name)}

I'm Lightbulb, your new assistant. Once you're set up, I answer your calls, book appointments and take messages.

Setup is five steps. Give me your website and I'll learn your hours, services and prices. Then choose where bookings go, keep your number or get a new one, and give me a name.

Set up your assistant: {url}

Reply to this email if anything is unclear.

Lightbulb, {workspace}
""",
    )


async def _workspace_name(owner_id: str) -> str:
    org = await clerk_admin.get_organization(owner_id) or {}
    return " ".join((org.get("name") or "").split()) or "your business"


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
