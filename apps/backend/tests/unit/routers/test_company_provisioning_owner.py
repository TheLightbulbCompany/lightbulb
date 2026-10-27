import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

RES = {
    "resource_id": "res_1",
    "provider": "posthog",
    "service": "analytics",
    "status": "active",
    "resource_name": "Northwind analytics",
    "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    "dashboard_url": "https://us.posthog.com/project/1",
    "level": "company",
    "workflow_id": "pwf_1",
    "project_id": "proj_1",
}


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
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


def test_resources_returns_an_allowlisted_projection_never_the_stored_pointer(client):
    """The pointer carries the Stripe `project_id` and a `dashboard_url` slot.
    Neither is the browser's business, and a blocklist would leak whatever the
    next writer adds."""
    with patch.object(mod.provisioning_landing, "provisioning_summary", AsyncMock(return_value=[RES])):
        resp = client.get("/api/v1/company/provisioning/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"resources"}
    row = body["resources"][0]
    assert row["level"] == "company"
    assert row["resource_name"] == "Northwind analytics"
    assert row["workflow_id"] == "pwf_1"
    assert set(row) == {
        "resource_id",
        "provider",
        "service",
        "resource_name",
        "status",
        "env_keys",
        "level",
        "workflow_id",
    }


def test_revoke_delegates_to_the_service_and_writes_facts(client, facts):
    """The router is a thin gate + delegate — the shared-blob-safe merge logic
    lives in provisioning_landing.revoke_credentials (see its own tests)."""
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    revoke.assert_awaited_once_with(owner_id="org_a", provider="posthog", resource_id="res_1")
    facts.assert_awaited_once_with("org_a")


def test_revoke_is_paid_only_with_the_provisioning_specific_message(client, monkeypatch):
    entitlement = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", entitlement)
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    entitlement.assert_awaited_once_with("org_a", message=mod.PAID_MESSAGE)


def test_revoke_returns_404_when_no_such_pointer_exists(client, facts):
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=False)):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_missing/revoke")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "resource_not_found"
    facts.assert_not_awaited()


def test_revoke_is_org_admin_only(member_client):
    resp = member_client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 403


# --- GET /charges — Spend's second source ---------------------------------
# The 2026-09-30.preview API has no charges endpoint, so the route only ever
# says "unavailable" and never calls Stripe.

CHARGES = "/api/v1/company/provisioning/charges"


def _charges(client, *, account="acct_1", **params):
    account_mock = (
        AsyncMock(return_value=account)
        if account
        else AsyncMock(side_effect=mod.WorkflowRefused("no_company_stripe_account", "connect Stripe"))
    )
    with (
        patch.object(mod.provisioning_workflow, "account_id_for", account_mock),
        patch.object(mod.api, "request", AsyncMock()) as stripe_call,
    ):
        resp = client.get(CHARGES, params=params)
    stripe_call.assert_not_awaited()
    return resp


def test_charges_says_unavailable_with_a_reason_when_stripe_is_unbound(client):
    body = _charges(client, account=None).json()
    assert body["provisioning_available"] is False
    assert body["reason"] == "no_company_stripe_account"
    assert body["charges"] == []


def test_charges_is_unavailable_because_the_api_has_no_usage_endpoint(client):
    body = _charges(client).json()
    assert body["provisioning_available"] is False and body["reason"] == "usage_unavailable"
    assert body["direction"] == "out"


def test_charges_rejects_a_bad_window(client):
    assert _charges(client, start="not-a-date").status_code == 422


def test_charges_rejects_an_unbounded_span(client):
    assert _charges(client, start="2020-01-01", end="2026-08-30").status_code == 422


def test_charges_is_org_admin_only(member_client):
    assert member_client.get(CHARGES).status_code == 403


def test_charges_is_paid_only_unlike_the_company_money_reads(client, monkeypatch):
    """The asymmetry is deliberate: this route sits on the rail whose other
    routes mutate. /company/rollup and friends stay unpaid-gated."""
    entitlement = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", entitlement)
    _charges(client)
    entitlement.assert_awaited_once_with("org_a", message=mod.PAID_MESSAGE)


