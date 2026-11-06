"""Lightbulb's welcome: true copy (08 §5), no emoji, sent through the agent-mail doors."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import welcome_email

URL = "https://app.example.com/chat?panel=chat&room=r1"


def test_owner_version_is_lightbulb_speaking_and_promises_nothing_false():
    subject, text = welcome_email.owner_welcome(first_name="Ada", workspace="Acme", url=URL)
    assert subject == "Your team at Acme is ready"
    assert text.startswith("Hi Ada,\n\nI'm Lightbulb.")
    assert f"Open #general: {URL}" in text
    assert text.rstrip().endswith("— Lightbulb, Acme")
    assert "CEO" not in text
    for false_claim in ("container", "Nothing is\nshared", "marketplace", "overnight"):
        assert false_claim not in text
    assert all(ord(ch) < 0x2000 or ch in "—" for ch in subject + text)  # no emoji


def test_with_the_switch_the_owner_is_sent_to_their_chat_with_lightbulb():
    _, text = welcome_email.owner_welcome(first_name="Ada", workspace="Acme", url=URL, lightbulb_chat=True)
    assert f"Your chat with Lightbulb: {URL}" in text
    assert "general" not in text and "#" not in text


@pytest.mark.asyncio
async def test_the_owner_start_url_is_the_lightbulb_dm_with_the_switch():
    from core.services import messaging

    with (
        patch.object(messaging, "_front_agent_id", new=AsyncMock(return_value="ceo")),
        patch.object(messaging, "_agent_dm_room_id", new=AsyncMock(return_value="dm-1")) as dm,
    ):
        url, chat = await welcome_email.owner_start_url("org_1", "user_1")
    assert url.endswith("/chat?panel=chat&room=dm-1") and chat
    dm.assert_awaited_once_with("org_1", "user_1", "ceo")
    with patch.object(messaging, "_front_agent_id", new=AsyncMock(return_value=None)):
        assert await welcome_email.owner_start_url("org_1", "user_1") == (welcome_email.general_url("org_1"), False)


def test_teammate_version_names_the_inviter_when_known():
    subject, text = welcome_email.teammate_welcome(first_name="Sam", inviter="Ada", workspace="Acme", url=URL)
    assert subject == "You're in Acme on Lightbulb"
    assert "Ada added you to Acme." in text
    _, anonymous = welcome_email.teammate_welcome(first_name=None, inviter=None, workspace="Acme", url=URL)
    assert anonymous.startswith("Hi,\n\nYou've been added to Acme.")


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
    assert kwargs["subject"] == "Your team at Acme is ready"
    assert kwargs["tenant_name"] == "tenant-org_1"


@pytest.mark.asyncio
async def test_teammate_welcome_names_who_let_them_in():
    with (
        patch.object(welcome_email.clerk_admin, "get_organization", new=AsyncMock(return_value={"name": "Acme"})),
        patch.object(welcome_email.clerk_admin, "get_user", new=AsyncMock(return_value={"first_name": "Ada"})) as user,
        patch(
            "core.services.join_analytics._read_join_via",
            new=AsyncMock(return_value={"via": "invite", "invited_by": "user_ada"}),
        ),
        patch("core.services.agent_mail_owner_send.send_to_member", new=AsyncMock(return_value="sent")) as send,
    ):
        await welcome_email.send_teammate_welcome("org_1", user_id="user_2", email="sam@x.com", first_name="Sam")
    user.assert_awaited_once_with("user_ada")
    assert "Ada added you to Acme." in send.call_args.kwargs["body_text"]


@pytest.mark.asyncio
async def test_teammate_welcome_goes_to_the_member():
    with (
        patch("core.services.join_analytics._read_join_via", new=AsyncMock(return_value={})),
        patch.object(welcome_email.clerk_admin, "get_organization", new=AsyncMock(return_value=None)),
        patch("core.services.agent_mail_owner_send.send_to_member", new=AsyncMock(return_value="sent")) as send,
    ):
        await welcome_email.send_teammate_welcome("org_1", user_id="user_2", email="sam@x.com", first_name="Sam")
    kwargs = send.call_args.kwargs
    assert kwargs["email"] == "sam@x.com"
    assert kwargs["subject"] == "You're in your workspace on Lightbulb"
