"""Phase 1 task 8b.3: workspace provisioning and membership write our own stores; no Paperclip call."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.repositories.paperclip_repo import PaperclipRepo, PaperclipWorkspace
from core.services import agent_registry, workspace_provisioning
from core.services.workspace_provisioning import STARTER_ROLE_KEYS, provision_member, provision_org
from tests.unit.repositories.test_paperclip_repo import create_companies_table
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


@pytest.fixture(autouse=True)
def no_paperclip(monkeypatch):
    from core.services.paperclip_admin_client import PaperclipAdminClient

    def boom(*a, **k):
        raise AssertionError("Paperclip called")

    monkeypatch.setattr(PaperclipAdminClient, "__init__", boom)


def _repo() -> PaperclipRepo:
    return PaperclipRepo(table_name="paperclip-companies")


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


async def test_old_failed_row_keeps_company_and_fields():
    """A workspace provisioned before this change keeps its company_id and onboarding fields."""
    now = datetime.now(timezone.utc)
    await _repo().put_workspace(
        PaperclipWorkspace(
            org_id=ORG,
            company_id="co_1",
            status="failed",
            created_at=now,
            updated_at=now,
            member_user_ids={"user_1"},
            starter_agent_ids={"ceo": "pc-ceo"},
            onboarding_generation="gen_1",
            funnel_state="submitted",
        )
    )
    ws = await provision_org(ORG, provisioner="user_2")
    stored = await _repo().resolve_owner_row(ORG)
    assert stored.company_id == "co_1" and stored.status == "active"
    assert stored.onboarding_generation == "gen_1" and stored.funnel_state == "submitted"
    assert stored.member_user_ids == {"user_1", "user_2"}
    assert ws.starter_agent_ids["ceo"] == "pc-ceo"
    assert {a["reportsTo"] for a in await agent_registry.roster(ORG)} == {"pc-ceo"}


async def test_seed_failure_marks_failed(monkeypatch):
    monkeypatch.setattr(agent_registry, "create", AsyncMock(side_effect=agent_registry.RegistryUnavailable("down")))
    with pytest.raises(agent_registry.RegistryUnavailable):
        await provision_org(ORG, provisioner="user_1")
    ws = await _repo().get_workspace(ORG)
    assert ws.status == "failed" and "down" in ws.last_error


async def test_member_join_and_leave_native():
    assert await provision_member(ORG, "user_2") is None  # no active workspace yet
    await provision_org(ORG, provisioner="user_1")
    await provision_member(ORG, "user_2")
    await provision_member(ORG, "user_2")
    assert (await _repo().resolve_owner_row(ORG)).member_user_ids == {"user_1", "user_2"}

    await _repo().remove_member(ORG, "user_2")
    assert (await _repo().resolve_owner_row(ORG)).member_user_ids == {"user_1"}


async def test_disable_workspace_sets_purge_timer():
    await provision_org(ORG, provisioner="user_1")
    await workspace_provisioning.disable_workspace(ORG)
    ws = await _repo().get_workspace(ORG)
    assert ws.status == "disabled" and ws.scheduled_purge_at is not None


@pytest.fixture
def rooms(monkeypatch):
    from core.services import messaging, welcome_email

    fx = SimpleNamespace(
        default=AsyncMock(return_value="room_1"), feed=AsyncMock(), welcome=AsyncMock(), proactive=AsyncMock()
    )
    monkeypatch.setattr(messaging, "ensure_default_room", fx.default)
    monkeypatch.setattr(messaging, "ensure_feed_room", fx.feed)
    monkeypatch.setattr(welcome_email, "send_owner_welcome", fx.welcome)
    return fx


async def test_default_room_and_welcome_after_native_seed(rooms):
    from core.services.user_provision import _ensure_workspace_for_org

    await _ensure_workspace_for_org(org_id=ORG, user_id="user_1", is_admin=True)
    rooms.default.assert_awaited_once_with(ORG, "user_1")
    rooms.feed.assert_awaited_once_with(ORG, "user_1")
    rooms.welcome.assert_awaited_once_with(ORG, "user_1")

    # Active now: a re-run joins (no-op) and makes no second room or welcome.
    await _ensure_workspace_for_org(org_id=ORG, user_id="user_1", is_admin=True)
    rooms.default.assert_awaited_once()
    rooms.welcome.assert_awaited_once()


async def test_member_never_provisions(rooms):
    from core.services.user_provision import _ensure_workspace_for_org

    await _ensure_workspace_for_org(org_id=ORG, user_id="user_2", is_admin=False)
    assert await _repo().get_workspace(ORG) is None
    rooms.default.assert_not_awaited()


async def test_provision_runner_step0_needs_no_paperclip(rooms, monkeypatch):
    from core.services import clerk_admin, proactive
    from scripts import provision_runner

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_1"}))
    monkeypatch.setattr(proactive, "ensure", rooms.proactive)

    assert await provision_runner._seed_owner_workspace(ORG) == provision_runner.EXIT_OK
    ws = await _repo().resolve_owner_row(ORG)
    assert ws.provisioner_user_id == "user_1" and set(ws.starter_agent_ids) == set(STARTER_ROLE_KEYS)
    rooms.proactive.assert_awaited_once_with(ORG)

    # A store outage is transient: the state machine retries it.
    await _repo().delete(ORG)
    monkeypatch.setattr(agent_registry, "create", AsyncMock(side_effect=agent_registry.RegistryUnavailable("down")))
    assert await provision_runner._seed_owner_workspace(ORG) == provision_runner.EXIT_TRANSIENT


async def test_teams_context_for_native_member_mints_no_session():
    """A workspace with no Paperclip company or identity still resolves the /teams context."""
    from core.auth import AuthContext
    from routers.teams.deps import resolve_teams_context

    await provision_org(ORG, provisioner="user_1")
    factory = AsyncMock(side_effect=AssertionError("session minted"))
    ctx = await resolve_teams_context(
        auth=AuthContext(user_id="user_1", org_id=ORG, org_role="org:admin"),
        repo=_repo(),
        session_factory=factory,
        cookies={},
        response=SimpleNamespace(set_cookie=lambda *a, **k: None),
    )
    assert ctx.owner_id == ORG and ctx.company_id == "" and ctx.session_cookie == "" and ctx.paperclip_user_id == ""
