"""A meeting, hand first (owner ruling 2026-10-06): an agent never speaks to acknowledge. A request shows on
its tile and runs with no voice, a ready answer raises its hand, and it speaks only when someone calls on it."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, make, ops, settle, talks

P1, P2 = "meet:room-1:p1", "meet:room-1:p2"
PEOPLE = {P1: "Priya", P2: "Dan"}


async def meeting(lines=False):
    """`lines`: the voice also says it is on it, how it is going, and that it is here. Off for the tests of the
    answer alone, which count sessions and what was said."""
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants(dict(PEOPLE))
    if not lines:
        h._line = lambda *a, **k: None
    return h, backend, lives, clock


async def says(h, who, text):
    await h.on_final_transcript(who, PEOPLE[who], text)
    await settle(h)


async def hears(h, who, text):
    """`says`, while an answer is being said: its play never ends, so nothing waits for every task."""
    await h.on_final_transcript(who, PEOPLE[who], text)
    await turns()


async def turns():
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)


def told(lives):
    """The answers a voice was given to say."""
    return [c[1] for live in lives for c in live.calls if c[0] == "commentary"]


async def first_sound(h, agent="forge"):
    h.on_agent_state(agent, "speaking")
    await turns()


async def last_word(h, agent="forge"):
    h.on_agent_state(agent, "listening")
    await turns()


async def pause(h, clock):
    """Everyone stops, for long enough even after a false start: the speech detector says so."""
    for who in PEOPLE:
        h.on_speech(who, False)
    h.on_active_speakers(set())
    clock.t += h_mod.HAND_RETRY_QUIET_S + 0.1
    await h.tick()
    await turns()


def status(h, agent):
    return h._state()[f"lightbulb.agent.{agent}.status"]


async def asked(text="Forge, what PRs are open?", task="list the open PRs", lines=False):
    h, backend, lives, clock = await meeting(lines)
    backend.route_extra = ops([("forge", task, "name")])
    await (hears if lines else says)(h, P1, text)  # with lines, its "on it" starts at once and is still being said
    backend.route_extra = ops([])
    return h, backend, lives, clock


async def test_a_named_request_opens_no_voice_and_shows_on_the_tile():
    h, backend, lives, clock = await asked()
    assert lives == [] and backend.sessions == [], "nobody speaks to acknowledge"
    assert len(backend.turns) == 1 and status(h, "forge") == "working"
    assert h._state()["lightbulb.agent.forge.working"]


async def test_a_name_in_the_middle_of_a_sentence_asks_that_agent():
    """10-06 prod Meet: "...all right, hey Forge, how is it going?" got nothing for 30 s."""
    h, backend, lives, clock = await asked("Now let us think about how to test this. All right, hey Forge, what PRs are open?")
    assert len(backend.turns) == 1 and lives == []



async def test_a_ready_answer_raises_its_hand_and_its_live_voice_says_it_when_people_stop():
    """Owner rulings 10-06: no set phrase to hear it, and GPT-Live says it, live, in its own voice."""
    h, backend, lives, clock = await asked()
    talks(h, P2)  # the meeting goes on while it works
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    assert status(h, "forge") == "has_news"
    assert len(lives) == 1 and lives[0].agent_id == "forge", "its voice opens when the hand goes up"
    assert told(lives) == [] and "Stay silent" in str(lives[0].seed), "open and silent"
    clock.t += 60
    await h.tick()
    await turns()
    assert told(lives) == [] and backend.late_answers == [], "never over a person"
    await pause(h, clock)
    assert told(lives) == ["Three PRs are open."], "told to speak the moment they stop"
    assert status(h, "forge") == "has_news", "the tile says speaking only at its first sound"
    await first_sound(h)
    assert status(h, "forge") == "speaking"
    await last_word(h)
    assert status(h, "forge") == "idle" and ("close",) in [c[:1] for c in lives[0].calls] or status(h, "forge") == "idle"
    assert len(backend.turns) == 1 and h.gptlive_seconds() >= 0


async def test_someone_turning_to_it_by_name_hears_the_answer_without_waiting_for_a_pause():
    h, backend, lives, clock = await asked()
    talks(h, P2)
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await hears(h, P2, "Go ahead, Forge.")  # anyone may
    assert told(lives) == ["Three PRs are open."] and len(backend.turns) == 1, "not a new request"


async def test_talk_before_its_first_sound_calls_the_start_off_and_uses_up_no_try():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    for _ in range(h_mod.DELIVERY_TRIES + 2):  # a lively meeting: many short pauses
        await pause(h, clock)
        assert h._saying is not None
        h.on_speech(P2, True)  # someone goes on before it made a sound
        await turns()
        assert h._saying is None and status(h, "forge") == "has_news"
        assert ("interrupt",) in lives[-2].calls and ("close",) in lives[-2].calls, "that session is closed: surely silent"
    clock.t += h_mod.gate.ANNOUNCE_QUIET + 1
    await h.tick()
    await turns()
    assert backend.late_answers == [], "nobody heard a start: it is still waiting, not sent to the notes"


async def test_talked_over_three_times_in_pauses_it_took_itself_the_answer_goes_to_the_notes():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    for _ in range(h_mod.DELIVERY_TRIES):
        clock.t += 10
        await pause(h, clock)
        await first_sound(h)
        assert status(h, "forge") == "speaking"
        clock.t += h_mod.COLLIDE_S
        h.on_speech(P2, True)
        h._yield_if_talked_over()
        await turns()
        assert status(h, "forge") == "has_news"
    clock.t += 10
    await pause(h, clock)
    await h.tick()
    await turns()
    assert status(h, "forge") == "idle" and [a[3] for a in backend.late_answers] == ["Three PRs are open."]


async def test_just_its_name_calls_on_an_agent_with_its_hand_up():
    h, backend, lives, clock = await asked()
    talks(h, P2)
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await hears(h, P1, "Forge?")
    assert told(lives) == ["Three PRs are open."]
    await first_sound(h)
    await last_word(h)
    await hears(h, P1, "Okay Forge, go ahead.")  # no hand up now: it asks nothing
    assert len(backend.turns) == 1 and len(told(lives)) == 1
    assert h._state()["lightbulb.agent.forge.working"] == "listening to Priya"


async def test_talk_between_people_reaches_no_agent():
    h, backend, lives, clock = await asked()
    routes, turns = len(backend.routes), len(backend.turns)
    await says(h, P2, "I think it is cool that they are even in here.")
    clock.t += h_mod.MEET_FOLLOWUP_S + 1
    await says(h, P1, "We are going to need to test this a lot.")
    assert (len(backend.routes), len(backend.turns)) == (routes, turns) and lives == []


async def test_who_asked_can_follow_up_without_the_name_for_a_few_seconds():
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "the closed PRs too", "follow")])
    clock.t += h_mod.MEET_FOLLOWUP_S - 1
    await says(h, P1, "and the closed ones too")
    # its own request, queued behind the one it is working on, and still no voice
    assert [(r.agent, r.by) for r in h._requests.items] == [("forge", "name"), ("forge", "follow")] and lives == []



async def test_talking_over_its_answer_stops_it_and_its_hand_stays_up():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await pause(h, clock)
    await first_sound(h)
    assert status(h, "forge") == "speaking"
    clock.t += h_mod.COLLIDE_S
    h._yield_if_talked_over()  # a cough: nobody is still talking when the clock runs out
    await turns()
    assert status(h, "forge") == "speaking"
    h.on_speech(P2, True)
    assert h._over_timer is not None, "the speech detector alone starts the clock"
    h._yield_if_talked_over()
    await turns()
    assert status(h, "forge") == "has_news" and backend.late_answers == [], "not lost and not posted: the hand again"
    assert ("interrupt",) in lives[0].calls
    clock.t += h_mod.gate.ANNOUNCE_QUIET  # it does not jump back in the instant they breathe
    await pause(h, clock)
    await first_sound(h)
    await last_word(h)
    assert len(told(lives)) == 2 and len(lives) == 2 and status(h, "forge") == "idle", "said whole, by a fresh session"


async def test_an_answer_nobody_calls_on_goes_on_the_record_when_the_meeting_ends():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."] and lives == []


async def test_a_question_nobody_takes_is_worked_on_with_no_voice():
    h, backend, lives, clock = await meeting()
    backend.route_extra = ops([("forge", "what shipped last week", "fit")])
    await says(h, P2, "what did we ship last week?")
    assert backend.turns == []
    clock.t += h_mod.UNCLAIMED_QUESTION_S
    await h.tick()
    await settle(h)
    assert len(backend.turns) == 1 and lives == []


async def test_its_tile_shows_it_heard_its_name_while_the_sentence_is_still_being_said():
    h, backend, lives, clock = await meeting()
    h.on_interim_transcript(P1, "Priya", "all right, hey Forge")
    assert status(h, "forge") == "working" and "listening to Priya" in h._state()["lightbulb.agent.forge.working"]
    assert lives == [] and backend.routes == []
    clock.t += h_mod.HEARD_NAME_S + 1  # nothing followed: back to idle
    assert status(h, "forge") == "idle"



async def test_a_call_on_gives_the_floor_when_they_stop_talking_not_seconds_later_on_the_final():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    talks(h, P2)
    h.on_interim_transcript(P2, "Dan", "go ahead Forge")
    h.on_speech(P2, False)  # the speech detector: they stopped
    h.on_active_speakers(set())
    await turns()
    assert len(told(lives)) == 1
    await hears(h, P2, "Go ahead, Forge.")  # the final, seconds later
    assert len(told(lives)) == 1 and len(backend.turns) == 1, "not delivered twice, not a new request"


async def test_stopping_mid_sentence_on_its_name_does_not_give_it_the_floor_for_a_new_request():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    h.on_interim_transcript(P2, "Dan", "Forge, never mind")
    h.on_speech(P2, False)
    await settle(h)
    assert lives == []
    h.on_interim_transcript(P2, "Dan", "Forge, can you also check the deploy pipeline status")
    h.on_speech(P2, False)
    await settle(h)
    assert lives == [], "a real request is decided on its final"


async def test_stop_while_it_says_its_answer_ends_it_and_it_is_not_said_again():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await pause(h, clock)
    await first_sound(h)
    await hears(h, P2, "Forge, stop.")
    await turns()
    assert status(h, "forge") == "idle" and not h._news.get("forge")
    assert backend.late_answers == [], "a stopped request is not posted either (engine §3.6)"


async def test_a_voice_the_backend_refuses_says_nothing():
    """The room was archived, the agent left it, or today's voice minutes are used up."""
    from huddle_agent.backend import BackendError

    h, backend, lives, clock = await asked()

    async def refuse(agent_id, silent=False):
        raise BackendError(409, "agent_not_in_room", None)

    backend.session = refuse
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await pause(h, clock)
    assert lives == [] and told(lives) == []


