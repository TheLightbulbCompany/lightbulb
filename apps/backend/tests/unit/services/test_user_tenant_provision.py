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
from core.config import settings
from core.repositories import (
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    messaging_repo,
    user_repo,
)
from core.services import (
    agent_email,
    memory_tags,
    provision_dispatch,
    user_provision,
    workspace_memory,
    workspace_provisioning,
)
from core.services.messaging import default_room_id
from core.services.provision_gate import evaluate_provision_gate
from main import app
from scripts import provision_runner
from tests.unit.auth.conftest import Tenant, calls, ddb, jwks  # noqa: F401
from tests.unit.services.test_account_wipe import wipe_mocks  # noqa: F401

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
    row = await billing_repo.get_by_owner_id(PERSON)
    assert row["subscription_status"] == "trial_local" and "owner_type" not in row

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


async def test_user_owner_provision_mints_no_slug_or_mailbox(ddb, calls, monkeypatch):  # noqa: F811
    """Step 0, seating, and the mailbox funnel, unmocked above the network: Clerk, SES and HTTP are the only
    fakes (`calls` closes the network; moto answers AWS). A person's tenant gets no company slug and no
    agent-mail address: mail for a person is a later task. An org's welcome is pinned in
    test_workspace_provisioning.py."""
    monkeypatch.setattr(settings, "CLERK_SECRET_KEY", "sk_test_not_real")  # Clerk answers 503: no org, no user
    assert settings.AGENT_EMAIL_DOMAIN  # mail is on: the guard, not a missing domain, keeps the mailbox away
    await user_repo.put(PERSON, f"{PERSON}@example.com")  # as the user.created webhook writes it
    await container_repo.mark_provisioning(PERSON)  # the dispatch's row
    await container_repo.ensure_owner_tokens(PERSON, hook_token="h", gateway_token="g")  # the runner's prepare leg
    assert await provision_runner._seed_owner_workspace(PERSON) == provision_runner.EXIT_OK
    await provision_runner._seat_workspace_rooms(PERSON)
    assert not [c for c in calls if c[0] == "aws" and c[1].startswith(("sesv2.", "ses."))]
    assert await company_slug_repo.get_by_owner(PERSON) is None
    assert await agent_email_repo.query_by_owner(PERSON) == []
    # An agent's send later reaches the same funnel.
    assert await agent_email.ensure_team_address(PERSON, f"{PERSON}@example.com") == (None, "disabled")
    assert await company_slug_repo.get_by_owner(PERSON) is None


