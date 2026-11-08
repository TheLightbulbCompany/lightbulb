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

Being owner-wide costs the caller the ability to tell "you already have THIS
listing installing" from "a DIFFERENT listing of yours is installing" just
from getting refused — and those two cases need OPPOSITE client advice (see
``routers/marketplace_listings``). ``try_claim_owner_deploy`` returns
``(token, held_listing_id)`` so the router can still make that call, reading
the held claim's listing_id off the SAME conditional write via
``ReturnValuesOnConditionCheckFailure`` rather than a second, racy read.

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

    first, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    second, held = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    assert first is not None
    assert second is None, "an overlapping deploy of the same listing must not get the claim"
    assert _claim(dynamodb_table)["tok"] == first
    assert held == "lst_pulse", "the refusal must surface which listing holds the claim"


@pytest.mark.asyncio
async def test_two_different_listings_same_owner_the_second_is_refused(dynamodb_table):
    """THE FIX: the claim is owner-wide, so a DIFFERENT listing no longer slips past it.

    Regression test for the 2026-08-06 incident: three listings hired within
    9 seconds all ran concurrently under the old per-listing claim because
    each wrote its own map key.
    """
    from core.repositories import container_repo

    a, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    b, held = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")

    assert a is not None
    assert b is None, "a second, DIFFERENT listing must still be fenced by the owner-wide claim"
    # The claim value still names the listing that holds it, for diagnostics —
    # the map KEY is fixed, but listing_id is preserved in the value.
    assert _claim(dynamodb_table)["listing_id"] == "lst_pulse"
    assert held == "lst_pulse", "the refusal must surface the HOLDER's listing, not the requester's"


@pytest.mark.asyncio
async def test_held_listing_id_lets_the_caller_distinguish_same_from_different(dynamodb_table):
    """THE ACTUAL POINT of returning held_listing_id: the router needs to give
    OPPOSITE advice for "same listing already deploying" (don't retry — that's
    the Pulse-2 duplicate-mint bug) vs. "a different listing of yours is
    deploying" (retry after a beat). Both look identical as a bare refusal;
    only ``held_listing_id`` tells them apart.
    """
    from core.repositories import container_repo

    _token, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    _, held_same = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    _, held_different = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")

    assert held_same == "lst_pulse"
    assert held_different == "lst_pulse"
    # Both calls see the SAME holder (the live claim never changed) — the
    # caller distinguishes "same vs. different" by comparing held_* against
    # the listing_id IT requested, not by anything the repo itself branches on.
    requested_same = "lst_pulse"
    requested_different = "lst_vera"
    assert held_same == requested_same, "same-listing case: holder equals requester's own listing"
    assert held_different != requested_different, "different-listing case: holder is NOT the requester's listing"


@pytest.mark.asyncio
async def test_different_owners_both_claims_succeed(dynamodb_table):
    """The fence must not be global — it is scoped to the owner_id partition key.

    Without this, serializing deploys per owner could regress into
    serializing ALL marketplace deploys fleet-wide.
    """
    from core.repositories import container_repo

    a, held_a = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    b, held_b = await container_repo.try_claim_owner_deploy("org_2", listing_id="lst_pulse")

    assert a is not None
    assert b is not None, "a different owner's deploy must not be blocked by another owner's claim"
    assert a != b
    assert held_a is None and held_b is None, "a WON claim carries no held_listing_id"
    assert _claim(dynamodb_table, "org_1")["tok"] == a
    assert _claim(dynamodb_table, "org_2")["tok"] == b


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_reclaimable_a_live_one_is_not(dynamodb_table):
    """A task that dies mid-deploy must not wedge the owner's hires forever."""
    from core.repositories import container_repo

    live, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    blocked, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")
    assert blocked is None

    # Backdate past the staleness window, as a dead ECS task would leave it.
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": container_repo._DEPLOY_CLAIM_KEY},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    taken_over, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")
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

    straggler, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": container_repo._DEPLOY_CLAIM_KEY},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    successor, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera")

    # The straggler finally finishes and runs its (stale) release — the
    # mismatched token must make this a no-op, not a delete of the live claim.
    await container_repo.release_owner_deploy_claim("org_1", listing_id="lst_pulse", token=straggler)
    assert _claim(dynamodb_table)["tok"] == successor, "the straggler's release clobbered its successor's live claim"
    blocked_again, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    assert blocked_again is None

    # The matching token DOES release it.
    await container_repo.release_owner_deploy_claim("org_1", listing_id="lst_vera", token=successor)
    assert _claim(dynamodb_table) is None
    freed, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    assert freed


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
        token, held = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    assert token is not None, "a DDB fault must still admit the deploy, unfenced"
    assert held is None


