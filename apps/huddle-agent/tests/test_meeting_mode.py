"""Meeting mode: a Google Meet bridged in by a Recall bot, whose one `meet:<roomId>` participant
carries the mixed meeting audio. Speaker names arrive as `meet.speaker` packets from the backend."""

import asyncio
import json

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from huddle_agent.transcript import is_question
from test_huddle import answer, frame, make, ops, packet, say, settle, talks

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
    h.on_interim_transcript(MEET, "Google Meet", "both")  # the utterance begins while Priya speaks...
    clock.t += 2
    speaker(h, "p2", "Dan", True)  # ...and Dan takes over before it ends (Meet sends no `off` for Priya)
    clock.t += 1
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


async def test_a_meeting_request_keeps_its_askers_name_when_someone_else_speaks_while_the_voice_opens():
    # review: the label was read after the awaits, when it was already the latest speaker's ("Dan asked: ...")
    h, room, backend, lives, clock, log = await meeting()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    h.gate["live"] = asyncio.Event()
    speaker(h, "p1", "Priya", True)
    await h.on_final_transcript(MEET, "Google Meet", "Forge, what PRs are open?")
    for _ in range(30):
        await asyncio.sleep(0)
    clock.t += 2
    speaker(h, "p2", "Dan", True)
    clock.t += 2
    await h.on_final_transcript(MEET, "Google Meet", "anyway, lunch")  # the latest label is Dan's now
    h.gate["live"].set()
    await settle(h)
    told = str(lives[0].seed) + str(lives[0].calls)
    assert "Priya asked" in told and "Dan" not in told
    assert h._voice.label == "Priya" and [r.label for r in h._requests.items] == ["Priya"]


async def unheard_answer():
    """Forge's voice closed while its run worked; the answer came and nobody has heard it yet."""
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert h._voice is None and row.status == "ready"
    return h, backend


async def test_a_meetings_unheard_answers_are_posted_before_its_transcript():
    # the backend writes the notes and the mail when the transcript lands: the answers must be on the record first
    h, backend = await unheard_answer()
    backend.transcript_gate = asyncio.Event()
    leaving = asyncio.ensure_future(h.clear_presence())
    for _ in range(30):
        await asyncio.sleep(0)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."] and backend.transcripts == []
    backend.transcript_gate.set()
    await leaving
    assert backend.finals == [True]


async def test_a_stuck_unheard_post_does_not_cost_the_meeting_its_transcript(monkeypatch):
    h, backend = await unheard_answer()
    monkeypatch.setattr(h_mod, "UNHEARD_BUDGET_S", 0.01)

    async def stuck(*a):
        await asyncio.Event().wait()

    backend.late_answer = stuck
    await h.clear_presence()
    assert backend.finals == [True]


async def test_a_meetings_ready_answer_waits_for_a_real_pause_and_its_hand_shows_what_it_is_about():
    h, backend = await unheard_answer()
    assert h._state()["lightbulb.agent.forge.status"] == "has_news"
    assert h._state()["lightbulb.agent.forge.hand"] == "list the open PRs"
    await hear(h, "ok so the roadmap")  # the meeting goes on
    h._clock.t += 1  # a breath, not a pause: Live would deliver here
    await h.tick()
    await settle(h)
    assert h._voice is None
    h._clock.t += h_mod.MEET_QUIET_S
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce"
    assert h._state()["lightbulb.agent.forge.hand"] == ""


async def test_a_meeting_answer_dropped_by_talk_is_tried_again_at_each_pause_and_never_given_up():
    h, backend = await unheard_answer()
    for _ in range(h_mod.DELIVERY_TRIES + 2):
        h._clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
        await h.tick()
        await settle(h)
        assert h._voice is not None and h._voice.announce is not None, "its voice opens for it"
        talks(h, MEET, "Google Meet")  # someone talks before its audio: dropped, the answer kept
        await settle(h)
        h.on_speech(MEET, False)
        h.on_active_speakers(set())
    h._clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert backend.late_answers == [], "a busy meeting does not push the answer off the call"
    assert h._voice is not None and h._voice.announce is not None


