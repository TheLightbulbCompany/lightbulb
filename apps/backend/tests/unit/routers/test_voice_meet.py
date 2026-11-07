"""The meeting agent's backend: who may act, one task per ask, chat vs link, the meeting record."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from routers import voice_meet

ROOM = "7f1d2c3b-0000-4000-8000-000000000001"
MEMBERS = [{"kind": "human", "id": "user_maya", "role": "owner"}, {"kind": "agent", "id": "a1"}]
MEETING = {
    "title": "Monday",
    "recallBotId": "bot_1",
    "inCallAt": 100,
    "organizer": "maya@salon.com",
    "invitedBy": "Maya@salon.com",
    "roster": {"11": {"name": "Maya", "in": True, "host": True}, "12": {"name": "Sam", "in": True}},
}


def test_only_the_host_who_is_the_inviting_organizer_is_the_member():
    assert voice_meet.member_speaking(MEETING, MEMBERS, "11") == "user_maya"
    assert voice_meet.member_speaking(MEETING, MEMBERS, "12") is None  # a guest
    assert voice_meet.member_speaking(MEETING, MEMBERS, "") is None  # nobody heard
    # someone else's meeting the member forwarded: its host is not the member
    other = {**MEETING, "organizer": "boss@elsewhere.com"}
    assert voice_meet.member_speaking(other, MEMBERS, "11") is None


@pytest.fixture
def m():
    meeting = dict(MEETING)
    patched = {}

    async def fake_patch(room_id, fields, drop=(), when=None, **_):
        if when is not None and not when(meeting):
            return None
        meeting.update(fields(meeting) if callable(fields) else fields)
        return meeting

    with (
        patch.object(voice_meet, "_meeting", AsyncMock(side_effect=lambda r, b: (ROOM, {}, meeting))),
        patch.object(voice_meet.meeting_invites, "_patch_meeting", AsyncMock(side_effect=fake_patch)),
        patch.object(voice_meet.meeting_bots, "say", AsyncMock()) as say,
        patch.object(voice_meet.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(voice_meet.messaging_repo, "get_voice_run", AsyncMock(return_value=None)) as get_run,
        patch.object(voice_meet.messaging_repo, "put_voice_run", AsyncMock()) as put_run,
        patch.object(voice_meet, "start_voice_work", AsyncMock(return_value={"triggerId": "t", "taskId": "k1"})) as sw,
    ):
        patched.update(meeting=meeting, say=say, get_run=get_run, put_run=put_run, sw=sw)
        yield patched


def _work(pid: str, title="Draft the holiday-hours post"):
    return voice_meet.WorkIn(
        ownerId="org_1", roomToken="t", title=title, brief="b", participantId=pid, speakerName="Maya"
    )


async def test_member_ask_starts_a_task_and_a_follow_up(m):
    out = await voice_meet.start_work(ROOM, _work("11"))
    assert out == {"started": True, "triggerId": "t", "taskId": "k1"}
    assert m["put_run"].await_args.args[2]["requesterUserId"] == "user_maya"
    [f] = m["meeting"]["followUps"]
    assert (f["text"], f["status"], f["taskId"]) == ("Draft the holiday-hours post", "working", "k1")


async def test_guest_ask_starts_nothing(m):
    out = await voice_meet.start_work(ROOM, _work("12"))
    assert out["started"] is False and out["reason"] == "needs_member"
    m["sw"].assert_not_awaited()
    m["put_run"].assert_not_awaited()


async def test_repeat_ask_is_already_on_it(m):
    await voice_meet.start_work(ROOM, _work("11"))
    run_id = m["put_run"].await_args.args[1]
    m["get_run"].return_value = {"roomId": ROOM}
    out = await voice_meet.start_work(ROOM, _work("11", title="  draft the HOLIDAY-hours post"))
    assert out == {"already": True, "taskId": "k1"}
    assert m["sw"].await_count == 1 and m["get_run"].await_args.args[1] == run_id


async def test_announce_once(m):
    with (
        patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")),
        patch.object(voice_meet.integration_reads, "warm_google", AsyncMock()) as warm,
    ):
        body = voice_meet.StartIn(ownerId="org_1", roomToken="t", company="Maya's Studio")
        assert (await voice_meet.start(ROOM, body))["assistantName"] == "Nova"
        await voice_meet.start(ROOM, body)  # a restarted worker does not say it again
        await asyncio.sleep(0)
    warm.assert_awaited_with("org_1")  # the Google grant is warmed at the start, not on the first read
    [call] = m["say"].await_args_list
    assert call.args[0] == "bot_1" and 'says "Nova, ..."' in call.args[1] and "members can approve" in call.args[1]


async def test_a_failed_announcement_still_starts_the_meeting(m, caplog):
    m["say"].side_effect = voice_meet.meeting_bots.RecallError("400")
    with (
        patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")),
        patch.object(voice_meet.integration_reads, "warm_google", AsyncMock()),
    ):
        out = await voice_meet.start(ROOM, voice_meet.StartIn(ownerId="org_1", roomToken="t"))
        await asyncio.sleep(0)
    assert out["assistantName"] == "Nova"
    assert "announce failed 400" in caplog.text


@pytest.mark.parametrize("days", [0, -3])
def test_today_or_less_reads_one_day(days):
    assert voice_meet.ReadIn(ownerId="o", roomToken="t", kind="calls", days=days).days == 1


async def test_short_text_to_chat_long_text_as_a_link(m):
    with (
        patch.object(voice_meet.meeting_invites, "_post_line", AsyncMock()) as post,
        patch.object(voice_meet.meeting_invites, "output_room", AsyncMock(return_value="room_out")),
    ):
        assert (
            await voice_meet.chat(
                ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text="Hours: 9-5", participantId="11")
            )
        )["sent"] == "chat"
        long = "Holiday hours draft\n" + "x" * 900
        out = await voice_meet.chat(ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text=long, participantId="11"))
    assert out["sent"] == "link" and out["link"].endswith("/chat?room=room_out")
    assert post.await_args.args[3] == long
    assert m["say"].await_args.args[1].startswith("Holiday hours draft ... Full text: ")


async def test_end_keeps_the_record(m):
    body = voice_meet.EndIn(
        ownerId="o",
        roomToken="t",
        summary="Asked twice",
        asked=[{"t": 5, "who": "Maya", "q": "no-shows?", "a": "Two."}],
    )
    await voice_meet.end(ROOM, body)
    assert m["meeting"]["asked"] == [{"t": 5, "who": "Maya", "q": "no-shows?", "a": "Two."}]
    assert m["meeting"]["summary"] == "Asked twice"


async def _settle():
    here = asyncio.get_running_loop()
    await asyncio.gather(
        *[t for t in voice_meet._warming if t.get_loop() is here]
    )  # end() returns at once; the decisions pass is held


async def test_end_writes_decisions_to_meta_and_memory(m):
    m["meeting"]["title"] = "Team meeting"
    body = voice_meet.EndIn(ownerId="o", roomToken="t")
    with (
        patch.object(voice_meet.meeting_notes, "_transcript_text", AsyncMock(return_value="Maya: closed Dec 25.")),
        patch.object(voice_meet.memory_models, "decisions", AsyncMock(return_value=["Closed Dec 25"])) as model,
        patch.object(voice_meet.memory_api, "remember", AsyncMock(return_value={})) as rem,
    ):
        await voice_meet.end(ROOM, body)
        await _settle()
    model.assert_awaited_once()
    assert m["meeting"]["decisions"] == ["Closed Dec 25"]
    rem.assert_awaited_once()
    assert rem.await_args.args == ("o",)
    kw = rem.await_args.kwargs
    assert kw["subject"] == "Decision: Team meeting" and kw["kind"] == "decision" and kw["scope"] == "team"
    # the engine ignores `source` for the cloud member, so the provenance is in the fact a later recall returns
    assert kw["fact"].startswith("Decided in Team meeting, ") and kw["fact"].endswith(": Closed Dec 25")
    assert kw["source"] == f"meeting:{ROOM}"


async def test_decisions_do_not_land_on_the_next_occurrence(m):
    async def advance(*_a, **_k):
        m["meeting"]["startAt"] = 999  # the series moved on while the model ran
        return ["Closed Dec 25"]

    with (
        patch.object(voice_meet.meeting_notes, "_transcript_text", AsyncMock(return_value="x")),
        patch.object(voice_meet.memory_models, "decisions", AsyncMock(side_effect=advance)),
        patch.object(voice_meet.memory_api, "remember", AsyncMock(return_value={})),
    ):
        await voice_meet.end(ROOM, voice_meet.EndIn(ownerId="o", roomToken="t"))
        await _settle()
    assert "decisions" not in m["meeting"]


async def test_empty_transcript_writes_nothing_and_skips_the_model(m):
    with (
        patch.object(voice_meet.meeting_notes, "_transcript_text", AsyncMock(return_value=None)),
        patch.object(voice_meet.memory_models, "decisions", AsyncMock()) as model,
        patch.object(voice_meet.memory_api, "remember", AsyncMock()) as rem,
    ):
        await voice_meet.end(ROOM, voice_meet.EndIn(ownerId="o", roomToken="t"))
        await _settle()
    model.assert_not_awaited()
    rem.assert_not_awaited()
    assert "decisions" not in m["meeting"]


async def test_integration_read_is_the_members_own_speech_only(m):
    with patch.object(voice_meet.integration_reads, "read", AsyncMock(return_value="Invoice 42")) as read:

        def body(pid):
            return voice_meet.IntegrationIn(
                ownerId="org_1", roomToken="t", provider="gmail", query="invoice", participantId=pid
            )

        assert (await voice_meet.integration_read(ROOM, body("11")))["text"] == "Invoice 42"
        assert "Only a member" in (await voice_meet.integration_read(ROOM, body("12")))["text"]
    read.assert_awaited_once_with("org_1", "gmail", "invoice")


async def test_a_guest_cannot_read_memory_or_bookings_or_post_to_chat(m):
    """Gate B1: the whole meeting hears a read and sees the chat; a guest gets needs_member, a member is answered."""
    page = {"results": [{"content": "Prices go up in May"}]}
    with (
        patch.object(voice_meet.memory_api, "recall_page", AsyncMock(return_value=page)) as recall,
        patch.object(
            voice_meet, "_bookings", AsyncMock(return_value=[{"sk": "VOICEBOOK#2026-11-09T10:00", "name": "Ana"}])
        ),
    ):
        for kind in ("memory", "calendar"):
            guest = voice_meet.ReadIn(ownerId="o", roomToken="t", kind=kind, participantId="12")
            assert (await voice_meet.read(ROOM, guest))["reason"] == "needs_member", kind
            member = voice_meet.ReadIn(ownerId="o", roomToken="t", kind=kind, participantId="11")
            assert "reason" not in await voice_meet.read(ROOM, member), kind
        assert recall.await_count == 1
        calls = voice_meet.ReadIn(ownerId="o", roomToken="t", kind="calls", participantId="12")
        with patch.object(voice_meet, "_calls", AsyncMock(return_value={"calls": 3})):
            assert (await voice_meet.read(ROOM, calls))["calls"] == 3  # counts stay open
    out = await voice_meet.chat(
        ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text="Ana at 10", participantId="12")
    )
    assert out["reason"] == "needs_member"
    m["say"].assert_not_awaited()


async def test_assistant_name_from_the_agent_page(m):
    with patch.object(voice_meet.messaging_repo, "get_voice_agent", AsyncMock(side_effect=[{"name": " Nova "}, None])):
        assert await voice_meet.assistant_name("org_1") == "Nova"
        assert await voice_meet.assistant_name("org_1") == "Lightbulb"


def test_call_start_times_compare_across_offsets():
    assert voice_meet._started({"startedAt": "2026-11-07T07:00:00-05:00"}) == voice_meet._started(
        {"startedAt": "2026-11-07T12:00:00+00:00"}
    )
    assert voice_meet._started({}).year == 1
