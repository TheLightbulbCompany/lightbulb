"""T23: week4_active fires once for a formed team with a human message in days 21-28."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import webhook_dedup, week4_active

DAY = 86400
FORMED = 1_800_000_000


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _msg(kind, epoch):
    return {"author": {"kind": kind}, "createdAt": _iso(epoch)}


@pytest.fixture
def world():
    repo = week4_active.messaging_repo
    with (
        patch.object(webhook_dedup, "_read_event", AsyncMock(return_value={"recorded_at": FORMED})),
        patch.object(
            webhook_dedup, "record_event_or_skip", AsyncMock(return_value=webhook_dedup.WebhookDedupResult.RECORDED)
        ) as claim,
        patch.object(repo, "list_owner_room_ids", AsyncMock(return_value=["r1"])),
        patch.object(repo, "get_room", AsyncMock(return_value={"lastSeq": 3})),
        patch.object(week4_active, "capture_product_event") as capture,
    ):
        yield claim, capture


@pytest.mark.asyncio
async def test_a_human_message_in_week_four_emits_once(world):
    claim, capture = world
    page = [_msg("human", FORMED + 22 * DAY), _msg("agent", FORMED + 29 * DAY)]
    with patch.object(week4_active.messaging_repo, "list_messages_before_page", AsyncMock(return_value=(page, None))):
        assert await week4_active.check_workspace("org_1", FORMED + 28 * DAY + 60) is True
        claim.return_value = webhook_dedup.WebhookDedupResult.ALREADY_SEEN
        assert await week4_active.check_workspace("org_1", FORMED + 29 * DAY) is False
    assert capture.call_count == 1
    assert capture.call_args.kwargs["event"] == "week4_active"
    assert capture.call_args.kwargs["properties"]["workspace_id"] == "org_1"


@pytest.mark.asyncio
async def test_only_agents_or_only_earlier_messages_emit_nothing(world):
    _, capture = world
    page = [_msg("human", FORMED + 3 * DAY), _msg("agent", FORMED + 23 * DAY)]
    with patch.object(week4_active.messaging_repo, "list_messages_before_page", AsyncMock(return_value=(page, None))):
        assert await week4_active.check_workspace("org_1", FORMED + 28 * DAY + 60) is False
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_read_takes_no_marker(world):
    claim, _ = world
    with patch.object(week4_active.messaging_repo, "list_owner_room_ids", AsyncMock(side_effect=RuntimeError("ddb"))):
        with pytest.raises(RuntimeError):
            await week4_active.check_workspace("org_1", FORMED + 28 * DAY + 60)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_busy_room_is_scanned_through_to_week_four(world):
    """Pages newer than week four do not use the in-window read budget."""
    _, capture = world
    newer = ([_msg("agent", FORMED + 30 * DAY)] * 100, 5)
    target = ([_msg("human", FORMED + 22 * DAY)], None)
    pages = [newer] * 40 + [target]
    with patch.object(week4_active.messaging_repo, "list_messages_before_page", AsyncMock(side_effect=pages)):
        assert await week4_active.check_workspace("org_1", FORMED + 28 * DAY + 60) is True
    assert capture.call_count == 1


@pytest.mark.asyncio
async def test_outside_the_check_window_does_nothing(world):
    claim, _ = world
    assert await week4_active.check_workspace("org_1", FORMED + 10 * DAY) is False
    claim.assert_not_awaited()
