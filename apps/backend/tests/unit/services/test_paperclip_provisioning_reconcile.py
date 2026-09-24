"""Stored-state reconciliation tests for Paperclip starter rosters."""

from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipApiError
from tests.unit.services.test_paperclip_provisioning import (
    _StatefulRepo,
    _build_provision_org_doubles,
    _identity,
    _ws,
)
from tests.unit.services.test_paperclip_provisioning_hardening import EXPECTED_ROSTER


def _flaky_engineering(attempts: Counter, exc_factory):
    async def create_agent(**kwargs):
        key = kwargs["idempotency_key"].removeprefix("seed:starter:").removesuffix(":v1")
        attempts[key] += 1
        if key == "engineering" and attempts[key] == 1:
            raise exc_factory()
        return {"id": f"agent-{key}"}

    return create_agent


@pytest.mark.asyncio
async def test_normal_reconciliation_fills_only_the_missing_starter(monkeypatch):
    """The persisted role map drives retry; display names are irrelevant. The
    retry adopts the provisioner's identity with the email Better Auth holds
    (not the renamed live one) and does not re-join an already-joined
    provisioner."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    repo = _StatefulRepo()
    attempts: Counter[str] = Counter()
    admin.create_agent = AsyncMock(
        side_effect=_flaky_engineering(attempts, lambda: PaperclipApiError("transient", status_code=500, body="x"))
    )
    service = PaperclipProvisioning(admin, repo, "dev")

    first = await service.provision_org(owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com")
    assert first.status == "failed"
    assert first.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != "engineering"}
    admin.list_agents.assert_not_awaited()
    first_generation = first.onboarding_generation
    assert first_generation

    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_reconcile"})
    second = await service.provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="renamed-owner@example.com"
    )

    assert second.status == "active"
    assert second.seeded_agent_ids is None
    assert second.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER}
    assert attempts == Counter({"engineering": 2, **{key: 1 for key, *_ in EXPECTED_ROSTER if key != "engineering"}})
    engineering_calls = [call for call in admin.create_agent.await_args_list if call.kwargs["name"] == "Engineering"]
    assert [call.kwargs["idempotency_key"] for call in engineering_calls] == [
        "seed:starter:engineering:v1",
        "seed:starter:engineering:v1",
    ]
    assert engineering_calls[-1].kwargs["reports_to"] == "agent-ceo"
    admin.create_company.assert_awaited_once()
    admin.sign_up_user.assert_awaited_once()
    assert admin.sign_in_user.await_args.kwargs["email"] == "owner@example.com"
    admin.create_invite.assert_awaited_once()  # the provisioner already joined on attempt one
    admin.list_agents.assert_awaited_once()
    # Reconcile is a retry of the SAME onboarding attempt.
    assert second.onboarding_generation == first_generation
    assert repo.workspaces["org_xyz"].member_user_ids == {"user_owner"}


@pytest.mark.asyncio
async def test_active_legacy_seed_row_short_circuits_unchanged(monkeypatch):
    """Legacy Friday/Atlas state is not migrated by name or by provisioning."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    legacy = _ws(
        company_id="co_legacy",
        members={"user_owner"},
        provisioner="user_owner",
        seeded_agent_ids=["renamed-friday-id", "renamed-atlas-id"],
    )
    repo.get_workspace = AsyncMock(return_value=legacy)

    result = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com"
    )

    assert result is legacy
    assert result.starter_agent_ids is None
    admin.create_agent.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_legacy_row_reconciles_to_starters_and_carries_fields_forward(monkeypatch):
    """A non-active workspace carrying a company is reconciled with a
    whole-item put that carries ``seeded_agent_ids`` and every onboarding
    field forward (it must not mint a new generation or reset the funnel)."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    failed_legacy = _ws(
        company_id="co_legacy",
        status="failed",
        members={"user_owner", "user_teammate"},
        provisioner="user_owner",
        seeded_agent_ids=["legacy-friday-id", "legacy-atlas-id"],
        onboarding_generation="gen_1",
        funnel_state="submitted",
        onboarding_goal_id="goal_1",
        onboarding_brief_text="build a thing",
        first_contact_state="sent",
    )
    repo.get_workspace = AsyncMock(return_value=failed_legacy)
    repo.get_identity = AsyncMock(
        return_value=_identity("user_owner", paperclip_user_id="pc_legacy", password="owner-password-plain")
    )
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_legacy"}, "_session_cookie": "cookie_reconcile"})

    result = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com"
    )

    assert result.status == "active"
    assert set(result.starter_agent_ids) == {key for key, *_ in EXPECTED_ROSTER}
    persisted = repo.put_workspace.await_args
    assert persisted.kwargs == {}  # reconcile put is unconditional
    row = persisted.args[0]
    assert row.company_id == "co_legacy"
    assert row.seeded_agent_ids == ["legacy-friday-id", "legacy-atlas-id"]
    assert row.onboarding_generation == "gen_1"
    assert row.funnel_state == "submitted"
    assert row.onboarding_goal_id == "goal_1"
    assert row.onboarding_brief_text == "build a thing"
    assert row.first_contact_state == "sent"
    assert row.member_user_ids == {"user_owner", "user_teammate"}
    admin.create_company.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    repo.try_acquire_seed_claim.assert_not_awaited()  # confirmed identity needs no fence
    admin.create_invite.assert_not_awaited()  # the provisioner already joined


@pytest.mark.parametrize("accept_error", [None, PaperclipApiError("already belongs", status_code=409, body="x")])
@pytest.mark.asyncio
async def test_different_admin_reconciling_failed_company_is_invited_and_accepted(monkeypatch, accept_error):
    """A DIFFERENT admin than the original provisioner reconciles a failed
    company: they are invited + accept into THAT company (a 409 "already
    belongs" is the join already done), recorded as a member, while the
    original provisioner is kept."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_second"}, "_session_cookie": "cookie_second"})
    if accept_error is not None:
        admin.accept_invite = AsyncMock(side_effect=accept_error)
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(
        status="failed",
        members={"user_first"},
        provisioner="user_first",
        starter_agent_ids={"ceo": "agent-ceo"},
        onboarding_generation="gen_1",
    )

    result = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz", owner_user_id="user_second", owner_email="second@example.com"
    )

    admin.create_company.assert_not_awaited()
    invite = admin.create_invite.await_args.kwargs
    assert invite["company_id"] == "co_xyz"
    assert invite["email"] == "second@example.com"
    assert invite["human_role"] == "owner"
    assert admin.accept_invite.await_args.kwargs["session_cookie"] == "cookie_second"
    assert result.status == "active"
    stored = repo.workspaces["org_xyz"]
    assert stored.member_user_ids == {"user_first", "user_second"}
    assert stored.provisioner_user_id == "user_first"
    assert stored.onboarding_generation == "gen_1"
    assert stored.starter_agent_ids["ceo"] == "agent-ceo"
    assert repo.identities["user_second"].paperclip_user_id == "pc_second"


