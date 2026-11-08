import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.services import voice_calls as vc
from tests.unit.services.voice_stubs import jev_says

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
        patch.object(vc.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
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


async def test_a_native_workspace_call_names_the_owner_as_its_company(start_env):
    """A workspace provisioned without Paperclip has company_id "": the bridge 400s an empty companyId."""
    vc._owner_row.return_value = SimpleNamespace(company_id="", starter_agent_ids={"ceo": "ceo-1"})
    await _start()
    assert vc.put_call.await_args.args[0]["companyId"] == "org_a"


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
ROOM_META = {"roomId": "room-1", "name": "general", "kind": "channel", "lastSeq": 7, "ownerId": "org_a", "revision": 3}
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
        patch.object(vc.messaging_repo, "put_voice_run", AsyncMock(return_value={})),
        patch.object(vc.messaging_repo, "update_voice_run", AsyncMock(return_value=True)),
        patch.object(vc.messaging_repo, "get_voice_run", AsyncMock(return_value=None)),
        patch.object(vc.openclaw_work, "room_history", history),
        patch.object(vc.messaging, "human_display_name", AsyncMock(return_value="Bea")),
        patch.object(vc.messaging, "human_emails", AsyncMock(return_value={"user_b": "bea@acme.test"})),
        patch("core.services.local_sessions.live_sessions_for_room", AsyncMock(return_value=[])),
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


async def test_voice_run_is_a_run_on_the_room_key_that_names_its_call(run_env):
    out = await _dispatch()
    assert run_env.await_args.args[1] == "/internal/calls/v1/runs"
    body = run_env.await_args.kwargs["json_body"]
    identity, req = body["identity"], body["request"]
    assert identity == {
        "runId": "run-1", "companyId": "co-1", "agentId": "fin-1", "roomId": "room-1",
        "sessionId": "agent:fin-1:lightbulb:channel:room-1", "callId": "win-1", "family": "voice",
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
        "org_a",
        "run-1",
        {"roomId": "room-1", "agentId": "fin-1", "speakerUserId": "user_b", "speakerName": "Bea",
         "requesterUserId": "user_b", "readerUserId": None},
        once=frozenset({"requesterUserId"}),
    )  # fmt: skip
    assert out == {"runId": "run-1", "agentId": "fin-1", "steered": False}


async def test_a_retry_after_a_lowering_steer_keeps_the_requester_lowered(run_env):
    """Codex P1: a /turns retry of a run another speaker already steered must not restore the first
    speaker's authority (the Mac tool, room reads). The row's requester is write-once; the grant is
    minted for what the row holds."""
    vc.messaging_repo.put_voice_run.return_value = {"runId": "run-1", "requesterUserId": None}
    mint = MagicMock(return_value="grant")
    with patch.object(vc.local_session_grants, "mint", mint):
        await _dispatch()
    mint.assert_called_once_with("org_a", "room-1", "fin-1", "run-1", None, None)


async def test_a_voice_run_carries_what_a_typed_turn_here_carries(run_env):
    """One path (prod Meet 10-09): the room block, the speaker as sender, human handles and the live
    sessions reach a voice run as they reach a typed turn (openclaw_work.build_ingress_body)."""
    sessions = [{"sessionId": "s-1", "title": "fix deploy"}]
    with patch("core.services.local_sessions.live_sessions_for_room", AsyncMock(return_value=sessions)):
        await _dispatch()
    req = run_env.await_args.kwargs["json_body"]["request"]
    assert req["room"] == {"id": "room-1", "name": "general", "kind": "channel", "revision": 3}
    assert req["sender"] == {"id": "user_b", "kind": "human", "displayName": "Bea"}
    assert req["sessions"] == sessions
    assert next(p for p in req["participants"] if p["id"] == "user_b").get("mentionHandle") == "bea"


@pytest.mark.parametrize(
    "speaker, members, requester",
    [
        ("user_b", MEMBERS, "user_b"),  # in-app Live: the signed-in member who spoke
        ("meet:room-1:p7", MEMBERS, None),  # a Meet speaker is never a member, nor the inviting owner
        ("Priya", MEMBERS + [{"kind": "human", "id": "user_o", "displayName": "Owen", "role": "owner"}], None),
        (
            "user_g",
            MEMBERS + [{"kind": "human", "id": "user_g", "displayName": "Gus", "homeOwnerId": "org_z"}],
            None,
        ),  # shared-room guest
    ],
    ids=["live-member", "meet-bot", "meet-label", "foreign-guest"],
)
async def test_voice_run_carries_the_room_grant_for_the_member_who_asked(run_env, speaker, members, requester):
    """Prod 10-09: a voice turn had no local-session grant, so lightbulb_local_sessions said NO_TURN."""
    mint = MagicMock(return_value="jwt")
    with (
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=members)),
        patch.object(vc.local_session_grants, "mint", mint),
    ):
        await vc.dispatch_voice_run(
            call=WINDOW, run_id="run-1", agent_id="fin-1", speaker_id=speaker, utterance="sessions?", transcript=""
        )
    mint.assert_called_once_with("org_a", "room-1", "fin-1", "run-1", requester, None)
    assert run_env.await_args.kwargs["headers"] == {
        "Authorization": "Bearer gw",
        "X-Lightbulb-Local-Session-Token": "jwt",
    }