# -- review: a muted voice must still yield to a person; a follow-up must not land on someone else's voice; the
# idle exit posts the unheard work before the transcript --


def quiet_meet(h):
    h.on_speech(MEET, False)
    h.on_active_speakers(set())


async def test_a_meeting_voice_stops_for_someone_who_keeps_talking_over_it_and_its_answer_goes_on_the_record():
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")  # handed to its open voice, which says it
    h.on_agent_state("forge", "speaking")
    talks(h, MEET, "Google Meet")  # "mm-hm": shorter than TALK_OVER_S
    clock.t += h_mod.TALK_OVER_S / 2
    await h.tick()
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and h._voice is not None, "a backchannel does not stop it"
    quiet_meet(h)
    talks(h, MEET, "Google Meet")  # someone starts a sentence over it and keeps going
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None
    quiet_meet(h)
    await hear(h, "sorry, one thing about the budget")  # the talker's words: nothing about the answer
    clock.t += h_mod.CUT_HANDLED_S
    await h.tick()
    await settle(h)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."], "not said to its end: on the record"


async def test_a_meeting_answer_still_coming_when_its_voice_is_talked_over_is_said_whole_at_the_next_pause():
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    first = {"type": "chunk", "content": "Three PRs are open. ", "append": True, "seq": 1}
    h.on_data(None, "call.frame", packet(frame(row.run_id, "forge", first)))
    h.on_agent_state("forge", "speaking")
    talks(h, MEET, "Google Meet")
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert h._voice is None and row.status == "working"
    rest = {"type": "chunk", "content": "The oldest is two days old.", "append": True, "seq": 2}
    h.on_data(
        None,
        "call.frame",
        packet(frame(row.run_id, "forge", rest), frame(row.run_id, "forge", {"type": "run_ended", "seq": 3})),
    )
    assert row.status == "ready"
    quiet_meet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce"
    assert "Three PRs are open. The oldest is two days old." in str(lives[-1].seed)
    assert backend.late_answers == []


async def test_a_follow_up_understood_after_its_voice_went_to_someone_else_is_not_said_to_them():
    h, room, backend, lives, clock, log = await priya_asks_forge()
    backend.route_gate = asyncio.Event()
    clock.t += 3
    await h.on_final_transcript(MEET, "Google Meet", "and the closed ones")  # Priya's follow-up, at /route
    for _ in range(30):
        await asyncio.sleep(0)
    assert h._voice.label == "Priya"
    await dan_starts(h, clock)
    await h.on_final_transcript(MEET, "Google Meet", "Forge, how many issues are open?")  # Dan names the agent
    for _ in range(30):
        await asyncio.sleep(0)
    assert h._voice.label == "Dan"
    calls, turns = len(lives[0].calls), len(backend.turns)
    backend.route_gate.set()
    await settle(h)
    said = [c for c in lives[0].calls[calls:] if "closed ones" in str(c)]
    assert said == [], "Priya's line is not put to the voice that is now with Dan"
    assert not any(t["utterance"] == "and the closed ones" for t in backend.turns[turns:])


async def test_a_meeting_that_idles_out_posts_its_unheard_answers_before_its_transcript():
    h, backend = await unheard_answer()
    backend.transcript_gate = asyncio.Event()
    leaving = asyncio.ensure_future(h._end_idle_room())
    for _ in range(30):
        await asyncio.sleep(0)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."] and backend.transcripts == []
    backend.transcript_gate.set()
    await leaving
    assert backend.finals == [True]


