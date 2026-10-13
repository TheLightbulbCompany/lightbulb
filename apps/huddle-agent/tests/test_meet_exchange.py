"""A per-person Google Meet: the voice is a mouth with no ears (owner ruling 10-07), told what was said as text.
The worker gives and takes the floor: a name opens the exchange, the person turning to a colleague suspends
it, anyone who keeps talking over the mouth makes it yield, and what it had not said stays in the room's ledger
until the conversation pauses or someone turns to the agent."""

import asyncio

from huddle_agent import huddle as h_mod
from test_huddle import answer, hand, ops, pause, settle, spoken, talks
from test_meeting_mode import P1, P2, per_person, says


async def asked(acked=True):
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = ops([])
    if acked:
        spoken(h, lives[0])  # its acknowledgement is said, and the room is quiet: an answer may start
        clock.t += h_mod.MEET_QUIET_S
    return h, backend, lives, clock


def quiet(h):
    for who in (P1, P2):
        h.on_speech(who, False)
    h.on_active_speakers(set())


async def test_its_person_turning_to_a_colleague_suspends_the_exchange_and_the_unsaid_answer_waits_for_a_pause():
    """Its person turns to someone else: they are not waiting on the agent now. Nothing is closed or lost."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await talked_over(h, clock, P1)
    await says(h, P1, "Dan, did you push that fix last night?")
    assert len(backend.turns) == 1 and len(backend.routes) == 1, "the line was no request, and needed no router"
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.why) == ("suspended", "people")
    assert hand(h, "forge") == ["Three PRs are open."] and backend.late_answers == []
    quiet(h)
    await pause(h, clock, h_mod.SETTLE_S + 0.1)
    assert len(says_of(lives[0])) == 2, "its hand is up: a breath is not its opening"
    await pause(h, clock)
    assert says_of(lives[0])[-1] == ("say", "Three PRs are open.") and len(lives) == 1, "said at the pause"


async def test_its_own_person_who_keeps_talking_over_it_makes_it_yield_like_anyone():
    """It hears nobody, its asker included: only the worker can yield for it."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert ("say", "Three PRs are open.") in lives[0].calls
    h.on_agent_state("forge", "speaking")
    talks(h, P1)  # its own person
    clock.t += h_mod.TALK_OVER_S - 0.1
    await h.tick()
    await settle(h)
    assert ("interrupt",) not in lives[0].calls, "not at the first sound"
    clock.t += 0.1
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is not None, "it yields, and its voice stays open"
    assert hand(h, "forge") == ["Three PRs are open."] and row.status == "ready", "paused, not lost"
    assert backend.late_answers == []


async def test_a_line_that_trails_off_waits_for_its_rest_and_is_one_line():
    """10-06 prod Meet: "Can one of you tell me..." was run as a request, and the agent asked what was meant."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Oh, and...")
    await says(h, P1, "Can one of you tell me...")
    assert backend.routes == [] and h._unclaimed is None
    await says(h, P1, "about my open tasks?")
    assert h._unclaimed is not None and h._unclaimed[2] == "Oh, and... Can one of you tell me... about my open tasks?"


async def test_a_name_in_the_middle_of_a_sentence_opens_the_exchange_when_nobody_has_the_floor():
    """10-06 prod Meet: "...all right, hey Forge, what PRs are open?" got nothing for 30 s."""
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Now let us think about how to test this. All right, hey Forge, what PRs are open?")
    assert len(lives) == 1 and lives[0].agent_id == "forge" and len(backend.turns) == 1


async def test_an_answer_talked_over_by_someone_else_stays_to_be_said_and_does_not_go_to_the_record():
    """Rig 10-07: Leonardo's "oh wait, sorry" over the announcement sent the answer to the record (late-answer)."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await closes(h, clock)  # the exchange's voice closed while the run worked
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce", "a voice opens for it: its asker is waiting"
    await talked_over(h, clock, P2)  # someone else talks over it, and keeps talking
    assert ("interrupt",) in lives[-1].calls
    assert hand(h, "forge") == ["Three PRs are open."] and backend.late_answers == []
    quiet(h)
    await pause(h, clock, h_mod.FINAL_WAIT_S + h_mod.MEET_QUIET_S)
    assert says_of(lives[-1]) == [("say", "Three PRs are open.")] * 2 and len(lives) == 2, "said whole at the pause"


