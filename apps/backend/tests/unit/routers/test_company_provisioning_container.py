"""The container door — what Friday may ask for, and what she may never see.

The body on these routes is written by an LLM, so every test here is really one
of two questions: does the gate run before the work, and can a credential, a
URL or a Stripe id reach the container.
"""

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import billing_repo  # noqa: E402
from core.services.paperclip_admin_client import PaperclipApiError  # noqa: E402
from core.services.provisioning_workflow import WorkflowRefused  # noqa: E402
from core.services.stripe_provisioning import ProvisioningError, TransientProvisioningError  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

BASE = "/api/v1/company/provisioning/u/org_a"
HDRS = {"Authorization": "Bearer hook-token"}

SERVICE = {
    "id": "psvc_1",
    # Live 2026-09-30.preview shape (verified 2026-10-29): the provider id is `provider`.
    "provider": "prvdr_1",
    "service_id": "svc_analytics",
    "provider_name": "posthog",
    "description": "Product analytics",
    "llm_context": "Use for web funnels.",
    "categories": ["analytics"],
    "configuration_schema": {"type": "object"},
    "pricing": {"type": "free", "paid_pricing": []},
    "kind": "deployable",
    "scope": "project",
    "availability": "PROVIDER_SERVICE_AVAILABILITY_AVAILABLE",
}
PROVIDER = {
    "id": "prvdr_1",
    "name": "posthog",
    "categories": ["analytics"],
    "description": "Product analytics",
    "tos_url": "https://posthog.com/terms",
    "website_url": "https://posthog.com",
    "deep_link_purposes": ["dashboard"],
}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _hook(monkeypatch):
    monkeypatch.setattr(mod, "_require_hook_token", AsyncMock(return_value={"owner_id": "org_a"}))


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


@pytest.fixture(autouse=True)
def _account(monkeypatch):
    monkeypatch.setattr(
        billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_a",
                "company_stripe_account_id": "acct_1",
                "company_stripe_source": "created",
            }
        ),
    )


def _catalog(services=(SERVICE,), providers=(PROVIDER,), requirements=()):
    return (
        patch.object(
            mod.api,
            "eligibility",
            AsyncMock(return_value={"is_eligible": True, "requirements": list(requirements)}),
        ),
        patch.object(mod.api, "catalog_providers", AsyncMock(return_value=list(providers))),
        patch.object(mod.api, "catalog_services", AsyncMock(return_value=list(services))),
    )


# --- catalog ---------------------------------------------------------------


def test_catalog_returns_policy_filtered_rows_with_llm_context_as_data(client):
    hidden = {**SERVICE, "service_id": "svc_eu_only", "availability": "PROVIDER_SERVICE_AVAILABILITY_NOT_IN_COUNTRY"}
    thin = {**SERVICE, "service_id": "svc_thin", "provider": "prvdr_9", "provider_name": "herenow"}
    thin_provider = {"id": "prvdr_9", "name": "herenow", "categories": [], "description": ""}
    elig, provs, svcs = _catalog(services=(SERVICE, hidden, thin), providers=(PROVIDER, thin_provider))
    with elig, provs, svcs:
        resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is True and body["requirements"] == []
    # NOT_IN_COUNTRY and the thin Stripe-test provider never reach the agent.
    assert [s["service"] for s in body["services"]] == ["svc_analytics"]
    row = body["services"][0]
    assert row["provider"] == "posthog" and row["pricing_type"] == "free"
    assert row["llm_context"] == "Use for web funnels."
    assert row["terms_url"] == "https://posthog.com/terms"


def test_catalog_drops_a_service_whose_provider_is_not_in_the_catalog(client):
    """`start` refuses these, so offering them would be a promise we break."""
    orphan = {**SERVICE, "service_id": "svc_orphan", "provider": "prvdr_gone"}
    elig, provs, svcs = _catalog(services=(SERVICE, orphan))
    with elig, provs, svcs:
        resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert [s["service"] for s in resp.json()["services"]] == ["svc_analytics"]


