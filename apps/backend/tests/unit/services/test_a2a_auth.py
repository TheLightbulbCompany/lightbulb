"""Who calls the A2A door: a session JWT, or an outside client's Clerk OAuth grant for the /a2a audience."""

import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core.auth import AuthContext
from core.services import a2a_auth, hosted_mcp

CIMD = "cimd:https://example/client.json"


def bearer(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture
def clerk(monkeypatch):
    """A fake Clerk: the verify answer is `grant`, userinfo is `identity`, membership is the identity's org."""
    grant = {
        "subject": "user_test",
        "client_id": CIMD,
        "scopes": ["user:org:read", a2a_auth.SEND_SCOPE],
        "expiration": int(time.time()) + 300,
        "revoked": False,
        "expired": False,
        "aud": [a2a_auth.RESOURCE_A2A],
    }
    identity = {"sub": "user_test", "org_id": "org_test_456"}

    class ClerkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _, **__):
            return httpx.Response(200, json=grant)

        async def get(self, _, **__):
            return httpx.Response(200, json=identity)

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test")
    monkeypatch.setattr(hosted_mcp.httpx, "AsyncClient", lambda **_: ClerkClient())
    monkeypatch.setattr(
        hosted_mcp.clerk_admin, "list_user_organizations", AsyncMock(return_value=[{"id": "org_test_456"}])
    )
    return grant, identity


async def test_session_jwt_goes_to_the_session_path(monkeypatch):
    session = AsyncMock(return_value=AuthContext(user_id="user_test", org_id="org_test_456"))
    monkeypatch.setattr(a2a_auth, "get_current_user", session)
    caller = await a2a_auth.a2a_caller(bearer("a.b.c"))
    assert caller.verified_by == "clerk-session" and caller.client_id is None
    assert caller.scopes == {a2a_auth.SEND_SCOPE, a2a_auth.READ_SCOPE_A2A}
    assert caller.auth.user_id == "user_test"


async def test_opaque_token_with_a2a_audience_and_send_scope_is_oauth_caller(clerk):
    caller = await a2a_auth.a2a_caller(bearer("opaque"))
    assert caller.verified_by == "clerk-oauth" and caller.client_id == CIMD
    assert caller.scopes == {"user:org:read", a2a_auth.SEND_SCOPE}
    assert (caller.auth.user_id, caller.auth.org_id) == ("user_test", "org_test_456")


async def test_mcp_audience_only_is_refused_unless_fallback_enabled(clerk, monkeypatch):
    grant, _ = clerk
    grant["aud"] = [hosted_mcp.RESOURCE]
    with pytest.raises(HTTPException) as exc:
        await a2a_auth.a2a_caller(bearer("opaque"))
    assert exc.value.status_code == 401
    monkeypatch.setattr("core.config.settings.A2A_ACCEPT_MCP_AUDIENCE", True)
    assert (await a2a_auth.a2a_caller(bearer("opaque"))).verified_by == "clerk-oauth"
    # Even with the fallback, an MCP grant without an A2A scope stays out.
    grant["scopes"] = ["user:org:read", hosted_mcp.READ_SCOPE]
    with pytest.raises(HTTPException) as exc:
        await a2a_auth.a2a_caller(bearer("opaque"))
    assert exc.value.status_code == 401


async def test_free_account_is_403_workspace_required(clerk):
    _, identity = clerk
    identity.pop("org_id")
    with pytest.raises(HTTPException) as exc:
        await a2a_auth.a2a_caller(bearer("opaque"))
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "workspace_required"


async def test_require_scope_refuses_read_only_for_send():
    caller = a2a_auth.A2ACaller(
        auth=AuthContext(user_id="user_test", org_id="org_test_456"),
        verified_by="clerk-oauth",
        client_id=CIMD,
        scopes=frozenset({"user:org:read", a2a_auth.READ_SCOPE_A2A}),
    )
    a2a_auth.require_scope(caller, a2a_auth.READ_SCOPE_A2A)
    with pytest.raises(HTTPException) as exc:
        a2a_auth.require_scope(caller, a2a_auth.SEND_SCOPE)
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "scope_required"
