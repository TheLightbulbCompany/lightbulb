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
        patch.object(voice_meet.livekit_huddles, "set_meet_phase", AsyncMock()) as phase,
        patch.object(voice_meet, "start_voice_work", AsyncMock(return_value={"triggerId": "t", "taskId": "k1"})) as sw,
        patch.object(voice_meet.voice_state, "context", AsyncMock(return_value="ctx")) as ctx,
    ):
        patched.update(phase=phase, meeting=meeting, say=say, get_run=get_run, put_run=put_run, sw=sw, ctx=ctx)
        yield patched
    voice_meet._clears.clear()


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


async def test_guest_start_work_runs_as_inviter(m):
    out = await voice_meet.start_work(ROOM, _work("12"))
    assert out["started"] is True
    assert m["put_run"].await_args.args[2]["requesterUserId"] == "user_maya"
    assert m["sw"].await_args.kwargs["confirm_all"] is True  # a meeting's effects always get a card


async def test_record_keeps_asker_name(m):
    body = _work("12").model_copy(update={"speakerName": ""})
    await voice_meet.start_work(ROOM, body)
    [f] = m["meeting"]["followUps"]
    assert f["who"] == "Sam"  # the roster name, for the record only
    assert m["put_run"].await_args.args[2]["requesterUserId"] == "user_maya"


async def test_no_inviter_no_host_refused(m):
    m["meeting"].pop("invitedBy")
    m["meeting"]["roster"] = {"12": {"name": "Sam", "in": True}}
    out = await voice_meet.start_work(ROOM, _work("12"))
    assert out == {"started": False, "reason": "no_inviter"}
    m["sw"].assert_not_awaited()


async def test_host_member_stands_in_without_an_inviter(m):
    m["meeting"].pop("invitedBy")
    assert (await voice_meet.start_work(ROOM, _work("12")))["started"] is True


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
    assert (
        call.args[0] == "bot_1"
        and 'says "Nova, ..."' in call.args[1]
        and "waits for the inviting member" in call.args[1]
    )


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


async def test_guest_read_allowed(m):
    page = {"results": [{"content": "Prices go up in May"}]}
    with (
        patch.object(voice_meet.memory_api, "recall_page", AsyncMock(return_value=page)),
        patch.object(voice_meet.integration_reads, "read", AsyncMock(return_value="Invoice 42")),
        patch.object(voice_meet, "_bookings", AsyncMock(return_value=[])),
    ):
        for kind in ("memory", "calendar"):
            out = await voice_meet.read(
                ROOM, voice_meet.ReadIn(ownerId="o", roomToken="t", kind=kind, participantId="12")
            )
            assert "reason" not in out, kind
        body = voice_meet.IntegrationIn(ownerId="o", roomToken="t", provider="gmail", query="q", participantId="12")
        assert (await voice_meet.integration_read(ROOM, body))["text"] == "Invoice 42"
    out = await voice_meet.chat(
        ROOM, voice_meet.ChatIn(ownerId="o", roomToken="t", text="Ana at 10", participantId="12")
    )
    assert out["sent"] == "chat"


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


async def test_meet_approve_counts_as_the_inviters_yes(m):
    body = voice_meet.ApproveIn(ownerId="org_1", roomToken="t", phrase="yes", hint="supplies", participantId="12")
    with patch.object(voice_meet.voice_approve, "approve", AsyncMock(return_value={"confirm_sent": True})) as ap:
        assert await voice_meet.approve(ROOM, body) == {"confirm_sent": True}
    assert ap.await_args.args == ("org_1", "meet", {"user_id": "user_maya", "name": "Sam", "where": "Monday"}, "yes",
                                  "supplies")  # fmt: skip
    assert ap.await_args.kwargs == {"room_id": ROOM}


# --- Task 21: a meeting's task answers back in the Meet ------------------------------------------------------


def _meet_task(status="idle"):
    return {"taskId": "k1", "title": "Draft the holiday-hours post", "status": status,
            "triggerId": f"voice:{ROOM}:run1", "attemptCount": 1}  # fmt: skip