def test_revoke_is_blocked_on_the_trial_plan(client, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(
            side_effect=HTTPException(
                status_code=402, detail={"blocked": True, "code": "routines_require_subscription"}
            )
        ),
    )
    resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "routines_require_subscription"


def test_revoke_rejects_a_hostile_provider_segment(client):
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/PostHog!/res_1/revoke")
    assert resp.status_code == 422
    revoke.assert_not_awaited()


def test_revoke_rejects_a_hostile_resource_id_segment(client):
    # Leading underscore fails _RESOURCE_ID_RE (must start [A-Za-z0-9]).
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/posthog/_res1/revoke")
    assert resp.status_code == 422
    revoke.assert_not_awaited()


# --- owner answers: approve / reject / cancel / information / links / remove ---

WF = {
    "workflow_id": "pwf_1",
    "owner_id": "org_a",
    "state": "awaiting_approval",
    "provider_id": "prvdr_1",
    "provider_name": "posthog",
    "service_ref": "analytics",
    "service_id": "analytics",
    "awaiting": None,
    "error_code": None,
    "approval": {"price_display": "$20/mo", "scope": "project"},
}

# Every owner-facing answer route, for the gate/ownership tests that apply to
# all of them identically.
WORKFLOW_ROUTES = [
    ("POST", "/workflows/pwf_1/approve", {}),
    ("POST", "/workflows/pwf_1/reject", None),
    ("POST", "/workflows/pwf_1/cancel", None),
    ("POST", "/workflows/pwf_1/information", {"information": {}}),
    ("POST", "/workflows/pwf_1/payment-link", None),
    ("POST", "/workflows/pwf_1/connect-link", None),
]
RESOURCE_ROUTES = [
    ("POST", "/resources/posthog/res_1/dashboard-link", None),
    ("POST", "/resources/posthog/res_1/remove", {"confirm": "x"}),
]
# The GET is deliberately absent: it is a READ any org member may make, gated
# like `GET /resources`. Only the ANSWERS are admin-and-paid.
ALL_ROUTES = [*WORKFLOW_ROUTES, *RESOURCE_ROUTES]


def _call(client, method, path, body):
    url = f"/api/v1/company/provisioning{path}"
    return client.get(url) if method == "GET" else client.post(url, json=body)


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES)
def test_every_owner_route_is_org_admin_only(member_client, method, path, body):
    assert _call(member_client, method, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES)
def test_every_owner_route_is_paid_only(client, monkeypatch, method, path, body):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(side_effect=HTTPException(status_code=402, detail={"blocked": True, "code": "needs_plan"})),
    )
    assert _call(client, method, path, body).status_code == 402


@pytest.mark.parametrize(("method", "path", "body"), WORKFLOW_ROUTES)
@pytest.mark.parametrize("row", [None, {**WF, "owner_id": "org_other"}])
def test_a_missing_or_foreign_workflow_is_404_either_way(client, method, path, body, row):
    with patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)):
        resp = _call(client, method, path, body)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "workflow_not_found"


@pytest.mark.parametrize(("method", "path", "body"), RESOURCE_ROUTES)
def test_a_resource_this_owner_never_landed_is_404(client, method, path, body):
    with patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[])):
        resp = _call(client, method, path, body)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "resource_not_found"


@pytest.mark.parametrize(("method", "path", "body"), RESOURCE_ROUTES)
def test_resource_routes_reject_a_hostile_provider_segment(client, method, path, body):
    with patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock()) as listed:
        resp = _call(client, method, path.replace("posthog", "PostHog!"), body)
    assert resp.status_code == 422
    listed.assert_not_awaited()


