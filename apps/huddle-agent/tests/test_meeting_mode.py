"""Meeting mode: a Google Meet bridged in by a Recall bot, whose one `meet:<roomId>` participant
carries the mixed meeting audio. Speaker names arrive as `meet.speaker` packets from the backend."""

import json

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from huddle_agent.transcript import is_question
from test_huddle import answer, make, ops, say, settle, talks

MEET = "meet:room-1"


async def meeting():
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants({MEET: "Google Meet"})
    return h, room, backend, lives, clock, log


def speaker(h, pid, name, speaking, at=0, **extra):  # Recall's `at` is logged, never used
    h.on_data(
        None,
        "meet.speaker",
        json.dumps(
            {"participantId": pid, "name": name, "speaking": speaking, "at": at, **extra}
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
    assert len(backend.routes) == 1 and backend.sessions == ["forge"]  # understood beside its voice (§4.6)
    assert lives[0].speaker == MEET


async def test_meeting_lines_carry_meet_speaker_names():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 2.5
    speaker(h, "p1", "Priya", False)
    await hear(h, "we shipped the importer")
    speaker(h, "p1", "Priya", True)
    speaker(h, "p2", "Dan", True)
    clock.t += 3
    h.on_interim_transcript(MEET, "Google Meet", "both")  # the utterance begins now
    await hear(h, "both of us talking now")
    speaker(h, "p1", "Priya", False)
    speaker(h, "p2", "Dan", False)
    clock.t += 60
    await hear(h, "is anyone there?")
    names = [x.name for x in h._transcript.after(-1)]
    assert names == ["Priya (Meet)", "Priya + Dan (Meet)", "Someone (Meet)"]
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert backend.routes[0][0] == "Someone"  # /route's speaker is the label


def labels(h):
    return [x.name.removesuffix(" (Meet)") for x in h._transcript.after(-1)]


async def test_an_on_without_an_off_keeps_labelling_until_someone_else_starts():
    # Recall on Google Meet sends an `on` only when the active speaker changes: no off for a lone speaker
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)  # an old packet: no kind, no roster
    clock.t += 60
    await hear(h, "we shipped the importer")
    speaker(h, "p2", "Dan", True)
    clock.t += 5
    await hear(h, "and the exporter")
    assert labels(h) == ["Priya", "Dan"]  # Dan's on closed Priya's span


async def test_a_leave_closes_the_span_and_a_join_opens_none():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True, kind="speech_on")
    clock.t += 5
    speaker(h, "p1", "Priya", False, kind="leave")
    speaker(h, "p2", "Dan", False, kind="join")
    clock.t += 5
    await hear(h, "is anyone there?")
    assert labels(h) == ["Someone"]


async def test_a_roster_of_one_labels_an_uncovered_line():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", False, kind="join", roster=["Priya"])
    await hear(h, "we shipped the importer")
    speaker(h, "p2", "Dan", False, kind="join", roster=["Priya", "Dan"])
    await hear(h, "and the exporter")
    assert labels(h) == ["Priya", "Someone"]


async def test_a_stale_utterance_start_is_ignored():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    h.on_interim_transcript(MEET, "Google Meet", "we")
    clock.t += 1
    speaker(h, "p1", "Priya", False)
    clock.t += 40  # that stream never finalised: its start is not this line's
    await hear(h, "we shipped the importer")
    assert h._transcript.after(-1)[0].name == "Someone (Meet)"
    h.on_interim_transcript(MEET, "Google Meet", "and")
    h.stt_stopped(MEET)  # the STT stream restarted
    assert MEET not in h._utter_from


async def test_the_final_transcript_is_one_whole_request():
    h, room, backend, lives, clock, log = await meeting()
    for i in range(1200):
        h._transcript.add_person(MEET, "Priya (Meet)", f"line {i}", at=clock.t)
    await h._flush_transcript()
    assert [len(x[1]) for x in backend.transcripts] == [1200]
    assert backend.finals == [True]


async def test_a_live_flush_still_goes_in_chunks_of_500():
    h, room, backend, lives, clock, log = make()
    for i in range(1200):
        h._transcript.add_person("u1", "Priya", f"line {i}", at=clock.t)
    await h._flush_transcript()
    assert [len(x[1]) for x in backend.transcripts] == [500, 500, 200]
    assert backend.finals == [False, False, False]


async def test_the_final_transcript_keeps_the_newest_5000(caplog):
    h, room, backend, lives, clock, log = await meeting()
    for i in range(5003):
        h._transcript.add_person(MEET, "Priya (Meet)", f"line {i}", at=clock.t)
    await h._flush_transcript()
    ((_, lines),) = backend.transcripts
    assert (
        len(lines) == 5000 and lines[0]["text"] == "line 3" and backend.finals == [True]
    )
    assert "keeping the newest 5000" in caplog.text


async def test_two_questions_route_once_and_a_summon_disarms_the_timer():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "what did we ship?")
    clock.t += 2
    await hear(h, "and who shipped it?")
    for _ in range(3):
        clock.t += 2
        await h.tick()
        await settle(h)
    assert [r[1] for r in backend.routes] == ["and who shipped it?"]

    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "what did we ship?")
    await h.talk_start(MEET, '{"agentId": "forge"}')
    clock.t += 5
    await h.tick()
    await settle(h)
    assert backend.routes == [] and backend.sessions == ["forge"]


