"""The meeting agent's backend: an addressed line is the inviter's message naming the speaker, the agent lines the
voice says, the announcement, the meeting record and its follow-ups."""

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
        patch.object(voice_meet.livekit_huddles, "set_meet_phase", AsyncMock()) as phase,
        patch.object(voice_meet, "latest_seq", AsyncMock(return_value=41)),
    ):
        patched.update(phase=phase, meeting=meeting, say=say)
        yield patched
    voice_meet._clears.clear()


async def test_announce_once(m):
    with (
        patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")),
    ):
        body = voice_meet.StartIn(ownerId="org_1", roomToken="t", company="Maya's Studio")
        assert (await voice_meet.start(ROOM, body))["assistantName"] == "Nova"
        await voice_meet.start(ROOM, body)  # a restarted worker does not say it again
        await asyncio.sleep(0)
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
    ):
        out = await voice_meet.start(ROOM, voice_meet.StartIn(ownerId="org_1", roomToken="t"))
        await asyncio.sleep(0)
    assert out["assistantName"] == "Nova"
    assert "announce failed 400" in caplog.text


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


async def test_assistant_name_from_the_agent_page(m):
    with patch.object(voice_meet.messaging_repo, "get_voice_agent", AsyncMock(side_effect=[{"name": " Nova "}, None])):
        assert await voice_meet.assistant_name("org_1") == "Nova"
        assert await voice_meet.assistant_name("org_1") == "Lightbulb"


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


async def test_single_announcement(m):
    """The Recall join chat is gone (test_meeting_bots), so the chat gets only voice_meet's line, with no opt-out
    that does not exist."""
    from core.services import meeting_bots

    assert not hasattr(meeting_bots, "CONSENT_LINE")
    assert "transcribe" not in voice_meet.ANNOUNCE


def test_announcement_says_recorded_and_fits_the_chat():
    longest = voice_meet.ANNOUNCE.format(name="N" * 30, company="C" * 50)
    assert "This meeting is recorded and notes are saved to " in longest and len(longest) <= 500  # Meet's chat limit


async def test_an_addressed_line_is_the_inviters_message_naming_who_said_it(m):
    from types import SimpleNamespace

    with patch.object(
        voice_meet.messaging, "create_human_message", AsyncMock(return_value=SimpleNamespace(message_id="m1"))
    ) as ch:
        body = voice_meet.AskIn(ownerId="org_1", roomToken="t", text="Nova, what shipped?", participantId="12")
        assert await voice_meet.ask(ROOM, body) == {"posted": True, "messageId": "m1"}
        await voice_meet.ask(ROOM, body)  # a retry
    first, again = ch.await_args_list
    auth, room, req = first.args
    assert (auth.user_id, auth.org_id, room) == ("user_maya", "org_1", ROOM)  # the inviter's authority
    assert req.text == "Sam said in the meeting: Nova, what shipped?"
    assert first.kwargs == {"via": "voice"}  # wakes the meeting room's Router (wake defaults on)
    assert again.args[2].client_message_id == req.client_message_id


async def test_no_inviter_posts_nothing(m):
    m["meeting"].pop("invitedBy")
    m["meeting"]["roster"] = {}
    with patch.object(voice_meet.messaging, "create_human_message", AsyncMock()) as ch:
        out = await voice_meet.ask(ROOM, voice_meet.AskIn(ownerId="org_1", roomToken="t", text="Nova, hi there"))
    assert out == {"posted": False, "reason": "no_inviter"}
    ch.assert_not_awaited()


async def test_the_voice_says_only_the_rooms_new_agent_lines():
    rows = [
        {"messageId": "h1", "messageSeq": 5, "author": {"kind": "human"}, "text": "What shipped?"},
        {"messageId": "a1", "messageSeq": 6, "author": {"kind": "agent"}, "text": "Checking GitHub."},
        {"messageId": "a2", "messageSeq": 7, "author": {"kind": "agent"}, "text": "...", "progress": True},
        {"messageId": "a3", "messageSeq": 8, "author": {"kind": "agent"}, "text": "x", "threadRootMessageId": "t"},
        {
            "messageId": "a4",
            "messageSeq": 9,
            "author": {"kind": "agent"},
            "text": "old",
            "editedText": "We shipped #5095.",
        },
        {"messageId": "s1", "messageSeq": 10, "author": {"kind": "system"}, "text": "Notes posted."},
    ]
    with patch.object(voice_meet.messaging_repo, "list_messages", AsyncMock(return_value=(rows, None))) as lm:
        out = await voice_meet.agent_lines(ROOM, 4)
    lm.assert_awaited_once_with(ROOM, 4, 100)
    assert out == {"messages": [{"id": "a1", "seq": 6, "text": "Checking GitHub."}, {"id": "a4", "seq": 9, "text": "We shipped #5095."}],
                   "after": 10}  # fmt: skip
    with patch.object(voice_meet.messaging_repo, "list_messages", AsyncMock(return_value=([], None))):
        assert await voice_meet.agent_lines(ROOM, 10) == {"messages": [], "after": 10}


