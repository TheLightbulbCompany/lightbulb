"""/api/v1/company/{rollup,payments,customers,products} — the P1 Money reads.

Four read-only routes. The rollup serves the stored day rows; the other three
read the owner's connected Stripe account THROUGH, storing nothing.

The honesty rules each get a test: followers/open_threads are hardcoded zeros
in the writer (so they are named `unavailable`, never emitted as 0), visitors
is a stored 0 for an owner with no PostHog connection (so it is unavailable
too — but a real 0 for one who HAS it), agent spend is month-to-date (so it is
one top-level scalar, never a per-day column that would double-count when
summed), and an owner with no connected account gets an empty 200 rather than
an error.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.services import company_stripe_reads  # noqa: E402
from main import app  # noqa: E402
from routers import company_money as mod  # noqa: E402

STRIPE_PATHS = ["/api/v1/company/payments", "/api/v1/company/customers", "/api/v1/company/products"]
ALL_PATHS = ["/api/v1/company/rollup", *STRIPE_PATHS]


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


def _rows(*days):
    return [
        {
            "day": d,
            "revenue_microcents": 1_000_000,
            "signups": 2,
            "visitors": 30,
            "followers": 0,
            "open_threads": 0,
            "agent_spend_microcents": 500_000,
        }
        for d in days
    ]


# The default owner HAS PostHog connected — the _rows fixture's visitors: 30 is
# only a coherent number for one who does.
CONNECTED_POSTHOG = {"owner_id": "org_a", "provider": "posthog", "status": "connected"}


def _rollup(client, rows=(), usage=None, billing=None, posthog=CONNECTED_POSTHOG, **params):
    with (
        patch.object(mod.rollup_repo, "get_days", AsyncMock(return_value=list(rows))),
        patch.object(mod.usage_repo, "get_period_usage", AsyncMock(return_value=usage)) as usage_mock,
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=billing)),
        patch.object(mod.integrations_repo, "get", AsyncMock(return_value=posthog)),
    ):
        resp = client.get("/api/v1/company/rollup", params=params)
    return resp, usage_mock


# --- rollup: the two omissions --------------------------------------------


def test_rollup_omits_the_hardcoded_zeros_and_names_them_unavailable(client):
    resp, _ = _rollup(client, rows=_rows("2026-08-30"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == [{"day": "2026-08-30", "revenue_microcents": 1_000_000, "signups": 2, "visitors": 30}]
    assert "followers" not in body["days"][0] and "open_threads" not in body["days"][0]
    assert body["unavailable"] == ["followers", "open_threads"]


def test_rollup_omits_visitors_when_the_owner_never_connected_posthog(client):
    """No PostHog connection means we have never measured visitors. The stored
    0 (rollup_service's, forced by put_day's every-key contract) must not reach
    a client as a number: omitted from days[], named in unavailable."""
    resp, _ = _rollup(client, rows=_rows("2026-08-30"), posthog=None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == [{"day": "2026-08-30", "revenue_microcents": 1_000_000, "signups": 2}]
    assert "visitors" in body["unavailable"]


def test_rollup_keeps_a_genuine_zero_for_a_connected_owner_with_no_traffic(client):
    """The asymmetry: an owner WITH PostHog who truly had nobody visit still
    sees a real 0. Suppressing that would be the opposite lie."""
    rows = _rows("2026-08-30")
    rows[0]["visitors"] = 0
    resp, _ = _rollup(client, rows=rows)
    body = resp.json()
    assert body["days"][0]["visitors"] == 0
    assert "visitors" not in body["unavailable"]


def test_rollup_treats_a_non_connected_posthog_row_as_unmeasured(client):
    """A row exists but its status is not ``connected`` — same verdict as no row."""
    resp, _ = _rollup(client, rows=_rows("2026-08-30"), posthog={**CONNECTED_POSTHOG, "status": "revoked"})
    body = resp.json()
    assert "visitors" not in body["days"][0]
    assert "visitors" in body["unavailable"]


def test_rollup_decides_visitors_from_one_ddb_row_never_from_posthog(client):
    """Cost fence: one GetItem on this owner's posthog status row. A vendor
    round-trip per rollup request is what this must never become."""
    get = AsyncMock(return_value=None)
    with (
        patch.object(mod.rollup_repo, "get_days", AsyncMock(return_value=[])),
        patch.object(mod.usage_repo, "get_period_usage", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        patch.object(mod.integrations_repo, "get", get),
    ):
        assert client.get("/api/v1/company/rollup").status_code == 200
    assert get.await_count == 1
    assert get.await_args.args == ("org_a", "posthog")


def test_rollup_agent_spend_is_one_mtd_scalar_from_usage_not_a_per_day_column(client):
    resp, usage_mock = _rollup(
        client,
        rows=_rows("2026-08-29", "2026-08-30"),
        usage={"total_spend_microdollars": 777},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all("agent_spend_microcents" not in d for d in body["days"])
    assert body["agent_spend_month_to_date_microcents"] == 777
    # Sourced live from the usage row the writer stamps from, keyed by month.
    period = usage_mock.await_args.args[1]
    assert len(period) == 7 and period[4] == "-"


def test_rollup_states_the_direction_of_every_money_field(client):
    """Spend sums three sources now — a client that nets money IN against
    money OUT is the failure mode, so the sign is stated, not inferred."""
    resp, _ = _rollup(client, rows=_rows("2026-08-30"))
    assert resp.json()["direction"] == {
        "revenue_microcents": "in",
        "agent_spend_month_to_date_microcents": "out",
    }


def test_rollup_agent_spend_is_zero_when_no_usage_row(client):
    resp, _ = _rollup(client, rows=_rows("2026-08-30"), usage=None)
    assert resp.json()["agent_spend_month_to_date_microcents"] == 0


# --- rollup: honest range labelling ---------------------------------------


def test_rollup_reports_stripe_bound_and_first_day(client):
    resp, _ = _rollup(
        client,
        rows=_rows("2026-08-29", "2026-08-30"),
        billing={"company_stripe_account_id": "acct_1"},
        start="2026-08-01",
        end="2026-08-30",
    )
    body = resp.json()
    assert body["stripe_bound"] is True
    assert body["first_day"] == "2026-08-29"
    assert (body["start"], body["end"]) == ("2026-08-01", "2026-08-30")


def test_rollup_unbound_owner_has_no_rows_and_no_first_day(client):
    resp, _ = _rollup(client, rows=[], billing={"owner_id": "org_a"})
    body = resp.json()
    assert body["stripe_bound"] is False
    assert body["days"] == [] and body["first_day"] is None


def test_rollup_defaults_to_a_30_day_window_ending_today(client):
    resp, _ = _rollup(client)
    body = resp.json()
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc)
    assert body["end"] == today.strftime("%Y-%m-%d")
    assert body["start"] == (today - timedelta(days=29)).strftime("%Y-%m-%d")


# --- rollup: bounded input ------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"start": "not-a-date"},
        {"end": "2026-13-01"},
        {"start": "2026/08/01"},
        {"start": "2020-01-01", "end": "2026-08-30"},  # span > 366 days
        {"start": "2026-08-30", "end": "2026-08-01"},  # reversed
    ],
)
def test_rollup_rejects_bad_or_unbounded_ranges(client, params):
    get_days = AsyncMock(return_value=[])
    with patch.object(mod.rollup_repo, "get_days", get_days):
        resp = client.get("/api/v1/company/rollup", params=params)
    assert resp.status_code == 422
    get_days.assert_not_awaited()


def test_rollup_accepts_a_366_day_span(client):
    resp, _ = _rollup(client, start="2026-01-01", end="2026-12-31")
    assert resp.status_code == 200


# --- Stripe list routes ---------------------------------------------------


def _stripe_client(data, has_more=False):
    client = MagicMock()
    page = {"data": list(data), "has_more": has_more}
    for resource in ("charges", "customers", "products"):
        getattr(client.v1, resource).list.return_value = page
    return client


def _with_stripe(billing, client_factory):
    return (
        patch.object(company_stripe_reads.billing_repo, "get_by_owner_id", AsyncMock(return_value=billing)),
        patch.object(company_stripe_reads, "_client", client_factory),
    )


@pytest.mark.parametrize("path", STRIPE_PATHS)
def test_unbound_owner_gets_an_empty_200_and_never_builds_a_stripe_client(client, path):
    factory = MagicMock()
    row = {"owner_id": "org_a", "company_stripe_details_submitted": False}
    with patch.object(company_stripe_reads.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        with patch.object(company_stripe_reads, "_client", factory):
            resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json() == {
        "stripe_bound": False,
        "data": [],
        "has_more": False,
        "next_cursor": None,
        "details_submitted": False,
        "charges_enabled": False,
        "payouts_enabled": False,
    }
    factory.assert_not_called()


@pytest.mark.parametrize("path", STRIPE_PATHS)
def test_bound_but_unonboarded_account_reports_its_onboarding_state(client, path):
    row = {
        "company_stripe_account_id": "acct_1",
        "company_stripe_details_submitted": False,
        "company_stripe_charges_enabled": False,
        "company_stripe_payouts_enabled": True,
    }
    binds = _with_stripe(row, MagicMock(return_value=_stripe_client([])))
    with binds[0], binds[1]:
        resp = client.get(path)
    body = resp.json()
    assert body["stripe_bound"] is True
    assert (body["details_submitted"], body["charges_enabled"], body["payouts_enabled"]) == (False, False, True)
    assert body["data"] == []


@pytest.mark.parametrize("path", STRIPE_PATHS)
def test_stripe_error_is_502_never_an_empty_list(client, path):
    sdk = MagicMock()
    for resource in ("charges", "customers", "products"):
        getattr(sdk.v1, resource).list.side_effect = stripe.error.APIConnectionError("stripe down")
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        resp = client.get(path)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "stripe_error"


def test_payments_pages_through_stripes_cursor(client):
    charges = [{"id": f"ch_{i}", "amount": 100, "customer": "cus_1", "receipt_url": "u"} for i in range(2)]
    sdk = _stripe_client(charges, has_more=True)
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        resp = client.get("/api/v1/company/payments", params={"limit": 2, "starting_after": "ch_prev"})
    body = resp.json()
    assert body["has_more"] is True
    assert body["next_cursor"] == "ch_1"
    params = sdk.v1.charges.list.call_args.kwargs["params"]
    assert params["starting_after"] == "ch_prev" and params["limit"] == 2
    # Charges, not PaymentIntents — refund state and the receipt live here.
    assert body["data"][0]["receipt_url"] == "u"
    assert body["data"][0]["customer"] == "cus_1"
    assert sdk.v1.charges.list.call_args.kwargs["options"] == {"stripe_account": "acct_1"}


def test_last_page_has_no_next_cursor(client):
    sdk = _stripe_client([{"id": "ch_1"}], has_more=False)
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        body = client.get("/api/v1/company/payments").json()
    assert body["has_more"] is False and body["next_cursor"] is None


def test_payments_day_range_filters_on_created(client):
    sdk = _stripe_client([])
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        resp = client.get("/api/v1/company/payments", params={"start": "2026-08-01", "end": "2026-08-30"})
    assert resp.status_code == 200
    created = sdk.v1.charges.list.call_args.kwargs["params"]["created"]
    assert created["gte"] == company_stripe_reads._day_bounds("2026-08-01")["gte"]
    assert created["lt"] == company_stripe_reads._day_bounds("2026-08-30")["lt"]


@pytest.mark.parametrize(
    "params",
    [
        {"start": "yesterday"},
        {"start": "2026-08-30", "end": "2026-08-01"},  # reversed, same rule as /rollup
    ],
)
def test_payments_rejects_a_bad_range(client, params):
    factory = MagicMock()
    with patch.object(company_stripe_reads, "_client", factory):
        resp = client.get("/api/v1/company/payments", params=params)
    assert resp.status_code == 422
    factory.assert_not_called()


@pytest.mark.parametrize("path", STRIPE_PATHS)
def test_limit_is_capped_at_100(client, path):
    resp = client.get(path, params={"limit": 500})
    assert resp.status_code == 422


def test_products_expands_default_price_in_one_call(client):
    sdk = _stripe_client([{"id": "prod_1", "name": "Pro", "default_price": {"id": "price_1", "unit_amount": 5000}}])
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        body = client.get("/api/v1/company/products").json()
    params = sdk.v1.products.list.call_args.kwargs["params"]
    assert params["active"] is True and params["expand"] == ["data.default_price"]
    assert body["data"][0]["default_price"]["unit_amount"] == 5000
    assert sdk.v1.products.list.call_count == 1


def test_customers_returns_the_named_fields_without_fanning_out(client):
    row = {"id": "cus_1", "email": "a@b.com", "name": "A", "created": 1, "currency": "usd", "delinquent": False}
    sdk = _stripe_client([{**row, "invoice_settings": {"secret": "leak"}}])
    binds = _with_stripe({"company_stripe_account_id": "acct_1"}, MagicMock(return_value=sdk))
    with binds[0], binds[1]:
        body = client.get("/api/v1/company/customers").json()
    assert body["data"] == [{**row, "balance": None}]
    sdk.v1.subscriptions.list.assert_not_called()


# --- gating ---------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PATHS)
def test_org_member_is_403_on_every_route(member_client, path):
    assert member_client.get(path).status_code == 403
