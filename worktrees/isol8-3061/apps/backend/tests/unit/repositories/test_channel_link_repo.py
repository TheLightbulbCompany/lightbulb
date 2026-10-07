"""Tests for channel_link DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB channel-links table with the by-member GSI."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-channel-links",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "member_id", "AttributeType": "S"},
                {"AttributeName": "owner_provider_agent", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-member",
                    "KeySchema": [
                        {"AttributeName": "member_id", "KeyType": "HASH"},
                        {"AttributeName": "owner_provider_agent", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-channel-links")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_put_and_get_by_peer(dynamodb_table):
    from core.repositories import channel_link_repo

    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="99999",
        member_id="user_bob",
        linked_via="settings",
    )
    link = await channel_link_repo.get_by_peer(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="99999",
    )
    assert link is not None
    assert link["member_id"] == "user_bob"
    assert link["linked_via"] == "settings"
    assert link["provider"] == "telegram"
    assert link["agent_id"] == "sales"
    assert link["peer_id"] == "99999"


@pytest.mark.asyncio
async def test_get_by_peer_miss_returns_none(dynamodb_table):
    from core.repositories import channel_link_repo

    result = await channel_link_repo.get_by_peer(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="00000",
    )
    assert result is None


@pytest.mark.asyncio
async def test_query_by_member_across_orgs(dynamodb_table):
    from core.repositories import channel_link_repo

    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
        peer_id="111",
        member_id="user_bob",
        linked_via="wizard",
    )
    await channel_link_repo.put(
        owner_id="org_2",
        provider="discord",
        agent_id="main",
        peer_id="222",
        member_id="user_bob",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
        peer_id="333",
        member_id="user_alice",
        linked_via="wizard",
    )

    bob_links = await channel_link_repo.query_by_member("user_bob")
    assert len(bob_links) == 2
    orgs = {link["owner_id"] for link in bob_links}
    assert orgs == {"org_1", "org_2"}


@pytest.mark.asyncio
async def test_delete_link(dynamodb_table):
    from core.repositories import channel_link_repo

    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="99999",
        member_id="user_bob",
        linked_via="settings",
    )
    await channel_link_repo.delete(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="99999",
    )
    result = await channel_link_repo.get_by_peer(
        owner_id="org_1",
        provider="telegram",
        agent_id="sales",
        peer_id="99999",
    )
    assert result is None


@pytest.mark.asyncio
async def test_query_by_owner(dynamodb_table):
    from core.repositories import channel_link_repo

    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
        peer_id="111",
        member_id="user_a",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_1",
        provider="discord",
        agent_id="sales",
        peer_id="222",
        member_id="user_b",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_2",
        provider="telegram",
        agent_id="main",
        peer_id="333",
        member_id="user_a",
        linked_via="settings",
    )

    rows = await channel_link_repo.query_by_owner("org_1")
    assert len(rows) == 2
    assert {r["provider"] for r in rows} == {"telegram", "discord"}


@pytest.mark.asyncio
async def test_sweep_by_owner_provider_agent(dynamodb_table):
    from core.repositories import channel_link_repo

    # Seed: two bots with members linked
    for peer in ["111", "222", "333"]:
        await channel_link_repo.put(
            owner_id="org_1",
            provider="telegram",
            agent_id="main",
            peer_id=peer,
            member_id=f"user_{peer}",
            linked_via="settings",
        )
    for peer in ["444", "555"]:
        await channel_link_repo.put(
            owner_id="org_1",
            provider="telegram",
            agent_id="sales",
            peer_id=peer,
            member_id=f"user_{peer}",
            linked_via="settings",
        )

    # Sweep only the main-agent bot
    count = await channel_link_repo.sweep_by_owner_provider_agent(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
    )
    assert count == 3

    # main is gone, sales is intact
    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_1",
            provider="telegram",
            agent_id="main",
            peer_id="111",
        )
        is None
    )
    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_1",
            provider="telegram",
            agent_id="sales",
            peer_id="444",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_sweep_by_owner_provider_agent_cross_provider_isolation(dynamodb_table):
    """Sweeping telegram#main must not touch discord#main (same owner, same agent_id)."""
    from core.repositories import channel_link_repo

    # Same agent_id "main" on two providers under one owner
    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
        peer_id="t-111",
        member_id="user_a",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
        peer_id="t-222",
        member_id="user_b",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_1",
        provider="discord",
        agent_id="main",
        peer_id="d-333",
        member_id="user_a",
        linked_via="settings",
    )

    count = await channel_link_repo.sweep_by_owner_provider_agent(
        owner_id="org_1",
        provider="telegram",
        agent_id="main",
    )
    assert count == 2

    # Telegram main is gone
    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_1",
            provider="telegram",
            agent_id="main",
            peer_id="t-111",
        )
        is None
    )
    # Discord main is intact (same agent_id but different provider)
    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_1",
            provider="discord",
            agent_id="main",
            peer_id="d-333",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_sweep_by_owner(dynamodb_table):
    from core.repositories import channel_link_repo

    # Seed across two orgs
    await channel_link_repo.put(
        owner_id="org_a",
        provider="telegram",
        agent_id="main",
        peer_id="111",
        member_id="user_1",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_a",
        provider="discord",
        agent_id="main",
        peer_id="222",
        member_id="user_1",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_b",
        provider="telegram",
        agent_id="main",
        peer_id="333",
        member_id="user_1",
        linked_via="settings",
    )

    count = await channel_link_repo.sweep_by_owner("org_a")
    assert count == 2

    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_a",
            provider="telegram",
            agent_id="main",
            peer_id="111",
        )
        is None
    )
    # org_b untouched
    assert (
        await channel_link_repo.get_by_peer(
            owner_id="org_b",
            provider="telegram",
            agent_id="main",
            peer_id="333",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_sweep_by_member(dynamodb_table):
    from core.repositories import channel_link_repo

    # Bob is in two orgs
    await channel_link_repo.put(
        owner_id="org_a",
        provider="telegram",
        agent_id="main",
        peer_id="111",
        member_id="user_bob",
        linked_via="settings",
    )
    await channel_link_repo.put(
        owner_id="org_b",
        provider="discord",
        agent_id="main",
        peer_id="222",
        member_id="user_bob",
        linked_via="settings",
    )
    # Alice is in one
    await channel_link_repo.put(
        owner_id="org_a",
        provider="telegram",
        agent_id="main",
        peer_id="333",
        member_id="user_alice",
        linked_via="settings",
    )

    count = await channel_link_repo.sweep_by_member("user_bob")
    assert count == 2

    # Bob's rows gone, Alice untouched
    assert len(await channel_link_repo.query_by_member("user_bob")) == 0
    assert len(await channel_link_repo.query_by_member("user_alice")) == 1