async def test_a_meeting_voice_is_told_what_to_say_and_gets_no_note_it_could_read_out():
    # 10-04 prod Meet: "Yes, I'm here. What's up?" to a question, then "Meeting is talking to me right now"
    h, room, backend, lives, clock, log = await meeting()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    speaker(h, "p1", "Priya", True)
    await hear(h, "Forge, what PRs are open?")
    notes = [c for c in lives[0].calls if c[0] == "thinking"]
    assert not any("is talking to you" in str(c) for c in notes)
    assert h_mod.MEET_ACK in str(lives[0].seed) and ("nudge",) in lives[0].calls

    h, room, backend, lives, clock, log = make()  # a Live call keeps its note
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "hey Forge what did we ship")
    assert ("thinking", "Prasiddha is talking to you", None) in lives[0].calls
    assert h_mod.MEET_ACK not in str(lives[0].seed)


async def test_a_meeting_voice_gets_no_working_note_for_each_tool_its_run_uses():
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    calls = len(lives[0].calls)
    tool = {"type": "tool_start", "tool": "github", "seq": 1}
    h.on_data(None, "call.frame", packet(frame(row.run_id, "forge", tool)))
    assert lives[0].calls[calls:] == []
    assert h._state()["lightbulb.agent.forge.working"]  # the tile still shows it


async def test_a_meeting_line_is_routed_and_run_under_its_own_speaker_not_the_latest():
    # review: /route and /turns read the latest label, so Priya's follow-up went out as Dan's
    h, room, backend, lives, clock, log = await priya_asks_forge()
    backend.route_extra = ops([("forge", "list the closed PRs", "floor")])
    clock.t += 3
    await h.on_final_transcript(MEET, "Google Meet", "and the closed ones")  # Priya's follow-up
    clock.t += 1
    speaker(h, "p2", "Dan", True)
    clock.t += 1
    await h.on_final_transcript(MEET, "Google Meet", "ok, lunch")  # Dan, before Priya's route call has run
    await settle(h)
    route = next(i for i, r in enumerate(backend.routes) if r[1] == "and the closed ones")
    assert backend.routes[route][0] == "Priya" and backend.route_bodies[route]["floor"]["mine"] is True
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")  # Forge's first run ends: the row starts
    await settle(h)
    turn = next(t for t in backend.turns if t["utterance"] == "list the closed PRs")
    assert turn["speakerName"] == "Priya", "its asker's, though Dan spoke last"