async def test_start_gives_the_first_cursor(m):
    with patch.object(voice_meet, "assistant_name", AsyncMock(return_value="Nova")):
        out = await voice_meet.start(ROOM, voice_meet.StartIn(ownerId="org_1", roomToken="t"))
    assert out == {"assistantName": "Nova", "title": "Monday", "after": 41}


def test_the_meeting_has_no_tool_routes():
    paths = {r.path.rsplit("/", 1)[-1] for r in voice_meet.router.routes}
    assert paths == {"start", "ask", "events", "end"}


QROW = {"messageId": "q1", "messageSeq": 8, "author": {"kind": "agent"}, "kind": "question",
        "text": "**Pulse** asks: What holiday hours should the post announce?", "threadRootMessageId": "t",
        "taskRefs": [{"taskId": "k1", "role": "started", "qid": "q-17b4"}]}  # fmt: skip


async def test_a_worker_question_in_a_thread_is_said_with_its_ref():
    """8 Nov prod Meet: Pulse's question (msg 8) went to the meeting room's thread and was never said."""
    rows = [QROW, {"messageId": "a3", "messageSeq": 9, "author": {"kind": "agent"}, "text": "x",
                   "threadRootMessageId": "t"}]  # fmt: skip
    with _open_questions("q-17b4"), patch.object(voice_meet.messaging_repo, "list_messages",
                                                 AsyncMock(return_value=(rows, None))):  # fmt: skip
        out = await voice_meet.agent_lines(ROOM, 4)
    assert out["messages"] == [{"id": "q1", "seq": 8, "text": QROW["text"],
                                "question": {"taskId": "k1", "qid": "q-17b4"}}]  # fmt: skip


