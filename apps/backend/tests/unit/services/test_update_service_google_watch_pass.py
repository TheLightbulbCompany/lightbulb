"""Tests for the update_service scheduled worker — the Google watch renewal
pass (auto_google_watch triggers).

Channel state lives as the ``google_watch_channels`` list attribute on each
owner's (owner_id, "google") integrations row, so these tests read and write
that row through a real (moto) integrations table; only the gog layer
(``google_watch.start_watch`` / ``stop_watch``) is mocked.
``asyncio_mode = "auto"`` is in effect.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import update_service  # noqa: E402


@pytest.fixture
def dynamodb_table():
    """A moto integrations table (owner_id PK + provider SK)."""
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


def _entry(channel_id="chan_old", *, expiration_ms=1_000, provider_event="calendar.changed") -> dict:
    return {
        "channel_id": channel_id,
        "provider_event": provider_event,
        "account_email": "ada@example.com",
        "resource_id": f"res_{channel_id}",
        "trigger_public_id": "pub_1",
        "proxy_url": "https://api/hooks/t/org_1/pub_1",
        "channel_token": "pcsec_1",
        "expiration_ms": expiration_ms,
    }


# Comfortably beyond the pass's 48h lookahead from any wall clock this test
# runs at (year ~2500).
_FAR_FUTURE_MS = 16_725_225_600_000


def _replacement(channel_id="chan_new", **over) -> dict:
    repl = {"channel_id": channel_id, "resource_id": f"res_{channel_id}", "expiration_ms": _FAR_FUTURE_MS}
    repl.update(over)
    return repl


async def _seed_owner(owner_id: str, entries: list[dict]) -> None:
    from core.repositories import integrations_repo

    await integrations_repo.put_status(owner_id, "google")
    await integrations_repo.set_google_watch_channels(owner_id, entries)


async def _channels(owner_id: str) -> list[dict]:
    from core.repositories import integrations_repo

    return await integrations_repo.get_google_watch_channels(owner_id)


async def test_renewal_pass_renews_expiring_channel(dynamodb_table):
    """The happy path: re-watch with the entry's own account/event/proxy/token
    (a fresh channel id comes from the watch itself), replace the entry on the
    integrations row, then best-effort stop the old channel."""
    await _seed_owner("org_1", [_entry("chan_old")])
    start = AsyncMock(return_value=_replacement("chan_new"))
    stop = AsyncMock()
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", stop),
    ):
        await update_service._google_watch_renewal_pass()

    start.assert_awaited_once_with(
        owner_id="org_1",
        account_email="ada@example.com",
        provider_event="calendar.changed",
        proxy_url="https://api/hooks/t/org_1/pub_1",
        channel_token="pcsec_1",  # SAME token — the trigger's secret is stable
    )
    stop.assert_awaited_once_with(
        owner_id="org_1",
        account_email="ada@example.com",
        provider_event="calendar.changed",
        channel_id="chan_old",
        resource_id="res_chan_old",
    )

    (entry,) = await _channels("org_1")
    assert entry["channel_id"] == "chan_new"
    assert entry["resource_id"] == "res_chan_new"
    assert int(entry["expiration_ms"]) == _FAR_FUTURE_MS
    # Identity carried over from the old entry — same trigger, token, account.
    assert entry["trigger_public_id"] == "pub_1"
    assert entry["channel_token"] == "pcsec_1"
    assert entry["account_email"] == "ada@example.com"


async def test_renewal_pass_skips_channels_outside_the_lookahead(dynamodb_table):
    await _seed_owner("org_1", [_entry("chan_fresh", expiration_ms=_FAR_FUTURE_MS)])
    start = AsyncMock()
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", AsyncMock()),
    ):
        await update_service._google_watch_renewal_pass()
    start.assert_not_awaited()
    (entry,) = await _channels("org_1")
    assert entry["channel_id"] == "chan_fresh"


async def test_renewal_failure_leaves_entry_for_next_pass(dynamodb_table):
    """A failed re-watch neither stops the old channel nor touches its entry —
    the next pass retries; worst case the channel dies at expiry."""
    await _seed_owner("org_1", [_entry("chan_old")])
    start = AsyncMock(side_effect=ValueError("Google refused the watch request"))
    stop = AsyncMock()
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", stop),
    ):
        await update_service._google_watch_renewal_pass()  # must not raise

    stop.assert_not_awaited()
    (entry,) = await _channels("org_1")
    assert entry["channel_id"] == "chan_old"


async def test_one_bad_channel_does_not_stop_the_sweep(dynamodb_table):
    """Per-entry isolation: the first entry failing must not prevent the
    second entry's renewal — and the row write keeps the failed entry."""
    await _seed_owner("org_1", [_entry("chan_a"), _entry("chan_b", provider_event="drive.changed")])
    start = AsyncMock(side_effect=[RuntimeError("boom"), _replacement("chan_b2", page_token="pt_9")])
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", AsyncMock()),
    ):
        await update_service._google_watch_renewal_pass()

    assert start.await_count == 2
    a, b = await _channels("org_1")
    assert a["channel_id"] == "chan_a"  # failed renewal: entry untouched
    assert b["channel_id"] == "chan_b2"
    assert b["page_token"] == "pt_9"  # drive replacement carries its fresh start token


async def test_renewal_pass_caps_per_pass_work(dynamodb_table):
    cap = update_service._GOOGLE_WATCH_RENEWAL_MAX_PER_PASS
    await _seed_owner("org_1", [_entry(f"chan_{i}") for i in range(cap + 50)])
    start = AsyncMock(side_effect=lambda **kw: _replacement(f"new_{kw['channel_token']}"))
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", AsyncMock()),
    ):
        await update_service._google_watch_renewal_pass()
    assert start.await_count == cap


async def test_renewal_pass_noop_without_google_rows(dynamodb_table):
    from core.repositories import integrations_repo

    # A non-google integration and a google row with no channels: neither renews.
    await integrations_repo.put_status("org_1", "github")
    await integrations_repo.put_status("org_2", "google")
    start = AsyncMock()
    with (
        patch("core.services.google_watch.start_watch", start),
        patch("core.services.google_watch.stop_watch", AsyncMock()),
    ):
        await update_service._google_watch_renewal_pass()
    start.assert_not_awaited()


async def test_renewal_pass_survives_scan_failure(dynamodb_table):
    start = AsyncMock()
    with (
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch("core.services.google_watch.start_watch", start),
    ):
        await update_service._google_watch_renewal_pass()  # must not raise
    start.assert_not_awaited()