async def test_a_failed_unclaimed_route_tells_nobody():
    h, room, backend, lives, clock, log = await meeting()

    async def refused(*a, **kw):
        raise BackendError(503, "runtime_unavailable", None)

    backend.route = refused
    await hear(h, "what did we ship?")
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert not [x for x in room.published if x[0] == "huddle.error"]


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


async def test_the_final_transcript_post_is_retried_until_acknowledged(monkeypatch):
    h, room, backend, lives, clock, log = await meeting()
    monkeypatch.setattr(h_mod, "FINAL_TRANSCRIPT_RETRY_S", (0, 0, 0, 0, 0))
    await hear(h, "first")
    post, fails = backend.transcript, [BackendError(503, "http_503", None)] * 2

    async def flaky(*a, **kw):
        if fails:
            raise fails.pop()
        await post(*a, **kw)

    backend.transcript = flaky
    await h._flush_transcript()
    assert backend.finals == [True] and not fails  # failed twice, then acknowledged
    await h._flush_transcript()
    assert len(backend.transcripts) == 1  # acknowledged: never sent again


async def test_a_final_transcript_never_acknowledged_stays_unsent(monkeypatch):
    h, room, backend, lives, clock, log = await meeting()
    monkeypatch.setattr(h_mod, "FINAL_TRANSCRIPT_RETRY_S", (0, 0))
    await hear(h, "first")
    calls = []

    async def down(*a, **kw):
        calls.append(1)
        raise BackendError(503, "http_503", None)

    backend.transcript = down
    await h._flush_transcript()
    assert len(calls) == 3 and h._transcript.after(
        h._sent_mark
    )  # bounded; the lines stay for a later flush


async def test_summon_and_turn_carry_the_meet_speaker_label_beside_the_raw_identity():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 2.5
    speaker(h, "p1", "Priya", False)
    await hear(h, "Forge, check the board")
    assert (
        lives[0].speaker == MEET
    )  # LiveKit audio stays pinned to the bot's participant
    assert (backend.summons, backend.summon_names) == ([MEET], ["Priya"])
    turn = backend.turns[0]
    assert (turn["speaker"], turn["speakerName"]) == (MEET, "Priya")


async def test_the_backend_client_sends_speaker_name(monkeypatch):
    import httpx

    from huddle_agent.backend import Backend

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setenv("LIGHTBULB_API_URL", "https://api.test")
    b = Backend("room-1", "org_a", "rt", client=client)
    await b.summon(MEET, "Priya")
    await b.turn("c1", MEET, "forge", "run-1", "check CI", "", speaker_name="Priya")
    await b.turn("c1", "u1", "forge", "run-2", "check CI", "")
    assert [x.get("speakerName") for x in sent] == ["Priya", "Priya", None]
    assert [x["speakerUserId"] for x in sent] == [MEET, MEET, "u1"]


# -- who a meeting voice answers: a line that names it, an unclaimed question, or its speaker's follow-up --


async def priya_asks_forge(rows=False):
    """Priya names Forge; Forge says its piece and stops. `rows`: /route lists the ask as a request row."""
    h, room, backend, lives, clock, log = await meeting()
    if rows:
        backend.route_extra = ops([("forge", "list the open PRs", "name")])
    speaker(h, "p1", "Priya", True)
    await hear(h, "Forge, what PRs are open?")
    backend.route_extra = {}
    h.on_agent_state("forge", "speaking")
    clock.t += 2
    h.on_agent_state("forge", "listening")
    return h, room, backend, lives, clock, log


async def dan_starts(h, clock):
    clock.t += 2
    speaker(h, "p2", "Dan", True)
    clock.t += 3


async def test_a_meeting_voice_is_muted_at_open_before_its_first_note():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "hey Forge what did we ship")
    assert lives[0].calls[0] == ("mute",)  # it hears the meeting only through the worker's transcript

    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "hey Forge what did we ship")
    assert ("mute",) not in lives[0].calls


async def test_a_meeting_voice_ignores_talk_from_someone_else():
    h, room, backend, lives, clock, log = await priya_asks_forge()
    routes, turns, calls = len(backend.routes), len(backend.turns), len(lives[0].calls)
    await dan_starts(h, clock)
    await hear(h, "let's move on")
    await hear(h, "should we ship Friday?")
    assert len(backend.routes) == routes and len(backend.turns) == turns
    assert lives[0].calls[calls:] == []
    assert h._unclaimed is not None and h._unclaimed[2] == "should we ship Friday?"