async def test_a_queued_agent_of_a_two_name_line_keeps_the_askers_label_when_the_meeting_talks_on():
    # review: the queue kept (agent, identity, text); its later summon read the latest speaker's label
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    await hear(h, "Forge and Gauge, what shipped this week?")  # no `ops`: Forge opens, Gauge is queued
    assert h._queue == [("gauge", MEET, "Forge and Gauge, what shipped this week?", "Priya")]
    clock.t += 2
    speaker(h, "p2", "Dan", True)
    clock.t += 1
    await hear(h, "we should order lunch")  # Dan's line: the latest label is his
    answer(h, backend.turns[0]["runId"], "forge", "Huddles shipped.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    h.on_speech(MEET, False)
    h.on_active_speakers(set())
    clock.t += 2
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.agent_id == "gauge" and h._voice.label == "Priya"
    assert "Priya asked" in str(lives[-1].seed) and "Dan asked" not in str(lives[-1].seed)


async def test_the_second_named_agent_still_answers_after_the_first_ones_meeting_voice_closed():
    # review: a meeting voice closing (its piece said, or talked over) cleared the queue behind it
    for how in ("said", "talked over"):
        h, room, backend, lives, clock, log = await meeting()
        speaker(h, "p1", "Priya", True)
        await hear(h, "Forge and Gauge, what shipped this week?")  # no `ops`: Forge opens, Gauge is queued
        assert [q[0] for q in h._queue] == ["gauge"]
        if how == "said":
            clock.t += h_mod.MEET_FOLLOWUP_S  # Forge's run still works; its voice has said its piece
        else:
            h.on_agent_state("forge", "speaking")
            talks(h, MEET, "Google Meet")
            clock.t += h_mod.TALK_OVER_S
        await h.tick()
        await settle(h)
        assert h._voice is None and [q[0] for q in h._queue] == ["gauge"], how
        h.on_speech(MEET, False)
        h.on_active_speakers(set())
        clock.t += h_mod.MEET_QUIET_S
        await h.tick()
        await settle(h)
        assert h._voice is not None and h._voice.agent_id == "gauge" and h._voice.label == "Priya", how
        assert h._queue == []


async def test_two_quick_speaker_changes_leave_one_speaker_not_a_stale_pair():
    # review: an `on` within a second of another's was kept as an overlap, so Dan's lines read "Priya + Dan"
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 0.5
    speaker(h, "p2", "Dan", True)
    clock.t += 5
    await hear(h, "so the launch plan")
    assert labels(h)[-1] == "Dan" and h._label(MEET) == "Dan"


async def test_the_speaker_before_is_not_named_on_the_next_persons_line():
    # review: the previous speaker's span ran half a second into the next line, which read "Priya + Dan"
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 5
    h.on_interim_transcript(MEET, "Google Meet", "so")  # Dan's words reach our transcriber first...
    clock.t += 0.4
    speaker(h, "p2", "Dan", True)  # ...Recall's change event lags them
    clock.t += 3
    await hear(h, "so the launch plan is ready")
    assert labels(h)[-1] == "Dan" and h._label(MEET) == "Dan"
    speaker(h, "p1", "Priya", True)
    clock.t += 0.5
    h.on_interim_transcript(MEET, "Google Meet", "ok")  # a line that begins just after the change
    clock.t += 2
    await hear(h, "ok that works for me")
    assert labels(h)[-1] == "Priya"


async def test_a_retried_turn_keeps_its_askers_name_when_the_voice_goes_to_someone_else():
    # review: each retry read the voice's label, so Priya's run was re-sent as Dan's after he named the agent
    h, room, backend, lives, clock, log = await meeting()
    backend.turn_gate = asyncio.Event()
    backend.turn_results = [BackendError(503, "runtime_unavailable", None)]
    speaker(h, "p1", "Priya", True)
    await h.on_final_transcript(MEET, "Google Meet", "Forge, what PRs are open?")
    for _ in range(40):
        await asyncio.sleep(0)
    run = backend.turns[0]["runId"]
    clock.t += 2
    speaker(h, "p2", "Dan", True)
    clock.t += 1
    await h.on_final_transcript(MEET, "Google Meet", "Forge, how many issues are open?")
    for _ in range(40):
        await asyncio.sleep(0)
    assert h._voice.label == "Dan"
    backend.turn_gate.set()
    await settle(h)
    tries = [t for t in backend.turns if t["runId"] == run]
    assert len(tries) == 2 and {t["speakerName"] for t in tries} == {"Priya"}


async def test_a_short_line_across_a_speaker_change_is_the_new_speakers():
    # review: Dan's 0.7 s "Forge, status?" began under Priya's span and was labelled Priya (the longer share)
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 5
    h.on_interim_transcript(MEET, "Google Meet", "Forge")
    clock.t += 0.4
    speaker(h, "p2", "Dan", True)  # Recall names Dan 0.4 s into his line
    clock.t += 0.3
    await hear(h, "Forge, status?")
    assert labels(h)[-1] == "Dan" and h._voice.label == "Dan"


async def test_a_named_line_understood_after_someone_else_named_the_agent_is_not_said_through_their_voice():
    # review: both lines share the Meet's one identity, so Priya's request was driven through Dan's voice
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p2", "Dan", True)
    await hear(h, "Forge, what PRs are open?")  # Forge's voice opens, with Dan
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")  # answered: Forge is free again
    h.on_agent_state("forge", "speaking")
    clock.t += 2
    h.on_agent_state("forge", "listening")
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops([("forge", "list the closed PRs", "name")])
    clock.t += 2
    speaker(h, "p1", "Priya", True)
    clock.t += 1
    await h.on_final_transcript(MEET, "Google Meet", "Forge, and the closed ones?")  # Priya names it: at /route
    for _ in range(40):
        await asyncio.sleep(0)
    assert h._voice.label == "Priya"
    clock.t += 2
    speaker(h, "p2", "Dan", True)
    clock.t += 1
    backend.route_extra = ops([("forge", "count the open issues", "name")])
    await h.on_final_transcript(MEET, "Google Meet", "Forge, how many issues are open?")  # Dan names it again
    for _ in range(40):
        await asyncio.sleep(0)
    assert h._voice.label == "Dan"
    calls = len(lives[0].calls)
    backend.route_gate.set()
    await settle(h)
    assert not any("Priya asked" in str(c) for c in lives[0].calls[calls:]), "not put to the voice now with Dan"
    (closed,) = [r for r in h._requests.items if r.task == "list the closed PRs"]
    assert closed.label == "Priya" and h._voice.driven_run != closed.run_id, "it still runs, as Priya's, voiceless"


async def test_a_follow_up_answer_on_a_voice_opened_for_an_announcement_is_talked_over_like_any_other():
    # review: the talk-over rule skipped every voice whose path was "announce", also after its announcement
    h, backend = await unheard_answer()
    clock = h._clock
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    v = h._voice
    assert v is not None and v.path == "announce"
    h.on_agent_state("forge", "speaking")  # it says the answer...
    h.on_agent_state("forge", "listening")  # ...to its end
    assert not v.announce and not v.announcing
    h.on_agent_state("forge", "speaking")  # later it speaks again (a follow-up's answer)
    talks(h, MEET, "Google Meet")
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert h._voice is None, "talked over: it stops"


async def test_an_earlier_answer_handed_to_an_open_meeting_voice_is_not_lost_when_it_is_talked_over():
    # review: the run of an earlier voice ended while a newer voice of its agent was open; its answer was
    # handed over as commentary, the row marked delivered, and a talk-over then lost it
    h, room, backend, lives, clock, log = await priya_asks_forge(rows=True)
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    assert h._voice is None and row.status == "working"  # the first voice closed; its run works on
    backend.route_extra = ops()
    await hear(h, "Forge, are you there?")  # a new voice of the same agent opens
    assert h._voice is not None and h._voice.agent_id == "forge"
    answer(h, row.run_id, "forge", "Three PRs are open.")  # the earlier run ends: handed to the open voice
    assert row.status == "delivered"
    h.on_agent_state("forge", "thinking")
    h.on_agent_state("forge", "speaking")
    talks(h, MEET, "Google Meet")
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert h._voice is None
    quiet_meet(h)
    await hear(h, "sorry, about the budget")
    clock.t += h_mod.CUT_HANDLED_S
    await h.tick()
    await settle(h)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."]


async def test_a_long_presentation_keeps_its_speakers_name():
    # review: an open span was cut at ten minutes, though Meet sends nothing until the speaker changes
    h, room, backend, lives, clock, log = await meeting()
    speaker(h, "p1", "Priya", True)
    clock.t += 25 * 60
    await hear(h, "and that is the roadmap")
    assert labels(h)[-1] == "Priya"


async def test_a_meeting_answer_with_no_request_row_is_said_at_the_next_pause_not_sent_to_the_record():
    # review: /route gave no `ops`, so the named request ran with no row; its voice closed after ten seconds
    # and the answer was posted as a late answer at once, never said
    h, room, backend, lives, clock, log = await priya_asks_forge()  # no rows: the whole line is the run
    assert h._requests.items == []
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    assert h._voice is None
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")
    assert backend.late_answers == [] and h._state()["lightbulb.agent.forge.status"] == "has_news"
    clock.t += h_mod.MEET_QUIET_S
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce" and "Six PRs are open." in str(lives[-1].seed)
    h.on_agent_state("forge", "speaking")
    assert h._rowless == set() and backend.late_answers == []


async def test_a_refused_voice_is_published_for_the_tile_and_cleared_when_voice_works_again():
    # 10-04 prod Meet: the day's free voice minutes were used up; /summon answered 402 and nobody was told
    h, room, backend, lives, clock, log = await meeting()
    backend.summon_error = BackendError(402, "voice_limit", "free_minutes_used")
    await hear(h, "Forge, what PRs are open?")
    assert lives == [] and h._state()["lightbulb.voice_limit"] == "free_minutes_used"
    assert any(a.get("lightbulb.voice_limit") == "free_minutes_used" for a in room.attrs), "set on the room"
    backend.summon_error = BackendError(503, "huddles_upstream", None)  # refused again, but not by the limit
    clock.t += 30
    await hear(h, "Forge, are you there?")
    assert h._state()["lightbulb.voice_limit"] == "", "an earlier limit must not stay up"
    backend.summon_error = BackendError(402, "voice_limit", "credits_low")
    clock.t += 30
    await hear(h, "Forge, are you there?")
    assert h._state()["lightbulb.voice_limit"] == "credits_low"
    backend.summon_error = None
    clock.t += 30
    await hear(h, "Forge, are you there?")
    assert lives and h._state()["lightbulb.voice_limit"] == ""


# -- per-person Meet audio: each person of the Meet is their own `meet:<roomId>:<participantId>` participant and
# runs on the Live path; the meeting policies (no presence, one transcript, unclaimed questions) stay --

P1, P2 = "meet:room-1:p1", "meet:room-1:p2"
MEET_PEOPLE = {P1: "Priya", P2: "Dan"}


async def per_person():
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants(dict(MEET_PEOPLE))
    return h, room, backend, lives, clock, log


async def says(h, who, text):
    await h.on_final_transcript(who, MEET_PEOPLE[who], text)
    await settle(h)


def test_only_the_one_colon_meet_participant_is_the_mixed_meeting():
    assert h_mod.mixed_meet(MEET) and not h_mod.mixed_meet(P1) and not h_mod.mixed_meet("u1")


async def test_a_per_person_meet_voice_hears_its_speaker_like_a_live_call():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "hey Forge what did we ship")
    assert lives[0].speaker == P1 and ("mute",) not in lives[0].calls
    assert ("thinking", "Priya is talking to you", None) in lives[0].calls
    assert h_mod.MEET_ACK not in str(lives[0].seed)


