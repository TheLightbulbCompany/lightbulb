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
    row = dict(repo.DEFAULTS, enabled=True, sheet_closes_at="2026-09-30T12:00:00-04:00", updated_by="user_admin")
    saved = await repo.put(row)
    assert saved["enabled"] is True and saved["sheet_closes_at"] == "2026-09-30T12:00:00-04:00"
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
    assert fetched["stripe_coupon_id"] == "" and fetched["comp_slots"] == 200


async def test_put_always_writes_to_the_singleton_id(repo):
    await repo.put(dict(repo.DEFAULTS, id="something-else"))
    # A caller-supplied id must not fork the row — put() always writes id="settings".
    resp = repo._table().scan()
    assert len(resp["Items"]) == 1 and resp["Items"][0]["id"] == "settings"


async def test_get_returns_python_ints_not_dynamodb_decimals(repo):
    """DynamoDB's low-level deserializer hands every numeric attribute back
    as Decimal regardless of what type was put — regression coverage for a
    launch_phase.schedule() TypeError on Decimal arithmetic."""
    await repo.put(dict(repo.DEFAULTS, comp_slots=200))
    fetched = await repo.get()
    for field in ("comp_slots", "pot_front_cents", "pot_per_entrant_cents"):
        assert type(fetched[field]) is int, f"{field} came back as {type(fetched[field])}"
    assert type(fetched["enabled"]) is bool


def test_get_sync_returns_python_ints_not_dynamodb_decimals(repo):
    import asyncio

    asyncio.run(repo.put(dict(repo.DEFAULTS, comp_slots=200)))
    fetched = repo.get_sync()
    for field in ("comp_slots", "pot_front_cents", "pot_per_entrant_cents"):
        assert type(fetched[field]) is int, f"{field} came back as {type(fetched[field])}"


async def test_schedule_reads_a_real_saved_row_with_int_typed_fields(repo):
    """End-to-end: put a valid enabled row through moto, invalidate the
    launch_phase cache, and confirm schedule() derives a real Schedule with
    Python-int fields (not Decimals)."""
    from core.services import launch_phase
    from core.services.launch_phase import parse_iso

    row = dict(
        repo.DEFAULTS,
        enabled=True,
        sheet_closes_at="2026-09-30T12:00:00-04:00",
        competition_start_at="2026-10-01T00:00:00-04:00",
        competition_end_at="2026-12-01T00:00:00-05:00",
    )
    await repo.put(row)
    launch_phase.invalidate()
    try:
        s = launch_phase.schedule()
        assert s is not None
        assert type(s.comp_slots) is int
        assert s.competition_end_at == parse_iso("2026-12-01T00:00:00-05:00")
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


async def test_update_fields_sets_only_the_supplied_fields(repo):
    await repo.put(dict(repo.DEFAULTS, enabled=True, stripe_coupon_id="coup_1"))
    await repo.update_fields({"closed_at": "2026-09-30T16:00:00+00:00", "close_result": {"winners": 2}})
    row = await repo.get()
    assert row["closed_at"] == "2026-09-30T16:00:00+00:00"
    assert row["close_result"] == {"winners": 2}  # ints, not Decimals
    assert type(row["close_result"]["winners"]) is int
    assert row["enabled"] is True and row["stripe_coupon_id"] == "coup_1"  # untouched


async def test_update_fields_upserts_when_no_row_saved_yet(repo):
    await repo.update_fields({"close_started_at": "2026-09-30T15:59:00+00:00"})
    row = await repo.get()
    assert row["close_started_at"] == "2026-09-30T15:59:00+00:00"


async def test_update_fields_condition_makes_the_pot_freeze_write_once(repo):
    first = await repo.update_fields(
        {"final_pot_cents": 103_000, "final_roster_count": 3},
        condition="attribute_not_exists(final_pot_cents)",
    )
    assert first is True
    second = await repo.update_fields(
        {"final_pot_cents": 999_999, "final_roster_count": 99},
        condition="attribute_not_exists(final_pot_cents)",
    )
    assert second is False  # swallowed, not raised
    row = await repo.get()
    assert row["final_pot_cents"] == 103_000 and row["final_roster_count"] == 3  # first values kept


async def _freeze(repo, pot: int, roster: int) -> bool:
    """The close's (d) pot-freeze write, exactly as launch_close issues it."""
    return await repo.update_fields(
        {"final_pot_cents": pot, "final_roster_count": roster},
        condition=repo.POT_UNFROZEN,
        condition_values=repo.POT_UNFROZEN_VALUES,
    )


async def test_put_omits_none_valued_fields_instead_of_writing_dynamodb_nulls(repo):
    """The bug, at its source: DEFAULTS carries `final_pot_cents: None`, and a
    put that persisted it stored a NULL attribute — which EXISTS, so the pot
    freeze's attribute_not_exists condition then rejected every close."""
    await repo.put(dict(repo.DEFAULTS, enabled=True, updated_by="user_admin"))
    item = repo._table().get_item(Key={"id": "settings"})["Item"]
    for field in ("final_pot_cents", "final_roster_count", "closed_at", "close_started_at", "close_result"):
        assert field not in item, f"{field} was persisted as a DynamoDB NULL"
    # The caller's view is unchanged: every reader merges DEFAULTS, so an
    # absent attribute still reads as None (launch_settings.effective,
    # launch_phase._row).
    stored = await repo.get()
    assert "final_pot_cents" not in stored
    assert {**repo.DEFAULTS, **stored}["final_pot_cents"] is None


async def test_admin_settings_save_then_close_still_freezes_the_pot(repo):
    """The regression: the owner MUST save settings (to set the dates) before
    the close ever runs. That save must not disarm the freeze."""
    await repo.put(dict(repo.DEFAULTS, enabled=True, updated_by="user_admin"))
    assert await _freeze(repo, 103_000, 3) is True
    row = await repo.get()
    assert row["final_pot_cents"] == 103_000 and row["final_roster_count"] == 3


async def test_freeze_condition_tolerates_a_legacy_null_attribute(repo):
    """Rows saved before the put fix (dev's, and prod's if it was ever saved)
    already carry the NULL — they must still freeze exactly once."""
    repo._table().put_item(Item={"id": "settings", "enabled": True, "final_pot_cents": None})
    assert await _freeze(repo, 103_000, 3) is True
    assert (await repo.get())["final_pot_cents"] == 103_000
    # …and only once: the write-once guarantee survives the NULL tolerance.
    assert await _freeze(repo, 999_999, 99) is False
    assert (await repo.get())["final_pot_cents"] == 103_000


async def test_freeze_condition_never_overwrites_a_real_frozen_value(repo):
    await repo.put(dict(repo.DEFAULTS, final_pot_cents=103_000, final_roster_count=3))
    assert await _freeze(repo, 999_999, 99) is False
    row = await repo.get()
    assert row["final_pot_cents"] == 103_000 and row["final_roster_count"] == 3
