"""The marketplace-deploy claim: single-flight per (owner, listing), fenced release.

THE INCIDENT this closes: a Hire took 85s on a busy container
(``pair_with_openclaw`` burns a 30s gateway-RPC timeout before its retry
succeeds), the browser lost the response, the drawer said "Couldn't hire", and
the buyer clicked again. The two deploys OVERLAPPED — the second minted its
Paperclip agent 35s before the first wrote the ``.template`` sidecar the
router's read-check looks at — so prod ended up with a "Pulse" and a "Pulse 2".

Exercised against a REAL DynamoDB engine (moto), not a hand mock: every
primitive here is a conditional write over a NESTED map path, and a MagicMock
would happily accept expressions DynamoDB rejects outright (a bare
``SET deploy_claims.#lid`` on an absent parent is a ValidationException).
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
        table.put_item(Item={"owner_id": "org_1", "status": "running"})
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _claims(table, owner_id="org_1"):
    return (table.get_item(Key={"owner_id": owner_id}).get("Item") or {}).get("deploy_claims", {})


@pytest.mark.asyncio
async def test_first_claim_wins_and_the_overlapping_one_loses(dynamodb_table):
    from core.repositories import container_repo

    first = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    second = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")

    assert first is not None
    assert second is None, "an overlapping deploy must not get the claim"
    assert _claims(dynamodb_table)["lst_pulse"]["tok"] == first


@pytest.mark.asyncio
async def test_two_different_listings_can_deploy_at_once(dynamodb_table):
    """The claim is per-listing, so hiring two agents concurrently still works."""
    from core.repositories import container_repo

    a = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    b = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_vera")

    assert a and b
    assert set(_claims(dynamodb_table)) == {"lst_pulse", "lst_vera"}


@pytest.mark.asyncio
async def test_release_frees_the_listing_for_a_later_hire(dynamodb_table):
    from core.repositories import container_repo

    token = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    await container_repo.release_listing_deploy_claim("org_1", listing_id="lst_pulse", token=token)

    assert "lst_pulse" not in _claims(dynamodb_table)
    assert await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_reclaimable_a_live_one_is_not(dynamodb_table):
    """A task that dies mid-deploy must not wedge the listing forever."""
    from core.repositories import container_repo

    live = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    assert await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse") is None

    # Backdate past the staleness window, as a dead ECS task would leave it.
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": "lst_pulse"},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    taken_over = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    assert taken_over is not None
    assert taken_over != live


@pytest.mark.asyncio
async def test_a_taken_over_straggler_cannot_release_its_successors_claim(dynamodb_table):
    """The fencing token, and why it exists.

    Without it, a deploy that hung past the staleness window would reach its
    ``finally`` and unconditionally delete the live claim of whoever took over —
    letting a third deploy in and reopening the overlap this whole mechanism
    closes.
    """
    from core.repositories import container_repo

    straggler = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": "lst_pulse"},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    successor = await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse")

    # The straggler finally finishes and runs its release.
    await container_repo.release_listing_deploy_claim("org_1", listing_id="lst_pulse", token=straggler)

    assert _claims(dynamodb_table)["lst_pulse"]["tok"] == successor, (
        "the straggler's release clobbered its successor's live claim"
    )
    assert await container_repo.try_claim_listing_deploy("org_1", listing_id="lst_pulse") is None


@pytest.mark.asyncio
async def test_release_on_an_already_free_listing_is_a_noop(dynamodb_table):
    """Runs in a ``finally`` — it must never raise, whatever the row looks like."""
    from core.repositories import container_repo

    await container_repo.release_listing_deploy_claim("org_1", listing_id="never_claimed", token="whatever")
    assert _claims(dynamodb_table) == {}


@pytest.mark.asyncio
async def test_claim_on_an_owner_with_no_container_row_is_refused(dynamodb_table):
    """``attribute_exists(owner_id)`` — never conjure a container row from a deploy.

    A deploy already requires a running container downstream; creating a bare
    row here would confuse the provision lease that lives on the same item.
    """
    from core.repositories import container_repo

    assert await container_repo.try_claim_listing_deploy("org_none", listing_id="lst") is None
    assert dynamodb_table.get_item(Key={"owner_id": "org_none"}).get("Item") is None
