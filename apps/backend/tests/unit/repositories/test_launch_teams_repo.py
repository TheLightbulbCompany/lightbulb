"""Team-interest rows share the launch-settings table under a `team#` id
prefix. The load-bearing invariant is that the two never see each other."""

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
    exercises the REAL reads, against moto."""
    yield


@pytest.fixture
def repos():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_settings_repo, launch_teams_repo

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield launch_teams_repo, launch_settings_repo


def _submit(repo, owner_id="org_1", **over):
    fields = {
        "team_name": "Tuple",
        "seats": 4,
        "note": "six of us",
        "submitter_user_id": "user_t",
        "submitter_email": "founder@tuple.com",
        "submitter_org_slug": "tuple",
    }
    fields.update(over)
    return repo.put_interest(owner_id, **fields)


async def test_put_then_get_round_trips(repos):
    teams, _ = repos
    saved = await _submit(teams)
    assert saved["team_name"] == "Tuple" and saved["seats"] == 4
    assert saved["counted"] is False  # never counted until a founder says so
    assert "id" not in saved  # the prefixed key is an implementation detail
    assert await teams.get("org_1") == saved


async def test_resubmitting_an_unreviewed_row_updates_it_in_place(repos):
    teams, _ = repos
    first = await _submit(teams)
    second = await _submit(teams, team_name="Tuple Inc", seats=9)

    assert len(await teams.list_all()) == 1
    assert second["team_name"] == "Tuple Inc" and second["seats"] == 9
    assert second["submitted_at"] == first["submitted_at"]  # the original stamp survives


async def test_a_counted_rows_seat_count_is_pinned_against_its_own_submitter(repos):
    """The founders' verdict is ABOUT a number. If a resubmission could still
    move `seats`, a team counted at 5 could resubmit at 500 and the close
    would freeze the pot with $2,510 nobody approved."""
    teams, _ = repos
    await _submit(teams, seats=5)
    await teams.update_fields("org_1", {"counted": True})

    after = await _submit(teams, team_name="Tuple Inc", seats=500, note="grew a lot")

    assert after["seats"] == 5  # pinned
    assert after["counted"] is True  # and still counted
    # The edit is not thrown away — everything BUT the number lands.
    assert after["team_name"] == "Tuple Inc" and after["note"] == "grew a lot"
    # …which is what the pot actually reads.
    counted = [r["seats"] for r in await teams.list_all() if r["counted"]]
    assert counted == [5]


async def test_an_admin_can_still_change_a_counted_rows_seats(repos):
    """The pin is on the SUBMITTER's path only — correcting a reviewed team is
    exactly what the admin edit is for."""
    teams, _ = repos
    await _submit(teams, seats=5)
    await teams.update_fields("org_1", {"counted": True})
    row = await teams.update_fields("org_1", {"seats": 8})
    assert row["seats"] == 8 and row["counted"] is True


async def test_a_resubmission_moves_updated_at_so_a_post_review_edit_is_visible(repos):
    teams, _ = repos
    first = await _submit(teams, seats=5)
    await teams.update_fields("org_1", {"counted": True})
    after = await _submit(teams, team_name="Tuple Inc", seats=500)
    # submitted_at is frozen by design, so updated_at is the only signal the
    # admin card can show that someone edited after review.
    assert after["submitted_at"] == first["submitted_at"]
    assert after["updated_at"] >= first["updated_at"]


async def test_update_fields_on_a_missing_row_returns_none(repos):
    teams, _ = repos
    assert await teams.update_fields("org_nope", {"counted": True}) is None


async def test_the_settings_row_and_the_team_rows_never_see_each_other(repos):
    teams, settings_repo = repos
    await settings_repo.put(dict(settings_repo.DEFAULTS, enabled=True, comp_slots=42))
    await _submit(teams, owner_id="org_1")
    await _submit(teams, owner_id="org_2", team_name="Pair")

    # The team scan skips the settings row…
    listed = await teams.list_all()
    assert sorted(r["team_name"] for r in listed) == ["Pair", "Tuple"]
    # …and the settings read is untouched by the team rows.
    row = await settings_repo.get()
    assert row["comp_slots"] == 42 and "team_name" not in row
    assert settings_repo.get_sync()["comp_slots"] == 42
