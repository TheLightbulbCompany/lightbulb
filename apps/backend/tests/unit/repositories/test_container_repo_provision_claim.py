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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "error", "provisioning"])
@pytest.mark.parametrize("pointer", ["reserved", None])
async def test_release_pointer_blocks_provision_acquisition(dynamodb_table, status, pointer):
    from core.repositories import container_repo

    item = {
        "owner_id": "release-acquire",
        "status": status,
        "release_update_id": pointer,
        "provision_claim_id": "candidate",
        "provision_attempt_token": "candidate",
    }
    if status == "provisioning":
        item.update(provision_claim_id="stale", provision_claimed_at=1)
    dynamodb_table.put_item(Item=item)

    assert await container_repo.try_claim_provision("release-acquire", claim_id="candidate") is None
    assert _row(dynamodb_table, "release-acquire") == item


@pytest.mark.asyncio
@pytest.mark.parametrize("pointer", ["reserved", None])
async def test_release_pointer_blocks_every_provision_writer(dynamodb_table, pointer):
    from core.repositories import container_repo

    original = {
        "owner_id": "release-provision",
        "status": "provisioning",
        "provision_claim_id": "claim",
        "provision_attempt_token": "attempt",
        "provision_claimed_at": 1,
        "release_update_id": pointer,
    }

    def reset():
        dynamodb_table.put_item(Item=original)

    reset()
    with pytest.raises(container_repo.ProvisionSuperseded):
        await container_repo.upsert("release-provision", {"status": "running"})
    assert _row(dynamodb_table, "release-provision") == original

    reset()
    with pytest.raises(container_repo.ProvisionSuperseded):
        await container_repo.update_fields("release-provision", {"status": "running"})
    assert _row(dynamodb_table, "release-provision") == original

    reset()
    with pytest.raises(container_repo.ProvisionSuperseded):
        await container_repo.takeover_provision("release-provision")
    assert _row(dynamodb_table, "release-provision") == original

    reset()
    with pytest.raises(container_repo.ProvisionSuperseded):
        await container_repo.claim_attempt("release-provision", "claim", "next-attempt")
    assert _row(dynamodb_table, "release-provision") == original

    for release_lease in (False, True):
        reset()
        with pytest.raises(container_repo.ProvisionSuperseded):
            await container_repo.update_container_leased(
                "release-provision",
                "claim",
                {"status": "running"},
                attempt_token="attempt",
                release_lease=release_lease,
            )
        assert _row(dynamodb_table, "release-provision") == original

    reset()
    await container_repo.release_provision_to_error(
        "release-provision", "claim", last_error="must-not-land", attempt_token="attempt"
    )
    assert _row(dynamodb_table, "release-provision") == original


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


# ---------------------------------------------------------------------------
# SFN reshape: claim_id/execution_name stamping (dispatch Branch B) +
# provision_attempt_token (claim_attempt, hazard 1 fencing) + generation/
# attempt_token conditioning on the fenced write helpers.


@pytest.mark.asyncio
async def test_try_claim_provision_stamps_execution_name_and_attempt_token(dynamodb_table):
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision(
        "user_abc", claim_id="fixed-claim-uuid", execution_name="provision-user_abc-fixedcla"
    )
    assert claim == "fixed-claim-uuid"
    row = _row(dynamodb_table, "user_abc")
    assert row["provision_execution_name"] == "provision-user_abc-fixedcla"
    assert row["provision_claim_id"] == "fixed-claim-uuid"
    # Attempt 0's fencing token is the claim_id itself — deterministic, so
    # dispatch_provision's execution_input() (which embeds it) stays
    # byte-identical across a Branch-A re-issue for the same claim.
    assert row["provision_attempt_token"] == "fixed-claim-uuid"
    assert row["status"] == "provisioning"


@pytest.mark.asyncio
async def test_release_lease_removes_all_lease_fields_on_success_write(dynamodb_table):
    """The terminal SUCCESS write (status="running", release_lease=True)
    atomically removes every lease field — so a "provisioning row carrying a
    claim" stays a reliable active-SFN-work signal for the boot reconcile,
    and a later interrupted wake/resize (which never touches lease fields)
    is not misclassified as SFN-owned."""
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision(
        "user_rel", claim_id="claim-rel", execution_name="provision-user_rel-claimrel"
    )
    assert claim == "claim-rel"

    await container_repo.update_container_leased(
        "user_rel",
        "claim-rel",
        {"status": "running", "substatus": "gateway_healthy"},
        release_lease=True,
    )

    row = _row(dynamodb_table, "user_rel")
    assert row["status"] == "running"
    for lease_field in (
        "provision_claim_id",
        "provision_claimed_at",
        "provision_attempt_token",
        "provision_execution_name",
    ):
        assert lease_field not in row

    # The lease is genuinely gone: a fresh claim on the running row must
    # REFUSE (running is not claimable) rather than see a stale lease.
    assert await container_repo.try_claim_provision("user_rel") is None


