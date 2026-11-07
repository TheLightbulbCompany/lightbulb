"""The meeting agent's backend: who may act, one task per ask, chat vs link, the meeting record."""

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
    with patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")):
        body = voice_meet.StartIn(ownerId="org_1", roomToken="t", company="Maya's Studio")
        assert (await voice_meet.start(ROOM, body))["assistantName"] == "Nova"
        await voice_meet.start(ROOM, body)  # a restarted worker does not say it again
    [call] = m["say"].await_args_list
    assert call.args[0] == "bot_1" and 'says "Nova, ..."' in call.args[1] and "members can approve" in call.args[1]


async def test_short_text_to_chat_long_text_as_a_link(m):
    with (
        patch.object(voice_meet.meeting_invites, "_post_line", AsyncMock()) as post,
        patch.object(voice_meet.meeting_invites, "output_room", AsyncMock(return_value="room_out")),
    ):
        assert (await voice_meet.chat(ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text="Hours: 9-5")))[
            "sent"
        ] == "chat"
        long = "Holiday hours draft\n" + "x" * 900
        out = await voice_meet.chat(ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text=long))
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


async def test_integration_read_is_the_members_own_speech_only(m):
    with patch.object(voice_meet.integration_reads, "read", AsyncMock(return_value="Invoice 42")) as read:

        def body(pid):
            return voice_meet.IntegrationIn(
                ownerId="org_1", roomToken="t", provider="gmail", query="invoice", participantId=pid
            )

        assert (await voice_meet.integration_read(ROOM, body("11")))["text"] == "Invoice 42"
        assert "Only a member" in (await voice_meet.integration_read(ROOM, body("12")))["text"]
    read.assert_awaited_once_with("org_1", "gmail", "invoice")


async def test_assistant_name_from_the_agent_page(m):
    with patch.object(voice_meet.messaging_repo, "get_voice_agent", AsyncMock(side_effect=[{"name": " Nova "}, None])):
        assert await voice_meet.assistant_name("org_1") == "Nova"
        assert await voice_meet.assistant_name("org_1") == "Lightbulb"
