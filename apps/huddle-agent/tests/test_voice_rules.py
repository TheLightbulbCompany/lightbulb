"""The general rules the replay loop (tests/replay) was used to settle, one test or two each: what a cut
sentence counts as, when a pause is counted from, what a listening sound does, and so on. Each was accepted
on the 15 replayed scenarios under two transcriber timings; these pin the rule itself."""

import asyncio

from huddle_agent import huddle as h_mod
from test_huddle import answer, frame, hand, ops, packet, pause, settle, spoken, talks
from test_meet_exchange import asked, closes, quiet, says_of, talked_over
from test_meeting_mode import P1, P2, per_person, says

FIRST, SECOND = "Three PRs are open.", "The oldest is two days old."
BREATH = h_mod.SETTLE_S + 0.1  # the room has settled, and the conversation has not paused


def agent_lines(h):
    return [x.text for x in h._transcript.after(-1) if x.kind == "agent"]


async def answering(text=f"{FIRST} {SECOND}"):
    """Forge is saying its answer to Priya: its first order is out."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", text)
    assert says_of(lives[0])[-1] == ("say", text)
    return h, backend, lives, clock, row


def cut(h, live, index, text, heard_s):
    """The mouth's receipts for a unit that was cut with `heard_s` seconds of it audible, and for its order."""
    h.on_unit("forge", live.order, index, text, "cut", {"loudMs": round(heard_s * 1000), "heardMs": round(heard_s * 1000)})
    h.on_playback_ended("forge", live.order)


def seconds(text, share):
    return len(text) * share / h_mod.SPEECH_CPS


async def listening_sound(h, clock, who=P2):
    """The talker stops, their words were only a listening sound, and the room settles: a paused mouth goes on."""
    quiet(h)
    await says(h, who, "Mm-hm.")
    await pause(h, clock, BREATH)


async def asked_and_acknowledging():
    """Priya asked Forge, and its acknowledgement of two sentences is on its way out: [sentence, sentence]."""
    h, room, backend, lives, clock, log = await per_person()
    h._line_seed = h_mod.ACKS.index("Got it. Give me a moment.")
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = ops([])
    return h, lives, clock, h_mod.units(says_of(lives[0])[0][1])


# -- a sentence that was cut is not said: it stays owed and is said again whole. What was audible is on the --
# -- record as a marked piece, and is no part of what was delivered                                        --


def cut_line(piece):
    return piece + h_mod.CUT_MARK


async def test_a_sentence_cut_after_a_word_is_not_said_and_is_said_whole_later():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 0.3))
    assert agent_lines(h)[1:] == [cut_line("Three")], "what the room heard of it, marked as cut off"
    assert hand(h, "forge") == [f"{FIRST} {SECOND}"], "the cursor did not move"
    await listening_sound(h, clock)
    assert says_of(lives[0])[2:] == [("say", f"{FIRST} {SECOND}")], "said whole, from the sentence it was cut in"


async def test_a_sentence_cut_before_a_word_of_it_was_audible_leaves_no_line():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, 0.05)
    assert len(agent_lines(h)) == 1, "only its acknowledgement is on the record"


async def test_a_sentence_cut_one_word_short_is_not_delivered_and_its_request_stays_owed():
    """10-08 review: "The monthly price is fifty" was recorded, "dollars" thrown away, the request delivered."""
    price = "The monthly price is fifty dollars."
    h, backend, lives, clock, row = await answering(price)
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, price, seconds(price, 0.85))
    assert agent_lines(h)[-1] == cut_line("The monthly price is fifty"), "the audible piece, for the record only"
    assert row.status != "delivered" and hand(h, "forge") == [price], "seconds guessed from characters deliver nothing"
    await listening_sound(h, clock)
    assert says_of(lives[0])[2:] == [("say", price)], "the whole sentence, with its last word"
    h.on_unit("forge", lives[0].order, 0, price, "said", {"loudMs": 2300})
    assert agent_lines(h)[-1] == price and row.status == "delivered" and h._ledger == []


async def test_a_sentence_cut_in_the_silence_after_its_last_word_is_still_owed():
    h, backend, lives, clock, row = await answering(FIRST)
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 1.2))  # every second of it by the estimate: its playback did not end
    assert agent_lines(h)[-1] == cut_line(FIRST) and row.status != "delivered" and hand(h, "forge") == [FIRST]