async def test_voice_run_goes_out_without_a_grant_when_none_can_be_minted(run_env):
    with patch.object(vc.local_session_grants, "mint", MagicMock(side_effect=RuntimeError)):
        await _dispatch()
    assert run_env.await_args.kwargs["headers"] == {"Authorization": "Bearer gw"}


async def test_voice_run_history_is_the_newest_few_messages_cut_short(run_env):
    """A voice answer is spoken while someone waits: the prompt carries the newest 12 room messages,
    each cut to 500 characters, not the 50 full messages a chat turn gets (a 30k-token first prompt
    cost 3-6 s of model time before a word was spoken)."""
    vc.openclaw_work.room_history.return_value = [{"messageId": f"m{i}", "text": "x" * 400_000} for i in range(50)]
    await _dispatch()
    history = run_env.await_args.kwargs["json_body"]["request"]["recentHistory"]
    assert [m["messageId"] for m in history] == [f"m{i}" for i in range(38, 50)]
    assert all(len(m["text"]) == vc._VOICE_HISTORY_CHARS + 1 and m["text"].endswith("…") for m in history)
    assert vc._VOICE_HISTORY_MESSAGES == 12 and vc._VOICE_HISTORY_CHARS == 500


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
    vc.messaging_repo.update_voice_run.assert_awaited_once_with(
        "org_a", "run-1", {"speakerUserId": "user_b", "speakerName": "Bea"}
    )
    vc.messaging_repo.put_voice_run.assert_not_awaited()  # never a whole-row rewrite: it would drop noPost


@pytest.mark.parametrize("steerer", ["user_a", "meet:room-1"], ids=["another-member", "meet-speaker"])
async def test_a_steer_by_another_speaker_lowers_the_runs_grant(run_env, steerer):
    """P1: a guest or Meet voice steering a member's run must not borrow that member's direct access to
    their own local sessions: the steer carries a grant naming nobody, which the bridge swaps in."""
    vc.messaging_repo.get_voice_run.return_value = VOICERUN  # user_b asked
    mint = MagicMock(return_value="lowered")
    with patch.object(vc.local_session_grants, "mint", mint):
        result = await vc.steer_voice_run(
            call=WINDOW, run_id="run-1", agent_id="fin-1", speaker_id=steerer, utterance="x", speaker_name="Z"
        )
    assert result["steered"] is True
    mint.assert_called_once_with("org_a", "room-1", "fin-1", "run-1", None, None)  # never a requester: only lowered
    assert run_env.await_args.kwargs["headers"]["X-Lightbulb-Local-Session-Token"] == "lowered"
    # The row's verified requester drops with the grant: the Mac tool and room reads check it.
    assert vc.messaging_repo.update_voice_run.await_args.args[2]["requesterUserId"] is None


