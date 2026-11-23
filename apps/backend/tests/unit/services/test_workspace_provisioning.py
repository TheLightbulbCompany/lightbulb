"""Phase 1 task 8b.3: workspace provisioning and membership write our own stores; no Paperclip call."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import agent_registry, workspace_provisioning
from core.services.workspace_provisioning import STARTER_ROLE_KEYS, provision_org
from tests.unit.repositories.test_workspace_repo import create_companies_table
from tests.unit.super_agent.registry.conftest import _create_tasks_table

ORG = "org_1"


@pytest.fixture(autouse=True)
def ddb(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        _create_tasks_table(resource)
        create_companies_table(resource, dynamodb.table_name("paperclip-companies"))
        yield


def _repo() -> WorkspaceRepo:
    return WorkspaceRepo()


async def test_provision_org_native():
    await provision_org(ORG, provisioner="user_1")

    ws = await _repo().resolve_owner_row(ORG)
    assert ws is not None and ws.status == "active"
    assert "user_1" in ws.member_user_ids and ws.provisioner_user_id == "user_1"
    assert ws.company_id == "" and ws.onboarding_generation and ws.funnel_state == "not_started"
    roster = await agent_registry.roster(ORG)
    assert set(ws.starter_agent_ids) == set(STARTER_ROLE_KEYS)
    assert set(ws.starter_agent_ids.values()) == {a["id"] for a in roster}
    ceo = ws.starter_agent_ids["ceo"]
    assert all(a["reportsTo"] == (None if a["id"] == ceo else ceo) for a in roster)
    assert all(a["capabilities"] for a in roster)


async def test_provision_org_idempotent():
    first = await provision_org(ORG, provisioner="user_1")
    again = await provision_org(ORG, provisioner="user_2")
    assert again.starter_agent_ids == first.starter_agent_ids
    assert len(await agent_registry.roster(ORG)) == 6
    # A run whose META write was lost (or a concurrent run) replays the same starter rows.
    await _repo().delete(ORG)
    replay = await provision_org(ORG, provisioner="user_1")
    assert replay.starter_agent_ids == first.starter_agent_ids
    assert len(await agent_registry.roster(ORG)) == 6


async def test_active_row_with_a_missing_starter_is_reseeded():
    """The old ``starter_incomplete`` reconcile: an active row missing a starter gets it back."""
    first = await provision_org(ORG, provisioner="user_1")
    await _repo().remove_starter_agent_id(ORG, first.starter_agent_ids["growth"])
    await agent_registry.delete(ORG, first.starter_agent_ids["growth"])

    again = await provision_org(ORG, provisioner="user_1")
    assert set(again.starter_agent_ids) == set(STARTER_ROLE_KEYS)
    growth = again.starter_agent_ids["growth"]
    assert growth != first.starter_agent_ids["growth"]  # the deleted id stays reserved
    assert growth in {a["id"] for a in await agent_registry.roster(ORG)}
    assert (await _repo().resolve_owner_row(ORG)).starter_agent_ids == again.starter_agent_ids


async def test_seed_failure_marks_failed(monkeypatch):
    monkeypatch.setattr(agent_registry, "create", AsyncMock(side_effect=agent_registry.RegistryUnavailable("down")))
    with pytest.raises(agent_registry.RegistryUnavailable):
        await provision_org(ORG, provisioner="user_1")
    ws = await _repo().get_workspace(ORG)
    assert ws.status == "failed" and "down" in ws.last_error


async def test_disable_workspace_sets_purge_timer():
    await provision_org(ORG, provisioner="user_1")
    await workspace_provisioning.disable_workspace(ORG)
    ws = await _repo().get_workspace(ORG)
    assert ws.status == "disabled" and ws.scheduled_purge_at is not None


@pytest.fixture
def rooms(monkeypatch):
    from core.services import messaging

    fx = SimpleNamespace(default=AsyncMock(return_value="room_1"), proactive=AsyncMock())
    monkeypatch.setattr(messaging, "ensure_default_room", fx.default)
    return fx


async def test_default_room_after_native_seed(rooms):
    from core.services.user_provision import _ensure_owner_workspace

    await _ensure_owner_workspace(owner_id=ORG, owner_user_id="user_1")
    rooms.default.assert_awaited_once_with(ORG, "user_1")

    # Active now: a re-run makes no second room.
    await _ensure_owner_workspace(owner_id=ORG, owner_user_id="user_1")
    rooms.default.assert_awaited_once()


async def test_a_full_provision_run_seeds_the_voice_workspace_with_no_outbound_http(monkeypatch):
    """A new workspace's whole provision execution (``provision_runner.run``) on real stores:
    the META row goes active with the six-agent roster (the receptionist is the CEO UUID), and
    nothing reaches the network (no Paperclip, no per-owner runtime). The Hermes legs, Clerk and
    the room/mail helpers are stubbed at their own seams; any HTTP attempt fails the test."""
    import httpx

    from core.repositories import billing_repo, container_repo
    from core.services import clerk_admin, hermes_provisioning, messaging, onboarding_brief
    from scripts import provision_runner

    async def no_http(*_a, **_k):
        raise AssertionError("a provision run made an outbound HTTP call")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    for name in ("prepare_hermes_workspace", "provision_hermes_runtime", "reconcile_runtime"):
        monkeypatch.setattr(hermes_provisioning, name, AsyncMock())
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_1"}))
    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=[{"user_id": "user_1"}]))
    for name in ("ensure_default_room", "join_default_room"):
        monkeypatch.setattr(messaging, name, AsyncMock(return_value="room_1"))
    monkeypatch.setattr(onboarding_brief, "reconcile_pending_brief", AsyncMock())
    monkeypatch.setattr(billing_repo, "arm_trial_if_unset", AsyncMock())
    monkeypatch.setattr("core.services.user_provision._ensure_trial_row", AsyncMock())

    assert await provision_runner.run(ORG) == provision_runner.EXIT_OK

    ws = await _repo().resolve_owner_row(ORG)
    assert ws is not None and ws.status == "active" and ws.company_id == ""
    assert set(ws.starter_agent_ids) == set(STARTER_ROLE_KEYS)
    assert len(await agent_registry.roster(ORG)) == 6
    hermes_provisioning.provision_hermes_runtime.assert_awaited_once_with(ORG)
    # A rerun (the webhook redelivered, or the SFN retried) converges on the same roster.
    assert await provision_runner.run(ORG) == provision_runner.EXIT_OK
    assert (await _repo().resolve_owner_row(ORG)).starter_agent_ids == ws.starter_agent_ids