async def test_an_agent_has_one_hand_voice_however_many_answers_and_its_time_is_charged_to_the_call():
    """Codex on #3776: two sessions of one agent share a track name, and their time was billed to nobody."""
    h, backend, lives, clock = await asked()
    run = h._requests.items[0].run_id
    talks(h, P2)
    answer(h, run, "forge", "Three PRs are open.")
    h._news["forge"].append(("run-2", "the closed PRs", "Nine are closed.", clock.t, P1))  # a second one, ready too
    h._rowless.add("run-2")
    await turns()
    assert len(lives) == 1 and backend.silent_sessions == ["forge"], "one voice, and no naming prompt used up"
    for words in ("Three PRs are open.", "Nine are closed."):
        clock.t += h_mod.gate.ANNOUNCE_QUIET
        await pause(h, clock)
        assert told(lives)[-1] == words
        await first_sound(h)
        await last_word(h)
    assert len(lives) == 1 and status(h, "forge") == "idle", "the same voice said both"
    lives[0].final_usage = 12.0
    await h.tick()
    await turns()
    assert ("close",) in [c[:1] for c in lives[0].calls]
    await h.clear_presence()
    assert backend.ends and backend.ends[-1][1] >= 12, "its seconds reach the call's bill"


async def test_a_hand_nobody_leaves_a_pause_for_closes_its_voice_and_opens_it_again_to_speak():
    h, backend, lives, clock = await asked()
    talks(h, P2)
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    clock.t += h_mod.HAND_HOLD_S
    await h.tick()
    await turns()
    assert ("close",) in [c[:1] for c in lives[0].calls] and status(h, "forge") == "has_news", "the hand stays up"
    await h.tick()
    await turns()
    assert len(lives) == 1, "not reopened while people talk"
    await pause(h, clock)
    assert len(lives) == 2 and told(lives) == ["Three PRs are open."]


