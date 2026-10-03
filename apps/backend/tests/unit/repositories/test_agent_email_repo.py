"""agent_email_repo: the workspace's one mailbox row, address-keyed with a by-owner GSI."""

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import agent_email_repo as repo  # noqa: E402


@pytest.fixture
def agent_email_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-agent-email-addresses",
            KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "address", "AttributeType": "S"},
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
        table.meta.client.get_waiter("table_exists").wait(TableName="test-agent-email-addresses")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_put_normalizes_and_get_by_address(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="Acme@Agents.Isol8.co",
        owner_id="user_1",
        verified_senders=["Owner@Example.com"],
    )

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row is not None
    assert row["owner_id"] == "user_1"
    assert "agent_id" not in row
    assert row["verified_senders"] == ["owner@example.com"]
    assert "public" not in row
    assert row["status"] == "active"
    assert row["created_at"]


@pytest.mark.asyncio
async def test_put_mints_open_rows_and_the_owner_can_close_them(agent_email_table):
    """Owner ruling 2026-09-24: agent email is open by default in both directions."""
    await repo.put(address="acme@agents.isol8.co", owner_id="user_1", verified_senders=[])

    assert (await repo.get_by_address("acme@agents.isol8.co"))["send_to_anyone"] is True

    await repo.update_settings("acme@agents.isol8.co", send_to_anyone=False)
    assert (await repo.get_by_address("acme@agents.isol8.co"))["send_to_anyone"] is False


@pytest.mark.asyncio
async def test_get_by_address_missing_returns_none(agent_email_table):
    from core.repositories import agent_email_repo

    assert await agent_email_repo.get_by_address("nope@agents.isol8.co") is None


@pytest.mark.asyncio
async def test_get_by_address_is_case_insensitive_on_read(agent_email_table):
    """Stored address is already lowercase; the query casing must still normalize.

    Regression guard for dropping `normalize()` from `get_by_address`'s
    `Key=` construction -- a mutation that every other test here misses
    because they all query with an already-lowercase address.
    """
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    row = await agent_email_repo.get_by_address("AcMe@Agents.Isol8.CO")
    assert row is not None
    assert row["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_get_by_address_uses_consistent_read(agent_email_table, monkeypatch):
    """`ensure_team_address` reads the row right before and after a create; an
    eventual miss would report a failed mint. GetItem supports ConsistentRead."""
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    calls = []
    real_get_item = agent_email_table.get_item

    def spy_get_item(**kwargs):
        calls.append(kwargs)
        return real_get_item(**kwargs)

    class _TableSpy:
        def get_item(self, **kwargs):
            return spy_get_item(**kwargs)

    monkeypatch.setattr(agent_email_repo, "_get_table", lambda: _TableSpy())

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row is not None
    assert len(calls) == 1
    assert calls[0].get("ConsistentRead") is True


@pytest.mark.asyncio
async def test_put_never_overwrites_an_existing_row(agent_email_table):
    """A create, not an upsert: a racing second create (same owner or not) must
    not reset the owner's settings."""
    from core.repositories import agent_email_repo

    first = await agent_email_repo.put(address="acme@agents.isol8.co", owner_id="user_1", verified_senders=[])
    await agent_email_repo.update_settings("acme@agents.isol8.co", send_to_anyone=False)
    second = await agent_email_repo.put(
        address="acme@agents.isol8.co", owner_id="user_1", verified_senders=["owner@example.com"]
    )

    assert first is not None
    assert second is None
    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row["send_to_anyone"] is False
    assert row["verified_senders"] == []


@pytest.mark.asyncio
async def test_put_rejects_and_returns_none_for_a_conflicting_owner(agent_email_table):
    """The conditional put must reject a different owner's write rather than
    last-writer-wins clobbering the existing row's owner_id."""
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    result = await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_2",
        verified_senders=["intruder@example.com"],
    )
    assert result is None

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row["owner_id"] == "user_1"
    assert row["verified_senders"] == []


@pytest.mark.asyncio
async def test_query_by_owner_returns_only_that_owner(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="acme-atlas@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="other@agents.isol8.co",
        owner_id="user_2",
        verified_senders=[],
    )

    rows = await agent_email_repo.query_by_owner("user_1")
    assert {r["address"] for r in rows} == {
        "acme@agents.isol8.co",
        "acme-atlas@agents.isol8.co",
    }


@pytest.mark.asyncio
async def test_update_settings_sets_only_supplied_fields(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c", "send_to_anyone": True}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    out = await repo.update_settings("A@b.c", send_to_anyone=True)
    assert out["send_to_anyone"] is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "a@b.c"}
    assert "send_to_anyone" in kwargs["UpdateExpression"]
    assert "verified_senders" not in kwargs["UpdateExpression"]
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"


@pytest.mark.asyncio
async def test_update_settings_none_removes_outbound_disabled(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c"}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.update_settings("a@b.c", outbound_disabled=None)
    assert "REMOVE outbound_disabled" in table.update_item.call_args.kwargs["UpdateExpression"]


@pytest.mark.asyncio
async def test_update_settings_missing_row_returns_none(monkeypatch):
    table = MagicMock()
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.update_settings("a@b.c", send_to_anyone=False) is None


@pytest.mark.asyncio
async def test_put_has_no_public_field(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.put(address="a@b.c", owner_id="o", verified_senders=["O@x.y"])
    assert "public" not in item
    assert item["verified_senders"] == ["o@x.y"]