async def test_a_steer_by_the_runs_own_speaker_keeps_the_grant(run_env):
    vc.messaging_repo.get_voice_run.return_value = VOICERUN
    mint = MagicMock()
    with patch.object(vc.local_session_grants, "mint", mint):
        await _steer()
    mint.assert_not_called()
    assert run_env.await_args.kwargs["headers"] == {"Authorization": "Bearer gw"}


async def test_a_steer_that_cannot_lower_the_grant_is_not_steered(run_env):
    vc.messaging_repo.get_voice_run.return_value = {**VOICERUN, "speakerUserId": "user_a"}
    with patch.object(vc.local_session_grants, "mint", MagicMock(side_effect=RuntimeError)):
        assert await _steer() is None  # the caller dispatches a run of its own instead
    run_env.assert_not_awaited()


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


# ---- The one Live router (route_live_line, routing v2 §6) -------------------------------------------

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
        patch.object(vc.jev, "agent_card", AsyncMock(side_effect=lambda owner, agent, name, recency="": name)),
        patch.object(vc.jev, "_decide_for_message", AsyncMock(return_value={"who": "none"})) as judge,
        patch.object(vc.jev, "decide", AsyncMock(return_value=None)) as decide,  # the one call per line
    ):
        decide.judge = judge  # the room judge, asked only about a line among several people
        yield decide


async def _live(decide, text, said=None, humans=1, transcript="", wake=False, floor=None):
    """One spoken line through the router; ``said`` is Jev's answer (``jev_says``), None is Jev down."""
    decide.return_value = said
    return await vc.route_live_line(
        owner_id="org_a",
        room_id="room-1",
        speaker_id="user_a",
        utterance=text,
        transcript=transcript,
        humans=humans,
        wake=wake,
        floor={"agent": floor, "speaker": "Prasiddha", "mine": True} if floor else None,
    )


def _asked(got):
    return [(r["agentId"], r["by"]) for r in got["ops"]["requests"]]


NOBODY = {
    "requests": [], "social": None, "more": False, "cancel": [], "hush": [], "called": None, "steer": [],
    "people": False, "say": None,
}  # fmt: skip


def _say(agent, kind, task=None):
    """``ops.say``. ``text`` is None: Jev writes no words, so nothing in the one call can."""
    return {"agentId": agent, "kind": kind, "task": task, "text": None}


TO_PEOPLE = {**NOBODY, "people": True}  # talk between people: a meeting voice gives the floor back


async def test_live_unnamed_lines_go_to_jev_with_every_agent_listening(live_env):
    got = await _live(live_env, "Hey guys, anything new?", jev_says(kinds=["request"], whos=["fri-1"]))
    assert got["agentIds"] == ["fri-1"] and _asked(got) == [("fri-1", "fit")]  # Friday is on "observe": Live ignores it
    assert set(live_env.await_args.args[1]["w0"]["criteria"]) == {"ceo-1", "fri-1", "fin-1", "unsure"}


async def test_live_an_opening_name_gets_the_request_even_with_three_people(live_env):
    said = jev_says(kinds=["request"], whos=[None], names=["addressed"])
    got = await _live(live_env, "Hey Finance, what's our burn?", said, humans=3)
    assert got["agentIds"] == ["fin-1"] and _asked(got) == [("fin-1", "name")]


async def test_live_several_addressed_agents_come_back_as_one_request_each(live_env):
    line = "Finance, what's the burn. Ada, what's the plan"
    said = jev_says(
        kinds=["request", "request"],
        whos=[None, None],
        names=["addressed", "addressed"],
        asked=["only_finance", "only_ada"],
    )
    got = await _live(live_env, line, said, humans=2)
    assert got["agentId"] == "fin-1" and got["agentIds"] == ["fin-1", "ceo-1"]  # in the order named
    assert got["requests"] == [
        {"agentId": "fin-1", "task": "what's the burn"},
        {"agentId": "ceo-1", "task": "what's the plan"},
    ]
    # Jev down: nothing is cut, each named agent's request is the whole line
    down = await _live(live_env, line, None, humans=2)
    assert [(r["agentId"], r["task"]) for r in down["ops"]["requests"]] == [("fin-1", line), ("ceo-1", line)]
    one = await _live(live_env, "Finance, what's the burn?", jev_says(kinds=["request"], names=["addressed"]), humans=2)
    assert "requests" not in one  # one agent: today's answer shape


