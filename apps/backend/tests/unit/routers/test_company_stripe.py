import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.services.paperclip_admin_client import PaperclipApiError  # noqa: E402
from main import app  # noqa: E402
from routers import company_stripe as mod  # noqa: E402


# A paid row: the callback re-checks the plan before it binds.
_PAID_ROW = {"owner_id": "org_a", "subscription_status": "active"}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin", org_id="org_a", org_role="org:admin", email="founder@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_member", org_id="org_a", org_role="org:member", email="m@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _owner_lifecycle_lease(monkeypatch):
    """The callback binds under the owner lease and checks revoke-all's mark: admit both."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(container_repo, "get_oauth_revoked_at", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


@pytest.fixture(autouse=True)
def _connect_configured(monkeypatch):
    """The platform client id is set in both real environments; the two
    unconfigured-environment tests clear it explicitly."""
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "ca_test")


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    """The container reads plan/Stripe state off the facts file, so every
    mutation refreshes it. Stubbed here (it writes to EFS) and asserted on."""
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


@pytest.fixture(autouse=True)
def metric(monkeypatch):
    """put_metric is sync (EMF print, not a network call) — stub it and let
    each test assert on when the create door actually mints a new account."""
    emitted = MagicMock()
    monkeypatch.setattr(mod, "put_metric", emitted)
    return emitted


def test_status_unbound(client):
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "account_id": None,
        "source": None,
        "details_submitted": False,
        "charges_enabled": False,
        "payouts_enabled": False,
        "livemode": None,
        "dashboard_url": None,
        "connect_configured": True,
        "provisioning_enabled": False,
    }


def test_status_reports_connect_unconfigured(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json()["connect_configured"] is False


def test_connect_start_409_when_unconfigured(client, monkeypatch):
    """Fail closed BEFORE the state row is written — an authorize URL with an
    empty client_id is a dead end the owner has to discover at Stripe."""
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with patch.object(mod.oauth_state_repo, "put", AsyncMock()) as put:
        resp = client.get("/api/v1/company/stripe/connect/start")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stripe_connect_not_configured"
    put.assert_not_awaited()


def test_disconnect_409_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock()) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stripe_connect_not_configured"
    deauth.assert_not_awaited()
    clear.assert_not_awaited()


def test_create_binds_and_returns_link(client, facts, metric):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_new")) as create,
        patch.object(
            mod.billing_repo,
            "set_company_stripe_account",
            AsyncMock(return_value={"company_stripe_account_id": "acct_new"}),
        ) as bind,
        patch.object(
            mod.company_stripe,
            "create_company_onboarding_link",
            AsyncMock(return_value="https://connect.stripe.com/setup/x"),
        ),
        patch.object(
            mod.company_stripe,
            "retrieve_status",
            AsyncMock(return_value={"details_submitted": False, "charges_enabled": False, "payouts_enabled": False}),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value={})),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "acct_new", "onboarding_url": "https://connect.stripe.com/setup/x"}
    facts.assert_awaited_once_with("org_a")
    create.assert_awaited_once_with(owner_id="org_a", email="founder@example.com", company_name="Northwind Ceramics")
    bind.assert_awaited_once_with("org_a", account_id="acct_new", source="created")
    metric.assert_called_once_with("company_stripe.account.created")


def _revoked_after_entry(monkeypatch):
    """revoke-all's mark lands after the request began: one millisecond past its entry time."""
    import time as _time

    from core.repositories import container_repo

    start = int(_time.time() * 1000)
    monkeypatch.setattr(container_repo, "get_oauth_revoked_at", AsyncMock(return_value=start + 60_000))


def test_create_that_began_before_revoke_all_binds_nothing(client, monkeypatch):
    _revoked_after_entry(monkeypatch)
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "resolve_company_name", AsyncMock(return_value="N")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_new")),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "revoked"
    bind.assert_not_awaited()


def test_api_key_that_began_before_revoke_all_stores_nothing(client, monkeypatch):
    _revoked_after_entry(monkeypatch)
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock(return_value=_KEY_INFO)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
        patch.object(mod.company_stripe, "store_api_key", AsyncMock()) as store,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "revoked"
    bind.assert_not_awaited()
    store.assert_not_awaited()


