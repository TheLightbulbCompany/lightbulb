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

ALL_TRUE = {"money_moves": True, "outreach": True, "publishing": True, "provisioning": True, "approval_mode": "auto"}


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
def _lease(monkeypatch):
    """revoke-all marks the owner revoked and takes the lifecycle lease: admit both without a table."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "mark_oauth_revoked", AsyncMock(return_value=200))
    monkeypatch.setattr(container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(mod.slack_installs_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod.website_logins, "list_logins", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod.link_wallet, "disconnect", AsyncMock(return_value=False))


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


# --- approval mode (founder ruling 6 Nov 2026) ----------------------------


def _patch_mode(c, body):
    run = AsyncMock(return_value={})
    with (
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(billing_repo, "_get_table", MagicMock()),
        patch.object(billing_repo, "run_in_thread", run),
    ):
        return c.patch("/api/v1/company/policy", json=body), run


def test_approval_mode_ask_and_auto_are_stored_and_audited(client, monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr(mod.questions, "audit", audit)
    resp, run = _patch_mode(client, {"approval_mode": "ask"})
    assert resp.status_code == 200 and resp.json()["approval_mode"] == "ask"
    assert run.await_args.kwargs["ExpressionAttributeValues"] == {":p": {"approval_mode": "ask"}}
    audit.assert_awaited_once_with("user_admin", "org_a", "approval.mode", {"mode": "ask"})


def test_never_ask_needs_the_typed_confirmation(client):
    resp, run = _patch_mode(client, {"approval_mode": "never_ask"})
    assert resp.status_code == 422
    run.assert_not_awaited()
    resp, run = _patch_mode(client, {"approval_mode": "never_ask", "confirm": "never ask"})
    assert resp.status_code == 200 and resp.json()["approval_mode"] == "never_ask"
    assert run.await_args.kwargs["ExpressionAttributeValues"] == {":p": {"approval_mode": "never_ask"}}


def test_never_ask_is_admin_only(member_client):
    resp, run = _patch_mode(member_client, {"approval_mode": "never_ask", "confirm": "never ask"})
    assert resp.status_code == 403
    run.assert_not_awaited()


def test_unknown_mode_is_422(client):
    resp, run = _patch_mode(client, {"approval_mode": "yolo"})
    assert resp.status_code == 422
    run.assert_not_awaited()


# --- revoke-all ------------------------------------------------------------


def _revoke_mocks():
    manager = MagicMock()
    cancel = AsyncMock(return_value=(1, 1))
    sdelete = AsyncMock(return_value=True)
    deauth = AsyncMock(return_value=True)
    manager.attach_mock(cancel, "cancel")
    manager.attach_mock(sdelete, "sdelete")
    manager.attach_mock(deauth, "deauth")
    return manager, cancel, sdelete, deauth


def test_revoke_all_composes_each_primitive_once_in_order(client, facts):
    manager, cancel, sdelete, deauth = _revoke_mocks()
    rows = [{"provider": "projects/stripe"}, {"provider": "projects/posthog"}]
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
        ],
        "failed": [],
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
    assert resp.json() == {"revoked": ["workflows_cancelled:1", "secret:posthog"], "failed": []}
    deauth.assert_not_awaited()


def test_revoke_all_on_a_clean_workspace_reports_nothing_revoked(client):
    """The toast counts `revoked`: no open workflows and no stored posthog token must
    read as 0, not as "Revoked 2 item(s)"."""
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", AsyncMock(return_value=(0, 0))),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "delete", AsyncMock(return_value=False)),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json() == {"revoked": [], "failed": []}


def test_revoke_all_deletes_every_saved_website_login(client, monkeypatch):
    """A saved website password is a credential copy we hold; the kill switch left them all."""
    _, cancel, sdelete, _ = _revoke_mocks()
    monkeypatch.setattr(mod.website_logins, "list_logins", AsyncMock(return_value=[{"id": "l1"}, {"id": "l2"}]))
    delete_login = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.website_logins, "delete_login", delete_login)
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json()["revoked"][-2:] == ["website_login:l1", "website_login:l2"]
    assert [c.args for c in delete_login.await_args_list] == [("org_a", "l1"), ("org_a", "l2")]


def test_a_website_login_delete_failure_is_reported_and_stripe_still_disconnects(client, monkeypatch):
    _, cancel, sdelete, deauth = _revoke_mocks()
    logins = [{"id": "l1"}, {"id": "l2"}, {"id": "l3"}]
    monkeypatch.setattr(mod.website_logins, "list_logins", AsyncMock(return_value=logins))
    delete_login = AsyncMock(side_effect=[True, RuntimeError("ddb down"), True])
    monkeypatch.setattr(mod.website_logins, "delete_login", delete_login)
    billing_row = {"owner_id": "org_a", "company_stripe_account_id": "acct_1"}
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value=billing_row)),
        patch.object(mod.company_stripe, "deauthorize", deauth),
        patch.object(billing_repo, "clear_company_stripe_account", AsyncMock(return_value=billing_row)),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json()["failed"] == ["website_login:l2"]
    assert resp.json()["revoked"][-3:] == ["website_login:l1", "website_login:l3", "stripe_connect:acct_1"]
    assert delete_login.await_count == 3
    deauth.assert_awaited_once()


class _Guard:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def test_revoke_all_disconnects_every_connected_app_and_reports_the_one_that_failed(client):
    """The dialog says it "deletes our copies of your credentials". It deleted the projects/*
    blobs and left every OAuth app connected. Each remaining row goes through the Disconnect
    route's own teardown; one that fails is reported and the rest still go."""
    _, cancel, sdelete, _ = _revoke_mocks()
    rows = [
        {"provider": "projects/stripe"},
        {"provider": "google"},
        {"provider": "slack"},
        {"provider": "github"},
    ]

    async def teardown(owner_id, provider, p):
        if provider == "slack":
            raise RuntimeError("s3 down")

    torn = AsyncMock(side_effect=teardown)
    order = MagicMock()
    mark = AsyncMock(return_value=200)
    listing = AsyncMock(return_value=rows)
    order.attach_mock(mark, "mark")
    order.attach_mock(listing, "list")
    order.attach_mock(torn, "teardown")
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", listing),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.container_repo, "mark_oauth_revoked", mark),
        patch.object(mod.connections, "owner_lifecycle_guard", lambda owner_id: _Guard()),
        patch.object(mod.connections, "_get_provider_for_teardown", lambda provider: provider),
        patch.object(mod.connections, "_disconnect_teardown", torn),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    assert resp.json() == {
        "revoked": [
            "workflows_cancelled:1",
            "secret:projects/stripe",
            "secret:posthog",
            "connection:google",
            "connection:github",
        ],
        "failed": ["slack"],
    }
    # The Projects rail's row is not an OAuth connection: its blob is deleted above and it
    # never reaches the OAuth teardown (which is keyed by the bare provider).
    assert [c.args[:2] for c in torn.await_args_list] == [("org_a", "google"), ("org_a", "slack"), ("org_a", "github")]
    # The mark is down before the rows are read, so a callback that lands after this snapshot
    # finds it and refuses (oauth_callback); one that was mid-write is waited out by the guard.
    mark.assert_awaited_once_with("org_a")
    assert [name for name, _, _ in order.mock_calls][:2] == ["mark", "list"]


@pytest.mark.parametrize("fails", [False, True])
def test_revoke_all_uninstalls_every_slack_workspace(client, fails):
    """Slack is not a registry provider: its bare row used to be skipped as a reserved key while
    the installs, their slack/<team> tokens and channel routes stayed live. Each active install
    goes through the Disconnect route's revoke_install; one that fails puts Slack in `failed`."""
    _, cancel, sdelete, _ = _revoke_mocks()
    installs = [
        {"team_id": "T1", "status": "active"},
        {"team_id": "T2", "status": "active"},
        {"team_id": "T0", "status": "revoked"},
    ]

    async def revoke(team_id, expected_owner_id=None):
        if fails and team_id == "T1":
            raise RuntimeError("ddb down")
        return True

    revoke_install = AsyncMock(side_effect=revoke)
    listing = AsyncMock(return_value=[{"provider": "slack", "team_ids": {"T1", "T8", "T9"}}])
    # Each team's install row, read strongly by team id. T8 was ours once; another owner has
    # installed that workspace since, so its row, token and routes are theirs and stay.
    current = {
        "T1": {"owner_id": "org_a", "status": "active"},
        "T2": {"owner_id": "org_a", "status": "active"},
        "T8": {"owner_id": "org_other", "status": "active"},
        "T9": {"owner_id": "org_a", "status": "active"},
    }
    with (
        patch.object(mod.account_wipe, "cancel_open_workflows", cancel),
        patch.object(mod.integrations_repo, "list_for_owner", listing),
        patch.object(mod.integration_secret_store, "delete", sdelete),
        patch.object(billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.slack_installs_repo, "list_for_owner", AsyncMock(return_value=installs)),
        patch.object(mod.slack_install_service, "revoke_install", revoke_install),
        patch.object(mod.slack_installs_repo, "get", AsyncMock(side_effect=current.get)),
    ):
        resp = client.post("/api/v1/company/revoke-all", json={"confirm": "revoke"})
    assert resp.status_code == 200
    # T8 is not ours any more: never revoked. T9 is on the owner's slack row (read strongly) but not yet on the by-owner GSI: still revoked.
    assert [c.args for c in revoke_install.await_args_list] == [("T1",), ("T2",), ("T9",)]
    assert {c.kwargs["expected_owner_id"] for c in revoke_install.await_args_list} == {"org_a"}
    listing.assert_awaited_once_with("org_a", consistent=True)
    body = resp.json()
    if fails:
        assert body["failed"] == ["slack"] and "slack:T2" in body["revoked"] and "slack:T1" not in body["revoked"]
    else:
        assert body["failed"] == [] and body["revoked"][-3:] == ["slack:T1", "slack:T2", "slack:T9"]


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


def test_always_allow_rules_list_and_remove(client, monkeypatch):
    rules = [{"id": "always-abc", "kind": "mail.send", "key": "to", "value": "x@example.com"}]
    monkeypatch.setattr(mod.questions, "always_allow_rules", AsyncMock(return_value=rules))
    monkeypatch.setattr(mod.questions, "remove_always_allow", AsyncMock(side_effect=[True, False]))
    assert client.get("/api/v1/company/policy/always-allow").json() == {"rules": rules}
    assert client.delete("/api/v1/company/policy/always-allow/always-abc").status_code == 200
    mod.questions.remove_always_allow.assert_awaited_with("org_a", "always-abc", by="user_admin")
    assert client.delete("/api/v1/company/policy/always-allow/always-abc").status_code == 404


def test_always_allow_rules_are_admin_only(member_client):
    assert member_client.get("/api/v1/company/policy/always-allow").status_code == 403
    assert member_client.delete("/api/v1/company/policy/always-allow/always-abc").status_code == 403