@pytest.mark.asyncio
async def test_try_claim_provision_losing_claim_leaves_stamp_unchanged(dynamodb_table):
    # A losing claim (fresh provisioning lease held by another claim) is one
    # atomic conditional write: the loser's claim_id AND execution_name must
    # both be rejected together — a half-applied stamp would point the winner's
    # row at an execution that will never run.
    from core.repositories import container_repo

    won = await container_repo.try_claim_provision(
        "user_ghi", claim_id="winner-claim", execution_name="provision-user_ghi-winnercl"
    )
    assert won == "winner-claim"
    lost = await container_repo.try_claim_provision(
        "user_ghi", claim_id="loser-claim", execution_name="provision-user_ghi-losercla"
    )
    assert lost is None
    row = _row(dynamodb_table, "user_ghi")
    assert row["provision_claim_id"] == "winner-claim"
    assert row["provision_execution_name"] == "provision-user_ghi-winnercl"


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_field", ["provision_claim_id", "provision_attempt_token"])
async def test_try_claim_provision_does_not_overwrite_lease_on_stopped_status(dynamodb_table, lease_field):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "owner-busy", "status": "stopped", lease_field: "existing"})

    assert await container_repo.try_claim_provision("owner-busy", claim_id="successor") is None
    row = _row(dynamodb_table, "owner-busy")
    assert row[lease_field] == "existing"
    assert row["status"] == "stopped"


@pytest.mark.asyncio
async def test_try_claim_provision_without_name_leaves_field_absent(dynamodb_table):
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_def")
    assert claim is not None
    row = _row(dynamodb_table, "user_def")
    assert "provision_execution_name" not in row
    # Still stamps the attempt token (claim_id itself) even without a name —
    # every claim, however dispatched, is fencible from attempt 0.
    assert row["provision_attempt_token"] == claim


@pytest.mark.asyncio
async def test_takeover_stamps_attempt_token_too(dynamodb_table):
    """A deliberate takeover door (conversion/admin/debug) also stamps
    provision_attempt_token, so a direct provision_user_container(claim_id=...)
    call from one of those doors is fenceable exactly like the SFN path."""
    from core.repositories import container_repo

    claim = await container_repo.takeover_provision("user_deliberate")
    row = _row(dynamodb_table, "user_deliberate")
    assert row["provision_attempt_token"] == claim


# ---------------------------------------------------------------------------
# claim_attempt — per-SFN-retry fencing (spec §7 hazard 1)


@pytest.mark.asyncio
async def test_claim_attempt_restamps_token_and_fences_the_straggler(dynamodb_table):
    """THE HAZARD: an SFN retry mints a fresh attempt_token while claim_id and
    the execution name stay stable. A straggler from the SUPERSEDED attempt
    (still holding the OLD token in memory) must be rejected at its next
    checkpoint, even though its claim_id still matches."""
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    await container_repo.try_claim_provision("user_retry", claim_id="claim-1", execution_name="exec-1")
    old_token = _row(dynamodb_table, "user_retry")["provision_attempt_token"]
    assert old_token == "claim-1"

    # SFN's BumpAttempt mints a fresh token for the new attempt; the new
    # runner task re-stamps it as its FIRST action.
    await container_repo.claim_attempt("user_retry", "claim-1", "fresh-attempt-token")
    row = _row(dynamodb_table, "user_retry")
    assert row["provision_attempt_token"] == "fresh-attempt-token"
    assert row["provision_claim_id"] == "claim-1"  # unchanged — execution name still matches

    # The straggler (still holding old_token) is fenced out at its NEXT
    # checkpoint, even though claim_id alone would still pass.
    with pytest.raises(ProvisionSuperseded):
        await container_repo.update_container_leased(
            "user_retry", "claim-1", {"substatus": "poison"}, attempt_token=old_token
        )
    assert _row(dynamodb_table, "user_retry")["substatus"] != "poison"

    # The NEW attempt's own checkpoints, carrying the fresh token, succeed.
    await container_repo.update_container_leased(
        "user_retry", "claim-1", {"substatus": "legit"}, attempt_token="fresh-attempt-token"
    )
    assert _row(dynamodb_table, "user_retry")["substatus"] == "legit"


@pytest.mark.asyncio
async def test_claim_attempt_raises_superseded_when_claim_itself_was_taken_over(dynamodb_table):
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    await container_repo.try_claim_provision("user_taken", claim_id="claim-old", execution_name="exec-old")
    await container_repo.takeover_provision("user_taken")  # a deliberate door takes over

    with pytest.raises(ProvisionSuperseded):
        await container_repo.claim_attempt("user_taken", "claim-old", "new-attempt-token")


