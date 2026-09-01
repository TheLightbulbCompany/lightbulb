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


async def test_resubmitting_updates_the_same_row_and_keeps_the_verdict(repos):
    teams, _ = repos
    first = await _submit(teams)
    await teams.update_fields("org_1", {"counted": True})
    second = await _submit(teams, team_name="Tuple Inc", seats=9)

    assert len(await teams.list_all()) == 1
    assert second["team_name"] == "Tuple Inc" and second["seats"] == 9
    assert second["submitted_at"] == first["submitted_at"]  # the original stamp survives
    assert second["counted"] is True  # a resubmission cannot un-review itself


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