async def test_live_an_unfinished_line_is_held_but_still_routed(live_env):
    """The shipped worker reads only agentId: a held line still names whom it routes to (as today);
    a worker that honours "hold" waits for the speaker's next final instead."""
    said = jev_says(kinds=["request"], whos=["fin-1"], complete=0.2)
    live_env.judge.return_value = {"who": "fin-1"}
    got = await _live(live_env, "so we were hoping", said, humans=2)
    assert got["agentId"] == "fin-1" and got["hold"] is True


async def test_live_alone_nobody_counts_as_jev_down(live_env):
    # nothing was asked: one agent may say a sentence -- whoever spoke last in the call, else the CEO
    greeting = jev_says(kinds=["social"])
    got = await _live(live_env, "Hey guys.", greeting, transcript="Finance: burn is flat\nPrasiddha: Hey guys.")
    assert got["agentIds"] == ["fin-1"] and got["ops"]["social"] == "fin-1" and got["ops"]["requests"] == []
    assert (await _live(live_env, "Hey guys.", greeting))["agentIds"] == ["ceo-1"]
    down = await _live(live_env, "Hey guys.", None)  # Jev down: a lone speaker's unnamed line is no request
    assert down["agentIds"] == [] and down["ops"]["requests"] == []


async def test_live_two_people_jev_may_pick_nobody(live_env):
    """Among several people the room judge says whether an agent answers at all."""
    assert (await _live(live_env, "did you see the game", jev_says(kinds=["people"]), humans=2))["ops"] == TO_PEOPLE
    assert (await _live(live_env, "what's our burn", jev_says(kinds=["request"]), humans=2))["ops"] == TO_PEOPLE
    live_env.judge.return_value = {"who": "fin-1"}
    sure = await _live(live_env, "what's our burn", jev_says(kinds=["request"], whos=["ceo-1"]), humans=2)
    assert _asked(sure) == [("fin-1", "room")]  # one ask: the judge's teammate, whatever fits the part
    # Jev down for both calls: chat's fallback picks one agent, as before understanding
    live_env.judge.return_value = {"fallback": True}
    assert _asked(await _live(live_env, "what's our burn", None, humans=2)) == [("ceo-1", "room")]


async def test_live_a_line_to_a_person_is_silent(live_env):
    said = jev_says(kinds=["request"], whos=["fin-1"], names=["addressed"])
    got = await _live(live_env, "Aden, can you check the deck?", said, humans=2)
    assert got["agentId"] is None and got["agentIds"] == [] and got["ops"] == TO_PEOPLE
    assert "n0" in live_env.await_args.args[1]  # Jev was asked whether "Aden" is a name


async def test_live_something_asked_of_anyone_is_not_to_people(live_env):
    """[AX] D05: "what's our MRR, and Aden where did you put the deck?" must not silence Finance."""
    line = "what's our MRR, and Aden where did you put the deck?"
    said = jev_says(kinds=["request", "people"], whos=["fin-1", None], names=["addressed"])
    said["anyone"] = {"probability": 1.0}
    live_env.judge.return_value = {"who": "fin-1"}
    got = await _live(live_env, line, said, humans=2)
    assert _asked(got) == [("fin-1", "room")] and got["agentIds"] == ["fin-1"]
    assert got["ops"]["requests"][0]["task"] == "what's our MRR"  # Aden's part is not the agent's
    # the judge is told the line names Aden AND asks the room, and that it is not only to a person
    asked = live_env.judge.await_args
    assert asked.args[1]["addressing"] == {"people": ["user_b"], "anyone": True}
    assert asked.kwargs["to_people"] is False


async def test_live_the_wake_word_always_gets_someone(live_env):
    """ "Lightbulb, Aden needs the deck": a wake word is never to people; an unsure owner goes to the CEO."""
    said = jev_says(kinds=["request"], whos=[None], names=["addressed"])
    got = await _live(live_env, "Lightbulb, Aden needs the deck", said, humans=3, wake=True)
    assert got["agentIds"] == ["ceo-1"] and _asked(got) == [("ceo-1", "chief")]