async def test_a_talked_over_answer_comes_back_from_the_sentence_it_was_cut_on():
    """Owner 10-07: per sentence, so whichever it was interrupted on can be said."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old. One of them is a draft.")
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")  # heard to its end
    h.on_agent_said("forge", "The oldest is two days old.", interrupted=True)  # cut in this one
    await talked_over(h, clock, P2)
    assert hand(h, "forge") == ["The oldest is two days old. One of them is a draft."]


async def test_a_follow_up_from_its_person_reaches_the_router_and_is_given_to_the_voice_as_text():
    """It heard none of it: /route understands the floor line, its run starts, and the voice acknowledges it
    with the worker's next fixed line. No model is asked for one."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    spoken(h, lives[0])
    before, routes, turns = len(lives[0].calls), len(backend.routes), len(backend.turns)
    backend.route_extra = ops([("forge", "the closed PRs too", "follow")])
    await says(h, P1, "and the closed ones too")
    assert len(backend.routes) == routes + 1 and backend.route_bodies[-1]["floor"]["mine"] is True
    assert len(backend.turns) == turns + 1, "its run is started by the worker"
    after = lives[0].calls[before:]
    assert [c for c in after if c[0] != "thinking"] == [("say", h_mod.ACKS[1])], after


async def test_small_talk_from_its_person_is_written_from_the_line_and_who_said_it_and_said_word_for_word():
    h, backend, lives, clock = await asked()
    before, written = len(lives[0].calls), []

    async def writer(persona, event):
        written.append((persona, event))
        return "Glad it helps."

    h._writer = writer
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": []}}
    await says(h, P1, "thanks, that is great")
    assert written == [("You are Forge.", 'Priya just said to you: "thanks, that is great"')], "the line and who said it"
    assert lives[0].calls[before:] == [("say", "Glad it helps.")], "no model of the voice is asked for it"


async def test_small_talk_is_written_while_the_voice_opens_and_said_only_once_nobody_is_talking():
    """10-07 rig: "Hi Friday." got its reply 5.3 s later, 2 s of it the writer, started after the session opened."""
    h, room, backend, lives, clock, log = await per_person()
    order = []

    async def writer(persona, event):
        order.append("write")
        return "Hello Priya."

    h._writer, h.gate["live"] = writer, asyncio.Event()
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": []}}
    task = asyncio.create_task(says(h, P1, "Hi Forge."))
    for _ in range(20):
        await asyncio.sleep(0)
    assert order == ["write"] and lives == [], "written before the session has opened"
    talks(h, P2)  # someone starts talking while it opens
    h.gate["live"].set()
    await task
    assert says_of(lives[0]) == [], "the line is ready and waits: nothing starts over a person"
    quiet(h)
    await says(h, P2, "Mm-hm.")  # what they said is known, and it asked nothing: their turn is over
    await pause(h, clock, h_mod.SETTLE_S + 0.1)  # a breath after the talk ended, not a real pause
    assert says_of(lives[0]) == [("say", "Hello Priya.")]
    assert backend.turns == [], "small talk is no run"




def test_opens_with_person_needs_a_word_said_to_them_not_just_a_name_that_is_also_a_word():
    """Codex on #3792: "Will we ship today?" with a Will in the meeting is not a handoff to Will."""
    people = [{"name": "Will Smith"}, {"name": "Leo"}]
    assert h_mod.opens_with_person("Leo, did you push that?", people)
    assert h_mod.opens_with_person("Leo did you push that", people)
    assert h_mod.opens_with_person("hey Will, can you hear me?", people)
    assert not h_mod.opens_with_person("Will we ship today?", people)
    assert not h_mod.opens_with_person("Leo pushed it last night.", people)


async def test_a_trailing_piece_stays_on_the_record_even_if_its_rest_never_comes():
    """Codex on #3792: a held fragment was dropped from the transcript when the speaker moved on."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Oh, and...")
    assert "Oh, and..." in h._transcript.text()
    clock.t += h_mod.TRAIL_S + 1
    await says(h, P1, "Can one of you tell me about my open tasks?")
    assert h._unclaimed is not None and h._unclaimed[2] == "Can one of you tell me about my open tasks?", "too late to join"
    assert h._transcript.text().count("Oh, and...") == 1


async def announced(h, backend, lives, clock, text="Three PRs are open. The oldest is two days old."):
    """Forge's exchange closed while its run worked; the answer came; its announcement is being said."""
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", text)
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce"
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")
    return row


