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


def _work(pid: str, title="Draft the holiday-hours post", brief="b"):
    return voice_meet.WorkIn(
        ownerId="org_1", roomToken="t", title=title, brief=brief, participantId=pid, speakerName="Maya"
    )


async def test_member_ask_starts_a_task_and_a_follow_up(m):
    out = await voice_meet.start_work(ROOM, _work("11"))
    assert out == {"started": True, "triggerId": "t", "taskId": "k1"}
    assert m["put_run"].await_args.args[2]["requesterUserId"] == "user_maya"
    [f] = m["meeting"]["followUps"]
    assert (f["text"], f["status"], f["taskId"]) == ("Draft the holiday-hours post", "working", "k1")


async def test_non_host_member_is_member(m):
    m["meeting"]["roster"]["13"] = {"name": "Lee", "in": True, "email": "Lee@Salon.com"}
    with patch.object(
        voice_meet.questions, "member_emails", AsyncMock(return_value={"maya@salon.com", "lee@salon.com"})
    ):
        out = await voice_meet.start_work(ROOM, _work("13"))
    assert out == {"started": True, "triggerId": "t", "taskId": "k1"}
    assert m["sw"].await_args.kwargs.get("origin") is None


async def test_guest_order_ask_creates_card(m):
    """A guest's ask starts the work marked origin=meeting, so effect_check puts a card in front of Maya."""
    with patch.object(voice_meet.clerk_admin, "get_user", AsyncMock(return_value={"first_name": "Maya"})):
        out = await voice_meet.start_work(ROOM, _work("12", title="Order the gel colours", brief="$212"))
    assert out["started"] and out["needs_ok"] and out["say"] == "I'll need Maya's OK for that."
    assert m["sw"].await_args.kwargs["origin"] == "meeting"
    assert m["put_run"].await_args.args[2]["requesterUserId"] == "user_maya"


async def test_unknown_speaker_is_guest(m):
    for pid in ("99", ""):  # not on the roster, nobody heard
        body = voice_meet.ReadIn(ownerId="org_1", roomToken="t", kind="memory", participantId=pid)
        assert (await voice_meet.read(ROOM, body))["reason"] == "needs_member"


async def test_display_name_matching_two_members_is_guest(m):
    m["meeting"]["roster"]["14"] = {"name": "Sam Lee", "in": True}
    two = [{"first_name": "Sam", "last_name": "Lee"}, {"first_name": "sam", "last_name": "LEE"}]
    body = voice_meet.ReadIn(ownerId="org_1", roomToken="t", kind="memory", participantId="14")
    with patch.object(voice_meet.clerk_admin, "list_organization_members", AsyncMock(return_value=two)):
        assert (await voice_meet.read(ROOM, body))["reason"] == "needs_member"
    with patch.object(voice_meet.clerk_admin, "list_organization_members", AsyncMock(return_value=two[:1])):
        page = {"results": []}
        with patch.object(voice_meet.memory_api, "recall_page", AsyncMock(return_value=page)):
            assert "reason" not in await voice_meet.read(ROOM, body)  # one match: a member


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


async def test_call_stats_month_vs_month(m):
    """call_stats over {from, to}: "bookings this month versus last" is two ranges, local days inclusive."""
    starts = {"r1": "2026-10-01T09:00:00-04:00", "r2": "2026-10-31T23:30:00-04:00", "r3": "2026-11-03T10:00:00-05:00",
              "r4": "2026-09-30T23:59:00-04:00"}  # fmt: skip
    rows = [{"roomId": r, "name": "Call · x", "createdAt": s} for r, s in starts.items()]
    rooms = {r: {"call": {"startedAt": s, "outcome": "booked"}} for r, s in starts.items()}
    with (
        patch.object(voice_meet.messaging_repo, "_query", AsyncMock(return_value=rows)),
        patch.object(voice_meet.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms[r])),
    ):

        def body(frm, to):
            return voice_meet.ReadIn.model_validate(
                {"ownerId": "o", "roomToken": "t", "kind": "calls", "from": frm, "to": to}
            )

        october = await voice_meet.read(ROOM, body("2026-10-01", "2026-10-31"))
        november = await voice_meet.read(ROOM, body("2026-11-01", "2026-11-30"))
    assert october == {"from": "2026-10-01", "to": "2026-10-31", "calls": 2, "byOutcome": {"booked": 2}}
    assert november == {"from": "2026-11-01", "to": "2026-11-30", "calls": 1, "byOutcome": {"booked": 1}}


async def test_a_read_nothing_here_can_do_is_handed_to_work(m):
    with patch.object(
        voice_meet.integration_reads, "read", AsyncMock(return_value=voice_meet.integration_reads.START_WORK)
    ):
        body = voice_meet.IntegrationIn(ownerId="org_1", roomToken="t", provider="x", query="q", participantId="11")
        assert await voice_meet.integration_read(ROOM, body) == {"start_work": True, "say": "I'll get that"}
