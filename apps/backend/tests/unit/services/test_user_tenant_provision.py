"""A person with no organization is a tenant: provisioned exactly as an org is, with the person as provisioner.

Rule before: a `user_` owner got no trial row, no dispatch, a "choose a workspace" gate.
Rule after: a `user_` owner gets the trial row, the dispatch, step 0 with itself as provisioner, `#general`, no gate.
Moto DynamoDB; Step Functions, Clerk and the network are faked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext
from core.repositories import billing_repo, messaging_repo
from core.services import memory_tags, provision_dispatch, user_provision, workspace_memory, workspace_provisioning
from core.services.messaging import default_room_id
from core.services.provision_gate import evaluate_provision_gate
from main import app
from scripts import provision_runner
from tests.unit.auth.conftest import Tenant, calls, ddb, jwks  # noqa: F401

PERSON = "user_x"


def _session(user_id: str) -> AuthContext:
    return AuthContext(user_id=user_id, human_session=True)


@pytest.fixture
def no_clerk_orgs(monkeypatch):
    """A person has no organization: Clerk answers no org (404) and lists no members."""
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.welcome_email.send_owner_welcome", AsyncMock())


@pytest.fixture
def sfn(monkeypatch):
    started = []
    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: [{"executions": []}]),
        start_execution=lambda **kw: started.append(kw),
    )
    monkeypatch.setattr(provision_dispatch, "_client", lambda: client)
    return started


async def test_user_owner_full_provision(ddb, calls, sfn, no_clerk_orgs):  # noqa: F811
    await user_provision.ensure_workspace_trial_default(_session(PERSON), PERSON)
    assert (await billing_repo.get_by_owner_id(PERSON))["subscription_status"] == "trial_local"

    assert await provision_dispatch.dispatch_provision(PERSON) is True
    assert [s["name"].startswith(f"provision-{PERSON}-") for s in sfn] == [True]
    assert '"owner_id": "user_x"' in sfn[0]["input"]

    assert await provision_runner._seed_owner_workspace(PERSON) == provision_runner.EXIT_OK
    await provision_runner._seat_workspace_rooms(PERSON)
    ws = await workspace_provisioning._repo().get_workspace(PERSON, consistent_read=True)
    assert ws.status == "active" and ws.member_user_ids == {PERSON}
    assert await messaging_repo.get_member(default_room_id(PERSON), "human", PERSON) is not None

    assert await evaluate_provision_gate(owner_id=PERSON, is_admin=True) is None


async def test_user_owner_container_status_creates_trial(ddb, calls, jwks):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/container/status", headers=Tenant(PERSON, None).headers)
    assert r.status_code == 200, r.text[:300]
    assert (await billing_repo.get_by_owner_id(PERSON))["subscription_status"] == "trial_local"


async def test_trial_door_refuses_other_tenant(ddb):  # noqa: F811
    """B's session cannot create (or touch) A's trial row by naming A's tenant."""
    with pytest.raises(HTTPException) as exc:
        await user_provision.ensure_workspace_trial_default(_session("user_b"), "user_a")
    assert exc.value.status_code == 403
    assert await billing_repo.get_by_owner_id("user_a") is None


def test_private_memory_tenant_is_its_person():
    """In a user tenant the tenant id and the person are one id: the person's private lane is theirs alone,
    distinct from the shared lane and from any other person's private lane, in this tenant or another."""
    block = memory_tags.block_for_router("private", PERSON, None)
    assert block["member"] == PERSON
    mine = workspace_memory.namespaces_for(PERSON, block["member"])
    assert mine["private"] == f"/owners/{PERSON}:{PERSON}/"
    assert mine["private"] != mine["facts"] and not mine["private"].startswith(mine["facts"])
    others = (workspace_memory.namespaces_for(PERSON, "user_y"), workspace_memory.namespaces_for("user_y", PERSON))
    assert all(mine["private"] != o["private"] for o in others)