def test_create_reuses_existing_created_account(client, metric):
    """Idempotent path: no account is minted, so no metric either."""
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_old", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock()) as create,
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock(return_value="https://x")),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acct_old"
    create.assert_not_awaited()
    metric.assert_not_called()


def test_create_lost_bind_race_adopts_the_winner(client, metric):
    """set_company_stripe_account -> None with a bound row means a concurrent
    create won; onboard the winner, not our orphan shell. We didn't mint
    anything here, so no metric either — the winner's own request emitted it."""
    with (
        patch.object(
            mod.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                side_effect=[
                    {"owner_id": "org_a"},
                    {"owner_id": "org_a", "company_stripe_account_id": "acct_winner"},
                ]
            ),
        ),
        patch.object(mod.company_stripe, "resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_orphan")),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock(return_value="https://x")) as link,
        patch.object(
            mod.company_stripe,
            "retrieve_status",
            AsyncMock(return_value={"details_submitted": False, "charges_enabled": False, "payouts_enabled": False}),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value={})),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acct_winner"
    link.assert_awaited_once_with(account_id="acct_winner")
    metric.assert_not_called()


def test_create_409s_when_there_is_no_billing_row(client):
    """The same None also means "no billing row". Handing back the orphan's
    onboarding link would give the owner an account nothing can look up."""
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(side_effect=[{"owner_id": "org_a"}, None])),
        patch.object(mod.company_stripe, "resolve_company_name", AsyncMock(return_value="Northwind")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_orphan")),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock()) as link,
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_billing_row"
    link.assert_not_awaited()


def test_create_refuses_when_connected_account_bound(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_theirs", "company_stripe_source": "connected"}
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 409


def test_create_requires_org_admin(member_client):
    resp = member_client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 403


def test_create_requires_paid(client, monkeypatch):
    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(
            side_effect=HTTPException(
                status_code=402, detail={"blocked": True, "code": "routines_require_subscription"}
            )
        ),
    )
    resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 402


def test_connect_start_stores_state(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.oauth_state_repo, "put", AsyncMock()) as put,
        patch.object(
            mod.company_stripe,
            "build_connect_authorize_url",
            return_value="https://connect.stripe.com/oauth/authorize?x",
        ),
    ):
        resp = client.get("/api/v1/company/stripe/connect/start")
    assert resp.status_code == 200
    assert resp.json() == {"authorize_url": "https://connect.stripe.com/oauth/authorize?x"}
    kwargs = put.call_args.kwargs
    assert kwargs["owner_id"] == "org_a"
    assert kwargs["provider"] == "stripe_company"


def test_connect_callback_binds_and_redirects(client, monkeypatch, facts):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value={})) as bind,
        patch.object(
            mod.company_stripe,
            "retrieve_status",
            AsyncMock(return_value={"details_submitted": True, "charges_enabled": True, "payouts_enabled": True}),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value={})) as set_status,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://dev.example/settings/connections?stripe=return&stripe_connect=connected"
    bind.assert_awaited_once_with("org_a", account_id="acct_theirs", source="connected")
    # An existing account is usually already live; without this pull the page
    # would show "Onboarding incomplete" until a webhook happened to fire.
    set_status.assert_awaited_once_with(
        "org_a", account_id="acct_theirs", details_submitted=True, charges_enabled=True, payouts_enabled=True
    )
    facts.assert_awaited_once_with("org_a")


