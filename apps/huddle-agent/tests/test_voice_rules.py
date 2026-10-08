"""The general rules the replay loop (tests/replay) was used to settle, one test or two each: what a cut
sentence counts as, when a pause is counted from, what a listening sound does, and so on. Each was accepted
on the 15 replayed scenarios under two transcriber timings; these pin the rule itself."""

import asyncio

from huddle_agent import huddle as h_mod
from test_huddle import answer, hand, ops, pause, settle, talks
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
    clock.t += 2 * h_mod.ENGAGE_GAP_S  # nobody said a word
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes + 1, "no UNCLAIMED_QUESTION_S of waiting: /route reads it now"
    (evidence,) = backend.route_bodies[-1]["engagements"]
    assert (evidence["agentId"], evidence["state"]) == ("forge", "active")


async def test_someone_elses_turn_and_then_a_long_gap_end_the_exchange():
    h, backend, lives, clock = await answered()
    await says(h, P2, "I will send the plan on Thursday.")
    clock.t += h_mod.ENGAGE_GAP_S
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes and h._unclaimed is not None and h._engaged == {}


async def test_a_listening_sound_from_someone_else_is_no_turn_and_ends_nothing():
    h, backend, lives, clock = await answered()
    await says(h, P2, "Mm-hm.")
    clock.t += h_mod.ENGAGE_GAP_S
    assert [e.agent for e in h._engagements(h._who(P1))] == ["forge"]


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


async def test_a_detector_that_never_says_stop_holds_speech_for_detector_lost_s_at_most():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    h.on_speech(P2, True)
    answer(h, row.run_id, "forge", FIRST)
    await pause(h, clock, h_mod.DETECTOR_LOST_S - 1)
    assert len(says_of(lives[0])) == 1
    await pause(h, clock, 1 + h_mod.MEET_QUIET_S)
    assert says_of(lives[0])[1:] == [("say", FIRST)], "its stop was lost, and the SFU never heard him: not speech"
