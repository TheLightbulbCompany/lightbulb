"""Device-token branch of the WebSocket Lambda authorizer.

The authorizer lives in apps/infra/lambda/websocket-authorizer, but is exercised
here so it runs in backend CI (which has pyjwt[crypto] + cryptography). Verifies
the EdDSA proof-of-possession path added for desktop device-key WS-upgrade auth.
"""

import base64
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_AUTHZ_DIR = Path(__file__).resolve().parents[3] / "infra" / "lambda" / "websocket-authorizer"
sys.path.insert(0, str(_AUTHZ_DIR))
import index as authz  # noqa: E402


def _raw_b64url(priv: Ed25519PrivateKey) -> str:
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(priv, *, device_id, owner, exp_delta=90, kind="device"):
    now = int(time.time())
    payload = {
        "kind": kind,
        "deviceId": device_id,
        "owner": owner,
        "iat": now,
        "exp": now + exp_delta,
    }
    return jwt.encode(payload, priv, algorithm="EdDSA")


def _event(token):
    return {"methodArn": "arn:x", "queryStringParameters": {"token": token}}


def _effect(policy):
    return policy["policyDocument"]["Statement"][0]["Effect"]


@pytest.fixture
def mock_row(monkeypatch):
    """Point the authorizer at a mocked containers row; return a setter."""
    monkeypatch.setattr(authz, "CONTAINERS_TABLE", "test-containers")
    table = MagicMock()
    monkeypatch.setattr(authz, "_get_containers_table", lambda: table)

    def _set(item):
        table.get_item.return_value = {"Item": item} if item else {}

    return _set


def test_org_owner_allows_as_member(mock_row):
    priv = Ed25519PrivateKey.generate()
    mock_row(
        {
            "owner_id": "org_x",
            "paired_devices": {"dev1": {"publicKey": _raw_b64url(priv), "userId": "usr_alice"}},
        }
    )
    policy = authz.handler(_event(_mint(priv, device_id="dev1", owner="org_x")), None)
    assert _effect(policy) == "Allow"
    # member identity comes from the server-authored row; org owner -> orgId set.
    assert policy["context"] == {
        "userId": "usr_alice",
        "orgId": "org_x",
        "authKind": "device",
        "deviceId": "dev1",
    }


def test_personal_owner_orgid_empty(mock_row):
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(priv), "userId": "usr_solo"}}})
    policy = authz.handler(_event(_mint(priv, device_id="dev1", owner="usr_solo")), None)
    assert _effect(policy) == "Allow"
    assert policy["context"]["orgId"] == ""
    assert policy["context"]["userId"] == "usr_solo"


def test_forged_signature_denied(mock_row):
    """Attacker submits the victim's deviceId but signs with their OWN key; the
    registered (victim's) pubkey won't verify it -> not authorized as the victim."""
    victim = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(victim), "userId": "usr_victim"}}})
    token = _mint(attacker, device_id="dev1", owner="org_x")
    assert authz._try_device_token(token) is None
    assert _effect(authz.handler(_event(token), None)) == "Deny"


def test_expired_denied(mock_row):
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(priv), "userId": "u"}}})
    token = _mint(priv, device_id="dev1", owner="o", exp_delta=-3600)
    assert authz._try_device_token(token) is None


def test_revoked_device_is_denied_after_strong_read(mock_row):
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {}})  # deviceId absent after un-pairing
    token = _mint(priv, device_id="dev1", owner="o")
    assert authz._try_device_token(token) is None
    authz._get_containers_table().get_item.assert_called_once_with(Key={"owner_id": "o"}, ConsistentRead=True)


def test_missing_container_row_denied(mock_row):
    priv = Ed25519PrivateKey.generate()
    mock_row(None)  # no row for this owner
    token = _mint(priv, device_id="dev1", owner="ghost")
    assert authz._try_device_token(token) is None


def test_entry_without_userid_denied(mock_row):
    """Fail closed: a registered entry with no attributed member must not be
    Allow'd as the owner (would masquerade an org id as a personal identity)."""
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(priv)}}})  # no userId
    token = _mint(priv, device_id="dev1", owner="org_x")
    assert authz._try_device_token(token) is None


def test_overlong_lifetime_denied(mock_row):
    """A token declaring a lifetime beyond the ceiling is refused even though
    its signature + exp verify — bounds the replay window at the boundary."""
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(priv), "userId": "u"}}})
    token = _mint(priv, device_id="dev1", owner="o", exp_delta=100_000)
    assert authz._try_device_token(token) is None


def test_missing_iat_denied(mock_row):
    """iat is required (needed for the lifetime cap); a token without it fails."""
    priv = Ed25519PrivateKey.generate()
    mock_row({"paired_devices": {"dev1": {"publicKey": _raw_b64url(priv), "userId": "u"}}})
    now = int(time.time())
    token = jwt.encode(
        {"kind": "device", "deviceId": "dev1", "owner": "o", "exp": now + 90},
        priv,
        algorithm="EdDSA",
    )
    assert authz._try_device_token(token) is None


def test_non_eddsa_token_is_not_treated_as_device(mock_row):
    """HS256/RS256 tokens must not be mistaken for paired devices."""
    hs = jwt.encode({"sub": "u", "kind": "paperclip_service"}, "secret", algorithm="HS256")
    assert authz._try_device_token(hs) is None


def test_device_token_ignored_when_table_unconfigured(monkeypatch):
    monkeypatch.setattr(authz, "CONTAINERS_TABLE", "")
    priv = Ed25519PrivateKey.generate()
    assert authz._try_device_token(_mint(priv, device_id="d", owner="o")) is None
