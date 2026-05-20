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

    repo.get.assert_awaited_once_with("u1")
    admin_client.sign_in_user.assert_awaited_once_with(email="alice@example.com", password="the-password")
    assert cookie == "paperclip-default.session_token=AAA"


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
