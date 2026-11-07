"""Tests for the ``PaperclipProvisioning`` orchestrator.

Strategy:

* The DynamoDB repo is real (moto-backed, with all three GSIs of the prod
  table) so the claim fences, conditional writes and the by-org-id guard are
  exercised end-to-end against the workspace-row + identity-row shape: ONE
  workspace row per Clerk org (company facts + ``member_user_ids``) and ONE
  identity row per human (their Better Auth account only).
* The Paperclip admin client is a plain ``MagicMock`` with ``AsyncMock``
  methods so we can assert call ordering and shape per scenario.

``asyncio_mode = "auto"`` (pyproject.toml) is in effect — async tests
need no decorator.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest

from core.config import settings as _settings
from core.encryption import decrypt, encrypt
from moto import mock_aws

from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipRepo, PaperclipWorkspace
from core.services.paperclip_provisioning import (
    STARTER_CAPABILITIES,
    STARTER_ROSTER,
    OrgNotProvisionedError,
    PaperclipProvisioning,
)
from core.services.paperclip_admin_client import PaperclipApiError

TABLE_NAME = "test-paperclip-companies-prov"

STARTER_KEYS = {"ceo", "engineering", "finance", "growth", "analytics", "customer_operations", "office"}


@pytest.fixture(autouse=True)
def _seed_container_row(monkeypatch):
    """provision_org's starter-seed loop reads the owner's container row for its
    ``hook_token`` -- that token is the native adapter's ``apiKey``, and
    ``build_hermes_gateway_adapter_config`` refuses an empty one (M4-R33: an adapter minted
    without it 401s at the facade on every run). The row lives in a DIFFERENT table than this
    file's moto-backed paperclip-companies fixture, so it is stubbed rather than modelled."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-seed-test"}))


@pytest.fixture(autouse=True)
def _stub_admin_session(monkeypatch):
    """Stub get_admin_session_cookie (Secrets Manager + Better Auth sign-in in
    prod) to a deterministic ``"admin-cookie-test"``."""

    async def _fake_admin_cookie(_http_client):
        return "admin-cookie-test"

    monkeypatch.setattr("core.services.paperclip_admin_session.get_admin_session_cookie", _fake_admin_cookie)


@pytest.fixture
def repo():
    """A moto-backed PaperclipRepo mirroring the prod table's three GSIs."""
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
                {"AttributeName": "company_id", "AttributeType": "S"},
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
                {
                    # Load-bearing: DynamoDB rejects any PutItem whose company_id
                    # is an empty STRING once this index exists, so the claim /
                    # failed-marker writes must OMIT the attribute.
                    "IndexName": "by-company-id",
                    "KeySchema": [{"AttributeName": "company_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["status"]},
                },
            ],
        )
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield PaperclipRepo(table_name=TABLE_NAME, region="us-east-1")


def _make_admin_mock() -> MagicMock:
    """An AsyncMock-backed admin client with sensible default returns."""
    admin = MagicMock()
    admin._http = MagicMock()  # passed into the (stubbed) get_admin_session_cookie
    admin.sign_up_user = AsyncMock(
        return_value={"user": {"id": "pc_user_default"}, "_session_cookie": "session-default"}
    )
    admin.sign_in_user = AsyncMock(
        return_value={"user": {"id": "pc_user_default"}, "_session_cookie": "session-default"}
    )
    admin.create_company = AsyncMock(return_value={"id": "co_default"})
    admin.create_agent = AsyncMock(return_value={"id": "agent_default"})
    # Builtin-agent purge: default to an empty roster so nothing is deleted.
    admin.list_agents = AsyncMock(return_value=[])
    admin.delete_agent = AsyncMock(return_value={})
    admin.create_invite = AsyncMock(return_value={"token": "invite-token-default"})
    # accept_invite returns the join request flat (``id`` top-level).
    admin.accept_invite = AsyncMock(return_value={"id": "req_default"})
    admin.approve_join_request = AsyncMock(return_value={})
    return admin


async def _seed_workspace(
    repo,
    *,
    org_id="org_acme",
    company_id="co_acme",
    status="active",
    members=("user_owner",),
    provisioner="user_owner",
    **kwargs,
) -> PaperclipWorkspace:
    now = datetime.now(timezone.utc)
    ws = PaperclipWorkspace(
        org_id=org_id,
        company_id=company_id,
        status=status,
        created_at=now,
        updated_at=now,
        provisioner_user_id=provisioner,
        member_user_ids=set(members),
        **kwargs,
    )
    await repo.put_workspace(ws)
    return ws


async def _seed_identity(
    repo,
    user_id="user_owner",
    *,
    paperclip_user_id="pc_user_owner",
    password="owner-password-plain",
    email="owner@acme.test",
) -> None:
    await repo.put_identity(
        PaperclipIdentity(
            user_id=user_id,
            paperclip_user_id=paperclip_user_id,
            paperclip_password_encrypted=encrypt(password),
            paperclip_email=email,
        )
    )


async def _seed_owner(repo, *, org_id="org_acme", **kwargs) -> PaperclipWorkspace:
    """An active workspace provisioned by user_owner, plus user_owner's identity."""
    ws = await _seed_workspace(repo, org_id=org_id, **kwargs)
    await _seed_identity(repo)
    return ws


# ----------------------------------------------------------------------
# provision_org
# ----------------------------------------------------------------------