# ---------------------------------------------------------------------------
# generation / attempt_token conditioning on the fenced write helpers


@pytest.mark.asyncio
async def test_update_container_leased_generation_mismatch_raises_superseded(dynamodb_table):
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    claim = await container_repo.try_claim_provision("user_gen")
    await container_repo.update_fields("user_gen", {"provision_generation": "gen-old"})

    with pytest.raises(ProvisionSuperseded):
        await container_repo.update_container_leased("user_gen", claim, {"substatus": "poison"}, generation="gen-new")
    assert _row(dynamodb_table, "user_gen")["substatus"] != "poison"

    # Matching generation succeeds.
    await container_repo.update_container_leased("user_gen", claim, {"substatus": "ok"}, generation="gen-old")
    assert _row(dynamodb_table, "user_gen")["substatus"] == "ok"


@pytest.mark.asyncio
async def test_update_container_leased_generation_absent_on_row_is_permissive(dynamodb_table):
    """A brand-new claim (fresh full-provision, generation not minted yet)
    must not be blocked by a generation condition that has nothing to compare
    against — attribute_not_exists(...) OR match, not AND."""
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_nogen")
    await container_repo.update_container_leased("user_nogen", claim, {"substatus": "ok"}, generation="anything")
    assert _row(dynamodb_table, "user_nogen")["substatus"] == "ok"


@pytest.mark.asyncio
async def test_release_provision_to_error_attempt_token_mismatch_is_a_noop(dynamodb_table):
    """Mirrors update_container_leased's attempt_token fence on the error
    transition: a straggler's release must not clobber the current attempt's
    error state."""
    from core.repositories import container_repo

    await container_repo.try_claim_provision("user_relgen", claim_id="claim-r", execution_name="exec-r")
    await container_repo.claim_attempt("user_relgen", "claim-r", "current-token")

    # Straggler holding the OLD (attempt-0) token tries to release -- no-op.
    await container_repo.release_provision_to_error(
        "user_relgen", "claim-r", last_error="stale attempt", attempt_token="claim-r"
    )
    assert _row(dynamodb_table, "user_relgen")["status"] == "provisioning"

    # The CURRENT attempt's own release lands normally.
    await container_repo.release_provision_to_error(
        "user_relgen", "claim-r", last_error="real failure", attempt_token="current-token"
    )
    row = _row(dynamodb_table, "user_relgen")
    assert row["status"] == "error"
    assert row["last_error"] == "real failure"


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


# ---------------------------------------------------------------------------
# update_conversion_fields — the SCOPED conversion-marker writer (#1428)
#
# The marker used to ride the get→put update_fields; racing the conversion's
# own freshly dispatched claim, the stale full-item put resurrected
# status="stopped" over "provisioning" and ERASED the lease — superseding the
# conversion's own SFN execution with no successor. The scoped writer must be
# physically incapable of that.


@pytest.mark.asyncio
async def test_update_conversion_fields_cannot_clobber_a_fresh_claim(dynamodb_table):
    """The incident interleaving: claim lands first, marker write second. The
    marker write must leave status AND every lease field intact."""
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision("user_conv", execution_name="provision-user_conv-abcd1234")
    await container_repo.update_conversion_fields(
        "user_conv",
        {"conversion_pending": False, "substatus": None},
    )

    row = _row(dynamodb_table, "user_conv")
    assert row["status"] == "provisioning", "marker write must never touch status"
    assert row["provision_claim_id"] == claim
    assert row["provision_attempt_token"] == claim
    assert row["provision_execution_name"] == "provision-user_conv-abcd1234"
    assert row["conversion_pending"] is False
    assert row.get("substatus") is None  # written as NULL, same read shape as before