def test_any_member_can_read_the_list_without_a_subscription(member_client, monkeypatch):
    """Seeing what your company was asked for is a read, like `GET /resources`
    — the admin-and-paid fence is on the answers, not on looking."""
    entitlement = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", entitlement)
    with (
        patch.object(mod.workflow_repo, "list_for_owner", AsyncMock(return_value=[WF])) as listed,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = member_client.get("/api/v1/company/provisioning/workflows")
    assert resp.status_code == 200 and resp.json()["workflows"][0]["workflow_id"] == "pwf_1"
    listed.assert_awaited_once_with("org_a")
    entitlement.assert_not_awaited()


def test_workflows_list_is_owner_scoped_newest_first_and_safe(client):
    old = {**WF, "workflow_id": "pwf_0", "created_at": "2026-01-01", "project_id": "proj_1"}
    new = {**WF, "created_at": "2026-02-01", "project_id": "proj_1"}
    with (
        patch.object(mod.workflow_repo, "list_for_owner", AsyncMock(return_value=[old, new])) as listed,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.get("/api/v1/company/provisioning/workflows")
    assert resp.status_code == 200
    listed.assert_awaited_once_with("org_a")
    rows = resp.json()["workflows"]
    assert [r["workflow_id"] for r in rows] == ["pwf_1", "pwf_0"]
    assert rows[0]["state"] == "awaiting_approval" and rows[0]["provider"] == "posthog"
    # `_safe_workflow` is an allowlist — the row's ids and the approval
    # snapshot never reach the browser.
    assert "project_id" not in rows[0] and "approval" not in rows[0]
    # Both rows are open, so the page load moves both — OLDEST first.
    assert [c.args[0] for c in step.await_args_list] == ["pwf_0", "pwf_1"]


# --- approve ---------------------------------------------------------------


def test_approve_honours_an_edited_limit_records_the_signer_and_takes_no_step(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.provisioning_workflow, "limit_is_editable", AsyncMock(return_value=True)),
        patch.object(
            mod.provisioning_workflow,
            "on_approved",
            AsyncMock(return_value={**WF, "state": "connecting_provider"}),
        ) as approved,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/approve",
            json={"usage_limit": {"max_amount": 2500, "currency": "usd", "recurring_interval": "month"}},
        )
    assert resp.status_code == 200 and resp.json()["state"] == "connecting_provider"
    assert approved.await_args.kwargs["usage_limit"]["max_amount"] == 2500
    assert approved.await_args.kwargs["approved_by"] == "user_admin"
    # A click is not a read: its own transition IS the step, and the next
    # read takes the one after it.
    step.assert_not_awaited()


def test_approve_ignores_an_edited_limit_once_a_payment_profile_exists(client):
    """The card said read-only, so the browser's number is not a second door on
    a live billing setting — the row's own limit is what is signed for."""
    row = {**WF, "usage_limit": {"max_amount": 5000, "currency": "usd", "recurring_interval": "month"}}
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.provisioning_workflow, "limit_is_editable", AsyncMock(return_value=False)),
        patch.object(mod.provisioning_workflow, "on_approved", AsyncMock(return_value=row)) as approved,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/approve",
            json={"usage_limit": {"max_amount": 99000, "currency": "usd", "recurring_interval": "month"}},
        )
    assert approved.await_args.kwargs["usage_limit"]["max_amount"] == 5000


def test_approve_without_a_limit_never_probes_the_payment_profile(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(mod.provisioning_workflow, "limit_is_editable", AsyncMock()) as editable,
        patch.object(mod.provisioning_workflow, "on_approved", AsyncMock(return_value=WF)) as approved,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/approve", json={})
    assert resp.status_code == 200
    editable.assert_not_awaited()
    assert approved.await_args.kwargs["usage_limit"] is None


@pytest.mark.parametrize(
    "limit",
    [
        {"max_amount": -5, "currency": "usd", "recurring_interval": "month"},
        {"max_amount": 2500, "currency": "usd", "recurring_interval": "decade"},
        {"max_amount": 2500, "currency": "eur", "recurring_interval": "month"},
        {"max_amount": 100_001, "currency": "usd", "recurring_interval": "month"},
    ],
)
def test_approve_rejects_a_nonsense_usage_limit(client, limit):
    with patch.object(mod.provisioning_workflow, "on_approved", AsyncMock()) as approved:
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/approve", json={"usage_limit": limit})
    assert resp.status_code == 422
    approved.assert_not_awaited()


def test_approve_409s_when_the_card_was_already_answered(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**WF, "state": "connecting_provider"})),
        patch.object(mod.provisioning_workflow, "on_approved", AsyncMock()) as approved,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/approve", json={})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_awaiting_approval"
    approved.assert_not_awaited()