def test_connect_callback_started_before_revoke_all_binds_nothing(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    state = {"owner_id": "org_a", "provider": "stripe_company", "ttl": 1, "created_at_ms": 100}
    with (
        patch.object(mod.oauth_state_repo, "consume", AsyncMock(return_value=state)),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch("core.repositories.container_repo.get_oauth_revoked_at", AsyncMock(return_value=200)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.headers["location"].endswith("&stripe_error=revoked")
    bind.assert_not_awaited()
    deauth.assert_awaited_once_with(account_id="acct_theirs")


def test_connect_callback_already_bound_releases_the_authorization(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=already_bound")
    deauth.assert_awaited_once_with(account_id="acct_theirs")


def test_connect_callback_already_bound_redirects_even_if_release_fails(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(side_effect=RuntimeError("stripe down"))),
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=already_bound")


def test_connect_callback_refuses_an_account_owned_by_someone_else(client, monkeypatch):
    """No deauthorize: that authorization belongs to the OTHER owner and
    revoking it would disconnect a working account."""
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW)),
        patch.object(
            mod.billing_repo,
            "get_by_company_stripe_account_id",
            AsyncMock(return_value={"owner_id": "org_other", "company_stripe_account_id": "acct_theirs"}),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
        patch.object(mod.company_stripe, "deauthorize", AsyncMock()) as deauth,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=account_in_use")
    bind.assert_not_awaited()
    deauth.assert_not_awaited()


def test_connect_callback_releases_when_the_plan_lapsed(client, monkeypatch):
    """connect/start gated on the plan, but the state row outlives the click."""
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with (
        patch.object(
            mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})
        ),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(
            mod.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "subscription_status": "trial_local"}),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=plan")
    bind.assert_not_awaited()
    deauth.assert_awaited_once_with(account_id="acct_theirs")


def test_connect_callback_bad_state_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(
        mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return"
    )
    with patch.object(mod.oauth_state_repo, "consume", AsyncMock(return_value=None)):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=bad", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=state")


def test_create_maps_stripe_error_to_502_and_does_not_bind(client):
    import stripe as stripe_mod

    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
        patch.object(
            mod.company_stripe,
            "create_company_account",
            AsyncMock(side_effect=stripe_mod.error.InvalidRequestError("bad country", "country")),
        ),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "stripe_error"
    bind.assert_not_awaited()


def test_disconnect_unbinds_even_when_stripe_errors(client, facts):
    """A Stripe error used to 502 with the row still bound — and this fixture
    is the case that made it permanent: a `created` account was minted through
    Accounts v2 and was never an OAuth grant, so Stripe can answer
    `invalid_client` ("isn't connected to your application") every single
    time, leaving the owner no way to unbind. The unbind is unconditional now;
    the unconfirmed revoke is reported rather than swallowed."""
    import stripe as stripe_mod

    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(
            mod.company_stripe, "deauthorize", AsyncMock(side_effect=stripe_mod.error.APIConnectionError("down"))
        ),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True, "stripe_released": False}
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
    facts.assert_awaited_once_with("org_a")


