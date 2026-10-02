import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.services import voice_calls as vc

TEAM = [{"id": "ceo-1", "name": "Ada"}, {"id": "fin-1", "name": "Finance"}]


OPEN_WINDOW = {
    "callId": "win-1",
    "ownerId": "org_a",
    "kind": "huddle",
    "roomId": "room-1",
    "startedAt": 1000,
    "deadline": 1600,
    "status": "call_open",
}


@pytest.fixture
def start_env():
    ps = [
        patch.object(vc.settings, "OPENAI_API_KEY", "sk-test"),
        patch.object(vc.billing_repo, "get_by_owner_id", AsyncMock(return_value={"plan": "base"})),
        patch.object(vc.voice_billing, "billing_mode", return_value="free"),
        patch.object(vc.voice_billing, "budget_seconds", AsyncMock(return_value=600)),
        patch.object(
            vc,
            "_owner_row",
            AsyncMock(return_value=SimpleNamespace(company_id="co-1", starter_agent_ids={"ceo": "ceo-1"})),
        ),
        patch.object(vc, "_room_team", AsyncMock(return_value=({"name": "general", "ownerId": "org_a"}, TEAM))),
        patch.object(vc.container_repo, "get_by_status", AsyncMock(return_value=[])),
        patch.object(vc, "put_call", AsyncMock()),
        patch.object(vc, "prewake_owner", MagicMock()),
        patch.object(vc, "_claim_free", AsyncMock()),
        patch.object(vc, "_release_free", AsyncMock()),
        patch.object(vc, "_hold", AsyncMock(return_value=(30_000, 300))),
        patch.object(vc.time, "time", return_value=1200),
    ]
    for p in ps:
        p.start()
    yield
    patch.stopall()


async def _start(room_id="room-1"):
    return await vc.open_voice_window(owner_id="org_a", room_id=room_id, speaker_user_id="user_b")


async def test_first_summon_opens_a_window_with_one_free_claim(start_env):
    out = await _start()
    call = vc.put_call.await_args.args[0]
    assert set(call) == {
        "callId", "ownerId", "userId", "kind", "roomId", "roomName", "companyId",
        "ceoId", "startedAt", "deadline", "billing", "holdMicrocents", "status",
    }  # fmt: skip
    assert call["kind"] == "huddle" and call["userId"] == "user_b" and call["ceoId"] == "ceo-1"
    assert call["billing"] == "free" and call["deadline"] - call["startedAt"] == 600
    vc._claim_free.assert_awaited_once()
    vc._hold.assert_not_awaited()
    vc.prewake_owner.assert_called_once_with("org_a")
    assert out == {"callId": call["callId"], "deadline": 1800, "budgetSeconds": 600, "joined": False}


async def test_second_summon_for_the_room_joins_without_a_claim_or_hold(start_env):
    vc.container_repo.get_by_status.return_value = [
        {**OPEN_WINDOW, "roomId": "room-2", "callId": "other-room"},
        {**OPEN_WINDOW, "ownerId": "org_b", "callId": "other-owner"},
        {**OPEN_WINDOW, "deadline": 1100, "callId": "expired"},
        OPEN_WINDOW,
    ]
    out = await _start()
    assert out == {"callId": "win-1", "deadline": 1600, "budgetSeconds": 400, "joined": True}
    vc._claim_free.assert_not_awaited()
    vc._hold.assert_not_awaited()
    vc.put_call.assert_not_awaited()
    vc._room_team.assert_awaited_once_with("org_a", "user_b", "room-1")  # a join is still membership-gated


async def test_another_room_while_a_free_window_is_open_is_voice_busy(start_env):
    vc.container_repo.get_by_status.return_value = [OPEN_WINDOW]
    vc._claim_free.side_effect = vc._problem(409, code="voice_busy")
    with pytest.raises(HTTPException) as exc:
        await _start("room-2")
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "voice_busy"
    vc.put_call.assert_not_awaited()


async def test_credits_take_one_hold_per_window(start_env):
    with patch.object(vc.voice_billing, "billing_mode", return_value="credits"):
        out = await _start()
        call = vc.put_call.await_args.args[0]
        vc.container_repo.get_by_status.return_value = [call]
        joined = await _start()
    vc._hold.assert_awaited_once_with("org_a")
    vc._claim_free.assert_not_awaited()
    assert call["billing"] == "credits" and call["holdMicrocents"] == 30_000
    assert out["budgetSeconds"] == 300 and joined == {**out, "joined": True}


