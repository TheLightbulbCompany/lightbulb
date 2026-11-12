"""Tests for container DynamoDB repository."""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


async def _seed(owner_id: str, fields: dict) -> dict:
    """Test seed: write a containers row directly, keeping an existing id/created_at."""
    from core.dynamodb import get_table

    table = get_table("containers")
    existing = table.get_item(Key={"owner_id": owner_id}).get("Item") or {}
    item = {
        **existing,
        "owner_id": owner_id,
        "id": existing.get("id") or str(uuid.uuid4()),
        "created_at": existing.get("created_at") or "2026-01-01T00:00:00+00:00",
        **fields,
    }
    table.put_item(Item=item)
    return item


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
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
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
async def test_browser_state_cas_fences_stale_writers_and_survives_row_rewrites(dynamodb_table):
    from core.repositories import container_repo

    await _seed("org_browser", {"status": "running"})
    first = {"generation": "first", "mode": "human"}
    second = {"generation": "second", "mode": "idle"}
    assert await container_repo.compare_browser_state("org_browser", None, first)
    assert not await container_repo.compare_browser_state("org_browser", None, second)
    assert await container_repo.compare_browser_state("org_browser", first, second)
    assert not await container_repo.compare_browser_state("org_browser", first, {"mode": "idle"})
    await _seed("org_browser", {"status": "running"})
    assert await container_repo.get_browser_state("org_browser") == second
    await container_repo.delete("org_browser")
    assert not await container_repo.compare_browser_state("org_browser", second, first)
    await _seed("org_browser", {"status": "running"})
    assert await container_repo.get_browser_state("org_browser") == second
    assert not await container_repo.compare_browser_state("org_missing", None, first)