async def test_a_per_person_line_carries_its_persons_identity_and_name():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge, check the board")
    assert (backend.summons, backend.summon_names) == ([P1], ["Priya"])
    assert backend.routes[0][0] == "Priya"  # /route's speaker is the name, as a mixed meeting's label is
    turn = backend.turns[0]
    assert (turn["speaker"], turn["speakerName"]) == (P1, "Priya")
    assert [x.name for x in h._transcript.after(-1)] == ["Priya (Meet)"]


async def test_a_per_person_follow_up_needs_no_name_and_has_no_window():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge, what PRs are open?")
    h.on_agent_state("forge", "speaking")
    clock.t += 2
    h.on_agent_state("forge", "listening")
    clock.t += 30  # long past a mixed meeting's MEET_FOLLOWUP_S
    await says(h, P1, "and the closed ones")
    assert backend.route_bodies[-1]["floor"] == {"agent": "forge", "speaker": "Priya", "mine": True}


async def test_another_persons_unnamed_line_goes_the_live_way():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge, what PRs are open?")
    routes = len(backend.routes)
    clock.t += h_mod.THINKING_EVERY
    await says(h, P2, "should we ship Friday?")
    assert len(backend.routes) == routes + 1, "routed like any line, not transcript only"
    assert backend.route_humans[-1] == 2  # the room judge may pick nobody between two people
    assert backend.route_bodies[-1]["floor"] == {"agent": "forge", "speaker": "Priya", "mine": False}
    assert ("thinking", "Dan: should we ship Friday?", None) in lives[0].calls  # the floor-holder's context
    assert h._unclaimed is None and h._voice.speaker == P1