async def test_the_last_person_leaving_closes_a_hand_voice():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await h.on_participants({})
    await turns()
    assert ("close",) in [c[:1] for c in lives[0].calls]


async def test_someone_starting_as_it_starts_makes_it_yield_at_once_and_costs_no_try():
    """Rig 10-06: its start takes 2 s; a person who begins in that time must not be talked over for a second."""
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    for _ in range(h_mod.DELIVERY_TRIES + 1):
        clock.t += h_mod.gate.ANNOUNCE_QUIET
        await pause(h, clock)
        await first_sound(h)
        clock.t += 0.3
        h.on_speech(P2, True)  # no grace second: a collision, not an interruption
        await turns()
        assert h._saying is None and status(h, "forge") == "has_news" and ("close",) in lives[-2].calls
    assert backend.late_answers == [] and h._news.get("forge")


async def test_after_a_false_start_it_waits_for_a_real_pause_not_the_next_breath():
    """Rig 10-06: with 1.7 s between speakers it blurted a syllable at every gap."""
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    await pause(h, clock)
    h.on_speech(P2, True)  # ran into someone
    await turns()
    for who in PEOPLE:
        h.on_speech(who, False)
    clock.t += 0.5
    await h.tick()
    await turns()
    assert len(told(lives)) == 1, "a breath: it holds"
    clock.t += h_mod.HAND_RETRY_QUIET_S
    await h.tick()
    await turns()
    assert len(told(lives)) == 2


