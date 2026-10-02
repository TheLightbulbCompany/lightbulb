import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.services import voice_calls as vc

TEAM = [{"id": "ceo-1", "name": "Ada"}, {"id": "fin-1", "name": "Finance"}]


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
        patch.object(vc.jev, "agent_card", AsyncMock(return_value="card")),
        patch.object(vc, "put_call", AsyncMock()),
        patch.object(vc, "_prewake", MagicMock()),
        patch.object(vc, "_claim_free", AsyncMock()),
        patch.object(vc, "_release_free", AsyncMock()),
    ]
    for p in ps:
        p.start()
    yield
    patch.stopall()


async def _start():
    return await vc.start_huddle_call(owner_id="org_a", room_id="room-1", speaker_user_id="user_b")


async def test_huddle_start_opens_a_huddle_row(start_env):
    out = await _start()
    call = vc.put_call.await_args.args[0]
    assert call["kind"] == "huddle" and call["sessionId"] is None and call["userId"] == "user_b"
    assert call["frames"] == {"sink": "huddle", "roomId": "room-1"}
    assert call["voiceAgentId"] == "ceo-1" and call["billing"] == "free" and call["deadline"] - call["startedAt"] == 600
    vc._prewake.assert_called_once_with(call)
    assert out["callId"] == call["callId"] and out["voiceName"] == "Lightbulb" and out["team"] == TEAM
    assert (
        out["persona"].startswith("You are Lightbulb,")
        and out["voice"] == "marin"
        and out["deadline"] == call["deadline"]
    )


async def test_huddle_start_without_a_live_key_is_503(start_env):
    with patch.object(vc.settings, "OPENAI_API_KEY", "not-a-key"):  # CDK's random seed counts as unset
        with pytest.raises(HTTPException) as exc:
            await _start()
    assert exc.value.status_code == 503 and exc.value.detail["code"] == "voice_unavailable"
    vc.put_call.assert_not_awaited()


async def test_huddle_voice_is_marin_without_the_ceo(start_env):
    vc._room_team.return_value = ({"name": "eng", "ownerId": "org_a"}, [{"id": "eng-1", "name": "Engineering"}])
    assert (await _start())["voice"] == "marin"  # GPT-Live's voices; Live is always Lightbulb


async def test_huddle_start_busy_free_claim_is_409(start_env):
    vc._claim_free.side_effect = vc._problem(409, code="voice_busy")
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "voice_busy"
    vc.put_call.assert_not_awaited()


async def test_huddle_start_credits_low_is_402_and_releases(start_env):
    with (
        patch.object(vc.voice_billing, "billing_mode", return_value="credits"),
        patch.object(vc, "_hold", AsyncMock(return_value=(0, 0))),
        patch.object(vc, "release_hold", AsyncMock()) as release,
    ):
        with pytest.raises(HTTPException) as exc:
            await _start()
    assert exc.value.status_code == 402 and exc.value.detail == {"code": "voice_limit", "reason": "credits_low"}
    release.assert_awaited_once_with("org_a", 0)
    vc.put_call.assert_not_awaited()


async def test_huddle_put_failure_releases_hold(start_env):
    vc.put_call.side_effect = RuntimeError("ddb down")
    with (
        patch.object(vc.voice_billing, "billing_mode", return_value="credits"),
        patch.object(vc, "_hold", AsyncMock(return_value=(30_000, 300))),
        patch.object(vc, "release_hold", AsyncMock()) as release,
    ):
        with pytest.raises(RuntimeError):
            await _start()
    release.assert_awaited_once_with("org_a", 30_000)


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


