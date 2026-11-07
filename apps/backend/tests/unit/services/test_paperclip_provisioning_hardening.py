"""Fresh-workspace starter roster + identity/workspace failure-edge contract tests.

``provision_org`` writes TWO rows: the provisioner's IDENTITY row (keyed by
their Clerk user id — the sign-up claim and their Better Auth credentials) and
the WORKSPACE row (keyed by the org id — company facts + members). These tests
pin which row each step touches, and that a failure never downgrades an
``active`` workspace or a confirmed identity.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.encryption import encrypt
from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipWorkspace
from core.services.paperclip_admin_client import PaperclipApiError


EXPECTED_ROSTER = (
    ("ceo", "CEO", "ceo", "CEO", None),
    ("engineering", "Engineering", "engineer", "Engineering", "ceo"),
    ("finance", "Finance", "cfo", "Finance", "ceo"),
    ("growth", "Growth", "cmo", "Growth", "ceo"),
    ("analytics", "Analytics", "researcher", "Analytics", "ceo"),
    ("customer_operations", "Front desk", "general", "Front desk", "ceo"),
    ("office", "Office", "general", "Office", "ceo"),
)
ORG = "org_xyz"
OWNER = "user_owner"


def _doubles(monkeypatch, *, workspace=None, identity=None):
    """AsyncMock admin + a repo double speaking the workspace/identity API."""
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)

    admin = MagicMock()
    admin._http = MagicMock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_in"})
    admin.create_company = AsyncMock(return_value={"id": "co_xyz"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()
    admin.create_agent = AsyncMock(return_value={"id": "seed-agent-default"})
    admin.list_agents = AsyncMock(return_value=[])
    admin.delete_agent = AsyncMock(return_value={})

    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=workspace)
    repo.get_identity = AsyncMock(return_value=identity)
    repo.org_company_ids = AsyncMock(return_value=[])
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
    repo.put_identity = AsyncMock()
    repo.put_workspace = AsyncMock()
    repo.fail_signup_claim = AsyncMock()
    repo.update_status = AsyncMock(return_value=True)
    repo.put_failed_marker_if_absent = AsyncMock(return_value=True)

    import core.services.paperclip_admin_session as admin_session_mod
    from core.repositories import container_repo

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-seed-test"}))
    return admin, repo


async def _provision(admin, repo):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    return await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id=ORG, owner_user_id=OWNER, owner_email="owner@example.com"
    )


def _workspace(**overrides) -> PaperclipWorkspace:
    now = datetime.now(timezone.utc)
    fields = dict(org_id=ORG, company_id="co_existing", status="failed", created_at=now, updated_at=now)
    fields.update(overrides)
    return PaperclipWorkspace(**fields)


@pytest.mark.asyncio
async def test_fresh_workspace_creates_the_complete_starter_roster(monkeypatch):
    """A fresh row records six role identities, not legacy seed protection."""
    admin, repo = _doubles(monkeypatch)
    agent_ids = [f"agent-{key}" for key, *_ in EXPECTED_ROSTER]
    admin.create_agent = AsyncMock(side_effect=[{"id": agent_id} for agent_id in agent_ids])

    row = await _provision(admin, repo)

    assert row.org_id == ORG
    assert row.status == "active"
    assert row.provisioner_user_id == OWNER
    assert row.member_user_ids == {OWNER}
    assert row.seeded_agent_ids is None
    assert row.starter_agent_ids == dict(zip((item[0] for item in EXPECTED_ROSTER), agent_ids, strict=True))

    calls = admin.create_agent.await_args_list
    assert len(calls) == 7
    assert [call.kwargs["name"] for call in calls] == [item[1] for item in EXPECTED_ROSTER]
    assert [call.kwargs["role"] for call in calls] == [item[2] for item in EXPECTED_ROSTER]
    assert [call.kwargs["title"] for call in calls] == [item[3] for item in EXPECTED_ROSTER]
    assert [call.kwargs.get("reports_to") for call in calls] == [None] + ["agent-ceo"] * 6
    assert [call.kwargs["idempotency_key"] for call in calls] == [
        f"seed:starter:{item[0]}:v1" for item in EXPECTED_ROSTER
    ]
    # Every seeded agent shares ONE adapter config: the native ``hermes_gateway``
    # facade authenticates by the OWNER's hook token and Paperclip supplies the
    # agent id per run. There is no per-agent operator device to pair.
    configs = [call.kwargs["adapter_config"] for call in calls]
    assert {call.kwargs["adapter_type"] for call in calls} == {"hermes_gateway"}
    assert all(cfg == configs[0] for cfg in configs)
    assert "devicePrivateKeyPem" not in configs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_key", [item[0] for item in EXPECTED_ROSTER])
async def test_any_missing_starter_keeps_the_row_retryable(monkeypatch, failed_key):
    """A create failure preserves successful IDs without leaking its body."""
    admin, repo = _doubles(monkeypatch)

    async def create_agent(**kwargs):
        key = kwargs["idempotency_key"].removeprefix("seed:starter:").removesuffix(":v1")
        if key == failed_key:
            raise PaperclipApiError(
                "request contained secret-token",
                status_code=500,
                body={"credential": "secret-token"},
            )
        return {"id": f"agent-{key}"}

    admin.create_agent = AsyncMock(side_effect=create_agent)

    row = await _provision(admin, repo)

    expected_ids = (
        {} if failed_key == "ceo" else {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != failed_key}
    )
    expected_missing = [item[0] for item in EXPECTED_ROSTER if item[0] not in expected_ids]
    assert row.status == "failed"
    assert row.company_id == "co_xyz"  # kept, so the next poll reconciles instead of reminting
    assert row.seeded_agent_ids is None
    assert row.starter_agent_ids == expected_ids
    assert row.last_error == f"provision_org: missing required starter agents: {', '.join(expected_missing)}"
    assert "secret" not in row.last_error
    repo.put_workspace.assert_awaited()
    admin.list_agents.assert_not_awaited()
    admin.delete_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_signup_claims_and_settles_the_identity_row(monkeypatch):
    """The sign-up fence is on the provisioner's IDENTITY row, and the identity
    is persisted right after sign-up — before the company exists."""
    admin, repo = _doubles(monkeypatch)
    order: list[str] = []
    repo.put_identity.side_effect = lambda *_a, **_k: order.append("put_identity")
    admin.create_company.side_effect = lambda **_k: order.append("create_company") or {"id": "co_xyz"}

    await _provision(admin, repo)

    repo.try_acquire_seed_claim.assert_awaited_once()
    assert repo.try_acquire_seed_claim.await_args.args == (OWNER,)
    assert set(repo.try_acquire_seed_claim.await_args.kwargs) == {"claim_id"}
    identity = repo.put_identity.await_args.args[0]
    assert isinstance(identity, PaperclipIdentity)
    assert identity.user_id == OWNER
    assert identity.paperclip_user_id == "pc_user_1"
    assert identity.status is None  # settled
    assert order == ["put_identity", "create_company"]
    # The workspace row carries no credentials.
    ws = repo.put_workspace.await_args.args[0]
    assert not hasattr(ws, "paperclip_password_encrypted")


@pytest.mark.asyncio
async def test_lost_seed_claim_makes_no_side_effect(monkeypatch):
    import core.services.paperclip_provisioning as mod

    monkeypatch.setattr(mod, "_SEED_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(mod, "_SEED_WAIT_INTERVAL_S", 0)
    admin, repo = _doubles(monkeypatch)
    repo.try_acquire_seed_claim.return_value = False

    with pytest.raises(PaperclipApiError) as exc:
        await _provision(admin, repo)

    assert exc.value.status_code == 409
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    repo.put_identity.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_with_a_company_on_record_but_no_workspace_row_is_refused(monkeypatch):
    """The duplicate-company guard: 409 before any side effect, and no failed
    marker lands on a workspace awaiting migration."""
    admin, repo = _doubles(monkeypatch)
    repo.org_company_ids.return_value = ["co_legacy"]

    with pytest.raises(PaperclipApiError) as exc:
        await _provision(admin, repo)

    assert exc.value.status_code == 409
    repo.try_acquire_seed_claim.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    repo.put_failed_marker_if_absent.assert_not_awaited()
    repo.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_before_any_workspace_row_downgrades_claim_and_puts_marker(monkeypatch):
    admin, repo = _doubles(monkeypatch)
    admin.create_company.side_effect = PaperclipApiError("boom", status_code=500, body=None)

    with pytest.raises(PaperclipApiError):
        await _provision(admin, repo)

    claim_id = repo.try_acquire_seed_claim.await_args.kwargs["claim_id"]
    repo.fail_signup_claim.assert_awaited_once()
    assert repo.fail_signup_claim.await_args.args == (OWNER,)
    assert repo.fail_signup_claim.await_args.kwargs["claim_id"] == claim_id
    repo.put_failed_marker_if_absent.assert_awaited_once()
    assert repo.put_failed_marker_if_absent.await_args.kwargs["org_id"] == ORG
    repo.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_on_existing_workspace_never_downgrades_an_active_one(monkeypatch):
    """A reconcile failure marks the WORKSPACE row failed in place, guarded so a
    concurrent attempt's ``active`` wins; a confirmed identity is adopted via
    sign-in and never touched by the failure path."""
    identity = PaperclipIdentity(
        user_id=OWNER,
        paperclip_user_id="pc_user_1",
        paperclip_password_encrypted="",
        paperclip_email="owner@example.com",
    )
    admin, repo = _doubles(
        monkeypatch,
        workspace=_workspace(member_user_ids={OWNER}, starter_agent_ids={}),
        identity=identity,
    )
    identity.paperclip_password_encrypted = encrypt("pw")
    admin.create_agent.side_effect = RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        await _provision(admin, repo)

    admin.sign_in_user.assert_awaited_once()
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()  # adopted co_existing
    repo.try_acquire_seed_claim.assert_not_awaited()
    repo.fail_signup_claim.assert_not_awaited()  # confirmed identity is never downgraded
    repo.put_identity.assert_not_awaited()
    repo.update_status.assert_awaited_once()
    assert repo.update_status.await_args.args == (ORG,)
    assert repo.update_status.await_args.kwargs["status"] == "failed"
    assert repo.update_status.await_args.kwargs["not_if_status"] == "active"
    repo.put_failed_marker_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_complete_workspace_short_circuits(monkeypatch):
    ws = _workspace(status="active", starter_agent_ids={key: f"a-{key}" for key, *_ in EXPECTED_ROSTER})
    admin, repo = _doubles(monkeypatch, workspace=ws)

    assert await _provision(admin, repo) is ws

    repo.get_workspace.assert_awaited_once_with(ORG)
    repo.try_acquire_seed_claim.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()