async def test_provision_org_happy_path(repo, monkeypatch):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})

    # ``org_acme`` is an org, so naming consults Clerk first — pin that to None
    # so this asserts the deterministic email-derived fallback.
    from core.services import company_identity

    async def _no_clerk_org(org_id):
        return None

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", _no_clerk_org)

    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    admin.sign_up_user.assert_awaited_once()
    sign_up_kwargs = admin.sign_up_user.call_args.kwargs
    assert sign_up_kwargs["email"] == "owner@acme.test"
    assert sign_up_kwargs["name"] == "owner@acme.test"
    assert len(sign_up_kwargs["password"]) >= 32  # token_urlsafe(32) -> ~43 chars

    # create_company runs as the BOOTSTRAP ADMIN (POST /api/companies requires
    # instance_admin).
    admin.create_company.assert_awaited_once()
    cc_kwargs = admin.create_company.call_args.kwargs
    assert cc_kwargs["name"] == "Owner"
    assert cc_kwargs["session_cookie"] == "admin-cookie-test"
    # One key per ORG: the fork scopes it to the creating principal (always
    # the bootstrap admin), so every caller for this org replays one company.
    assert cc_kwargs["idempotency_key"] == "lightbulb-company-create:org_acme"

    # The provisioner joins as a co-owner via invite/accept/approve.
    admin.create_invite.assert_awaited_once()
    invite_kwargs = admin.create_invite.call_args.kwargs
    assert invite_kwargs["company_id"] == "co_acme"
    assert invite_kwargs["session_cookie"] == "admin-cookie-test"
    # humanRole="owner" or Paperclip defaults to "operator" (no agents:create).
    assert invite_kwargs["human_role"] == "owner"
    admin.accept_invite.assert_awaited_once()
    assert admin.accept_invite.call_args.kwargs["session_cookie"] == "owner-session-1"
    admin.approve_join_request.assert_awaited_once()
    assert admin.approve_join_request.call_args.kwargs["session_cookie"] == "admin-cookie-test"

    # The complete generic starter roster.
    assert admin.create_agent.await_count == 7
    by_name = {call.kwargs["name"]: call.kwargs for call in admin.create_agent.await_args_list}
    assert set(by_name) == {"CEO", "Engineering", "Finance", "Growth", "Analytics", "Front desk", "Office"}
    for key, name, *_ in STARTER_ROSTER:
        assert by_name[name]["capabilities"] == STARTER_CAPABILITIES[key]

    agent_kwargs = by_name["CEO"]
    assert agent_kwargs["company_id"] == "co_acme"
    # ONE adapter: Paperclip's native ``hermes_gateway`` at the hook-token facade.
    assert agent_kwargs["adapter_type"] == "hermes_gateway"
    adapter_cfg = agent_kwargs["adapter_config"]
    assert adapter_cfg["apiBaseUrl"] == (
        f"{_settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/internal/paperclip/hermes/org_acme"
    )
    assert adapter_cfg["apiKey"] == "hook-seed-test"
    assert adapter_cfg["sessionKeyStrategy"] == "issue"
    # Agent is owned BY the user (their session) so they can edit it later.
    assert agent_kwargs["session_cookie"] == "owner-session-1"

    # The workspace row: company facts + the provisioner as its first member.
    assert row.org_id == "org_acme"
    assert row.company_id == "co_acme"
    assert row.status == "active"
    assert row.provisioner_user_id == "user_owner"
    assert row.member_user_ids == {"user_owner"}
    assert row.seeded_agent_ids is None
    assert set(row.starter_agent_ids or {}) == STARTER_KEYS

    fetched = await repo.get_workspace("org_acme")
    assert fetched is not None
    assert fetched.company_id == "co_acme"
    assert fetched.status == "active"
    assert fetched.member_user_ids == {"user_owner"}

    # The identity row: the human's Better Auth account, settled (no status),
    # naming no company and no org.
    identity = await repo.get_identity("user_owner")
    assert identity.paperclip_user_id == "pc_user_owner"
    assert identity.paperclip_email == "owner@acme.test"
    assert decrypt(identity.paperclip_password_encrypted) == sign_up_kwargs["password"]
    assert identity.status is None
    raw_identity = repo._table().get_item(Key={"user_id": "user_owner"})["Item"]
    assert "company_id" not in raw_identity
    assert "org_id" not in raw_identity


async def test_provision_org_idempotent_on_second_call(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert admin.sign_up_user.await_count == 1
    assert admin.create_company.await_count == 1
    assert first.company_id == second.company_id == "co_acme"


async def test_concurrent_provision_org_for_one_org_converges_on_one_company(repo, monkeypatch):
    """Two admins of one org provision it at the same time, with no backend
    claim or lock. Both reach company create before either returns; the fork
    replays the per-org Idempotency-Key, so both land on ONE company and both
    succeed."""
    import asyncio

    from core.services import company_identity

    async def _no_clerk_org(org_id):
        return None

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", _no_clerk_org)

    admin = _make_admin_mock()

    async def _sign_up(*, email, password, name):
        return {"user": {"id": f"pc_{email}"}, "_session_cookie": f"session-{email}"}

    admin.sign_up_user = AsyncMock(side_effect=_sign_up)

    # Fake Paperclip: company create keyed by Idempotency-Key (scope = the one
    # bootstrap admin); a barrier holds both calls until both have arrived.
    companies: dict[str, dict] = {}
    arrived = asyncio.Event()
    calls: list[str] = []

    async def _create_company(*, name, description, session_cookie, idempotency_key):
        calls.append(idempotency_key)
        if len(calls) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=5)
        return companies.setdefault(idempotency_key, {"id": f"co_{len(companies) + 1}"})

    admin.create_company = AsyncMock(side_effect=_create_company)

    agents: dict[tuple[str, str], dict] = {}

    async def _create_agent(*, company_id, idempotency_key, **_kwargs):
        return agents.setdefault((company_id, idempotency_key), {"id": f"agent_{len(agents) + 1}"})

    admin.create_agent = AsyncMock(side_effect=_create_agent)
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    a, b = await asyncio.gather(
        prov.provision_org(owner_id="org_acme", owner_user_id="user_a", owner_email="a@acme.test"),
        prov.provision_org(owner_id="org_acme", owner_user_id="user_b", owner_email="b@acme.test"),
    )

    assert calls == ["lightbulb-company-create:org_acme"] * 2
    assert len(companies) == 1
    assert a.company_id == b.company_id == "co_1"
    assert a.status == b.status == "active"
    assert a.starter_agent_ids == b.starter_agent_ids
    stored = await repo.get_workspace("org_acme", consistent_read=True)
    assert stored.company_id == "co_1" and stored.status == "active"