def _turn_patches(call):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {}}
    state = {}

    def claim(**kw):
        state["run"] = kw["ExpressionAttributeValues"][":t"]["runId"]
        return {"Attributes": {}}

    table.update_item.side_effect = claim
    rt = AsyncMock(return_value=MagicMock(status_code=202))
    get = AsyncMock(
        side_effect=lambda _id: (
            {**call, "turns": {"fin-1": {"runId": state["run"], "seq": 1}}} if "run" in state else call
        )
    )
    return rt, [
        patch.object(vc, "get_call", get),
        patch.object(vc, "_table", return_value=table),
        patch.object(vc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw"})),
        patch.object(vc.runtime_transport, "request", rt),
        patch.object(vc, "_stop_turn", AsyncMock()),
        patch.object(vc.voice_routing, "pick", AsyncMock(return_value="fin-1")),
        patch.object(vc.settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai/"),
    ]


async def test_huddle_turn_by_another_speaker_routes_and_sinks_frames_to_backend():
    rt, ps = _turn_patches(HUDDLE_CALL)
    for p in ps:
        p.start()
    try:
        out = await vc.start_huddle_turn(
            call_id="call-h", owner_id="org_a", speaker_user_id="user_b", seq=1, utterance="ask finance", transcript=""
        )
        body = rt.await_args.kwargs["json_body"]
        assert body["request"]["frames"] == {
            "sink": "backend",
            "endpoint": "https://api-dev.flickmeon.ai/api/v1/internal/calls/call-h/frames",
            "channel": "backend",
        }
        # The bridge's _call_envelope 400s a call run unless frames has a non-empty endpoint AND channel.
        assert all(
            isinstance(body["request"]["frames"].get(k), str) and body["request"]["frames"][k]
            for k in ("endpoint", "channel")
        )
        assert out == {"turnId": body["identity"]["runId"], "agentId": "fin-1"}
        assert vc.voice_routing.pick.await_args.args[1]["kind"] == "room"  # huddles route like rooms
    finally:
        patch.stopall()


@pytest.mark.parametrize(
    "row",
    [
        {**HUDDLE_CALL, "kind": "room"},
        {**HUDDLE_CALL, "ownerId": "org_b"},
        {**HUDDLE_CALL, "status": "call_closing"},
        None,
    ],
)
async def test_huddle_turn_on_a_non_huddle_or_foreign_call_is_404(row):
    with patch.object(vc, "get_call", AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc:
            await vc.start_huddle_turn(
                call_id="call-h", owner_id="org_a", speaker_user_id="user_a", seq=1, utterance="x", transcript=""
            )
    assert exc.value.status_code == 404 and exc.value.detail["code"] == "call_not_found"


def _close_env(call):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {**call, "status": "call_closing"}}
    table.name = "lightbulb-dev-containers"
    table.meta.client = MagicMock()
    return table, [
        patch.object(vc, "_table", return_value=table),
        patch.object(vc, "_stop_turn", AsyncMock()),
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


def _msg(seq, who, text, **extra):
    return {"messageSeq": seq, "author": {"kind": "human", "id": f"u{seq}", "displayName": who}, "text": text, **extra}


async def _huddle_turn_body(rows):
    rt, ps = _turn_patches(HUDDLE_CALL)
    ps += [
        patch.object(vc.messaging, "human_display_name", AsyncMock(return_value="Bea")),
        patch.object(vc.messaging_repo, "get_room", AsyncMock(return_value={"lastSeq": len(rows)})),
        patch.object(vc.messaging_repo, "list_messages_before", AsyncMock(return_value=rows)),
    ]
    for p in ps:
        p.start()
    try:
        await vc.start_huddle_turn(
            call_id="call-h",
            owner_id="org_a",
            speaker_user_id="user_b",
            seq=1,
            utterance="ask finance",
            transcript="Bea: hi",
        )
        return rt.await_args.kwargs["json_body"], vc.messaging_repo.list_messages_before
    finally:
        patch.stopall()


async def test_huddle_turn_carries_the_voice_note_speaker_and_room_history():
    rows = [
        _msg(1, "Ada", "revenue is up"),
        _msg(2, "Cy", "gone", deletedAt="x"),
        _msg(3, "Cy", "old", editedText="new"),
    ]
    body, listed = await _huddle_turn_body(rows)
    req = body["request"]
    assert req["instructions"] == vc.TURN_NOTE and "paperclip-api.sh create" in vc.TURN_NOTE
    text = req["input"]
    assert "Speaker: Bea (user id user_b)" in text and "#general (room id room-1)" in text
    assert "Ada: revenue is up\nCy: new" in text and "gone" not in text
    assert text.endswith("Bea: hi]\n\nask finance")
    assert listed.await_args.args == ("room-1", 4, vc._ROOM_HISTORY)


async def test_huddle_turn_context_is_bounded_and_keeps_the_newest():
    rows = [_msg(i, "Ada", f"m{i} " + "x" * 1000) for i in range(1, 21)]
    body, _ = await _huddle_turn_body(rows)
    context = body["request"]["input"].split("\n\n", 1)[0]
    assert len(context) <= vc._CONTEXT_CHARS
    assert "m20 " in context and "m1 " not in context
    assert all(len(line.rstrip("]")) <= len("Ada: ") + vc._ROOM_MESSAGE_CHARS for line in context.splitlines())


async def test_turn_context_failure_still_dispatches_with_speaker_id():
    with patch.object(vc.messaging, "human_display_name", AsyncMock(side_effect=RuntimeError("clerk down"))):
        out = await vc._turn_context({"callId": "c", "roomId": None}, "user_b")
    assert out.startswith("[Live voice request context") and "user id user_b" in out
    assert "one-to-one voice call" in out


async def test_huddle_start_without_a_live_plan_is_402(start_env):
    vc.voice_billing.billing_mode.return_value = None
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == 402 and exc.value.detail["reason"] == "plan_inactive"
    vc.put_call.assert_not_awaited()


async def test_huddle_start_free_minutes_used_is_402(start_env):
    vc.voice_billing.budget_seconds.return_value = vc.voice_billing.MIN_CALL_SECONDS - 1
    with pytest.raises(HTTPException) as exc:
        await _start()
    assert exc.value.status_code == 402 and exc.value.detail["reason"] == "free_minutes_used"
    vc._claim_free.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError("ddb"), asyncio.CancelledError()])
async def test_huddle_start_failed_put_releases_the_free_claim(start_env, error):
    vc.put_call.side_effect = error
    with patch.object(vc, "release_hold", AsyncMock()), pytest.raises(type(error)):
        await _start()
    call_id = vc._claim_free.await_args.args[1]
    vc._release_free.assert_awaited_once_with("org_a", call_id)