def test_approve_maps_a_stripe_outage_to_503(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(
            mod.provisioning_workflow,
            "on_approved",
            AsyncMock(side_effect=mod.api.TransientProvisioningError("api_error", "down")),
        ),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/approve", json={})
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "stripe_unavailable"


def test_approve_relays_a_workflow_refusal_as_409(client):
    refused = mod.WorkflowRefused("service_unavailable", "gone from your catalog")
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(mod.provisioning_workflow, "on_approved", AsyncMock(side_effect=refused)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/approve", json={})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "service_unavailable"


# --- reject / cancel -------------------------------------------------------


def test_reject_is_terminal_and_refreshes_the_facts(client, facts):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(
            mod.provisioning_workflow, "on_rejected", AsyncMock(return_value={**WF, "state": "rejected"})
        ) as rejected,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/reject")
    assert resp.status_code == 200 and resp.json()["state"] == "rejected"
    rejected.assert_awaited_once_with("pwf_1")
    facts.assert_awaited_once_with("org_a")
    # `rejected` is terminal — there is nothing left to step.
    step.assert_not_awaited()


def test_reject_409s_on_a_card_that_is_not_open(client, facts):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**WF, "state": "active"})),
        patch.object(mod.provisioning_workflow, "on_rejected", AsyncMock()) as rejected,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/reject")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_awaiting_approval"
    rejected.assert_not_awaited()
    facts.assert_not_awaited()


def test_cancel_withdraws_the_ask(client, facts):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(
            mod.provisioning_workflow, "cancel", AsyncMock(return_value={**WF, "state": "cancelled"})
        ) as cancelled,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/cancel")
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    cancelled.assert_awaited_once_with("pwf_1")
    facts.assert_awaited_once_with("org_a")
    # `cancelled` is terminal — there is nothing left to step.
    step.assert_not_awaited()


def test_cancel_refuses_once_something_exists_on_the_owners_stripe_account(client, facts):
    refused = mod.WorkflowRefused("cannot_cancel", "This request has already started — review it instead")
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**WF, "state": "provisioning"})),
        patch.object(mod.provisioning_workflow, "cancel", AsyncMock(side_effect=refused)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/cancel")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "cannot_cancel"
    facts.assert_not_awaited()


# --- information -----------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {"team": {"type": "string"}}, "required": ["team"]}
AR_INFO = {
    **WF,
    "state": "awaiting_user_action",
    "awaiting": "provider_information",
    "information_schema": _SCHEMA,
    "information_target": "account_request",
    "provider_connection_request_id": "ar_1",
    "resource_id": "res_1",
}
RES_INFO = {**AR_INFO, "information_target": "resource"}


def test_information_submits_to_the_resource_endpoint_for_that_target(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=RES_INFO)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "submit_resource_information", AsyncMock(return_value={"id": "res_1"})) as submit,
        patch.object(mod.api, "submit_provider_connection_request_information", AsyncMock()) as other,
        patch.object(
            mod.workflow_repo, "transition", AsyncMock(return_value={**RES_INFO, "state": "provisioning"})
        ) as move,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {"team": "growth"}}
        )
    assert resp.status_code == 200 and resp.json()["state"] == "provisioning"
    assert submit.await_args.args == ("acct_1", "res_1")
    assert submit.await_args.kwargs["submitted_information"] == {"team": "growth"}
    other.assert_not_awaited()
    assert move.await_args.kwargs["to_state"] == "provisioning"
    # The answered question's schema and the owner's clock go with it.
    assert move.await_args.kwargs["fields"]["information_schema"] is None
    assert move.await_args.kwargs["fields"]["user_action_since"] is None
    # A click is not a read: its own transition IS the step, and the next
    # read takes the one after it.
    step.assert_not_awaited()


