from unittest.mock import AsyncMock, patch

import pytest

from core.services import voice_calls as vc


@pytest.mark.asyncio
async def test_sweep_closes_only_past_deadline_and_stale_closing():
    rows = {
        "call_open": [{"callId": "late", "deadline": 100}, {"callId": "live", "deadline": 10_000}],
        "call_closing": [
            {"callId": "stuck", "deadline": 100, "closingAt": 1},
            {"callId": "busy", "deadline": 100, "closingAt": 900},
        ],
    }
    with (
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=lambda s: rows[s])),
        patch.object(vc, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert await vc.sweep_calls_once(now=1000) == 2
    assert sorted(c.args[0] for c in close.await_args_list) == ["late", "stuck"]


@pytest.mark.asyncio
async def test_sweep_releases_a_huddle_whose_worker_vanished():
    rows = {
        "call_open": [
            {
                "callId": "orphan",
                "kind": "huddle",
                "roomId": "11111111-1111-4111-8111-111111111111",
                "startedAt": 800,
                "deadline": 10_000,
            },
            {"callId": "young", "kind": "huddle", "roomId": "r2", "startedAt": 950, "deadline": 10_000},
            {"callId": "served", "kind": "huddle", "roomId": "r3", "startedAt": 800, "deadline": 10_000},
        ],
        "call_closing": [],
    }
    publisher = AsyncMock()
    with (
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=lambda s: rows[s])),
        patch.object(vc.livekit_huddles, "configured", return_value=True),
        patch.object(vc.livekit_huddles, "has_ceo", AsyncMock(side_effect=lambda room: room == "r3")),
        patch.object(vc.livekit_huddles, "participants", AsyncMock(return_value=["user_a"])),
        patch.object(vc, "get_live_event_publisher", return_value=publisher),
        patch.object(vc, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert await vc.sweep_calls_once(now=1000) == 1
    assert [c.args[0] for c in close.await_args_list] == ["orphan"]
    room, event = publisher.publish_room.await_args.args  # the stuck marker is corrected from LiveKit
    assert room == "11111111-1111-4111-8111-111111111111" and event["payload"]["userIds"] == ["user_a"]


@pytest.mark.asyncio
async def test_sweep_reports_open_windows():
    rows = {"call_open": [{"callId": c, "deadline": 10_000} for c in ("a", "b", "c")], "call_closing": []}
    with (
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=lambda s: rows[s])),
        patch.object(vc, "put_metric") as metric,
    ):
        assert await vc.sweep_calls_once(now=1000) == 0
    metric.assert_called_once_with("voice.calls.open", value=3)


@pytest.mark.asyncio
async def test_phone_sweep_reads_every_page_of_the_open_call_index():
    from unittest.mock import MagicMock

    pages = [{"Items": [{"pk": "p", "sk": "a", "startedAtEpoch": 0}], "LastEvaluatedKey": {"k": 1}},
             {"Items": [{"pk": "p", "sk": "b", "startedAtEpoch": 0}]}]  # fmt: skip
    table = MagicMock()
    table.query.side_effect = pages
    from routers import voice_phone

    with (
        patch.object(vc.messaging_repo, "_table", return_value=table),
        patch.object(voice_phone, "end_call", AsyncMock()) as end,
    ):
        assert await vc.sweep_phone_calls(10**9) == 2
    assert [c.args[0] for c in end.await_args_list] == ["a", "b"]