@pytest.mark.asyncio
async def test_browser_cas_retries_a_transaction_conflict_and_never_raises_it(dynamodb_table):
    """Two writers on the lease row at one instant cancel with TransactionConflict (prod
    2026-09-27: five 503s). The loser retries; a row that stays contended reads as "changed"."""
    from core.repositories import container_repo

    await _seed("org_browser", {"status": "running"})
    table = container_repo._get_table()
    real = table.meta.client.transact_write_items
    conflict = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
        },
        "TransactWriteItems",
    )
    calls = []

    def once_then_real(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise conflict
        return real(**kwargs)

    with (
        patch.object(table.meta.client, "transact_write_items", side_effect=once_then_real),
        patch.object(container_repo, "_get_table", return_value=table),
    ):
        assert await container_repo.compare_browser_state("org_browser", None, {"generation": "g", "mode": "idle"})
    assert len(calls) == 2
    with (
        patch.object(table.meta.client, "transact_write_items", side_effect=conflict),
        patch.object(container_repo, "_get_table", return_value=table),
    ):
        assert not await container_repo.compare_browser_state("org_browser", None, {"mode": "idle"})


@pytest.mark.asyncio
async def test_browser_input_lives_beside_the_fenced_state(dynamodb_table):
    """A person's input timestamp must never change browser_state: every agent claim is a
    compare-and-set on that map, and the panel posts input every couple of seconds."""
    from core.repositories import container_repo

    await container_repo.touch_browser_input("org_browser", 100.0)  # no browser yet: no row
    assert await container_repo.get_browser_input_at("org_browser") is None
    await _seed("org_browser", {"status": "running"})
    state = {"generation": "g1", "mode": "agent"}
    assert await container_repo.compare_browser_state("org_browser", None, state)
    await container_repo.touch_browser_input("org_browser", 1790390000.25)
    assert await container_repo.get_browser_input_at("org_browser") == 1790390000.25
    assert await container_repo.compare_browser_state("org_browser", state, {**state, "mode": "idle"})


@pytest.mark.asyncio
async def test_browser_extensions_live_beside_the_fenced_state(dynamodb_table):
    """An admin's extension edit is its own compare-and-set: it never moves browser_state
    (an in-flight session claim) and never resurrects the row after a wipe."""
    from core.repositories import container_repo

    await _seed("org_browser", {"status": "running"})
    state = {"generation": "g1", "mode": "idle"}
    assert await container_repo.compare_browser_state("org_browser", None, state)
    one = [{"slug": "a", "key": "org_browser/browser-extensions/a.zip"}]
    assert await container_repo.get_browser_extensions("org_browser") is None
    assert await container_repo.compare_browser_extensions("org_browser", None, one)
    assert not await container_repo.compare_browser_extensions("org_browser", None, [])
    assert await container_repo.compare_browser_extensions("org_browser", one, [])
    assert await container_repo.compare_browser_extensions("org_browser", [], one)
    assert await container_repo.get_browser_extensions("org_browser") == one
    assert await container_repo.compare_browser_state("org_browser", state, {**state, "mode": "agent"})
    assert not await container_repo.compare_browser_extensions("org_missing", None, one)


@pytest.mark.asyncio
async def test_update_fields_preserves_provision_generation(dynamodb_table):
    """INV-2: restart/redeploy/hot-swap/revoke-restore/conversion all write
    through update_fields, never through upsert -- which is the single mint
    point for provision_generation. So an existing generation must survive any
    update_fields write, and the returned item must still carry it (callers read
    the generation back off this return, and ALL_NEW is what makes that hold)."""
    from core.repositories import container_repo

    await _seed(
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

    await _seed(
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
async def test_get_by_status(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"gateway_token": "t1", "status": "running"})
    await _seed("user_b", {"gateway_token": "t2", "status": "stopped"})
    await _seed("user_c", {"gateway_token": "t3", "status": "running"})

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

    await _seed("user_s", {"gateway_token": "ts", "status": "provisioning"})
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

    await _seed("user_f", {"gateway_token": "tf", "status": "running"})
    result = await container_repo.update_fields("user_f", {"task_arn": "arn:aws:ecs:task/123"})
    assert result is not None
    assert result["task_arn"] == "arn:aws:ecs:task/123"


@pytest.mark.asyncio
async def test_a_running_write_removes_the_previous_attempts_error(dynamodb_table):
    """A row that reaches running must not carry the error of an earlier failed attempt."""
    from core.repositories import container_repo

    await _seed(
        "user_e",
        {"status": "error", "last_error": "boom", "last_error_at": "2026-01-01T00:00:00Z", "retryable": False},
    )
    result = await container_repo.update_fields("user_e", {"status": "running", "agentcore_runtime_arn": "arn:rt"})
    assert result["status"] == "running"
    assert not {"last_error", "last_error_at", "retryable"} & set(result)


@pytest.mark.asyncio
async def test_delete(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_d", {"gateway_token": "td", "status": "running"})
    await container_repo.delete("user_d")
    item = await container_repo.get_by_owner_id("user_d")
    assert item is None


@pytest.mark.asyncio
async def test_delete_returns_the_row_as_it_was(dynamodb_table):
    """ALL_OLD. The row is the only handle on the owner's bedrock-gate token
    hashes, so an account teardown has to learn what it named at the instant it
    died — a read before the delete can miss a concurrent write."""
    from core.repositories import container_repo

    await _seed("user_all_old", {"gateway_token": "tao", "status": "running"})
    # swap_gate_token_hash is the only writer of these two fields.
    await container_repo.swap_gate_token_hash(
        "user_all_old",
        new_hash="hash-current",
        superseded=["hash-old"],
        expected_prior_hash=None,
    )

    deleted = await container_repo.delete("user_all_old")

    assert deleted is not None
    assert deleted["bedrock_gate_token_hash"] == "hash-current"
    assert deleted["bedrock_gate_superseded_hashes"] == ["hash-old"]
    # Idempotent, and says so: nothing was there the second time.
    assert await container_repo.delete("user_all_old") is None


@pytest.mark.asyncio
async def test_get_by_owner_id_alias(dynamodb_table):
    """get_by_owner_id returns the container for the given owner."""
    from core.repositories import container_repo

    await _seed(
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

    await _seed("owner_1", {"gateway_token": "t", "status": "running"})

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
async def test_concurrent_pairing_retries_a_transaction_conflict_instead_of_raising(dynamodb_table):
    """Load 3b (dev 2026-10-08): 25 Macs of one workspace paired at once and the owner-row
    UpdateItem raised TransactionConflictException -> 500. It now backs off and lands."""
    from core.repositories import container_repo

    await _seed("owner_1", {"status": "running"})
    real = container_repo.run_in_thread
    conflicts = iter([True, True])

    async def flaky(fn, *args, **kwargs):
        if "#did" in kwargs.get("ExpressionAttributeNames", {}) and next(conflicts, False):
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException", "Message": "Transaction is ongoing"}}, "UpdateItem"
            )
        return await real(fn, *args, **kwargs)

    sleep = AsyncMock()
    with patch.object(container_repo, "run_in_thread", flaky), patch("asyncio.sleep", sleep):
        await container_repo.upsert_paired_devices("owner_1", {"dev_a": {"publicKey": "ka", "userId": "u1"}})
    assert sleep.await_count == 2
    assert (await container_repo.get_by_owner_id("owner_1"))["paired_devices"]["dev_a"]["userId"] == "u1"


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

    await _seed("owner_org", {"gateway_token": "t"})
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

    await _seed("owner_1", {"gateway_token": "t", "status": "running"})

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

    await _seed("owner_1", {"gateway_token": "t"})
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
async def test_member_revocation_writes_spare_a_re_paired_device(dynamodb_table):
    from core.repositories import container_repo

    await _seed("owner_1", {"gateway_token": "t"})
    await container_repo.upsert_paired_devices(
        "owner_1", {"dev_a": {"userId": "u1"}, "dev_b": {"userId": "u1"}, "dev_c": {"userId": "u2"}}
    )
    assert await container_repo.mark_paired_device_revoking("owner_1", "dev_a", user_id="u1")
    assert await container_repo.mark_paired_device_revoking("owner_1", "dev_b", user_id="u1")
    assert not await container_repo.mark_paired_device_revoking("owner_1", "dev_c", user_id="u1")  # someone else's
    assert not await container_repo.mark_paired_device_revoking("owner_1", "ghost", user_id="u1")
    await container_repo.upsert_paired_devices("owner_1", {"dev_b": {"userId": "u1"}})  # re-paired: flag gone

    assert await container_repo.remove_revoked_paired_device("owner_1", "dev_a", "u1")
    assert not await container_repo.remove_revoked_paired_device("owner_1", "dev_b", "u1")
    assert not await container_repo.remove_revoked_paired_device("owner_1", "dev_c", "u1")
    row = await container_repo.get_by_owner_id("owner_1")
    assert set(row["paired_devices"]) == {"dev_b", "dev_c"}


def test_classify_provision_error_reuses_retryable_attr():
    """An exception's own ``.retryable`` is reused verbatim."""
    from core.repositories import container_repo

    class Err(Exception):
        def __init__(self, msg, retryable):
            super().__init__(msg)
            self.retryable = retryable

    assert container_repo.classify_provision_error(Err("bad slug", False))[1] is False
    assert container_repo.classify_provision_error(Err("down", True))[1] is True


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
async def test_update_fields_cannot_touch_the_gate_fields_at_all(dynamodb_table):
    """``update_fields`` needs no CAS because it names only the caller's fields.

    It used to be a whole-item put, so it rewrote the gate pointer — and every
    other field the snapshot carried — from a read taken milliseconds earlier.
    A swap landing after that read is now simply not part of the write.
    """
    from core.repositories import container_repo

    await _seed("user_scoped", {"status": "running"})
    await container_repo.swap_gate_token_hash("user_scoped", new_hash="hash_A", superseded=[], expected_prior_hash=None)

    real_run_in_thread = container_repo.run_in_thread

    async def racing_run_in_thread(fn, **kwargs):
        # A rival swap immediately BEFORE the write lands. The pre-scoped
        # version would have put hash_A straight back over it.
        if kwargs.get("UpdateExpression", "").startswith("SET #f0"):
            await container_repo.swap_gate_token_hash(
                "user_scoped", new_hash="hash_B", superseded=["hash_A"], expected_prior_hash="hash_A"
            )
        return await real_run_in_thread(fn, **kwargs)

    with patch.object(container_repo, "run_in_thread", racing_run_in_thread):
        merged = await container_repo.update_fields("user_scoped", {"status": "provisioning"})

    assert merged["status"] == "provisioning"
    row = await container_repo.get_by_owner_id("user_scoped")
    assert row["bedrock_gate_token_hash"] == "hash_B"
    assert row["bedrock_gate_superseded_hashes"] == ["hash_A"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["bedrock_gate_token_hash", "bedrock_gate_superseded_hashes"])
async def test_the_gate_fields_are_refused_from_every_other_writer(dynamodb_table, field):
    """ "Only swap_gate_token_hash writes these" is enforced, not just asserted.

    The CAS conditions on the POINTER, so a caller naming only the superseded
    list would sail through it and silently replace the list — every hash
    dropped from it being a live credential the kill switch can no longer reach.
    """
    from core.repositories import container_repo

    await _seed("user_reject", {"status": "running"})

    with pytest.raises(ValueError, match="swap_gate_token_hash"):
        await container_repo.update_fields("user_reject", {"status": "running", field: "x"})


@pytest.mark.asyncio
async def test_swap_gate_token_hash_writes_on_a_matching_prior(dynamodb_table):
    from core.billing import bedrock_gate_tokens
    from core.repositories import container_repo

    field = bedrock_gate_tokens._SUPERSEDED_FIELD
    await _seed("user_swap", {"status": "running"})
    await container_repo.swap_gate_token_hash("user_swap", new_hash="hash_A", superseded=[], expected_prior_hash=None)

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

    await _seed("user_swap_new", {"status": "provisioning"})

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
    await _seed("user_swap_race", {"status": "running"})
    await container_repo.swap_gate_token_hash(
        "user_swap_race", new_hash="hash_A", superseded=[], expected_prior_hash=None
    )
    await container_repo.swap_gate_token_hash(
        "user_swap_race", new_hash="hash_B", superseded=["hash_A"], expected_prior_hash="hash_A"
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
async def test_daily_email_cap_allows_up_to_cap_then_blocks(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"hook_token": "t"})

    for _ in range(3):
        assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=3) is True

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=3) is False


_DAY = "2026-09-24"


async def _wake(owner, sender, *, day=_DAY, owner_cap=50, sender_cap=10):
    from core.repositories import container_repo

    return await container_repo.try_consume_stranger_wake(
        owner, sender_key=sender, day=day, owner_cap=owner_cap, sender_cap=sender_cap
    )


@pytest.mark.asyncio
async def test_stranger_wake_is_capped_per_sender_and_a_new_sender_is_unaffected(dynamodb_table):
    await _seed("owner_a", {"hook_token": "t"})

    assert [await _wake("owner_a", "s1", sender_cap=3) for _ in range(4)] == [True, True, True, False]
    assert await _wake("owner_a", "s2", sender_cap=3) is True


@pytest.mark.asyncio
async def test_stranger_wake_is_capped_per_owner_across_senders(dynamodb_table):
    await _seed("owner_a", {"hook_token": "t"})

    assert [await _wake("owner_a", f"s{i}", owner_cap=3) for i in range(4)] == [True, True, True, False]


@pytest.mark.asyncio
async def test_stranger_wake_prefix_names_the_attributes(dynamodb_table):
    from core.repositories import container_repo

    await _seed("owner_a", {"hook_token": "t"})
    assert await container_repo.try_consume_stranger_wake(
        "owner_a", sender_key="s1", day=_DAY, owner_cap=5, sender_cap=5, attr_prefix="text_wake"
    )
    row = await container_repo.get_by_owner_id("owner_a", consistent=True)
    assert row["text_wake_count"] == 1 and row["text_wake_senders"] == {"s1": 1}
    assert "stranger_wake_count" not in row


@pytest.mark.asyncio
async def test_a_refused_stranger_wake_burns_neither_counter(dynamodb_table):
    """Both limits are one conditional write: refused under one, nothing is consumed under the other."""
    from core.repositories import container_repo

    await _seed("owner_a", {"hook_token": "t"})
    assert await _wake("owner_a", "s1", owner_cap=10, sender_cap=1) is True
    assert await _wake("owner_a", "s1", owner_cap=10, sender_cap=1) is False  # sender limit

    row = await container_repo.get_by_owner_id("owner_a", consistent=True)
    assert row["stranger_wake_count"] == 1 and row["stranger_wake_senders"] == {"s1": 1}


@pytest.mark.asyncio
async def test_stranger_wake_counters_reset_on_a_new_day_and_are_per_owner(dynamodb_table):
    await _seed("owner_a", {"hook_token": "t"})
    await _seed("owner_b", {"hook_token": "t"})

    assert await _wake("owner_a", "s1", sender_cap=1) is True
    assert await _wake("owner_a", "s1", sender_cap=1) is False
    assert await _wake("owner_a", "s1", sender_cap=1, day="2026-09-25") is True
    assert await _wake("owner_b", "s1", sender_cap=1) is True


@pytest.mark.asyncio
async def test_a_new_day_resets_an_under_cap_window_to_one(dynamodb_table):
    from core.repositories import container_repo

    await _seed("owner_a", {"hook_token": "t"})
    for _ in range(3):
        assert await _wake("owner_a", "s1") is True  # well under both caps
    assert await _wake("owner_a", "s2", day="2026-09-25") is True

    row = await container_repo.get_by_owner_id("owner_a", consistent=True)
    assert row["stranger_wake_day"] == "2026-09-25"
    assert row["stranger_wake_count"] == 1 and row["stranger_wake_senders"] == {"s2": 1}


@pytest.mark.asyncio
async def test_a_caller_holding_a_stale_day_cannot_wind_the_window_back(dynamodb_table):
    from core.repositories import container_repo

    await _seed("owner_a", {"hook_token": "t"})
    for _ in range(3):
        assert await _wake("owner_a", "s1", day="2026-09-25") is True

    # A task that read the clock just before midnight, arriving after another task rolled forward.
    assert await _wake("owner_a", "s1", day="2026-09-24") is False

    row = await container_repo.get_by_owner_id("owner_a", consistent=True)
    assert row["stranger_wake_day"] == "2026-09-25" and row["stranger_wake_count"] == 3


@pytest.mark.asyncio
async def test_concurrent_first_wakes_of_the_day_admit_exactly_the_sender_cap(dynamodb_table):
    """Racing rollovers: the loser of the rollover must still bump, not be refused.

    Serializes only Moto's own UpdateItem execution -- Moto does not support concurrent
    writes from worker threads (same gotcha as test_local_sessions.py's
    test_command_idempotency_ordering_and_receipt_provenance), so without this a run
    flakes with more than sender_cap admits. The race under test is untouched: each of
    try_consume_stranger_wake's two conditional writes is still a separate call other
    tasks can interleave around; only the individual write itself is now atomic, which
    is exactly what real DynamoDB already guarantees per item.
    """
    import asyncio

    from core.repositories import container_repo

    await _seed("owner_a", {"hook_token": "t"})

    lock = asyncio.Lock()
    real_run_in_thread = container_repo.run_in_thread

    async def locked_run_in_thread(*args, **kwargs):
        async with lock:
            return await real_run_in_thread(*args, **kwargs)

    with patch.object(container_repo, "run_in_thread", locked_run_in_thread):
        results = await asyncio.gather(*[_wake("owner_a", "s1", sender_cap=10) for _ in range(30)])

    assert sum(results) == 10


@pytest.mark.asyncio
async def test_stranger_wake_fails_closed_without_a_container_row(dynamodb_table):
    assert await _wake("owner_missing", "s1") is False


@pytest.mark.asyncio
async def test_stranger_wake_fails_closed_on_a_dynamodb_fault(monkeypatch):
    from core.repositories import container_repo

    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
    )
    monkeypatch.setattr(container_repo, "_get_table", lambda: table)

    assert await _wake("owner_a", "s1") is False


