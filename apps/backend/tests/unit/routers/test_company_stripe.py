import os
from unittest.mock import AsyncMock, patch

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
        "dashboard_url": None,
        "connect_configured": True,
    }


def test_status_reports_connect_unconfigured(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json()["connect_configured"] is False


def test_connect_start_503_when_unconfigured(client, monkeypatch):
    """Fail closed BEFORE the state row is written — an authorize URL with an
    empty client_id is a dead end the owner has to discover at Stripe."""
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    with patch.object(mod.oauth_state_repo, "put", AsyncMock()) as put:
        resp = client.get("/api/v1/company/stripe/connect/start")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "stripe_connect_not_configured"
    put.assert_not_awaited()


def test_disconnect_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_CONNECT_CLIENT_ID", "")
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock()) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "stripe_connect_not_configured"
    deauth.assert_not_awaited()
    clear.assert_not_awaited()


def test_create_binds_and_returns_link(client, facts):
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


def test_create_reuses_existing_created_account(client):
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


def test_create_lost_bind_race_adopts_the_winner(client):
    """set_company_stripe_account -> None with a bound row means a concurrent
    create won; onboard the winner, not our orphan shell."""
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


def test_disconnect_maps_stripe_error_and_keeps_binding(client):
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
    assert resp.status_code == 502
    clear.assert_not_awaited()


def test_disconnect_deauthorizes_and_clears(client, facts):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
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