async def test_meeting_task_done_posts_link_in_chat(m):
    """Through the lifecycle hook every status change runs (activities._announce)."""
    from core.temporal import activities

    m["meeting"].update(status="in_call", followUps=[{"text": "Draft", "status": "working", "taskId": "k1"}])
    with (
        patch.object(activities.tasks_repo, "get_task_tree", AsyncMock(return_value={"task": _meet_task()})),
        patch.object(activities.task_conversation, "ensure_task_room", AsyncMock()),
        patch.object(activities.task_conversation, "post_as_lightbulb", AsyncMock()),
        patch.object(voice_meet, "_tile_phase", AsyncMock()),  # no real 60 s clear task
    ):
        await activities._announce("org_1", "k1", "idle")
    m["say"].assert_awaited_once()
    line = m["say"].await_args.args[1]
    assert m["say"].await_args.args[0] == "bot_1"
    assert line.startswith("Draft ready: Draft the holiday-hours post — ") and line.endswith("panel=task&taskId=k1")
    assert m["meeting"]["followUps"][0]["status"] == "done"
    await voice_meet.task_moved(_meet_task(), "idle")  # a retried Activity: said once
    m["say"].assert_awaited_once()


@pytest.mark.parametrize(("to", "status"), [("waiting", "needs_you"), ("working", "working"), ("idle", "done")])
async def test_followup_status_follows_task(m, to, status):
    other = {"text": "Other", "status": "working", "taskId": "k2"}
    m["meeting"].update(status="ended", followUps=[{"text": "Draft", "status": "x", "taskId": "k1"}, other])
    await voice_meet.task_moved(_meet_task(to), to)
    assert [f["status"] for f in m["meeting"]["followUps"]] == [status, "working"]


async def test_ended_meeting_updates_status_only(m):
    m["meeting"].update(status="ended", followUps=[{"text": "Draft", "status": "working", "taskId": "k1"}])
    await voice_meet.task_moved(_meet_task(), "idle")
    assert m["meeting"]["followUps"][0]["status"] == "done"
    m["say"].assert_not_awaited()


async def test_tile_states(m):
    """Drafting while the follow-up works; page_ready once the link posts, cleared after 60 s."""
    await voice_meet.start_work(ROOM, _work("11"))
    m["phase"].assert_awaited_with(ROOM, "drafting")
    m["meeting"].update(status="in_call", followUps=[{"text": "Draft", "status": "working", "taskId": "k1"}])
    with patch.object(voice_meet.asyncio, "sleep", AsyncMock()):
        await voice_meet.task_moved(_meet_task(), "idle")
        m["phase"].assert_awaited_with(ROOM, "page_ready")
        await voice_meet._clears[ROOM]
    m["phase"].assert_awaited_with(ROOM, "")


async def test_single_announcement(m):
    """The Recall join chat is gone (test_meeting_bots), so the chat gets only voice_meet's line, with no opt-out
    that does not exist."""
    from core.services import meeting_bots

    assert not hasattr(meeting_bots, "CONSENT_LINE")
    assert "transcribe" not in voice_meet.ANNOUNCE


def test_announcement_says_recorded_and_fits_the_chat():
    longest = voice_meet.ANNOUNCE.format(name="N" * 30, company="C" * 50)
    assert "This meeting is recorded and notes are saved to " in longest and len(longest) <= voice_meet.CHAT_CHARS


async def test_start_gives_the_inviters_state_with_nothing_private(m):
    with (
        patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")),
        patch.object(voice_meet.integration_reads, "warm_google", AsyncMock()),
    ):
        out = await voice_meet.start(ROOM, voice_meet.StartIn(ownerId="org_1", roomToken="t"))
        await asyncio.sleep(0)
    assert out["context"] == "ctx"
    ws, viewer, room, keep, memory_user, chat_room = m["ctx"].await_args.args
    assert (ws, viewer, room, memory_user, chat_room) == ("org_1", "user_maya", ROOM, None, ROOM)  # workspace memory
    assert keep({"audience": {"roomId": ROOM}})
    assert keep({"ownerUserId": "user_maya", "audience": {"kind": "workspace"}})
    assert not keep({"ownerUserId": "user_maya", "audience": {"kind": "person", "roomId": "dm"}})  # never a DM's
