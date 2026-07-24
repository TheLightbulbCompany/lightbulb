"""Tests for ``PaperclipProvisioning`` orchestrator.

Strategy:

* The DynamoDB repo is real (moto-backed) so idempotency invariants
  + GSI lookups are exercised end-to-end.
* The Paperclip admin client is a plain ``MagicMock`` with ``AsyncMock``
  methods so we can assert call ordering and shape per scenario.
* ``ENCRYPTION_KEY`` and ``PAPERCLIP_SERVICE_TOKEN_KEY`` are set via
  ``conftest.py`` / fixture below so encrypt/mint round-trips work.

``asyncio_mode = "auto"`` (pyproject.toml) is in effect — async tests
need no decorator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services.paperclip_provisioning import (
    OrgNotProvisionedError,
    PaperclipProvisioning,
    _ws_gateway_url,
)
from core.services.paperclip_admin_client import PaperclipApiError

TABLE_NAME = "test-paperclip-companies-prov"


@pytest.fixture(autouse=True)
def _service_token_key(monkeypatch):
    """Ensure service_token.mint() can run inside provisioning.

    ``service_token.mint`` reads the env var lazily on each call, so it
    must be present whenever the orchestrator runs.
    """
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-service-token-key")


@pytest.fixture(autouse=True)
def _stub_admin_session(monkeypatch):
    """Stub get_admin_session_cookie to return a fake cookie for all tests.

    The real implementation calls Secrets Manager (boto3) + Better Auth
    sign-in (httpx) — both unwanted in unit tests. Stubbing returns
    ``"admin-cookie-test"`` so provision_org's calls to admin-bearing
    endpoints carry a deterministic value tests can assert on.
    """

    async def _fake_admin_cookie(_http_client):
        return "admin-cookie-test"

    monkeypatch.setattr(
        "core.services.paperclip_admin_session.get_admin_session_cookie",
        _fake_admin_cookie,
    )
    # The provisioning code imports from this module too — patch in case
    # someone re-imports.
    import core.services.paperclip_provisioning as prov_mod

    if hasattr(prov_mod, "get_admin_session_cookie"):
        monkeypatch.setattr(prov_mod, "get_admin_session_cookie", _fake_admin_cookie, raising=False)


@pytest.fixture
def repo():
    """A moto-backed PaperclipRepo with the by-org-id + by-status-purge-at GSIs."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "scheduled_purge_at", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-status-purge-at",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "scheduled_purge_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                {
                    "IndexName": "by-org-id",
                    "KeySchema": [{"AttributeName": "org_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield PaperclipRepo(table_name=TABLE_NAME, region="us-east-1")


def _make_admin_mock() -> MagicMock:
    """Build an AsyncMock-backed admin client with sensible default returns.

    Tests override individual methods (e.g. to make ``sign_up_user``
    raise) per-scenario.
    """
    admin = MagicMock()
    # The provisioning code passes ``self._admin._http`` into
    # get_admin_session_cookie. Stubbed in _stub_admin_session fixture
    # but the attribute access still has to succeed — give it a
    # placeholder.
    admin._http = MagicMock()
    admin.sign_up_user = AsyncMock(
        return_value={"user": {"id": "pc_user_default"}, "_session_cookie": "session-default"}
    )
    admin.sign_in_user = AsyncMock(
        return_value={"user": {"id": "pc_user_default"}, "_session_cookie": "session-default"}
    )
    admin.create_company = AsyncMock(return_value={"id": "co_default"})
    admin.create_agent = AsyncMock(return_value={"id": "agent_default"})
    # Builtin-agent purge (step 7c): list then attempt to delete non-seeded
    # agents. Default to an empty roster so nothing is deleted unless a test opts
    # in. (Paperclip's own built-in agents are undeletable — DELETE answers 409
    # built_in_agent_undeletable — but the purge no longer pre-touches routines:
    # the BFF clears a deletable agent's assigned routines in the same delete.)
    admin.list_agents = AsyncMock(return_value=[])
    admin.delete_agent = AsyncMock(return_value={})
    # Retained as harmless defaults for unrelated routine-management tests.
    admin.list_routines = AsyncMock(return_value=[])
    admin.delete_routine = AsyncMock(return_value={})
    admin.create_invite = AsyncMock(return_value={"token": "invite-token-default"})
    # accept_invite returns the join request flat — see access.ts:3604
    # (toJoinRequestResponse spreads the row, so ``id`` is top-level).
    admin.accept_invite = AsyncMock(return_value={"id": "req_default"})
    admin.approve_join_request = AsyncMock(return_value={})
    return admin


# ----------------------------------------------------------------------
# _ws_gateway_url
# ----------------------------------------------------------------------


def test_ws_gateway_url_prod_drops_suffix():
    assert _ws_gateway_url("prod") == "wss://ws.isol8.co"
    assert _ws_gateway_url("production") == "wss://ws.isol8.co"


def test_ws_gateway_url_dev_uses_env_suffix():
    assert _ws_gateway_url("dev") == "wss://ws-dev.isol8.co"
    assert _ws_gateway_url("staging") == "wss://ws-staging.isol8.co"


def test_ws_gateway_url_empty_falls_back_to_localhost():
    assert _ws_gateway_url("") == "ws://localhost:8000"


# ----------------------------------------------------------------------
# provision_org
# ----------------------------------------------------------------------


async def test_provision_org_happy_path(repo, monkeypatch):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})

    # The company name is auto-generated now. ``owner_id="org_acme"`` is an org,
    # so generation consults Clerk first — pin that to None so this asserts the
    # deterministic email-derived fallback instead of depending on whether a
    # Clerk key happens to be configured in the test environment.
    from core.services import company_identity

    async def _no_clerk_org(org_id):
        return None

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", _no_clerk_org)

    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )

    # API call sequence
    admin.sign_up_user.assert_awaited_once()
    sign_up_kwargs = admin.sign_up_user.call_args.kwargs
    assert sign_up_kwargs["email"] == "owner@acme.test"
    assert sign_up_kwargs["name"] == "owner@acme.test"
    assert len(sign_up_kwargs["password"]) >= 32  # token_urlsafe(32) -> ~43 chars

    # create_company now runs as the BOOTSTRAP ADMIN (not the new user)
    # because Paperclip's POST /api/companies requires instance_admin.
    # The admin session token comes from get_admin_session_cookie, which
    # the autouse _stub_admin_session fixture pins to "admin-cookie-test".
    admin.create_company.assert_awaited_once()
    cc_kwargs = admin.create_company.call_args.kwargs
    # Companies used to be named with the owner's raw email address. They now
    # get a real generated name; with no Clerk org name available that is the
    # prettified email local part. (The Better Auth *user* name above is a
    # different field and deliberately still the email.)
    assert cc_kwargs["name"] == "Owner"
    assert cc_kwargs["session_cookie"] == "admin-cookie-test"
    assert cc_kwargs["idempotency_key"] == "user_owner"

    # New user is added as a co-owner via the invite/accept/approve chain.
    admin.create_invite.assert_awaited_once()
    invite_kwargs = admin.create_invite.call_args.kwargs
    assert invite_kwargs["company_id"] == "co_acme"
    assert invite_kwargs["session_cookie"] == "admin-cookie-test"  # admin invites
    # Invite must carry humanRole="owner" so the new user gets
    # agents:create when Paperclip's resolveHumanInviteRole runs;
    # otherwise the role defaults to "operator" and they 403 on
    # POST /api/companies/{co}/agents.
    assert invite_kwargs["human_role"] == "owner"
    admin.accept_invite.assert_awaited_once()
    accept_kwargs = admin.accept_invite.call_args.kwargs
    assert accept_kwargs["session_cookie"] == "owner-session-1"  # user accepts
    admin.approve_join_request.assert_awaited_once()
    approve_kwargs = admin.approve_join_request.call_args.kwargs
    assert approve_kwargs["session_cookie"] == "admin-cookie-test"  # admin approves

    # provision_org seeds TWO agents in one step: Friday (ceo) and Atlas
    # (recruiter). The existing kwargs assertions below describe the
    # Friday seed; Atlas is exercised in the unit-level suite at
    # tests/unit/services/test_paperclip_provisioning.py.
    assert admin.create_agent.await_count == 2
    by_name = {call.kwargs["name"]: call.kwargs for call in admin.create_agent.await_args_list}
    assert set(by_name) == {"Friday", "Atlas"}

    agent_kwargs = by_name["Friday"]
    assert agent_kwargs["company_id"] == "co_acme"
    # Canonical adapter_type is the underscore form ("openclaw_gateway")
    # — the previous hyphenated value ("openclaw-gateway") was rejected
    # by Paperclip's assertKnownAdapterType so seed-agent creation
    # silently fails on every provision.
    assert agent_kwargs["adapter_type"] == "openclaw_gateway"
    adapter_cfg = agent_kwargs["adapter_config"]
    assert adapter_cfg["url"] == f"wss://ws-dev.isol8.co?token={adapter_cfg['authToken']}&clientId=paperclip-operator"
    assert agent_kwargs["adapter_config"]["sessionKey"] == "user_owner"
    assert agent_kwargs["adapter_config"]["sessionKeyStrategy"] == "issue"
    assert agent_kwargs["adapter_config"]["authToken"]  # JWT minted
    # Agent is owned BY the user (their session) so they can edit it later.
    assert agent_kwargs["session_cookie"] == "owner-session-1"

    # Persisted row
    assert row.user_id == "user_owner"
    assert row.org_id == "org_acme"
    assert row.company_id == "co_acme"
    assert row.paperclip_user_id == "pc_user_owner"
    assert row.status == "active"
    assert row.paperclip_password_encrypted  # not empty
    assert row.service_token_encrypted  # not empty

    # Repo round-trip
    fetched = await repo.get("user_owner")
    assert fetched is not None
    assert fetched.company_id == "co_acme"
    assert fetched.status == "active"


