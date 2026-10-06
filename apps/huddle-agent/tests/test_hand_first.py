"""A meeting, hand first (owner ruling 2026-10-06): an agent never speaks to acknowledge. A request shows on
its tile and runs with no voice, a ready answer raises its hand, and it speaks only when someone calls on it."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, make, ops, settle, talks

P1, P2 = "meet:room-1:p1", "meet:room-1:p2"
PEOPLE = {P1: "Priya", P2: "Dan"}


async def meeting():
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants(dict(PEOPLE))
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
    """Everyone stops: the speech detector says so, and the worker looks at once."""
    for who in PEOPLE:
        h.on_speech(who, False)
    h.on_active_speakers(set())
    clock.t += 0.1
    await h.tick()
    await turns()


def status(h, agent):
    return h._state()[f"lightbulb.agent.{agent}.status"]


async def asked(text="Forge, what PRs are open?", task="list the open PRs"):
    h, backend, lives, clock = await meeting()
    backend.route_extra = ops([("forge", task, "name")])
    await says(h, P1, text)
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
        assert lives[0].calls[-1] == ("interrupt",)
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
    assert len(told(lives)) == 2 and len(lives) == 1 and status(h, "forge") == "idle", "said whole, same voice"


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
