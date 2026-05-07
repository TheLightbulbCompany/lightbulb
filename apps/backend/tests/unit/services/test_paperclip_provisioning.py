"""Test the agent name used when seeding a new org's Main Agent."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_provision_org_seeds_agent_named_friday(monkeypatch):
    """The auto-seeded CEO agent for a new org is named 'Friday'."""
    # service_token.mint() reads PAPERCLIP_SERVICE_TOKEN_KEY lazily on each
    # call, so set it before invoking provision_org.
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-service-token-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)  # Fernet expects 32 url-safe-base64 bytes

    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = MagicMock()
    admin._http = MagicMock()  # touched by get_admin_session_cookie
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"})
    admin.create_company = AsyncMock(return_value={"id": "co_xyz"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()
    admin.create_agent = AsyncMock()

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.put = AsyncMock()

    # Stub admin-session helper imported lazily inside provision_org.
    import core.services.paperclip_provisioning as mod
    import core.services.paperclip_admin_session as admin_session_mod

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)
    if hasattr(mod, "get_admin_session_cookie"):
        monkeypatch.setattr(mod, "get_admin_session_cookie", _fake_admin_cookie, raising=False)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        org_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    admin.create_agent.assert_awaited_once()
    assert admin.create_agent.await_args.kwargs["name"] == "Friday"
    assert admin.create_agent.await_args.kwargs["role"] == "ceo"