async def test_live_the_wake_word_is_stripped_before_the_name_is_read(live_env):
    said = jev_says(kinds=["request"], names=["addressed"])
    got = await _live(live_env, "Lightbulb, Finance, what's our burn?", said, humans=2, wake=True)
    assert got["agentIds"] == ["fin-1"] and _asked(got) == [("fin-1", "name")]
    assert "Lightbulb" not in live_env.await_args.args[0]["latest"]["text"]


async def test_live_jev_timeout_falls_back(live_env):
    async def slow(*_a, **_k):
        await asyncio.sleep(1)

    live_env.side_effect = slow
    with patch.object(vc.messaging, "ADDRESSING_TIMEOUT_S", 0.01):
        alone = await _live(live_env, "hm what now", humans=1)
        two = await _live(live_env, "hm what now", humans=2)
    assert alone["agentIds"] == [] and alone["ops"]["requests"] == []  # one speaker, no name: no request
    assert two["agentIds"] == [] and two["ops"] == NOBODY  # two people: the judge said nobody


def test_live_transcript_becomes_history_before_the_line():
    transcript = "Pat: hi Aden\nFinance → Pat: burn is flat\nPat: and now?"
    rows = vc._transcript_history(transcript, LIVE_MEMBERS, "and now?")
    assert [(r["author"]["kind"], r["author"]["id"], r["text"], r["to"]) for r in rows] == [
        ("human", "Pat", "hi Aden", ["Aden"]),  # the names a line says stand in for @mentions
        ("agent", "fin-1", "burn is flat", ["Pat"]),  # the worker's arrow: who the voice answered
    ]


async def test_live_at_ages_the_history_rows_only_when_the_worker_sends_it():
    """An older worker sends no ``at``: rows carry no time."""
    rows = vc._transcript_history("Pat: a\nPat: b", LIVE_MEMBERS, "c")
    assert all("createdAt" not in r for r in rows)
    rows = vc._transcript_history("Pat: a\nPat: b", LIVE_MEMBERS, "c", at=[9, 100, 160])  # aligned to the end
    assert [r["createdAt"] for r in rows] == ["1970-01-01T00:01:40+00:00", "1970-01-01T00:02:40+00:00"]


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