def test_information_submits_to_the_account_request_endpoint_for_that_target(client):
    """`resource_id` is ALSO set — during a reauth both are. The stored target
    decides, never whichever id happens to be there."""
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=AR_INFO)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api, "submit_provider_connection_request_information", AsyncMock(return_value={"id": "ar_1"})
        ) as submit,
        patch.object(mod.api, "submit_resource_information", AsyncMock()) as other,
        patch.object(
            mod.workflow_repo, "transition", AsyncMock(return_value={**AR_INFO, "state": "connecting_provider"})
        ) as move,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {"team": "growth"}}
        )
    assert resp.status_code == 200
    assert submit.await_args.args == ("acct_1", "ar_1")
    assert submit.await_args.kwargs["information"] == {"team": "growth"}
    other.assert_not_awaited()
    assert move.await_args.kwargs["to_state"] == "connecting_provider"


def test_information_is_validated_against_the_stored_schema(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=RES_INFO)),
        patch.object(mod.api, "submit_resource_information", AsyncMock()) as submit,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {"team": 7}}
        )
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_information"
    # The path, never the submitted value — a provider's form can ask for a
    # credential.
    assert "7" not in resp.json()["detail"]["message"]
    assert "team" in resp.json()["detail"]["message"]
    submit.assert_not_awaited()


@pytest.mark.parametrize(
    "information",
    [{"team": {"nested": "object"}}, {"team": ["a", "list"]}, {"team": "x" * 9000}, {"team": "two\nlines"}],
)
def test_information_bounds_its_values_like_a_configuration(client, information):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=RES_INFO)),
        patch.object(mod.api, "submit_resource_information", AsyncMock()) as submit,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": information}
        )
    assert resp.status_code == 422
    submit.assert_not_awaited()


@pytest.mark.parametrize("key", ["k" * 200, "two\nlines"])
def test_information_bounds_its_KEYS_like_a_configuration(client, key):
    """The names come off a schema WE stored, but the body does not — an
    oversized or newline-carrying field name is forwarded to the provider
    verbatim unless it is stopped here."""
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=RES_INFO)),
        patch.object(mod.api, "submit_resource_information", AsyncMock()) as submit,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {key: "growth"}}
        )
    assert resp.status_code == 422
    submit.assert_not_awaited()


def test_information_409s_when_nothing_is_pending(client):
    row = {**WF, "state": "provisioning", "awaiting": None, "information_target": None, "resource_id": "res_1"}
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "submit_resource_information", AsyncMock()) as submit,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {}})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_information_pending"
    submit.assert_not_awaited()


def test_information_409s_when_the_target_id_is_missing(client):
    with patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**RES_INFO, "resource_id": None})):
        resp = client.post(
            "/api/v1/company/provisioning/workflows/pwf_1/information", json={"information": {"team": "x"}}
        )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_information_pending"


# --- payment link ----------------------------------------------------------

PAYING = {
    **WF,
    "state": "awaiting_user_action",
    "awaiting": "payment_setup",
    "usage_limit": {"max_amount": 2500, "currency": "usd", "recurring_interval": "month"},
}


def test_payment_link_is_minted_on_click_and_never_stored(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=PAYING)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api,
            "create_payment_method_request",
            AsyncMock(
                return_value={"status": "checkout_initiated", "checkout_session_url": "https://checkout.stripe.com/x"}
            ),
        ) as minted,
        patch.object(mod.workflow_repo, "patch", AsyncMock()) as patched,
        patch.object(mod.workflow_repo, "transition", AsyncMock()) as move,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/payment-link")
    assert resp.json()["checkout_session_url"] == "https://checkout.stripe.com/x"
    # The approved limit, not a default.
    assert minted.await_args.kwargs == {"max_amount": 2500, "currency": "usd", "recurring_interval": "month"}
    # Nothing bearer-like is written to the row.
    for call in [*patched.await_args_list, *move.await_args_list]:
        assert "checkout.stripe.com" not in str(call)
    # A click is not a read: its own transition IS the step, and the next
    # read takes the one after it.
    step.assert_not_awaited()