async def test_window_start_credits_low_is_402_and_releases(start_env):
    vc._hold.return_value = (0, 0)
    with (
        patch.object(vc.voice_billing, "billing_mode", return_value="credits"),
        patch.object(vc, "release_hold", AsyncMock()) as release,
    ):
        with pytest.raises(HTTPException) as exc:
            await _start()
    assert exc.value.status_code == 402 and exc.value.detail == {"code": "voice_limit", "reason": "credits_low"}
    release.assert_awaited_once_with("org_a", 0)
    vc.put_call.assert_not_awaited()


async def test_window_put_failure_releases_hold(start_env):
    vc.put_call.side_effect = RuntimeError("ddb down")
    with (
        patch.object(vc.voice_billing, "billing_mode", return_value="credits"),
        patch.object(vc, "release_hold", AsyncMock()) as release,
    ):
        with pytest.raises(RuntimeError):
            await _start()
    release.assert_awaited_once_with("org_a", 30_000)


async def test_window_start_without_a_live_key_is_503(start_env):
    with patch.object(vc.settings, "OPENAI_API_KEY", "not-a-key"):  # CDK's random seed counts as unset
        with pytest.raises(HTTPException) as exc:
            await _start()
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "voice_unavailable"
    vc.put_call.assert_not_awaited()


async def test_window_start_without_a_live_plan_is_402(start_env):
    vc.voice_billing.billing_mode.return_value = None
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == 402 and exc.value.detail["reason"] == "plan_inactive"
    vc.put_call.assert_not_awaited()


async def test_window_start_free_minutes_used_is_402(start_env):
    vc.voice_billing.budget_seconds.return_value = vc.voice_billing.MIN_CALL_SECONDS - 1
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == 402 and exc.value.detail["reason"] == "free_minutes_used"
    vc._claim_free.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError("ddb"), asyncio.CancelledError()])
async def test_window_start_failed_put_releases_the_free_claim(start_env, error):
    vc.put_call.side_effect = error
    with patch.object(vc, "release_hold", AsyncMock()), pytest.raises(type(error)):
        await _start()
    call_id = vc._claim_free.await_args.args[1]
    vc._release_free.assert_awaited_once_with("org_a", call_id)


@pytest.mark.parametrize("status,code", [(409, "room_archived"), (404, "room_not_found")])
async def test_huddle_start_room_gates(start_env, status, code):
    vc._room_team.side_effect = vc._problem(status, code=code)
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == status and exc.value.detail["code"] == code


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


async def test_close_window_stops_no_runs_bills_capped_usage_and_releases_the_claim():
    table, ps = _close_env({**FREE_HUDDLE, "turns": {"fin-1": {"runId": "r1", "seq": 1}}})
    for p in ps:
        p.start()
    try:
        with patch.object(vc.runtime_transport, "request", AsyncMock()) as runtime:
            assert await vc.close_call("call-h", now=1100, usage_seconds=900) is True
            await asyncio.gather(*vc._background)
        runtime.assert_not_awaited()
        items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        assert items[0]["Update"]["ExpressionAttributeValues"][":s"] == 600  # min(900, deadline - startedAt)
        assert items[1]["Delete"]["Key"] == {"owner_id": "__call__#call-h"}
        vc._release_free.assert_awaited_once_with("org_a", "call-h")
    finally:
        patch.stopall()


async def test_sweeper_closes_an_orphan_window_and_stops_no_runs():
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
            patch.object(vc.runtime_transport, "request", AsyncMock()) as runtime,
        ):
            assert await vc.sweep_calls_once(now=1200) == 1
            await asyncio.gather(*vc._background)
        runtime.assert_not_awaited()
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


# ---- Option D: voice requests are runs on the room key ------------------------------------------

WINDOW = {**OPEN_WINDOW, "companyId": "co-1", "roomName": "general", "deadline": 4102444800}
ROOM_META = {"roomId": "room-1", "name": "general", "kind": "channel", "lastSeq": 7, "ownerId": "org_a"}
MEMBERS = [
    {"kind": "agent", "id": "fin-1", "displayName": "Finance", "listenMode": "active"},
    {"kind": "agent", "id": "obs-1", "displayName": "Quiet", "listenMode": "observe"},
    {"kind": "human", "id": "user_b", "displayName": "Bea"},
]


