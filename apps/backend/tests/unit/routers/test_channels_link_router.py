"""Tests for channels router — link endpoints + admin delete."""

import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext  # noqa: E402
from core.services.channel_link_service import (  # noqa: E402
    PairingCodeNotFoundError,
    PeerAlreadyLinkedError,
)


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _patch_auth(auth: AuthContext):
    from core.auth import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = lambda: auth
    return lambda: app.dependency_overrides.pop(get_current_user, None)


def _personal_auth(user_id: str = "user_personal") -> AuthContext:
    # Construct AuthContext matching the real signature (NO email param)
    return AuthContext(user_id=f"human_{user_id}", org_id=user_id, org_role="org:admin")


def test_delete_bot_unsupported_provider_returns_400(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        resp = client.delete("/api/v1/channels/link/whatsapp/main")
        assert resp.status_code == 400
    finally:
        cleanup()


def test_link_complete_happy_path(client):
    cleanup = _patch_auth(_personal_auth("user_bob"))
    try:
        with patch(
            "routers.channels.channel_link_service.complete_link",
            AsyncMock(return_value={"status": "linked", "peer_id": "12345"}),
        ) as mock_link:
            resp = client.post(
                "/api/v1/channels/link/telegram/complete",
                json={"agent_id": "main", "code": "XYZ98765"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "linked"
        assert resp.json()["peer_id"] == "12345"
        mock_link.assert_awaited_once()
        kwargs = mock_link.call_args.kwargs
        assert kwargs["owner_id"] == "user_bob"
        assert kwargs["provider"] == "telegram"
        assert kwargs["agent_id"] == "main"
        assert kwargs["code"] == "XYZ98765"
        assert kwargs["member_id"] == "human_user_bob"
    finally:
        cleanup()


def test_link_complete_code_not_found_returns_404(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch(
            "routers.channels.channel_link_service.complete_link",
            AsyncMock(side_effect=PairingCodeNotFoundError("not found")),
        ):
            resp = client.post(
                "/api/v1/channels/link/telegram/complete",
                json={"agent_id": "main", "code": "BAD"},
            )
        assert resp.status_code == 404
    finally:
        cleanup()


def test_link_complete_peer_already_linked_returns_409(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        with patch(
            "routers.channels.channel_link_service.complete_link",
            AsyncMock(side_effect=PeerAlreadyLinkedError("taken")),
        ):
            resp = client.post(
                "/api/v1/channels/link/telegram/complete",
                json={"agent_id": "main", "code": "XYZ98765"},
            )
        assert resp.status_code == 409
    finally:
        cleanup()


def test_link_complete_unsupported_provider_returns_400(client):
    cleanup = _patch_auth(_personal_auth())
    try:
        resp = client.post(
            "/api/v1/channels/link/whatsapp/complete",
            json={"agent_id": "main", "code": "XYZ"},
        )
        assert resp.status_code == 400
    finally:
        cleanup()


def test_link_complete_uses_member_id_not_owner_for_org_callers(client):
    # NOTE: AuthContext has NO email field
    auth = AuthContext(
        user_id="user_bob",
        org_id="org_1",
        org_role="org:member",
    )
    cleanup = _patch_auth(auth)
    try:
        with patch(
            "routers.channels.channel_link_service.complete_link",
            AsyncMock(return_value={"status": "linked", "peer_id": "12345"}),
        ) as mock_link:
            resp = client.post(
                "/api/v1/channels/link/telegram/complete",
                json={"agent_id": "main", "code": "XYZ98765"},
            )
        assert resp.status_code == 200
        kwargs = mock_link.call_args.kwargs
        assert kwargs["owner_id"] == "org_1"
        assert kwargs["member_id"] == "user_bob"
    finally:
        cleanup()


def test_get_links_me_returns_grouped_by_provider(client):
    cleanup = _patch_auth(_personal_auth("user_bob"))
    try:
        fake_config = {
            "channels": {
                "telegram": {
                    "accounts": {
                        "main": {"botToken": "xxx"},
                        "sales": {"botToken": "yyy"},
                    },
                },
                "discord": {
                    "accounts": {},
                },
            },
        }
        fake_links = [
            {
                "owner_id": "user_bob",
                "provider": "telegram",
                "agent_id": "main",
                "peer_id": "12345",
                "member_id": "user_bob",
            },
        ]
        with (
            patch(
                "routers.channels.read_openclaw_config_from_efs",
                AsyncMock(return_value=fake_config),
            ),
            patch(
                "routers.channels.channel_link_repo.query_by_member",
                AsyncMock(return_value=fake_links),
            ),
        ):
            resp = client.get("/api/v1/channels/links/me")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["telegram"]) == 2  # main + sales
        main = next(b for b in body["telegram"] if b["agent_id"] == "main")
        assert main["linked"] is True
        sales = next(b for b in body["telegram"] if b["agent_id"] == "sales")
        assert sales["linked"] is False
        assert body["discord"] == []
        assert body["slack"] == []
        # Personal user is always admin of their own container
        assert body["can_create_bots"] is True
    finally:
        cleanup()


def test_get_links_me_org_member_cannot_create_bots(client):
    auth = AuthContext(
        user_id="user_bob",
        org_id="org_1",
        org_role="org:member",
    )
    cleanup = _patch_auth(auth)
    try:
        with (
            patch(
                "routers.channels.read_openclaw_config_from_efs",
                AsyncMock(return_value={"channels": {"telegram": {"accounts": {}}}}),
            ),
            patch(
                "routers.channels.channel_link_repo.query_by_member",
                AsyncMock(return_value=[]),
            ),
        ):
            resp = client.get("/api/v1/channels/links/me")
        assert resp.status_code == 200
        assert resp.json()["can_create_bots"] is False
    finally:
        cleanup()


def test_delete_link_unlinks_self(client):
    cleanup = _patch_auth(_personal_auth("user_bob"))
    try:
        fake_link = {
            "owner_id": "user_bob",
            "provider": "telegram",
            "agent_id": "main",
            "peer_id": "12345",
            "member_id": "user_bob",
        }
        with (
            patch(
                "routers.channels.channel_link_repo.query_by_member",
                AsyncMock(return_value=[fake_link]),
            ),
            patch(
                "routers.channels.channel_link_repo.delete",
                AsyncMock(),
            ) as mock_delete,
            patch(
                "routers.channels.remove_from_openclaw_config_list",
                AsyncMock(),
            ) as mock_remove,
        ):
            resp = client.delete("/api/v1/channels/link/telegram/main")
        assert resp.status_code == 200
        mock_delete.assert_awaited_once()
        mock_remove.assert_awaited_once()
        call = mock_remove.call_args
        # Path is allowFrom
        assert call[0][1] == ["channels", "telegram", "accounts", "main", "allowFrom"]
    finally:
        cleanup()


def test_admin_delete_bot_sweeps_links_and_config(client):
    auth = AuthContext(
        user_id="admin_a",
        org_id="org_1",
        org_role="org:admin",
    )
    cleanup = _patch_auth(auth)
    try:
        with (
            patch(
                "routers.channels.delete_openclaw_config_path",
                AsyncMock(),
            ) as mock_del_path,
            patch(
                "routers.channels.remove_from_openclaw_config_list",
                AsyncMock(),
            ) as mock_rm_binding,
            patch(
                "routers.channels.delete_provider_level_channel_secrets",
                AsyncMock(),
            ),
            patch(
                "routers.channels.channel_link_repo.sweep_by_owner_provider_agent",
                AsyncMock(return_value=3),
            ) as mock_sweep,
        ):
            resp = client.delete("/api/v1/channels/telegram/sales")
        assert resp.status_code == 200
        mock_del_path.assert_awaited_once()
        mock_rm_binding.assert_awaited_once()
        mock_sweep.assert_awaited_once()
        assert resp.json()["links_swept"] == 3
    finally:
        cleanup()


def test_admin_delete_bot_personal_account_sweeps_links_and_config(client):
    """Personal-account user calling admin_delete_bot also gets the sweep —
    require_org_admin is a no-op pass-through for non-org callers."""
    cleanup = _patch_auth(_personal_auth("user_personal"))
    try:
        with (
            patch(
                "routers.channels.delete_openclaw_config_path",
                AsyncMock(),
            ) as mock_del_path,
            patch(
                "routers.channels.remove_from_openclaw_config_list",
                AsyncMock(),
            ) as mock_rm_binding,
            patch(
                "routers.channels.delete_provider_level_channel_secrets",
                AsyncMock(),
            ),
            patch(
                "routers.channels.channel_link_repo.sweep_by_owner_provider_agent",
                AsyncMock(return_value=2),
            ) as mock_sweep,
        ):
            resp = client.delete("/api/v1/channels/telegram/sales")
        assert resp.status_code == 200
        mock_del_path.assert_awaited_once()
        mock_rm_binding.assert_awaited_once()
        mock_sweep.assert_awaited_once()
        # Personal account: owner_id should equal user_id
        sweep_call = mock_sweep.call_args.kwargs
        assert sweep_call["owner_id"] == "user_personal"
        assert sweep_call["provider"] == "telegram"
        assert sweep_call["agent_id"] == "sales"
        assert resp.json()["links_swept"] == 2
    finally:
        cleanup()


def test_admin_delete_bot_strips_orphan_provider_secrets(client):
    """Deleting a bot also strips any legacy provider-level credential keys
    (channels.<provider>.botToken/appToken/...) so a revoked orphan "[default]"
    account can't keep crash-looping after its real accounts are gone."""
    cleanup = _patch_auth(_personal_auth("user_personal"))
    try:
        with (
            patch("routers.channels.delete_openclaw_config_path", AsyncMock()),
            patch("routers.channels.remove_from_openclaw_config_list", AsyncMock()),
            patch(
                "routers.channels.delete_provider_level_channel_secrets",
                AsyncMock(),
            ) as mock_strip,
            patch(
                "routers.channels.channel_link_repo.sweep_by_owner_provider_agent",
                AsyncMock(return_value=0),
            ),
        ):
            resp = client.delete("/api/v1/channels/slack/sales")
        assert resp.status_code == 200
        mock_strip.assert_awaited_once_with("user_personal", "slack")
    finally:
        cleanup()
