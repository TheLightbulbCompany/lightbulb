"""Lightbulb's welcome: true copy (08 §5), no emoji, sent through the agent-mail doors."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import welcome_email

URL = "https://app.example.com/voice/setup"


def test_owner_version_is_lightbulb_speaking_and_promises_nothing_false():
    subject, text = welcome_email.owner_welcome(first_name="Ada", workspace="Acme", url=URL)
    assert subject == "Your assistant for Acme is ready to set up"
    assert text.startswith("Hi Ada,\n\nI'm Lightbulb, your new assistant. Once you're set up, I answer your calls")
    assert "try me" not in text and "before you go live" not in text  # a test call needs a paid plan
    assert f"Set up your assistant: {URL}" in text
    assert text.rstrip().endswith("Lightbulb, Acme")
    for old_product in ("CEO", "specialist", "#general", "Engineering", "container", "marketplace", "overnight"):
        assert old_product not in text
    assert all(ord(ch) < 0x2000 for ch in subject + text)  # no emoji, no em dash


def test_the_owner_is_pointed_at_assistant_setup():
    assert welcome_email.setup_url().endswith("/voice/setup")


@pytest.mark.asyncio
async def test_owner_welcome_is_lightbulbs_first_contact():
    with (
        patch.object(welcome_email.clerk_admin, "get_user", new=AsyncMock(return_value={"first_name": "Ada"})),
        patch.object(welcome_email.clerk_admin, "get_organization", new=AsyncMock(return_value={"name": "Acme"})),
        patch("core.services.agent_mail_owner_send.send_to_owner", new=AsyncMock(return_value="sent")) as send,
        patch("core.services.agent_mail_tenant.ensure_tenant", new=AsyncMock(return_value="tenant-org_1")),
    ):
        assert await welcome_email.send_owner_welcome("org_1", "user_1") == "sent"
    kwargs = send.call_args.kwargs
    assert kwargs["owner_id"] == "org_1"
    assert kwargs["purpose"] == "first_contact"
    assert kwargs["subject"] == "Your assistant for Acme is ready to set up"
    assert kwargs["body_text"].count(welcome_email.setup_url()) == 1
    assert kwargs["tenant_name"] == "tenant-org_1"