@pytest.fixture
def run_env():
    rt = AsyncMock(return_value=MagicMock(status_code=202))
    history = AsyncMock(return_value=[{"messageId": f"m{i}", "text": "hi"} for i in range(3)])
    ps = [
        patch.object(vc.messaging_repo, "get_room", AsyncMock(return_value=ROOM_META)),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(vc.messaging_repo, "put_voice_run", AsyncMock()),
        patch.object(vc.messaging_repo, "get_voice_run", AsyncMock(return_value=None)),
        patch.object(vc.openclaw_work, "room_history", history),
        patch.object(vc.messaging, "human_display_name", AsyncMock(return_value="Bea")),
        patch.object(vc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw"})),
        patch.object(vc.runtime_transport, "request", rt),
        patch.object(vc.settings, "PUBLIC_API_BASE", "https://api-dev.thelightbulb.company/"),
    ]
    for p in ps:
        p.start()
    yield rt
    patch.stopall()


async def _dispatch(run_id="run-1", agent_id="fin-1", transcript="Ada: numbers?"):
    return await vc.dispatch_voice_run(
        call=WINDOW,
        run_id=run_id,
        agent_id=agent_id,
        speaker_id="user_b",
        utterance="what's our burn",
        transcript=transcript,
    )


async def test_voice_run_is_an_ordinary_run_on_the_room_key(run_env):
    out = await _dispatch()
    assert run_env.await_args.args[1] == "/internal/calls/v1/runs"
    body = run_env.await_args.kwargs["json_body"]
    identity, req = body["identity"], body["request"]
    assert identity == {
        "runId": "run-1", "companyId": "co-1", "agentId": "fin-1", "roomId": "room-1",
        "sessionId": "agent:fin-1:lightbulb:channel:room-1", "family": "voice",
    }  # fmt: skip
    assert req["session_id"] == identity["sessionId"] and req["instructions"] == vc.TURN_NOTE
    assert req["frames"] == {
        "sink": "backend",
        "endpoint": "https://api-dev.thelightbulb.company/api/v1/internal/huddles/org_a/rooms/room-1/frames",
    }
    assert req["voice"] == {
        "roomId": "room-1", "roomKind": "channel", "speaker": {"id": "user_b", "name": "Bea"},
        "utterance": "what's our burn",
    }  # fmt: skip
    assert [m["messageId"] for m in req["recentHistory"]] == ["m0", "m1", "m2"]
    vc.openclaw_work.room_history.assert_awaited_once_with(ROOM_META, 8)  # through the latest message
    assert vc.openclaw_work._HISTORY_LIMIT == 50
    assert {p["id"] for p in req["participants"]} == {"fin-1", "obs-1", "user_b"}
    assert (
        req["input"]
        == "[Live voice in #general. Spoken since you last spoke, newest last:\nAda: numbers?]\n\nBea (voice): what's our burn"
    )
    for text in (req["input"], vc.TURN_NOTE):
        assert "room-1" not in text and "lightbulb-post" not in text and "--room" not in text
    vc.messaging_repo.put_voice_run.assert_awaited_once_with(
        "org_a", "run-1", {"roomId": "room-1", "agentId": "fin-1", "speakerUserId": "user_b", "speakerName": "Bea"}
    )
    assert out == {"runId": "run-1", "agentId": "fin-1", "steered": False}


async def test_voice_run_history_drops_the_oldest_past_one_mib(run_env):
    vc.openclaw_work.room_history.return_value = [{"messageId": f"m{i}", "text": "x" * 400_000} for i in range(3)]
    await _dispatch()
    history = run_env.await_args.kwargs["json_body"]["request"]["recentHistory"]
    assert [m["messageId"] for m in history] == ["m1", "m2"]


async def test_voice_run_for_an_agent_not_in_the_room_is_409(run_env):
    with pytest.raises(HTTPException) as exc:
        await _dispatch(agent_id="nobody")
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "agent_not_in_room"
    run_env.assert_not_awaited()
    vc.messaging_repo.put_voice_run.assert_not_awaited()


async def test_voice_run_reaches_an_observe_agent(run_env):
    """Listen modes are chat-only: a name said in Live reaches any agent member (owner ruling 2026-10-02)."""
    assert (await _dispatch(agent_id="obs-1"))["agentId"] == "obs-1"


async def test_two_requests_to_one_agent_are_two_runs_and_nothing_is_stopped(run_env):
    await _dispatch("run-1")
    await _dispatch("run-2")
    paths = [c.args[1] for c in run_env.await_args_list]
    assert paths == ["/internal/calls/v1/runs"] * 2
    assert [c.kwargs["json_body"]["identity"]["runId"] for c in run_env.await_args_list] == ["run-1", "run-2"]


async def test_a_retry_reuses_the_run_id_and_a_replay_counts_as_dispatched(run_env):
    import httpx

    run_env.side_effect = [httpx.ReadTimeout("cold"), MagicMock(status_code=200)]  # 200 = the bridge's "replayed"
    with pytest.raises(HTTPException) as exc:
        await _dispatch("run-1")
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "runtime_unavailable"
    assert (await _dispatch("run-1"))["runId"] == "run-1"
    assert [c.kwargs["json_body"]["identity"]["runId"] for c in run_env.await_args_list] == ["run-1", "run-1"]


async def test_a_refused_dispatch_is_503_and_stops_nothing(run_env):
    run_env.return_value = MagicMock(status_code=400, text="invalid_call_run")
    with pytest.raises(HTTPException) as exc:
        await _dispatch()
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "runtime_unavailable"
    assert [c.args[1] for c in run_env.await_args_list] == ["/internal/calls/v1/runs"]


VOICERUN = {"runId": "run-1", "roomId": "room-1", "agentId": "fin-1", "speakerUserId": "user_b"}


async def _steer(agent_id="fin-1"):
    return await vc.steer_voice_run(
        call=WINDOW, run_id="run-1", agent_id=agent_id, speaker_id="user_b", utterance="also X"
    )


async def test_steer_lands_in_the_agents_live_voice_run(run_env):
    vc.messaging_repo.get_voice_run.return_value = VOICERUN
    assert await _steer() == {"runId": "run-1", "agentId": "fin-1", "steered": True}
    assert run_env.await_args.args[1] == "/plugins/lightbulb-channel/runs/run-1/steer"
    assert run_env.await_args.kwargs["json_body"] == {"text": "Bea (voice): also X"}
    vc.messaging_repo.put_voice_run.assert_awaited_once_with(
        "org_a", "run-1", {**VOICERUN, "speakerUserId": "user_b", "speakerName": "Bea"}
    )


async def test_steer_into_a_finished_run_falls_back(run_env):
    vc.messaging_repo.get_voice_run.return_value = VOICERUN
    run_env.return_value = MagicMock(status_code=404)
    assert await _steer() is None


@pytest.mark.parametrize("row", [None, {**VOICERUN, "agentId": "other"}, {**VOICERUN, "roomId": "room-2"}])
async def test_steer_refuses_another_agents_or_rooms_run(run_env, row):
    vc.messaging_repo.get_voice_run.return_value = row
    assert await _steer() is None
    run_env.assert_not_awaited()


async def test_abort_needs_the_runs_room(run_env):
    vc.messaging_repo.get_voice_run.return_value = VOICERUN
    assert await vc.abort_voice_run("org_a", "room-2", "run-1") is False
    run_env.assert_not_awaited()
    assert await vc.abort_voice_run("org_a", "room-1", "run-1") is True
    assert run_env.await_args.args[1] == "/plugins/lightbulb-channel/runs/run-1/abort"


GAUGE_TEAM = [{"id": "ceo-1", "name": "Ada"}, {"id": "gauge-1", "name": "Gauge"}, {"id": "fin-1", "name": "Finance"}]


@pytest.fixture
def route_env():
    with (
        patch.object(vc, "_room_team", AsyncMock(return_value=({"name": "general"}, GAUGE_TEAM))),
        patch.object(vc, "_starter_ids", AsyncMock(return_value={"ceo": "ceo-1"})),
    ):
        yield


async def _route(text):
    return await vc.route_voice(owner_id="org_a", room_id="room-1", speaker_id="user_b", utterance=text, transcript="")


async def test_route_a_name_inside_the_request_beats_jev(route_env):
    with patch.object(vc.jev, "pick_agent", AsyncMock(side_effect=AssertionError("no Jev"))):
        assert await _route("Lightbulb, what did Gauge find?") == "gauge-1"


async def test_route_jev_then_ceo_on_timeout(route_env):
    with patch.object(vc.jev, "pick_agent", AsyncMock(return_value="fin-1")):
        assert await _route("Lightbulb, how's the burn") == "fin-1"

    async def slow(*_a, **_k):
        await asyncio.sleep(1)

    with (
        patch.object(vc, "_ROUTING_TIMEOUT_S", 0.01),
        patch.object(vc.jev, "pick_agent", slow),
        patch.object(vc, "put_metric") as metric,
    ):
        assert await _route("Lightbulb, anything new") == "ceo-1"
    metric.assert_called_once_with("voice.routing.timeout")


async def test_route_falls_back_to_the_first_agent_without_a_ceo(route_env):
    vc._starter_ids.return_value = {}
    vc._room_team.return_value = ({"name": "general"}, GAUGE_TEAM[1:])
    with patch.object(vc.jev, "pick_agent", AsyncMock(return_value=None)):
        assert await _route("Lightbulb, anything new") == "gauge-1"


# ---- Live lines with no name: routed like a chat message (route_live_line) --------------------------

LIVE_MEMBERS = [
    {"kind": "agent", "id": "ceo-1", "displayName": "Ada", "listenMode": "mentions"},
    {"kind": "agent", "id": "fri-1", "displayName": "Friday", "listenMode": "observe"},  # Live ignores modes
    {"kind": "agent", "id": "fin-1", "displayName": "Finance", "listenMode": "active"},
    {"kind": "human", "id": "user_a", "displayName": "Prasiddha Parthsarthy"},
    {"kind": "human", "id": "user_b", "displayName": "Aden"},
]


@pytest.fixture
def live_env():
    with (
        patch.object(vc, "_room_team", AsyncMock(return_value=({"name": "general", "kind": "channel"}, []))),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=LIVE_MEMBERS)),
        patch.object(vc.messaging, "human_display_name", AsyncMock(return_value="Prasiddha")),
        patch.object(vc.openclaw_work, "_fallback_ceo_id", AsyncMock(return_value="ceo-1")),
    ):
        yield