async def test_its_own_person_interrupting_an_answer_keeps_the_rest_and_it_is_said_at_the_next_opening():
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock)
    await talked_over(h, clock, P1)  # its own person, whose words never come (noise)
    assert ("interrupt",) in lives[-1].calls
    assert hand(h, "forge") == ["The oldest is two days old."], "the rest, from the cut sentence"
    await next_pause(h, clock)
    assert says_of(lives[-1])[-1] == ("say", "The oldest is two days old."), "only the rest"


async def test_a_joined_line_records_only_its_new_piece():
    """Codex on #3792: the fragment and then the whole joined line were both on the record."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Oh, and...")
    await says(h, P1, "what shipped?")
    text = h._transcript.text()
    assert text.count("Oh, and...") == 1 and "what shipped?" in text and "Oh, and... what shipped?" not in text
    assert h._unclaimed is not None and h._unclaimed[2] == "Oh, and... what shipped?", "understood as one line"


async def test_a_hand_raised_after_the_same_words_were_said_is_not_let_go():
    """Codex on #3792: an earlier answer's words must not count for a later hand."""
    h, backend, lives, clock = await asked()
    h.on_agent_said("forge", "No PRs are open.")  # an earlier answer
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    clock.t += 1
    talks(h, P2)  # the room is talking when it lands: its hand goes up
    answer(h, row.run_id, "forge", "No PRs are open.")  # the same words, raised later
    await h.tick()
    await settle(h)
    assert hand(h, "forge") == ["No PRs are open."]


async def test_a_stop_that_trails_off_is_a_stop_not_a_fragment():
    """Codex on #3792: "Forge, stop..." held as a fragment was never applied."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Forge, stop...")
    # applied: Forge is silenced. It was saying nothing, and that does not withdraw its work.
    assert h._hushed(row.id) and row.status == "working" and backend.aborts == []



async def test_a_trailing_fragment_still_claims_an_unclaimed_question():
    """Codex on #3792: "Well..." from a person answering a question nobody claimed must count as someone
    answering, or the question is routed to an agent over them."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "what did we ship last week?")
    assert h._unclaimed is not None
    await says(h, P2, "Well...")
    assert h._unclaimed is None and h._last_speaker == P2


async def test_an_answer_cut_before_its_person_turned_away_is_kept_once_and_never_posted():
    """Codex on #3792: the hand took the answer, then the close kept the same answer a second time as cut, so
    the room got it posted while the hand still announced it."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "speaking")
    await talked_over(h, clock, P1)
    h.on_agent_said("forge", "Three PRs are open.", interrupted=True)  # their sound cut its first sentence
    await says(h, P1, "Dan, did you push that fix last night?")
    await settle(h)
    assert hand(h, "forge") == ["Three PRs are open. The oldest is two days old."]
    quiet(h)
    clock.t += 10.0 + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET  # (10 s: the old cut-answer wait)
    await h.tick()
    await settle(h)
    assert backend.late_answers == [] and len(h._ledger) == 1 and h._ledger[0].state == "speaking"


def test_a_line_about_a_colleague_is_not_a_handoff_to_them():
    """Codex on #3792: "Leo is pushing the fix" said to the agent closed its voice as a turn to Leo."""
    people = [{"name": "Leo Park"}, {"name": "Dan"}]
    for line in ("Leo is pushing the fix", "Dan has the details", "Dan will push it tonight", "Dan can push it"):
        assert not h_mod.opens_with_person(line, people), line
    for line in ("Dan, is it ready?", "Dan will you push it?", "Dan what do you think?", "Leo you there?",
                 "Dan please push it", "Leo did you push that fix?", "Dan are the PRs open?", "Dan is it ready?"):
        assert h_mod.opens_with_person(line, people), line
    # a bare imperative has no mark the worker can read; the voice's own meeting rule covers it
    assert not h_mod.opens_with_person("Dan fix the deployment", people)


async def test_a_stop_after_a_trailing_fragment_is_still_a_stop():
    """Codex on #3792: "Oh, and..." then "Forge, stop..." was joined into one trailing line and never applied."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Oh, and...")
    await says(h, P1, "Forge, stop...")
    assert h._hushed(row.id) and row.status == "working" and backend.aborts == []




async def test_an_answer_whose_person_turns_away_before_its_first_word_is_not_lost():
    """Codex on #3792 (after the merge): a participant called on Forge's hand, the seed consumed the news, and the
    voice closed before a word (they turned to a colleague), so the answer was gone."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await closes(h, clock)
    talks(h, P2)  # the room is talking when it lands
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert hand(h, "forge") == ["Three PRs are open."] and h._voice is None
    await says(h, P1, "Forge, go ahead.")  # called on: its voice opens. Dan is still talking
    assert h._voice is not None and says_of(lives[-1]) == []
    await says(h, P1, "Dan, did you push that fix last night?")  # before its first word
    assert hand(h, "forge") == ["Three PRs are open."] and backend.late_answers == []
    quiet(h)
    await pause(h, clock)
    assert says_of(lives[-1]) == [("say", "Three PRs are open.")]


