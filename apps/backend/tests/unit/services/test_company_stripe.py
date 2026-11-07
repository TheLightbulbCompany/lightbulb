import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    assert params["configuration"] == {
        "merchant": {"capabilities": {"card_payments": {"requested": True}}},
        "developer": {"capabilities": {"projects": {"requested": True}}},
    }
    assert params["defaults"] == {
        "currency": "usd",
        "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
    }
    assert params["metadata"] == {"owner_id": "org_a", "kind": "company"}
    assert options["idempotency_key"].startswith("company_account_create:")


@pytest.mark.asyncio
async def test_services_account_requests_developer_only():
    """merchant would add the payments KYC fields to a provisioning-only account."""
    create = MagicMock(return_value=SimpleNamespace(id="acct_svc"))
    with patch("core.services.company_stripe._client", return_value=_client_with(accounts_create=create)):
        await company_stripe.create_company_account(
            owner_id="org_a", email="f@example.com", company_name="N", kind="company_services"
        )
    params = create.call_args.args[0]
    assert params["configuration"] == {
        "developer": {"capabilities": {"projects": {"requested": True}}},
    }
    # Stripe refuses `dashboard` and `responsibilities` on a non-merchant account.
    assert "dashboard" not in params
    assert params["defaults"] == {"currency": "usd"}


@pytest.mark.asyncio
async def test_create_company_account_omits_empty_contact_email():
    """v2 rejects contact_email="" — send no key at all."""
    create = MagicMock(return_value=SimpleNamespace(id="acct_new"))
    with patch("core.services.company_stripe._client", return_value=_client_with(accounts_create=create)):
        await company_stripe.create_company_account(owner_id="org_a", email="", company_name="Northwind Ceramics")
    assert "contact_email" not in create.call_args.args[0]


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
            "collection_options": {"fields": "currently_due"},
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


@pytest.mark.asyncio
async def test_developer_configuration_matches_the_guide_shape(monkeypatch):
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v2.core.accounts.create = MagicMock(return_value=SimpleNamespace(id="acct_1"))
    with patch.object(mod, "_client", return_value=client):
        await mod.create_company_account(owner_id="org_a", email="f@example.com", company_name="Northwind")
    params = client.v2.core.accounts.create.call_args.args[0]
    # Verbatim from the guide's "Create the connected account" example.
    assert params["configuration"]["developer"] == {"capabilities": {"projects": {"requested": True}}}
    assert params["include"] == ["configuration.developer", "requirements", "identity"]
    # We keep dashboard: full — the owner logs into their own Stripe (spec §3.1);
    # the guide's example uses "none" because Forge's tenants never do.
    assert params["dashboard"] == "full"
    assert params["configuration"]["merchant"]["capabilities"]["card_payments"]["requested"] is True


@pytest.mark.asyncio
async def test_onboarding_link_uses_collection_options(monkeypatch):
    """On the provisioning preview `configurations` is an unknown field; the
    guide's collection_options shape is what the sandbox accepts."""
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v2.core.account_links.create = MagicMock(return_value=SimpleNamespace(url="https://x"))
    with patch.object(mod, "_client", return_value=client):
        await mod.create_company_onboarding_link(account_id="acct_1")
    onboarding = client.v2.core.account_links.create.call_args.args[0]["use_case"]["account_onboarding"]
    assert "configurations" not in onboarding
    assert onboarding["collection_options"] == {"fields": "currently_due"}


@pytest.mark.asyncio
async def test_accounts_v2_version_pins_the_preview_unless_configured(monkeypatch):
    """The developer configuration exists only in the provisioning preview: the
    SDK default answered "configuration.developer: Unknown field" in the dev
    sandbox. An explicit STRIPE_ACCOUNTS_V2_VERSION wins."""
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v2.core.accounts.create = MagicMock(return_value=SimpleNamespace(id="acct_1"))
    with patch.object(mod, "_client", return_value=client):
        monkeypatch.setattr(mod.settings, "STRIPE_ACCOUNTS_V2_VERSION", "")
        await mod.create_company_account(owner_id="org_a", email="f@example.com", company_name="N")
        options = client.v2.core.accounts.create.call_args.kwargs["options"]
        assert options["stripe_version"] == "2026-09-30.preview"
        assert options["idempotency_key"].startswith("company_account_create:")

        monkeypatch.setattr(mod.settings, "STRIPE_ACCOUNTS_V2_VERSION", "2026-08-26.dahlia")
        await mod.create_company_account(owner_id="org_a", email="f@example.com", company_name="N")
        options = client.v2.core.accounts.create.call_args.kwargs["options"]
        assert options["stripe_version"] == "2026-08-26.dahlia"


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
async def test_validate_api_key_proves_the_key_learns_the_account_and_probes_every_read(monkeypatch):
    monkeypatch.setattr(company_stripe.settings, "STRIPE_SECRET_KEY", "sk_live_platform")
    with (
        patch("core.services.company_stripe.stripe.Account.retrieve") as retrieve,
        patch.object(company_stripe, "_probe_read_permissions") as probe,
    ):
        retrieve.return_value = {
            "id": "acct_theirs",
            "details_submitted": True,
            "charges_enabled": True,
            "payouts_enabled": True,
        }
        info = await company_stripe.validate_api_key("  rk_live_abc  ")
    retrieve.assert_called_once_with(api_key="rk_live_abc")
    probe.assert_called_once_with("rk_live_abc")
    assert info == {
        "account_id": "acct_theirs",
        "livemode": True,
        "details_submitted": True,
        "charges_enabled": True,
        "payouts_enabled": True,
    }