async def test_a_per_person_voice_follows_the_live_idle_rule_and_is_never_talked_over():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    h.on_agent_state("forge", "speaking")
    clock.t += 2
    h.on_agent_state("forge", "listening")
    clock.t += h_mod.MEET_FOLLOWUP_S + 1
    await h.tick()
    await settle(h)
    assert h._voice is not None, "its piece said, its run working: a Live voice stays open"
    h.on_agent_state("forge", "speaking")
    talks(h, P1)
    clock.t += 3 * h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and h._voice is not None, "the model's own ear handles a barge"


async def test_with_no_voice_a_per_person_statement_wakes_nobody_and_a_question_waits_unclaimed():
    h, room, backend, lives, clock, log = await per_person()
    backend.jev_to = "forge"
    await says(h, P1, "we shipped the importer")
    clock.t += 10
    await h.tick()
    await settle(h)
    assert backend.routes == [] and backend.sessions == []
    await says(h, P2, "what did we ship last week?")
    assert backend.routes == [] and h._unclaimed is not None and h._unclaimed[1] == P2
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert len(backend.routes) == 1 and backend.route_bodies[0]["unclaimed"] is True
    assert [x.name for x in h._transcript.after(-1)] == ["Priya (Meet)", "Dan (Meet)"]
    assert backend.presences == []