def test_catalog_q_filters_after_policy_not_before(client):
    other = {**SERVICE, "service_id": "svc_flags", "description": "Feature flags"}
    elig, provs, svcs = _catalog(services=(SERVICE, other))
    with elig, provs, svcs:
        resp = client.get(f"{BASE}/catalog?q=FLAGS", headers=HDRS)
    assert [s["service"] for s in resp.json()["services"]] == ["svc_flags"]


def test_catalog_reports_outstanding_requirements_without_failing(client):
    elig, provs, svcs = _catalog(requirements=("tos.stripe",))
    with elig, provs, svcs:
        resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert resp.status_code == 200
    assert resp.json()["requirements"] == ["tos.stripe"]


def test_catalog_never_leaks_the_stripe_account_id(client):
    elig, provs, svcs = _catalog()
    with elig, provs, svcs:
        resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert "acct_1" not in resp.text


def test_catalog_stripe_outage_is_a_503_the_agent_can_retry(client):
    """The catalog is three Stripe reads; an outage there is retryable too."""
    with patch.object(
        mod.api, "eligibility", AsyncMock(side_effect=TransientProvisioningError("transport_error", "no route"))
    ):
        resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "stripe_unavailable"


def test_catalog_without_a_company_stripe_account_is_an_actionable_409(client, monkeypatch):
    monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"}))
    resp = client.get(f"{BASE}/catalog", headers=HDRS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_company_stripe_account"


# --- requests --------------------------------------------------------------


def test_request_starts_a_workflow_and_takes_no_step(client):
    row = {
        "workflow_id": "pwf_1",
        "state": "awaiting_approval",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
    }
    with (
        patch.object(mod.provisioning_workflow, "start", AsyncMock(return_value=row)) as start,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post(
            f"{BASE}/requests",
            headers=HDRS,
            json={
                "provider": "posthog",
                "service": "svc_analytics",
                "configuration": {"region": "us"},
                "issue_id": "iss_1",
                "reason": "The launch needs funnel numbers.",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == "pwf_1"
    assert resp.json()["state"] == "awaiting_approval"
    assert resp.json()["message"]
    assert start.await_args.kwargs["provider_name"] == "posthog"
    assert start.await_args.kwargs["configuration"] == {"region": "us"}
    assert start.await_args.kwargs["reason"] == "The launch needs funnel numbers."
    assert resp.json()["reused"] is False
    # Nothing to step: the card was filed a millisecond ago, and Friday's next
    # status read is what moves it.
    step.assert_not_awaited()


def test_asking_twice_reuses_the_open_workflow_instead_of_refusing(client):
    """Friday retrying after a timeout is a no-op, not a conflict: `start`
    hands back the open row and the message must not claim a second card."""
    open_row = {
        "workflow_id": "pwf_1",
        "state": "awaiting_approval",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "reused": True,
    }
    with (
        patch.object(mod.provisioning_workflow, "start", AsyncMock(return_value=open_row)),
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == "pwf_1"
    assert resp.json()["reused"] is True
    assert resp.json()["message"] == mod.REUSED_MESSAGE
    # The in-memory marker must not ride along inside the safe shape.
    assert "reused" not in mod._safe_workflow(open_row)


@pytest.mark.parametrize(
    "code",
    [
        "not_eligible",
        "requirements_pending",
        "service_unavailable",
        "invalid_configuration",
        "no_company_stripe_account",
        "no_company",
        "needs_review_first",
    ],
)
def test_refusals_are_409s_the_agent_can_branch_on(client, code):
    with patch.object(
        mod.provisioning_workflow,
        "start",
        AsyncMock(side_effect=WorkflowRefused(code, "nope", requirements=["tos.stripe"])),
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == code
    assert resp.json()["detail"]["requirements"] == ["tos.stripe"]


def test_stripe_outage_is_a_503_the_agent_can_retry(client):
    """The CLASS is the classifier, not the code string — `is_transient` says so."""
    with patch.object(
        mod.provisioning_workflow,
        "start",
        AsyncMock(side_effect=TransientProvisioningError("transport_error", "Could not reach Stripe")),
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "stripe_unavailable"
    assert resp.json()["detail"]["stripe_code"] == "transport_error"


def test_an_unreachable_board_is_a_503_not_a_500(client):
    """`start` files the consent card, so a Paperclip outage surfaces HERE.
    Unmapped it is a 500 the agent reads as a platform fault it must never work
    around; 503 tells her to try on a later turn."""
    with patch.object(mod.provisioning_workflow, "start", AsyncMock(side_effect=httpx.ConnectError("board down"))):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "paperclip_unavailable"


def test_a_board_that_refuses_the_card_is_a_502(client):
    """Retrying a 4xx from the board changes nothing, so it must not be dressed
    up as an outage."""
    with patch.object(
        mod.provisioning_workflow,
        "start",
        AsyncMock(side_effect=PaperclipApiError("POST /approvals -> 400", 400, {"error": "bad"})),
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "paperclip_rejected"


def test_a_stripe_refusal_is_a_502_not_worth_retrying(client):
    with patch.object(
        mod.provisioning_workflow,
        "start",
        AsyncMock(side_effect=ProvisioningError("provider_failure", "Provider said no")),
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "stripe_error"
    assert resp.json()["detail"]["stripe_code"] == "provider_failure"


# --- validation (the body is LLM-written) ----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "PostHog!", "service": "svc_analytics"},
        {"provider": "posthog", "service": "svc analytics"},
        {"provider": "posthog\n", "service": "svc_analytics"},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k": "x" * 2000}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k": "a\nb"}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k" * 200: "v"}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k\ney": "v"}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k": {"nested": 1}}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {"k": [1, 2]}},
        {"provider": "posthog", "service": "svc_analytics", "configuration": {str(i): i for i in range(26)}},
        {"provider": "posthog", "service": "svc_analytics", "issue_id": "i" * 129},
        {"provider": "posthog", "service": "svc_analytics", "issue_id": "iss\n1"},
        {"provider": "posthog", "service": "svc_analytics", "reason": "r" * 501},
        {"provider": "posthog", "service": "svc_analytics", "reason": "line\nbreak"},
    ],
)
def test_a_hostile_body_is_a_422_with_no_side_effect(client, body):
    with (
        patch.object(mod.provisioning_workflow, "start", AsyncMock()) as start,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.post(f"{BASE}/requests", headers=HDRS, json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    start.assert_not_awaited()
    step.assert_not_awaited()


def test_a_scalar_configuration_is_accepted(client):
    row = {"workflow_id": "pwf_1", "state": "awaiting_approval", "provider_name": "posthog"}
    config = {"region": "us", "seats": 3, "beta": True, "ratio": 0.5, "note": None}
    with (
        patch.object(mod.provisioning_workflow, "start", AsyncMock(return_value=row)) as start,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)),
    ):
        resp = client.post(
            f"{BASE}/requests",
            headers=HDRS,
            json={"provider": "posthog", "service": "svc_analytics", "configuration": config},
        )
    assert resp.status_code == 200
    assert start.await_args.kwargs["configuration"] == config


# --- status, list ----------------------------------------------------------


def test_request_status_is_owner_scoped_and_safe(client):
    row = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "state": "provisioning",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "resource_id": "res_1",
        "account_request_id": "ar_1",
        "project_id": "proj_1",
        "error_code": None,
        "approval": {"terms_url": "https://posthog.com/terms"},
    }
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=row)) as step,
    ):
        resp = client.get(f"{BASE}/requests/pwf_1", headers=HDRS)
    body = resp.json()
    # The read IS the clock — an open row is moved one step by looking at it.
    step.assert_awaited_once_with("pwf_1")
    assert body["state"] == "provisioning"
    assert body["provider"] == "posthog"
    for leaked in (
        "account_request_id",
        "project_id",
        "redirect_url",
        "checkout_session_url",
        "access_configuration",
        "approval",
    ):
        assert leaked not in body


def test_the_safe_shape_is_the_only_shape(client):
    """`_safe_workflow` is the ONE serializer. A row stuffed with every
    dangerous field the rail can hold must still come out clean."""
    row = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "state": "active",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "resource_name": "svc_analytics-abc123",
        "env_keys": ["NEXT_PUBLIC_POSTHOG_KEY"],
        "account_request_id": "ar_1",
        "resource_id": "res_1",
        "project_id": "proj_1",
        "checkout_session_url": "https://checkout.stripe.com/c/pay/cs_test_1",
        "redirect_url": "https://connect.posthog.com/oauth?code=zzz",
        "access_configuration": {"api_key": "phc_supersecret"},
        "url": "https://deep.link/dashboard",
        "lease_owner": "task-1:42",
        "lease_until": "2026-08-29T00:00:00Z",
        "approval": {"paid_pricing": [{"freeform": "$0"}], "terms_url": "https://posthog.com/terms"},
        "provider_account": {
            "display_name": "Northwind",
            "primary_email": "f@example.com",
            "link_action": "created",
            "active_services": [],
            "provider_account_id": "pacct_secret",
        },
    }
    with patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)):
        resp = client.get(f"{BASE}/requests/pwf_1", headers=HDRS)
    body = resp.json()
    assert set(body) == {
        "workflow_id",
        "state",
        "provider",
        "service",
        "app_id",
        "environment",
        "app_name",
        "awaiting",
        "information_target",
        "error_code",
        "message",
        "provider_account",
        "resource_name",
        "resource_status",
        "env_keys",
        "created_at",
        "updated_at",
    }
    for secret in (
        "cs_test_1",
        "phc_supersecret",
        "deep.link",
        "pacct_secret",
        "proj_1",
        "ar_1",
        "res_1",
        "task-1",
        "acct_1",
    ):
        assert secret not in resp.text
    assert body["env_keys"] == ["NEXT_PUBLIC_POSTHOG_KEY"]


