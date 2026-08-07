"""Tests for container DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB containers table with GSIs."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "gateway_token", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gateway-token-index",
                    "KeySchema": [{"AttributeName": "gateway_token", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-index",
                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_upsert_creates_new(dynamodb_table):
    from core.repositories import container_repo

    result = await container_repo.upsert(
        "user_1",
        {
            "gateway_token": "tok_abc",
            "status": "provisioning",
        },
    )
    assert result["owner_id"] == "user_1"
    assert result["gateway_token"] == "tok_abc"
    assert "id" in result
    assert "created_at" in result


@pytest.mark.asyncio
async def test_upsert_preserves_id_and_created_at(dynamodb_table):
    from core.repositories import container_repo

    first = await container_repo.upsert(
        "user_1",
        {
            "gateway_token": "tok_abc",
            "status": "provisioning",
        },
    )

    second = await container_repo.upsert(
        "user_1",
        {
            "gateway_token": "tok_abc",
            "status": "running",
        },
    )

    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["status"] == "running"


@pytest.mark.asyncio
async def test_upsert_preserves_full_sfn_provision_lease(dynamodb_table):
    """create_user_service upserts MID-provision, and upsert's put_item
    replaces the whole item — it must carry the ENTIRE lease through, not
    just claim_id/claimed_at. Dropping provision_execution_name blinds
    Branch-A redispatch to the live execution; dropping
    provision_attempt_token makes every attempt-token fence permissive
    (they all allow attribute_not_exists), letting a superseded straggler
    pass its next checkpoint."""
    from core.repositories import container_repo

    claim = await container_repo.try_claim_provision(
        "user_lease",
        claim_id="claim-uuid-1",
        execution_name="provision-user_lease-claimuui",
    )
    assert claim == "claim-uuid-1"

    # The mid-provision upsert (create_user_service's row-creating write).
    row = await container_repo.upsert(
        "user_lease",
        {"gateway_token": "tok_abc", "status": "provisioning"},
    )

    assert row["provision_claim_id"] == "claim-uuid-1"
    assert row["provision_attempt_token"] == "claim-uuid-1"
    assert row["provision_execution_name"] == "provision-user_lease-claimuui"
    assert "provision_claimed_at" in row

    # And the persisted item, independently re-read, agrees.
    fetched = await container_repo.get_by_owner_id("user_lease")
    assert fetched["provision_attempt_token"] == "claim-uuid-1"
    assert fetched["provision_execution_name"] == "provision-user_lease-claimuui"


@pytest.mark.asyncio
async def test_upsert_preserves_the_bedrock_gate_superseded_hashes(dynamodb_table):
    """The gate-tokens table is hash-keyed with no owner index, so this list is
    the ONLY handle on an owner's superseded token rows — for deletion AND for
    deny_gate_token. Wiping it here strands those rows forever: nothing can find
    them, nothing can delete them, and the kill switch cannot reach them, while
    their plaintext still sits in the retained task-definition revision.

    The shape: an owner whose row exists in status="error" with no ECS service
    reprovisions. mint_gate_token writes {hash: B, superseded: [A]}, then
    create_user_service's row-creating upsert writes a fresh whole item. Without
    the preserve, that upsert drops [A] on the floor — a REGRESSION versus the
    one-hour TTL this branch deleted, which at least bounded the leak."""
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    # The repo names the field as a literal (importing the constant would close
    # an import cycle); bind the two so they cannot drift apart silently.
    assert bedrock_gate_tokens._SUPERSEDED_FIELD in container_repo._UPSERT_PRESERVED_FIELDS

    await container_repo.upsert(
        "user_gate",
        {"status": "error", "bedrock_gate_token_hash": "hash_A"},
    )
    # mint_gate_token: point the row at B, track A as superseded.
    await container_repo.update_fields(
        "user_gate",
        {"bedrock_gate_token_hash": "hash_B", bedrock_gate_tokens._SUPERSEDED_FIELD: ["hash_A"]},
    )

    # create_user_service's row-creating upsert. It folds in the new hash but
    # knows nothing about the superseded list.
    row = await container_repo.upsert(
        "user_gate",
        {"gateway_token": "tok", "status": "provisioning", "bedrock_gate_token_hash": "hash_B"},
    )

    assert row[bedrock_gate_tokens._SUPERSEDED_FIELD] == ["hash_A"]
    fetched = await container_repo.get_by_owner_id("user_gate")
    assert fetched[bedrock_gate_tokens._SUPERSEDED_FIELD] == ["hash_A"]


@pytest.mark.asyncio
async def test_upsert_lets_an_explicit_superseded_list_win(dynamodb_table):
    """The preserve is a fallback, not an override: a caller that passes the
    field explicitly still writes what it passed (same rule as the lease
    fields)."""
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    field = bedrock_gate_tokens._SUPERSEDED_FIELD
    await container_repo.upsert("user_gate2", {"status": "running", field: ["hash_A"]})

    row = await container_repo.upsert("user_gate2", {"status": "running", field: []})

    assert row[field] == []


@pytest.mark.asyncio
async def test_upsert_mints_distinct_provision_generation_per_call(dynamodb_table):
    """create_user_service (the sole caller of upsert) mints a fresh
    provision_generation on every row-creating write -- this repo function
    itself has no opinion on the value, it just writes whatever's handed to
    it. Two upserts carrying different provision_generation values land
    exactly what was written (no server-side preservation of this field)."""
    from core.repositories import container_repo

    first = await container_repo.upsert(
        "user_gen",
        {"gateway_token": "tok_abc", "status": "provisioning", "provision_generation": "gen_1"},
    )
    assert first["provision_generation"] == "gen_1"

    second = await container_repo.upsert(
        "user_gen",
        {"gateway_token": "tok_abc", "status": "provisioning", "provision_generation": "gen_2"},
    )
    assert second["provision_generation"] == "gen_2"


@pytest.mark.asyncio
async def test_update_fields_preserves_provision_generation(dynamodb_table):
    """INV-2: restart/redeploy/hot-swap/revoke-restore/conversion all write
    through update_fields's get->put merge, never through upsert -- so an
    existing provision_generation must survive a field-scoped update_fields
    write untouched, even though that write doesn't mention the field at
    all."""
    from core.repositories import container_repo

    await container_repo.upsert(
        "user_restart",
        {"gateway_token": "tok_abc", "status": "provisioning", "provision_generation": "gen_original"},
    )

    # Simulate a restart-path write (e.g. force-redeploy): a scoped field
    # update that says nothing about provision_generation.
    updated = await container_repo.update_fields(
        "user_restart",
        {"status": "running", "substatus": None},
    )

    assert updated["provision_generation"] == "gen_original"

    # And the persisted row, independently re-read, agrees.
    fetched = await container_repo.get_by_owner_id("user_restart")
    assert fetched["provision_generation"] == "gen_original"


@pytest.mark.asyncio
async def test_get_by_owner_id(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert(
        "user_2",
        {
            "gateway_token": "tok_def",
            "status": "running",
        },
    )
    item = await container_repo.get_by_owner_id("user_2")
    assert item is not None
    assert item["status"] == "running"


@pytest.mark.asyncio
async def test_get_by_owner_id_nonexistent(dynamodb_table):
    from core.repositories import container_repo

    item = await container_repo.get_by_owner_id("ghost")
    assert item is None


@pytest.mark.asyncio
async def test_get_by_gateway_token(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert(
        "user_3",
        {
            "gateway_token": "tok_ghi",
            "status": "running",
        },
    )
    item = await container_repo.get_by_gateway_token("tok_ghi")
    assert item is not None
    assert item["owner_id"] == "user_3"


@pytest.mark.asyncio
async def test_get_by_gateway_token_nonexistent(dynamodb_table):
    from core.repositories import container_repo

    item = await container_repo.get_by_gateway_token("nope")
    assert item is None


@pytest.mark.asyncio
async def test_get_by_status(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"gateway_token": "t1", "status": "running"})
    await container_repo.upsert("user_b", {"gateway_token": "t2", "status": "stopped"})
    await container_repo.upsert("user_c", {"gateway_token": "t3", "status": "running"})

    running = await container_repo.get_by_status("running")
    assert len(running) == 2
    owner_ids = {item["owner_id"] for item in running}
    assert owner_ids == {"user_a", "user_c"}


@pytest.mark.asyncio
async def test_get_by_status_paginates_through_last_evaluated_key():
    """Regression: DDB Query caps at 1MB per page. get_by_status must follow
    LastEvaluatedKey so the reaper doesn't silently miss users past the first
    page once the fleet grows."""
    from unittest.mock import MagicMock, patch

    from core.repositories import container_repo

    page1 = {
        "Items": [{"owner_id": "user_page1_a"}, {"owner_id": "user_page1_b"}],
        "LastEvaluatedKey": {"owner_id": "user_page1_b"},
    }
    page2 = {
        "Items": [{"owner_id": "user_page2_a"}],
        # no LastEvaluatedKey → loop terminates
    }

    fake_table = MagicMock()
    fake_table.query = MagicMock(side_effect=[page1, page2])

    with patch("core.repositories.container_repo._get_table", return_value=fake_table):
        results = await container_repo.get_by_status("running")

    assert [r["owner_id"] for r in results] == [
        "user_page1_a",
        "user_page1_b",
        "user_page2_a",
    ]
    assert fake_table.query.call_count == 2
    # First call must NOT include ExclusiveStartKey; second call MUST include
    # the key returned by the first page.
    first_kwargs = fake_table.query.call_args_list[0].kwargs
    second_kwargs = fake_table.query.call_args_list[1].kwargs
    assert "ExclusiveStartKey" not in first_kwargs
    assert second_kwargs["ExclusiveStartKey"] == {"owner_id": "user_page1_b"}


@pytest.mark.asyncio
async def test_update_status(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_s", {"gateway_token": "ts", "status": "provisioning"})
    result = await container_repo.update_status("user_s", "running", "gateway_healthy")
    assert result is not None
    assert result["status"] == "running"
    assert result["substatus"] == "gateway_healthy"


@pytest.mark.asyncio
async def test_update_status_nonexistent(dynamodb_table):
    from core.repositories import container_repo

    result = await container_repo.update_status("ghost", "running")
    assert result is None


@pytest.mark.asyncio
async def test_update_fields(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_f", {"gateway_token": "tf", "status": "running"})
    result = await container_repo.update_fields("user_f", {"task_arn": "arn:aws:ecs:task/123"})
    assert result is not None
    assert result["task_arn"] == "arn:aws:ecs:task/123"


@pytest.mark.asyncio
async def test_delete(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_d", {"gateway_token": "td", "status": "running"})
    await container_repo.delete("user_d")
    item = await container_repo.get_by_owner_id("user_d")
    assert item is None


@pytest.mark.asyncio
async def test_upsert_org_container(dynamodb_table):
    """Org containers store owner_type and org_id fields."""
    from core.repositories import container_repo

    result = await container_repo.upsert(
        "org_456",
        {
            "gateway_token": "tok_org",
            "status": "provisioning",
            "owner_type": "org",
            "org_id": "org_456",
        },
    )
    assert result["owner_id"] == "org_456"
    assert result["owner_type"] == "org"
    assert result["org_id"] == "org_456"


@pytest.mark.asyncio
async def test_get_by_owner_id_alias(dynamodb_table):
    """get_by_owner_id returns the container for the given owner."""
    from core.repositories import container_repo

    await container_repo.upsert(
        "org_456",
        {"gateway_token": "tok_org", "status": "running", "owner_type": "org"},
    )
    item = await container_repo.get_by_owner_id("org_456")
    assert item is not None
    assert item["owner_type"] == "org"


@pytest.mark.asyncio
async def test_upsert_paired_devices_merges_and_preserves_row(dynamodb_table):
    """Device pubkeys land on the owner row without clobbering existing fields,
    and multiple members' devices coexist in a shared org row."""
    from core.repositories import container_repo

    await container_repo.upsert("owner_1", {"gateway_token": "t", "status": "running"})

    await container_repo.upsert_paired_devices(
        "owner_1",
        {"dev_a": {"publicKey": "ka", "userId": "u1", "clientId": "node-host"}},
    )
    row = await container_repo.get_by_owner_id("owner_1")
    assert row["paired_devices"] == {"dev_a": {"publicKey": "ka", "userId": "u1", "clientId": "node-host"}}
    assert row["gateway_token"] == "t"  # row-level merge, not overwrite

    await container_repo.upsert_paired_devices(
        "owner_1",
        {"dev_b": {"publicKey": "kb", "userId": "u2", "clientId": "openclaw-macos"}},
    )
    row = await container_repo.get_by_owner_id("owner_1")
    assert set(row["paired_devices"]) == {"dev_a", "dev_b"}