async def test_an_answer_said_to_its_end_stays_said_when_a_later_answer_arrives():
    """Codex on #3800 (after the merge): a named voice said its seeded news, a second run's answer arrived while
    it still talked and reset `v.said`, and a turn-away then raised the said news again."""
    h, backend, lives, clock = await asked()
    (row1,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    backend.route_extra = ops([])
    spoken(h, lives[0])
    row2 = next(r for r in h._requests.items if r is not row1)
    answer(h, row1.run_id, "forge", "Three PRs are open.")
    h.on_agent_said("forge", "Three PRs are open.")  # said to its end
    await settle(h)  # its next request starts
    talks(h, P1)
    answer(h, row2.run_id, "forge", "The deploy is green.")  # a second answer lands as she starts to talk
    await says(h, P1, "Dan, did you push that fix last night?")  # turns away
    assert hand(h, "forge") == ["The deploy is green."] and row1.status == "delivered"


async def test_a_line_the_router_understood_as_said_to_a_person_suspends_the_exchange():
    """10-07 rig sales call: "Monthly, with an annual option. Maya, what is your current tool costing you?" kept
    the floor and the agent said its answer over Maya's reply. /route says who a line is for; no word pattern."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = {"ops": {"requests": [], "social": None, "more": False, "cancel": [], "steer": [], "people": True}}
    await says(h, P1, "Monthly, with an annual option. And what is your current tool costing you?")
    assert h._engagements(h._who(P1))[0].state == "suspended"
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, h_mod.SETTLE_S + 0.1)
    assert hand(h, "forge") == ["Three PRs are open."] and len(says_of(lives[0])) == 1, "its hand, not its voice"
    await pause(h, clock)
    assert says_of(lives[-1])[-1] == ("say", "Three PRs are open."), "at a pause in the conversation"


async def test_a_meeting_voice_that_names_a_person_stays_with_its_own_speaker():
    """10-07 rig sales call: "Maya, it is fifty dollars" moved its ears to the prospect and it ran the call."""
    h, backend, lives, clock = await asked()
    h.on_agent_said("forge", "Dan, it is fifty dollars a month.")
    assert h._voice is not None and h._voice.speaker == P1 and ("set_participant", P2) not in lives[0].calls


async def test_a_mouths_answer_cut_after_a_holding_line_ended_still_goes_to_its_hand():
    """10-07 rig sales call on the driven voice: the answer was ordered, the voice stopped speaking (its holding
    line had just ended), and that silence was taken for the answer said. Talked over two words in, no hand."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    h.on_agent_state("forge", "speaking")  # its acknowledgement
    h.on_agent_said("forge", "On it.")
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "listening")  # the acknowledgement's audio ends AFTER the answer was ordered
    assert ("say", "Three PRs are open. The oldest is two days old.") in lives[0].calls
    h.on_agent_state("forge", "speaking")  # the answer begins
    talks(h, P2)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls
    assert hand(h, "forge") == ["Three PRs are open. The oldest is two days old."]


async def test_a_mouths_answer_it_reported_said_leaves_no_hand():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")
    h.on_agent_state("forge", "listening")
    talks(h, P2)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert not hand(h, "forge")


async def test_called_on_while_its_work_runs_the_voice_says_what_it_is_still_working_on():
    """Codex on #3820: /route's `ops.say` status was read by nobody. A mouth says a fixed status line."""
    h, backend, lives, clock = await asked()
    clock.t += h_mod.MEET_FOLLOWUP_S  # its voice closed; the run still works
    await h.tick()
    await settle(h)
    assert h._voice is None
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": [],
                                   "people": False, "say": {"agentId": "forge", "kind": "status",
                                                            "task": "list the open PRs", "text": None}}}
    await says(h, P1, "Forge, go ahead.")
    assert len(backend.turns) == 1, "no new run"
    assert [c for c in lives[-1].calls if c[0] in ("say", "reply_to", "nudge", "commentary")] == [
        ("say", h_mod.STILL_WORKING[0])
    ]


