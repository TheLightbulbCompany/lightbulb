"""The marketplace-deploy claim: single-flight PER OWNER, fenced release.

THE INCIDENT this closes (v2): the claim used to be keyed per LISTING (``SET
deploy_claims.#lid`` with ``#lid`` bound to ``listing_id``), which only ever
fenced two deploys of the SAME listing — the "Pulse 2" duplicate-hire bug. On
2026-08-06 a buyer clicked Hire on three DIFFERENT listings within 9 seconds;
three different listing_ids wrote three different map keys, so all three
conditions passed and all three deploys ran concurrently against the one
OpenClaw container. Two came back HTTP 500 and rolled back.

The claim is now OWNER-WIDE: one marketplace deploy in flight per owner, any
listing. This costs nothing — the container already serializes config
mutations internally (a ``KeyedAsyncQueue`` wrapped in ``withFileLock``), so
concurrent callers only ever bought themselves queueing + retry churn, never
real parallelism (``agents.create`` measured 4s idle vs. 39s/51s/31s under
3-way concurrency).

Exercised against a REAL DynamoDB engine (moto), not a hand mock: every
primitive here is a conditional write over a NESTED map path, and a MagicMock
would happily accept expressions DynamoDB rejects outright (a bare
``SET deploy_claims.#lid`` on an absent parent is a ValidationException).
"""

import os
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
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
        table.put_item(Item={"owner_id": "org_2", "status": "running"})
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _claim(table, owner_id="org_1"):
    """The live owner-wide claim (or None), keyed on the fixed map key."""
    from core.repositories import container_repo

    claims = (table.get_item(Key={"owner_id": owner_id}).get("Item") or {}).get("deploy_claims", {})
    return claims.get(container_repo._DEPLOY_CLAIM_KEY)


@pytest.mark.asyncio
async def test_same_listing_twice_the_second_is_refused(dynamodb_table):
    """No regression of the original Pulse-2 dedupe: same listing, overlapping calls."""
    from core.repositories import container_repo

    first = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    second = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    assert first is not None
    assert second is None, "an overlapping deploy of the same listing must not get the claim"
    assert _claim(dynamodb_table)["tok"] == first


@pytest.mark.asyncio
async def test_two_different_listings_same_owner_the_second_is_refused(dynamodb_table):
    """THE FIX: the claim is owner-wide, so a DIFFERENT listing no longer slips past it.

    Regression test for the 2026-08-06 incident: three listings hired within
    9 seconds all ran concurrently under the old per-listing claim because
    each wrote its own map key.
    """
    from core.repositories import container_repo

    a = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    b = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")

    assert a is not None
    assert b is None, "a second, DIFFERENT listing must still be fenced by the owner-wide claim"
    # The claim value still names the listing that holds it, for diagnostics —
    # the map KEY is fixed, but listing_id is preserved in the value.
    assert _claim(dynamodb_table)["listing_id"] == "lst_pulse"


@pytest.mark.asyncio
async def test_different_owners_both_claims_succeed(dynamodb_table):
    """The fence must not be global — it is scoped to the owner_id partition key.

    Without this, serializing deploys per owner could regress into
    serializing ALL marketplace deploys fleet-wide.
    """
    from core.repositories import container_repo

    a = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    b = await container_repo.try_claim_owner_deploy("org_2", listing_id="lst_pulse")

    assert a is not None
    assert b is not None, "a different owner's deploy must not be blocked by another owner's claim"
    assert a != b
    assert _claim(dynamodb_table, "org_1")["tok"] == a
    assert _claim(dynamodb_table, "org_2")["tok"] == b


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_reclaimable_a_live_one_is_not(dynamodb_table):
    """A task that dies mid-deploy must not wedge the owner's hires forever."""
    from core.repositories import container_repo

    live = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    assert await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera") is None

    # Backdate past the staleness window, as a dead ECS task would leave it.
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": container_repo._DEPLOY_CLAIM_KEY},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    taken_over = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")
    assert taken_over is not None
    assert taken_over != live


@pytest.mark.asyncio
async def test_release_only_releases_when_the_fencing_token_matches(dynamodb_table):
    """The fencing token, and why it exists.

    Without it, a deploy that hung past the staleness window would reach its
    ``finally`` and unconditionally delete the live claim of whoever took
    over — letting a third deploy in and reopening the overlap this whole
    mechanism closes.
    """
    from core.repositories import container_repo

    straggler = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": container_repo._DEPLOY_CLAIM_KEY},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    successor = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")

    # The straggler finally finishes and runs its (stale) release — the
    # mismatched token must make this a no-op, not a delete of the live claim.
    await container_repo.release_owner_deploy_claim("org_1", listing_id="lst_pulse", token=straggler)
    assert _claim(dynamodb_table)["tok"] == successor, "the straggler's release clobbered its successor's live claim"
    assert await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse") is None

    # The matching token DOES release it.
    await container_repo.release_owner_deploy_claim("org_1", listing_id="lst_vera", token=successor)
    assert _claim(dynamodb_table) is None
    assert await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")


@pytest.mark.asyncio
async def test_ddb_fault_fails_open_and_still_admits_the_deploy(dynamodb_table):
    """A transient DynamoDB fault must not block a legitimate hire.

    Losing dedupe on a fault costs a duplicate agent the owner can delete;
    failing closed would block a paid hire on infra flakiness. The returned
    token fences nothing (no claim was actually stored), same as before the
    owner-wide rename.
    """
    from core.repositories import container_repo

    spy = MagicMock()
    spy.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem"
    )
    with patch.object(container_repo, "_get_table", return_value=spy):
        token = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    assert token is not None, "a DDB fault must still admit the deploy, unfenced"


@pytest.mark.asyncio
async def test_release_on_an_already_free_claim_is_a_noop(dynamodb_table):
    """Runs in a ``finally`` — it must never raise, whatever the row looks like."""
    from core.repositories import container_repo

    await container_repo.release_owner_deploy_claim("org_1", listing_id="never_claimed", token="whatever")
    assert _claim(dynamodb_table) is None


@pytest.mark.asyncio
async def test_claim_on_an_owner_with_no_container_row_is_refused(dynamodb_table):
    """``attribute_exists(owner_id)`` — never conjure a container row from a deploy.

    A deploy already requires a running container downstream; creating a bare
    row here would confuse the provision lease that lives on the same item.
    """
    from core.repositories import container_repo

    assert await container_repo.try_claim_owner_deploy("org_none", listing_id="lst") is None
    assert dynamodb_table.get_item(Key={"owner_id": "org_none"}).get("Item") is None
