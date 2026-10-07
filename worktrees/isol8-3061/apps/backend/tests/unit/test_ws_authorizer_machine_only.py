"""Machine-only admission for the API Gateway WebSocket authorizer."""

import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

_AUTHZ_DIR = Path(__file__).resolve().parents[3] / "infra" / "lambda" / "websocket-authorizer"
sys.path.insert(0, str(_AUTHZ_DIR))
import index as authz  # noqa: E402


def _event(token=None):
    query = {"token": token} if token is not None else {}
    return {"methodArn": "arn:test", "queryStringParameters": query}


def _effect(policy):
    return policy["policyDocument"]["Statement"][0]["Effect"]


def test_valid_clerk_browser_jwt_is_denied_without_jwks_lookup():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user_browser",
            "iss": "https://clerk.example.test",
            "iat": now,
            "exp": now + 60,
        },
        private_key,
        algorithm="RS256",
    )
    assert _effect(authz.handler(_event(token), None)) == "Deny"


def test_signed_paperclip_service_token_is_now_denied():
    """Paperclip uses the HTTP facade. Neither the API Gateway authorizer
    nor the raw desktop-node WS relay accepts its service tokens."""
    assert not hasattr(authz, "_try_service_token")
    assert not hasattr(authz, "PAPERCLIP_SERVICE_TOKEN_KEY")
    token = jwt.encode(
        {
            "sub": "owner_123",
            "kind": "paperclip_service",
            "exp": int(time.time()) + 60,
        },
        "paperclip-test-signing-key",
        algorithm="HS256",
    )
    assert _effect(authz.handler(_event(token), None)) == "Deny"


def test_missing_or_unknown_token_is_denied():
    assert _effect(authz.handler(_event(), None)) == "Deny"
    assert _effect(authz.handler(_event("not-a-jwt"), None)) == "Deny"
