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



async def test_a_ready_answer_raises_its_hand_and_is_not_said_until_someone_calls_on_it():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    assert status(h, "forge") == "has_news" and h._state()["lightbulb.agent.forge.hand"]
    await settle(h)
    assert h._room.prepared == [("forge", "Three PRs are open.")], "its audio is made when the hand goes up"
    assert backend.meet_chats == ['\u270b Forge has an answer: list the open PRs. Say "go ahead, Forge".'], "and the Meet's chat says so, once"
    clock.t += 60  # a long quiet: it still does not take the floor by itself
    await h.tick()
    await settle(h)
    assert not hasattr(h._room, "played") and backend.late_answers == []
    await says(h, P2, "Go ahead, Forge.")  # anyone may call on it
    assert h._room.played == [("forge", "Three PRs are open.")] and lives == [], "prepared audio, no voice session"
    assert status(h, "forge") == "idle" and h._room.discarded == h._room.played
    assert len(backend.turns) == 1, "a call-on is not a new request"



async def test_just_its_name_calls_on_an_agent_with_its_hand_up():
    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    await says(h, P1, "Forge?")
    assert h._room.played == [("forge", "Three PRs are open.")]
    await says(h, P1, "Okay Forge, go ahead.")  # no hand up now: it asks nothing
    assert len(backend.turns) == 1 and len(h._room.played) == 1
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



async def test_talking_over_a_called_on_answer_stops_it_and_its_hand_goes_back_up():
    import asyncio

    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    h._room.hold = asyncio.Event()  # the answer is being said
    await hears(h, P1, "Go ahead, Forge.")
    assert status(h, "forge") == "speaking"
    h._yield_if_talked_over()  # a cough: nobody is still talking when the clock runs out
    await turns()
    assert status(h, "forge") == "speaking"
    h.on_speech(P2, True)
    assert h._over_timer is not None, "the speech detector alone starts the clock"
    h._yield_if_talked_over()
    await turns()
    assert status(h, "forge") == "has_news" and backend.late_answers == [], "not lost and not posted: the hand again"
    h.on_speech(P2, False)
    h._room.hold = None
    await says(h, P1, "Sorry Forge, go ahead.")
    assert len(h._room.played) == 2 and status(h, "forge") == "idle", "said whole the second time"


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
    talks(h, P2)
    h.on_interim_transcript(P2, "Dan", "go ahead Forge")
    h.on_speech(P2, False)  # the speech detector: they stopped
    h.on_active_speakers(set())
    await settle(h)
    assert len(h._room.played) == 1
    await says(h, P2, "Go ahead, Forge.")  # the final, seconds later
    assert len(h._room.played) == 1 and len(backend.turns) == 1, "not delivered twice, not a new request"


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
    import asyncio

    h, backend, lives, clock = await asked()
    answer(h, h._requests.items[0].run_id, "forge", "Three PRs are open.")
    h._room.hold = asyncio.Event()
    await hears(h, P1, "Go ahead, Forge.")
    await hears(h, P2, "Forge, stop.")
    await turns()
    assert status(h, "forge") == "idle" and not h._news.get("forge")
    assert backend.late_answers == [], "a stopped request is not posted either (engine §3.6)"