async def test_reprovision_with_company_preserved_funnel_stays_complete_no_reprompt(repo):
    """L1, end to end: re-running provision_org on an ACTIVE workspace with a
    complete starter roster hits the idempotency short-circuit (ZERO writes),
    so a funnel that already reached "complete" survives and the owner is
    never re-shown the first-run funnel."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    provisioned = await prov.provision_org(
        owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test"
    )
    generation = provisioned.onboarding_generation

    await repo.claim_funnel_submission("org_acme", onboarding_generation=generation, submitted_by="user_owner")
    await repo.record_onboarding_ids(
        "org_acme", onboarding_generation=generation, goal_id="goal-1", project_id="proj-1"
    )
    repo._ws_table().update_item(  # workspace META lives on the tasks table (funnelState there)
        Key=repo._ws_key("org_acme"),
        UpdateExpression="SET funnelState = :c",
        ExpressionAttributeValues={":c": "complete"},
    )
    assert (await repo.get_workspace("org_acme")).funnel_state == "complete"

    reprovisioned = await prov.provision_org(
        owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test"
    )

    assert admin.sign_up_user.await_count == 1
    assert admin.create_company.await_count == 1
    assert reprovisioned.onboarding_generation == generation
    assert reprovisioned.funnel_state == "complete"
    assert reprovisioned.onboarding_goal_id == "goal-1"

    fetched = await repo.get_workspace("org_acme")
    assert fetched.funnel_state == "complete"
    assert fetched.onboarding_generation == generation


async def test_provision_org_seed_agent_failure_is_not_fatal(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    admin.create_agent = AsyncMock(
        side_effect=[
            {"id": "agent_ceo"},
            PaperclipApiError("engineering create failed", status_code=500, body="boom"),
            {"id": "agent_finance"},
            {"id": "agent_growth"},
            {"id": "agent_analytics"},
            {"id": "agent_customer_operations"},
            {"id": "agent_office"},
        ]
    )
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert row.status == "failed"
    assert row.company_id == "co_acme"
    assert "engineering" not in (row.starter_agent_ids or {})
    assert "engineering" in (row.last_error or "")
    assert (await repo.get_workspace("org_acme")).status == "failed"


async def test_provision_org_signup_failure_writes_durable_failed_markers(repo):
    """Sign-up itself fails, so no workspace row exists yet. ``_mark_failed``
    leaves BOTH markers durable: the provisioner's sign-up claim goes
    ``failed`` (credential-less, so the next attempt reclaims it and re-runs
    sign-up), and a put-if-absent ``failed`` WORKSPACE marker makes ``/teams``
    answer an honest 503 instead of an eternal spinner. Neither carries a
    fabricated id; provision_org still RAISES."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("auth disabled", status_code=403, body="forbidden"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    marker = await repo.get_workspace("org_acme")
    assert marker is not None, "a durable failed workspace marker must exist (honest 503, not eternal spinner)"
    assert marker.status == "failed"
    assert marker.company_id == ""
    assert "provision_org failed" in (marker.last_error or "")
    raw_ws = repo._ws_table().get_item(Key=repo._ws_key("org_acme"))["Item"]
    assert "company_id" not in raw_ws and "companyId" not in raw_ws  # absent, not "" (the by-company-id GSI rejects "")

    identity = await repo.get_identity("user_owner")
    assert identity.status == "failed"
    raw_identity = repo._table().get_item(Key={"user_id": "user_owner"})["Item"]
    assert "paperclip_user_id" not in raw_identity