@pytest.mark.asyncio
async def test_post_late_answer_is_the_agents_own_message_once_per_run(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(vc.messaging, "create_agent_message", create)
    monkeypatch.setattr(vc.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"}))
    room, agent = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    await vc.post_late_answer("org_a", room, agent, "run-9", "what's on my board?", "Three tasks open; two due today.")
    req = create.await_args.args[1]
    assert create.await_args.args[0] == "org_a"
    assert (req.event_id, str(req.room_id), str(req.agent_id)) == ("live-late:run-9", room, agent)
    assert req.text == 'From the Live call "what\'s on my board?":\n\nThree tasks open; two due today.'
    assert req.run_id is None  # a voice run is not a ledger run


@pytest.mark.asyncio
async def test_a_meetings_late_answer_is_kept_on_the_record_once_not_posted(monkeypatch):
    """A Meet's record room is hidden: an unheard answer goes to the notes and the mail instead."""
    meeting = {"status": "ended", "inCallAt": 1}

    async def patch_meeting(room_id, fields, drop=(), when=None):
        if when is not None and not when(meeting):
            return None
        meeting.update(fields(meeting))
        return meeting

    create = AsyncMock()
    monkeypatch.setattr(vc.messaging, "create_agent_message", create)
    monkeypatch.setattr(vc.messaging_repo, "get_room", AsyncMock(return_value={"meeting": meeting}))
    monkeypatch.setattr(vc.messaging_repo, "get_member", AsyncMock(return_value={"displayName": "Forge"}))
    monkeypatch.setattr(vc.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting))
    await vc.post_late_answer("org_a", "room-1", "a1", "run-9", "  open PRs?  ", "x" * 30_000)
    await vc.post_late_answer("org_a", "room-1", "a1", "run-9", "open PRs?", "again")  # the worker's retry
    create.assert_not_awaited()
    [kept] = meeting["unsaid"]
    assert kept["runId"] == "run-9" and kept["agentId"] == "a1" and kept["agentName"] == "Forge"
    assert kept["request"] == "open PRs?" and kept["answer"] == "x" * 2000 and kept["at"]
    # Cut by UTF-8 bytes, never mid-character: the record's item has a byte limit, not a character one.
    await vc.post_late_answer("org_a", "room-1", "a1", "run-cjk", "q", "\u6f22" * 3000)
    cjk = meeting["unsaid"][-1]["answer"]
    assert cjk == "\u6f22" * 666 and len(cjk.encode()) <= 2000
    for i in range(30):
        await vc.post_late_answer("org_a", "room-1", "a1", f"run-{i}", "q", "a")
    assert len(meeting["unsaid"]) == 20
    # After a series moved on (no call on the record), nothing lands on the next occurrence.
    meeting.clear()
    meeting["status"] = "scheduled"
    await vc.post_late_answer("org_a", "room-1", "a1", "run-late", "q", "a")
    assert "unsaid" not in meeting


async def test_a_meeting_summon_is_authorized_as_the_owner_and_a_live_stranger_is_still_403(start_env):
    members = [
        {"kind": "human", "id": "user_owner", "role": "owner"},
        {"kind": "human", "id": "user_m", "role": "member"},
        {"kind": "agent", "id": "ceo-1"},
    ]

    async def team(owner_id, user_id, room_id, **_):  # the real guard's shape: members only
        if user_id not in ("user_owner", "user_m"):
            raise vc._problem(403, code="not_a_member")
        return {"name": "sync", "ownerId": owner_id}, TEAM

    vc._room_team.side_effect = team
    with patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=members)):
        vc.messaging_repo.get_room.return_value = {"ownerId": "org_a", "meeting": {"status": "in_call"}}
        opened = await vc.open_voice_window(owner_id="org_a", room_id="room-1", speaker_user_id="meet:room-1")
        assert opened["joined"] is False
        vc._room_team.assert_awaited_with("org_a", "user_owner", "room-1")
        assert vc.put_call.await_args.args[0]["userId"] == "user_owner"  # the inviter's window and allowance
        vc.messaging_repo.get_room.return_value = {"ownerId": "org_a"}  # a Live room: no mapping
        for stranger in ("meet:room-1", "user_x"):
            with pytest.raises(HTTPException) as exc:
                await vc.open_voice_window(owner_id="org_a", room_id="room-1", speaker_user_id=stranger)
            assert exc.value.status_code == 403


def test_meeting_actor_maps_only_non_members_of_a_meeting():
    members = [{"kind": "human", "id": "user_owner", "role": "owner"}, {"kind": "human", "id": "user_m"}]
    assert vc._meeting_actor(members, "meet:r", True) == ("user_owner", "Someone")
    assert vc._meeting_actor(members, "Priya + Dan", True) == ("user_owner", "Priya + Dan")
    assert vc._meeting_actor(members, "user_m", True) == ("user_m", None)
    assert vc._meeting_actor(members, "meet:r", False) == ("meet:r", None)
    # The worker's Meet label is display only: the bot's line shows it, and nothing else reads it.
    assert vc._meeting_actor(members, "meet:r", True, " Priya  +\nDan ") == ("user_owner", "Priya + Dan")
    assert vc._meeting_actor(members, "meet:r", False, "Priya") == ("meet:r", None)
    assert vc._meeting_actor(members, "user_m", True, "Priya") == ("user_m", None)
    assert vc._meeting_actor(members[1:], "Priya", True) == ("Priya", "Priya")  # no owner member: 403s as before


def test_a_bridged_meet_person_acts_as_the_owner_under_their_own_name():
    members = [{"kind": "human", "id": "user_owner", "role": "owner"}]
    assert vc._meeting_actor(members, "meet:room-1:100", True, "Priya") == ("user_owner", "Priya")


