import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import company_stripe  # noqa: E402


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_CONNECT_CLIENT_ID", "ca_test")


@pytest.mark.asyncio
async def test_deauthorize_calls_oauth_with_client_id():
    with patch("core.services.company_stripe.stripe.OAuth.deauthorize") as deauth:
        deauth.return_value = {"stripe_user_id": "acct_existing"}
        assert await company_stripe.deauthorize(account_id="acct_existing") is True
    assert deauth.call_args.kwargs == {"client_id": "ca_test", "stripe_user_id": "acct_existing"}


# --- the exit: deauthorize + release_and_unbind ----------------------------
#
# `release_and_unbind` exists because three call sites (the account wipe, the
# owner's revoke-all, and the disconnect route) each open-coded "deauthorize,
# THEN clear the binding" — an ordering that lost the unbind whenever Stripe
# raised.


@pytest.mark.asyncio
async def test_deauthorize_skips_stripe_when_the_client_id_is_unset(monkeypatch):
    """`STRIPE_CONNECT_CLIENT_ID` was `""` in prod until the Connect app was
    configured. Sending `client_id=""` buys an `invalid_client` error, not a
    revoke — so there is nothing to gain by making the call."""
    monkeypatch.setattr(settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with patch("core.services.company_stripe.stripe.OAuth.deauthorize") as deauth:
        assert await company_stripe.deauthorize(account_id="acct_existing") is False
    deauth.assert_not_called()


@pytest.mark.asyncio
async def test_release_and_unbind_reports_the_revoke_and_clears():
    with (
        patch("core.services.company_stripe.stripe.OAuth.deauthorize"),
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        assert await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_x") is True
    clear.assert_awaited_once_with("org_a", account_id="acct_x")


@pytest.mark.asyncio
async def test_release_and_unbind_clears_the_binding_even_when_stripe_raises():
    """The load-bearing property. A raise used to skip the unbind entirely,
    leaving the row bound to an account we claim to have let go of."""
    with (
        patch("core.services.company_stripe.stripe.OAuth.deauthorize", side_effect=RuntimeError("stripe down")),
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        assert await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_x") is False
    clear.assert_awaited_once_with("org_a", account_id="acct_x")


@pytest.mark.asyncio
async def test_release_and_unbind_clears_without_calling_stripe_when_unconfigured(monkeypatch):
    """An empty client id must never again strand a bound row."""
    monkeypatch.setattr(settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with (
        patch("core.services.company_stripe.stripe.OAuth.deauthorize") as deauth,
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        assert await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_x") is False
    deauth.assert_not_called()
    clear.assert_awaited_once_with("org_a", account_id="acct_x")


@pytest.mark.asyncio
async def test_release_and_unbind_never_closes_or_deletes_a_created_account():
    """A `source="created"` account takes the SAME exit as a connected one.

    Stripe offers no third option: v2 `close` "removes access to the Account
    and its associated resources" (a delete, banned by spec §3.6 — and
    refused anyway for our `losses_collector=stripe` + `dashboard=full`
    shape: `stripe_loss_liable_cannot_be_deleted`), and v1 DELETE excludes
    accounts "that have access to the standard dashboard". `OAuth.deauthorize`
    is the only documented primitive that leaves the owner's account working,
    so it is attempted for both doors — and its outcome never decides the
    unbind. This asserts EXACT calls, so a future `close`/`delete` fails it.
    """
    with (
        patch("core.services.company_stripe.stripe") as stripe_spy,
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_created")
    assert [c[0] for c in stripe_spy.mock_calls] == ["OAuth.deauthorize"]
    clear.assert_awaited_once_with("org_a", account_id="acct_created")


# --- the api_key path ------------------------------------------------------


@pytest.mark.asyncio
async def test_release_and_unbind_for_an_api_key_row_deletes_the_secret_and_never_calls_stripe():
    """No platform grant exists to revoke. Delete our copy of the key FIRST,
    then unbind — the failure mode of a crash between them is "unbound row,
    no key", never "bound row, readable key"."""
    with (
        patch.object(company_stripe, "delete_api_key", AsyncMock()) as delete,
        patch.object(company_stripe, "deauthorize", AsyncMock()) as deauth,
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        released = await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_x", source="api_key")
    assert released is True
    delete.assert_awaited_once_with("org_a")
    deauth.assert_not_awaited()
    clear.assert_awaited_once_with("org_a", account_id="acct_x")


@pytest.mark.asyncio
async def test_release_and_unbind_for_an_api_key_row_keeps_the_binding_when_the_secret_delete_fails():
    """A stranded ROW is recoverable — the owner clicks Disconnect again and
    the delete is retried. A stranded CREDENTIAL behind no row is not. So on a
    failed delete the binding is kept and the caller is told it did not
    release."""
    with (
        patch.object(company_stripe, "delete_api_key", AsyncMock(side_effect=RuntimeError("sm down"))),
        patch.object(company_stripe.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        released = await company_stripe.release_and_unbind(owner_id="org_a", account_id="acct_x", source="api_key")
    assert released is False
    clear.assert_not_awaited()