@pytest.mark.asyncio
async def test_reconcile_join_non_409_failure_marks_workspace_failed(monkeypatch):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(side_effect=PaperclipApiError("down", status_code=503, body="x"))
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(status="failed", members={"user_first"}, provisioner="user_first")

    with pytest.raises(PaperclipApiError):
        await PaperclipProvisioning(admin, repo, "dev").provision_org(
            owner_id="org_xyz", owner_user_id="user_second", owner_email="second@example.com"
        )

    assert repo.workspaces["org_xyz"].status == "failed"
    assert repo.workspaces["org_xyz"].member_user_ids == {"user_first"}
    admin.create_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_failure_persists_progress_and_reconciles_same_company(monkeypatch):
    """A timed-out create follows the ordinary missing-starter retry path."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    repo = _StatefulRepo()
    attempts: Counter[str] = Counter()

    def _timeout():
        request = httpx.Request(
            "POST",
            "https://credential.example.test/agents",
            headers={"Authorization": "Bearer secret-token"},
        )
        return httpx.ReadTimeout("read failed with secret-token", request=request)

    admin.create_agent = AsyncMock(side_effect=_flaky_engineering(attempts, _timeout))
    service = PaperclipProvisioning(admin, repo, "dev")

    first = await service.provision_org(owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com")

    assert first.status == "failed"
    assert first.company_id == "co_xyz"
    assert first.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != "engineering"}
    assert first.last_error == "provision_org: missing required starter agents: engineering"
    assert "secret" not in first.last_error
    admin.list_agents.assert_not_awaited()

    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_reconcile"})
    second = await service.provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com"
    )

    assert second.status == "active"
    assert second.company_id == "co_xyz"
    assert second.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER}
    assert attempts == Counter(
        {
            "engineering": 2,
            **{key: 1 for key, *_ in EXPECTED_ROSTER if key != "engineering"},
        }
    )
    admin.create_company.assert_awaited_once()
    admin.list_agents.assert_awaited_once()