def test_another_owners_workflow_is_a_404_not_a_403(client):
    """404, not 403: a different status would confirm the id exists."""
    with patch.object(
        mod.workflow_repo,
        "get",
        AsyncMock(return_value={"workflow_id": "pwf_1", "owner_id": "org_OTHER", "state": "active"}),
    ):
        assert client.get(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 404


def test_requests_list_is_owner_scoped_and_newest_first(client):
    rows = [
        {
            "workflow_id": "pwf_1",
            "owner_id": "org_a",
            "state": "active",
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {
            "workflow_id": "pwf_2",
            "owner_id": "org_a",
            "state": "awaiting_approval",
            "provider_name": "vercel",
            "service_ref": "svc_hosting",
            "created_at": "2026-08-02T00:00:00Z",
        },
    ]
    with (
        patch.object(mod.workflow_repo, "list_for_owner", AsyncMock(return_value=rows)) as listed,
        patch.object(mod.provisioning_workflow, "advance_on_read", AsyncMock(return_value=None)) as step,
    ):
        resp = client.get(f"{BASE}/requests", headers=HDRS)
    assert [r["workflow_id"] for r in resp.json()["requests"]] == ["pwf_2", "pwf_1"]
    listed.assert_awaited_once_with("org_a")
    # Only the OPEN one is stepped; `pwf_1` is `active` and nothing reopens it.
    step.assert_awaited_once_with("pwf_2")


def test_status_exposes_the_provider_account_names_only(client):
    row = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "state": "active",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "provider_account": {
            "display_name": "Northwind",
            "primary_email": "f@example.com",
            "link_action": "created",
            "active_services": [],
        },
    }
    with patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)):
        body = client.get(f"{BASE}/requests/pwf_1", headers=HDRS).json()
    assert body["provider_account"]["display_name"] == "Northwind"
    assert body["provider_account"]["link_action"] == "created"


