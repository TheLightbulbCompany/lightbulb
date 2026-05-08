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
    admin.create_invite = AsyncMock(return_value={"token": "invite-token-default"})
    # accept_invite returns the join request flat — see access.ts:3604
    # (toJoinRequestResponse spreads the row, so ``id`` is top-level).
    admin.accept_invite = AsyncMock(return_value={"id": "req_default"})
    admin.approve_join_request = AsyncMock(return_value={})
    admin.disable_company = AsyncMock(return_value={})
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


async def test_provision_org_happy_path(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session-1"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
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
    assert cc_kwargs["name"] == "owner@acme.test"
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

    admin.create_agent.assert_awaited_once()
    agent_kwargs = admin.create_agent.call_args.kwargs
    assert agent_kwargs["company_id"] == "co_acme"
    # Canonical adapter_type is the underscore form ("openclaw_gateway")
    # — the previous hyphenated value ("openclaw-gateway") was rejected
    # by Paperclip's assertKnownAdapterType so seed-agent creation
    # silently failed on every provision. See Task 13.
    assert agent_kwargs["adapter_type"] == "openclaw_gateway"
    assert agent_kwargs["adapter_config"]["url"] == "wss://ws-dev.isol8.co"
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


async def test_provision_org_seed_agent_failure_is_not_fatal(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "tok"})
    admin.create_company = AsyncMock(return_value={"id": "co_acme"})
    admin.create_agent = AsyncMock(side_effect=PaperclipApiError("agent create failed", status_code=500, body="boom"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    row = await prov.provision_org(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    # Company is still active; the agent seeding is best-effort.
    assert row.status == "active"
    assert row.company_id == "co_acme"


async def test_provision_org_signup_failure_marks_failed(repo):
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("auth disabled", status_code=403, body="forbidden"))
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await prov.provision_org(
            owner_id="org_acme",
            owner_user_id="user_owner",
            owner_email="owner@acme.test",
        )

    # Failed-state row persisted for observability
    failed_row = await repo.get("user_owner")
    assert failed_row is not None
    assert failed_row.status == "failed"
    assert "provision_org failed" in (failed_row.last_error or "")
    # company_id is empty since we never got that far
    assert failed_row.company_id == ""


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


async def test_provision_member_signup_failure_marks_failed(repo):
    await _seed_owner(repo)
    admin = _make_admin_mock()
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup email", status_code=409, body="conflict"))
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
    # company_id IS known here (from the org lookup) so it's persisted
    assert failed.company_id == "co_acme"
    assert "provision_member failed" in (failed.last_error or "")


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
    """Codex P1 (round 3): ``find_org_owner`` must NOT return a disabled row.

    ``disable()`` flips ``status="disabled"`` and leaves the row in DDB
    until the purge worker runs (up to 30 days). If the OLDEST row in
    an org is disabled, the previous "min(created_at)" pick handed
    back a stale Better Auth account and broke ``provision_member``
    even when other active admins existed.

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
    # disable_company is NEVER called from disable() — that's purge's job
    admin.disable_company.assert_not_awaited()


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
    admin.disable_company.assert_not_awaited()


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
    # Company NOT archived because the owner is still around
    admin.disable_company.assert_not_awaited()


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
    # Company NOT archived: another member remains.
    admin.disable_company.assert_not_awaited()


async def test_purge_idempotent_for_missing_row(repo):
    admin = _make_admin_mock()
    prov = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await prov.purge(user_id="user_does_not_exist")
    admin.disable_company.assert_not_awaited()


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


# test_purge_swallows_disable_company_failure removed — purge no longer calls
# disable_company in v1 (see purge() docstring; orphan-company cleanup is an
# out-of-band ops concern, not a per-user purge step).
