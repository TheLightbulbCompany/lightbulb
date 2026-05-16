"""HTTP-level tests for the desktop pair / revoke endpoints.

Covers POST /api/v1/desktop/pair and DELETE /api/v1/desktop/devices/{id}.
Uses FastAPI dependency_overrides to bypass Clerk JWT validation, then
patches the service-layer calls so the test stays at the router layer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user


@pytest.fixture
def desktop_auth_ctx() -> AuthContext:
    return AuthContext(user_id="user_alice", org_id=None, email="alice@example.com")


@pytest.fixture
def desktop_client(desktop_auth_ctx):
    from main import app

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
    ):
        response = desktop_client.post(
            "/api/v1/desktop/pair",
            headers={"Authorization": "Bearer test"},
            json={
                "tauriMacPubKey": "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
                "cliPubKey": "-----BEGIN PUBLIC KEY-----\nDEF\n-----END PUBLIC KEY-----\n",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "gatewayUrl": "wss://ws-dev.isol8.co",
        "gatewayToken": "tok_xyz",
        "deviceIdMac": "dev_mac_hash",
        "deviceIdCli": "dev_cli_hash",
        "commandsMac": ["system.notify", "device.info"],
        "commandsCli": ["system.run", "browser.proxy"],
    }


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
    with patch(
        "routers.desktop_pair.revoke_desktop_device",
        AsyncMock(return_value=True),
    ) as revoke:
        response = desktop_client.delete(
            "/api/v1/desktop/devices/dev_abc",
            headers={"Authorization": "Bearer test"},
        )
    assert response.status_code == 204
    revoke.assert_awaited_once()
    kwargs = revoke.await_args.kwargs
    assert kwargs["device_id"] == "dev_abc"
