"""The general rules the replay loop (tests/replay) was used to settle, one test or two each: what a cut
sentence counts as, when a pause is counted from, what a listening sound does, and so on. Each was accepted
on the 15 replayed scenarios under two transcriber timings; these pin the rule itself."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, hand, ops, pause, settle, talks
from test_meet_exchange import asked, quiet, says_of, talked_over
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


# -- a sentence that was cut: said or not said, by how much of it was audible --


async def test_a_sentence_cut_after_a_word_is_not_said_leaves_no_line_and_is_said_whole_later():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 0.3))
    assert FIRST not in agent_lines(h) and len(agent_lines(h)) == 1, "only its acknowledgement is on the record"
    assert hand(h, "forge") == [f"{FIRST} {SECOND}"], "the cursor did not move"
    await listening_sound(h, clock)
    assert says_of(lives[0])[2:] == [("say", f"{FIRST} {SECOND}")], "said whole, from the sentence it was cut in"


async def test_a_sentence_cut_near_its_end_is_said_and_the_record_shows_what_was_audible():
    h, backend, lives, clock, row = await answering()
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 0.9))
    assert agent_lines(h)[-1] == "Three PRs are", "what the room heard of it, to a whole word"
    assert hand(h, "forge") == [SECOND], "it counts as said: the answer goes on after it"
    await listening_sound(h, clock)
    assert says_of(lives[0])[2:] == [("say", SECOND)], "and it is not said again"


async def test_a_sentence_cut_after_its_last_word_is_on_the_record_whole():
    h, backend, lives, clock, row = await answering(FIRST)
    await talked_over(h, clock, P2)
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 1.2))  # the cut came in the silence after it
    assert agent_lines(h)[-1] == FIRST and h._ledger == [] and row.status == "delivered"


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
    cut(h, lives[0], 0, FIRST, seconds(FIRST, 0.9))
    await settle(h)
    assert says_of(lives[0])[2:] == [("say", SECOND)], "the receipt said the sentence was heard"


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
    assert len(says_of(lives[0])) == 1 and agent_lines(h) == [ack[0]]


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