def test_disconnect_deauthorizes_and_clears(client, facts):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True, "stripe_released": True}
    deauth.assert_awaited_once_with(account_id="acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
    facts.assert_awaited_once_with("org_a")


def test_request_from_container_files_approval(client):
    with (
        patch.object(mod, "_require_hook_token", AsyncMock(return_value={})),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch(
            "core.services.company_stripe_approval.request_company_stripe",
            AsyncMock(return_value="apr_new"),
        ),
    ):
        resp = client.post(
            "/api/v1/company/stripe/u/org_a/request",
            json={"issue_id": "iss_1", "reason": "x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"approval_id": "apr_new", "status": "filed"}


def test_request_from_container_already_connected(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x"}
    with (
        patch.object(mod, "_require_hook_token", AsyncMock(return_value={})),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
    ):
        resp = client.post(
            "/api/v1/company/stripe/u/org_a/request",
            json={"issue_id": "iss_1", "reason": "x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"approval_id": None, "status": "connected"}


def test_request_from_container_maps_paperclip_4xx_to_502(client):
    """A 400 means the board REFUSED the card. Reporting it as an outage tells
    the agent to retry something that can never succeed."""
    with (
        patch.object(mod, "_require_hook_token", AsyncMock(return_value={})),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(
            mod.company_stripe_approval,
            "request_company_stripe",
            AsyncMock(side_effect=PaperclipApiError("POST /approvals -> 400", 400, {"error": "invalid type"})),
        ),
    ):
        resp = client.post(
            "/api/v1/company/stripe/u/org_a/request",
            json={"issue_id": "iss_1", "reason": "x"},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "paperclip_rejected"
    assert resp.json()["detail"]["message"] == "POST /approvals -> 400"


def test_request_from_container_no_paperclip_company(client):
    with (
        patch.object(mod, "_require_hook_token", AsyncMock(return_value={})),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe_approval, "request_company_stripe", AsyncMock(return_value=None)),
    ):
        resp = client.post(
            "/api/v1/company/stripe/u/org_a/request",
            json={"issue_id": "iss_1", "reason": "x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"approval_id": None, "status": "no_company"}


def test_request_from_container_maps_paperclip_5xx_to_503(client):
    with (
        patch.object(mod, "_require_hook_token", AsyncMock(return_value={})),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch(
            "core.services.company_stripe_approval.request_company_stripe",
            AsyncMock(side_effect=PaperclipApiError("boom", 502, None)),
        ),
    ):
        resp = client.post(
            "/api/v1/company/stripe/u/org_a/request",
            json={"issue_id": "iss_1", "reason": "x"},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "paperclip_unavailable"


# --- the api_key door -------------------------------------------------------


_KEY_INFO = {
    "account_id": "acct_theirs",
    "livemode": True,
    "details_submitted": True,
    "charges_enabled": True,
    "payouts_enabled": False,
}


def test_api_key_binds_first_then_stores_the_key_and_syncs_status(client, facts, metric):
    """Bind FIRST — the conditional write is the lock, so a losing concurrent
    bind never touches the secret. A bound row whose key has not landed yet
    reads as unbound, so a crash between the two writes fails safe."""
    calls: list[str] = []
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock(return_value=_KEY_INFO)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(
            mod.company_stripe, "store_api_key", AsyncMock(side_effect=lambda *a, **k: calls.append("store"))
        ) as store,
        patch.object(
            mod.billing_repo,
            "set_company_stripe_account",
            AsyncMock(
                side_effect=lambda *a, **k: (calls.append("bind"), {"company_stripe_account_id": "acct_theirs"})[1]
            ),
        ) as bind,
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock()) as status,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "acct_theirs", "livemode": True}
    assert calls == ["bind", "store"]
    store.assert_awaited_once_with("org_a", "rk_live_abc")
    bind.assert_awaited_once_with("org_a", account_id="acct_theirs", source="api_key")
    status.assert_awaited_once_with(
        "org_a",
        account_id="acct_theirs",
        details_submitted=True,
        charges_enabled=True,
        payouts_enabled=False,
        livemode=True,
    )
    metric.assert_called_once_with("company_stripe.account.api_key_bound")
    facts.assert_awaited_once_with("org_a")


def test_api_key_422_on_a_malformed_key_without_storing(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(
            mod.company_stripe, "validate_api_key", AsyncMock(side_effect=mod.company_stripe.InvalidApiKey("nope"))
        ),
        patch.object(mod.company_stripe, "store_api_key", AsyncMock()) as store,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "pk_live_abc"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_api_key"
    store.assert_not_awaited()


def test_api_key_422_when_stripe_rejects_the_key(client):
    import stripe as stripe_mod

    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(
            mod.company_stripe,
            "validate_api_key",
            AsyncMock(side_effect=stripe_mod.error.AuthenticationError("Invalid API Key provided")),
        ),
        patch.object(mod.company_stripe, "store_api_key", AsyncMock()) as store,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_wrong"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_api_key"
    store.assert_not_awaited()


def test_api_key_409_when_the_account_belongs_to_another_owner(client):
    """Same cross-owner fence as the OAuth callback. Neither row is touched
    and the key is never stored."""
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock(return_value=_KEY_INFO)),
        patch.object(
            mod.billing_repo,
            "get_by_company_stripe_account_id",
            AsyncMock(return_value={"owner_id": "org_OTHER", "company_stripe_account_id": "acct_theirs"}),
        ),
        patch.object(mod.company_stripe, "store_api_key", AsyncMock()) as store,
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock()) as bind,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_in_use"
    store.assert_not_awaited()
    bind.assert_not_awaited()


def test_api_key_409_when_already_bound_and_never_validates(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "connected"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock()) as validate,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 409
    validate.assert_not_awaited()


def test_api_key_lost_bind_race_never_touches_the_secret(client):
    """set_company_stripe_account -> None: the loser of a concurrent bind must
    not store OR delete — either would clobber the winner's key."""
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock(return_value=_KEY_INFO)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "store_api_key", AsyncMock()) as store,
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value=None)),
        patch.object(mod.company_stripe, "delete_api_key", AsyncMock()) as delete,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 409
    store.assert_not_awaited()
    delete.assert_not_awaited()


def test_api_key_vault_failure_unbinds_and_502s(client, facts):
    """The vault write failed after we won the bind: the binding must not
    outlive the key it points at."""
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "validate_api_key", AsyncMock(return_value=_KEY_INFO)),
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value={"x": 1})),
        patch.object(mod.company_stripe, "store_api_key", AsyncMock(side_effect=RuntimeError("sm down"))),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "vault_error"
    clear.assert_awaited_once_with("org_a", account_id="acct_theirs")
    facts.assert_not_awaited()


