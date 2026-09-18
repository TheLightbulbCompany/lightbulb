"""Unit tests for scripts/wipe_org.py — the org-owned workspace teardown.

Every collaborator is patched on the script module (``mod.<name>``), the same
style tests/unit/scripts/test_migrate_workspace_to_hermes.py uses: nothing here
reaches ECS, EFS, S3, Aurora, DynamoDB, Stripe, Metronome or Clerk. The point
of the ``wipe_mocks`` fixture is that a test can assert on what the script
CALLED, and — for the preview tests — that it called nothing at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import wipe_org as mod

ORG = "org_target1"
MEMBER = "user_member1"
OTHER_MEMBER = "user_member2"
COMPANY = "company-abc"


def _paperclip_row(user_id: str = MEMBER, **overrides) -> dict:
    return {
        "user_id": user_id,
        "org_id": ORG,
        "company_id": COMPANY,
        "paperclip_user_id": f"pc_{user_id}",
        "paperclip_email": f"{user_id}@example.com",
        "status": "active",
        **overrides,
    }


def _container_row(**overrides) -> dict:
    return {
        "owner_id": ORG,
        "service_name": f"openclaw-{ORG}-abc",
        "runtime_host": "ecs",
        "bedrock_gate_token_hash": "hash-1",
        **overrides,
    }


def _billing_row(**overrides) -> dict:
    return {
        "owner_id": ORG,
        "metronome_customer_id": "met_1",
        "stripe_customer_id": "cus_1",
        **overrides,
    }


def _inventory(**overrides) -> mod._Inventory:
    defaults = {
        "org_id": ORG,
        "org": {"id": ORG, "name": "Target Workspace"},
        "members": [{"user_id": MEMBER, "email": f"{MEMBER}@example.com"}],
        "paperclip_rows": [_paperclip_row()],
        "container": _container_row(),
        "billing": _billing_row(),
        "user_rows": {MEMBER: {"user_id": MEMBER}},
    }
    return mod._Inventory(**{**defaults, **overrides})


@pytest.fixture
def paperclip_repo():
    """One shared AsyncMock repo behind every ``PaperclipRepo(...)`` construction."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[_paperclip_row()])
    repo.get_user_ids_for_company = AsyncMock(return_value=[MEMBER])
    repo.delete = AsyncMock()
    repo.delete_pending_brief = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def wipe_mocks(monkeypatch, paperclip_repo, tmp_path):
    """Patch every collaborator the script reaches. Returns a namespace of the
    mocks so a test can assert call counts (or their absence)."""
    ecs = MagicMock()
    ecs.delete_user_service = AsyncMock(return_value=_container_row(bedrock_gate_token_hash="hash-late"))
    ecs.wait_until_drained = AsyncMock(return_value=True)

    workspace_dir = tmp_path / ORG
    workspace_dir.mkdir()
    workspace = MagicMock()
    workspace.user_path = MagicMock(return_value=workspace_dir)
    workspace.delete_user_dir = MagicMock()

    m = SimpleNamespace(
        archive_member=AsyncMock(),
        delete_paperclip_user=AsyncMock(),
        user_repo=MagicMock(delete=AsyncMock(), get=AsyncMock(return_value={"user_id": MEMBER})),
        delete_company=AsyncMock(),
        container_repo=MagicMock(get_by_owner_id=AsyncMock(return_value=_container_row())),
        delete_gate_token_rows=AsyncMock(return_value=1),
        ecs=ecs,
        workspace=workspace,
        delete_replica_prefix=AsyncMock(return_value=True),
        archive_metronome=AsyncMock(),
        delete_owner_secrets=AsyncMock(return_value=["a", "b"]),
        delete_owner_agent_key_secrets=AsyncMock(return_value=2),
        integrations_repo=MagicMock(delete_all_for_owner=AsyncMock(return_value=["github"])),
        revoke_user_oauth=AsyncMock(),
        delete_stripe_customer=AsyncMock(return_value=True),
        wipe_company_stripe=AsyncMock(),
        billing_repo=MagicMock(
            delete=AsyncMock(),
            get_by_owner_id=AsyncMock(return_value=_billing_row()),
        ),
        messaging_repo=MagicMock(
            list_owner_room_ids=AsyncMock(return_value=["room-1"]),
            get_room=AsyncMock(return_value={"ownerId": ORG}),
            delete_room_partition=AsyncMock(return_value=3),
            delete_owner_partition=AsyncMock(return_value=4),
        ),
        usage_repo=MagicMock(delete_all_for_owner=AsyncMock(return_value=5)),
        update_repo=MagicMock(delete_all_for_owner=AsyncMock(return_value=1)),
        paperclip_repo=paperclip_repo,
        company_slug_repo=MagicMock(delete_for_owner=AsyncMock(return_value="target")),
        agent_email_repo=MagicMock(delete_all_for_owner=AsyncMock(return_value=["ceo@x"])),
        slack_installs_repo=MagicMock(delete_all_for_owner=AsyncMock(return_value=["T1"])),
        clerk_admin=MagicMock(
            get_organization=AsyncMock(return_value={"id": ORG, "name": "Target Workspace"}),
            list_organization_members=AsyncMock(return_value=[{"user_id": MEMBER, "email": f"{MEMBER}@example.com"}]),
            delete_organization=AsyncMock(),
            delete_user=AsyncMock(),
        ),
        admin_actions_repo=MagicMock(create=AsyncMock()),
    )

    monkeypatch.setattr(mod, "_archive_paperclip_member_call", m.archive_member)
    monkeypatch.setattr(mod, "delete_paperclip_user", m.delete_paperclip_user)
    monkeypatch.setattr(mod, "user_repo", m.user_repo)
    monkeypatch.setattr(mod, "_delete_paperclip_company_call", m.delete_company)
    monkeypatch.setattr(mod, "container_repo", m.container_repo)
    monkeypatch.setattr(mod, "delete_gate_token_rows", m.delete_gate_token_rows)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)
    monkeypatch.setattr(mod, "get_workspace", lambda: workspace)
    monkeypatch.setattr(mod, "delete_replica_prefix", m.delete_replica_prefix)
    monkeypatch.setattr(mod, "_archive_metronome_customer", m.archive_metronome)
    monkeypatch.setattr(mod, "delete_owner_secrets", m.delete_owner_secrets)
    monkeypatch.setattr(mod, "delete_owner_agent_key_secrets", m.delete_owner_agent_key_secrets)
    monkeypatch.setattr(mod, "integrations_repo", m.integrations_repo)
    monkeypatch.setattr(mod, "revoke_user_oauth", m.revoke_user_oauth)
    monkeypatch.setattr(mod, "delete_stripe_customer", m.delete_stripe_customer)
    monkeypatch.setattr(mod, "_wipe_company_stripe", m.wipe_company_stripe)
    monkeypatch.setattr(mod, "billing_repo", m.billing_repo)
    monkeypatch.setattr(mod, "messaging_repo", m.messaging_repo)
    monkeypatch.setattr(mod, "usage_repo", m.usage_repo)
    monkeypatch.setattr(mod, "update_repo", m.update_repo)
    monkeypatch.setattr(mod, "PaperclipRepo", MagicMock(return_value=paperclip_repo))
    monkeypatch.setattr(mod, "company_slug_repo", m.company_slug_repo)
    monkeypatch.setattr(mod, "agent_email_repo", m.agent_email_repo)
    monkeypatch.setattr(mod, "slack_installs_repo", m.slack_installs_repo)
    monkeypatch.setattr(mod, "clerk_admin", m.clerk_admin)
    monkeypatch.setattr(mod, "admin_actions_repo", m.admin_actions_repo)
    monkeypatch.setattr(mod.settings, "LITESTREAM_BUCKET", "lightbulb-dev-litestream")
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    return m