async def test_live_two_names_with_nothing_asked_get_one_voice_and_no_requests(live_env):
    """ "Finance and Ada?" asks nothing: no run for either, and one voice (the first named) says a line."""
    got = await _live(live_env, "Finance and Ada?", jev_says(names=["addressed", "addressed"]), humans=2)
    assert got["ops"] == {**NOBODY, "social": "fin-1", "called": "fin-1", "say": _say("fin-1", "social")}
    assert got["agentIds"] == ["fin-1"] and "requests" not in got


async def test_voice_run_reads_what_teammates_were_asked_in_the_same_breath(run_env):
    await vc.dispatch_voice_run(
        call=WINDOW, run_id="run-1", agent_id="fin-1", speaker_id="user_b", utterance="the burn",
        transcript="", requests=['Ada is answering "the plan"'],
    )  # fmt: skip
    text = run_env.await_args.kwargs["json_body"]["request"]["input"]
    assert text.endswith(
        '[Asked in the same breath; teammates are on these:\nAda is answering "the plan"]\n\nBea (voice): the burn'
    )


async def test_no_post_marks_this_rooms_voice_run_only(run_env):
    vc.messaging_repo.get_voice_run.return_value = {"runId": "run-1", "roomId": "room-1", "agentId": "fin-1"}
    assert await vc.mark_no_post("org_a", "room-1", "run-1") is True
    vc.messaging_repo.update_voice_run.assert_awaited_once_with("org_a", "run-1", {"noPost": True})
    vc.messaging_repo.put_voice_run.assert_not_awaited()
    assert await vc.mark_no_post("org_a", "room-2", "run-1") is False  # another room's run


async def test_no_post_on_a_missing_or_expired_run_writes_nothing(run_env):
    vc.messaging_repo.get_voice_run.return_value = None
    assert await vc.mark_no_post("org_a", "room-1", "run-1") is False
    vc.messaging_repo.get_voice_run.return_value = {"roomId": "room-1"}
    vc.messaging_repo.update_voice_run.return_value = False  # expired between the read and the SET
    assert await vc.mark_no_post("org_a", "room-1", "run-1") is False
    vc.messaging_repo.put_voice_run.assert_not_awaited()


async def test_a_turns_retry_never_reads_or_rewrites_no_post(run_env):
    """The row write is one atomic SET of the turn's own fields (the repo test pins that a mark survives it)."""
    await _dispatch()
    assert "noPost" not in vc.messaging_repo.put_voice_run.await_args.args[2]
    vc.messaging_repo.get_voice_run.assert_not_awaited()


def test_turn_note_checks_now_and_answers_one_request():
    """Conversation understanding §4.7 (prod 2026-10-04: a stale answer from memory in 5 s, two of
    three asks dropped). The note is static: Hermes caches the prompt prefix it sits in."""
    note = vc.TURN_NOTE
    assert "must be checked with a tool in this turn before you answer" in note
    assert "never what the answer is" in note and "never give an old value as current" in note
    assert "answer only what is asked here" in note
    # Meet run 2026-10-07: "Tally is handling the Workspace pricing question." was spoken as the answer. A
    # name is not forbidden (an answer can need one): narrating the team's work and passing the question on are.
    assert "Answer it from your own knowledge and tools" in note and "say what is missing" in note
    assert "Do not narrate who is handling what, and do not hand the question to a teammate" in note
    assert "name a person or a teammate only when the answer itself requires it" in note
    assert "never say a teammate's name" not in note
    assert "if it holds more than one, do each and give each result" in note  # a missed cut is still worked
    assert "speed matters more than completeness" not in note
    assert "from what you already know" not in note
    for way in ("- Answer:", "- Ask:", "- Hand it off:", "- Cannot:"):
        assert way in note
    assert "{" not in note.replace("${", "")  # no per-run placeholder
    # no step cap (owner ruling 2026-10-05): the run takes the steps it needs and speaks updates as it goes
    assert "hard limit" not in note and "Take as many steps as the request needs" in note
    assert "what you found so far or what you are checking next" in note


# ---- ops.say: who speaks now, and about what (driven voice) ----