@pytest.mark.asyncio
async def test_a_non_clienterror_boto_fault_also_fails_open(dynamodb_table):
    """``BotoCoreError`` is NOT a ``ClientError`` — it carries no ``Error`` code.

    The handler used to catch only ``ClientError``, so an
    ``EndpointConnectionError`` (credential resolution, DNS, connect timeout —
    all ``BotoCoreError``) propagated out of a call whose docstring promises a
    DDB fault fails open. That raise escaped ``deploy_for_approval`` BEFORE its
    rollback envelope and stranded an already-approved agent with no workspace.
    """
    from botocore.exceptions import EndpointConnectionError

    from core.repositories import container_repo

    spy = MagicMock()
    spy.update_item.side_effect = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
    with patch.object(container_repo, "_get_table", return_value=spy):
        token, held = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    assert token is not None, "a BotoCoreError must fail OPEN like any other DDB fault, not raise"
    assert held is None


@pytest.mark.asyncio
async def test_client_error_fails_closed_when_requested(dynamodb_table):
    """Approval pre-creation cannot guess whether an exclusive claim exists."""
    from core.repositories import container_repo

    spy = MagicMock()
    spy.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem"
    )
    with patch.object(container_repo, "_get_table", return_value=spy):
        token, held = await container_repo.try_claim_owner_deploy(
            "org_1", listing_id="approval:issue-1", fail_open=False
        )

    assert token is None
    assert held is None


@pytest.mark.asyncio
async def test_generic_fault_fails_closed_when_requested(dynamodb_table):
    """The fail-closed choice covers non-ClientError failures too."""
    from botocore.exceptions import EndpointConnectionError

    from core.repositories import container_repo

    spy = MagicMock()
    spy.update_item.side_effect = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
    with patch.object(container_repo, "_get_table", return_value=spy):
        token, held = await container_repo.try_claim_owner_deploy(
            "org_1", listing_id="approval:issue-1", fail_open=False
        )

    assert token is None
    assert held is None


@pytest.mark.asyncio
async def test_a_timed_out_deploy_holds_the_claim_until_the_lease_expires(dynamodb_table):
    """THE FIX: a client-side timeout must NOT hand the claim to the next hire.

    ``unwind_partial_deploy`` deliberately leaves the container-side install
    alone on a ``TimeoutError`` because the container may still be writing it.
    Releasing the claim in the same breath let a retry start a second install
    concurrently with the still-live first one — defeating the fence in exactly
    the case it exists for. The lease is the settling period instead.
    """
    from core.repositories import container_repo

    token, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")

    await container_repo.release_owner_deploy_claim(
        "org_1", listing_id="lst_pulse", token=token, cause=TimeoutError("no response")
    )
    assert _claim(dynamodb_table) is not None, "a timed-out deploy must keep its claim"
    retry, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    assert retry is None, "the retry must still be fenced while the timed-out deploy settles"

    # But the lease is a lease, not a wedge: it ages out on its own.
    dynamodb_table.update_item(
        Key={"owner_id": "org_1"},
        UpdateExpression="SET deploy_claims.#lid.claimed_at = :old",
        ExpressionAttributeNames={"#lid": container_repo._DEPLOY_CLAIM_KEY},
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.DEPLOY_CLAIM_STALE_S - 1},
    )
    after_settling, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    assert after_settling is not None


@pytest.mark.asyncio
async def test_a_non_timeout_failure_releases_the_claim_immediately(dynamodb_table):
    """The other half: only the timeout path holds. A normal failed hire must
    free the owner at once, or one 500 wedges every hire for the lease's 600s.
    """
    from core.repositories import container_repo

    token, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    await container_repo.release_owner_deploy_claim(
        "org_1", listing_id="lst_pulse", token=token, cause=RuntimeError("deploy failed")
    )

    assert _claim(dynamodb_table) is None
    assert (await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_vera"))[0] is not None


@pytest.mark.asyncio
async def test_a_timeout_wrapped_in_an_http_error_still_holds_the_claim(dynamodb_table):
    """Callers re-raise the failure wrapped (the marketplace router as a 500),
    so the timeout reaches the release one link
    down the ``__cause__`` chain. Matching only the outermost type would let
    every real deploy path release on timeout while the unit test passed.
    """
    from core.repositories import container_repo

    token, _ = await container_repo.try_claim_owner_deploy("org_1", listing_id="lst_pulse")
    try:
        try:
            raise TimeoutError("pair RPC")
        except TimeoutError as exc:
            raise RuntimeError("deploy failed") from exc
    except RuntimeError as wrapped:
        await container_repo.release_owner_deploy_claim("org_1", listing_id="lst_pulse", token=token, cause=wrapped)

    assert _claim(dynamodb_table) is not None, "the wrapped timeout must still hold the claim"


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
    This condition fails on the FIRST write (no ``ReturnValuesOnConditionCheckFailure``
    requested there), so held_listing_id is None too — there's no claim to report.
    """
    from core.repositories import container_repo

    token, held = await container_repo.try_claim_owner_deploy("org_none", listing_id="lst")
    assert token is None
    assert held is None
    assert dynamodb_table.get_item(Key={"owner_id": "org_none"}).get("Item") is None
