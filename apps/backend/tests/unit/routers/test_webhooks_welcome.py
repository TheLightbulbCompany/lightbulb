"""The welcome comes from the workspace's CEO once a workspace exists (T14, D10):
user.created sends nothing; a teammate's membership sends the teammate version;
the creator's own membership does not (their version goes out after provisioning)."""

from unittest.mock import AsyncMock, patch

import pytest

_HEADERS = {"svix-id": "msg_welcome", "svix-timestamp": "1", "svix-signature": "ignored"}


@pytest.mark.asyncio
async def test_user_created_sends_no_welcome(async_client, monkeypatch):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    payload = {
        "type": "user.created",
        "data": {
            "id": "u_new",
            "first_name": "Ada",
            "email_addresses": [{"id": "ea_1", "email_address": "ada@example.com"}],
            "primary_email_address_id": "ea_1",
        },
    }
    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.services.welcome_email.send_teammate_welcome", new=AsyncMock()) as teammate,
        patch("core.services.welcome_email.send_owner_welcome", new=AsyncMock()) as owner,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=payload, headers=_HEADERS)

    assert resp.status_code == 200
    teammate.assert_not_awaited()
    owner.assert_not_awaited()


def _membership(user_id: str) -> dict:
    return {
        "organization": {"id": "org_x", "created_by": "user_owner"},
        "public_user_data": {"user_id": user_id, "identifier": "m@example.com", "first_name": "Sam"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id, sends", [("user_member", True), ("user_owner", False)])
async def test_membership_created_welcomes_a_teammate_but_not_the_creator(user_id, sends):
    from routers.webhooks import _handle_organization_membership_created

    with (
        patch("core.services.user_provision._ensure_workspace_member", new=AsyncMock()),
        patch("core.repositories.messaging_repo.ensure_user_stream", new=AsyncMock()),
        patch("core.services.messaging.on_org_member_joined", new=AsyncMock()),
        patch("core.services.messaging.join_default_room", new=AsyncMock()),
        patch("core.services.messaging.join_feed_room", new=AsyncMock()),
        patch("core.services.welcome_email.send_teammate_welcome", new=AsyncMock(return_value="sent")) as teammate,
    ):
        await _handle_organization_membership_created(_membership(user_id))

    if sends:
        teammate.assert_awaited_once_with("org_x", user_id="user_member", email="m@example.com", first_name="Sam")
    else:
        teammate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_teammate_welcome_failure_never_fails_the_webhook():
    from routers.webhooks import _handle_organization_membership_created

    with (
        patch("core.services.user_provision._ensure_workspace_member", new=AsyncMock()),
        patch("core.repositories.messaging_repo.ensure_user_stream", new=AsyncMock()),
        patch("core.services.messaging.on_org_member_joined", new=AsyncMock()),
        patch("core.services.messaging.join_default_room", new=AsyncMock()),
        patch("core.services.messaging.join_feed_room", new=AsyncMock()),
        patch("core.services.welcome_email.send_teammate_welcome", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        await _handle_organization_membership_created(_membership("user_member"))