async def test_user_created_writes_the_row_and_provisions_nothing(ddb, calls, sfn, jwks, monkeypatch):  # noqa: F811
    """Every Clerk sign-up fires user.created, Spark-only people included: it writes the users row and
    nothing else (no trial row, so no trial clock; no container row; no dispatch). The person's own first
    open still provisions them: GET /container/status creates the unarmed trial row, POST /container/provision
    dispatches."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    payload = {
        "type": "user.created",
        "data": {
            "id": PERSON,
            "email_addresses": [{"id": "ea_1", "email_address": f"{PERSON}@example.com"}],
            "primary_email_address_id": "ea_1",
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/webhooks/clerk", json=payload)
        assert r.status_code == 200, r.text[:300]
        assert (await user_repo.get(PERSON))["email"] == f"{PERSON}@example.com"
        assert await billing_repo.get_by_owner_id(PERSON) is None
        assert await container_repo.get_by_owner_id(PERSON) is None
        assert sfn == []

        r = await c.get("/api/v1/container/status", headers=Tenant(PERSON, None).headers)
        assert r.status_code == 200, r.text[:300]
        row = await billing_repo.get_by_owner_id(PERSON)
        assert row["subscription_status"] == "trial_local" and row.get("trial_end") is None  # unarmed
        r = await c.post("/api/v1/container/provision", headers=Tenant(PERSON, None).headers)
        assert r.status_code in (200, 202), r.text[:300]
    assert [s["name"].startswith(f"provision-{PERSON}-") for s in sfn] == [True]


async def _claim(c, user_id: str, slug: str, name: str = "Alice Studio"):
    headers = Tenant(user_id, None).headers
    await company_slug_repo.hold(slug=slug, user_id=user_id)
    return await c.post("/api/v1/company/slug/claim", json={"slug": slug, "name": name}, headers=headers)


async def test_slug_claim_sets_name(ddb, calls, jwks, no_clerk_orgs, monkeypatch):  # noqa: F811
    """A person's own claim is the one door that gives their tenant a slug, a name and a team address; the
    provision keeps the name. One slug per tenant, and nobody claims another person's hold.

    Rule before: claiming a slug required an org. Rule after: a person claims for their own tenant."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "dev.thelightbulb.company")
    await user_repo.put(PERSON, f"{PERSON}@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await _claim(c, PERSON, "alice-studio")
        assert r.status_code == 200, r.text[:300]
        assert (await _claim(c, PERSON, "alice-other")).json()["detail"] == "slug_already_claimed"
        await company_slug_repo.hold(slug="bob-studio", user_id="user_b")
        stolen = await c.post(  # C, with no slug of their own, names B's held slug
            "/api/v1/company/slug/claim",
            json={"slug": "bob-studio", "name": "Mine"},
            headers=Tenant("user_c", None).headers,
        )
        assert stolen.json()["detail"] == "slug_hold_lost"
    address = "agents@alice-studio.dev.thelightbulb.company"
    assert (await company_slug_repo.get_by_owner(PERSON))["slug"] == "alice-studio"
    row = await agent_email_repo.get_by_address(address)
    assert row["owner_id"] == PERSON and row["verified_senders"] == [f"{PERSON}@example.com"]
    assert await agent_email.ensure_team_address(PERSON, None) == (address, "ok")
    assert await provision_runner._seed_owner_workspace(PERSON) == provision_runner.EXIT_OK
    from core.services.workspace_name import workspace_name

    assert await workspace_name(PERSON) == "Alice Studio"
    assert (await company_slug_repo.get_by_slug("bob-studio"))["owner_id"] != "user_c"


async def test_source_adopted_after_provision(ddb, calls, jwks):  # noqa: F811
    """Where a person came from, answered after their tenant exists, is kept on the tenant (no expiry)."""
    from core.services import webhook_dedup

    headers = Tenant(PERSON, None).headers
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/container/status", headers=headers)).status_code == 200
        r = await c.post("/api/v1/company/source", json={"answer": "friend"}, headers=headers)
        assert r.status_code == 200, r.text[:300]
    item = await webhook_dedup._read_event(f"ws-source:{PERSON}")
    assert item["record"]["self_reported_source"] == "friend" and "ttl" not in item


async def test_user_deleted_keeps_an_org_bound_chatgpt_sign_in(ddb, wipe_mocks):  # noqa: F811
    """A member who connected ChatGPT for an org is deleted: that org's credential row stays (the org's own
    lifecycle owns it) and nothing of the org's is touched. A person's sign-in for their own tenant goes."""
    from unittest.mock import patch

    from core.services import account_wipe, oauth_service

    oauth_service._table().put_item(Item={"user_id": "user_m", "bound_owner_id": "org_x", "state": "active"})
    oauth_service._table().put_item(Item={"user_id": PERSON, "bound_owner_id": PERSON, "state": "active"})
    wipe_mocks.messaging_list_rooms.return_value = []
    with (
        patch.object(account_wipe, "bound_owner_of", oauth_service.bound_owner_of),
        patch.object(account_wipe, "revoke_user_oauth", oauth_service.revoke_user_oauth),
    ):
        for who in ("user_m", PERSON):
            await account_wipe.wipe_account(who, actor_admin_id="clerk:user.deleted", clerk_user_deleted=True)
    assert oauth_service._table().get_item(Key={"user_id": "user_m"})["Item"]["bound_owner_id"] == "org_x"
    assert "Item" not in oauth_service._table().get_item(Key={"user_id": PERSON})
    touched = {c.args[0] for m in (wipe_mocks.billing_delete, wipe_mocks.stripe_delete) for c in m.await_args_list}
    assert touched <= {"user_m", PERSON}  # no org's billing row or Stripe customer
