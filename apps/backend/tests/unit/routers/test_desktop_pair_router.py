"""HTTP-level tests for the desktop pair / revoke endpoints.

Covers POST /api/v1/desktop/pair and DELETE /api/v1/desktop/devices/{id}.
Uses FastAPI dependency_overrides to bypass Clerk JWT validation, then
patches the service-layer calls so the test stays at the router layer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.config import settings


@pytest.fixture
def desktop_auth_ctx() -> AuthContext:
    return AuthContext(user_id="user_alice", org_id="org_test", email="alice@example.com")


@pytest.fixture
def desktop_client(desktop_auth_ctx):
    from routers.desktop_pair import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _override():
        return desktop_auth_ctx

    app.dependency_overrides[get_current_user] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_post_desktop_pair_returns_pair_result(desktop_client):
    fake_result = MagicMock(
        gateway_url="wss://ws-dev.isol8.co",
        gateway_token="tok_xyz",
        device_id_mac="dev_mac_hash",
        device_id_cli="dev_cli_hash",
        commands_mac=("system.notify", "device.info"),
        commands_cli=("system.run", "browser.proxy"),
    )
    with patch(
        "routers.desktop_pair.pair_desktop_devices",
        AsyncMock(return_value=fake_result),
    ) as pair:
        response = desktop_client.post(
            "/api/v1/desktop/pair",
            headers={"Authorization": "Bearer test"},
            json={
                "tauriMacPubKey": "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
                "cliPubKey": "-----BEGIN PUBLIC KEY-----\nDEF\n-----END PUBLIC KEY-----\n",
                "permissions": {
                    "screen": False,
                    "shell": True,
                    "browser": True,
                    "files": False,
                },
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "gatewayUrl": "wss://ws-dev.isol8.co",
        # relayUrl is the new raw-WS gateway relay door — a settings-sourced
        # constant, not derived from pair_desktop_devices (unlike gatewayUrl
        # above, which the local-session relay still reads via WS_GATEWAY_URL).
        "relayUrl": settings.GATEWAY_RELAY_URL,
        "gatewayToken": "tok_xyz",
        "ownerId": "org_test",
        "deviceIdMac": "dev_mac_hash",
        "deviceIdCli": "dev_cli_hash",
        "commandsMac": ["system.notify", "device.info"],
        "commandsCli": ["system.run", "browser.proxy"],
    }
    permissions = pair.await_args.kwargs["permissions"]
    assert permissions.screen is False
    assert permissions.shell is True
    assert permissions.browser is True
    assert permissions.files is False


def test_post_desktop_pair_pushes_nothing_into_a_runtime(desktop_client):
    """The trust record IS the containers row every reader already consults (``machine_auth`` at
    the WS upgrade, ``declared_commands_for`` at connect), so a pair takes effect on the device's
    very next connect with nothing to propagate.

    It used to also RPC the running container's own trust store, because the gateway read a
    file the boot import was add-only against -- so a pair that missed the live push did not
    take effect until the next boot, and a revoke never evicted at all.
    """
    from routers import desktop_pair

    assert not hasattr(desktop_pair, "push_desktop_pairing_live")
    assert not hasattr(desktop_pair, "push_desktop_revoke_live")


def test_post_desktop_pair_rejects_missing_pubkey(desktop_client):
    response = desktop_client.post(
        "/api/v1/desktop/pair",
        headers={"Authorization": "Bearer test"},
        json={"tauriMacPubKey": "x"},  # missing cliPubKey
    )
    assert response.status_code == 422


def test_post_desktop_pair_returns_400_on_value_error(desktop_client):
    """build_paired_entry / pair_desktop_devices raise ValueError on bad PEM input."""
    with patch(
        "routers.desktop_pair.pair_desktop_devices",
        AsyncMock(side_effect=ValueError("bad pem")),
    ):
        response = desktop_client.post(
            "/api/v1/desktop/pair",
            headers={"Authorization": "Bearer test"},
            json={"tauriMacPubKey": "x", "cliPubKey": "y"},
        )
    assert response.status_code == 400
    assert "bad pem" in response.text


def test_delete_device_204(desktop_client):
    with patch("routers.desktop_pair.revoke_desktop_device", AsyncMock(return_value=True)) as revoke:
        response = desktop_client.delete(
            "/api/v1/desktop/devices/dev_abc",
            headers={"Authorization": "Bearer test"},
        )
    assert response.status_code == 204
    revoke.assert_awaited_once()
    kwargs = revoke.await_args.kwargs
    assert kwargs["device_id"] == "dev_abc"
    assert kwargs["owner_id"] == "org_test"
    # A revoke force-closes the device's live socket by DELETING its presence rows: the relay
    # pins a device's socket to whichever backend instance accepted it, so there is no
    # in-process map to pop -- the relay's own 60s refresh notices the row is gone and closes
    # 1008.
    assert callable(kwargs["force_close_device"])