async def test_the_share_is_measured_at_the_rate_the_calls_own_units_were_said_at():
    h, backend, lives, clock, row = await answering()
    third, slow = "One of them is a draft.", lambda text: round(len(text) / (h_mod.SPEECH_CPS / 2) * 1000)
    for index, unit in enumerate((FIRST, SECOND)):  # this voice speaks at half the assumed rate
        h.on_unit("forge", lives[0].order, index, unit, "said", {"loudMs": slow(unit)})
    assert round(h._audible(third, {"heardMs": slow(third) // 2})[1], 2) == 0.5, "half its seconds at this voice's rate: half of it"


async def test_a_paused_answer_is_not_said_on_before_the_receipt_of_what_was_cut():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    await listening_sound(h, clock)  # it may go on, and the cut sentence's receipt is not in
    assert len(says_of(lives[0])) == 2, "where it goes on from is not known yet"
    h.on_unit("forge", lives[0].order, 0, FIRST, "said", {"loudMs": 1200})  # it had ended as the yield came
    h.on_playback_ended("forge", lives[0].order)
    await settle(h)
    assert says_of(lives[0])[2:] == [("say", SECOND)], "the receipt said the sentence was played to its end"


async def test_a_voice_that_never_sends_the_receipt_holds_the_answer_a_second_at_most():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    await listening_sound(h, clock)
    assert len(says_of(lives[0])) == 2
    await pause(h, clock, h_mod.RECEIPT_WAIT_S)
    assert says_of(lives[0])[2:] == [("say", f"{FIRST} {SECOND}")]


# -- a line is owed to a moment: one that stopped after some of it was heard is not finished later --


async def test_an_acknowledgement_cut_after_its_first_sentence_is_not_finished_later():
    h, lives, clock, ack = await asked_and_acknowledging()
    h.on_unit("forge", lives[0].order, 0, ack[0], "said", {"loudMs": 900})
    await talked_over_line(h, clock)
    assert [u for u in h._ledger if u.kind == "ack"] == [], "its moment has passed"
    await listening_sound(h, clock, P1)
    await pause(h, clock)
    assert len(says_of(lives[0])) == 1 and agent_lines(h) == [ack[0]], "(no receipt of the cut sentence came: no piece)"


async def test_an_acknowledgement_whose_second_sentence_did_not_play_is_not_finished_later():
    h, lives, clock, ack = await asked_and_acknowledging()
    h.on_unit("forge", lives[0].order, 0, ack[0], "said", {"loudMs": 900})
    h.on_unit("forge", lives[0].order, 1, ack[1], "silent", {"loudMs": 0})
    h.on_playback_ended("forge", lives[0].order, True)
    await pause(h, clock)
    assert len(says_of(lives[0])) == 1 and [u for u in h._ledger if u.kind == "ack"] == []


async def test_an_acknowledgement_cut_before_a_word_of_it_was_heard_is_still_owed():
    h, lives, clock, ack = await asked_and_acknowledging()
    await talked_over_line(h, clock)
    cut(h, lives[0], 0, ack[0], 0.0)
    assert [u.kind for u in h._ledger] == ["ack"]
    await listening_sound(h, clock, P1)
    assert says_of(lives[0])[1:] == [("say", " ".join(ack))], "nothing of it was said: it is said whole"


async def talked_over_line(h, clock):
    """Its own person talks over the acknowledgement long enough for it to yield."""
    talks(h, P1)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)


# -- a listening sound ("mm-hm", "yeah"): known from the first words, not from the final transcript --


async def test_a_sentence_being_said_does_not_yield_to_words_that_are_only_a_listening_sound():
    h, backend, lives, clock, row = await answering()
    talks(h, P2)
    h.on_interim_transcript(P2, "Dan", "Mm-hm")
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    assert ("interrupt",) not in lives[0].calls and [u.state for u in h._ledger] == ["speaking"]
    h.on_interim_transcript(P2, "Dan", "Mm-hm, but wait")  # it was the start of a turn after all
    assert ("interrupt",) in lives[0].calls and [u.state for u in h._ledger] == ["paused"], "it yields as the words come"


async def test_talk_with_no_words_yet_makes_it_yield_as_before():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)  # (asserts it paused)


async def test_a_paused_answer_goes_on_when_the_listening_sound_ends_without_waiting_for_its_final():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, 0.0)
    h.on_interim_transcript(P2, "Dan", "Yeah")  # the words came after the yield
    assert len(says_of(lives[0])) == 2, "they are still heard speaking: nothing starts"
    quiet(h)  # the detector's stop. The final is 1.2-1.9 s away, and the room has not had SETTLE_S of quiet.
    await settle(h)
    assert says_of(lives[0])[2:] == [("say", f"{FIRST} {SECOND}")], "at once"
    await says(h, P2, "Yeah.")  # the final, when it comes, changes nothing
    assert len(says_of(lives[0])) == 3 and h._turns == {}