@pytest.mark.asyncio
async def test_daily_email_cap_resets_on_a_new_day(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"hook_token": "t"})

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is True
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is False
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-27", cap=1) is True


@pytest.mark.asyncio
async def test_daily_email_cap_is_per_owner(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"hook_token": "t"})
    await _seed("user_b", {"hook_token": "t"})

    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is True
    assert await container_repo.try_consume_daily_email_send("user_a", day="2026-07-26", cap=1) is False
    # user_b's budget is untouched by user_a exhausting theirs.
    assert await container_repo.try_consume_daily_email_send("user_b", day="2026-07-26", cap=1) is True


# --- Once-per-day platform notice claim -----------------------------------
#
# The notices this fences ride `send_to_owner(system_notice=True)`, which
# bypasses the daily send cap -- this claim is the only bound on them.


@pytest.mark.asyncio
async def test_daily_system_notice_claim_is_once_per_day(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"hook_token": "t"})

    assert await container_repo.try_claim_daily_system_notice("user_a", day="2026-07-26") is True
    assert await container_repo.try_claim_daily_system_notice("user_a", day="2026-07-26") is False
    assert await container_repo.try_claim_daily_system_notice("user_a", day="2026-07-27") is True


@pytest.mark.asyncio
async def test_daily_system_notice_claim_is_per_owner_and_per_kind(dynamodb_table):
    from core.repositories import container_repo

    await _seed("user_a", {"hook_token": "t"})
    await _seed("user_b", {"hook_token": "t"})

    assert await container_repo.try_claim_daily_system_notice("user_a", day="2026-07-26") is True
    # A different owner, and a different notice kind, each have their own slot.
    assert await container_repo.try_claim_daily_system_notice("user_b", day="2026-07-26") is True
    assert await container_repo.try_claim_daily_system_notice("user_a", day="2026-07-26", kind="other") is True


