"""Meeting mode: a Google Meet bridged in by a Recall bot, whose one `meet:<roomId>` participant
carries the mixed meeting audio. Speaker names arrive as `meet.speaker` packets from the backend."""

import json

from huddle_agent import huddle as h_mod
from huddle_agent.transcript import is_question
from test_huddle import make, settle

MEET = "meet:room-1"


async def meeting():
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants({MEET: "Google Meet"})
    return h, room, backend, lives, clock, log


def speaker(h, pid, name, speaking, at):
    h.on_data(
        None,
        "meet.speaker",
        json.dumps(
            {"participantId": pid, "name": name, "speaking": speaking, "at": at}
        ).encode(),
    )


async def hear(h, text):
    await h.on_final_transcript(MEET, "Google Meet", text)
    await settle(h)


def test_is_question():
    assert is_question("ship it?") and is_question("What did we ship")
    assert not is_question("we shipped huddles") and not is_question("Doesn't matter")


async def test_the_bot_joining_posts_no_presence():
    h, room, backend, lives, clock, log = await meeting()
    assert backend.presences == []


async def test_unclaimed_question_waits_then_routes_once():
    h, room, backend, lives, clock, log = await meeting()
    backend.jev_to = "forge"
    await hear(h, "what did we ship last week?")
    clock.t += h_mod.UNCLAIMED_QUESTION_S - 0.5
    await h.tick()
    await settle(h)
    assert backend.routes == []  # a person may still answer it
    clock.t += 0.5
    await h.tick()
    await settle(h)
    await h.tick()
    await settle(h)
    assert len(backend.routes) == 1 and backend.route_bodies[0]["unclaimed"] is True
    assert backend.route_bodies[0]["setting"] == "meeting"
    assert backend.sessions == ["forge"]  # one route, one summon

    # a second human line within the window cancels it
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "how is the launch going?")
    clock.t += 2
    await hear(h, "pretty well, we're on track")
    clock.t += 5
    await h.tick()
    await settle(h)
    assert backend.routes == []


async def test_a_line_that_is_not_a_question_wakes_nobody():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "we shipped the new onboarding flow")
    clock.t += 10
    await h.tick()
    await settle(h)
    assert backend.routes == [] and backend.sessions == []


async def test_a_named_agent_still_answers_at_once():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "hey Forge what did we ship")
    assert backend.routes == [] and backend.sessions == ["forge"]
    assert lives[0].speaker == MEET


async def test_meeting_lines_carry_meet_speaker_names():
    h, room, backend, lives, clock, log = await meeting()
    t = clock.t
    speaker(h, "p1", "Priya", True, t - 3)
    speaker(h, "p1", "Priya", False, t - 0.5)
    await hear(h, "we shipped the importer")
    speaker(h, "p1", "Priya", True, t)
    speaker(h, "p2", "Dan", True, t)
    clock.t += 3
    h.on_interim_transcript(
        MEET, "Google Meet", "both"
    )  # this utterance begins at t + 3
    await hear(h, "both of us talking now")
    speaker(h, "p1", "Priya", False, clock.t)
    speaker(h, "p2", "Dan", False, clock.t)
    clock.t += 60
    await hear(h, "is anyone there?")
    names = [x.name for x in h._transcript.after(-1)]
    assert names == ["Priya (Meet)", "Priya + Dan (Meet)", "Someone (Meet)"]
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert backend.routes[0][0] == "Someone"  # /route's speaker is the label


async def test_meeting_idle_leaves_after_60_minutes_via_the_backend():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "first")
    await hear(h, "second")
    clock.t += h_mod.TRANSCRIPT_FLUSH_S
    await h.tick()
    await settle(h)
    assert backend.transcripts == []  # never mid-call
    clock.t += h_mod.MEETING_IDLE_SECONDS - h_mod.TRANSCRIPT_FLUSH_S - 1
    await h.tick()
    assert "leave_meeting" not in log
    clock.t += 1
    await h.tick()
    assert log[-1] == "leave_meeting" and backend.finals == [True]
    assert [x["text"] for x in backend.transcripts[0][1]] == ["first", "second"]
    await h.tick()
    assert room.shut
    await h.clear_presence()  # the shutdown flush: nothing new, no second post
    assert len(backend.transcripts) == 1
