import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-settings"


@pytest.fixture(autouse=True)
def _launch_settings_read_defaults():
    """Override the global autouse default in tests/conftest.py — this module
    is the one that exercises the REAL read, against moto."""
    yield


@pytest.fixture
def repo():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_settings_repo as repo

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield repo


async def test_get_returns_none_when_no_row_saved_yet(repo):
    assert await repo.get() is None


def test_get_sync_returns_none_when_no_row_saved_yet(repo):
    assert repo.get_sync() is None


async def test_put_then_get_round_trips(repo):
    row = dict(repo.DEFAULTS, enabled=True, drop_at="2026-09-18T00:00:00-04:00", updated_by="user_admin")
    saved = await repo.put(row)
    assert saved["enabled"] is True and saved["drop_at"] == "2026-09-18T00:00:00-04:00"
    assert saved["updated_by"] == "user_admin"
    assert "id" not in saved  # the partition key is an implementation detail, not part of the row

    fetched = await repo.get()
    assert fetched == saved


def test_put_then_get_sync_round_trips(repo):
    import asyncio

    row = dict(repo.DEFAULTS, comp_slots=500)
    asyncio.run(repo.put(row))
    fetched = repo.get_sync()
    assert fetched["comp_slots"] == 500
    assert "id" not in fetched


async def test_put_stamps_updated_at(repo):
    row = dict(repo.DEFAULTS)
    assert "updated_at" not in row
    saved = await repo.put(row)
    assert saved["updated_at"]


async def test_put_is_a_full_replace(repo):
    """A second put with a smaller payload must not leave fields from the
    first put behind — put() replaces the whole item, it doesn't merge."""
    first = dict(repo.DEFAULTS, stripe_coupon_id="promo_old", comp_slots=300)
    await repo.put(first)

    second = dict(repo.DEFAULTS)  # stripe_coupon_id defaults back to ""
    await repo.put(second)

    fetched = await repo.get()
    assert fetched["stripe_coupon_id"] == "" and fetched["comp_slots"] == 220


async def test_put_always_writes_to_the_singleton_id(repo):
    await repo.put(dict(repo.DEFAULTS, id="something-else"))
    # A caller-supplied id must not fork the row — put() always writes id="settings".
    resp = repo._table().scan()
    assert len(resp["Items"]) == 1 and resp["Items"][0]["id"] == "settings"


async def test_get_returns_python_ints_not_dynamodb_decimals(repo):
    """DynamoDB's low-level deserializer hands every numeric attribute back
    as Decimal regardless of what type was put — regression coverage for the
    launch_phase.schedule() TypeError this caused at timedelta(days=Decimal(...))."""
    await repo.put(dict(repo.DEFAULTS, comp_slots=220, competition_days=30))
    fetched = await repo.get()
    for field in ("comp_slots", "competition_days", "pot_front_cents", "pot_per_entrant_cents"):
        assert type(fetched[field]) is int, f"{field} came back as {type(fetched[field])}"
    assert type(fetched["enabled"]) is bool


def test_get_sync_returns_python_ints_not_dynamodb_decimals(repo):
    import asyncio

    asyncio.run(repo.put(dict(repo.DEFAULTS, comp_slots=220, competition_days=30)))
    fetched = repo.get_sync()
    for field in ("comp_slots", "competition_days", "pot_front_cents", "pot_per_entrant_cents"):
        assert type(fetched[field]) is int, f"{field} came back as {type(fetched[field])}"


async def test_schedule_reads_a_real_saved_row_with_int_typed_fields(repo):
    """End-to-end: put a valid enabled row through moto, invalidate the
    launch_phase cache, and confirm schedule() derives a real Schedule with
    Python-int fields (not Decimals) and the right derived math."""
    from datetime import timedelta

    from core.services import launch_phase

    row = dict(
        repo.DEFAULTS,
        enabled=True,
        drop_at="2026-09-18T00:00:00-04:00",
        competition_start_at="2026-09-21T00:00:00-04:00",
    )
    await repo.put(row)
    launch_phase.invalidate()
    try:
        s = launch_phase.schedule()
        assert s is not None
        assert type(s.comp_slots) is int and type(s.competition_days) is int
        assert s.competition_end_at == s.competition_start_at + timedelta(days=30)
    finally:
        launch_phase.invalidate()


async def test_enabled_string_false_reads_back_as_false(repo):
    """A hand-edited row storing the string "false" must not enable the
    launch — `bool("false")` is True in Python, so the coercion has to be a
    strict `is True` check, not a truthy cast."""
    saved = await repo.put(dict(repo.DEFAULTS, enabled="false"))
    assert saved["enabled"] is False
    fetched = await repo.get()
    assert fetched["enabled"] is False


async def test_enabled_int_one_reads_back_as_false(repo):
    saved = await repo.put(dict(repo.DEFAULTS, enabled=1))
    assert saved["enabled"] is False
    fetched = await repo.get()
    assert fetched["enabled"] is False


def test_get_sync_reads_through_a_client_with_bounded_timeouts():
    """get_sync runs on the container-status poll path and blocks the caller
    while it waits — botocore's 60s defaults would park every poll behind a
    single unreachable table."""
    from unittest.mock import MagicMock

    from core.repositories import launch_settings_repo as r

    resource = MagicMock()
    resource.Table.return_value.get_item.return_value = {}
    with patch.object(r, "_sync_resource", None), patch.object(r, "boto3") as boto3_mod:
        boto3_mod.resource.return_value = resource
        assert r.get_sync() is None
        cfg = boto3_mod.resource.call_args.kwargs["config"]
    assert cfg.connect_timeout == 2 and cfg.read_timeout == 2
    assert cfg.retries["max_attempts"] == 2
