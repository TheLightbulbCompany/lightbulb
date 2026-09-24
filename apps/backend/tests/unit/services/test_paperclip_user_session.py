import pytest
from unittest.mock import AsyncMock, MagicMock

from core.services.paperclip_user_session import (
    get_user_session_cookie,
    UserSessionError,
)
from core.repositories.paperclip_repo import PaperclipIdentity


@pytest.fixture
def active_company():
    """A settled identity row (the name is kept for the tests below)."""
    return PaperclipIdentity(
        user_id="u1",
        paperclip_user_id="pcu_xyz",
        paperclip_password_encrypted="ENC_PWD",
    )


@pytest.mark.asyncio
async def test_get_user_session_cookie_signs_in_with_decrypted_password(active_company, monkeypatch):
    monkeypatch.setattr("core.services.paperclip_user_session.decrypt", lambda v: "the-password")
    repo = MagicMock()
    repo.get_identity = AsyncMock(return_value=active_company)
    admin_client = MagicMock()
    admin_client.sign_in_user = AsyncMock(return_value={"_session_cookie": "paperclip-default.session_token=AAA"})

    cookie = await get_user_session_cookie(
        user_id="u1",
        repo=repo,
        admin_client=admin_client,
        clerk_email_resolver=AsyncMock(return_value="alice@example.com"),
    )

    # Strong read is load-bearing: a replica-lagged miss right after the row
    # settles burns one of the brief drain's failure attempts.
    repo.get_identity.assert_awaited_once_with("u1", consistent_read=True)
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
    repo.get_identity = AsyncMock(return_value=active_company)
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
    repo.get_identity = AsyncMock(return_value=active_company)
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
async def test_get_user_session_cookie_raises_when_identity_missing():
    repo = MagicMock()
    repo.get_identity = AsyncMock(return_value=None)
    with pytest.raises(UserSessionError, match="no Paperclip identity"):
        await get_user_session_cookie(
            user_id="u1",
            repo=repo,
            admin_client=MagicMock(),
            clerk_email_resolver=AsyncMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["seeding", "failed"])
async def test_get_user_session_cookie_raises_when_identity_unconfirmed(status):
    """A sign-up claim or a pre-identity failure has no Better Auth user id:
    no sign-in is attempted."""
    repo = MagicMock()
    repo.get_identity = AsyncMock(return_value=PaperclipIdentity(user_id="u1", status=status))
    admin_client = MagicMock()
    admin_client.sign_in_user = AsyncMock()
    with pytest.raises(UserSessionError, match="no Paperclip identity"):
        await get_user_session_cookie(
            user_id="u1",
            repo=repo,
            admin_client=admin_client,
            clerk_email_resolver=AsyncMock(),
        )
    admin_client.sign_in_user.assert_not_awaited()