@pytest.mark.asyncio
async def test_upsert_paired_devices_missing_container_returns_none(dynamodb_table):
    from core.repositories import container_repo

    assert await container_repo.upsert_paired_devices("ghost", {"d": {"publicKey": "k"}}) is None


@pytest.mark.asyncio
async def test_upsert_paired_devices_refuses_cross_member_relabel(dynamodb_table):
    """Security: deviceId=sha256(pubkey) and pairing doesn't prove key
    possession, so a co-tenant could submit another member's PUBLIC key and
    collide their deviceId. The row must NOT relabel a device already owned by
    a different member (else the authorizer would attribute the real owner's
    device to the attacker). Idempotent same-owner re-writes still apply."""
    from core.repositories import container_repo

    await container_repo.upsert("owner_org", {"gateway_token": "t"})
    # Bob's device paired first.
    await container_repo.upsert_paired_devices("owner_org", {"dev_b": {"publicKey": "pk_b", "userId": "bob"}})
    # Alice submits Bob's PUBLIC key -> same deviceId, tries to relabel as alice.
    await container_repo.upsert_paired_devices("owner_org", {"dev_b": {"publicKey": "pk_b", "userId": "alice"}})
    row = await container_repo.get_by_owner_id("owner_org")
    assert row["paired_devices"]["dev_b"]["userId"] == "bob"  # incumbent preserved

    # But Bob re-pairing his own device (same userId) still updates fine.
    await container_repo.upsert_paired_devices(
        "owner_org", {"dev_b": {"publicKey": "pk_b", "userId": "bob", "clientId": "node-host"}}
    )
    row = await container_repo.get_by_owner_id("owner_org")
    assert row["paired_devices"]["dev_b"]["clientId"] == "node-host"