async def test_its_name_then_a_pause_then_the_question_is_asked_of_that_agent():
    """10-06 prod Meet (Aden): "Friday." ... "Can you tell me about our most recent container run?" came as two
    lines; the second names nobody, was routed with no agent, and nothing happened."""
    h, backend, lives, clock = await meeting()
    await says(h, P1, "Forge.")
    assert backend.turns == [] and h._state()["lightbulb.agent.forge.working"] == "listening to Priya"
    backend.route_extra = ops([("forge", "the most recent container run", "floor")])
    clock.t += 6
    await says(h, P1, "Can you tell me about our most recent container run?")
    assert backend.route_floors[-1] == {"agent": "forge", "speaker": "Priya", "mine": True}
    assert len(backend.turns) == 1 and status(h, "forge") == "working"
    # and a line that names it carries the name to /route as well
    await says(h, P2, "Forge, what PRs are open?")
    assert backend.route_floors[-1]["agent"] == "forge" and backend.routes[-1][1].startswith("Forge")


async def test_only_the_first_hand_holds_a_voice_open_and_a_failed_close_still_bills_its_time():
    """Codex on #3777: two held hands spend two sessions against one window; a close that raises billed nothing."""
    h, backend, lives, clock = await meeting()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = ops([("gauge", "this week's signups", "name")])
    await says(h, P2, "Gauge, how many signups this week?")
    backend.route_extra = ops([])
    talks(h, P2)
    forge, gauge = (next(r.run_id for r in h._requests.items if r.agent == a) for a in ("forge", "gauge"))
    answer(h, forge, "forge", "Three PRs are open.")
    clock.t += 1
    answer(h, gauge, "gauge", "Forty signups.")
    await turns()
    assert [l.agent_id for l in lives] == ["forge"], "the second hand waits its turn with no session open"

    async def broken_close():
        raise RuntimeError("close failed")

    lives[0].aclose = broken_close
    lives[0].usage_seconds = lambda: (_ for _ in ()).throw(RuntimeError("no usage"))
    clock.t += 7
    await h.end_exchange(force=True)  # the worker is leaving
    assert backend.ends and backend.ends[-1][1] >= 7, "the time it was open is billed, and before /end"


# --- the voice being natural (owner ruling 10-06, after a real meeting that felt dead) ---


async def spoken(h, lives, clock, agent="forge"):
    """People stop, and the agent says the next thing it has: what it was told, once it is done."""
    if h._saying is None:  # nothing is being said yet: people stop, and it starts
        before = len(told(lives))
        await pause(h, clock)
        assert len(told(lives)) == before + 1, "nothing was said"
    await first_sound(h, agent)
    await last_word(h, agent)
    return told(lives)[-1]


