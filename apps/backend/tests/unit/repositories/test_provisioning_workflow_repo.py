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
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


async def _new(owner_id="org_a", **kwargs):
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
        **kwargs,
    )


@pytest.mark.asyncio
async def test_create_starts_in_ask_and_is_open(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    assert row["state"] == "ask"
    assert row["state"] not in repo.TERMINAL_STATES
    assert row["owner_id"] == "org_a"
    assert row["provider_name"] == "posthog"
    assert row["configuration"] == {"region": "us"}
    assert row["workflow_id"].startswith("pwf_")
    assert row["created_at"] and row["updated_at"]


@pytest.mark.asyncio
async def test_create_persists_the_asking_agents_reason(workflows_table):
    """The row is the memory: a re-filed card after a catalog drift still has
    to say why, and the reason is not recoverable from the catalog."""
    row = await _new(reason="The launch needs funnel numbers.")
    assert row["reason"] == "The launch needs funnel numbers."
    # Absent, it is stored empty rather than missing — no caller has to guard.
    assert (await _new())["reason"] == ""


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


@pytest.mark.asyncio
async def test_rejected_and_cancelled_are_terminal(workflows_table):
    """The owner saying no must stop the workflow as firmly as success does —
    a terminal row is never stepped again on a read."""
    from core.repositories import provisioning_workflow_repo as repo

    for state in ("rejected", "cancelled"):
        row = await _new()
        done = await repo.transition(row["workflow_id"], from_state="ask", to_state=state)
        assert done["state"] == state
    assert {"rejected", "cancelled"} <= repo.TERMINAL_STATES


@pytest.mark.asyncio
async def test_stale_is_open_not_terminal(workflows_table):
    """A workflow the owner ignored keeps its ids and keeps being stepped on
    the next read — the next click re-mints the link rather than starting over."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.transition(row["workflow_id"], from_state="ask", to_state="awaiting_user_action")
    stale = await repo.transition(row["workflow_id"], from_state="awaiting_user_action", to_state="stale")
    assert stale["state"] == "stale"
    assert stale["state"] not in repo.TERMINAL_STATES


@pytest.mark.asyncio
async def test_transition_rejects_an_unknown_state(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    with pytest.raises(ValueError):
        await repo.transition(row["workflow_id"], from_state="ask", to_state="nonsense")


@pytest.mark.asyncio
async def test_claim_takes_an_unheld_row_and_is_not_renewable(workflows_table):
    """The per-row claim: two readers can arrive at once, so a reader claims
    before every step.

    NOT renewable, even by the holder. The backend runs `desiredCount: 1`, so
    every reader in the process shares one owner string — a renewal clause
    would hand the row to the SECOND concurrent read of the same process and
    both would create the resource.
    """
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    held = await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    assert held["lease_owner"] == "host-a:1"
    assert held["lease_until"] > held["created_at"]
    assert await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60) is None


@pytest.mark.asyncio
async def test_claim_refuses_a_row_another_instance_holds(workflows_table):
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    assert await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60) is not None
    assert await repo.claim(row["workflow_id"], owner="host-b:2", ttl_s=60) is None


@pytest.mark.asyncio
async def test_two_reads_in_one_process_cannot_both_step_a_row(workflows_table):
    """The double-create guard, end to end: same owner string, two reads. The
    second is refused until the first hands the row back."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    assert await repo.claim(row["workflow_id"], owner="task-1:42", ttl_s=300) is not None
    assert await repo.claim(row["workflow_id"], owner="task-1:42", ttl_s=300) is None
    await repo.release(row["workflow_id"], owner="task-1:42")
    assert await repo.claim(row["workflow_id"], owner="task-1:42", ttl_s=300) is not None


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
    """Nothing steps a terminal row, so its claim must not outlive it."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    await repo.claim(row["workflow_id"], owner="host-a:1", ttl_s=60)
    done = await repo.transition(row["workflow_id"], from_state="ask", to_state="active")
    assert "lease_owner" not in done and "lease_until" not in done


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
async def test_floats_in_catalog_values_are_written_not_refused(workflows_table):
    """Live 2026-10-29: Vercel's catalog snapshot carried a float and the
    transition raised "Float types are not supported", leaving the row in `ask`."""
    from core.repositories import provisioning_workflow_repo as repo

    row = await _new()
    moved = await repo.transition(
        row["workflow_id"],
        from_state="ask",
        to_state="awaiting_approval",
        fields={"approval": {"paid_pricing": [{"amount": 1.25}], "schema": {"minimum": 0.1}}},
    )
    assert moved is not None and moved["state"] == "awaiting_approval"
    patched = await repo.patch(row["workflow_id"], {"usage_limit": {"max_amount": 12.5}})
    assert str(patched["usage_limit"]["max_amount"]) == "12.5"


@pytest.mark.asyncio
async def test_create_records_the_app_and_environment(workflows_table):
    """An app's rows say which app and which environment; a company row is
    `app_id=None`, `prod` — the shape every existing row reads as."""
    company = await _new()
    assert company["app_id"] is None and company["environment"] == "prod"
    app_row = await _new(app_id="app_1", environment="dev")
    assert app_row["app_id"] == "app_1" and app_row["environment"] == "dev"