async def test_a_paused_answer_waits_for_the_final_of_talk_whose_first_words_were_real_words():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, 0.0)
    h.on_interim_transcript(P2, "Dan", "Yeah, I think")
    quiet(h)
    await pause(h, clock, BREATH)
    assert len(says_of(lives[0])) == 2 and [u.state for u in h._ledger] == ["paused"]


# -- a sentence that began as a person did: they were speaking first, and the detector had not heard them --


def sounds(h, live, index=0):
    """The mouth's report: the first frame of that unit with sound in it has gone to the track."""
    h.on_unit_audible("forge", live.order, index)


async def test_a_sentence_whose_first_sound_just_went_out_yields_at_once_to_a_person_who_is_then_heard():
    h, backend, lives, clock, row = await answering()
    assert h.may_say("forge", lives[0].order, 0), "its first frame goes out"
    sounds(h, lives[0])
    clock.t += h_mod.PREROLL_S - 0.1
    talks(h, P2)  # heard now: they began before it did
    assert ("interrupt",) in lives[0].calls and [u.state for u in h._ledger] == ["paused"], "no TALK_OVER_S of waiting"
    cut(h, lives[0], 0, FIRST, 0.2)
    assert hand(h, "forge") == [f"{FIRST} {SECOND}"] and len(agent_lines(h)) == 1, "not said, and no line of it"


async def test_a_sentence_well_under_way_still_yields_only_to_talk_that_lasts():
    h, backend, lives, clock, row = await answering()
    assert h.may_say("forge", lives[0].order, 0)
    sounds(h, lives[0])
    clock.t += h_mod.PREROLL_S + 0.1
    talks(h, P2)
    assert ("interrupt",) not in lives[0].calls and [u.state for u in h._ledger] == ["speaking"]
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    assert ("interrupt",) in lives[0].calls


async def test_the_window_is_counted_from_the_first_sound_not_from_permission_or_a_silent_frame():
    """10-08 review: the stamp was the permission, and the frames after it may be silent for a second."""
    h, backend, lives, clock, row = await answering()
    assert h.may_say("forge", lives[0].order, 0)
    clock.t += 2 * h_mod.PREROLL_S  # quiet frames: the room has heard nothing of it
    sounds(h, lives[0])
    clock.t += h_mod.PREROLL_S - 0.1
    talks(h, P2)
    assert ("interrupt",) in lives[0].calls, "its sound began as they did, whenever it was permitted"


async def test_a_sentence_permitted_and_not_yet_audible_yields_at_once_too():
    h, backend, lives, clock, row = await answering()
    assert h.may_say("forge", lives[0].order, 0)
    clock.t += 2 * h_mod.PREROLL_S  # still only quiet frames
    talks(h, P2)
    assert ("interrupt",) in lives[0].calls and [u.state for u in h._ledger] == ["paused"], "nothing of it was heard"


async def test_a_sentence_only_just_begun_does_not_yield_to_a_listening_sound():
    """10-08 review: the pre-roll yield fired for a person whose words were only "mm-hm"."""
    h, backend, lives, clock, row = await answering()
    assert h.may_say("forge", lives[0].order, 0)
    sounds(h, lives[0])
    h.on_speech(P2, True)  # the detector hears him, and his first words come before the SFU's sound of him
    h.on_interim_transcript(P2, "Dan", "Mm-hm")
    assert h._only_listening(P2)
    clock.t += h_mod.PREROLL_S - 0.1
    h.on_active_speakers({P2})  # now he is talking, inside the window
    assert ("interrupt",) not in lives[0].calls and [u.state for u in h._ledger] == ["speaking"]
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    assert ("interrupt",) not in lives[0].calls, "nor later, as for any listening sound"
    h.on_interim_transcript(P2, "Dan", "Mm-hm, but wait")
    assert ("interrupt",) in lives[0].calls, "words that say more make it yield"


# -- an exchange is its person's until someone else takes a turn --