async def test_it_says_it_is_on_it_when_asked_and_then_the_answer():
    h, backend, lives, clock = await asked(lines=True)
    assert await spoken(h, lives, clock) == "On it, Priya: list the open PRs."
    assert status(h, "forge") == "working", "said, and back to work"
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    clock.t += h_mod.gate.ANNOUNCE_QUIET
    assert await spoken(h, lives, clock) == "Three PRs are open."


async def test_asked_of_one_agent_and_routed_to_another_the_one_who_takes_it_says_so():
    """10-06 prod Meet: "Friday, ... the most recent PR you made?" went to Forge, whose hand then came out of
    nowhere. Jev may be right about whose work it is; the meeting has to hear who took it."""
    h, backend, lives, clock = await meeting(lines=True)
    backend.route_extra = ops([("forge", "the most recent PR", "fit")])
    await hears(h, P1, "Ada, can you tell me about the most recent PR?")
    assert await spoken(h, lives, clock) == "Priya asked Ada, but this one is mine. I'm on it: the most recent PR."


async def test_an_answer_that_is_ready_is_said_and_the_unsaid_on_it_is_dropped():
    h, backend, lives, clock = await meeting(lines=True)
    talks(h, P2)  # someone else is talking when the request is understood: its "on it" has to wait
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await hears(h, P1, "Forge, what PRs are open?")
    assert told(lives) == []
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await turns()
    assert await spoken(h, lives, clock) == "Three PRs are open."
    clock.t += h_mod.gate.ANNOUNCE_QUIET
    await pause(h, clock)
    assert told(lives) == ["Three PRs are open."], "no late on-it after the answer"


async def test_a_long_run_says_how_it_is_going_before_each_lookup():
    """10-06 prod Meet: a two-minute run was two minutes of silence."""
    from huddle_agent.frames import Run

    h, backend, lives, clock = await asked(lines=True)
    await spoken(h, lives, clock)  # on it
    run = Run(h._requests.items[0].run_id, "forge", speaker=P1)
    h.say(run, "Three are open so far, ", False)
    h.say(run, "checking the closed ones.", False)
    assert len(told(lives)) == 1, "a note is whole only when its lookup starts"
    h.progress(run, "gh pr list")
    await turns()
    clock.t += h_mod.gate.ANNOUNCE_QUIET
    assert await spoken(h, lives, clock) == "Three are open so far, checking the closed ones."


async def test_calling_its_name_while_it_works_gets_still_on_it_and_while_idle_gets_i_am_here():
    """10-06 prod Meet: "Hello?" "Forge." "Forge, hello? Anyone?" got nothing for a minute."""
    h, backend, lives, clock = await asked(lines=True)
    await spoken(h, lives, clock)
    clock.t += 20
    await hears(h, P1, "Forge?")
    assert (await spoken(h, lives, clock)).startswith("Still working on it: list the open PRs")
    h2, backend2, lives2, clock2 = await meeting(lines=True)
    await hears(h2, P1, "Forge?")
    assert await spoken(h2, lives2, clock2) == "Yes, I'm here."


async def test_a_line_that_trails_off_waits_for_its_rest_and_is_one_request():
    """10-06 prod Meet: "Can one of you tell me..." was run as a request, and the agent asked what it meant."""
    h, backend, lives, clock = await asked()
    routes = len(backend.routes)
    await says(h, P1, "Oh, and...")
    await says(h, P1, "Can one of you tell me...")
    await says(h, P1, "Um...")
    assert len(backend.routes) == routes and len(backend.turns) == 1, "nothing is asked yet"
    backend.route_extra = ops([("forge", "Priya's open tasks", "floor")])
    await says(h, P1, "Can one of you tell me about my tasks that I have open?")
    assert len(backend.routes) == routes + 1 and backend.routes[-1][1].endswith("about my tasks that I have open?")
    assert "Can one of you tell me..." in backend.routes[-1][1]


async def test_a_note_nobody_left_a_pause_for_is_dropped_not_said_late():
    h, backend, lives, clock = await meeting(lines=True)
    talks(h, P2)
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await hears(h, P1, "Forge, what PRs are open?")
    clock.t += h_mod.LINE_TTL_S["ack"] + 1
    await pause(h, clock)
    assert told(lives) == [], "a stale on-it is worse than none"
