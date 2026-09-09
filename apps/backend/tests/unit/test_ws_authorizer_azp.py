"""SEC-8: `azp` (authorized-party) branch of the WebSocket Lambda authorizer.

Same harness pattern as test_ws_authorizer_device.py — the authorizer lives in
apps/infra/lambda/websocket-authorizer but is exercised here so it runs in
backend CI (which has pyjwt[crypto]). Mints real RS256 tokens and stubs the
JWKS client, so the full Clerk decode path runs, not just the azp helper.
"""

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

_AUTHZ_DIR = Path(__file__).resolve().parents[3] / "infra" / "lambda" / "websocket-authorizer"
sys.path.insert(0, str(_AUTHZ_DIR))
import index as authz  # noqa: E402

_ISSUER = "https://clerk.test.isol8.co"
_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(azp=None):
    now = int(time.time())
    payload = {"sub": "user_abc", "iss": _ISSUER, "iat": now, "exp": now + 60}
    if azp is not None:
        payload["azp"] = azp
    return jwt.encode(payload, _PRIV, algorithm="RS256")


def _event(token):
    return {"methodArn": "arn:x", "queryStringParameters": {"token": token}}


def _effect(policy):
    return policy["policyDocument"]["Statement"][0]["Effect"]


class _FakeSigningKey:
    key = _PRIV.public_key()


class _FakeJwksClient:
    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


@pytest.fixture
def clerk_env(monkeypatch):
    """Wire the Clerk branch: issuer, stubbed JWKS, allowlist; returns mode setter."""
    monkeypatch.setattr(authz, "CLERK_ISSUER", _ISSUER)
    monkeypatch.setattr(authz, "get_jwks_client", lambda: _FakeJwksClient())
    monkeypatch.setattr(authz, "CLERK_AUTHORIZED_PARTIES", frozenset({"https://dev.isol8.co"}))

    def _mode(mode: str):
        monkeypatch.setattr(authz, "CLERK_AZP_MODE", mode)

    return _mode


def test_matching_azp_allows_in_enforce(clerk_env):
    clerk_env("enforce")
    policy = authz.handler(_event(_mint(azp="https://dev.isol8.co")), None)
    assert _effect(policy) == "Allow"
    assert policy["context"]["userId"] == "user_abc"
    assert policy["context"]["verifiedIssuer"] == _ISSUER
    assert policy["context"]["verifiedSubject"] == "user_abc"


def test_missing_azp_allows_in_enforce(clerk_env):
    # Native-app tokens carry no azp; must not be bricked.
    clerk_env("enforce")
    assert _effect(authz.handler(_event(_mint()), None)) == "Allow"


def test_mismatched_azp_allows_in_audit(clerk_env):
    clerk_env("audit")
    assert _effect(authz.handler(_event(_mint(azp="https://evil.example")), None)) == "Allow"


def test_mismatched_azp_denies_in_enforce(clerk_env):
    clerk_env("enforce")
    assert _effect(authz.handler(_event(_mint(azp="https://evil.example")), None)) == "Deny"


def test_empty_allowlist_disables_check(clerk_env, monkeypatch):
    clerk_env("enforce")
    monkeypatch.setattr(authz, "CLERK_AUTHORIZED_PARTIES", frozenset())
    assert _effect(authz.handler(_event(_mint(azp="https://evil.example")), None)) == "Allow"