async def answered():
    """Forge answered Priya, its voice has closed, and nothing is owed."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", FIRST)
    for unit in (FIRST,):
        h.on_agent_said("forge", unit)
    await closes(h, clock)
    return h, backend, lives, clock


async def test_silence_alone_ends_no_exchange_and_the_nameless_follow_up_goes_to_the_router_at_once():
    h, backend, lives, clock = await answered()
    clock.t += 60  # nobody said a word for a minute
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes + 1, "no UNCLAIMED_QUESTION_S of waiting: /route reads it now"
    (evidence,) = backend.route_bodies[-1]["engagements"]
    assert (evidence["agentId"], evidence["state"]) == ("forge", "active")


AMONG_PEOPLE = {"ops": {"requests": [], "social": None, "more": False, "cancel": [], "steer": [], "people": True}}


async def test_someone_elses_turn_among_people_ends_the_exchange_with_no_gap_to_wait_out():
    """10-08 review: the rule read "until someone else takes a turn", and the code still wanted 30 s too."""
    h, backend, lives, clock = await answered()
    backend.route_extra = AMONG_PEOPLE
    await says(h, P2, "Lee, I will send you the plan on Thursday.")
    backend.route_extra = ops([])
    assert h._engagements() == [] and h._engaged == {}, "at once: it was not part of her exchange with Forge"
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes and h._unclaimed is not None, "a question to the room again"


async def test_someone_elses_turn_is_no_end_while_what_it_was_is_not_known():
    h, backend, lives, clock = await answered()
    backend.route_extra, backend.route_gate = AMONG_PEOPLE, asyncio.Event()
    asyncio.ensure_future(h.on_final_transcript(P2, "Dan", "Lee, I will send you the plan on Thursday."))
    for _ in range(20):
        await asyncio.sleep(0)
    clock.t += 60
    assert [e.agent for e in h._engagements(h._who(P1))] == ["forge"], "/route has not said what it was"
    backend.route_gate.set()
    await settle(h)
    assert h._engagements() == []


async def test_someone_elses_turn_that_is_part_of_the_exchange_ends_nothing_however_long_ago():
    h, backend, lives, clock = await answered()
    await says(h, P2, "Good question, I wondered too.")  # /route: about the request, said to no person
    backend.route_extra = ops([("forge", "which PR is the oldest", "name")])
    await says(h, P2, "Forge, which of them is the oldest?")  # and a line to the same agent
    backend.route_extra = ops([])
    clock.t += 60
    assert {e.person[0] for e in h._engagements()} == {P1, P2}, "hers is still open, and he has one too"


async def test_someone_elses_turn_among_people_suspends_an_exchange_whose_answer_is_still_to_come():
    h, backend, lives, clock = await asked()
    backend.route_extra = AMONG_PEOPLE
    await says(h, P2, "Lee, I will send you the plan on Thursday.")
    clock.t += 60
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.why) == ("suspended", "other"), "the answer is owed: it waits, the exchange is not forgotten"


async def test_a_listening_sound_from_someone_else_is_no_turn_and_ends_nothing():
    h, backend, lives, clock = await answered()
    await says(h, P2, "Mm-hm.")
    clock.t += 60
    assert [e.agent for e in h._engagements(h._who(P1))] == ["forge"]


async def test_the_safety_net_ends_an_exchange_nobody_ended():
    h, backend, lives, clock = await answered()
    (e,) = h._engagements(h._who(P1))
    clock.t = e.at + h_mod.ENGAGE_SAFETY_S - 1
    assert [e.agent for e in h._engagements(h._who(P1))] == ["forge"]
    clock.t += 1
    assert h._engagements() == [], "only so that a record nobody closes is not kept for the whole call"


# -- small talk for a line that names the agent is written while /route decides --


async def named(text, route):
    """Priya says `text` to Forge by name; /route is still deciding. (h, backend, lives, the writer's calls)"""
    h, room, backend, lives, clock, log = await per_person()
    written = []

    async def writer(persona, event):
        written.append(event)
        return "Hi, Priya."

    h._writer = writer
    backend.route_extra, backend.route_gate = route, asyncio.Event()
    asyncio.ensure_future(h.on_final_transcript(P1, "Priya", text))
    for _ in range(20):
        await asyncio.sleep(0)
    return h, backend, lives, written


async def test_a_reply_to_a_greeting_by_name_is_being_written_before_the_router_has_answered():
    h, backend, lives, written = await named("Hi Forge.", {"ops": {"requests": [], "social": "forge", "more": False}})
    assert len(written) == 1 and "Hi Forge." in written[0], "the writer was asked while /route decides"
    backend.route_gate.set()
    await settle(h)
    assert len(written) == 1 and says_of(lives[0]) == [("say", "Hi, Priya.")], "that text is the reply: written once"


async def test_the_text_written_for_a_named_line_that_was_a_request_is_thrown_away():
    h, backend, lives, written = await named("Forge, what PRs are open?", ops([("forge", "list the open PRs", "name")]))
    assert len(written) == 1
    backend.route_gate.set()
    await settle(h)
    assert [u.kind for u in h._ledger] == ["ack"] and h._voice.writing is None
    assert len(says_of(lives[0])) == 1 and says_of(lives[0])[0][1] in h_mod.ACKS, "only its acknowledgement is said"


async def test_a_line_that_names_the_agent_whose_voice_is_open_is_written_for_beside_the_router_too():
    h, backend, lives, clock = await asked()
    written = []

    async def writer(persona, event):
        written.append(event)
        return "You're welcome."

    h._writer = writer
    backend.route_extra, backend.route_gate = {"ops": {"requests": [], "social": "forge", "more": False}}, asyncio.Event()
    asyncio.ensure_future(h.on_final_transcript(P1, "Priya", "Thanks, Forge."))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(written) == 1, "while /route decides"
    backend.route_gate.set()
    await settle(h)
    assert len(written) == 1 and says_of(lives[0])[-1] == ("say", "You're welcome.")


# -- the detector alone (the SFU has not heard them yet) holds what has not begun, and stops nothing --


async def test_speech_only_the_detector_hears_holds_a_new_answer_and_the_next_sentence():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    h.on_speech(P2, True)  # the SFU's sound of him has not come
    assert h._speaking == set(), "not talk yet: the SFU has not heard him"
    answer(h, row.run_id, "forge", f"{FIRST} {SECOND}")
    await pause(h, clock, BREATH)
    assert len(says_of(lives[0])) == 1 and h._hold(h._ledger[0], clock.t)[0] == "person", "nothing new starts"
    h.on_speech(P2, False)
    await says(h, P2, "Mm-hm.")
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[1:] == [("say", f"{FIRST} {SECOND}")]
    h.on_unit("forge", lives[0].order, 0, FIRST, "said", {"loudMs": 1200})
    h.on_speech(P2, True)
    assert not h.may_say("forge", lives[0].order, 1), "nor the next sentence of one being said"


async def test_speech_only_the_detector_hears_does_not_stop_a_sentence_being_said():
    h, backend, lives, clock, row = await answering()
    h.on_speech(P2, True)
    clock.t += 2 * h_mod.TALK_OVER_S
    await h.tick()
    assert ("interrupt",) not in lives[0].calls and [u.state for u in h._ledger] == ["speaking"]
    h.on_active_speakers({P2})  # the SFU hears him too: it is talk, and it has lasted
    await h.tick()
    assert ("interrupt",) in lives[0].calls


async def test_sound_only_the_sfu_hears_holds_nothing():
    """Tried the other way round (10-08, replay, 200 seeds x 15 scenarios, `--noise 0.1`): the SFU's level
    alone holding a new unit for PREROLL_S. Without noise nothing improved (1587 -> 1589 failing check-seeds,
    749 -> 751 with the fast transcriber). With a burst every 10 s on each silent track it held 4,096 starts
    and acknowledgements past 4 s went 720 -> 848 (75 -> 107). Not done."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    h.on_active_speakers({P2})  # a cough, a key: the detector hears no speech
    answer(h, row.run_id, "forge", FIRST)
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[1:] == [("say", FIRST)] and h._turn_open(clock.t) is None


async def test_a_detector_that_never_says_stop_holds_speech_for_detector_lost_s_at_most():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    h.on_speech(P2, True)
    answer(h, row.run_id, "forge", FIRST)
    await pause(h, clock, h_mod.DETECTOR_LOST_S - 1)
    assert len(says_of(lives[0])) == 1
    await pause(h, clock, 1 + h_mod.MEET_QUIET_S)
    assert says_of(lives[0])[1:] == [("say", FIRST)], "its stop was lost, and the SFU never heard him: not speech"


# -- a follow-up said while its request is still being worked on goes into that run: it is acknowledged  --
# -- aloud, it is answered whatever the run can still take, and one line goes into the run once           --


def steers(backend):
    return [(t["steerRunId"], t["utterance"]) for t in backend.late_steers]


async def test_a_follow_up_added_to_a_working_request_is_acknowledged_aloud():
    """10-08 Meet: "Friday, what about Thursday?" went into the calendar run, and the person heard nothing."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.steer, said = True, len(says_of(lives[0]))
    backend.route_extra = ops(steer=[(row.id, "only the ones on main")])
    await says(h, P1, "Forge, only the ones on main.")
    assert steers(backend) == [(row.run_id, "only the ones on main")] and len(h._requests.items) == 1
    (line,) = says_of(lives[0])[said:]
    assert line[1] in h_mod.STEER_ACKS, "one short fixed line, at once, and no new run"
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.expects, e.obligation) == ("active", "answer", row.id), "they wait for that answer again"


