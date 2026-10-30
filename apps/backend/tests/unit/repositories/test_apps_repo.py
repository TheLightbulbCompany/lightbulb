import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def apps_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-apps",
            KeySchema=[{"AttributeName": "app_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("app_id", "owner_id", "name")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-index",
                    "KeySchema": [
                        {"AttributeName": "owner_id", "KeyType": "HASH"},
                        {"AttributeName": "name", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "name-index",
                    "KeySchema": [{"AttributeName": "name", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


@pytest.mark.asyncio
async def test_get_and_patch(apps_table):
    from core.repositories import apps_repo

    apps_table.put_item(Item={"app_id": "app_1", "name": "crew-schedule", "stripe_project_id": None})
    assert await apps_repo.get("app_missing") is None
    row = await apps_repo.patch("app_1", {"state": "live"})
    assert row["state"] == "live" and row["name"] == "crew-schedule" and row["updated_at"]
    assert (await apps_repo.get("app_1"))["state"] == "live"


@pytest.mark.asyncio
async def test_stripe_project_id_is_set_exactly_once(apps_table):
    """Two workers that both minted a Project: the first store wins, the
    second learns it lost and re-reads the winner."""
    from core.repositories import apps_repo

    # Task 7's create stores the field as None, so None counts as unset.
    apps_table.put_item(Item={"app_id": "app_1", "name": "crew-schedule", "stripe_project_id": None})
    assert await apps_repo.set_stripe_project_id("app_1", "proj_a") is True
    assert await apps_repo.set_stripe_project_id("app_1", "proj_b") is False
    assert (await apps_repo.get("app_1"))["stripe_project_id"] == "proj_a"
    # No record at all is a loss too, never an upsert.
    assert await apps_repo.set_stripe_project_id("app_missing", "proj_c") is False
    assert await apps_repo.get("app_missing") is None


def _new(repo, owner="org_a", name="crew-schedule", **kw):
    return repo.create(
        owner_id=owner,
        name=name,
        room_id="room_1",
        created_by=kw.get("created_by", "user_1"),
        actor_id="a1",
        actor_kind="agent",
        origin={"kind": "chat"},
        services=["vercel", "neon"],
        open_to="owner",
        budget_usd=20,
    )


@pytest.mark.asyncio
async def test_create_then_get_round_trips(apps_table):
    from core.repositories import apps_repo

    app = await _new(apps_repo, created_by=None)
    assert app["app_id"].startswith("app_") and app["state"] == "building"
    assert app["stripe_project_id"] is None and app["versions"] == [] and app["published_migrations"] == []
    assert app["destroy_requested_by"] is None and app["created_by"] is None
    assert app["budget_usd"] == 20 and app["created_at"] == app["updated_at"]
    assert await apps_repo.get(app["app_id"]) == app
    assert (await apps_repo.get_by_name("org_a", "crew-schedule"))["app_id"] == app["app_id"]
    assert await apps_repo.get_by_name("org_a", "nope") is None


@pytest.mark.asyncio
async def test_name_is_unique_across_owners(apps_table):
    from core.repositories import apps_repo

    await _new(apps_repo)
    with pytest.raises(apps_repo.NameTaken):
        await _new(apps_repo, owner="org_b")
    # the failed create left no half-made app behind
    assert await apps_repo.list_for_owner("org_b") == []
    assert apps_table.scan()["Count"] == 2  # app + NAME# guard


@pytest.mark.asyncio
async def test_add_version_appends_and_sets_live(apps_table):
    from core.repositories import apps_repo

    app = await _new(apps_repo)
    a = await apps_repo.add_version(app["app_id"], {"n": 1, "live": False})
    assert a["versions"] == [{"n": 1, "live": False}] and a["live_version"] is None
    b = await apps_repo.add_version(app["app_id"], {"n": 2, "live": True})
    assert [v["n"] for v in b["versions"]] == [1, 2] and b["live_version"] == 2


@pytest.mark.asyncio
async def test_list_for_owner_only_mine_and_delete_frees_name(apps_table):
    from core.repositories import apps_repo

    mine = await _new(apps_repo)
    await _new(apps_repo, owner="org_b", name="other")
    assert [a["app_id"] for a in await apps_repo.list_for_owner("org_a")] == [mine["app_id"]]
    await apps_repo.delete(mine["app_id"])
    assert await apps_repo.get(mine["app_id"]) is None
    assert apps_table.scan()["Count"] == 2  # other app + its guard only
    await _new(apps_repo, owner="org_b", name="crew-schedule")
