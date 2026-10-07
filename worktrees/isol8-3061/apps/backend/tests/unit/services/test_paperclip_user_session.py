import pytest
from unittest.mock import AsyncMock, MagicMock

from core.services.paperclip_user_session import (
    get_user_session_cookie,
    UserSessionError,
)
from core.repositories.paperclip_repo import PaperclipCompany
from datetime import datetime, timezone


@pytest.fixture
def active_company():
    return PaperclipCompany(
        user_id="u1",
        org_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        paperclip_password_encrypted="ENC_PWD",
        service_token_encrypted="ENC_TOK",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_get_user_session_cookie_signs_in_with_decrypted_password(active_company, monkeypatch):
    monkeypatch.setattr("core.services.paperclip_user_session.decrypt", lambda v: "the-password")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    admin_client = MagicMock()
    admin_client.sign_in_user = AsyncMock(return_value={"_session_cookie": "paperclip-default.session_token=AAA"})

    cookie = await get_user_session_cookie(
        user_id="u1",
        repo=repo,
        admin_client=admin_client,
        clerk_email_resolver=AsyncMock(return_value="alice@example.com"),
    )

    # Strong read is load-bearing: a replica-lagged miss right after the row
    # flips to active burns one of the brief drain's failure attempts.
    repo.get.assert_awaited_once_with("u1", consistent_read=True)
    admin_client.sign_in_user.assert_awaited_once_with(email="alice@example.com", password="the-password")
    assert cookie == "paperclip-default.session_token=AAA"


@pytest.mark.asyncio
async def test_get_user_session_cookie_prefers_the_stored_paperclip_email(active_company, monkeypatch):
    """Better Auth's user.email is frozen at provision time, so a Clerk
    primary-email change must NOT reach the sign-in — it would 401, fall
    through to sign-UP, and mint a duplicate Better Auth account."""
    monkeypatch.setattr("core.services.paperclip_user_session.decrypt", lambda v: "the-password")
    active_company.paperclip_email = "alice@isol8.co"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    admin_client = MagicMock()
    admin_client.sign_in_user = AsyncMock(return_value={"_session_cookie": "c"})
    clerk_resolver = AsyncMock(return_value="alice@thelightbulb.company")

    await get_user_session_cookie(
        user_id="u1",
        repo=repo,
        admin_client=admin_client,
        clerk_email_resolver=clerk_resolver,
    )

    admin_client.sign_in_user.assert_awaited_once_with(email="alice@isol8.co", password="the-password")
    # A genuine preference, not a silent overwrite: Clerk is never consulted.
    clerk_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_user_session_cookie_falls_back_when_email_unbackfilled(active_company, monkeypatch):
    monkeypatch.setattr("core.services.paperclip_user_session.decrypt", lambda v: "the-password")
    assert active_company.paperclip_email == ""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    admin_client = MagicMock()
    admin_client.sign_in_user = AsyncMock(return_value={"_session_cookie": "c"})

    await get_user_session_cookie(
        user_id="u1",
        repo=repo,
        admin_client=admin_client,
        clerk_email_resolver=AsyncMock(return_value="alice@example.com"),
    )

    admin_client.sign_in_user.assert_awaited_once_with(email="alice@example.com", password="the-password")


@pytest.mark.asyncio
async def test_get_user_session_cookie_raises_when_company_missing():
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    with pytest.raises(UserSessionError, match="not provisioned"):
        await get_user_session_cookie(
            user_id="u1",
            repo=repo,
            admin_client=MagicMock(),
            clerk_email_resolver=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_get_user_session_cookie_raises_when_company_not_active(active_company):
    active_company.status = "provisioning"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=active_company)
    with pytest.raises(UserSessionError, match="not active"):
        await get_user_session_cookie(
            user_id="u1",
            repo=repo,
            admin_client=MagicMock(),
            clerk_email_resolver=AsyncMock(),
        )