@pytest.mark.asyncio
async def test_upsert_paired_device_wont_resurrect_row_deleted_mid_write(dynamodb_table, monkeypatch):
    """Race guard: the row exists when step 1 (ensure paired_devices map) runs,
    then a concurrent deprovision deletes it before step 2 (set the device).
    Step 2's attribute_exists(owner_id) condition must prevent recreating a
    partial zombie row that later get_by_owner_id consumers would choke on."""
    from core.repositories import container_repo

    await container_repo.upsert("owner_1", {"gateway_token": "t", "status": "running"})

    real_run = container_repo.run_in_thread
    calls = {"n": 0}

    async def racing_run(fn, *args, **kwargs):
        result = await real_run(fn, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:  # after step-1 ensure-map: simulate concurrent delete
            dynamodb_table.delete_item(Key={"owner_id": "owner_1"})
        return result

    monkeypatch.setattr(container_repo, "run_in_thread", racing_run)
    await container_repo.upsert_paired_devices("owner_1", {"dev_x": {"publicKey": "kx", "userId": "u1"}})
    # Row was deleted mid-write; step 2 must NOT have recreated it.
    assert dynamodb_table.get_item(Key={"owner_id": "owner_1"}).get("Item") is None


@pytest.mark.asyncio
async def test_remove_paired_device_deletes_only_target(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("owner_1", {"gateway_token": "t"})
    await container_repo.upsert_paired_devices(
        "owner_1",
        {"dev_a": {"publicKey": "ka"}, "dev_b": {"publicKey": "kb"}},
    )

    await container_repo.remove_paired_device("owner_1", "dev_a")
    row = await container_repo.get_by_owner_id("owner_1")
    assert set(row["paired_devices"]) == {"dev_b"}

    # Removing a non-present device (and removing on a missing row) is a no-op.
    await container_repo.remove_paired_device("owner_1", "dev_a")
    await container_repo.remove_paired_device("ghost", "dev_a")
    row = await container_repo.get_by_owner_id("owner_1")
    assert set(row["paired_devices"]) == {"dev_b"}


@pytest.mark.asyncio
async def test_record_wake_debit_increments_nested_map(dynamodb_table):
    """The wake budget's debit must actually count against a REAL DynamoDB
    engine — the whole budget is dead if the UpdateExpression raises. Guards
    the nested-map SET form against a regression back to `ADD wake_debits.#d`
    (ValidationException → fail-open → unbounded wakes)."""
    from core.repositories import container_repo

    await container_repo.upsert("user_wd", {"status": "stopped", "stop_reason": "trial_idle"})

    assert await container_repo.record_wake_debit("user_wd", "2026-07-19") == 1
    assert await container_repo.record_wake_debit("user_wd", "2026-07-19") == 2
    assert await container_repo.record_wake_debit("user_wd", "2026-07-19") == 3
    # A different UTC day is an independent counter.
    assert await container_repo.record_wake_debit("user_wd", "2026-07-20") == 1

    row = await container_repo.get_by_owner_id("user_wd")
    assert row["wake_debits"] == {"2026-07-19": 3, "2026-07-20": 1}


@pytest.mark.asyncio
async def test_record_wake_debit_missing_row_returns_zero(dynamodb_table):
    from core.repositories import container_repo

    assert await container_repo.record_wake_debit("nonexistent", "2026-07-19") == 0


@pytest.mark.asyncio
async def test_release_provision_to_error_records_cause(dynamodb_table):
    """The error transition records WHY it failed -- last_error (a short
    human-readable cause) and retryable (transient vs deterministic) -- so the
    status endpoint can tell a failure to silently retry from one to surface."""
    from core.repositories import container_repo

    claim_id = await container_repo.try_claim_provision("user_err")
    assert claim_id is not None

    await container_repo.release_provision_to_error(
        "user_err", claim_id, last_error="Paperclip 400: bad company slug", retryable=False
    )
    row = await container_repo.get_by_owner_id("user_err")
    assert row["status"] == "error"
    assert row["substatus"] == "provision_failed"
    assert row["last_error"] == "Paperclip 400: bad company slug"
    assert row["retryable"] is False


@pytest.mark.asyncio
async def test_release_provision_to_error_records_retryable_transient(dynamodb_table):
    """A transient cause is recorded retryable=True."""
    from core.repositories import container_repo

    claim_id = await container_repo.try_claim_provision("user_err2")
    await container_repo.release_provision_to_error("user_err2", claim_id, last_error="ECS throttled", retryable=True)
    row = await container_repo.get_by_owner_id("user_err2")
    assert row["retryable"] is True
    assert row["last_error"] == "ECS throttled"


@pytest.mark.asyncio
async def test_release_provision_to_error_cause_stays_token_conditioned(dynamodb_table):
    """The cause params are ADDITIVE -- the write stays conditioned on the
    fencing token AND status=provisioning exactly as before. A stale/wrong token
    must NOT clobber the row nor write a cause onto it."""
    from core.repositories import container_repo

    claim_id = await container_repo.try_claim_provision("user_err3")
    assert claim_id is not None

    await container_repo.release_provision_to_error(
        "user_err3", "wrong-token", last_error="should not land", retryable=False
    )
    row = await container_repo.get_by_owner_id("user_err3")
    assert row["status"] == "provisioning"  # unchanged -- wrong token no-ops
    assert "last_error" not in row
    assert "retryable" not in row


def test_classify_provision_error_paperclip_reuses_retryable():
    """PaperclipApiError already auto-classifies retryability off its status
    code — the classifier reuses .retryable verbatim (4xx deterministic, 5xx/429
    transient)."""
    from core.repositories import container_repo
    from core.services.paperclip_admin_client import PaperclipApiError

    det_cause, det_retryable = container_repo.classify_provision_error(PaperclipApiError("bad slug", 400, {}))
    assert det_retryable is False
    assert "400" in det_cause

    _, transient_retryable = container_repo.classify_provision_error(PaperclipApiError("down", 503, {}))
    assert transient_retryable is True


def test_classify_provision_error_validation_is_deterministic():
    """ValueError / TypeError / KeyError are deterministic config/shape bugs."""
    from core.repositories import container_repo

    for exc in (ValueError("bad"), TypeError("wrong type"), KeyError("missing")):
        _, retryable = container_repo.classify_provision_error(exc)
        assert retryable is False


def test_classify_provision_error_aws_validation_exception_deterministic():
    """A ClientError coded ValidationException (e.g. an empty-string GSI key
    in a PutItem) is a malformed request that fails identically forever —
    deterministic. Other ClientError codes keep the retryable default."""
    from botocore.exceptions import ClientError

    from core.repositories import container_repo

    def _client_error(code):
        return ClientError({"Error": {"Code": code, "Message": "boom"}}, "PutItem")

    _, retryable = container_repo.classify_provision_error(_client_error("ValidationException"))
    assert retryable is False

    _, retryable = container_repo.classify_provision_error(_client_error("ThrottlingException"))
    assert retryable is True


def test_classify_provision_error_unknown_defaults_retryable():
    """An unrecognised failure (incl. EcsManagerError wrapping AWS
    Spot/throttle/capacity) defaults retryable=True — a needless retry is
    cheaper than a wrongly-surfaced permanent error."""
    from core.repositories import container_repo

    cause, retryable = container_repo.classify_provision_error(RuntimeError("mystery"))
    assert retryable is True
    assert "RuntimeError" in cause


def test_classify_provision_error_deterministic_markers():
    """Known provider-config failures raise as plain Exception — the
    deterministic message markers catch them before the retryable default."""
    from core.repositories import container_repo

    _, retryable = container_repo.classify_provision_error(
        Exception("byo_provider is required when provider_choice == 'byo_key'")
    )
    assert retryable is False

    _, retryable = container_repo.classify_provision_error(Exception("Unknown provider_choice: 'bogus'"))
    assert retryable is False


def test_classify_provision_error_retryable_attr_beats_markers():
    """An exception's own retryable self-classification takes precedence —
    it is checked before the message markers, so a marker-free throttle
    message with retryable=False still classifies deterministic."""
    from core.repositories import container_repo

    exc = Exception("ThrottlingException")
    exc.retryable = False
    _, retryable = container_repo.classify_provision_error(exc)
    assert retryable is False


@pytest.mark.asyncio
async def test_upsert_without_a_gate_hash_does_not_delete_it(dynamodb_table):
    """``routers/debug.py`` calls ``create_user_service(owner_id, gateway_token)``
    with no hash, and a Bedrock -> chatgpt_oauth provider swap reprovisions
    through the same no-service branch. The whole-item put used to DELETE the
    pointer to the token the LIVE container is still presenting — stranding it
    exactly as a dropped superseded hash would."""
    from core.repositories import container_repo

    await container_repo.upsert("user_keep", {"status": "running", "bedrock_gate_token_hash": "hash_A"})

    row = await container_repo.upsert("user_keep", {"gateway_token": "tok", "status": "provisioning"})

    assert row["bedrock_gate_token_hash"] == "hash_A"
    assert (await container_repo.get_by_owner_id("user_keep"))["bedrock_gate_token_hash"] == "hash_A"


@pytest.mark.asyncio
async def test_upsert_reads_the_existing_row_consistently(dynamodb_table):
    """ASSERTS THE REQUEST PARAMETER, deliberately — the one place that is right.

    The displacement fold below decides whether the hash it is about to write
    replaces a DIFFERENT live one. A stale replica reports the hash being
    written as already current, so nothing looks displaced, the fold does not
    run, and the whole-item put erases the other hash — orphaning a
    denied=False token row nothing can find, expire, or deny.

    moto is always strongly consistent, so no observable state can distinguish
    the two reads. The guarantee lives entirely in ConsistentRead, which is why
    that is what gets asserted. Also asserted: the default read is NOT
    consistent — get_by_owner_id is the ~3s /container/status poll on every
    signed-in user, and doubling its RCUs to fix a write path would be the
    wrong trade.
    """
    from core.repositories import container_repo

    real_table = container_repo._get_table()
    reads: list[bool] = []

    class RecordingTable:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def get_item(self, **kwargs):
            reads.append(kwargs.get("ConsistentRead"))
            return real_table.get_item(**kwargs)

    with patch.object(container_repo, "_get_table", return_value=RecordingTable()):
        await container_repo.upsert("user_cr", {"status": "running"})
        assert reads == [True]

        reads.clear()
        await container_repo.get_by_owner_id("user_cr")
        assert reads == [False]


@pytest.mark.asyncio
async def test_upsert_tracks_the_hash_it_displaces(dynamodb_table):
    """An explicit hash still wins — but the one it replaces must be folded
    into the superseded list, not dropped: the row is the only handle on it."""
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    field = bedrock_gate_tokens._SUPERSEDED_FIELD
    await container_repo.upsert(
        "user_fold",
        {"status": "running", "bedrock_gate_token_hash": "hash_B", field: ["hash_A"]},
    )

    row = await container_repo.upsert("user_fold", {"status": "provisioning", "bedrock_gate_token_hash": "hash_C"})

    assert row["bedrock_gate_token_hash"] == "hash_C"
    assert row[field] == ["hash_A", "hash_B"]

    # Re-writing the SAME hash changes nothing, and a hash may never appear as
    # both current and superseded.
    row = await container_repo.upsert("user_fold", {"status": "running", "bedrock_gate_token_hash": "hash_A"})
    assert row["bedrock_gate_token_hash"] == "hash_A"
    assert row[field] == ["hash_B", "hash_C"]


@pytest.mark.asyncio
async def test_swap_gate_token_hash_writes_on_a_matching_prior(dynamodb_table):
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    field = bedrock_gate_tokens._SUPERSEDED_FIELD
    await container_repo.upsert("user_swap", {"status": "running", "bedrock_gate_token_hash": "hash_A"})

    outcome, conflicting = await container_repo.swap_gate_token_hash(
        "user_swap", new_hash="hash_B", superseded=["hash_A"], expected_prior_hash="hash_A"
    )

    assert (outcome, conflicting) == ("written", None)
    row = await container_repo.get_by_owner_id("user_swap")
    assert row["bedrock_gate_token_hash"] == "hash_B"
    assert row[field] == ["hash_A"]


@pytest.mark.asyncio
async def test_swap_gate_token_hash_writes_when_no_prior_hash_was_read(dynamodb_table):
    """A row that exists but carries no hash yet: the condition is
    attribute_not_exists, not equality with a null."""
    from core.repositories import container_repo

    await container_repo.upsert("user_swap_new", {"status": "provisioning"})

    outcome, conflicting = await container_repo.swap_gate_token_hash(
        "user_swap_new", new_hash="hash_A", superseded=[], expected_prior_hash=None
    )

    assert (outcome, conflicting) == ("written", None)
    row = await container_repo.get_by_owner_id("user_swap_new")
    assert row["bedrock_gate_token_hash"] == "hash_A"


@pytest.mark.asyncio
async def test_swap_gate_token_hash_refuses_a_changed_prior(dynamodb_table):
    """The concurrency fence: a hash moved out from under the caller means the
    caller's superseded list is stale, so the write must be refused rather than
    clobber the other mint's hash into unreachability."""
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    field = bedrock_gate_tokens._SUPERSEDED_FIELD
    await container_repo.upsert(
        "user_swap_race",
        {"status": "running", "bedrock_gate_token_hash": "hash_B", field: ["hash_A"]},
    )

    # Caller read hash_A, but a concurrent mint already moved the row to hash_B.
    outcome, conflicting = await container_repo.swap_gate_token_hash(
        "user_swap_race", new_hash="hash_C", superseded=["hash_A"], expected_prior_hash="hash_A"
    )

    assert outcome == "conflict"
    # The conflict state comes from the refused write itself, not a follow-up
    # (eventually consistent) read: it is the authoritative pre-write row, and
    # it carries everything the caller needs to re-arm.
    assert conflicting["bedrock_gate_token_hash"] == "hash_B"
    assert conflicting[field] == ["hash_A"]
    row = await container_repo.get_by_owner_id("user_swap_race")
    assert row["bedrock_gate_token_hash"] == "hash_B"
    assert row[field] == ["hash_A"]

    # Same for the no-prior form against a row that has since gained one.
    outcome, conflicting = await container_repo.swap_gate_token_hash(
        "user_swap_race", new_hash="hash_C", superseded=[], expected_prior_hash=None
    )
    assert outcome == "conflict"
    assert conflicting["bedrock_gate_token_hash"] == "hash_B"


@pytest.mark.asyncio
async def test_swap_gate_token_hash_no_ops_on_an_absent_row(dynamodb_table):
    """Preserves update_fields' silent no-op: mint_gate_token runs on the
    fresh-provision path before the row exists, and must NOT create one."""
    from core.repositories import container_repo

    outcome, conflicting = await container_repo.swap_gate_token_hash(
        "user_swap_missing", new_hash="hash_A", superseded=[], expected_prior_hash=None
    )

    assert (outcome, conflicting) == ("absent", None)
    assert await container_repo.get_by_owner_id("user_swap_missing") is None


@pytest.mark.asyncio
async def test_stamp_stop_reason_trial_idle_sets_parked_at(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_sr", {"status": "stopped"})
    await container_repo.stamp_stop_reason("user_sr", "trial_idle")
    row = await container_repo.get_by_owner_id("user_sr")
    assert row["stop_reason"] == "trial_idle"
    assert "parked_at" in row

    # A subsequent operator stop clears parked_at (no longer a healthy park).
    await container_repo.stamp_stop_reason("user_sr", "admin")
    row = await container_repo.get_by_owner_id("user_sr")
    assert row["stop_reason"] == "admin"
    assert "parked_at" not in row


# --- Daily owner-email send cap -------------------------------------------
#
# DURABLE by design: this is the only fence against an agent looping on the
# owner-send endpoint, and the backend runs several ECS tasks behind an ALB,
# so an in-process bucket would give a real limit of cap*task_count and reset
# on every deploy.


@pytest.mark.asyncio
async def test_daily_email_cap_allows_up_to_cap_then_blocks(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})

    for _ in range(3):
        assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=3) is True

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=3) is False


