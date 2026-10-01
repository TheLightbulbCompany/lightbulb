"""Hosted MCP accepts only current, resource-bound Clerk opaque grants."""

import time
from types import SimpleNamespace

import httpx
import pytest

from core.services import hosted_mcp


@pytest.mark.asyncio
async def test_oauth_verifier_binds_audience_org_and_revocation(monkeypatch):
    now = int(time.time())
    grant = {
        "subject": "user_test",
        "client_id": "client_test",
        "scopes": ["user:org:read", hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        "expiration": now + 300,
        "revoked": False,
        "expired": False,
        "aud": [hosted_mcp.RESOURCE],
    }
    identity = {"sub": "user_test", "org_id": "org_allowed"}
    monkeypatch.setattr(
        hosted_mcp,
        "settings",
        SimpleNamespace(CLERK_SECRET_KEY="sk_test", CLERK_ISSUER="https://clerk.example.com"),
    )

    async def memberships(*_, **__):
        return [{"id": "org_allowed"}]

    class ClerkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _, **kwargs):
            return httpx.Response(200, json=grant)

        async def get(self, _, **kwargs):
            return httpx.Response(200, json=identity)

    monkeypatch.setattr(hosted_mcp.clerk_admin, "list_user_organizations", memberships)
    monkeypatch.setattr(hosted_mcp.httpx, "AsyncClient", lambda **_: ClerkClient())
    verifier = hosted_mcp.ClerkOAuthVerifier()

    assert (await verifier.verify_token("opaque-test")).subject == "user_test"
    grant["scopes"].remove(hosted_mcp.WRITE_SCOPE)
    assert (await verifier.verify_token("opaque-test")).subject == "user_test"
    grant["scopes"].append(hosted_mcp.WRITE_SCOPE)
    grant["aud"] = ["https://other.example.com/mcp"]
    assert await verifier.verify_token("opaque-test") is None
    grant["aud"] = [hosted_mcp.RESOURCE]
    identity["org_id"] = "org_other"
    assert await verifier.verify_token("opaque-test") is None
    identity["org_id"] = "org_allowed"
    grant["revoked"] = True
    assert await verifier.verify_token("opaque-test") is None
    assert await verifier.verify_token("jwt.part.signature") is None
