"""The provision lease: single-flight claim, fencing token, takeover, release.

This is the state machine that closes the double-provision incident: two ~3s
status polls (a page refresh mid-provision) both dispatched a full provision;
the two runs interleaved, minted duplicate Bedrock inference profiles, and
split them across the container config and the IAM policy — AccessDenied on
every model call. The lease makes the container row's creation itself the
gate, and the fencing token stops a superseded run before its next side
effect.

Exercised against a REAL DynamoDB engine (moto), not a hand mock: every
primitive here is a conditional write, and a mock would happily pass
expressions DynamoDB rejects.
"""

import os
import time
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _row(table, owner_id):
    return table.get_item(Key={"owner_id": owner_id}).get("Item")


# ---------------------------------------------------------------------------
# try_claim_provision — the single-flight gate


@pytest.mark.asyncio
async def test_fresh_owner_claim_wins_and_creates_the_provisioning_row(dynamodb_table):
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_new")

    assert claim is not None
    row = _row(dynamodb_table, "user_new")
    assert row["status"] == "provisioning"
    assert row["provision_claim_id"] == claim
    assert row["provision_claimed_at"] > 0
    assert row["id"] and row["created_at"]


@pytest.mark.asyncio
async def test_second_concurrent_claim_loses(dynamodb_table):
    """THE INCIDENT: two polls both decide to provision. Exactly one wins."""
    from core.repositories import container_repo

    first = await container_repo.try_claim_provision("user_race")
    second = await container_repo.try_claim_provision("user_race")

    assert first is not None
    assert second is None
    # The loser did not disturb the winner's lease.
    assert _row(dynamodb_table, "user_race")["provision_claim_id"] == first


@pytest.mark.asyncio
@pytest.mark.parametrize("claimable_status", ["error", "stopped"])
async def test_error_and_stopped_rows_are_claimable(dynamodb_table, claimable_status):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_x", "status": claimable_status})
    assert await container_repo.try_claim_provision("user_x") is not None
    assert _row(dynamodb_table, "user_x")["status"] == "provisioning"


@pytest.mark.asyncio
async def test_running_row_is_never_claimable(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_run", "status": "running"})
    assert await container_repo.try_claim_provision("user_run") is None


@pytest.mark.asyncio
async def test_stale_provisioning_lease_is_reclaimable_fresh_is_not(dynamodb_table):
    """A hard-killed provision leaves the row provisioning with a lease that
    stops being refreshed; after PROVISION_CLAIM_STALE_S the next poll may
    re-claim it. A FRESH lease (live provision) may not be stolen."""
    from core.repositories import container_repo

    stale_at = int(time.time()) - container_repo.PROVISION_CLAIM_STALE_S - 5
    dynamodb_table.put_item(
        Item={
            "owner_id": "user_dead",
            "status": "provisioning",
            "provision_claim_id": "old",
            "provision_claimed_at": stale_at,
        }
    )
    reclaim = await container_repo.try_claim_provision("user_dead")
    assert reclaim is not None and reclaim != "old"

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_live",
            "status": "provisioning",
            "provision_claim_id": "live",
            "provision_claimed_at": int(time.time()),
        }
    )
    assert await container_repo.try_claim_provision("user_live") is None