async def test_provision_org_create_company_failure_marks_failed(repo):
    """Sign-up succeeded (identity settled BEFORE the rest of the chain), then
    create_company failed: the workspace is marked failed, the confirmed
    identity is never downgraded."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("server err", status_code=500, body="fail"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    failed = await repo.get_workspace("org_acme")
    assert failed is not None and failed.status == "failed"
    identity = await repo.get_identity("user_owner")
    assert identity.paperclip_user_id == "pc_user_owner"
    assert identity.status is None


async def test_provision_org_refuses_second_company_for_org_with_legacy_rows(repo):
    """An org whose company is still recorded on a pre-migration per-member row
    (indexed under ``org_id``) but has no workspace row yet must NOT get a
    second company: 409 (the caller's "in flight — no-op") BEFORE any Paperclip
    side effect, and no failed marker on the workspace awaiting migration."""
    now = datetime.now(timezone.utc).isoformat()
    repo._table().put_item(
        Item={
            "user_id": "user_legacy",
            "org_id": "org_acme",
            "company_id": "co_legacy",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
    )
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError) as excinfo:
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert excinfo.value.status_code == 409
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    assert await repo.get_workspace("org_acme") is None
    assert await repo.get_identity("user_owner") is None  # no claim taken either


# ----------------------------------------------------------------------
# provision_org — cross-instance sign-up fence (try_acquire_seed_claim)
# ----------------------------------------------------------------------


async def test_provision_org_fresh_create_claims_fence_before_any_side_effect(repo, monkeypatch):
    """The fence is only a fence if it lands BEFORE the first Paperclip side
    effect: at claim time neither sign-up nor the company create may have run."""
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    observed = {}
    real_claim = repo.try_acquire_seed_claim

    async def spy_claim(user_id, **kwargs):
        observed["user_id"] = user_id
        observed["signups_at_claim_time"] = admin.sign_up_user.await_count
        observed["companies_at_claim_time"] = admin.create_company.await_count
        return await real_claim(user_id, **kwargs)

    monkeypatch.setattr(repo, "try_acquire_seed_claim", spy_claim)

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    # The claim lives on the provisioner's IDENTITY row, not the workspace row.
    assert observed == {"user_id": "user_owner", "signups_at_claim_time": 0, "companies_at_claim_time": 0}
    assert row.status == "active"


async def test_provision_org_loser_adopts_winners_active_workspace(repo, monkeypatch):
    """Two backend instances race a fresh create (the 2026-07-24 dev incident,
    102ms apart). The loser runs ZERO Paperclip side effects and adopts the
    winner's active workspace once its final put lands."""
    import core.services.paperclip_provisioning as prov_mod

    monkeypatch.setattr(prov_mod, "_SEED_WAIT_INTERVAL_S", 0)

    # The "winner" (another instance) already holds the sign-up claim...
    assert await repo.try_acquire_seed_claim("user_owner", claim_id="peer") is True

    # ...and finishes while the loser is polling: the loser's FIRST workspace
    # read sees nothing, its first poll sees the winner's active row.
    real_get = repo.get_workspace
    calls = {"n": 0}

    async def flipping_get(org_id, *, consistent_read=False):
        calls["n"] += 1
        if calls["n"] == 2:
            await _seed_workspace(repo, company_id="co_peer", members=("user_owner",))
        return await real_get(org_id, consistent_read=consistent_read)

    monkeypatch.setattr(repo, "get_workspace", flipping_get)

    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert row.status == "active"
    assert row.company_id == "co_peer"  # the WINNER's company, adopted
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()


async def test_provision_org_loser_times_out_with_in_flight_409(repo, monkeypatch):
    """A loser whose wait window closes before the winner finishes raises the
    409-shaped "in flight — no-op" error; the next poll adopts."""
    import core.services.paperclip_provisioning as prov_mod

    monkeypatch.setattr(prov_mod, "_SEED_WAIT_INTERVAL_S", 0)
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_ATTEMPTS", 3)

    assert await repo.try_acquire_seed_claim("user_owner", claim_id="peer") is True

    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as excinfo:
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert excinfo.value.status_code == 409
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    # The loser never touched the winner's claim, nor wrote a workspace marker.
    assert (await repo.get_identity("user_owner")).status == "seeding"
    assert await repo.get_workspace("org_acme") is None


async def test_provision_org_steals_stale_seed_claim_and_seeds(repo):
    """A claim whose holder crashed hard (SIGKILL) before any durable progress
    leaves a bare seeding identity row. Past the stale window a later poll
    re-claims it and runs the full chain — the owner is never stranded."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    repo._table().put_item(
        Item={
            "user_id": "user_owner",
            "status": "seeding",
            "seeding_claim_id": "dead-holder",
            "seeding_claimed_at": stale,
            "created_at": stale,
            "updated_at": stale,
        }
    )

    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    admin.sign_up_user.assert_awaited_once()
    admin.create_company.assert_awaited_once()
    assert row.status == "active"
    assert (await repo.get_workspace("org_acme")).status == "active"
    assert (await repo.get_identity("user_owner")).status is None  # settled


async def test_provision_org_winner_failure_releases_claim_for_prompt_retry(repo):
    """A winner that fails pre-sign-up must not leave the claim parked at
    "seeding" (every retry would wait out the 5-minute stale window):
    _mark_failed downgrades it to "failed", and the NEXT poll re-runs the
    sign-up immediately and lands."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("paperclip 503", status_code=503, body="down"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert (await repo.get_identity("user_owner")).status == "failed"  # NOT "seeding"
    assert (await repo.get_workspace("org_acme")).status == "failed"

    admin_retry = _make_admin_mock()
    prov_retry = PaperclipProvisioning(admin_client=admin_retry, repo=repo, env_name="dev")
    row = await prov_retry.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert row.status == "active"
    admin_retry.sign_up_user.assert_awaited_once()
    # The staged password from the failed attempt was reused, never re-minted.
    assert admin_retry.sign_up_user.call_args.kwargs["password"] == admin.sign_up_user.call_args.kwargs["password"]
    assert (await repo.get_workspace("org_acme")).status == "active"


async def test_provision_org_retries_after_failure(repo):
    """A failed workspace should not block a subsequent successful retry, and
    the retry ADOPTS the confirmed identity via sign-in (Better Auth sign-up is
    not idempotent)."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("transient", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert (await repo.get_workspace("org_acme")).status == "failed"

    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert row.status == "active"
    assert row.company_id == "co_acme"
    assert admin.sign_up_user.await_count == 1
    admin.sign_in_user.assert_awaited()


async def test_provision_org_reconciles_failed_company_without_reminting(repo):
    """A persistent starter-seed outage must retry the seed against the SAME
    company — never mint a new orphan company on each poll (Paperclip ignores
    our Idempotency-Key). The reconcile adopts the workspace row's company_id
    and resumes at the seed."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-2"})
    admin.create_company = AsyncMock(side_effect=[{"id": "co_first"}, {"id": "co_second"}])
    admin.create_agent = AsyncMock(side_effect=PaperclipApiError("seed create failed", status_code=500, body="boom"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert first.status == "failed"
    assert first.company_id == "co_first"
    assert first.member_user_ids == {"user_owner"}
    assert admin.create_company.await_count == 1

    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert admin.create_company.await_count == 1
    assert admin.create_invite.await_count == 1  # provisioner already joined — no second invite
    assert second.company_id == "co_first"
    assert second.status == "failed"
    admin.sign_in_user.assert_awaited()
    assert admin.sign_up_user.await_count == 1

    fetched = await repo.get_workspace("org_acme")
    assert fetched.company_id == "co_first"
    assert fetched.status == "failed"


async def test_seed_front_desk_and_office(repo):
    """End-state Task 4: a workspace seeded before Office existed gets Office (and only Office) on its next
    provision; its six starters, Front desk (the customer_operations id) among them, stay as they are."""
    six = {k: f"agent_{k}" for k in STARTER_KEYS - {"office"}}
    await _seed_owner(repo, starter_agent_ids=six)
    admin = _make_admin_mock()
    admin.create_agent = AsyncMock(return_value={"id": "agent_office"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    admin.create_agent.assert_awaited_once()
    assert admin.create_agent.call_args.kwargs["name"] == "Office"
    assert row.starter_agent_ids == {**six, "office": "agent_office"} and row.status == "active"
    admin.delete_agent.assert_not_awaited()  # nothing gets removed
    names = {key: name for key, name, *_ in STARTER_ROSTER}
    assert (names["customer_operations"], names["office"]) == ("Front desk", "Office")
    again = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert admin.create_agent.await_count == 1 and again.starter_agent_ids == row.starter_agent_ids


async def test_provision_org_for_second_workspace_adopts_identity_but_mints_new_company(repo):
    """A human who already provisioned (or joined) workspace org_prev provisions
    a NEW workspace org_new: their ONE identity is adopted via sign-in, but the
    new workspace gets its own company — the identity row names no company, so
    there is nothing cross-org to adopt — and org_prev's row is untouched."""
    await _seed_owner(repo, org_id="org_prev", company_id="co_prev")
    admin = _make_admin_mock()
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session"})
    admin.create_company = AsyncMock(return_value={"id": "co_new"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_new", owner_user_id="user_owner", owner_email="owner@acme.test")

    admin.create_company.assert_awaited_once()
    assert row.org_id == "org_new"
    assert row.company_id == "co_new"
    assert row.status == "active"
    admin.sign_in_user.assert_awaited_once_with(email="owner@acme.test", password="owner-password-plain")
    admin.sign_up_user.assert_not_awaited()

    prev = await repo.get_workspace("org_prev")
    assert prev.company_id == "co_prev"
    assert prev.member_user_ids == {"user_owner"}


# ----------------------------------------------------------------------
# Onboarding: G2 mint + writer safety
#
# onboarding_generation is minted EXACTLY ONCE at G2 (fresh company only,
# INV-3), and every later whole-item put on the workspace row (notably the
# reconcile-adopt retry path) must carry those fields forward (INV-4).
# ----------------------------------------------------------------------


async def test_provision_org_fresh_create_mints_onboarding_generation_at_g2(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert row.status == "active"
    assert row.onboarding_generation is not None
    assert row.funnel_state == "not_started"
    fetched = await repo.get_workspace("org_acme")
    assert fetched.onboarding_generation == row.onboarding_generation
    assert fetched.funnel_state == "not_started"


async def test_provision_org_reconcile_adopt_does_not_clobber_onboarding_fields(repo):
    """THE writer-safety proof: a reconcile-adopt retry (starter seed failed on
    attempt 1, succeeds on attempt 2, same company) must NOT reset
    onboarding_generation or the funnel, even though the whole-item
    put_workspace runs again."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-2"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    admin.create_agent = AsyncMock(side_effect=PaperclipApiError("CEO create failed", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    assert first.status == "failed"
    original_generation = first.onboarding_generation
    assert original_generation is not None

    await repo.claim_funnel_submission("org_acme", onboarding_generation=original_generation, submitted_by="user_owner")
    await repo.record_onboarding_ids(
        "org_acme", onboarding_generation=original_generation, goal_id="goal-1", project_id="proj-1"
    )
    pre = await repo.get_workspace("org_acme")
    assert pre.funnel_state == "submitted"
    assert pre.onboarding_goal_id == "goal-1"

    admin.create_agent = AsyncMock(
        side_effect=[
            {"id": "agent_ceo"},
            {"id": "agent_engineering"},
            {"id": "agent_finance"},
            {"id": "agent_growth"},
            {"id": "agent_analytics"},
            {"id": "agent_customer_operations"},
            {"id": "agent_office"},
        ]
    )
    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert admin.create_company.await_count == 1
    assert second.status == "active"
    assert second.onboarding_generation == original_generation
    assert second.funnel_state == "submitted"
    assert second.onboarding_goal_id == "goal-1"
    assert second.onboarding_project_id == "proj-1"
    assert second.onboarding_submitted_by == "user_owner"

    fetched = await repo.get_workspace("org_acme")
    assert fetched.onboarding_generation == original_generation
    assert fetched.funnel_state == "submitted"
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"
    assert set(fetched.starter_agent_ids or {}) == STARTER_KEYS


async def test_provision_org_company_recreated_mints_a_new_onboarding_generation(repo):
    """A genuinely NEW company for the same workspace (after the workspace row
    was deleted) mints a FRESH onboarding_generation — the funnel resets."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_first"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")
    first_generation = first.onboarding_generation
    await repo.claim_funnel_submission("org_acme", onboarding_generation=first_generation, submitted_by="user_owner")
    await repo.record_onboarding_ids(
        "org_acme", onboarding_generation=first_generation, goal_id="goal-1", project_id="proj-1"
    )

    await repo.delete("org_acme")

    admin.create_company = AsyncMock(return_value={"id": "co_second"})
    second = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert second.company_id == "co_second"
    assert second.onboarding_generation is not None
    assert second.onboarding_generation != first_generation
    assert second.funnel_state == "not_started"
    assert second.onboarding_goal_id is None
    assert second.onboarding_project_id is None


async def test_provision_org_conditional_mint_loses_and_adopts_the_stored_generation(repo):
    """Read-then-put is not atomic: when the conditioned mint-site put loses to
    a peer who landed first with a DIFFERENT generation, provision_org re-reads
    and adopts the STORED generation rather than raising or overwriting it."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    real_put = repo.put_workspace
    injected = {"done": False}

    async def _racing_put(ws, **kwargs):
        if not injected["done"] and kwargs.get("condition"):
            injected["done"] = True
            await real_put(dataclasses.replace(ws, onboarding_generation="peer-generation"))
        return await real_put(ws, **kwargs)

    repo.put_workspace = _racing_put

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert injected["done"], "the conditioned put was never attempted -- the race was never exercised"
    assert row.onboarding_generation == "peer-generation"
    assert (await repo.get_workspace("org_acme")).onboarding_generation == "peer-generation"


async def test_provision_org_retry_with_no_company_preserves_a_concurrently_set_generation(repo):
    """A confirmed-identity retry over a company-less ``failed`` workspace row
    runs the unclaimed "not reconciling" create branch. If a peer's retry
    already persisted onboarding_generation, this retry adopts it rather than
    stomping it with a fresh ULID."""
    await _seed_workspace(
        repo,
        company_id="",
        status="failed",
        members=(),
        onboarding_generation="peer-minted-generation",
        funnel_state="not_started",
    )
    await _seed_identity(repo)

    admin = _make_admin_mock()
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(owner_id="org_acme", owner_user_id="user_owner", owner_email="owner@acme.test")

    assert row.company_id == "co_acme"
    assert row.onboarding_generation == "peer-minted-generation"
    assert (await repo.get_workspace("org_acme")).onboarding_generation == "peer-minted-generation"


async def test_provision_member_does_not_mint_onboarding_generation(repo):
    """INV-3: a member join is NEVER a generation bump — the workspace's
    onboarding fields are untouched, and the member's identity row carries
    none."""
    await _seed_owner(repo, onboarding_generation="gen-1", funnel_state="submitted")
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "tok"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    ws = await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    assert ws.onboarding_generation == "gen-1"
    fetched = await repo.get_workspace("org_acme")
    assert fetched.onboarding_generation == "gen-1"
    assert fetched.funnel_state == "submitted"
    raw_identity = repo._table().get_item(Key={"user_id": "user_member"})["Item"]
    assert "onboarding_generation" not in raw_identity
    assert "funnel_state" not in raw_identity


# ----------------------------------------------------------------------
# provision_member
# ----------------------------------------------------------------------


async def test_provision_member_happy_path(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.create_invite = AsyncMock(return_value={"token": "invite-secret"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_abc"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    ws = await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    admin.sign_up_user.assert_awaited_once()
    assert admin.sign_up_user.call_args.kwargs["email"] == "member@acme.test"

    # The BOOTSTRAP ADMIN invites and approves — the owner is never signed in.
    admin.sign_in_user.assert_not_awaited()
    admin.create_invite.assert_awaited_once()
    ci_kwargs = admin.create_invite.call_args.kwargs
    assert ci_kwargs["session_cookie"] == "admin-cookie-test"
    assert ci_kwargs["company_id"] == "co_acme"
    assert ci_kwargs["email"] == "member@acme.test"
    assert ci_kwargs["human_role"] == "owner"

    admin.accept_invite.assert_awaited_once()
    ai_kwargs = admin.accept_invite.call_args.kwargs
    assert ai_kwargs["session_cookie"] == "member-session"
    assert ai_kwargs["invite_token"] == "invite-secret"

    admin.approve_join_request.assert_awaited_once()
    aj_kwargs = admin.approve_join_request.call_args.kwargs
    assert aj_kwargs["session_cookie"] == "admin-cookie-test"
    assert aj_kwargs["company_id"] == "co_acme"
    assert aj_kwargs["request_id"] == "req_abc"

    # The join is recorded on the WORKSPACE row; the member gets an identity row.
    assert ws.org_id == "org_acme"
    assert ws.company_id == "co_acme"
    assert ws.member_user_ids == {"user_owner", "user_member"}
    fetched = await repo.get_workspace("org_acme")
    assert fetched.member_user_ids == {"user_owner", "user_member"}
    assert fetched.provisioner_user_id == "user_owner"
    identity = await repo.get_identity("user_member")
    assert identity.paperclip_user_id == "pc_user_member"
    assert identity.paperclip_email == "member@acme.test"
    assert identity.status is None


async def test_provision_member_raises_when_org_not_provisioned(repo):
    """No active workspace means we MUST refuse (caller should retry)."""
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(OrgNotProvisionedError):
        await prov.provision_member(owner_id="org_phantom", user_id="user_member", email="member@phantom.test")
    admin.sign_up_user.assert_not_awaited()
    assert await repo.get_identity("user_member") is None


async def test_provision_member_raises_when_workspace_not_active(repo):
    """A failed/disabled workspace is not a company to join."""
    await _seed_owner(repo, status="failed")
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(OrgNotProvisionedError):
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")
    admin.sign_up_user.assert_not_awaited()


async def test_provision_member_idempotent_on_second_call(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_abc"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    first = await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")
    second = await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    assert first.company_id == second.company_id
    assert "user_member" in second.member_user_ids
    assert admin.sign_up_user.await_count == 1
    assert admin.create_invite.await_count == 1


async def test_provision_member_signup_failure_writes_durable_failed_claim(repo):
    """Member sign-up failing net-new leaves the member's sign-up claim durably
    ``failed`` with NO fabricated paperclip_user_id (so a retry reclaims and
    re-runs sign-up) and does NOT record the join on the workspace row, which
    stays active. provision_member still RAISES."""
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup email", status_code=422, body="unprocessable"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    identity = await repo.get_identity("user_member")
    assert identity is not None and identity.status == "failed"
    raw = repo._table().get_item(Key={"user_id": "user_member"})["Item"]
    assert "paperclip_user_id" not in raw
    assert "company_id" not in raw
    ws = await repo.get_workspace("org_acme")
    assert ws.status == "active"
    assert "user_member" not in ws.member_user_ids


async def test_provision_member_invite_failure_does_not_record_join(repo):
    """Sign-up landed (identity settled), then the invite failed: the confirmed
    identity is kept (a retry adopts it via sign-in), the join is NOT recorded,
    and the workspace is never downgraded."""
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("perm denied", status_code=403, body="nope"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    identity = await repo.get_identity("user_member")
    assert identity.paperclip_user_id == "pc_user_member"
    assert identity.status is None
    ws = await repo.get_workspace("org_acme")
    assert ws.status == "active"
    assert "user_member" not in ws.member_user_ids


async def test_provision_member_raises_when_accept_invite_missing_id(repo):
    """If Paperclip's accept_invite ever changes shape (the flat join-request
    row with top-level ``id``), fail loudly rather than silently skip approve."""
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "ms"})
    admin.accept_invite = AsyncMock(return_value={"unexpected": "shape"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError, match="missing 'id'"):
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")
    admin.approve_join_request.assert_not_awaited()
    assert "user_member" not in (await repo.get_workspace("org_acme")).member_user_ids


async def test_provision_member_records_join_when_already_a_paperclip_member(repo):
    """Prod defect 2026-09-13: a human already in the company answers the
    accept with 409 ("You already belong to this company"). The join is done on
    Paperclip, so it must be RECORDED on the workspace row — otherwise the
    Teams fence (member_user_ids) never converges and every route stays shut."""
    await _seed_owner(repo)
    await _seed_identity(
        repo, "user_member", paperclip_user_id="pc_user_member", password="member-pw", email="member@acme.test"
    )
    admin = _make_admin_mock()
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.accept_invite = AsyncMock(
        side_effect=PaperclipApiError(
            "You already belong to this company",
            status_code=409,
            body={"error": "You already belong to this company"},
        )
    )
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    ws = await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    assert "user_member" in ws.member_user_ids
    assert "user_member" in (await repo.get_workspace("org_acme")).member_user_ids
    # Adopted, not re-signed-up: the member already had a Better Auth account.
    admin.sign_up_user.assert_not_awaited()
    admin.sign_in_user.assert_awaited_once_with(email="member@acme.test", password="member-pw")
    assert (await repo.get_identity("user_member")).paperclip_user_id == "pc_user_member"


async def test_provision_member_concurrent_retries_after_pre_signup_failure_sign_up_once(repo, monkeypatch):
    """Sign-up fails once (transient 5xx) → credential-less failed claim. The
    next page load fires concurrent retries; exactly ONE may sign up — the
    other must LOSE the reclaimed claim while the winner is mid sign-up, then
    adopt the winner's recorded join without signing in itself.

    Deterministic: the winner's sign-up is paused on an Event so the second
    retry provably contends for the claim."""
    import asyncio

    import core.services.paperclip_provisioning as prov_mod

    await _seed_owner(repo)
    first = _make_admin_mock()
    first.sign_up_user = AsyncMock(side_effect=PaperclipApiError("upstream", status_code=503, body="boom"))
    prov = PaperclipProvisioning(admin_client=first, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="member@acme.test")
    marker = await repo.get_identity("user_member", consistent_read=True)
    assert marker is not None and marker.status == "failed" and not marker.paperclip_user_id

    monkeypatch.setattr(prov_mod, "_SEED_WAIT_INTERVAL_S", 0.01)
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_ATTEMPTS", 300)
    sign_up_started = asyncio.Event()
    release_sign_up = asyncio.Event()
    claim_results: list[bool] = []
    real_claim = repo.try_acquire_seed_claim

    async def observed_claim(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        claim_results.append(claimed)
        return claimed

    monkeypatch.setattr(repo, "try_acquire_seed_claim", observed_claim)

    async def paused_sign_up(**_kwargs):
        sign_up_started.set()
        await release_sign_up.wait()
        return {"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"}

    retry = _make_admin_mock()
    retry.sign_up_user = AsyncMock(side_effect=paused_sign_up)
    prov2 = PaperclipProvisioning(admin_client=retry, repo=repo, env_name="dev")
    kwargs = dict(owner_id="org_acme", user_id="user_member", email="member@acme.test")

    winner = asyncio.create_task(prov2.provision_member(**kwargs))
    await asyncio.wait_for(sign_up_started.wait(), timeout=5)
    loser = asyncio.create_task(prov2.provision_member(**kwargs))
    while len(claim_results) < 2:
        await asyncio.sleep(0.01)
    assert claim_results == [True, False]
    release_sign_up.set()
    winner_ws, loser_ws = await asyncio.wait_for(asyncio.gather(winner, loser), timeout=10)

    assert retry.sign_up_user.await_count == 1, "concurrent retries must sign up exactly once"
    retry.sign_in_user.assert_not_awaited()  # the loser adopted; no owner sign-in exists any more
    assert retry.create_invite.await_count == 1
    assert "user_member" in winner_ws.member_user_ids
    assert "user_member" in loser_ws.member_user_ids
    assert (await repo.get_identity("user_member")).paperclip_user_id == "pc_user_member"


# ----------------------------------------------------------------------
# find_org_owner / get_workspace
# ----------------------------------------------------------------------


async def test_find_org_owner_returns_active_workspace(repo):
    await _seed_owner(repo)
    prov = PaperclipProvisioning(admin_client=_make_admin_mock(), repo=repo, env_name="dev")
    found = await prov.find_org_owner("org_acme")
    assert found is not None
    assert found.company_id == "co_acme"
    assert found.status == "active"


@pytest.mark.parametrize("status", ["disabled", "failed", "provisioning"])
async def test_find_org_owner_returns_none_for_non_active_workspace(repo, status):
    """A non-active workspace is not a company to join — the caller raises
    OrgNotProvisionedError and the retry path takes over."""
    await _seed_owner(repo, org_id="org_dead", status=status)
    prov = PaperclipProvisioning(admin_client=_make_admin_mock(), repo=repo, env_name="dev")
    assert await prov.find_org_owner("org_dead") is None
    # get_workspace is status-agnostic.
    assert (await prov.get_workspace("org_dead")).status == status


# ----------------------------------------------------------------------
# archive_member / remove_member
# ----------------------------------------------------------------------


async def _seed_member(repo, *, org_id="org_acme", company_id="co_acme"):
    await _seed_workspace(repo, org_id=org_id, company_id=company_id, members=("user_owner", "user_member"))
    await _seed_identity(
        repo, "user_member", paperclip_user_id="pc_user_member", password="member-pw", email="member@acme.test"
    )


async def test_archive_member_removes_ddb_membership_then_archives_paperclip(repo):
    """``archive_member`` drops the member from the workspace's
    ``member_user_ids`` (the Teams fence), finds their Paperclip ``member.id``
    via ``list_members`` and archives it as the bootstrap admin. The workspace
    row stays active; the identity row is untouched."""
    await _seed_member(repo)
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(
        return_value={
            "members": [
                {"id": "mem_other", "principalId": "pc_user_other"},
                {"id": "mem_target", "principalId": "pc_user_member"},
            ]
        }
    )
    admin.archive_member = AsyncMock(
        return_value={"member": {"id": "mem_target", "status": "archived"}, "reassignedIssueCount": 0}
    )
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", org_id="org_acme")

    admin.list_members.assert_awaited_once()
    list_kwargs = admin.list_members.call_args.kwargs
    assert list_kwargs["company_id"] == "co_acme"
    assert list_kwargs["session_cookie"] == "admin-cookie-test"

    admin.archive_member.assert_awaited_once()
    archive_kwargs = admin.archive_member.call_args.kwargs
    assert archive_kwargs["company_id"] == "co_acme"
    assert archive_kwargs["member_id"] == "mem_target"
    assert archive_kwargs["session_cookie"] == "admin-cookie-test"

    ws = await repo.get_workspace("org_acme")
    assert ws.member_user_ids == {"user_owner"}
    assert ws.status == "active"
    assert (await repo.get_identity("user_member")).paperclip_user_id == "pc_user_member"


async def test_archive_member_is_scoped_to_one_workspace(repo):
    """One human, two workspaces (two workspace rows, ONE identity row).
    Removing them from org_a archives them in org_a's company ONLY; org_b's
    row still lists them and its company is never touched."""
    await _seed_workspace(repo, org_id="org_a", company_id="co_a", members=("user_h", "user_x"), provisioner="user_x")
    await _seed_workspace(repo, org_id="org_b", company_id="co_b", members=("user_h",), provisioner="user_h")
    await _seed_identity(repo, "user_h", paperclip_user_id="pc_h", password="h-pw", email="h@acme.test")

    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value=[{"id": "mem_h_in_a", "principalId": "pc_h"}])
    admin.archive_member = AsyncMock(return_value={"member": {"id": "mem_h_in_a"}})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_h", org_id="org_a")

    ws_a = await repo.get_workspace("org_a")
    ws_b = await repo.get_workspace("org_b")
    assert ws_a.member_user_ids == {"user_x"}
    assert ws_b.member_user_ids == {"user_h"}
    assert ws_a.status == ws_b.status == "active"

    admin.list_members.assert_awaited_once()
    assert admin.list_members.call_args.kwargs["company_id"] == "co_a"
    admin.archive_member.assert_awaited_once()
    assert admin.archive_member.call_args.kwargs["company_id"] == "co_a"
    assert admin.archive_member.call_args.kwargs["member_id"] == "mem_h_in_a"

    # The one identity survives: the human is still a member of org_b.
    assert (await repo.get_identity("user_h")).paperclip_user_id == "pc_h"


async def test_archive_member_noop_for_missing_workspace(repo):
    """No workspace row → no-op (does not call Paperclip)."""
    admin = _make_admin_mock()
    admin.list_members = AsyncMock()
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", org_id="org_missing")

    admin.list_members.assert_not_awaited()
    admin.archive_member.assert_not_awaited()
    assert await repo.get_workspace("org_missing") is None  # never resurrected


async def test_archive_member_without_identity_only_drops_membership(repo):
    """A member with no confirmed identity has nothing to archive on Paperclip;
    the DDB fence is still dropped."""
    await _seed_workspace(repo, members=("user_owner", "user_member"))
    admin = _make_admin_mock()
    admin.list_members = AsyncMock()
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", org_id="org_acme")

    admin.list_members.assert_not_awaited()
    assert (await repo.get_workspace("org_acme")).member_user_ids == {"user_owner"}


async def test_archive_member_handles_flat_list_response(repo):
    """``list_members`` may return a flat list (admin BFF unwraps the envelope)."""
    await _seed_member(repo)
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value=[{"id": "mem_target", "principalId": "pc_user_member"}])
    admin.archive_member = AsyncMock(return_value={"member": {"id": "mem_target"}})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", org_id="org_acme")

    admin.archive_member.assert_awaited_once()
    assert "user_member" not in (await repo.get_workspace("org_acme")).member_user_ids


async def test_archive_member_logs_and_returns_when_paperclip_member_missing(repo):
    """Paperclip no longer lists the member (already archived, drift): the DDB
    membership is still dropped and archive is NOT called — no raise."""
    await _seed_member(repo)
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value={"members": []})
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.archive_member(user_id="user_member", org_id="org_acme")

    admin.archive_member.assert_not_awaited()
    ws = await repo.get_workspace("org_acme")
    assert ws.member_user_ids == {"user_owner"}
    assert ws.status == "active"


async def test_archive_member_reraises_paperclip_5xx_after_dropping_the_fence(repo):
    """The archive call 5xx's: re-raise so the webhook enqueues a retry. The DDB
    membership (the Teams authorization fence) is already dropped — it goes
    FIRST — and the workspace + identity rows the retry needs are intact."""
    await _seed_member(repo)
    admin = _make_admin_mock()
    admin.list_members = AsyncMock(return_value={"members": [{"id": "mem_target", "principalId": "pc_user_member"}]})
    admin.archive_member = AsyncMock(side_effect=PaperclipApiError("server-down", 503, ""))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.archive_member(user_id="user_member", org_id="org_acme")

    ws = await repo.get_workspace("org_acme")
    assert ws.member_user_ids == {"user_owner"}
    assert ws.status == "active"
    assert (await repo.get_identity("user_member")).paperclip_user_id == "pc_user_member"

    # The retry completes the Paperclip side against the same rows.
    admin.archive_member = AsyncMock(return_value={"member": {"id": "mem_target"}})
    await prov.archive_member(user_id="user_member", org_id="org_acme")
    admin.archive_member.assert_awaited_once()


# ----------------------------------------------------------------------
# disable_workspace + purge
# ----------------------------------------------------------------------


async def test_disable_workspace_marks_disabled_with_grace_window(repo):
    await _seed_owner(repo)
    prov = PaperclipProvisioning(admin_client=_make_admin_mock(), repo=repo, env_name="dev")

    before = datetime.now(timezone.utc)
    await prov.disable_workspace(org_id="org_acme", grace_days=7)
    after = datetime.now(timezone.utc)

    fetched = await repo.get_workspace("org_acme")
    assert fetched.status == "disabled"
    assert fetched.scheduled_purge_at is not None
    assert 6 * 86400 < (fetched.scheduled_purge_at - before).total_seconds()
    assert (fetched.scheduled_purge_at - after).total_seconds() < 8 * 86400
    # The identity outlives the workspace.
    assert (await repo.get_identity("user_owner")).paperclip_user_id == "pc_user_owner"


async def test_disable_workspace_noop_for_missing_row(repo):
    prov = PaperclipProvisioning(admin_client=_make_admin_mock(), repo=repo, env_name="dev")
    await prov.disable_workspace(org_id="org_missing")
    assert await repo.get_workspace("org_missing") is None  # never resurrected


async def test_purge_deletes_workspace_row_keeps_identities_and_does_not_call_paperclip(repo):
    """Purge removes the workspace row only. The Paperclip company is left for
    an out-of-band sweep, and identity rows — a human's identity outlives any
    one workspace — are untouched."""
    await _seed_member(repo)
    await _seed_identity(repo)
    admin = _make_admin_mock()
    admin.archive_member = AsyncMock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    await prov.purge(org_id="org_acme")

    assert await repo.get_workspace("org_acme") is None
    assert (await repo.get_identity("user_owner")).paperclip_user_id == "pc_user_owner"
    assert (await repo.get_identity("user_member")).paperclip_user_id == "pc_user_member"
    admin.archive_member.assert_not_awaited()


async def test_purge_idempotent_for_missing_row(repo):
    prov = PaperclipProvisioning(admin_client=_make_admin_mock(), repo=repo, env_name="dev")
    await prov.purge(org_id="org_missing")


# ----------------------------------------------------------------------
# retryable error classification (consumed by the webhook handler)
# ----------------------------------------------------------------------


def test_org_not_provisioned_error_is_retryable_by_default():
    exc = OrgNotProvisionedError("org missing")
    assert getattr(exc, "retryable", False) is True


@pytest.mark.parametrize(("status_code", "retryable"), [(503, True), (409, False), (429, True)])
async def test_paperclip_api_error_retryable_annotation_on_provision_org(repo, status_code, retryable):
    """5xx/429 are retryable; other 4xx (e.g. a 409 dup-email) are permanent."""
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("x", status_code=status_code, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_org(owner_id="org_a", owner_user_id="user_a", owner_email="a@a.test")
    assert getattr(ei.value, "retryable", None) is retryable


async def test_paperclip_api_error_annotated_on_provision_member(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("server", status_code=500, body="x"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await prov.provision_member(owner_id="org_acme", user_id="user_member", email="m@a.test")
    assert getattr(ei.value, "retryable", None) is True


# ----------------------------------------------------------------------
# Staged sign-up credentials (lost response / crash mid sign-up)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("member", [False, True], ids=["owner", "member"])
@pytest.mark.parametrize("crashed", [False, True], ids=["lost-response", "crashed"])
async def test_signup_retry_authenticates_with_credentials_saved_before_request(repo, member, crashed):
    """The remote account can exist even when its creation never returns: the
    credentials are staged on the identity row BEFORE the sign-up request, and
    the retry recovers the account with exactly those credentials."""
    if member:
        await _seed_owner(repo)
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    user_id = "user_member" if member else "user_owner"
    email = "new@acme.test"
    signup_password = None

    async def signup(**kwargs):
        nonlocal signup_password
        if signup_password is None:
            signup_password = kwargs["password"]
            pending = await repo.get_identity(user_id, consistent_read=True)
            assert decrypt(pending.paperclip_password_encrypted) == signup_password
            assert pending.paperclip_email == email
            if crashed:
                raise KeyboardInterrupt("process died after remote account creation")
            raise TimeoutError("response lost after remote account creation")
        assert kwargs["password"] == signup_password
        assert kwargs["email"] == email
        raise PaperclipApiError("duplicate", 422, {"code": "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL"})

    async def signin(**kwargs):
        assert kwargs["email"] == email
        assert kwargs["password"] == signup_password
        return {"user": {"id": "pc_recovered"}, "_session_cookie": "member-cookie"}

    admin.sign_up_user.side_effect = signup
    admin.sign_in_user.side_effect = signin

    async def run(current_email):
        if member:
            return await prov.provision_member(owner_id="org_acme", user_id=user_id, email=current_email)
        return await prov.provision_org(owner_id="org_acme", owner_user_id=user_id, owner_email=current_email)

    with pytest.raises(KeyboardInterrupt if crashed else TimeoutError):
        await run(email)
    if crashed:
        repo._table().update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET seeding_claimed_at = :old",
            ExpressionAttributeValues={":old": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()},
        )
    # A Clerk email change during the retry must not discard the staged identity.
    ws = await run("changed@acme.test")
    assert ws.status == "active"
    assert user_id in ws.member_user_ids
    identity = await repo.get_identity(user_id)
    assert identity.paperclip_user_id == "pc_recovered"
    assert identity.paperclip_email == email
    assert decrypt(identity.paperclip_password_encrypted) == signup_password
    assert identity.status is None
    assert admin.accept_invite.await_args.kwargs["session_cookie"] == "member-cookie"
    await run("changed@acme.test")  # settled: short-circuits
    assert admin.sign_up_user.await_count == 2


@pytest.mark.parametrize("member", [False, True], ids=["owner", "member"])
async def test_duplicate_email_without_matching_staged_password_stays_failed(repo, member):
    """An unrelated existing Better Auth account (422 duplicate, sign-in 401)
    is a sign-up FAILURE: never infer identity or membership from email."""
    if member:
        await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user.side_effect = PaperclipApiError(
        "duplicate", 422, {"code": "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL"}
    )
    admin.sign_in_user.side_effect = PaperclipApiError("wrong password", 401, {"code": "INVALID_EMAIL_OR_PASSWORD"})
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    user_id = "user_member" if member else "user_owner"
    with pytest.raises(PaperclipApiError) as error:
        if member:
            await prov.provision_member(owner_id="org_acme", user_id=user_id, email="collision@acme.test")
        else:
            await prov.provision_org(owner_id="org_acme", owner_user_id=user_id, owner_email="collision@acme.test")
    assert error.value.status_code == 422
    identity = await repo.get_identity(user_id)
    assert identity.status == "failed"
    assert not identity.paperclip_user_id
    admin.sign_in_user.assert_awaited_once()
    admin.create_company.assert_not_awaited()
    admin.create_invite.assert_not_awaited()
    if member:
        assert user_id not in (await repo.get_workspace("org_acme")).member_user_ids
    else:
        assert (await repo.get_workspace("org_acme")).status == "failed"