@pytest.mark.asyncio
async def test_validate_api_key_refuses_publishable_and_garbage_before_calling_stripe():
    with patch("core.services.company_stripe.stripe.Account.retrieve") as retrieve:
        for bad in ("pk_live_abc", "", "whsec_x", "acct_x"):
            with pytest.raises(company_stripe.InvalidApiKey):
                await company_stripe.validate_api_key(bad)
    retrieve.assert_not_called()


def test_probe_read_permissions_names_the_missing_permission():
    """A key with only Account:Read passes Account.retrieve; the probe is what
    catches it, and it must say WHICH read is missing."""
    import stripe as stripe_mod

    class _Client:
        class v1:  # noqa: N801 — mirrors the SDK's attribute shape
            class charges:  # noqa: N801
                @staticmethod
                def list(params):
                    return {"data": []}

            class customers:  # noqa: N801
                @staticmethod
                def list(params):
                    raise stripe_mod.error.PermissionError("nope")

    with patch.object(company_stripe, "StripeClient", return_value=_Client()):
        with pytest.raises(company_stripe.InvalidApiKey, match="Customers"):
            company_stripe._probe_read_permissions("rk_live_abc")


@pytest.mark.asyncio
async def test_api_key_lives_in_secrets_manager_under_the_company_stripe_provider():
    store = company_stripe.integration_secret_store
    with (
        patch.object(store, "put", AsyncMock()) as put,
        patch.object(store, "get", AsyncMock(return_value={"api_key": "rk_live_abc"})) as get,
        patch.object(store, "delete", AsyncMock()) as delete,
    ):
        await company_stripe.store_api_key("org_a", " rk_live_abc ")
        assert await company_stripe.load_api_key("org_a") == "rk_live_abc"
        await company_stripe.delete_api_key("org_a")
    put.assert_awaited_once_with("org_a", "company_stripe", {"api_key": "rk_live_abc"})
    get.assert_awaited_once_with("org_a", "company_stripe")
    delete.assert_awaited_once_with("org_a", "company_stripe")


@pytest.mark.asyncio
async def test_retrieve_status_uses_the_owners_key_when_given():
    with patch("core.services.company_stripe.stripe.Account.retrieve") as retrieve:
        retrieve.return_value = {"details_submitted": True, "charges_enabled": False, "payouts_enabled": False}
        status = await company_stripe.retrieve_status(account_id="acct_x", api_key="rk_live_abc")
    retrieve.assert_called_once_with(api_key="rk_live_abc")
    # The mode rides along so the card can say "Test mode" for an rk_test_ key.
    assert status == {"details_submitted": True, "charges_enabled": False, "payouts_enabled": False, "livemode": True}


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


@pytest.mark.asyncio
async def test_an_onboarded_account_gets_the_account_update_link():
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v2.core.account_links.create = MagicMock(return_value=SimpleNamespace(url="https://x"))
    with patch.object(mod, "_client", return_value=client):
        await mod.create_company_onboarding_link(account_id="acct_1", update=True)
    use_case = client.v2.core.account_links.create.call_args.args[0]["use_case"]
    assert use_case["type"] == "account_update"
    assert set(use_case["account_update"]) == {"refresh_url", "return_url"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ready", [("active", True), ("restricted", False), (None, False)])
async def test_projects_ready_reads_the_developer_capability(status, ready):
    from core.services import company_stripe as mod

    caps = {"projects": {"status": status}} if status else {}
    client = MagicMock()
    client.v2.core.accounts.retrieve = MagicMock(return_value={"configuration": {"developer": {"capabilities": caps}}})
    with patch.object(mod, "_client", return_value=client):
        assert await mod.projects_ready(account_id="acct_1") is ready
    assert client.v2.core.accounts.retrieve.call_args.args[1] == {
        "include": ["configuration.developer", "requirements"]
    }


@pytest.mark.asyncio
async def test_request_projects_capability_adds_the_developer_configuration():
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v2.core.accounts.update = MagicMock(return_value=SimpleNamespace(id="acct_1"))
    with patch.object(mod, "_client", return_value=client):
        await mod.request_projects_capability(account_id="acct_1")
    args = client.v2.core.accounts.update.call_args.args
    assert args[0] == "acct_1"
    assert args[1] == {"configuration": {"developer": {"capabilities": {"projects": {"requested": True}}}}}


@pytest.mark.asyncio
async def test_account_session_enables_account_onboarding():
    from core.services import company_stripe as mod

    client = MagicMock()
    client.v1.account_sessions.create = MagicMock(return_value=SimpleNamespace(client_secret="cs_1"))
    with patch.object(mod, "_client", return_value=client):
        assert await mod.create_account_session(account_id="acct_1") == "cs_1"
    assert client.v1.account_sessions.create.call_args.args[0] == {
        "account": "acct_1",
        "components": {"account_onboarding": {"enabled": True}},
    }


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"company_stripe_account_id": "acct_m", "company_stripe_source": "created"}, "acct_m"),
        ({"company_stripe_account_id": "acct_m", "company_stripe_source": "connected"}, None),
        ({"company_stripe_account_id": "acct_m", "company_stripe_source": "api_key"}, None),
        (
            {
                "company_stripe_account_id": "acct_m",
                "company_stripe_source": "connected",
                "company_stripe_projects_account_id": "acct_s",
            },
            "acct_s",
        ),
        (None, None),
    ],
)
def test_projects_account_of(row, expected):
    from core.services import company_stripe as mod

    assert mod.projects_account_of(row) == expected