async def test_provision_org_idempotent_on_second_call(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    second = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )

    # Second call short-circuits — only ONE sign_up + create_company across both
    assert admin.sign_up_user.await_count == 1
    assert admin.create_company.await_count == 1
    assert first.company_id == second.company_id == "co_acme"


async def test_reprovision_with_company_preserved_funnel_stays_complete_no_reprompt(repo):
    """L1, end to end: a container-only reprovision (e.g. `DELETE
    /debug/provision` then `POST /debug/provision`, or the periodic
    /container/status poll re-running the Paperclip seed step) NEVER mints a
    new company — it hits provision_org's ``status=="active"`` idempotency
    short-circuit, which returns the EXISTING row completely unchanged (ZERO
    writes). A funnel that already reached "complete" therefore survives a
    container recreation by construction, and the owner is never re-shown
    the first-run funnel."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    provisioned = await prov.provision_org(
        owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test"
    )
    generation = provisioned.onboarding_generation

    # Simulate the funnel having reached "complete" (a later PR's bootstrap
    # dispatcher would set this on observed chat.final; direct write here
    # isolates this test from that machinery).
    await repo.claim_funnel_submission("user_owner", onboarding_generation=generation)
    await repo.record_onboarding_ids(
        "user_owner", onboarding_generation=generation, goal_id="goal-1", project_id="proj-1"
    )
    table = repo._table()
    table.update_item(
        Key={"user_id": "user_owner"},
        UpdateExpression="SET funnel_state = :c",
        ExpressionAttributeValues={":c": "complete"},
    )
    assert (await repo.get("user_owner")).funnel_state == "complete"

    # A container-only recreation (G1) never touches this table at all — the
    # part of the flow THIS test proves is the Paperclip-side leg that DOES
    # run again on every /container/status poll (_ensure_paperclip_for_owner
    # calls provision_org unconditionally, best-effort). It must be a no-op.
    reprovisioned = await prov.provision_org(
        owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test"
    )

    assert admin.sign_up_user.await_count == 1  # no re-signup
    assert admin.create_company.await_count == 1  # no new company
    assert reprovisioned.onboarding_generation == generation
    assert reprovisioned.funnel_state == "complete"  # unchanged — no re-prompt
    assert reprovisioned.onboarding_goal_id == "goal-1"

    fetched = await repo.get("user_owner")
    assert fetched.funnel_state == "complete"
    assert fetched.onboarding_generation == generation


async def test_provision_org_seed_agent_failure_is_not_fatal(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    # Friday (the REQUIRED CEO) lands; the OPTIONAL Atlas seed fails. An
    # optional-seed failure is best-effort and must NOT block the active
    # company. (A FAILED Friday, by contrast, leaves the company non-active —
    # covered by the NEW-2 unit test in tests/unit/services.)
    admin.create_agent = AsyncMock(
        side_effect=[
            {"id": "agent_friday"},
            PaperclipApiError("atlas create failed", status_code=500, body="boom"),
        ]
    )
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    # Company is still active; the optional (Atlas) agent seed is best-effort.
    assert row.status == "active"
    assert row.company_id == "co_acme"


async def test_provision_org_signup_failure_writes_durable_failed_marker(repo):
    """A pre-company failure — here sign-up itself fails, so no provisioning row
    was written yet — must leave ONE durable ``failed`` marker so ``/teams``
    gives an honest 503 instead of the eternal 202 spinner a MISSING row
    produces. The marker carries NO fabricated paperclip_user_id: an empty id
    was the old poison (blocked adoption → re-sign-up → re-422 forever; clobbered
    a real id on a stale read), so the field is left ABSENT and the marker is
    written put-if-absent (durable-visibility only, not adoptable). provision_org
    still RAISES so the caller records the failure."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("auth disabled", status_code=403, body="forbidden"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(
            owner_id="org_acme",
            owner_user_id="user_owner",
            owner_email="owner@acme.test",
        )

    marker = await repo.get("user_owner")
    assert marker is not None, "a durable failed marker must exist (honest 503, not eternal spinner)"
    assert marker.status == "failed"
    # The raw item carries NO paperclip_user_id attribute at all — absent, not ""
    # — the exact distinction from the old poisoned empty-id placeholder.
    raw = repo._table().get_item(Key={"user_id": "user_owner"})["Item"]
    assert "paperclip_user_id" not in raw