async def _live(text, humans=1, who=None, transcript=""):
    decide = AsyncMock(return_value={"who": who} if who else {"fallback": True})
    with patch.object(vc.jev, "_decide_for_message", decide):
        got = await vc.route_live_line(
            owner_id="org_a",
            room_id="room-1",
            speaker_id="user_a",
            utterance=text,
            transcript=transcript,
            humans=humans,
        )
    return got, decide


async def test_live_repro_lines_go_to_jev_with_every_agent_listening(live_env):
    for text in ("Hey guys.", "Hey guys, hey Friday.", "Hey guys. Hey Friday, what's up?"):
        got, decide = await _live(text, who="fri-1")
        assert got == "fri-1"
        message = decide.await_args.args[1]
        assert {t["agentId"] for t in message["dispatchTargets"]} == {"ceo-1", "fri-1", "fin-1"}
        assert message["author"]["kind"] == "human" and message["text"] == text


async def test_live_an_opening_name_is_a_mention_and_skips_jev(live_env):
    got, decide = await _live("Hey Friday, what's up?", humans=3)
    assert got == "fri-1"
    decide.assert_not_awaited()


async def test_live_alone_nobody_counts_as_jev_down(live_env):
    # the fallback: whoever spoke last in the call, else the CEO
    got, _ = await _live("Hey guys.", humans=1, who="none", transcript="Finance: burn is flat\nPrasiddha: Hey guys.")
    assert got == "fin-1"
    assert (await _live("Hey guys.", humans=1, who="none"))[0] == "ceo-1"
    assert (await _live("Hey guys.", humans=1))[0] == "ceo-1"  # Jev down