# -- the delivery ledger: a mouth reports each sentence it said, and delivered means the last one --


def says_of(live):
    return [c for c in live.calls if c[0] == "say"]


async def talked_over(h, clock, who=P2):
    """Someone keeps talking over the mouth: it yields. Paused at its cursor, its voice still open."""
    talks(h, who)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert h._voice is not None and "paused" in [u.state for u in h._ledger]


async def closes(h, clock):
    """Its voice has said its piece and nobody follows up: it closes."""
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    assert h._voice is None


async def next_pause(h, clock):
    """The talk ends, the talker's words never come, and the conversation pauses."""
    quiet(h)
    clock.t += h_mod.FINAL_WAIT_S + h_mod.MEET_QUIET_S
    await h.tick()
    await settle(h)
    assert h._voice is not None


async def test_an_answer_cut_in_its_second_sentence_resumes_with_exactly_that_sentence():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")
    await talked_over(h, clock)
    h.on_agent_said("forge", "The oldest is two days old.", interrupted=True)  # its report lands after the yield
    assert row.status == "ready" and backend.late_answers == []
    assert hand(h, "forge") == ["The oldest is two days old."]
    assert h._transcript.text().count("Three PRs are open.") == 1
    assert "The oldest is two days old." not in h._transcript.text(), "a cut sentence was not said"
    await next_pause(h, clock)
    assert says_of(lives[-1])[-1] == ("say", "The oldest is two days old.") and len(lives) == 1, (
        "the first sentence is not said twice, and no voice was reopened for the rest"
    )
    h.on_agent_said("forge", "The oldest is two days old.")
    assert row.status == "delivered" and not hand(h, "forge")
    assert h._transcript.text().count("The oldest is two days old.") == 1


async def test_a_cut_first_sentence_is_said_again_whole():
    """10-07 rig: an answer cut after two words was taken for said."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.", interrupted=True)  # two words in
    await talked_over(h, clock)
    assert hand(h, "forge") == ["Three PRs are open. The oldest is two days old."]
    await next_pause(h, clock)
    assert says_of(lives[-1])[-2:] == [("say", "Three PRs are open. The oldest is two days old.")] * 2


async def test_an_answer_is_delivered_when_its_last_sentence_is_reported_said_not_when_it_is_ordered():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    assert row.status == "ready" and ("say", "Three PRs are open. The oldest is two days old.") in lives[0].calls
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")  # it went quiet (its acknowledgement ended): that says nothing
    assert row.status == "ready"
    h.on_agent_said("forge", "The oldest is two days old.")  # not the sentence it is on: counts for nothing
    h.on_agent_said("forge", "Three PRs are open.")
    assert row.status == "ready" and h._ledger[-1].cursor == 1
    h.on_agent_said("forge", "The oldest is two days old.")
    assert row.status == "delivered" and h._ledger == []
    await closes(h, clock)
    assert not hand(h, "forge"), "said to its end: no hand"


async def test_a_stop_said_to_a_speaking_agent_silences_the_answer_and_none_of_it_is_said_later():
    """10-07 rig: after a stop the rest of a cut answer was spoken."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")
    await says(h, P1, "Forge, stop.")
    assert ("interrupt",) in lives[0].calls and h._ledger == []
    h.on_agent_said("forge", "The oldest is two days old.", interrupted=True)
    h.on_agent_state("forge", "listening")
    await closes(h, clock)  # nothing was kept to say
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is None and not hand(h, "forge") and len(lives) == 1, "no voice opens to say the rest"
    # Asked for and not withdrawn: the room has it in writing, whole. Nothing was aborted.
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open. The oldest is two days old."]
    assert row.status == "delivered" and backend.aborts == []


async def test_the_acknowledgement_is_a_fixed_line_and_no_model_writes_it():
    h, backend, lives, clock = await asked(acked=False)
    (row,) = h._requests.items
    assert says_of(lives[0]) == [("say", h_mod.ACKS[0])] and "PRs" not in h_mod.ACKS[0], "the task is not read back"
    assert not [c for c in lives[0].calls if c[0] in ("nudge", "commentary", "reply_to")], "nothing asks a model"
    assert [(u.kind, u.obligation) for u in h._ledger] == [("ack", row.run_id)]
    spoken(h, lives[0])
    assert h._ledger == [] and row.status == "working", "a line is no delivery of the request"
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")  # it waits behind the first: a "next" line, cut by talk
    assert says_of(lives[0])[-1] == ("say", h_mod.QUEUED_LINES[0]) and [u.kind for u in h._ledger] == ["ack"]
    h.on_agent_state("forge", "speaking")
    await talked_over(h, clock)
    assert not hand(h, "forge"), "a line is not an answer"


