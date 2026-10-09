"""slack_installs_repo: team_id-keyed rows with a by-owner GSI.

Mirrors test_agent_email_repo — moto table + prefix/resource patches.
"""

import os

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import slack_installs_repo as repo  # noqa: E402


@pytest.fixture
def installs_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-slack-installs",
            KeySchema=[{"AttributeName": "team_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "team_id", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-slack-installs")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


def _put(**overrides):
    base = dict(
        team_id="T0123ABC",
        owner_id="org_1",
        agent_id="agent_friday",
        team_name="Acme",
        bot_user_id="U_BOT",
        app_id="A_APP",
        scopes=["chat:write", "app_mentions:read"],
        installer_slack_user_id="U_ADMIN",
        installer_member_id="user_1",
        token_secret_name="lightbulb/dev/integrations/org_1/slack/T0123ABC",
    )
    base.update(overrides)
    return repo.put(**base)


@pytest.mark.asyncio
async def test_put_then_get(installs_table):
    row = await _put()
    assert row["status"] == "active"
    got = await repo.get("T0123ABC")
    assert got["owner_id"] == "org_1"
    assert got["agent_id"] == "agent_friday"
    assert got["scopes"] == ["chat:write", "app_mentions:read"]


@pytest.mark.asyncio
async def test_same_owner_reinstall_overwrites(installs_table):
    await _put()
    row = await _put(bot_user_id="U_BOT2")
    assert row is not None
    assert (await repo.get("T0123ABC"))["bot_user_id"] == "U_BOT2"


@pytest.mark.asyncio
async def test_other_owner_cannot_hijack_active_install(installs_table):
    await _put(owner_id="org_1")
    assert await _put(owner_id="org_2") is None
    assert (await repo.get("T0123ABC"))["owner_id"] == "org_1"


@pytest.mark.asyncio
async def test_other_owner_can_claim_after_revoke(installs_table):
    await _put(owner_id="org_1")
    await repo.set_status("T0123ABC", "revoked")
    row = await _put(owner_id="org_2")
    assert row is not None and row["owner_id"] == "org_2"
    assert (await repo.get("T0123ABC"))["status"] == "active"


@pytest.mark.asyncio
async def test_list_for_owner_and_delete_all(installs_table):
    await _put(team_id="T1", owner_id="org_1")
    await _put(team_id="T2", owner_id="org_1")
    await _put(team_id="T3", owner_id="org_other")
    assert {r["team_id"] for r in await repo.list_for_owner("org_1")} == {"T1", "T2"}
    deleted = await repo.delete_all_for_owner("org_1")
    assert sorted(deleted) == ["T1", "T2"]
    assert await repo.get("T1") is None
    assert await repo.get("T3") is not None


@pytest.mark.asyncio
async def test_get_missing_is_none(installs_table):
    assert await repo.get("T_NOPE") is None


@pytest.mark.asyncio
async def test_a_revoke_for_the_wrong_owner_leaves_the_row_alone(installs_table):
    """The owner changed between revoke-all's pre-check and its revoke: the conditional write fails
    and the other owner's active row is untouched."""
    await _put(owner_id="org_2")
    assert await repo.set_status("T0123ABC", "revoked", expected_owner_id="org_1") is False
    assert (await repo.get("T0123ABC"))["status"] == "active"
    assert await repo.set_status("T0123ABC", "revoked", expected_owner_id="org_2") is True
    assert (await repo.get("T0123ABC"))["status"] == "revoked"
    # Already revoked: not "ours and active" any more.
    assert await repo.set_status("T0123ABC", "revoked", expected_owner_id="org_2") is False


@pytest.mark.asyncio
async def test_set_status_on_unknown_team_creates_nothing(installs_table):
    await repo.set_status("T_NOPE", "revoked")
    assert await repo.get("T_NOPE") is None