async def test_live_two_people_jev_may_pick_nobody(live_env):
    assert (await _live("did you see the game", humans=2, who="none"))[0] is None
    assert (await _live("what's our burn", humans=2, who="fin-1"))[0] == "fin-1"
    assert (await _live("what's our burn", humans=2))[0] == "ceo-1"  # Jev down: chat's fallback


async def test_live_a_line_to_a_person_is_silent_without_jev(live_env):
    got, decide = await _live("Aden, can you check the deck?", humans=2)
    assert got is None
    assert decide.await_args.kwargs["to_people"] is True
    assert decide.await_args.args[1]["mentions"] == [{"kind": "human", "id": "user_b"}]


async def test_live_jev_timeout_falls_back(live_env):
    async def slow(*_a, **_k):
        await asyncio.sleep(1)

    with patch.object(vc, "_ROUTING_TIMEOUT_S", 0.01), patch.object(vc.jev, "_decide_for_message", slow):
        got = await vc.route_live_line(
            owner_id="org_a", room_id="room-1", speaker_id="user_a", utterance="hm what now", transcript="", humans=2
        )
    assert got == "ceo-1"


def test_live_transcript_becomes_history_before_the_line():
    rows = vc._transcript_history("Pat: hi\nFinance: burn is flat\nPat: and now?", LIVE_MEMBERS, "and now?")
    assert [(r["author"]["kind"], r["author"]["id"], r["text"]) for r in rows] == [
        ("human", "Pat", "hi"),
        ("agent", "fin-1", "burn is flat"),
    ]