def test_fixed_lines_do_not_repeat_within_a_call_and_none_reads_the_task_back():
    from test_huddle import make

    h = make()[0]
    assert len(h_mod.ACKS) >= 8 and len(h_mod.STILL_WORKING) >= 5
    assert len(h_mod.QUEUED_LINES) == 3 and len(h_mod.COULD_NOT) == 3
    for lines in (h_mod.ACKS, h_mod.STILL_WORKING, h_mod.QUEUED_LINES, h_mod.COULD_NOT, h_mod.HERE):
        # plain lines: fitting a request into a sentence took a word list, and still came out wrong
        # ("Sure, let me work on that: what is on my calendar tomorrow.", 10-07 rig)
        assert not [x for x in lines if "—" in x or "{" in x or x == "On it."]
        said = [h._line(lines) for _ in lines]
        assert len(set(said)) == len(lines), "each once before any twice"
    h2 = make()[0]
    h2._line_seed = 5  # another call starts somewhere else
    assert h2._line(h_mod.ACKS) == h_mod.ACKS[5]


async def test_an_answer_that_is_ready_is_said_and_no_status_line_with_it():
    """Called on as its answer lands: /route still says "status", and the answer is what is said."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await closes(h, clock)
    talks(h, P2)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    quiet(h)
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": [],
                                   "people": False, "say": {"agentId": "forge", "kind": "status",
                                                            "task": "list the open PRs", "text": None}}}
    await says(h, P1, "Forge, go ahead.")
    await pause(h, clock)
    assert says_of(lives[-1]) == [("say", "Three PRs are open.")]


async def test_a_named_line_the_router_calls_unfinished_asks_nothing_until_its_rest_comes():
    """10-09 prod Meet: "Hey, Friday. That's awesome, but can you actually give me a summary of" came back
    `hold` and was made a request all the same; its rest made two more. Unfinished means wait, also by name."""
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = {**ops([("forge", "give me a summary of", "name")]), "hold": True}
    await says(h, P1, "Forge, can you actually give me a summary of")
    assert h._requests.items == [] and backend.turns == [], "an unfinished turn starts nothing"
    assert all(("say",) != c[:1] for live in lives for c in live.calls), "and says nothing"
    backend.route_extra = ops([("forge", "summarise my open PRs", "name")])
    await says(h, P1, "my open PRs.")
    assert backend.routes[-1][1] == "Forge, can you actually give me a summary of my open PRs.", "routed once, whole"
    assert [r.task for r in h._requests.items] == ["summarise my open PRs"]


async def test_more_of_a_turn_that_already_asked_amends_its_request_and_is_not_acknowledged_again():
    """A piece that goes straight on from a routed turn is the same turn: routed again whole, its request is that
    row, amended. Not a second run, and not a second "on it"."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = True  # its run takes the words (a run that already ended makes a row of them: `_steer_row`)
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "summarise what my sessions are doing", "name")])
    await says(h, P1, "Forge, summarise what my sessions are doing.")
    (row,) = h._requests.items
    acks = [u for u in h._ledger if u.kind == "ack"]
    h.on_speech(P1, True)  # 0.5 s later they go on
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "the sessions of the whole workspace", "name")])
    await says(h, P1, "or the sessions of the workspace.")
    assert backend.routes[-1][1] == "Forge, summarise what my sessions are doing. or the sessions of the workspace."
    assert h._requests.items == [row] and row.status in h_mod.OPEN, "no second request"
    assert backend.late_steers[-1]["steerRunId"] == row.run_id, "the words went into its run"
    assert [u for u in h._ledger if u.kind == "ack"] == acks, "acknowledged once"
    clock.t += 5  # a later line, after a real pause, is a turn of its own
    h.on_speech(P1, True)
    clock.t += 1
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    assert len(h._requests.items) == 2