def test_payment_link_falls_back_to_the_default_limit(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**PAYING, "usage_limit": None})),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "create_payment_method_request", AsyncMock(return_value={"status": "complete"})) as m,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/payment-link")
    assert resp.json() == {"checkout_session_url": None, "status": "complete"}
    assert m.await_args.kwargs["max_amount"] == mod.provisioning_approvals.DEFAULT_USAGE_LIMIT["max_amount"]


def test_payment_link_409s_when_the_row_is_not_waiting_on_a_card(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(mod.api, "create_payment_method_request", AsyncMock()) as minted,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/payment-link")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_payment_pending"
    minted.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "code"),
    [("payment-link", "no_payment_pending"), ("connect-link", "no_connect_pending")],
)
def test_a_withdrawn_request_can_never_mint_a_link(client, path, code):
    """`cancel` clears `awaiting`, but a row cancelled before that shipped —
    or by any other terminal move — must not mint one either."""
    row = {**PAYING, "state": "cancelled", "awaiting": "payment_setup"}
    if path == "connect-link":
        row = {**row, "awaiting": "provider_connect", "provider_connection_request_id": "ar_1"}
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.api, "create_payment_method_request", AsyncMock()) as paid,
        patch.object(mod.api, "get_provider_connection_request", AsyncMock()) as read,
    ):
        resp = client.post(f"/api/v1/company/provisioning/workflows/pwf_1/{path}")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == code
    paid.assert_not_awaited()
    read.assert_not_awaited()


def test_payment_link_un_stales_the_row_so_the_next_read_is_not_a_no_op(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**PAYING, "state": "stale"})),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api, "create_payment_method_request", AsyncMock(return_value={"status": "checkout_initiated"})
        ),
        patch.object(mod.workflow_repo, "transition", AsyncMock()) as move,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        client.post("/api/v1/company/provisioning/workflows/pwf_1/payment-link")
    assert move.await_args.kwargs["from_state"] == "stale"
    assert move.await_args.kwargs["to_state"] == "awaiting_user_action"
    assert move.await_args.kwargs["fields"]["awaiting"] == "payment_setup"


# --- connect link ----------------------------------------------------------

CONNECTING = {
    **WF,
    "state": "awaiting_user_action",
    "awaiting": "provider_connect",
    "provider_connection_request_id": "ar_1",
}


def test_connect_link_reuses_a_live_account_request(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=CONNECTING)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(
                return_value={
                    "id": "ar_1",
                    "request_status": "pending_auth",
                    "redirect_url": "https://provider.example/oauth?fresh",
                }
            ),
        ),
        patch.object(mod.api, "create_provider_connection_request", AsyncMock()) as created,
        patch.object(mod.workflow_repo, "patch", AsyncMock()) as patched,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.json() == {"redirect_url": "https://provider.example/oauth?fresh"}
    created.assert_not_awaited()
    # Minted per click: the URL never reaches the row.
    for call in patched.await_args_list:
        assert "provider.example" not in str(call)
    # A click is not a read: its own transition IS the step, and the next
    # read takes the one after it.
    step.assert_not_awaited()


def test_connect_link_mints_a_new_account_request_when_the_old_one_errored(client):
    with (
        patch.object(
            mod.workflow_repo, "get", AsyncMock(return_value={**CONNECTING, "provider_connection_request_id": "ar_old"})
        ),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "ar_old", "request_status": "error"}),
        ),
        patch.object(
            mod.api,
            "create_provider_connection_request",
            AsyncMock(
                return_value={
                    "id": "ar_new",
                    "request_status": "pending_auth",
                    "redirect_url": "https://provider.example/oauth?new",
                }
            ),
        ) as created,
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"company_stripe_project_id": "p_1"})),
        patch.object(mod.workflow_repo, "patch", AsyncMock()) as patched,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.json()["redirect_url"] == "https://provider.example/oauth?new"
    assert created.await_args.kwargs == {"provider": "prvdr_1", "project": "p_1"}
    # Persisted before the URL is handed over — an account request has no name
    # to reconcile a lost id by.
    assert patched.await_args.args[1] == {"provider_connection_request_id": "ar_new"}