def _open_questions(*qids):
    from contextlib import ExitStack

    from core.repositories import questions_repo

    async def get(ws, task_id, qid):
        assert ws == "org_1"
        return {"qid": qid, "state": "open" if qid in qids else "answered"}

    stack = ExitStack()
    stack.enter_context(
        patch.object(voice_meet.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_1"}))
    )
    stack.enter_context(patch.object(questions_repo, "get_question", get))
    return stack


async def test_an_answered_or_closed_question_is_never_said():
    """8 Nov prod Meet re-run #3: Pulse's answered holiday-hours question kept being said, over Vera's question."""
    vera = {**QROW, "messageId": "q2", "messageSeq": 9, "taskRefs": [{"taskId": "k2", "role": "started", "qid": "q-2"}]}
    with _open_questions("q-2"), patch.object(voice_meet.messaging_repo, "list_messages",
                                              AsyncMock(return_value=([QROW, vera], None))):  # fmt: skip
        out = await voice_meet.agent_lines(ROOM, 4)
    assert [m["id"] for m in out["messages"]] == ["q2"] and out["after"] == 9


def _ask(**over):
    return voice_meet.AskIn(ownerId="org_1", roomToken="t", text="We close at 2 on Christmas Eve.", participantId="12",
                            answers={"taskId": "k1", "qid": "q-17b4"}, **over)  # fmt: skip


async def test_the_line_after_a_said_question_answers_it_like_a_typed_answer(m):
    """The spoken hours left LB-10 waiting (a message never answers a Question): now they answer it through the
    card's own path, as the inviting member, and no message wakes the Router to claim progress."""
    from routers import questions as question_routes

    from types import SimpleNamespace

    with (
        patch.object(voice_meet.messaging_repo, "list_messages_before", AsyncMock(return_value=[QROW])),
        patch.object(question_routes, "submit_answer", AsyncMock()) as sa,
        patch.object(
            voice_meet.messaging, "create_human_message", AsyncMock(return_value=SimpleNamespace(message_id="m5"))
        ) as ch,
    ):
        assert await voice_meet.ask(ROOM, _ask()) == {"posted": True, "messageId": "m5", "answered": "q-17b4"}
        await voice_meet.ask(ROOM, _ask())  # a retry: the same message
    # the chat keeps the spoken line, but it wakes nobody: the Router never gets the answer as a second input
    first, again = ch.await_args_list
    assert first.kwargs == {"via": "voice", "wake": False}
    assert first.args[2].text == "Sam said in the meeting: We close at 2 on Christmas Eve."
    assert again.args[2].client_message_id == first.args[2].client_message_id
    ws, task_id, qid, me, body = sa.await_args.args
    assert (ws, task_id, qid, me, body.option_id) == ("org_1", "k1", "q-17b4", "user_maya", "answer")
    assert body.text == "Sam said in the meeting: We close at 2 on Christmas Eve."


async def test_a_refused_or_foreign_question_falls_back_to_a_message(m):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from routers import questions as question_routes

    posted = AsyncMock(return_value=SimpleNamespace(message_id="m9"))
    refused = AsyncMock(side_effect=HTTPException(status_code=403, detail={"code": "not_answerer"}))
    with (
        patch.object(voice_meet.messaging_repo, "list_messages_before", AsyncMock(return_value=[QROW])),
        patch.object(question_routes, "submit_answer", refused),
        patch.object(voice_meet.messaging, "create_human_message", posted),
    ):
        assert (await voice_meet.ask(ROOM, _ask()))["posted"] is True
    other = {**QROW, "taskRefs": [{"taskId": "k2", "qid": "q-other"}]}
    with (
        patch.object(voice_meet.messaging_repo, "list_messages_before", AsyncMock(return_value=[other])),
        patch.object(question_routes, "submit_answer", AsyncMock()) as sa,
        patch.object(voice_meet.messaging, "create_human_message", posted),
    ):
        assert (await voice_meet.ask(ROOM, _ask()))["posted"] is True  # not a question said in this room
    sa.assert_not_awaited()


def _made_task():
    """A Router thread started from a meeting message (a `chat:` trigger, no follow-up), that made a document."""
    return {"taskId": "k9", "title": "Draft the holiday-hours post", "status": "idle", "triggerId": "chat:m7",
            "attemptCount": 1, "audience": {"kind": "room", "roomId": ROOM},
            "outputs": [{"id": "file:a1", "kind": "document", "title": "holiday-hours-post.md"}]}  # fmt: skip


async def _announce_made(m, task):
    from core.temporal import activities

    tree = {"task": task, "attempts": [{"n": 1, "outcome": "completed", "output": "Drafted the holiday-hours post."}],
            "subscriptions": [{"createdBy": "router", "threadKey": f"room:{ROOM}:root1"}]}  # fmt: skip
    post = AsyncMock()
    with (
        patch.object(activities.tasks_repo, "get_task_tree", AsyncMock(return_value=tree)),
        patch.object(activities.task_conversation, "ensure_task_room", AsyncMock()),
        patch.object(activities.task_conversation, "post_as_lightbulb", post),
        patch.object(voice_meet.messaging_repo, "get_room", AsyncMock(return_value={"meeting": m["meeting"]})),
        patch.object(voice_meet, "_tile_phase", AsyncMock()),
    ):
        await activities._announce("org_1", task["taskId"], "idle")
    return next(c.kwargs["text"] for c in post.await_args_list if c.kwargs.get("kind") == "task_line")


async def test_a_meeting_thread_that_made_something_posts_its_link_in_the_room_and_the_meet_chat(m):
    """8 Nov prod Meet re-run: "put the link in chat" -- LB-19 made holiday-hours-post.md, but the room line had only
    a task card and nothing reached the Meet's chat (`task_moved` says a link only for follow-ups, never written)."""
    m["meeting"].update(status="in_call")
    line = await _announce_made(m, _made_task())
    assert line.startswith("Drafted the holiday-hours post.") and line.endswith("/chat?panel=task&taskId=k9")
    m["say"].assert_awaited_once_with("bot_1", line)
    await _announce_made(m, _made_task())  # a retried Activity: the Meet chat gets it once
    m["say"].assert_awaited_once()


async def test_after_the_call_the_room_line_still_has_the_link_and_an_answer_gets_none(m):
    m["meeting"].update(status="ended")
    line = await _announce_made(m, _made_task())
    assert line.endswith("taskId=k9") and m["say"].await_count == 0
    m["meeting"].update(status="in_call")
    line = await _announce_made(m, {**_made_task(), "outputs": []})  # an answer: nothing made, no link
    assert "http" not in line and m["say"].await_count == 0


async def test_a_thread_outside_a_meeting_keeps_its_line():
    with patch.object(voice_meet.messaging_repo, "get_room", AsyncMock(return_value={"kind": "dm"})):
        assert await voice_meet.with_link("dm-1", _made_task(), "Done.") == "Done."


async def test_a_caller_threads_lines_are_in_the_call_rooms_main_chat():
    """8 Nov prod booking smoke after #5122 (call f50ca3da, rows as read from lightbulb-prod-messaging): the Front desk
    thread's read-back and "you're booked" are main-chat `task_line` rows in the call's room, so `/events` hands both
    to the voice in order (the caller never heard them: the talker, not the backend, dropped them)."""
    task = [{"taskId": "fab82a45-7268-5fb3-92ac-cb5a1aa0b0df"}]
    lb = {"kind": "agent", "id": "lightbulb", "displayName": "Lightbulb"}
    rows = [
        {"messageId": "m2", "messageSeq": 2, "author": lb, "taskRefs": task,
         "text": "Customer Operations is checking Saturday afternoon availability for your founding team setup."},
        {"messageId": "m3", "messageSeq": 3, "author": {"kind": "human", "displayName": "Caller Taylor Test"},
         "text": "The first time you said works for me."},
        {"messageId": "m4", "messageSeq": 4, "author": lb, "kind": "task_line", "taskRefs": task,
         "text": "Just to confirm, Saturday, November 14 at 12:00 PM for the 45-minute founding team setup—is that right?"},
        {"messageId": "m6", "messageSeq": 6, "author": lb, "kind": "task_line", "taskRefs": task,
         "text": "You’re booked for the founding team setup on Saturday, November 14 at 12:00 PM."},
    ]  # fmt: skip
    with patch.object(voice_meet.messaging_repo, "list_messages", AsyncMock(return_value=(rows, None))):
        out = await voice_meet.agent_lines(ROOM, 1)
    assert [m["id"] for m in out["messages"]] == ["m2", "m4", "m6"] and out["after"] == 6
