"""/api/v1/company/policy + /revoke-all — the P0d slice.

The policy row is fail-closed (absent key ⇒ True = require approval), the
PATCH is a validated merge, and revoke-all composes EXISTING primitives in a
fixed order behind a typed confirm word. Enforcement of the map inside the
rails is P1 — these tests cover only the row + surface.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.repositories import billing_repo  # noqa: E402
from main import app  # noqa: E402
from routers import company_policy as mod  # noqa: E402

ALL_TRUE = {"money_moves": True, "outreach": True, "publishing": True, "provisioning": True}


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
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


# --- GET: defaults fill ----------------------------------------------------


def test_get_policy_fills_all_four_defaults_true(client):
    with patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/policy")
    assert resp.status_code == 200
    assert resp.json() == ALL_TRUE


def test_get_policy_absent_key_is_true_stored_false_survives(client):
    row = {"owner_id": "org_a", "capability_policy": {"money_moves": False}}
    with patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        resp = client.get("/api/v1/company/policy")
    assert resp.status_code == 200
    assert resp.json() == {**ALL_TRUE, "money_moves": False}


# --- PATCH: merge + validate ----------------------------------------------


def test_patch_merges_over_stored_and_returns_the_filled_map(client):
    row = {"owner_id": "org_a", "capability_policy": {"money_moves": False}}
    run = AsyncMock(return_value={})
    with (
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(billing_repo, "_get_table", MagicMock()),
        patch.object(billing_repo, "run_in_thread", run),
    ):
        resp = client.patch("/api/v1/company/policy", json={"outreach": False})
    assert resp.status_code == 200
    assert resp.json() == {**ALL_TRUE, "money_moves": False, "outreach": False}
    # The write persists only explicit keys (merged over stored) — never the
    # filled defaults, so "absent ⇒ True" stays live for future readers.
    assert run.await_args.kwargs["ExpressionAttributeValues"] == {":p": {"money_moves": False, "outreach": False}}


def test_patch_unknown_capability_is_422_and_writes_nothing(client):
    run = AsyncMock()
    with (
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(billing_repo, "run_in_thread", run),
    ):
        resp = client.patch("/api/v1/company/policy", json={"bogus": True})
    assert resp.status_code == 422
    run.assert_not_awaited()


# --- revoke-all ------------------------------------------------------------


def _revoke_mocks():
    manager = MagicMock()
    cancel = AsyncMock(return_value=(1, 1))
    sdelete = AsyncMock()
    deauth = AsyncMock(return_value=True)
    manager.attach_mock(cancel, "cancel")
    manager.attach_mock(sdelete, "sdelete")
    manager.attach_mock(deauth, "deauth")
    return manager, cancel, sdelete, deauth


def test_revoke_all_composes_each_primitive_once_in_order(client, facts):
    manager, cancel, sdelete, deauth = _revoke_mocks()
    rows = [
        {"provider": "projects/stripe"},
        {"provider": "google"},  # OAuth rail row — not ours to delete here
        {"provider": "projects/posthog"},
    ]
    billing_row = {"owner_id": "org_a", "company_stripe_account_id": "acct_1"}
    clear = AsyncMock(return_value=billing_row)
    manager.attach_mock(clear, "clear")
    manager.attach_mock(facts, "facts")
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value=billing_row)),
        patch.object(mod.company_stripe, "deauthorize", deauth),
        patch.object(billing_repo, "clear_company_stripe_account", clear),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json() == {
        "revoked": [
            "workflows_cancelled:1",
            "secret:projects/stripe",
            "secret:projects/posthog",
            "secret:posthog",
            "stripe_connect:acct_1",
        ]
    }
    cancel.assert_awaited_once_with("org_a")
    deauth.assert_awaited_once_with(account_id="acct_1")
    clear.assert_awaited_once_with("org_a", account_id="acct_1")
    facts.assert_awaited_once_with("org_a")
    assert sdelete.await_count == 3
    # Order: workflows closed first, then our secret copies, then Stripe
    # (deauthorize → unbind → facts rewrite).
    assert [name for name, _, _ in manager.mock_calls] == [
        "cancel",
        "sdelete",
        "sdelete",
        "sdelete",
        "deauth",
        "clear",
        "facts",
    ]


def test_revoke_all_unbinds_and_still_succeeds_when_stripe_errors(client, facts):
    """A kill switch that 500s is not a kill switch. The Stripe call was
    unguarded and not inside a swallow, so a Stripe outage — or a `created`
    account Stripe refuses to deauthorize at all — raised straight out of the
    request, leaving the row bound and the owner with a dead button."""
    _, cancel, sdelete, _ = _revoke_mocks()
    billing_row = {"owner_id": "org_a", "company_stripe_account_id": "acct_1"}
    clear = AsyncMock(return_value=billing_row)
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value=billing_row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(side_effect=RuntimeError("stripe down"))),
        patch.object(billing_repo, "clear_company_stripe_account", clear),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    # The unbind happened, and the unconfirmed revoke is REPORTED, not hidden.
    clear.assert_awaited_once_with("org_a", account_id="acct_1")
    assert resp.json()["revoked"][-1] == "stripe_connect:acct_1(stripe_unconfirmed)"
    facts.assert_awaited_once_with("org_a")


def test_revoke_all_skips_stripe_when_unbound(client):
    _, cancel, sdelete, deauth = _revoke_mocks()
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.company_stripe, "deauthorize", deauth),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json() == {"revoked": ["workflows_cancelled:1", "secret:posthog"]}
    deauth.assert_not_awaited()


@pytest.mark.parametrize("body", [{}, {"confirm": "REVOKE"}, {"confirm": "revoke everything"}])
def test_revoke_all_requires_the_exact_confirm_word(client, body):
    _, cancel, sdelete, deauth = _revoke_mocks()
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(mod.company_stripe, "deauthorize", deauth),
    ):
        resp = client.post("/api/v1/company/revoke-all", json=body)
    assert resp.status_code == 422
    cancel.assert_not_awaited()
    sdelete.assert_not_awaited()
    deauth.assert_not_awaited()


# --- gating ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/v1/company/policy", None),
        ("patch", "/api/v1/company/policy", {"outreach": False}),
        ("post", "/api/v1/company/revoke-all", {"confirm": "revoke"}),
    ],
)
def test_org_member_is_403_on_every_route(member_client, method, path, body):
    resp = getattr(member_client, method)(path, **({"json": body} if body is not None else {}))
    assert resp.status_code == 403