def test_connect_link_mints_a_new_one_when_the_old_id_is_gone(client):
    gone = mod.ProvisioningError("not_found", "no such account request")
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=CONNECTING)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "get_provider_connection_request", AsyncMock(side_effect=gone)),
        patch.object(
            mod.api,
            "create_provider_connection_request",
            AsyncMock(
                return_value={"id": "ar_new", "request_status": "pending_auth", "redirect_url": "https://p.example/x"}
            ),
        ) as created,
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={})),
        patch.object(mod.workflow_repo, "patch", AsyncMock()),
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.status_code == 200
    created.assert_awaited_once()


def test_connect_link_un_stales_the_row_so_the_next_read_is_not_a_no_op(client):
    """The driver stops on `stale` by design, so the click must move the row
    back — and restart the seven days — or the next read finds it `stale`."""
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value={**CONNECTING, "state": "stale"})),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(
                return_value={"id": "ar_1", "request_status": "pending_auth", "redirect_url": "https://p.example/x"}
            ),
        ),
        patch.object(mod.workflow_repo, "transition", AsyncMock()) as move,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.status_code == 200
    assert move.await_args.kwargs["from_state"] == "stale"
    assert move.await_args.kwargs["to_state"] == "awaiting_user_action"
    assert move.await_args.kwargs["fields"]["awaiting"] == "provider_connect"
    assert move.await_args.kwargs["fields"]["user_action_since"]
    # Un-staling writes state, never the minted URL.
    assert "p.example" not in str(move.await_args_list)


def test_connect_link_409s_when_the_connection_needs_no_browser_step(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=CONNECTING)),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(
            mod.api,
            "get_provider_connection_request",
            AsyncMock(return_value={"id": "ar_1", "request_status": "complete"}),
        ),
        patch.object(mod.api, "create_provider_connection_request", AsyncMock()) as created,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_redirect_available"
    # A `complete` request is not one to replace — and the next READ is what
    # moves the row on from it.
    created.assert_not_awaited()
    step.assert_not_awaited()


def test_connect_link_409s_when_the_row_is_not_waiting_on_a_connection(client):
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=WF)),
        patch.object(mod.api, "get_provider_connection_request", AsyncMock()) as read,
    ):
        resp = client.post("/api/v1/company/provisioning/workflows/pwf_1/connect-link")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_connect_pending"
    read.assert_not_awaited()


# --- dashboard link --------------------------------------------------------

POINTER = {"resource_id": "res_1", "provider": "posthog", "service": "analytics", "resource_name": "analytics-abc123"}


def test_dashboard_link_answers_no_url_because_the_api_has_no_deep_links(client):
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[POINTER])),
        patch.object(mod.api, "request", AsyncMock()) as stripe_call,
    ):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/dashboard-link")
    assert resp.json() == {"url": None, "expires_time": None}
    stripe_call.assert_not_awaited()


# --- remove ----------------------------------------------------------------


def test_remove_needs_the_resource_name_typed_back(client, facts):
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[POINTER])),
        patch.object(mod.api, "remove_resource", AsyncMock()) as removed,
        patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoked,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": "wrong name"}
        )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "confirm_mismatch"
    removed.assert_not_awaited()
    revoked.assert_not_awaited()
    facts.assert_not_awaited()