@pytest.mark.asyncio
async def test_daily_email_cap_resets_on_a_new_day(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is True
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is False
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-27", cap=1) is True


@pytest.mark.asyncio
async def test_daily_email_cap_is_per_owner(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.upsert("user_b", {"hook_token": "t"})

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is True
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is False
    # user_b's budget is untouched by user_a exhausting theirs.
    assert await container_repo.try_consume_daily_email_send("user_b", day="2026-07-26", cap=1) is True


@pytest.mark.asyncio
async def test_cold_wake_stamp_is_consumed_once(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    assert await container_repo.consume_email_cold_wake("user_a") is True
    # Second read must be False -- the footer appears exactly once.
    assert await container_repo.consume_email_cold_wake("user_a") is False


@pytest.mark.asyncio
async def test_cold_wake_stamp_expires(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    # A stale stamp must not attach a footer to an unrelated reply days later.
    assert await container_repo.consume_email_cold_wake("user_a", max_age_seconds=0) is False


@pytest.mark.asyncio
async def test_no_stamp_is_false_not_an_error(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    assert await container_repo.consume_email_cold_wake("user_a") is False


@pytest.mark.asyncio
async def test_consume_email_cold_wake_is_one_conditional_write(dynamodb_table):
    """Two concurrent consumers must not both see the stamp as fresh -- that
    would attach the "I was asleep" footer to two separate replies. Safety
    comes from ONE conditional write: the condition admits a single winner and
    the loser gets ConditionalCheckFailedException.

    This asserts that SHAPE rather than racing two callers, because racing them
    is not a sound test against moto: moto does not serialize conditional
    writes the way DynamoDB does, so both threads can pass
    attribute_exists(email_cold_wake_at) before either REMOVEs it and a
    CORRECT implementation fails intermittently. That flake was hit in CI.
    Real DynamoDB guarantees the single winner; what our code has to get right
    -- and what a read-then-remove regression would break -- is issuing one
    conditional write instead of a read followed by a write.
    """
    from unittest.mock import MagicMock, patch

    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    real_table = container_repo._get_table()
    spy = MagicMock(wraps=real_table)
    with patch.object(container_repo, "_get_table", return_value=spy):
        assert await container_repo.consume_email_cold_wake("user_a") is True

    # Exactly one write, and NO separate read: a get_item/query first would be
    # the read-then-remove shape this guards against.
    assert spy.update_item.call_count == 1
    assert spy.get_item.call_count == 0
    kwargs = spy.update_item.call_args.kwargs
    # The condition is what makes a single winner possible at all.
    assert "attribute_exists(email_cold_wake_at)" in kwargs["ConditionExpression"]
    # UPDATED_OLD is how the same write also READS the value it cleared --
    # without it we would need a second call and the race would be back.
    assert kwargs["ReturnValues"] == "UPDATED_OLD"


@pytest.mark.asyncio
async def test_consume_email_cold_wake_loser_reports_false(dynamodb_table):
    """The concurrent LOSER's half of the contract: a
    ConditionalCheckFailedException means another caller already consumed the
    stamp, so this caller must report False -- not raise, and not report a
    cold wake it did not win."""
    from unittest.mock import MagicMock, patch

    from botocore.exceptions import ClientError

    from core.repositories import container_repo

    spy = MagicMock()
    spy.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem"
    )
    with patch.object(container_repo, "_get_table", return_value=spy):
        assert await container_repo.consume_email_cold_wake("user_a") is False


@pytest.mark.asyncio
async def test_cold_wake_stamp_cleared_on_stale_path_too(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    assert await container_repo.consume_email_cold_wake("user_a", max_age_seconds=0) is False
    # The stamp must have been cleared even though it was judged stale --
    # otherwise it could resurface as "fresh" on a later, unrelated reply.
    assert await container_repo.consume_email_cold_wake("user_a") is False