async def test_provision_org_create_company_failure_marks_failed(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("server err", status_code=500, body="fail"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(
            owner_id="org_acme",
            owner_user_id="user_owner",
            owner_email="owner@acme.test",
        )

    failed_row = await repo.get("user_owner")
    assert failed_row is not None
    assert failed_row.status == "failed"


async def test_provision_org_retries_after_failure(repo):
    """A failed row should not block a subsequent successful retry."""
    admin = _make_admin_mock()
    # First attempt: create_company fails
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("transient", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_org(
            owner_id="org_acme",
            owner_user_id="user_owner",
            owner_email="owner@acme.test",
        )
    assert (await repo.get("user_owner")).status == "failed"

    # Second attempt with healthy admin
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    row = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    assert row.status == "active"
    assert row.company_id == "co_acme"


async def test_provision_org_reconciles_failed_company_without_reminting(repo):
    """A persistent Friday(CEO)-seed outage must retry the seed against the
    SAME company — never mint a new orphan company on each poll.

    provision_org persists a non-active ``failed`` row carrying the real
    company_id when the required Friday seed doesn't land (a CEO-less company
    must never be marked active). The ``status=="active"`` short-circuit never
    fires for that row, so before this reconcile every ``/container/status``
    poll re-ran the full create + invite/accept/approve chain and — because
    Paperclip ignores our ``Idempotency-Key`` — minted a brand-new company each
    time. The reconcile adopts the stored company_id and resumes at the seed.
    """
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-2"})
    # A DISTINCT id per create_company call, so a second create (the bug) would
    # be observable as a different company_id on the reconciled row.
    admin.create_company = AsyncMock(side_effect=[{"id": "co_first"}, {"id": "co_second"}])
    # Friday (+ Atlas) seed fails on every attempt — a persistent outage.
    admin.create_agent = AsyncMock(side_effect=PaperclipApiError("friday create failed", status_code=500, body="boom"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    # First attempt created the company but couldn't seed Friday -> non-active
    # row carrying the real company_id.
    assert first.status == "failed"
    assert first.company_id == "co_first"
    assert admin.create_company.await_count == 1

    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    # The reconcile ADOPTED the existing company: create_company was NOT called
    # a second time, no second invite was issued, and the row still carries the
    # ORIGINAL company_id (never "co_second").
    assert admin.create_company.await_count == 1
    assert admin.create_invite.await_count == 1  # only the first attempt invited
    assert second.company_id == "co_first"
    assert second.status == "failed"  # Friday still down -> still non-active, same company

    # Adoption signed IN (with the stored credential) rather than re-signing-up,
    # which would 422 against the now-existing Better Auth user.
    admin.sign_in_user.assert_awaited()
    assert admin.sign_up_user.await_count == 1

    # Persisted row confirms no new company was minted across the two polls.
    fetched = await repo.get("user_owner")
    assert fetched is not None
    assert fetched.company_id == "co_first"
    assert fetched.status == "failed"


# ----------------------------------------------------------------------
# Onboarding: G2 mint + writer safety (spec PR-4 / ONBOARD-TABLE)
#
# See docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md
# §2's "load-bearing safety requirement": onboarding_generation is minted
# EXACTLY ONCE at G2 (fresh company only, INV-3), and every later whole-item
# put on the same row (notably the reconcile-adopt retry path) must carry
# those fields forward untouched (INV-4) or co-location reintroduces the
# clobber a dedicated table would have avoided.
# ----------------------------------------------------------------------


async def test_provision_org_fresh_create_mints_onboarding_generation_at_g2(repo):
    """A genuinely-new company (Friday seed lands on the FIRST attempt) mints
    a fresh onboarding_generation and initializes funnel/bootstrap state on
    the SAME write that persists the active row (INV-3)."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert row.status == "active"
    assert row.onboarding_generation is not None
    assert row.funnel_state == "not_started"
    assert row.bootstrap_state == "not_started"
    # Repo round-trip — not just the in-memory return value.
    fetched = await repo.get("user_owner")
    assert fetched.onboarding_generation == row.onboarding_generation
    assert fetched.funnel_state == "not_started"
    assert fetched.bootstrap_state == "not_started"


async def test_provision_org_reconcile_adopt_does_not_clobber_onboarding_fields(repo):
    """THE writer-safety proof (spec PR-4's hardest requirement): a
    reconcile-adopt retry (Friday seed failed on attempt 1, succeeds on
    attempt 2, same company_id) must NOT reset onboarding_generation or
    funnel_state/bootstrap_state -- even though the row's whole-item `put`
    is called again on the retry. If this test fails, co-location just
    reintroduced the exact clobber bug the dedicated-table alternative would
    have structurally avoided.
    """
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-2"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    # Friday fails on the first attempt (company created, seed not landed).
    admin.create_agent = AsyncMock(side_effect=PaperclipApiError("friday create failed", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert first.status == "failed"
    original_generation = first.onboarding_generation
    assert original_generation is not None  # minted on THIS (fresh-create) attempt

    # Simulate that some onboarding progress had already landed on this
    # generation before the retry (proves the FULL set of onboarding fields
    # survives, not just the generation id) — a real caller would get here
    # via claim_funnel_submission / record_onboarding_ids; we write directly
    # to isolate this test from that machinery.
    await repo.claim_funnel_submission("user_owner", onboarding_generation=original_generation)
    await repo.record_onboarding_ids(
        "user_owner", onboarding_generation=original_generation, goal_id="goal-1", project_id="proj-1"
    )
    pre_reconcile = await repo.get("user_owner")
    assert pre_reconcile.funnel_state == "submitted"
    assert pre_reconcile.onboarding_goal_id == "goal-1"

    # Second attempt: Friday now succeeds. This is the RECONCILE-ADOPT path
    # (reconciling_company=True) -- create_company must NOT be called again,
    # and the row's whole-item put must carry the onboarding fields forward.
    admin.create_agent = AsyncMock(side_effect=[{"id": "agent_friday"}, {"id": "agent_atlas"}])
    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert admin.create_company.await_count == 1  # no new company minted
    assert second.status == "active"
    assert second.onboarding_generation == original_generation  # NOT re-minted
    assert second.funnel_state == "submitted"  # NOT reset to not_started
    assert second.onboarding_goal_id == "goal-1"  # NOT wiped
    assert second.onboarding_project_id == "proj-1"

    # Repo round-trip — the actual persisted item, not just the return value.
    fetched = await repo.get("user_owner")
    assert fetched.onboarding_generation == original_generation
    assert fetched.funnel_state == "submitted"
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"


async def test_provision_org_company_recreated_mints_a_new_onboarding_generation(repo):
    """A genuinely NEW company for the same owner_user_id (e.g. after an
    atomic wipe deleted the old row) mints a FRESH onboarding_generation --
    the funnel resets, it does not inherit the deleted company's state."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_first"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    first_generation = first.onboarding_generation
    await repo.claim_funnel_submission("user_owner", onboarding_generation=first_generation)
    await repo.record_onboarding_ids(
        "user_owner", onboarding_generation=first_generation, goal_id="goal-1", project_id="proj-1"
    )

    # Atomic wipe: the row (and the Aurora company) is gone.
    await repo.delete("user_owner")

    # Fresh provision for the SAME owner_user_id -- a genuinely new company.
    admin.create_company = AsyncMock(return_value={"id": "co_second"})
    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert second.company_id == "co_second"
    assert second.onboarding_generation is not None
    assert second.onboarding_generation != first_generation
    assert second.funnel_state == "not_started"
    assert second.onboarding_goal_id is None
    assert second.onboarding_project_id is None


async def test_provision_member_does_not_mint_onboarding_generation(repo):
    """INV-3: a seat-join (provision_member) is NEVER a generation bump --
    the member's own row carries no onboarding fields at all."""
    admin = _make_admin_mock()
    owner_row = await _seed_owner(repo)
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "tok"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    member = await prov.provision_member(
        owner_id="org_acme", user_id="user_member", email="member@acme.test", owner_email="owner@acme.test"
    )

    assert member.onboarding_generation is None
    assert member.funnel_state is None
    # The owner row (fetched independently) is untouched by the member join.
    assert (await repo.get(owner_row.user_id)).onboarding_generation is None  # owner row was never minted either


# ----------------------------------------------------------------------
# provision_member
# ----------------------------------------------------------------------


async def _seed_owner(repo, *, org_id="org_acme", owner_user_id="user_owner"):
    """Seed the org-owner row directly so provision_member has someone to find."""
    from core.encryption import encrypt

    now = datetime.now(timezone.utc)
    row = PaperclipCompany(
        user_id=owner_user_id,
        org_id=org_id,
        company_id="co_acme",
        paperclip_user_id="pc_user_owner",
        paperclip_password_encrypted=encrypt("owner-password-plain"),
        service_token_encrypted=encrypt("svc-token"),
        status="active",
        created_at=now,
        updated_at=now,
    )
    await repo.put(row)
    return row


async def test_provision_member_happy_path(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session"})
    admin.create_invite = AsyncMock(return_value={"token": "invite-secret"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_abc"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_member(
        owner_id="org_acme",
        user_id="user_member",
        email="member@acme.test",
        owner_email="owner@acme.test",
    )

    # Sequence: signUp, signIn, createInvite, acceptInvite, approveJoinRequest
    admin.sign_up_user.assert_awaited_once()
    su_kwargs = admin.sign_up_user.call_args.kwargs
    assert su_kwargs["email"] == "member@acme.test"

    admin.sign_in_user.assert_awaited_once()
    si_kwargs = admin.sign_in_user.call_args.kwargs
    assert si_kwargs["email"] == "owner@acme.test"
    assert si_kwargs["password"] == "owner-password-plain"

    admin.create_invite.assert_awaited_once()
    ci_kwargs = admin.create_invite.call_args.kwargs
    assert ci_kwargs["session_cookie"] == "owner-session"
    assert ci_kwargs["company_id"] == "co_acme"
    assert ci_kwargs["email"] == "member@acme.test"
    # Same humanRole="owner" rationale as provision_org — without it
    # the new member would land as Paperclip's default "operator"
    # role and 403 on agents:create.
    assert ci_kwargs["human_role"] == "owner"

    admin.accept_invite.assert_awaited_once()
    ai_kwargs = admin.accept_invite.call_args.kwargs
    assert ai_kwargs["session_cookie"] == "member-session"
    assert ai_kwargs["invite_token"] == "invite-secret"

    admin.approve_join_request.assert_awaited_once()
    aj_kwargs = admin.approve_join_request.call_args.kwargs
    assert aj_kwargs["session_cookie"] == "owner-session"
    assert aj_kwargs["company_id"] == "co_acme"
    assert aj_kwargs["request_id"] == "req_abc"

    # Persisted member row reuses the org's company_id
    assert row.user_id == "user_member"
    assert row.company_id == "co_acme"
    assert row.org_id == "org_acme"
    assert row.paperclip_user_id == "pc_user_member"
    assert row.status == "active"
    fetched = await repo.get("user_member")
    assert fetched is not None
    assert fetched.company_id == "co_acme"


async def test_provision_member_raises_when_org_not_provisioned(repo):
    """No owner row + no company means we MUST refuse (caller should retry)."""
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(OrgNotProvisionedError):
        await prov.provision_member(
            owner_id="org_phantom",
            user_id="user_member",
            email="member@phantom.test",
            owner_email="owner@phantom.test",
        )
    # No API calls at all
    admin.sign_up_user.assert_not_awaited()


async def test_provision_member_idempotent_on_second_call(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_abc"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_member(
        owner_id="org_acme",
        user_id="user_member",
        email="member@acme.test",
        owner_email="owner@acme.test",
    )
    second = await prov.provision_member(
        owner_id="org_acme",
        user_id="user_member",
        email="member@acme.test",
        owner_email="owner@acme.test",
    )
    assert first.company_id == second.company_id
    # Only one full chain — second call short-circuits
    assert admin.sign_up_user.await_count == 1
    assert admin.create_invite.await_count == 1


async def test_provision_member_signup_failure_writes_durable_failed_marker(repo):
    """Member sign-up failing net-new (no provisioning row written yet) must
    leave ONE durable ``failed`` marker for the member — carrying the known
    company_id but NO fabricated paperclip_user_id (an empty id was the old
    poison that wedged every retry). This surfaces the honest failure (/teams →
    503, teams_status="failed") instead of an eternal spinner. provision_member
    still RAISES; the marker is put-if-absent (never clobbers a real row)."""
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup email", status_code=422, body="unprocessable"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_member(
            owner_id="org_acme",
            user_id="user_member",
            email="member@acme.test",
            owner_email="owner@acme.test",
        )
    # Durable failed marker for the member — company_id known, but no fabricated id.
    marker = await repo.get("user_member")
    assert marker is not None and marker.status == "failed"
    assert marker.company_id == "co_acme"
    raw = repo._table().get_item(Key={"user_id": "user_member"})["Item"]
    assert "paperclip_user_id" not in raw


async def test_provision_member_invite_failure_marks_failed(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session"})
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("perm denied", status_code=403, body="nope"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_member(
            owner_id="org_acme",
            user_id="user_member",
            email="member@acme.test",
            owner_email="owner@acme.test",
        )
    failed = await repo.get("user_member")
    assert failed is not None
    assert failed.status == "failed"


async def test_provision_member_raises_when_accept_invite_missing_id(repo):
    """If Paperclip's accept_invite ever changes shape, fail loudly.

    The original implementation tolerated four different keys
    (``request_id``/``requestId``/``joinRequest.id``/``id``) but the
    actual Paperclip route (``server/src/routes/access.ts:3604``)
    always returns the join-request row spread flat with field
    ``id``. Defensive multi-key fallback was dead code that would
    mask a future schema change. We now strictly require ``id`` and
    raise on anything else so a regression is observable.
    """
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "ms"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "os"})
    admin.accept_invite = AsyncMock(return_value={"unexpected": "shape"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError, match="missing 'id'"):
        await prov.provision_member(
            owner_id="org_acme",
            user_id="user_member",
            email="member@acme.test",
            owner_email="owner@acme.test",
        )
    # Approve was never called — we bailed before then.
    admin.approve_join_request.assert_not_awaited()


# ----------------------------------------------------------------------
# find_org_owner
# ----------------------------------------------------------------------


async def test_find_org_owner_skips_disabled_rows(repo):
    """``find_org_owner`` must NOT return a disabled row.

    ``disable()`` flips ``status="disabled"`` and leaves the row in DDB
    until the purge worker runs (up to 30 days). If the OLDEST row in
    an org is disabled, a naive "min(created_at)" pick would hand back
    a stale Better Auth account and break ``provision_member`` even
    when other active admins exist.

    Seed: row A (disabled, older created_at) + row B (active, newer).
    Expect: ``find_org_owner`` returns row B.
    """
    from datetime import timedelta

    from core.encryption import encrypt

    older = datetime.now(timezone.utc) - timedelta(days=10)
    newer = datetime.now(timezone.utc)

    # Row A: oldest by created_at, but disabled.
    await repo.put(
        PaperclipCompany(
            user_id="user_disabled_admin",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_disabled",
            paperclip_password_encrypted=encrypt("disabled-password"),
            service_token_encrypted=encrypt("disabled-token"),
            status="disabled",
            created_at=older,
            updated_at=older,
        )
    )
    # Row B: newer, active — the only valid owner candidate.
    await repo.put(
        PaperclipCompany(
            user_id="user_active_admin",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_active",
            paperclip_password_encrypted=encrypt("active-password"),
            service_token_encrypted=encrypt("active-token"),
            status="active",
            created_at=newer,
            updated_at=newer,
        )
    )

    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    found = await prov.find_org_owner("org_acme")

    assert found is not None
    assert found.user_id == "user_active_admin"
    assert found.status == "active"


async def test_find_org_owner_returns_none_when_no_active_rows(repo):
    """If every row in the org is non-active (disabled/failed/provisioning),
    ``find_org_owner`` returns None so the caller raises
    ``OrgNotProvisionedError`` and the retry path takes over.
    """
    from core.encryption import encrypt

    now = datetime.now(timezone.utc)
    await repo.put(
        PaperclipCompany(
            user_id="user_disabled",
            org_id="org_dead",
            company_id="co_dead",
            paperclip_user_id="pc_dead",
            paperclip_password_encrypted=encrypt("p"),
            service_token_encrypted=encrypt("t"),
            status="disabled",
            created_at=now,
            updated_at=now,
        )
    )

    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    found = await prov.find_org_owner("org_dead")

    assert found is None


# ----------------------------------------------------------------------
# disable + purge
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# archive_member
# ----------------------------------------------------------------------


async def test_archive_member_archives_paperclip_then_disables_ddb(repo):
    """``archive_member`` looks up the member's Paperclip ``member.id`` via
    ``list_members``, POSTs to the archive endpoint as the bootstrap
    admin, and finally marks the DDB row ``disabled`` with a purge timer.
    """
    await _seed_owner(repo, owner_user_id="user_member")
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(
        return_value={
            "members": [
                # Some other member — should be skipped over.
                {"id": "mem_other", "principalId": "pc_user_other"},
                # The target row.
                {"id": "mem_target", "principalId": "pc_user_owner"},
            ]
        }
    )
    admin.archive_member = AsyncMock(
        return_value={"member": {"id": "mem_target", "status": "archived"}, "reassignedIssueCount": 0}
    )
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", grace_days=14)

    admin.list_members.assert_awaited_once()
    list_kwargs = admin.list_members.call_args.kwargs
    assert list_kwargs["company_id"] == "co_acme"
    assert list_kwargs["session_cookie"] == "admin-cookie-test"

    admin.archive_member.assert_awaited_once()
    archive_kwargs = admin.archive_member.call_args.kwargs
    assert archive_kwargs["company_id"] == "co_acme"
    assert archive_kwargs["member_id"] == "mem_target"
    assert archive_kwargs["session_cookie"] == "admin-cookie-test"

    fetched = await repo.get("user_member")
    assert fetched is not None
    assert fetched.status == "disabled"
    assert fetched.scheduled_purge_at is not None


async def test_archive_member_idempotent_for_missing_row(repo):
    """No DDB row → no-op (does not call Paperclip)."""
    admin = _make_admin_mock()
    admin.list_members = AsyncMock()
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_does_not_exist")

    admin.list_members.assert_not_awaited()
    admin.archive_member.assert_not_awaited()


async def test_archive_member_handles_flat_list_response(repo):
    """``list_members`` may return a flat list (admin BFF unwraps server
    envelope). archive_member must accept either shape."""
    await _seed_owner(repo, owner_user_id="user_member")
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value=[{"id": "mem_target", "principalId": "pc_user_owner"}])
    admin.archive_member = AsyncMock(return_value={"member": {"id": "mem_target"}})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member")

    admin.archive_member.assert_awaited_once()
    fetched = await repo.get("user_member")
    assert fetched.status == "disabled"


async def test_archive_member_marks_disabled_with_error_when_member_missing(repo):
    """If the Paperclip side has no matching member (already archived,
    drift, etc.) we still flip the DDB row to disabled — but record
    ``last_error`` so admin tooling can investigate. We do NOT call
    archive_member in that branch.
    """
    await _seed_owner(repo, owner_user_id="user_member")
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value={"members": []})
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member")

    admin.archive_member.assert_not_awaited()
    fetched = await repo.get("user_member")
    assert fetched.status == "disabled"
    assert fetched.scheduled_purge_at is not None
    assert fetched.last_error is not None
    assert "member row not found" in fetched.last_error


async def test_archive_member_reraises_paperclip_5xx_without_touching_ddb(repo):
    """If the archive call itself 5xx's, we re-raise so the webhook
    can enqueue retry. DDB row stays untouched (active->active) so the
    retry pass can finish the chain without coming up against an
    already-disabled row.
    """
    from core.services.paperclip_admin_client import PaperclipApiError

    await _seed_owner(repo, owner_user_id="user_member")
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value={"members": [{"id": "mem_target", "principalId": "pc_user_owner"}]})
    admin.archive_member = AsyncMock(side_effect=PaperclipApiError("server-down", 503, ""))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.archive_member(user_id="user_member")

    fetched = await repo.get("user_member")
    # Still active — caller is expected to retry.
    assert fetched.status == "active"


async def test_disable_marks_status_disabled_with_grace_window(repo):
    await _seed_owner(repo, owner_user_id="user_a")
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    before = datetime.now(timezone.utc)
    await prov.disable(user_id="user_a", grace_days=7)
    after = datetime.now(timezone.utc)

    fetched = await repo.get("user_a")
    assert fetched.status == "disabled"
    assert fetched.scheduled_purge_at is not None
    # Within ~7 days (allow generous bounds for test clock drift)
    delta = (fetched.scheduled_purge_at - before).total_seconds()
    delta_max = (fetched.scheduled_purge_at - after).total_seconds()
    assert 6 * 86400 < delta
    assert delta_max < 8 * 86400


async def test_disable_idempotent_for_missing_row(repo):
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    # Should not raise
    await prov.disable(user_id="user_does_not_exist")


async def test_purge_deletes_row_only_does_not_call_paperclip(repo):
    """v1: purge removes the local mapping but does NOT touch the
    Paperclip company. Cleanup of orphaned Paperclip companies is
    deferred to an out-of-band admin sweep (see purge() docstring)."""
    await _seed_owner(repo, owner_user_id="user_lonely")
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.purge(user_id="user_lonely")

    assert await repo.get("user_lonely") is None


async def test_purge_does_not_archive_when_other_members_remain(repo):
    """If purging a non-owner member, company stays alive."""
    from core.encryption import encrypt

    now = datetime.now(timezone.utc)
    # Owner row (created earliest)
    await repo.put(
        PaperclipCompany(
            user_id="user_owner",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_owner",
            paperclip_password_encrypted=encrypt("p"),
            service_token_encrypted=encrypt("t"),
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    # Member row (created later) — this is the one being purged
    await repo.put(
        PaperclipCompany(
            user_id="user_member",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_member",
            paperclip_password_encrypted=encrypt("p"),
            service_token_encrypted=encrypt("t"),
            status="disabled",
            created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
    )
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.purge(user_id="user_member")

    # Member row gone, owner row still there
    assert await repo.get("user_member") is None
    assert await repo.get("user_owner") is not None
    # (Company is never archived by provisioning — orphan cleanup is an
    # out-of-band ops concern.)


async def test_purge_owner_purge_with_other_members_does_not_archive(repo):
    """Regression: purging the org owner with another member still
    present must NOT archive the company.

    The original implementation used "owner row matches purge target"
    as the last-member signal, which falsely triggered archive any
    time the owner was the one being purged — even with active
    co-tenants. The new implementation counts via the by-org-id GSI
    AFTER deletion, so the post-delete count of 1 here correctly
    keeps the company alive.
    """
    from core.encryption import encrypt

    now = datetime.now(timezone.utc)
    # Owner row (created earliest) — this is the one being purged.
    await repo.put(
        PaperclipCompany(
            user_id="user_owner",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_owner",
            paperclip_password_encrypted=encrypt("p"),
            service_token_encrypted=encrypt("t"),
            status="disabled",
            created_at=now,
            updated_at=now,
        )
    )
    # Co-tenant member — should keep the company alive.
    await repo.put(
        PaperclipCompany(
            user_id="user_member",
            org_id="org_acme",
            company_id="co_acme",
            paperclip_user_id="pc_user_member",
            paperclip_password_encrypted=encrypt("p"),
            service_token_encrypted=encrypt("t"),
            status="active",
            created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
    )
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.purge(user_id="user_owner")

    assert await repo.get("user_owner") is None
    assert await repo.get("user_member") is not None


async def test_purge_idempotent_for_missing_row(repo):
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await prov.purge(user_id="user_does_not_exist")


# ----------------------------------------------------------------------
# retryable error classification (consumed by T12 webhook handler)
# ----------------------------------------------------------------------


def test_org_not_provisioned_error_is_retryable_by_default():
    """OrgNotProvisionedError carries class-level ``retryable=True`` so
    T12 can dispatch a retry via ``getattr(exc, "retryable", False)``
    without inspecting the exception type directly.
    """
    exc = OrgNotProvisionedError("org missing")
    assert getattr(exc, "retryable", False) is True


async def test_paperclip_api_error_5xx_annotated_retryable_on_provision_org(repo):
    """5xx from the admin client classifies retryable=True after
    bubbling through provision_org's except block."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("server", status_code=503, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_org(
            owner_id="org_a",
            owner_user_id="user_a",
            owner_email="a@a.test",
        )
    assert getattr(ei.value, "retryable", None) is True


async def test_paperclip_api_error_4xx_annotated_not_retryable_on_provision_org(repo):
    """4xx (excluding 429) classifies retryable=False — a 409 dup-email
    is a permanent state, not a transient one."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup", status_code=409, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_org(
            owner_id="org_a",
            owner_user_id="user_a",
            owner_email="a@a.test",
        )
    assert getattr(ei.value, "retryable", None) is False


async def test_paperclip_api_error_429_annotated_retryable(repo):
    """429 rate-limit is retryable — caller should back off and retry."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("rate-limited", status_code=429, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_org(
            owner_id="org_a",
            owner_user_id="user_a",
            owner_email="a@a.test",
        )
    assert getattr(ei.value, "retryable", None) is True


async def test_paperclip_api_error_annotated_on_provision_member(repo):
    """provision_member exception path annotates retryable too."""
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("server", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_member(
            owner_id="org_acme",
            user_id="user_member",
            email="m@a.test",
            owner_email="o@a.test",
        )
    assert getattr(ei.value, "retryable", None) is True