async def test_live_say_names_the_agent_and_the_ask_and_nothing_for_people_or_jev_down(live_env):
    asked = await _live(live_env, "Finance, what's our burn", jev_says(kinds=["request"], names=["addressed"]))
    assert asked["ops"]["say"] == _say("fin-1", "ack", "what's our burn")
    thanks = await _live(live_env, "Thanks, Finance.", jev_says(kinds=["social"], names=["addressed"]))
    assert thanks["ops"]["say"] == _say("fin-1", "social") and thanks["ops"]["requests"] == []
    people = await _live(live_env, "did you see the game", jev_says(kinds=["people"]), humans=2)
    assert people["ops"]["say"] is None
    down = await _live(live_env, "Finance, what's our burn", None)  # Jev down: the request runs, nobody speaks
    assert _asked(down) == [("fin-1", "name")] and down["ops"]["say"] is None


async def test_a_window_logs_how_long_each_of_its_steps_took(start_env, caplog):
    """10-08 Meet: a call's first line waited 4.7 s on /summon and /session, and nothing said on which step."""
    with caplog.at_level("INFO", logger=vc.logger.name):
        await _start()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("voice window opened")]
    assert "room=room-1 billing=free ms: room=" in line
    assert [step.split("=")[0] for step in line.split("ms: ")[1].split()] == [
        "room", "open_windows", "gate", "owner_row", "claim", "put_call", "total",
    ]  # fmt: skip
    vc.container_repo.get_by_status.return_value = [OPEN_WINDOW]
    with caplog.at_level("INFO", logger=vc.logger.name):
        await _start()
    assert any(r.getMessage().startswith("voice window joined room=room-1 ms: room=") for r in caplog.records)


def test_steps_are_timed_one_after_the_other(monkeypatch):
    clock = iter([10.0, 10.25, 12.25])
    monkeypatch.setattr(vc.time, "perf_counter", lambda: next(clock))
    steps = vc._Steps()
    steps.done("room")
    steps.done("gate")
    assert steps.ms == {"room": 250, "gate": 2000} and str(steps) == "room=250 gate=2000 total=2250"


async def test_a_meet_run_reads_rooms_as_the_member_who_invited_the_agents(run_env):
    """Owner ruling 2026-10-13: in a Google Meet the agent reads with the inviter's access. Rooms only:
    the speaker is no member, so the run's requester (the Mac, local sessions) stays nobody."""
    vc.messaging_repo.get_room.return_value = {**ROOM_META, "meeting": {"title": "Sync"}}
    members = [m for m in vc.messaging_repo.list_members.return_value if m.get("kind") != "human"]
    vc.messaging_repo.list_members.return_value = members + [
        {"kind": "human", "id": "user_inviter", "role": "owner", "displayName": "Ina"}
    ]
    vc.messaging_repo.put_voice_run.return_value = {"runId": "run-1", "requesterUserId": None}
    mint = MagicMock(return_value="grant")
    with (
        patch.object(vc.local_session_grants, "mint", mint),
        patch.object(vc.messaging_repo, "member_home", MagicMock(return_value="org_a")),
    ):
        await vc.dispatch_voice_run(
            call=WINDOW, run_id="run-1", agent_id="fin-1", speaker_id="meet:room-1", utterance="what did we decide",
            transcript="", speaker_name="Prospect",
        )  # fmt: skip
    mint.assert_called_once_with("org_a", "room-1", "fin-1", "run-1", None, "user_inviter")
    fields = vc.messaging_repo.put_voice_run.await_args.args[2]
    assert (fields["requesterUserId"], fields["readerUserId"]) == (None, "user_inviter")


async def test_a_steer_on_a_meet_run_keeps_its_reader(run_env):
    vc.messaging_repo.get_voice_run.return_value = {**VOICERUN, "readerUserId": "user_inviter"}
    mint = MagicMock(return_value="lowered")
    with patch.object(vc.local_session_grants, "mint", mint):
        await vc.steer_voice_run(
            call=WINDOW, run_id="run-1", agent_id="fin-1", speaker_id="meet:room-1", utterance="x", speaker_name="Z"
        )
    mint.assert_called_once_with("org_a", "room-1", "fin-1", "run-1", None, "user_inviter")
