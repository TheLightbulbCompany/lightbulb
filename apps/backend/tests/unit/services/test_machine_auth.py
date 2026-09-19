"""Device authentication and unsupported-client rejection for the node relay."""

import base64
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services import machine_auth

KEY = Ed25519PrivateKey.generate()
PUB = (
    base64.urlsafe_b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    .decode()
    .rstrip("=")
)
DEVICE = "a" * 64
ROW = {"owner_id": "org_1", "paired_devices": {DEVICE: {"publicKey": PUB, "userId": "user_1"}}}


def _device_token(**overrides):
    now = int(time.time())
    claims = {"kind": "device", "deviceId": DEVICE, "owner": "org_1", "iat": now, "exp": now + 90, **overrides}
    return jwt.encode(
        claims,
        KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        algorithm="EdDSA",
    )


@pytest.mark.asyncio
async def test_device_token_yields_a_node_identity():
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        ident = await machine_auth.verify_upgrade(_device_token(), "openclaw-macos")
    assert ident is not None
    assert (ident.kind, ident.owner_id, ident.user_id, ident.org_id, ident.device_id, ident.client_id) == (
        "node",
        "org_1",
        "user_1",
        "org_1",
        DEVICE,
        "openclaw-macos",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,client_id",
    [
        (lambda: _device_token(exp=int(time.time()) + 900), "node-host"),  # lifetime ceiling
        (lambda: _device_token(deviceId="b" * 64), "node-host"),  # unknown device
        (lambda: _device_token(), "paperclip-operator"),  # wrong kind for the clientId
        (lambda: _device_token(), "browser"),  # unknown clientId
    ],
)
async def test_device_token_rejections(token, client_id):
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        assert await machine_auth.verify_upgrade(token(), client_id) is None


# --- Fail-closed userId and clock-skew leeway ---


@pytest.mark.asyncio
async def test_device_entry_missing_user_id_fails_closed():
    """The authorizer forbids the owner-masquerade fallback (index.py:228-234):
    a paired-device entry with no attributed member must never resolve to the
    owner id. verify_upgrade must return None, not `entry.get("userId") or owner_id`."""
    row = {"owner_id": "org_1", "paired_devices": {DEVICE: {"publicKey": PUB}}}
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=row):
        assert await machine_auth.verify_upgrade(_device_token(), "node-host") is None


@pytest.mark.asyncio
async def test_device_token_honors_clock_skew_leeway():
    """A token expired 10s ago is still accepted under the authorizer's
    leeway=30 tolerance for clock skew."""
    now = int(time.time())
    token = _device_token(iat=now - 100, exp=now - 10)
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        ident = await machine_auth.verify_upgrade(token, "node-host")
    assert ident is not None and ident.kind == "node"


# --- Forged signatures, malformed keys, and expired tokens ---


@pytest.mark.asyncio
async def test_device_token_forged_signature_rejected():
    """A token signed with a DIFFERENT key than the one on file must fail
    signature verification even though its claims look otherwise valid —
    the deviceId/owner claims only locate the registered pubkey; they never
    substitute for proof of possession."""
    forged_key = Ed25519PrivateKey.generate()
    now = int(time.time())
    claims = {"kind": "device", "deviceId": DEVICE, "owner": "org_1", "iat": now, "exp": now + 90}
    token = jwt.encode(
        claims,
        forged_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        algorithm="EdDSA",
    )
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        assert await machine_auth.verify_upgrade(token, "node-host") is None


@pytest.mark.asyncio
async def test_device_entry_malformed_public_key_rejected():
    """A stored publicKey that isn't valid base64 must not raise past
    verify_upgrade's "never raises" contract — it returns None."""
    row = {"owner_id": "org_1", "paired_devices": {DEVICE: {"publicKey": "!!!not-b64!!!", "userId": "user_1"}}}
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=row):
        assert await machine_auth.verify_upgrade(_device_token(), "node-host") is None


@pytest.mark.asyncio
async def test_device_token_expired_beyond_leeway_rejected():
    """60s past exp is outside the 30s leeway tolerance, even though the
    declared lifetime (exp - iat) is well under the 300s ceiling."""
    now = int(time.time())
    token = _device_token(iat=now - 200, exp=now - 60)
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        assert await machine_auth.verify_upgrade(token, "node-host") is None


@pytest.mark.asyncio
async def test_operator_upgrade_is_refused_before_any_verification():
    with (
        patch(
            "core.services.service_token.verify",
            side_effect=AssertionError("retired operator verifier must not run"),
        ),
        patch(
            "core.services.machine_auth.container_repo.get_by_owner_id",
            side_effect=AssertionError("operator must not read device state"),
        ),
    ):
        assert await machine_auth.verify_upgrade("svc", "paperclip-operator") is None