async def test_a_grown_turn_that_drops_an_agent_calls_that_agents_row_off():
    """#3961 review (P1): a first piece asked two agents; the whole turn asks one. The other's row is called off,
    not left to run and answer."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = True
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name"), ("gauge", "check the numbers", "name")])
    await says(h, P1, "Forge and Gauge, check the deploy")
    forge, gauge = sorted(h._requests.items, key=lambda r: r.agent)
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy and the numbers", "name")])
    await says(h, P1, "actually just Forge, both.")
    assert forge.status in h_mod.OPEN, "the agent it still asks keeps its row"
    assert gauge.status not in h_mod.OPEN, "the dropped agent's row is called off"
    assert len(h._requests.items) == 2, "no new row"


async def test_a_piece_said_while_the_turn_before_it_is_at_route_joins_that_turn():
    """#3961 review (P1): /route is slower than the pause. The first piece's `hold` comes back after the rest was
    heard; the rest is routed with the whole turn, and the late answer for the start alone is not acted on."""
    h, room, backend, lives, clock, log = await per_person()
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    slow = backend.route_gate = asyncio.Event()
    backend.route_extra = {**ops([("forge", "give me a summary of", "name")]), "hold": True}
    await h.on_final_transcript(P1, "Priya", "Forge, can you give me a summary of")
    for _ in range(30):
        await asyncio.sleep(0)
    backend.route_gate = None
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "summarise my open PRs", "name")])
    await h.on_final_transcript(P1, "Priya", "my open PRs.")
    for _ in range(30):
        await asyncio.sleep(0)
    slow.set()
    await settle(h)
    assert backend.routes[-1][1] == "Forge, can you give me a summary of my open PRs.", "the rest routed whole"
    assert [r.task for r in h._requests.items] == ["summarise my open PRs"], "one request, for the whole turn"
    assert P1 not in h._held, "the late hold is not kept to join a later line"


async def test_a_grown_turn_that_now_asks_nothing_calls_its_row_off():
    """#3961 review (P1): the first piece asked Forge; the whole turn is talk to a person. Its row is called off."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = True
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, check the deploy")
    (row,) = h._requests.items
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([])
    await says(h, P1, "actually no, Dan, what do you think?")
    assert row.status not in h_mod.OPEN, "taken back: its row is called off"


async def test_a_grown_turn_whose_first_answer_is_ready_asks_again_and_drops_that_answer():
    """#3961 review (P1): the first piece's run already answered, unsaid, when the turn goes on. That answer is
    struck and the whole turn is one new request, not a second request beside a queued answer."""
    h, room, backend, lives, clock, log = await per_person()
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, check the deploy")
    (row,) = h._requests.items
    row.status = "ready"  # its run answered; the answer is not said yet
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy and the numbers", "name")])
    await says(h, P1, "and the numbers.")
    assert row.status not in h_mod.OPEN, "the answer for half the turn is struck"
    assert [r.task for r in h._requests.items if r.status in h_mod.OPEN] == ["check the deploy and the numbers"]


async def test_a_grown_turn_that_withdraws_its_ask_and_asks_the_same_agent_again_keeps_the_new_ask():
    """#3961 review (P1): "Forge, check the deploy. Never mind that; check the pipeline instead." The first row is
    withdrawn and the replacement is a request, not a steer of the withdrawn row."""
    h, room, backend, lives, clock, log = await per_person()
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, check the deploy.")
    (row,) = h._requests.items
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the pipeline", "name")], cancel=[row.id])
    await says(h, P1, "Never mind that; check the pipeline instead.")
    assert row.status not in h_mod.OPEN
    assert [r.task for r in h._requests.items if r.status in h_mod.OPEN] == ["check the pipeline"]


async def test_a_grown_turn_that_asks_one_agent_two_things_keeps_both_asks():
    """The first piece asked Forge one thing; the whole turn asks Forge two. One amends its row, one is new."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = True
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, check the deploy")
    (row,) = h._requests.items
    h.on_speech(P1, True)
    clock.t += 0.5 + 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name"), ("forge", "draft the release notes", "name")])
    await says(h, P1, "and draft the release notes.")
    assert row.status in h_mod.OPEN
    assert sorted(r.task for r in h._requests.items if r.status in h_mod.OPEN) == ["check the deploy", "draft the release notes"]


async def test_a_third_piece_amends_the_row_a_failed_steer_made_instead_of_adding_another():
    """#3961 review (P1): the second piece's steer found the run ended and made a row of its own (`_steer_row`).
    A third piece sees that row as the turn's: one replacement stays open, never two."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = False  # every steer finds the run gone: `_steer_row` makes a row of the words
    pieces = [("Forge, check the deploy", "check the deploy"), ("and the numbers", "check the deploy and the numbers"),
              ("and the logs.", "check the deploy, the numbers and the logs")]
    for said, task in pieces:
        h.on_speech(P1, True)
        clock.t += 0.5 + 2
        h.on_speech(P1, False)
        backend.route_extra = ops([("forge", task, "name")])
        await says(h, P1, said)
    assert len([r for r in h._requests.items if r.status in h_mod.OPEN]) <= 2, "the run's row and one replacement"