async def test_a_follow_up_brings_its_speaker_back_to_the_agent_they_had_turned_away_from():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Dan, did you push that fix last night?")
    assert h._engagements(h._who(P1))[0].state == "suspended"
    backend.steer, said = True, len(says_of(lives[0]))
    backend.route_extra = ops(steer=[(row.id, "only the ones on main")])
    await says(h, P1, "Forge, only the ones on main.")
    assert [c[1] in h_mod.STEER_ACKS for c in says_of(lives[0])[said:]] == [True]
    assert h._engagements(h._who(P1))[0].state == "active"


async def test_a_follow_up_the_run_can_no_longer_take_becomes_a_request_of_its_own():
    """The bridge refuses a steer once a run has stopped taking them: the words are never lost."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.steer = False  # the run is finishing
    backend.route_extra = ops(steer=[(row.id, "only the ones on main")])
    await says(h, P1, "Forge, only the ones on main.")
    first, second = h._requests.items
    assert (second.agent, second.task, second.asker) == ("forge", "only the ones on main", P1)
    assert second.status == "queued", "behind the run it could not join: one run per agent at a time"
    answer(h, first.run_id, "forge", FIRST)
    await settle(h)
    assert second.status == "working" and backend.turns[-1]["utterance"] == "only the ones on main"


async def test_a_question_routed_at_once_and_taken_is_not_routed_again_as_unclaimed():
    """10-08 Meet: "And what are they called?" was steered twice. Its speaker was talking with the agent, so
    it went to /route at once, and as a question that named nobody it also armed the unclaimed-question
    timer, which sent it again 3.5 s later."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.steer = True
    backend.route_extra = ops(steer=[(row.id, "which is the oldest")])
    await closes(h, clock)  # its voice has closed: the nameless line is not a floor line
    routes = len(backend.routes)
    await says(h, P1, "And which is the oldest?")
    assert len(backend.routes) == routes + 1 and steers(backend) == [(row.run_id, "which is the oldest")]
    assert h._unclaimed is None, "an agent took it: the room is not asked"
    quiet(h)
    await pause(h, clock, h_mod.UNCLAIMED_QUESTION_S + 1)
    assert len(backend.routes) == routes + 1 and len(steers(backend)) == 1


