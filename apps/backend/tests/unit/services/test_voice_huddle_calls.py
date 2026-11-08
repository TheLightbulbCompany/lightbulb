import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.services import voice_calls as vc

TEAM = [{"id": "ceo-1", "name": "Ada"}, {"id": "fin-1", "name": "Finance"}]


async def test_huddle_start_guest_is_404():
    """The real _room_team: a shared-channel guest (room owned by another workspace) 404s."""
    with patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_other"}, {}))):
        with pytest.raises(HTTPException) as exc:
            await vc._room_team("org_a", "user_b", "room-1")
    assert exc.value.status_code == 404 and exc.value.detail["code"] == "room_not_found"


async def test_room_team_people_only_room():
    """Live join (need_team=False) gets an empty team for a people-only room; summon still 409s."""
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=[{"kind": "human", "id": "u"}])),
    ):
        assert (await vc._room_team("org_a", "u", "room-1", need_team=False))[1] == []
        with pytest.raises(HTTPException) as exc:
            await vc._room_team("org_a", "u", "room-1")
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "no_agents"


HUDDLE_CALL = {
    "callId": "call-h",
    "ownerId": "org_a",
    "userId": "user_a",
    "agentId": "ceo-1",
    "companyId": "co-1",
    "status": "call_open",
    "kind": "huddle",
    "roomId": "room-1",
    "roomName": "general",
    "team": TEAM,
    "ceoId": "ceo-1",
    "voiceAgentId": "ceo-1",
    "sessionId": None,
    "turns": {},
    "deadline": 4102444800,
}


def _close_env(call):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {**call, "status": "call_closing"}}
    table.name = "lightbulb-dev-containers"
    table.meta.client = MagicMock()
    return table, [
        patch.object(vc, "_table", return_value=table),
        patch.object(vc, "_release_free", AsyncMock()),
    ]


FREE_HUDDLE = {**HUDDLE_CALL, "billing": "free", "holdMicrocents": 0, "startedAt": 1000, "deadline": 1600}


async def test_close_with_usage_seconds_bills_them():
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        assert await vc.close_call("call-h", now=1100, usage_seconds=42) is True
        persisted = table.update_item.call_args_list[1].kwargs["ExpressionAttributeValues"][":s"]
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert persisted == 42 and items[0]["Update"]["ExpressionAttributeValues"][":s"] == 42
    finally:
        patch.stopall()


async def test_close_window_bills_capped_usage_and_releases_the_claim():
    table, ps = _close_env({**FREE_HUDDLE, "turns": {"fin-1": {"runId": "r1", "seq": 1}}})
    for p in ps:
        p.start()
    try:
        assert await vc.close_call("call-h", now=1100, usage_seconds=900) is True
        await asyncio.gather(*vc._background)
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert items[0]["Update"]["ExpressionAttributeValues"][":s"] == 600  # min(900, deadline - startedAt)
        assert items[1]["Delete"]["Key"] == {"owner_id": "__call__#call-h"}
        vc._release_free.assert_awaited_once_with("org_a", "call-h")
    finally:
        patch.stopall()


async def test_sweeper_closes_an_orphan_window():
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        with (
            patch.object(
                vc.container_repo, "get_by_status", AsyncMock(side_effect=[[{**FREE_HUDDLE, "deadline": 10_000}], []])
            ),
            patch.object(vc.livekit_huddles, "configured", return_value=True),
            patch.object(vc.livekit_huddles, "has_ceo", AsyncMock(return_value=False)),
            patch.object(vc, "_publish_huddle_presence", AsyncMock()),
        ):
            assert await vc.sweep_calls_once(now=1200) == 1
            await asyncio.gather(*vc._background)
    finally:
        patch.stopall()


async def test_sweeper_closes_unreported_huddle_at_deadline_billing_zero():
    """Worker crashed: the sweeper's deadline close bills 0, never wall-clock, and flags it."""
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        with (
            patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=[[dict(FREE_HUDDLE)], []])),
            patch.object(vc, "put_metric") as metric,
        ):
            assert await vc.sweep_calls_once(now=2000) == 1
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert items[0]["Update"]["ExpressionAttributeValues"][":s"] == 0
        names = [c.args[0] for c in metric.call_args_list]
        assert "voice.huddle.usage_missing" in names
    finally:
        patch.stopall()


async def test_sweeper_gives_huddle_rows_a_grace_so_the_worker_end_wins():
    with (
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=[[dict(FREE_HUDDLE)], []])),
        patch.object(vc, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert await vc.sweep_calls_once(now=1600 + 59) == 0
        close.assert_not_awaited()


@pytest.mark.parametrize("bad", ["abc", [1]])
async def test_close_rejects_bad_usage_before_claiming(bad):
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        with pytest.raises((TypeError, ValueError)):
            await vc.close_call("call-h", now=1100, usage_seconds=bad)
        table.update_item.assert_not_called()  # the row is never moved to CLOSING
    finally:
        patch.stopall()


async def test_negative_usage_bills_zero():
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        await vc.close_call("call-h", now=1100, usage_seconds=-5)
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert items[0]["Update"]["ExpressionAttributeValues"][":s"] == 0
    finally:
        patch.stopall()


async def test_close_caps_reported_usage_at_the_budget():
    table, ps = _close_env(FREE_HUDDLE)
    for p in ps:
        p.start()
    try:
        await vc.close_call("call-h", now=1100, usage_seconds=10_000)
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert items[0]["Update"]["ExpressionAttributeValues"][":s"] == 600
    finally:
        patch.stopall()


async def test_prewake_warms_every_room_agents_profile_after_the_health_wake(monkeypatch):
    """Going Live: one health RPC wakes the runtime, then profile.warm per agent pulls each profile
    off the bridge's request thread, so the first summon's restore (23 s cold) is already done."""
    calls = []

    async def fake_call(owner_id, method, params=None, **kwargs):
        calls.append((owner_id, method, params))
        return {"ok": True}

    tasks = []
    monkeypatch.setattr(vc.container_rpc, "call", fake_call)
    monkeypatch.setattr(vc, "_fire", lambda coro: tasks.append(asyncio.ensure_future(coro)))
    vc.prewake_owner("org_a", [{"id": "a1", "name": "Pulse"}, {"id": "a2", "name": "Friday"}])
    await asyncio.gather(*tasks)
    assert calls == [
        ("org_a", "health", None),
        ("org_a", "profile.warm", {"agentId": "a1"}),
        ("org_a", "profile.warm", {"agentId": "a2"}),
    ]


async def test_prewake_names_the_room_so_its_sessions_compact_before_the_first_question(monkeypatch):
    """A spoken turn never compacts (67-103 s inside a turn, prod 10-07): the bridge does it on this
    warm instead, off its request thread, so the RPC still answers at once."""
    calls = []

    async def fake_call(owner_id, method, params=None, **kwargs):
        calls.append((method, params, kwargs.get("timeout_s")))
        return {"ok": True}

    tasks = []
    monkeypatch.setattr(vc.container_rpc, "call", fake_call)
    monkeypatch.setattr(vc, "_fire", lambda coro: tasks.append(asyncio.ensure_future(coro)))
    vc.prewake_owner("org_a", [{"id": "a1", "name": "Pulse"}], "room-1")
    await asyncio.gather(*tasks)
    assert calls[1] == ("profile.warm", {"agentId": "a1", "roomId": "room-1"}, 10.0)