@pytest.mark.asyncio
async def test_update_conversion_fields_writes_the_stamp_fields(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_stamp", "status": "running"})
    await container_repo.update_conversion_fields(
        "user_stamp",
        {"conversion_pending": True, "substatus": "converting", "conversion_started_at": "2026-07-24T00:00:00+00:00"},
    )

    row = _row(dynamodb_table, "user_stamp")
    assert row["conversion_pending"] is True
    assert row["substatus"] == "converting"
    assert row["conversion_started_at"] == "2026-07-24T00:00:00+00:00"
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_update_conversion_fields_rejects_non_marker_fields(dynamodb_table):
    """The allowlist is the guarantee: this helper must never grow into a
    general row writer (that is exactly the clobber class it replaces)."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_allow", "status": "running"})
    with pytest.raises(ValueError, match="disallowed fields"):
        await container_repo.update_conversion_fields("user_allow", {"status": "stopped"})
    with pytest.raises(ValueError, match="disallowed fields"):
        await container_repo.update_conversion_fields("user_allow", {"provision_claim_id": "x"})
    assert _row(dynamodb_table, "user_allow")["status"] == "running"


@pytest.mark.asyncio
async def test_update_conversion_fields_noops_on_an_absent_row(dynamodb_table):
    """A concurrently deleted row must not be resurrected as a marker-only
    ghost (mirrors stamp_stop_reason's condition)."""
    from core.repositories import container_repo

    await container_repo.update_conversion_fields("user_ghost", {"conversion_pending": True})
    assert _row(dynamodb_table, "user_ghost") is None


# ---------------------------------------------------------------------------
# release_stranded_stop_to_error — recovery CAS for the superseded-without-
# successor strand (#1428)


@pytest.mark.asyncio
async def test_stranded_release_flips_a_claimless_stopped_row(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_strand", "status": "stopped", "stop_reason": "reprovision"})

    released = await container_repo.release_stranded_stop_to_error("user_strand", last_error="no successor")

    assert released is True
    row = _row(dynamodb_table, "user_strand")
    assert row["status"] == "error"
    assert row["substatus"] == "provision_failed"
    assert row["retryable"] is True
    assert row["last_error"] == "no successor"
    # ...and the error row is immediately re-claimable — the whole point.
    assert await container_repo.try_claim_provision("user_strand") is not None


@pytest.mark.asyncio
async def test_stranded_release_refuses_when_a_claim_exists(dynamodb_table):
    """A present claim == a successor exists; its liveness belongs to Branch A
    / the stale-lease heuristic, never to this flip. All clauses evaluate
    atomically, so a claim landing before the write wins."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_claimed", "status": "stopped", "provision_claim_id": "claim-live"})

    assert await container_repo.release_stranded_stop_to_error("user_claimed", last_error="x") is False
    row = _row(dynamodb_table, "user_claimed")
    assert row["status"] == "stopped"
    assert "last_error" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "provisioning", "error"])
async def test_stranded_release_only_targets_stopped_rows(dynamodb_table, status):
    """running = healthy; unclaimed provisioning = a wake/resize transition's
    row; error = already in the self-healing state. None may be flipped."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_other", "status": status})
    assert await container_repo.release_stranded_stop_to_error("user_other", last_error="x") is False
    assert _row(dynamodb_table, "user_other")["status"] == status


@pytest.mark.asyncio
async def test_stranded_release_refuses_a_parked_trial_box(dynamodb_table):
    """stop_reason="trial_idle" is the deliberately parked trial box — it keeps
    its wake-gated semantics and must never be error-flipped into an
    unconditional poll retry."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_parked", "status": "stopped", "stop_reason": "trial_idle"})
    assert await container_repo.release_stranded_stop_to_error("user_parked", last_error="x") is False
    assert _row(dynamodb_table, "user_parked")["status"] == "stopped"


# ---------------------------------------------------------------------------
# clear_conversion_deferral: the re-arm CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_conversion_deferral_clears_only_the_deferral_it_observed(dynamodb_table):
    """A re-arm drops the self-heal anchor + deferral marker ONLY while the row
    still carries the deferral it read; pending and status are untouched."""
    from core.repositories import container_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_defer",
            "status": "running",
            "conversion_pending": True,
            "conversion_started_at": "2026-07-24T00:00:00+00:00",
            "conversion_deferred_reason": "oauth_required",
        }
    )
    assert await container_repo.clear_conversion_deferral("user_defer", deferred_reason="oauth_required") is True
    row = _row(dynamodb_table, "user_defer")
    assert row["conversion_started_at"] is None
    assert row["conversion_deferred_reason"] is None
    assert row["conversion_pending"] is True
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_clear_conversion_deferral_refuses_when_a_fresh_dispatch_cleared_the_marker(dynamodb_table):
    """The race the CAS exists for: a fresh stamp cleared the marker (and
    re-armed the guard over a real stop + drain) between the re-arm's read and
    write. The write must refuse and leave the fresh anchor alone."""
    from core.repositories import container_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_fresh",
            "status": "running",
            "conversion_pending": True,
            "conversion_started_at": "2026-07-24T00:05:00+00:00",
            "conversion_deferred_reason": None,
        }
    )
    assert await container_repo.clear_conversion_deferral("user_fresh", deferred_reason="oauth_required") is False
    row = _row(dynamodb_table, "user_fresh")
    assert row["conversion_started_at"] == "2026-07-24T00:05:00+00:00"

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_other",
            "status": "running",
            "conversion_pending": False,
            "conversion_started_at": "2026-07-24T00:05:00+00:00",
            "conversion_deferred_reason": "oauth_required",
        }
    )
    assert await container_repo.clear_conversion_deferral("user_other", deferred_reason="oauth_required") is False
    assert await container_repo.clear_conversion_deferral("user_missing", deferred_reason="oauth_required") is False