async def test_a_question_routed_at_once_that_nobody_took_is_still_put_to_the_room():
    h, backend, lives, clock = await asked()
    await closes(h, clock)
    backend.route_extra = ops([])  # /route gives it to nobody
    routes = len(backend.routes)
    await says(h, P1, "And which is the oldest?")
    assert len(backend.routes) == routes + 1 and h._unclaimed is not None
    quiet(h)
    await pause(h, clock, h_mod.UNCLAIMED_QUESTION_S + 1)
    assert len(backend.routes) == routes + 2 and backend.route_bodies[-1]["unclaimed"] is True


async def test_one_line_steers_a_request_once_however_often_its_answer_is_applied():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.steer = True
    took = ops(steer=[(row.id, "which is the oldest")])["ops"]
    mark = h._final_mark[P1]
    h._apply(P1, mark, took)
    h._apply(P1, mark, took)  # the same line's answer again (a second /route of it that was already on its way)
    await settle(h)
    assert len(steers(backend)) == 1
    h._apply(P1, mark + 1, took)  # another line that says the same is another steer
    await settle(h)
    assert len(steers(backend)) == 2


# -- a run that is slow, or never ends: its asker is told once that it is still working, and once that --
# -- it could not be done. Nothing is said into a meeting that has moved on                             --


def lines(lives):
    """Everything the mouths were given to say, whichever voice was open for it."""
    return [c[1] for live in lives for c in says_of(live)]


async def wait(h, clock, seconds, lives=()):
    """Time passes a tick at a time, as it does on a call, and whatever a mouth is handed it says."""
    for _ in range(round(seconds)):
        clock.t += 1
        await h.tick()
        await settle(h)
        for live in lives:
            spoken(h, live)


async def test_a_request_that_has_run_long_with_nothing_to_show_gets_one_still_working_line():
    """10-08 Meet: runs that took 45-110 s left their askers with an acknowledgement and then silence."""
    h, backend, lives, clock = await asked()
    said = len(lines(lives))
    await wait(h, clock, h_mod.RUN_STATUS_S - h_mod.MEET_QUIET_S - 2, lives)
    assert lines(lives)[said:] == [], "not before the bound"
    await wait(h, clock, 4, lives)
    (line,) = lines(lives)[said:]
    assert line in h_mod.STILL_WORKING
    await wait(h, clock, 4 * h_mod.RUN_STATUS_S, lives)  # ...and never a second time, however long the run takes
    assert lines(lives)[said:] == [line]