def test_disconnect_502s_and_keeps_the_binding_when_the_key_delete_fails(client, facts):
    """release_and_unbind returned False for an api_key row: the vault delete
    failed and the row was KEPT so Disconnect can be retried. The response must
    not claim a disconnect happened."""
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "api_key"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "release_and_unbind", AsyncMock(return_value=False)) as release,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "key_delete_failed"
    release.assert_awaited_once_with(owner_id="org_a", account_id="acct_x", source="api_key")
    facts.assert_not_awaited()


def test_api_key_door_is_admin_only(member_client):
    with patch.object(mod.company_stripe, "validate_api_key", AsyncMock()) as validate:
        resp = member_client.post("/api/v1/company/stripe/api-key", json={"api_key": "rk_live_abc"})
    assert resp.status_code == 403
    validate.assert_not_awaited()


def test_status_refreshes_an_api_key_row_on_read(client):
    """No Connect relationship ⇒ no account.updated ⇒ the stored flags are
    bind-time only. Re-read with THEIR key on every status GET."""
    row = {
        "owner_id": "org_a",
        "company_stripe_account_id": "acct_x",
        "company_stripe_source": "api_key",
        "company_stripe_charges_enabled": False,
    }
    fresh = {**row, "company_stripe_charges_enabled": True}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "load_api_key", AsyncMock(return_value="rk_live_abc")),
        patch.object(
            mod.company_stripe,
            "retrieve_status",
            AsyncMock(return_value={"details_submitted": True, "charges_enabled": True, "payouts_enabled": False}),
        ) as retrieve,
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value=fresh)),
    ):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json()["charges_enabled"] is True
    retrieve.assert_awaited_once_with(account_id="acct_x", api_key="rk_live_abc")


def test_status_serves_the_stored_copy_when_the_refresh_fails(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "api_key"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "load_api_key", AsyncMock(side_effect=RuntimeError("sm down"))),
    ):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acct_x"


def test_status_does_not_refresh_connect_rows(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "connected"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "retrieve_status", AsyncMock()) as retrieve,
    ):
        client.get("/api/v1/company/stripe/status")
    retrieve.assert_not_awaited()


def test_disconnect_api_key_row_needs_no_client_id(client, facts, monkeypatch):
    """The unconfigured-Connect 409 only guards Connect rows — an api_key row
    has nothing at Stripe to revoke."""
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "api_key"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "release_and_unbind", AsyncMock(return_value=True)) as release,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
    release.assert_awaited_once_with(owner_id="org_a", account_id="acct_x", source="api_key")
    facts.assert_awaited_once_with("org_a")


def test_status_reports_the_key_mode_so_the_card_can_label_test_data():
    """An api_key row bound with an rk_test_ key says livemode=False; a
    Connect row (mode follows the platform) and an unbound row do not
    pretend to know."""
    row = {"company_stripe_account_id": "acct_t", "company_stripe_source": "api_key", "company_stripe_livemode": False}
    assert mod._status_payload(row)["livemode"] is False
    assert (
        mod._status_payload({"company_stripe_account_id": "acct_c", "company_stripe_source": "connected"})["livemode"]
        is True
    )
    assert mod._status_payload({})["livemode"] is None