# --- cancel ----------------------------------------------------------------


def test_cancel_withdraws_an_ask_nothing_was_created_for(client):
    row = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "state": "awaiting_approval",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
    }
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=row)),
        patch.object(mod.workflow_repo, "transition", AsyncMock(return_value={**row, "state": "cancelled"})) as move,
    ):
        resp = client.delete(f"{BASE}/requests/pwf_1", headers=HDRS)
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    assert move.await_args.kwargs["to_state"] == "cancelled"


@pytest.mark.parametrize(
    "row",
    [
        {"state": "awaiting_user_action", "account_request_id": "ar_1"},
        {"state": "provisioning", "resource_id": "res_1"},
        {"state": "binding", "resource_id": "res_1"},
        {"state": "active", "resource_id": "res_1"},
    ],
)
def test_cancel_refuses_once_a_state_changing_post_has_been_made(client, row):
    """There is no idempotency contract and no undo: cancelling here would
    orphan a real provider resource nobody is tracking."""
    full = {
        "workflow_id": "pwf_1",
        "owner_id": "org_a",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        **row,
    }
    with (
        patch.object(mod.workflow_repo, "get", AsyncMock(return_value=full)),
        patch.object(mod.workflow_repo, "transition", AsyncMock()) as move,
    ):
        resp = client.delete(f"{BASE}/requests/pwf_1", headers=HDRS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "cannot_cancel"
    move.assert_not_awaited()


def test_cancel_of_another_owners_workflow_is_a_404(client):
    with (
        patch.object(
            mod.workflow_repo,
            "get",
            AsyncMock(return_value={"workflow_id": "pwf_1", "owner_id": "org_OTHER", "state": "ask"}),
        ),
        patch.object(mod.workflow_repo, "transition", AsyncMock()) as move,
    ):
        assert client.delete(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 404
    move.assert_not_awaited()


# --- the two gates, on every route -----------------------------------------


def test_every_container_route_requires_the_hook_token(monkeypatch, client):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod, "_require_hook_token", AsyncMock(side_effect=HTTPException(status_code=401, detail="invalid hook token"))
    )
    assert client.get(f"{BASE}/catalog", headers=HDRS).status_code == 401
    assert (
        client.post(
            f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"}
        ).status_code
        == 401
    )
    assert client.get(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 401
    assert client.get(f"{BASE}/requests", headers=HDRS).status_code == 401
    assert client.delete(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 401


def test_every_container_route_is_paid_only(monkeypatch, client):
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
    assert client.get(f"{BASE}/catalog", headers=HDRS).status_code == 402
    assert (
        client.post(
            f"{BASE}/requests", headers=HDRS, json={"provider": "posthog", "service": "svc_analytics"}
        ).status_code
        == 402
    )
    assert client.get(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 402
    assert client.get(f"{BASE}/requests", headers=HDRS).status_code == 402
    assert client.delete(f"{BASE}/requests/pwf_1", headers=HDRS).status_code == 402


def test_the_paid_gate_runs_before_the_body_is_trusted(monkeypatch, client):
    """Order: hook token -> paid gate -> validation. A hostile body from an
    unentitled owner is a 402, never a 422 that says which field was wrong."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(side_effect=HTTPException(status_code=402, detail={"code": "routines_require_subscription"})),
    )
    resp = client.post(f"{BASE}/requests", headers=HDRS, json={"provider": "NOPE!", "service": "x y"})
    assert resp.status_code == 402


def test_request_from_a_task_run_asks_on_the_task(client, monkeypatch):
    """Spec 05 P1: `X-Lightbulb-Run` names a live task run (switch on) → `start` gets its task, and the Worker is
    told to end its turn on the Question."""
    find = AsyncMock(return_value={"taskId": "t-1"})
    monkeypatch.setattr(mod.provisioning_questions, "task_for_run", find)
    row = {
        "workflow_id": "pwf_1",
        "state": "awaiting_approval",
        "provider_name": "posthog",
        "service_ref": "svc_analytics",
        "question": {"taskId": "t-1", "qid": "q-1", "workflowId": "pwf_1"},
    }
    with patch.object(mod.provisioning_workflow, "start", AsyncMock(return_value=row)) as start:
        resp = client.post(
            f"{BASE}/requests",
            headers={**HDRS, "X-Lightbulb-Run": "run-1"},
            json={"provider": "posthog", "service": "svc_analytics"},
        )
    assert resp.status_code == 200
    find.assert_awaited_once_with("org_a", "run-1")
    assert start.await_args.kwargs["task_id"] == "t-1"
    assert resp.json()["questionId"] == "q-1" and resp.json()["message"] == mod.QUESTION_MESSAGE
    assert "question" not in resp.json()