async def test_a_meeting_voice_takes_its_speakers_follow_up_for_10_seconds():
    h, room, backend, lives, clock, log = await priya_asks_forge()
    clock.t += 9
    await hear(h, "and the closed ones")
    assert backend.route_bodies[-1]["floor"] == {"agent": "forge", "speaker": "Priya", "mine": True}
    # /route gave no `ops`: the whole line is the request, and the muted voice is told to answer it
    assert backend.turns[-1]["utterance"] == "and the closed ones" and lives[0].calls[-1] == ("nudge",)

    # begun at 9 s, final at 12 s: the voice waits while the meeting talks, and the line is a follow-up
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    clock.t += 9
    talks(h, MEET)
    h.on_interim_transcript(MEET, "Google Meet", "and")
    clock.t += 1.5
    await h.tick()
    await settle(h)
    assert h._voice is not None
    clock.t += 1.5
    await hear(h, "and the merged ones")
    assert backend.route_bodies[-1]["floor"]["mine"] is True

    # 11 s later: the voice has closed, and the line is nobody's
    h.on_speech(MEET, False)
    routes = len(backend.routes)
    clock.t += 10
    await h.tick()
    await settle(h)
    assert h._voice is None
    clock.t += 1
    await hear(h, "and the draft ones")
    assert len(backend.routes) == routes


async def test_an_unlabelled_speaker_never_gets_a_follow_up():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "Forge, what PRs are open?")  # no speaker packets: "Someone"
    clock.t += 2
    await hear(h, "and the closed ones")
    assert len(backend.routes) == 1  # only the named line's own


async def test_a_meeting_voice_closes_after_its_piece_and_its_answer_is_said_later():
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    assert row.status == "working" and row.label == "Priya"
    clock.t += h_mod.MEET_FOLLOWUP_S - 1
    await h.tick()
    await settle(h)
    assert h._voice is not None
    clock.t += 1
    await h.tick()
    await settle(h)
    assert h._voice is None and row.status == "working"  # a working run keeps no meeting voice open
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert row.status == "ready"
    clock.t += 1
    await h.tick()
    await settle(h)
    v = h._voice
    assert v is not None and v.agent_id == "forge" and v.path == "announce" and v.label == "Priya"
    assert "Three PRs are open." in str(lives[1].seed)
    h.on_agent_state("forge", "speaking")
    assert row.status == "delivered" and backend.late_answers == []


async def test_only_a_named_or_spoken_over_stop_stops_a_meeting_agent():
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    await dan_starts(h, clock)
    await hear(h, "never mind")  # between two people: not to Forge
    assert row.status == "working" and backend.aborts == []
    await hear(h, "Forge, never mind")
    assert row.status == "cancelled" and backend.aborts == [row.run_id]

    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    await dan_starts(h, clock)
    h.on_agent_state("forge", "speaking")
    await hear(h, "stop")  # said over a talking agent: it stops
    assert row.status == "cancelled" and ("interrupt",) in lives[0].calls


async def test_an_unclaimed_question_is_claimed_by_talk_and_fires_once():
    h, room, backend, lives, clock, log = await meeting()
    await hear(h, "what did we ship last week?")
    talks(h, MEET)  # someone answers it
    assert h._unclaimed is None

    h, room, backend, lives, clock, log = await priya_asks_forge()
    backend.jev_to = "gauge"
    await dan_starts(h, clock)
    await hear(h, "should we ship Friday?")
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert h._unclaimed is None  # a voice was open at its deadline: dropped, not kept
    for _ in range(3):
        clock.t += h_mod.MEET_FOLLOWUP_S
        await h.tick()
        await settle(h)
    assert not [b for b in backend.route_bodies if b["unclaimed"]]


async def test_a_line_naming_another_agent_is_not_the_floor_speakers():
    h, room, backend, lives, clock, log = await priya_asks_forge()
    await dan_starts(h, clock)
    await hear(h, "Gauge, check the board")
    floors = [b["floor"] for b in backend.route_bodies if b["floor"]]
    assert floors[-1]["agent"] == "forge" and floors[-1]["mine"] is False
    assert backend.sessions[-1] == "gauge"


async def test_a_bare_name_then_the_request_is_understood_as_that_agents():
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    await hear(h, "Forge?")
    clock.t += 2
    backend.route_extra = ops([("forge", "list the open PRs", "floor")])
    await hear(h, "what PRs are open")
    assert backend.route_bodies[-1]["floor"]["mine"] is True
    assert backend.turns[-1]["agentId"] == "forge"
    assert lives[0].calls[-1] == ("nudge",)  # the muted voice is told to acknowledge it


async def test_a_live_floor_line_is_still_its_speakers_and_the_voice_stays_open():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "hey Forge what did we ship")
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    clock.t += 15
    await say(h, "u1", "and the closed ones")
    assert backend.route_bodies[-1]["floor"]["mine"] is True
    clock.t += 11
    await h.tick()
    await settle(h)
    assert h._voice is not None and ("mute",) not in lives[0].calls


async def test_lightbulb_from_someone_else_closes_the_voice_and_routes_as_with_none():
    h, room, backend, lives, clock, log = await priya_asks_forge()
    backend.route_to = "gauge"
    await dan_starts(h, clock)
    await hear(h, "Lightbulb, what's on the board?")
    assert backend.route_bodies[-1]["floor"] is None  # routed with no voice open
    assert lives[0].calls[-1] == ("close",) and h._voice.agent_id == "gauge" and h._voice.label == "Dan"