@pytest.mark.asyncio
async def test_legacy_provisioning_row_without_lease_is_claimable(dynamodb_table):
    """Rows wedged in 'provisioning' from before the lease existed carry no
    provision_claimed_at — they must be recoverable, not stuck forever."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_legacy", "status": "provisioning"})
    assert await container_repo.try_claim_provision("user_legacy") is not None


# ---------------------------------------------------------------------------
# takeover_provision — the deliberate doors


@pytest.mark.asyncio
async def test_takeover_always_wins_and_preserves_a_running_status(dynamodb_table):
    """Conversion//recover/admin must never be blocked — and taking over a
    RUNNING container's lease must not flip its visible status into the
    provisioning stepper before the reprovision decides what to write."""
    from core.repositories import container_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_paid",
            "status": "running",
            "provision_claim_id": "poll-run",
            "provision_claimed_at": int(time.time()),
        }
    )
    new_claim = await container_repo.takeover_provision("user_paid")

    row = _row(dynamodb_table, "user_paid")
    assert row["provision_claim_id"] == new_claim != "poll-run"
    assert row["status"] == "running"  # preserved


@pytest.mark.asyncio
async def test_takeover_on_absent_row_creates_it_as_provisioning(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.takeover_provision("user_fresh_force")
    assert _row(dynamodb_table, "user_fresh_force")["status"] == "provisioning"


# ---------------------------------------------------------------------------
# update_container_leased — the fencing checkpoints


@pytest.mark.asyncio
async def test_leased_update_refreshes_stamp_and_superseded_run_is_stopped(dynamodb_table):
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    claim = await container_repo.try_claim_provision("user_cp")
    before = _row(dynamodb_table, "user_cp")["provision_claimed_at"]

    time.sleep(1)
    await container_repo.update_container_leased("user_cp", claim, {"substatus": "prepared"})
    row = _row(dynamodb_table, "user_cp")
    assert row["substatus"] == "prepared"
    assert row["provision_claimed_at"] > before

    # A deliberate door takes over -> the old run's next checkpoint raises.
    await container_repo.takeover_provision("user_cp")
    with pytest.raises(ProvisionSuperseded):
        await container_repo.update_container_leased("user_cp", claim, {"substatus": "poison"})
    assert _row(dynamodb_table, "user_cp")["substatus"] == "prepared"  # zombie wrote nothing


@pytest.mark.asyncio
async def test_leased_update_with_no_fields_is_a_pure_refresh(dynamodb_table):
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_ref")
    before = _row(dynamodb_table, "user_ref")["provision_claimed_at"]
    time.sleep(1)
    await container_repo.update_container_leased("user_ref", claim)
    assert _row(dynamodb_table, "user_ref")["provision_claimed_at"] > before


# ---------------------------------------------------------------------------
# release_provision_to_error — failure self-healing


@pytest.mark.asyncio
async def test_release_flips_provisioning_to_error_making_it_reclaimable(dynamodb_table):
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_fail")
    await container_repo.release_provision_to_error("user_fail", claim)

    row = _row(dynamodb_table, "user_fail")
    assert row["status"] == "error"
    assert row["substatus"] == "provision_failed"
    # ...and the next poll's dispatch can claim again immediately.
    assert await container_repo.try_claim_provision("user_fail") is not None


@pytest.mark.asyncio
async def test_release_noops_when_lease_was_taken_over(dynamodb_table):
    """The failing zombie must not clobber the superseding run's row."""
    from core.repositories import container_repo

    old = await container_repo.try_claim_provision("user_z")
    await container_repo.takeover_provision("user_z")
    await container_repo.release_provision_to_error("user_z", old)
    assert _row(dynamodb_table, "user_z")["status"] == "provisioning"  # untouched


@pytest.mark.asyncio
async def test_release_noops_on_a_running_row(dynamodb_table):
    """A failed DELIBERATE redeploy of a still-running container must leave
    the row 'running' — the container IS running; an error write would send
    the status poll into a needless reprovision loop."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_ok", "status": "running"})
    claim = await container_repo.takeover_provision("user_ok")
    await container_repo.release_provision_to_error("user_ok", claim)
    assert _row(dynamodb_table, "user_ok")["status"] == "running"


# ---------------------------------------------------------------------------
# upsert lease preservation + staleness helper


@pytest.mark.asyncio
async def test_upsert_preserves_the_lease_mid_provision(dynamodb_table):
    """create_user_service whole-item-upserts the row MID-provision; dropping
    the lease there would make the live provision look unowned/stale and
    re-claimable while still running."""
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_up")
    await container_repo.upsert("user_up", {"status": "provisioning", "service_name": "svc-1"})

    row = _row(dynamodb_table, "user_up")
    assert row["provision_claim_id"] == claim
    assert row["provision_claimed_at"] > 0
    assert row["service_name"] == "svc-1"


def test_is_provision_claim_stale_matrix():
    from core.repositories import container_repo

    now = int(time.time())
    stale = now - container_repo.PROVISION_CLAIM_STALE_S - 1
    assert container_repo.is_provision_claim_stale(None) is False
    assert container_repo.is_provision_claim_stale({"status": "running", "provision_claimed_at": stale}) is False
    assert container_repo.is_provision_claim_stale({"status": "provisioning", "provision_claimed_at": now}) is False
    assert container_repo.is_provision_claim_stale({"status": "provisioning", "provision_claimed_at": stale}) is True
    # Legacy wedged row: provisioning with no lease at all -> stale (recoverable).
    assert container_repo.is_provision_claim_stale({"status": "provisioning"}) is True