async def test_a_piece_said_seconds_later_while_the_turn_is_at_route_joins_it():
    """#3961 review (P1): /route still decides on the first piece when they go on 3 s later, inside the hold's
    window. The rest is routed with the whole turn; the late `hold` for the start alone is not acted on."""
    h, room, backend, lives, clock, log = await per_person()
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    slow = backend.route_gate = asyncio.Event()
    backend.route_extra = {**ops([("forge", "give me a summary of", "name")]), "hold": True}
    await h.on_final_transcript(P1, "Priya", "Forge, can you give me a summary of")
    for _ in range(30):
        await asyncio.sleep(0)
    backend.route_gate = None
    clock.t += 3
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "summarise my open PRs", "name")])
    await h.on_final_transcript(P1, "Priya", "my open PRs.")
    for _ in range(30):
        await asyncio.sleep(0)
    slow.set()
    await settle(h)
    assert backend.routes[-1][1] == "Forge, can you give me a summary of my open PRs."
    assert [r.task for r in h._requests.items] == ["summarise my open PRs"]


async def test_two_steers_of_a_turns_row_in_flight_make_one_replacement_row():
    """#3961 review (P1): two pieces steer the turn's working row at once and both find its run gone. One
    replacement row, amended by the second, never two."""
    h, room, backend, lives, clock, log = await per_person()
    backend.steer = False
    h.on_speech(P1, True)
    clock.t += 2
    h.on_speech(P1, False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, check the deploy")
    backend.steer_gate = asyncio.Event()
    for said, task in [("and the numbers", "check the deploy and the numbers"), ("and the logs.", "check all three")]:
        h.on_speech(P1, True)
        clock.t += 0.5 + 2
        h.on_speech(P1, False)
        backend.route_extra = ops([("forge", task, "name")])
        await h.on_final_transcript(P1, "Priya", said)
        for _ in range(30):
            await asyncio.sleep(0)
    assert len(backend.late_steers) == 2, "both steers are in flight"
    backend.steer_gate.set()
    await settle(h)
    assert len([r for r in h._requests.items if r.status == "queued"]) == 1, "one replacement"


async def test_an_answers_line_carries_when_it_was_ready_so_the_wait_to_be_heard_is_on_the_record():
    """10-09 prod Meet: ~19 s between Friday's answer being ready and being heard, invisible (the worker's own
    logs are not ours). Its spoken line now carries the ready time; the backend logs the wait."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    ready = clock.t
    answer(h, row.run_id, "forge", "Three PRs are open.")
    clock.t += 7.5
    spoken(h, lives[0])
    said = [x for x in h._transcript._lines if x.kind == "agent" and x.text == "Three PRs are open."]
    assert said and said[-1].ready == ready and said[-1].at - said[-1].ready == 7.5
    ack = [x for x in h._transcript._lines if x.kind == "agent" and x.text != "Three PRs are open."]
    assert all(x.ready == 0.0 for x in ack), "an acknowledgement opens no answer"


async def test_an_acknowledgement_said_after_the_answer_is_ready_does_not_take_its_ready_time():
    """Codex P2 on #4026: the answer was ready while the acknowledgement still played. The ready time is the
    answer's own (its utterance), never the agent's next line."""
    h, backend, lives, clock = await asked(acked=False)  # the acknowledgement is not said yet
    (row,) = h._requests.items
    ready = clock.t
    answer(h, row.run_id, "forge", "Three PRs are open.")
    spoken(h, lives[0])  # the acknowledgement, said after the answer was ready
    await pause(h, clock, h_mod.MEET_QUIET_S + 0.1)  # the room is quiet: the answer goes out
    spoken(h, lives[0])
    lines = [x for x in h._transcript._lines if x.kind == "agent"]
    assert [x.ready for x in lines if x.text != "Three PRs are open."] == [0.0] * (len(lines) - 1)
    assert [x.ready for x in lines if x.text == "Three PRs are open."] == [ready]