async def test_no_still_working_line_is_said_into_a_meeting_that_has_moved_on():
    h, backend, lives, clock = await asked()
    said = len(lines(lives))
    backend.route_extra = AMONG_PEOPLE
    await says(h, P2, "Did anyone hear back from the vendor?")  # someone else took a turn since the request
    quiet(h)
    await wait(h, clock, 3 * h_mod.RUN_STATUS_S, lives)
    assert lines(lives)[said:] == []
    # the asker turned to a colleague themselves: the same
    h, backend, lives, clock = await asked()
    said = len(lines(lives))
    await says(h, P1, "Dan, did you push that fix last night?")
    quiet(h)
    await wait(h, clock, 3 * h_mod.RUN_STATUS_S, lives)
    assert lines(lives)[said:] == []
    # someone else's turn that /route could not place: the asker's exchange still stands, and still the
    # room is not theirs alone any more
    h, backend, lives, clock = await asked()
    said = len(lines(lives))
    backend.route_extra = ops([])
    await says(h, P2, "Hm, the vendor thing.")
    quiet(h)
    assert h._engagements(h._who(P1))[0].state == "active"
    await wait(h, clock, 3 * h_mod.RUN_STATUS_S, lives)
    assert lines(lives)[said:] == []


async def test_no_still_working_line_while_someone_is_talking():
    h, backend, lives, clock = await asked()
    said = len(lines(lives))
    talks(h, P1)  # its own asker is saying something
    await wait(h, clock, h_mod.RUN_STATUS_S + 5, lives)
    assert lines(lives)[said:] == []


async def test_a_run_whose_frames_keep_coming_is_not_called_silent():
    """The bound is on the run saying nothing: a tool starting is the run at work."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    said = len(lines(lives))
    for i in range(4):
        await wait(h, clock, h_mod.RUN_STATUS_S - 5, lives)
        h.on_data(None, "call.frame", packet(frame(row.run_id, "forge", {"type": "tool_start", "tool": f"t{i}", "seq": i + 1})))
    assert lines(lives)[said:] == []


async def test_asked_how_it_stands_the_agent_does_not_say_it_again_unasked():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    said = len(lines(lives))
    status = {"agentId": "forge", "kind": "status", "task": row.task, "text": None}
    backend.route_extra = {"agentId": None, "agentIds": [], "ops": {**ops([])["ops"], "called": "forge", "say": status}}
    await says(h, P1, "Forge?")
    quiet(h)
    await wait(h, clock, h_mod.MEET_QUIET_S, lives)
    (line,) = lines(lives)[said:]
    assert line in h_mod.STILL_WORKING
    await wait(h, clock, 3 * h_mod.RUN_STATUS_S, lives)
    assert lines(lives)[said:] == [line]


def sorry(lives):
    return [t for t in lines(lives) if t in h_mod.COULD_NOT]


async def given_up():
    """Forge's first request never ended, and a second one of Priya's was in line behind it."""
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "which is the oldest", "name")])
    await says(h, P1, "Forge, which is the oldest?")
    spoken(h, lives[0])
    first, second = h._requests.items
    assert (first.status, second.status) == ("working", "queued")
    quiet(h)
    await wait(h, clock, h_mod.RUN_DEADLINE_S - 10, lives)
    assert first.status == "working" and sorry(lives) == [], "not before the deadline"
    await wait(h, clock, 15, lives)
    return h, backend, lives, clock, first, second


async def test_a_run_with_no_end_by_the_deadline_is_given_up_once_and_its_asker_is_told():
    h, backend, lives, clock, first, second = await given_up()
    assert first.status == "failed", "closed as failed"
    assert second.status == "working", "the agent's next request starts: one that never ends holds nothing up"
    assert len(sorry(lives)) == 1, "told once"
    await wait(h, clock, h_mod.RUN_DEADLINE_S - 20, lives)
    assert len(sorry(lives)) == 1 and second.status == "working"
    assert backend.late_answers == [], "nothing is posted for it: no answer exists, and none is made up"


async def test_the_deadline_counts_from_when_turns_accepted_the_run_not_from_the_first_try():
    """A cold workspace took the run after 70 s of retries (TURN_RETRY_BUDGET is 75). Counted from the first
    try the run had 50 s to work in, and a healthy long run was called failed before its answer came."""
    from huddle_agent.backend import BackendError

    h, room, backend, lives, clock, log = await per_person()
    backend.turn_results = [BackendError(503, "runtime_unavailable", None)] * 10  # 2+4+8*8 = 70 s of backoff
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    asked_at = clock.t
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = ops([])
    (row,) = h._requests.items
    run = h._runs.get(row.run_id)
    assert len(backend.turns) == 11 and row.status == "working"
    assert run.accepted - asked_at >= 70, "the wake took most of the retry budget"
    quiet(h)
    await wait(h, clock, h_mod.RUN_DEADLINE_S - (clock.t - run.accepted) - 2, lives)
    assert clock.t - asked_at > h_mod.RUN_DEADLINE_S + 60, "well past a deadline counted from the request"
    assert row.status == "working" and sorry(lives) == [], "the run still has its whole time to work"
    answered_run = row.run_id
    await wait(h, clock, 4, lives)
    assert row.status == "failed" and len(sorry(lives)) == 1, "120 s after acceptance it is given up"
    said = lines(lives)
    answer(h, answered_run, "forge", FIRST)
    await wait(h, clock, 5, lives)
    assert lines(lives) == said, "an answer after the apology is never said"
    assert [(a[1], a[3]) for a in backend.late_answers] == [(answered_run, FIRST)], "it is written to the room"


async def test_the_deadline_does_not_run_while_a_run_is_queued_behind_a_chat_run():
    """10-09 review: a chat run held the room lock (`run_queued`, waited for up to QUEUED_TURN_SECONDS), and
    the request was failed and apologised for 120 s after /turns took it, before it had begun."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    run_id, said = row.run_id, len(lines(lives))
    h.on_data(None, "call.frame", packet(frame(run_id, "forge", {"type": "run_queued", "seq": 1})))
    await wait(h, clock, 3 * h_mod.RUN_DEADLINE_S, lives)
    assert row.status == "working" and sorry(lives) == [], "queued is not executing: no deadline runs"
    (line,) = lines(lives)[said:]
    assert line in h_mod.QUEUED_LINES, "its asker is told once that it is in line, never that it is being worked on"
    h.on_data(None, "call.frame", packet(frame(run_id, "forge", {"type": "tool_start", "tool": "t", "seq": 2})))
    left_queue = clock.t
    await wait(h, clock, h_mod.RUN_DEADLINE_S - 10, lives)
    assert row.status == "working" and sorry(lives) == [], "its whole time to work, from when it left the queue"
    assert lines(lives)[said:] == [line], "and no second status line"
    await wait(h, clock, 15, lives)
    assert clock.t - left_queue >= h_mod.RUN_DEADLINE_S
    assert row.status == "failed" and len(sorry(lives)) == 1, "a run that then never ends is still given up"