@pytest.mark.asyncio
async def test_ensure_owner_tokens_first_writer_wins_and_existing_tokens_are_kept(dynamodb_table):
    """Two provision runs each offer fresh tokens; the row keeps the first pair
    and both runs read that winner back. An owner who already has tokens (they
    are snapshotted into Paperclip adapters) is never rotated."""
    from core.repositories import container_repo

    await _seed("org_t", {"status": "provisioning"})

    first = await container_repo.ensure_owner_tokens("org_t", hook_token="h1", gateway_token="g1")
    second = await container_repo.ensure_owner_tokens("org_t", hook_token="h2", gateway_token="g2")

    assert (first["hook_token"], first["gateway_token"]) == ("h1", "g1")
    assert (second["hook_token"], second["gateway_token"]) == ("h1", "g1")
    # The row id seeds the gate bearer: both runs read back the SAME one, the creator's.
    assert first["id"] and second["id"] == first["id"]
    tokenless = await container_repo.ensure_owner_tokens("org_t", hook_token="x", gateway_token="y")
    assert tokenless["id"] == first["id"]

    with pytest.raises(RuntimeError, match="no containers row"):
        await container_repo.ensure_owner_tokens("org_missing", hook_token="h", gateway_token="g")
    assert await container_repo.get_by_owner_id("org_missing") is None


@pytest.mark.asyncio
async def test_ensure_owner_tokens_gives_an_id_less_row_one_stable_id(dynamodb_table):
    """A row created by a writer that sets no id still hands every run the same id."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_noid", "status": "provisioning"})
    a = await container_repo.ensure_owner_tokens("org_noid", hook_token="h", gateway_token="g")
    b = await container_repo.ensure_owner_tokens("org_noid", hook_token="h2", gateway_token="g2")
    assert a["id"] and a["id"] == b["id"]
