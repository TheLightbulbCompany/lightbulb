"""The hosted MCP must not accept a valid Clerk token for another audience or org."""

import base64
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core.services import hosted_mcp


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_oauth_verifier_binds_audience_and_selected_org(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwks = {"keys": [{"kid": "test", "kty": "RSA", "use": "sig", "n": _b64(numbers.n), "e": _b64(numbers.e)}]}
    now = int(time.time())
    monkeypatch.setattr(
        hosted_mcp,
        "settings",
        SimpleNamespace(CLERK_SECRET_KEY="sk_test", CLERK_ISSUER="https://clerk.example.com"),
    )

    async def get_jwks(_):
        return jwks

    async def memberships(*_, **__):
        return [{"id": "org_allowed"}]

    class ClerkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _, **kwargs):
            return httpx.Response(
                200,
                json={
                    "subject": "user_test",
                    "client_id": "client_test",
                    "scopes": ["user:org:read", hosted_mcp.READ_SCOPE],
                    "expiration": now + 300,
                    "revoked": False,
                    "expired": False,
                },
            )

    monkeypatch.setattr(hosted_mcp, "_get_cached_jwks", get_jwks)
    monkeypatch.setattr(hosted_mcp.clerk_admin, "list_user_organizations", memberships)
    monkeypatch.setattr(hosted_mcp.httpx, "AsyncClient", lambda **_: ClerkClient())

    def token(audience: str, org: str) -> str:
        return jwt.encode(
            {"iss": "https://clerk.example.com", "sub": "user_test", "aud": audience, "org_id": org, "exp": now + 300},
            key,
            algorithm="RS256",
            headers={"kid": "test"},
        )

    verifier = hosted_mcp.ClerkOAuthVerifier()
    assert (await verifier.verify_token(token(hosted_mcp.RESOURCE, "org_allowed"))).subject == "user_test"
    assert await verifier.verify_token(token("https://other.example.com/mcp", "org_allowed")) is None
    assert await verifier.verify_token(token(hosted_mcp.RESOURCE, "org_other")) is None