# -- a Live voice's answer cut by its own speaker's sound (10-04 prod Meet: a noisy microphone's junk lines landed
# as Forge began its answer; the model stopped for them and the answer was gone) --


async def forge_answers_priya(text="Three pull requests are open, the oldest is the Meta callbacks draft."):
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    (row,) = h._requests.items
    backend.route_extra = ops([])  # what Priya's microphone picks up next asks nothing
    answer(h, row.run_id, "forge", text)  # handed to its open voice, which says it
    return h, backend, lives, clock, text


async def quiet_tick(h, clock, seconds):
    clock.t += seconds
    await h.tick()
    await settle(h)


def resays(live):
    return [c for c in live.calls if c[0] == "commentary" and "Say it again now" in c[1]]


async def test_an_answer_cut_by_sound_that_was_not_for_the_agent_is_said_once_more():
    h, backend, lives, clock, text = await forge_answers_priya()
    await says(h, P1, "Keep us.")  # before a word of the answer: Live's model dropped its reply for this
    await says(h, P1, "Hello, guys.")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S / 2)
    assert resays(lives[0]) == [], "her words are not handled yet: a stop may be in them"
    await quiet_tick(h, clock, h_mod.MEET_QUIET_S)
    assert [c[1] for c in resays(lives[0])] == [h_mod.RESAY.format(answer=text)]
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", text)  # said to its end this time
    h.on_agent_state("forge", "listening")
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    assert len(resays(lives[0])) == 1 and backend.late_answers == [] and h._voice.answer is None


async def test_an_answer_interrupted_mid_speech_twice_goes_on_the_record_not_a_third_time():
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull requests", interrupted=True)
    h.on_agent_state("forge", "listening")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    assert len(resays(lives[0])) == 1
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull", interrupted=True)  # cut again
    h.on_agent_state("forge", "listening")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)  # nobody says a word about it: posted all the same
    assert len(resays(lives[0])) == 1
    assert [a[3] for a in backend.late_answers] == [text], "never lost: the meeting's record keeps it"


