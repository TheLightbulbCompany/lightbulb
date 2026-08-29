import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def workflows_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-provisioning-workflows",
            KeySchema=[{"AttributeName": "workflow_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "workflow_id", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "state", "AttributeType": "S"},
                {"AttributeName": "sweep_key", "AttributeType": "S"},
                {"AttributeName": "updated_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-state-index",
                    "KeySchema": [
                        {"AttributeName": "owner_id", "KeyType": "HASH"},
                        {"AttributeName": "state", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "open-sweep-index",
                    "KeySchema": [
                        {"AttributeName": "sweep_key", "KeyType": "HASH"},
                        {"AttributeName": "updated_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


async def _new(owner_id="org_a"):
    from core.repositories import provisioning_workflow_repo as repo

    return await repo.create(
        owner_id=owner_id,
        action="create_resource",
        provider_id="prvdr_1",
        provider_name="posthog",
        service_ref="svc_analytics",
        service_id="svc_analytics",
        configuration={"region": "us"},
        issue_id="iss_1",
    )


@pytest.mark.asyncio
async def test_create_starts_in_ask_and_is_open(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    assert row["state"] == "ask"
    assert row["sweep_key"] == repo.OPEN_SWEEP_KEY
    assert row["owner_id"] == "org_a"
    assert row["provider_name"] == "posthog"
    assert row["configuration"] == {"region": "us"}
    assert row["workflow_id"].startswith("pwf_")
    assert row["created_at"] and row["updated_at"]


@pytest.mark.asyncio
async def test_transition_is_conditional_on_the_current_state(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    moved = await repo.transition(
        row["workflow_id"], from_state="ask", to_state="awaiting_approval", fields={"approval_id": "apr_1"}
    )
    assert moved["state"] == "awaiting_approval"
    assert moved["approval_id"] == "apr_1"
    # The same transition a second time loses the race and reports it.
    assert await repo.transition(row["workflow_id"], from_state="ask", to_state="awaiting_approval") is None


@pytest.mark.asyncio
async def test_terminal_transition_clears_the_sweep_key(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    done = await repo.transition(
        row["workflow_id"], from_state="ask", to_state="failed", fields={"error_code": "provider_failure"}
    )
    assert done["state"] == "failed"
    assert "sweep_key" not in done
    item = workflows_table.get_item(Key={"workflow_id": row["workflow_id"]})["Item"]
    assert "sweep_key" not in item
    assert await repo.list_open() == []


@pytest.mark.asyncio
async def test_list_open_returns_only_non_terminal_rows(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    a = await _new("org_a")
    b = await _new("org_b")
    await repo.transition(b["workflow_id"], from_state="ask", to_state="active")
    ids = [r["workflow_id"] for r in await repo.list_open()]
    assert ids == [a["workflow_id"]]


@pytest.mark.asyncio
async def test_list_for_owner_is_owner_scoped(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    await _new("org_a")
    await _new("org_a")
    await _new("org_b")
    assert len(await repo.list_for_owner("org_a")) == 2
    assert len(await repo.list_for_owner("org_b")) == 1
    assert await repo.list_for_owner("org_zzz") == []


@pytest.mark.asyncio
async def test_patch_refuses_to_move_state(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    ok = await repo.patch(row["workflow_id"], {"resource_id": "res_1", "attempts": 2})
    assert ok["resource_id"] == "res_1" and ok["attempts"] == 2 and ok["state"] == "ask"
    with pytest.raises(ValueError):
        await repo.patch(row["workflow_id"], {"state": "active"})
    with pytest.raises(ValueError):
        await repo.patch(row["workflow_id"], {"sweep_key": "open"})


@pytest.mark.asyncio
async def test_rejected_and_cancelled_are_terminal_and_leave_the_sweep(workflows_table):
    """The owner saying no must stop the driver as firmly as success does."""
    from core.repositories import provisioning_workflow_repo as repo

    for state in ("rejected", "cancelled"):
        row = await _new()
        done = await repo.transition(row["workflow_id"], from_state="ask", to_state=state)
        assert done["state"] == state
        assert "sweep_key" not in done
    assert await repo.list_open() == []
    assert {"rejected", "cancelled"} <= repo.TERMINAL_STATES


@pytest.mark.asyncio
async def test_stale_is_open_not_terminal(workflows_table):
    """A workflow the owner ignored keeps its ids and stays sweepable — the
    next click re-mints the link rather than starting over."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.transition(row["workflow_id"], from_state="ask", to_state="awaiting_user_action")
    stale = await repo.transition(row["workflow_id"], from_state="awaiting_user_action", to_state="stale")
    assert stale["state"] == "stale"
    assert stale["sweep_key"] == repo.OPEN_SWEEP_KEY
    assert [r["workflow_id"] for r in await repo.list_open()] == [row["workflow_id"]]


@pytest.mark.asyncio
async def test_transition_rejects_an_unknown_state(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    with pytest.raises(ValueError):
        await repo.transition(row["workflow_id"], from_state="ask", to_state="nonsense")


@pytest.mark.asyncio
async def test_claim_takes_an_unheld_row_and_renews_its_own_lease(workflows_table):
    """The per-row lease: two backend instances run during a rolling deploy
    (maxHealthyPercent: 200), so the driver claims before every step."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    held = await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    assert held["lease_owner"] == "host-a:1"
    assert held["lease_until"] > held["created_at"]
    # The holder may renew as often as it likes.
    again = await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    assert again["lease_owner"] == "host-a:1"


@pytest.mark.asyncio
async def test_claim_refuses_a_row_another_instance_holds(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    assert await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60) is not None
    assert await repo.claim(row["workflow_id"], owner="host-b:2", ttl_s=60) is None


@pytest.mark.asyncio
async def test_claim_takes_over_an_expired_lease(workflows_table):
    """The old instance died mid-step. Nothing else would ever move the row."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=-1)
    taken = await repo.claim(row["workflow_id"], owner="host-b:2", ttl_s=60)
    assert taken["lease_owner"] == "host-b:2"


@pytest.mark.asyncio
async def test_claim_reports_a_row_that_no_longer_exists(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    assert await repo.claim("pwf_gone", owner="host-a:1", ttl_s=60) is None


@pytest.mark.asyncio
async def test_release_clears_only_your_own_lease(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    # Someone else's release is a no-op, not a theft.
    await repo.release(row["workflow_id"], owner="host-b:2")
    assert (await repo.get(row["workflow_id"]))["lease_owner"] == "host-a:1"
    await repo.release(row["workflow_id"], owner="host-a:1")
    item = await repo.get(row["workflow_id"])
    assert "lease_owner" not in item and "lease_until" not in item
    # Releasing twice, or a row that vanished, is silent.
    await repo.release(row["workflow_id"], owner="host-a:1")
    await repo.release("pwf_gone", owner="host-a:1")


@pytest.mark.asyncio
async def test_terminal_transition_releases_the_lease(workflows_table):
    """Nothing drives a terminal row, so its lease must not outlive it."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    done = await repo.transition(row["workflow_id"], from_state="ask", to_state="active")
    assert "lease_owner" not in done and "lease_until" not in done
    assert "sweep_key" not in done


@pytest.mark.asyncio
async def test_the_lease_cannot_be_forged_through_patch_or_transition(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    with pytest.raises(ValueError):
        await repo.patch(row["workflow_id"], {"lease_owner": "host-b:2"})
    with pytest.raises(ValueError):
        await repo.transition(
            row["workflow_id"], from_state="ask", to_state="awaiting_approval", fields={"lease_until": "9999"}
        )


@pytest.mark.asyncio
async def test_list_open_follows_every_page(workflows_table, monkeypatch):
    """A truncated Query would strand whatever fell off the first page — and
    the sweep is the only thing that ever resumes those rows."""
    from core.repositories import provisioning_workflow_repo as repo

    monkeypatch.setattr(repo, "_PAGE_SIZE", 1)
    expected = {(await _new())["workflow_id"] for _ in range(3)}
    assert {r["workflow_id"] for r in await repo.list_open()} == expected