def _statuses(report, system: str) -> list[str]:
    return [s.status for s in report.steps if s.system == system]


# ---------------------------------------------------------------------------
# Refusals — every one of these must fire before a single step runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_non_org_owner_id():
    with pytest.raises(mod.Refusal, match="not a Clerk org id"):
        await mod.check_refusals(mod._Inventory("user_solo", None, []), allow_multi_member=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("org_id", [mod.FOUNDER_ORG_ID, mod.TEAM_ORG_ID])
async def test_refuses_protected_orgs(org_id):
    with pytest.raises(mod.Refusal, match="protected org"):
        await mod.check_refusals(mod._Inventory(org_id, None, []), allow_multi_member=False)


@pytest.mark.asyncio
async def test_refuses_multi_member_without_flag(paperclip_repo, monkeypatch):
    monkeypatch.setattr(mod, "PaperclipRepo", MagicMock(return_value=paperclip_repo))
    inv = _inventory(
        members=[{"user_id": MEMBER, "email": "a@x"}, {"user_id": OTHER_MEMBER, "email": "b@x"}],
    )
    with pytest.raises(mod.Refusal, match="has 2 members"):
        await mod.check_refusals(inv, allow_multi_member=False)
    # ...and the same org is accepted once the operator says so explicitly.
    paperclip_repo.get_user_ids_for_company.return_value = [MEMBER, OTHER_MEMBER]
    await mod.check_refusals(inv, allow_multi_member=True)


@pytest.mark.asyncio
async def test_refuses_multi_member_hidden_in_paperclip_rows(paperclip_repo, monkeypatch):
    """A Clerk roster read that comes back short (it returns [] on ANY error)
    must not be able to smuggle a second member past the guard."""
    monkeypatch.setattr(mod, "PaperclipRepo", MagicMock(return_value=paperclip_repo))
    inv = _inventory(members=[], paperclip_rows=[_paperclip_row(MEMBER), _paperclip_row(OTHER_MEMBER)])
    with pytest.raises(mod.Refusal, match="has 2 members"):
        await mod.check_refusals(inv, allow_multi_member=False)


@pytest.mark.asyncio
async def test_refuses_bootstrap_admin_member():
    bootstrap = sorted(mod.BOOTSTRAP_EMAILS)[0]
    inv = _inventory(members=[{"user_id": MEMBER, "email": bootstrap.upper()}])
    with pytest.raises(mod.Refusal, match="bootstrap Paperclip admin"):
        await mod.check_refusals(inv, allow_multi_member=False)


@pytest.mark.asyncio
async def test_refuses_company_shared_with_another_workspace(paperclip_repo, monkeypatch):
    monkeypatch.setattr(mod, "PaperclipRepo", MagicMock(return_value=paperclip_repo))
    paperclip_repo.get_user_ids_for_company.return_value = [MEMBER, "user_from_another_org"]
    with pytest.raises(mod.Refusal, match="two workspaces share"):
        await mod.check_refusals(_inventory(), allow_multi_member=False)


@pytest.mark.asyncio
async def test_main_exits_2_on_refusal_without_running_anything(wipe_mocks):
    assert await mod.main(["--org", mod.FOUNDER_ORG_ID, "--execute"]) == 2
    wipe_mocks.ecs.delete_user_service.assert_not_called()
    wipe_mocks.clerk_admin.delete_organization.assert_not_called()


@pytest.mark.asyncio
async def test_one_refusal_blocks_the_whole_run(wipe_mocks):
    """Validation covers every named org before any org's first step."""
    assert await mod.main(["--org", ORG, "--org", mod.FOUNDER_ORG_ID, "--execute"]) == 2
    wipe_mocks.ecs.delete_user_service.assert_not_called()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_deletes_nothing(wipe_mocks):
    report = await mod.wipe_org(_inventory(), execute=False, delete_clerk_users=True)

    assert report.dry_run is True
    assert {s.status for s in report.steps} <= {"would_delete", "skipped"}
    for name in (
        "archive_member",
        "delete_paperclip_user",
        "delete_company",
        "delete_gate_token_rows",
        "delete_replica_prefix",
        "archive_metronome",
        "delete_owner_secrets",
        "delete_owner_agent_key_secrets",
        "revoke_user_oauth",
        "delete_stripe_customer",
        "wipe_company_stripe",
    ):
        getattr(wipe_mocks, name).assert_not_called()
    wipe_mocks.ecs.delete_user_service.assert_not_called()
    wipe_mocks.workspace.delete_user_dir.assert_not_called()
    wipe_mocks.user_repo.delete.assert_not_called()
    wipe_mocks.billing_repo.delete.assert_not_called()
    wipe_mocks.paperclip_repo.delete.assert_not_called()
    wipe_mocks.messaging_repo.delete_owner_partition.assert_not_called()
    wipe_mocks.clerk_admin.delete_organization.assert_not_called()
    wipe_mocks.clerk_admin.delete_user.assert_not_called()


@pytest.mark.asyncio
async def test_main_default_is_preview(wipe_mocks):
    assert await mod.main(["--org", ORG]) == 0
    wipe_mocks.ecs.delete_user_service.assert_not_called()
    wipe_mocks.admin_actions_repo.create.assert_not_called()


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_ecs_org_calls_every_step_once(wipe_mocks):
    report = await mod.wipe_org(_inventory(), execute=True, delete_clerk_users=False)

    assert not [s for s in report.steps if s.status == "failed"], report.to_dict()
    wipe_mocks.archive_member.assert_awaited_once_with(MEMBER)
    wipe_mocks.delete_paperclip_user.assert_awaited_once()
    assert wipe_mocks.delete_paperclip_user.await_args.kwargs["paperclip_user_id"] == f"pc_{MEMBER}"
    wipe_mocks.user_repo.delete.assert_awaited_once_with(MEMBER)
    wipe_mocks.delete_company.assert_awaited_once_with(COMPANY)
    wipe_mocks.ecs.delete_user_service.assert_awaited_once_with(ORG, delete_hermes_profiles=True)
    wipe_mocks.workspace.delete_user_dir.assert_called_once_with(ORG)
    wipe_mocks.ecs.wait_until_drained.assert_awaited_once()
    wipe_mocks.delete_replica_prefix.assert_awaited_once_with(ORG)
    wipe_mocks.archive_metronome.assert_awaited_once_with("met_1")
    wipe_mocks.delete_owner_secrets.assert_awaited_once_with(ORG)
    wipe_mocks.delete_owner_agent_key_secrets.assert_awaited_once_with(ORG)
    wipe_mocks.integrations_repo.delete_all_for_owner.assert_awaited_once_with(ORG)
    assert wipe_mocks.revoke_user_oauth.await_count == 2  # provider row + harness row
    wipe_mocks.delete_stripe_customer.assert_awaited_once_with(ORG)
    wipe_mocks.wipe_company_stripe.assert_awaited_once()
    wipe_mocks.billing_repo.delete.assert_awaited_once_with(ORG)
    wipe_mocks.messaging_repo.delete_room_partition.assert_awaited_once_with("room-1")
    wipe_mocks.messaging_repo.delete_owner_partition.assert_awaited_once_with(ORG)
    wipe_mocks.usage_repo.delete_all_for_owner.assert_awaited_once_with(ORG)
    wipe_mocks.update_repo.delete_all_for_owner.assert_awaited_once_with(ORG)
    wipe_mocks.company_slug_repo.delete_for_owner.assert_awaited_once_with(ORG)
    wipe_mocks.agent_email_repo.delete_all_for_owner.assert_awaited_once_with(ORG)
    wipe_mocks.slack_installs_repo.delete_all_for_owner.assert_awaited_once_with(ORG)
    wipe_mocks.clerk_admin.delete_organization.assert_awaited_once_with(ORG)
    # The member's paperclip row + the two owner-keyed synthetic items.
    assert wipe_mocks.paperclip_repo.delete.await_count == 2
    wipe_mocks.paperclip_repo.delete_pending_brief.assert_awaited_once_with(ORG)


@pytest.mark.asyncio
async def test_execute_agentcore_org_uses_the_same_seam(wipe_mocks):
    """An AgentCore workspace has no ECS service and no EFS dir; the branch
    lives inside delete_user_service, so the script calls the same function
    with the terminal-caller flag and the report says which host it was."""
    wipe_mocks.workspace.user_path.return_value = wipe_mocks.workspace.user_path.return_value / "missing"
    inv = _inventory(
        container=_container_row(
            runtime_host="agentcore",
            service_name="",
            agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        )
    )
    report = await mod.wipe_org(inv, execute=True, delete_clerk_users=False)

    assert not [s for s in report.steps if s.status == "failed"], report.to_dict()
    wipe_mocks.ecs.delete_user_service.assert_awaited_once_with(ORG, delete_hermes_profiles=True)
    wipe_mocks.workspace.delete_user_dir.assert_not_called()
    assert _statuses(report, "efs_workspace_dir") == ["skipped"]
    assert "runtime_host=agentcore" in next(s for s in report.steps if s.system == "ecs_service").detail
    wipe_mocks.delete_replica_prefix.assert_awaited_once_with(ORG)
    wipe_mocks.clerk_admin.delete_organization.assert_awaited_once_with(ORG)


@pytest.mark.asyncio
async def test_step_failure_is_recorded_and_later_steps_still_run(wipe_mocks):
    wipe_mocks.delete_company.side_effect = RuntimeError("paperclip timed out")

    report = await mod.wipe_org(_inventory(), execute=True, delete_clerk_users=False)

    failed = [s for s in report.steps if s.status == "failed"]
    assert [s.system for s in failed] == ["paperclip_company"]
    assert "paperclip timed out" in failed[0].error
    # Everything after it still ran, including the always-last Clerk org delete.
    wipe_mocks.ecs.delete_user_service.assert_awaited_once()
    wipe_mocks.clerk_admin.delete_organization.assert_awaited_once_with(ORG)


@pytest.mark.asyncio
async def test_missing_paperclip_row_skips_the_company_step(wipe_mocks):
    """The two first prod targets: a member-less org with no Paperclip side at
    all. That is a clean skip, never a failure."""
    inv = _inventory(members=[], paperclip_rows=[], user_rows={})

    report = await mod.wipe_org(inv, execute=True, delete_clerk_users=False)

    assert _statuses(report, "paperclip_company") == ["skipped"]
    assert not [s for s in report.steps if s.status == "failed"]
    wipe_mocks.delete_company.assert_not_called()
    wipe_mocks.archive_member.assert_not_called()
    wipe_mocks.delete_paperclip_user.assert_not_called()
    # The org itself still goes.
    wipe_mocks.clerk_admin.delete_organization.assert_awaited_once_with(ORG)


@pytest.mark.asyncio
async def test_bare_seed_claim_row_has_no_company_to_delete(wipe_mocks):
    """company_id="" is a failed-seed marker, not a company."""
    inv = _inventory(paperclip_rows=[_paperclip_row(company_id="")])

    report = await mod.wipe_org(inv, execute=True, delete_clerk_users=False)

    assert _statuses(report, "paperclip_company") == ["skipped"]
    wipe_mocks.delete_company.assert_not_called()


@pytest.mark.asyncio
async def test_clerk_user_delete_is_gated_by_the_flag(wipe_mocks):
    report = await mod.wipe_org(_inventory(), execute=True, delete_clerk_users=False)
    assert _statuses(report, "clerk_user") == ["skipped"]
    wipe_mocks.clerk_admin.delete_user.assert_not_called()

    report = await mod.wipe_org(_inventory(), execute=True, delete_clerk_users=True)
    assert _statuses(report, "clerk_user") == ["ok"]
    wipe_mocks.clerk_admin.delete_user.assert_awaited_once_with(MEMBER)


@pytest.mark.asyncio
async def test_clerk_user_delete_runs_after_the_org_delete(wipe_mocks):
    report = await mod.wipe_org(_inventory(), execute=True, delete_clerk_users=True)
    systems = [s.system for s in report.steps]
    assert systems.index("clerk_org") < systems.index("clerk_user")
    assert systems[-1] == "clerk_user"
    # Gate tokens must go before the containers row that names them dies.
    assert systems.index("bedrock_gate_tokens") < systems.index("ecs_service")


@pytest.mark.asyncio
async def test_execute_writes_one_audit_row(wipe_mocks):
    assert await mod.main(["--org", ORG, "--execute"]) == 0
    wipe_mocks.admin_actions_repo.create.assert_awaited_once()
    kwargs = wipe_mocks.admin_actions_repo.create.await_args.kwargs
    assert kwargs["action"] == "wipe_org"
    assert kwargs["target_user_id"] == ORG
    assert kwargs["result"] == "success"


@pytest.mark.asyncio
async def test_main_exits_1_when_a_step_failed(wipe_mocks):
    wipe_mocks.delete_company.side_effect = RuntimeError("boom")
    assert await mod.main(["--org", ORG, "--execute"]) == 1
    assert wipe_mocks.admin_actions_repo.create.await_args.kwargs["result"] == "partial_failure"
