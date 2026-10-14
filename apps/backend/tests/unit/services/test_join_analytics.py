"""T02 join events: workspace_created, member_joined (+ via marker), team_formed."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import join_analytics, webhook_dedup


def _org(members: int, hours_old: float) -> dict:
    return {"members_count": members, "created_at": int((time.time() - hours_old * 3600) * 1000)}


@pytest.fixture
def capture():
    with patch.object(join_analytics, "capture_product_event") as cap:
        yield cap


def _events(cap) -> dict:
    return {c.kwargs["event"]: c.kwargs for c in cap.call_args_list}


def test_workspace_created_is_keyed_on_the_creator(capture):
    with patch.object(join_analytics, "remember_org_creator") as remember:
        join_analytics.workspace_created("org_1", "user_c")
    remember.assert_called_once_with("org_1", "user_c")
    kw = capture.call_args.kwargs
    assert kw["distinct_id"] == "user_c"
    assert kw["properties"] == {"workspace_id": "org_1", "created_by": "user_c"}
    assert kw["event_uuid"]


@pytest.mark.asyncio
async def test_member_joined_reads_the_doors_marker_and_forms_a_team(capture):
    marker = {"via": "join_link", "invited_by": "user_c", "ttl": time.time() + 60}
    with (
        patch.object(webhook_dedup, "_read_event", AsyncMock(side_effect=lambda key: marker if "user_b" in key else {})),
        patch.object(join_analytics.clerk_admin, "get_organization", AsyncMock(return_value=_org(2, 3))),
        patch.object(
            webhook_dedup, "record_event_or_skip", AsyncMock(return_value=webhook_dedup.WebhookDedupResult.RECORDED)
        ),
    ):
        await join_analytics.member_joined("org_1", "user_b", email="b@x.co", creator_id="user_c", membership_id="m1")
    events = _events(capture)
    joined = events["member_joined"]
    assert joined["distinct_id"] == "user_b"
    assert joined["properties"]["via"] == "join_link"
    assert joined["properties"]["invited_by"] == "user_c"
    assert joined["properties"]["members_count"] == 2
    assert events["team_formed"]["properties"]["workspace_id"] == "org_1"


@pytest.mark.asyncio
async def test_an_email_invite_marker_is_found_by_address(capture):
    marker = {"via": "invite", "invited_by": "user_c", "ttl": time.time() + 60}
    with (
        patch.object(webhook_dedup, "_read_event", AsyncMock(side_effect=lambda key: marker if "b@x.co" in key else {})),
        patch.object(join_analytics.clerk_admin, "get_organization", AsyncMock(return_value=_org(5, 100))),
    ):
        await join_analytics.member_joined("org_1", "user_b", email="B@x.co", creator_id="user_c", membership_id="m1")
    events = _events(capture)
    assert events["member_joined"]["properties"]["via"] == "invite"
    assert "team_formed" not in events  # 100 h old: outside the 48 h window


@pytest.mark.asyncio
async def test_no_marker_reads_other_and_team_formed_fires_once(capture):
    with (
        patch.object(webhook_dedup, "_read_event", AsyncMock(return_value={})),
        patch.object(join_analytics.clerk_admin, "get_organization", AsyncMock(return_value=_org(3, 1))),
        patch.object(
            webhook_dedup, "record_event_or_skip", AsyncMock(return_value=webhook_dedup.WebhookDedupResult.ALREADY_SEEN)
        ),
    ):
        await join_analytics.member_joined("org_1", "user_d", email=None, creator_id="user_c", membership_id="m2")
    events = _events(capture)
    assert events["member_joined"]["properties"]["via"] == "other"
    assert "team_formed" not in events


@pytest.mark.asyncio
async def test_the_creators_own_seat_is_not_a_join(capture):
    await join_analytics.member_joined("org_1", "user_c", email=None, creator_id="user_c", membership_id="m0")
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_mark_join_via_writes_a_ttl_marker_and_rejects_unknown_doors():
    table = MagicMock()
    with patch.object(webhook_dedup, "_table", return_value=table):
        await join_analytics.mark_join_via("org_1", via="invite", email="B@x.co", invited_by="user_c")
    item = table.put_item.call_args.kwargs["Item"]
    assert item["event_id"] == "join-via:org_1:b@x.co"
    assert item["via"] == "invite"
    assert item["ttl"] > time.time() + 7 * 86400
    with pytest.raises(ValueError):
        await join_analytics.mark_join_via("org_1", via="bogus", user_id="user_b")