def test_remove_deprovisions_at_the_provider_then_drops_our_copy(client, facts):
    order = []
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[POINTER])),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "remove_resource", AsyncMock(side_effect=lambda *a: order.append("stripe"))) as removed,
        patch.object(
            mod.provisioning_landing,
            "revoke_credentials",
            AsyncMock(side_effect=lambda **k: order.append("ours") or True),
        ) as revoked,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": " analytics-abc123 "}
        )
    assert resp.status_code == 200 and resp.json()["removed"] is True
    # The 200 says what just happened to their infrastructure and their bill.
    assert "deprovisioned" in resp.json()["message"] and "billing" in resp.json()["message"]
    assert order == ["stripe", "ours"]
    removed.assert_awaited_once_with("acct_1", "res_1")
    revoked.assert_awaited_once_with(owner_id="org_a", provider="posthog", resource_id="res_1")
    facts.assert_awaited_once_with("org_a")


def test_remove_stamps_the_workflow_so_the_history_stops_saying_active(client):
    with (
        patch.object(
            mod.integrations_repo,
            "list_provider_resources",
            AsyncMock(return_value=[{**POINTER, "workflow_id": "pwf_1"}]),
        ),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "remove_resource", AsyncMock()),
        patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)),
        patch.object(mod.workflow_repo, "patch", AsyncMock()) as patched,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": "analytics-abc123"}
        )
    assert resp.status_code == 200
    assert patched.await_args.args[1]["resource_status"] == "removed"
    assert patched.await_args.args[1]["removed_at"]


def test_a_purged_workflow_row_cannot_fail_a_completed_removal(client):
    """The resource is already gone at the provider and our copy with it. A
    stamp that cannot land must not hand the owner a 500 to retry."""
    with (
        patch.object(
            mod.integrations_repo,
            "list_provider_resources",
            AsyncMock(return_value=[{**POINTER, "workflow_id": "pwf_gone"}]),
        ),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "remove_resource", AsyncMock()),
        patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)),
        patch.object(mod.workflow_repo, "patch", AsyncMock(side_effect=RuntimeError("ConditionalCheckFailed"))),
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": "analytics-abc123"}
        )
    assert resp.status_code == 200 and resp.json()["removed"] is True


def test_remove_of_a_pointer_with_no_workflow_never_patches(client):
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[POINTER])),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "remove_resource", AsyncMock()),
        patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)),
        patch.object(mod.workflow_repo, "patch", AsyncMock()) as patched,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": "analytics-abc123"}
        )
    assert resp.status_code == 200
    patched.assert_not_awaited()


def test_remove_keeps_our_copy_when_the_provider_refuses(client, facts):
    """Our pointer is the only thing that still names the resource, so it is
    dropped only after the provider accepted the deprovision."""
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[POINTER])),
        patch.object(mod.provisioning_workflow, "account_id_for", AsyncMock(return_value="acct_1")),
        patch.object(mod.api, "remove_resource", AsyncMock(side_effect=mod.ProvisioningError("invalid_request", "no"))),
        patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoked,
    ):
        resp = client.post(
            "/api/v1/company/provisioning/resources/posthog/res_1/remove", json={"confirm": "analytics-abc123"}
        )
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "stripe_error"
    revoked.assert_not_awaited()
    facts.assert_not_awaited()


def test_a_list_read_steps_the_five_oldest_open_rows_and_starves_none(client):
    """More open rows than the cap. Taking them newest-first would step the
    same recent five on every read and leave the oldest — the ones nothing else
    is going to move — parked forever."""
    rows = [{**WF, "workflow_id": f"pwf_{i}", "created_at": f"2026-08-{i + 10:02d}"} for i in range(7)]
    rows.reverse()  # the repo hands them back newest first, and so does the route
    with (
        patch.object(mod.workflow_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.get("/api/v1/company/provisioning/workflows")
    assert [c.args[0] for c in step.await_args_list] == ["pwf_0", "pwf_1", "pwf_2", "pwf_3", "pwf_4"]
    # Presentation order is untouched by which rows were stepped.
    assert [r["workflow_id"] for r in resp.json()["workflows"]] == [f"pwf_{i}" for i in range(6, -1, -1)]