async def test_an_agent_that_replied_to_who_cut_it_does_not_repeat_its_answer():
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull requests", interrupted=True)
    h.on_agent_state("forge", "listening")
    await says(h, P1, "that is enough, thank you")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Sure.")  # she was talking to it
    h.on_agent_state("forge", "listening")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    assert resays(lives[0]) == []
    assert [a[3] for a in backend.late_answers] == [text]


async def test_an_answer_said_to_its_end_is_never_said_again():
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", text)
    h.on_agent_state("forge", "listening")
    await says(h, P1, "great, thanks")  # after the answer: nothing was cut
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S + h_mod.CUT_WAIT_S)
    assert resays(lives[0]) == [] and backend.late_answers == []


async def test_a_stop_said_over_a_cut_answer_neither_repeats_nor_posts_it():
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull requests", interrupted=True)
    h.on_agent_state("forge", "listening")
    await says(h, P1, "Forge, stop")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    assert resays(lives[0]) == [] and backend.late_answers == []


async def test_a_call_that_ends_on_a_cut_answer_puts_it_on_the_record():
    h, backend, lives, clock, text = await forge_answers_priya()
    await says(h, P1, "Keep us.")
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == [text]


async def test_a_follow_up_that_cut_the_answer_replaces_it_and_the_stale_one_is_not_posted():
    """Review: "actually, last month" over a cut answer made a follow-up run; the stale answer was posted beside
    the corrected one."""
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull requests", interrupted=True)
    h.on_agent_state("forge", "listening")
    backend.route_extra = ops([("forge", "the ones opened last month", "follow")])
    await says(h, P1, "actually, only the ones from last month")
    follow = h._requests.items[-1]
    answer(h, follow.run_id, "forge", "One was opened last month.")
    h.on_agent_said("forge", "One was opened last month.")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    await h.clear_presence()
    assert resays(lives[0]) == [] and backend.late_answers == []


async def test_another_request_that_cut_the_answer_leaves_it_on_the_record():
    h, backend, lives, clock, text = await forge_answers_priya()
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three pull requests", interrupted=True)
    h.on_agent_state("forge", "listening")
    backend.route_extra = ops([("forge", "what shipped last week", "floor")])
    await says(h, P1, "wait, what shipped last week")
    other = h._requests.items[-1]
    answer(h, other.run_id, "forge", "The importer shipped.")
    h.on_agent_said("forge", "The importer shipped.")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    assert resays(lives[0]) == []
    assert [a[3] for a in backend.late_answers] == [text], "a different request: the cut answer is not lost"


async def test_a_call_that_ends_while_the_second_saying_is_in_flight_puts_the_answer_on_the_record():
    """Review: the retry cleared `unheard`, so a hang-up before its audio lost the answer again."""
    h, backend, lives, clock, text = await forge_answers_priya()
    await says(h, P1, "Keep us.")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    assert len(resays(lives[0])) == 1
    await h.clear_presence()  # everyone left before the second saying was heard
    assert [a[3] for a in backend.late_answers] == [text]


async def test_a_long_answer_cut_in_a_later_sentence_is_still_kept():
    """Review: the first sentence said to its end cleared the answer; the second was cut and nothing was left."""
    first, second = "Three pull requests are open.", "The oldest is the Meta callbacks draft from August."
    h, backend, lives, clock, text = await forge_answers_priya(f"{first} {second}")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", first)  # only the start of the answer
    assert h._voice.answer is not None
    h.on_agent_said("forge", "The oldest is", interrupted=True)
    h.on_agent_state("forge", "listening")
    await quiet_tick(h, clock, h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S)
    assert [c[1] for c in resays(lives[0])] == [h_mod.RESAY.format(answer=text)]
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", first)
    h.on_agent_said("forge", second)  # both parts to their end: heard
    h.on_agent_state("forge", "listening")
    assert h._voice.answer is None
    await quiet_tick(h, clock, h_mod.CUT_WAIT_S)
    assert len(resays(lives[0])) == 1 and backend.late_answers == []