async def test_an_answer_that_comes_after_the_apology_is_written_to_the_room_and_not_said():
    h, backend, lives, clock, first, second = await given_up()
    said = lines(lives)
    answer(h, first.run_id, "forge", FIRST)
    await wait(h, clock, 5, lives)
    assert lines(lives) == said, "not said: it would take back an apology minutes old"
    assert [(a[1], a[3]) for a in backend.late_answers] == [(first.run_id, FIRST)]


async def test_an_answer_that_comes_before_the_apology_was_said_is_said_in_its_place():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    talks(h, P2)  # people are talking as the deadline passes: the apology waits for them
    await wait(h, clock, h_mod.RUN_DEADLINE_S + 1, lives)
    assert row.status == "failed" and sorry(lives) == []
    assert [" ".join(u.units) for u in h._ledger if u.kind == "answer"][0] in h_mod.COULD_NOT
    answer(h, row.run_id, "forge", FIRST)
    assert hand(h, "forge") == [FIRST], "the answer took the apology's place"
    quiet(h)
    await wait(h, clock, 2 * h_mod.MEET_QUIET_S, lives)
    assert FIRST in lines(lives) and sorry(lives) == [] and backend.late_answers == []


def test_a_run_given_up_is_waited_for_no_longer_and_its_frames_are_still_read():
    from huddle_agent.frames import Runs

    class Sink:
        ended_with = None

        def progress(self, run, text): ...
        def say(self, run, text, final): ...
        def failed(self, run): ...
        def ended(self, run, answer):
            self.ended_with = answer

    sink, now = Sink(), [100.0]
    runs = Runs(sink, clock=lambda: now[0])
    run = runs.started("r1", "forge", speaker=P1, request="x", delegation_id=None, gen=None)
    assert run.accepted == 0.0 and runs.running("forge") == [run]  # /turns has not taken it: no deadline runs
    runs.give_up("r1")
    assert runs.running("forge") == []
    for f in ({"type": "chunk", "content": FIRST, "seq": 1}, {"type": "run_ended", "seq": 2}):
        runs.on_event(frame("r1", "forge", f))
    assert sink.ended_with == FIRST and run.gave_up


# -- where the time of a voice opening went is on one log line --


async def test_an_open_logs_how_long_the_window_the_session_the_router_and_the_voice_each_took(caplog):
    """10-08 Meet: "Hi Friday." was answered 4.7 s after the router had read it, and the logs could not say
    whether the window, the session or the voice had been the slow one."""
    import json

    with caplog.at_level("INFO", logger=h_mod.logger.name):
        h, backend, lives, clock = await asked()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("huddle open ")]
    took = json.loads(line.removeprefix("huddle open "))
    assert {"windowMs", "sessionMs", "routeMs", "voiceMs", "waitedMs", "totalMs"} <= set(took)
    assert (took["agent"], took["path"]) == ("forge", "name")
