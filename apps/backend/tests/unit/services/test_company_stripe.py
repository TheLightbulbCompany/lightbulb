import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import company_stripe  # noqa: E402


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_CONNECT_CLIENT_ID", "ca_test")
    monkeypatch.setattr(
        settings, "STRIPE_COMPANY_REFRESH_URL", "https://dev.example/settings/connections?stripe=refresh"
    )
    monkeypatch.setattr(settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return")
    monkeypatch.setattr(
        settings, "STRIPE_COMPANY_OAUTH_REDIRECT_URL", "https://api-dev.example/api/v1/company/stripe/connect/callback"
    )


def _client_with(accounts_create=None, links_create=None):
    """A stand-in for StripeClient exposing only v2.core.accounts / account_links."""
    accounts = SimpleNamespace(create=accounts_create or MagicMock())
    links = SimpleNamespace(create=links_create or MagicMock())
    return SimpleNamespace(v2=SimpleNamespace(core=SimpleNamespace(accounts=accounts, account_links=links)))


@pytest.mark.asyncio
async def test_create_company_account_uses_v2_merchant_config_and_stripe_fees():
    create = MagicMock(return_value=SimpleNamespace(id="acct_new"))
    with patch("core.services.company_stripe._client", return_value=_client_with(accounts_create=create)):
        account_id = await company_stripe.create_company_account(
            owner_id="org_a", email="founder@example.com", company_name="Northwind Ceramics"
        )
    assert account_id == "acct_new"
    params, options = create.call_args.args[0], create.call_args.kwargs["options"]
    assert params["dashboard"] == "full"
    assert params["contact_email"] == "founder@example.com"
    assert params["display_name"] == "Northwind Ceramics"
    assert params["identity"] == {"country": "us"}
    assert params["configuration"] == {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}
    assert params["defaults"] == {
        "currency": "usd",
        "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
    }
    assert params["metadata"] == {"owner_id": "org_a", "kind": "company"}
    assert options["idempotency_key"].startswith("company_account_create:")


@pytest.mark.asyncio
async def test_create_company_onboarding_link_v2():
    link = MagicMock(return_value=SimpleNamespace(url="https://accounts.stripe.com/r/acct_new#alu_x"))
    with patch("core.services.company_stripe._client", return_value=_client_with(links_create=link)):
        url = await company_stripe.create_company_onboarding_link(account_id="acct_new")
    assert url == "https://accounts.stripe.com/r/acct_new#alu_x"
    params = link.call_args.args[0]
    assert params["account"] == "acct_new"
    assert params["use_case"] == {
        "type": "account_onboarding",
        "account_onboarding": {
            "configurations": ["merchant"],
            "refresh_url": settings.STRIPE_COMPANY_REFRESH_URL,
            "return_url": settings.STRIPE_COMPANY_RETURN_URL,
        },
    }


def test_build_connect_authorize_url():
    url = company_stripe.build_connect_authorize_url(state="st_123")
    assert url.startswith("https://connect.stripe.com/oauth/authorize?")
    assert "client_id=ca_test" in url
    assert "scope=read_write" in url
    assert "state=st_123" in url
    assert "redirect_uri=https%3A%2F%2Fapi-dev.example%2Fapi%2Fv1%2Fcompany%2Fstripe%2Fconnect%2Fcallback" in url


@pytest.mark.asyncio
async def test_exchange_connect_code_returns_account_id():
    with patch("core.services.company_stripe.stripe.OAuth.token") as token:
        token.return_value = {"stripe_user_id": "acct_existing"}
        assert await company_stripe.exchange_connect_code(code="ac_1") == "acct_existing"
    assert token.call_args.kwargs == {"grant_type": "authorization_code", "code": "ac_1"}


@pytest.mark.asyncio
async def test_deauthorize_calls_oauth_with_client_id():
    with patch("core.services.company_stripe.stripe.OAuth.deauthorize") as deauth:
        deauth.return_value = {"stripe_user_id": "acct_existing"}
        assert await company_stripe.deauthorize(account_id="acct_existing") is True
    assert deauth.call_args.kwargs == {"client_id": "ca_test", "stripe_user_id": "acct_existing"}


@pytest.mark.asyncio
async def test_retrieve_status_shape():
    with patch("core.services.company_stripe.stripe.Account.retrieve") as retrieve:
        retrieve.return_value = {"details_submitted": True, "charges_enabled": True, "payouts_enabled": False}
        status = await company_stripe.retrieve_status(account_id="acct_x")
    assert status == {"details_submitted": True, "charges_enabled": True, "payouts_enabled": False}
