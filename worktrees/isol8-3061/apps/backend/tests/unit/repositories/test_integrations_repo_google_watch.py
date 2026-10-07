"""Tests for the google_watch_channels helpers on integrations_repo — the
gmail-watch entries the trigger proxy's OIDC bearer lookup reads live as a
list attribute on the owner's existing (owner_id, "google") status row, not
in a table of its own."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto integrations table (owner_id PK + provider SK)."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-integrations",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "provider", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "provider", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-integrations")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _entry(channel_id="chan_1", expiration_ms=1000):
    return {
        "channel_id": channel_id,
        "provider_event": "calendar.changed",
        "account_email": "ada@example.com",
        "resource_id": "res_1",
        "trigger_public_id": "pub_1",
        "proxy_url": "https://api/hooks/t/org_1/pub_1",
        "channel_token": "pcsec_1",
        "expiration_ms": expiration_ms,
    }


@pytest.mark.asyncio
async def test_get_returns_empty_when_no_row(dynamodb_table):
    from core.repositories import integrations_repo

    assert await integrations_repo.get_google_watch_channels("org_1") == []


@pytest.mark.asyncio
async def test_set_and_get_roundtrip_preserves_status_fields(dynamodb_table):
    """The channels attr rides the EXISTING connected-status row — writing it
    must not clobber the status the connect path recorded."""
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_1", "google")
    await integrations_repo.set_google_watch_channels("org_1", [_entry()])

    channels = await integrations_repo.get_google_watch_channels("org_1")
    assert len(channels) == 1
    assert channels[0]["channel_id"] == "chan_1"
    assert channels[0]["channel_token"] == "pcsec_1"
    assert int(channels[0]["expiration_ms"]) == 1000

    row = await integrations_repo.get("org_1", "google")
    assert row["status"] == "connected"  # survived the channels write


@pytest.mark.asyncio
async def test_set_overwrites_the_list(dynamodb_table):
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_1", "google")
    await integrations_repo.set_google_watch_channels("org_1", [_entry("chan_1")])
    await integrations_repo.set_google_watch_channels("org_1", [_entry("chan_2"), _entry("chan_3")])

    channels = await integrations_repo.get_google_watch_channels("org_1")
    assert [c["channel_id"] for c in channels] == ["chan_2", "chan_3"]


@pytest.mark.asyncio
async def test_channels_do_not_leak_between_owners(dynamodb_table):
    from core.repositories import integrations_repo

    await integrations_repo.set_google_watch_channels("org_1", [_entry("chan_1")])
    assert await integrations_repo.get_google_watch_channels("org_2") == []
