"""A per-person Google Meet: the voice is a mouth with no ears (owner ruling 10-07), told what was said as text.
The worker gives and takes the floor: a name opens it, the person turning to a colleague closes it, anyone
who keeps talking over it stops it, and what was left unsaid keeps the agent's hand up until a pause or a name."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, ops, settle, talks
from test_meeting_mode import P1, P2, per_person, says


async def asked():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = ops([])
    return h, backend, lives, clock


def quiet(h):
    for who in (P1, P2):
        h.on_speech(who, False)
    h.on_active_speakers(set())


async def test_its_person_turning_to_a_colleague_closes_the_voice_and_the_unsaid_answer_keeps_its_hand_up():
    """Its person turns to someone else: the worker takes the floor away on the name."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    h.on_agent_state("forge", "speaking")
    await says(h, P1, "Dan, did you push that fix last night?")
    assert h._voice is None and len(backend.turns) == 1, "closed, and the line was no request"
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."] and backend.late_answers == []
    assert h._state()["lightbulb.agent.forge.status"] == "has_news"
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce" and ("say", "Three PRs are open.") in lives[-1].calls


async def test_its_own_person_who_keeps_talking_over_it_stops_it_like_anyone_and_its_hand_goes_up():
    """It hears nobody, its asker included: only the worker can stop it for them."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert ("say", "Three PRs are open.") in lives[0].calls
    h.on_agent_state("forge", "speaking")
    talks(h, P1)  # its own person
    clock.t += h_mod.TALK_OVER_S - 0.1
    await h.tick()
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and h._voice is not None, "a backchannel: it carries on"
    clock.t += 0.1
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."], "its hand is up again"
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


async def test_an_announced_answer_talked_over_by_someone_else_goes_back_to_the_hand_not_the_record():
    """Rig 10-07: Leonardo's "oh wait, sorry" over the announcement sent the answer to the record (late-answer)."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S  # the exchange closed while the run worked
    await h.tick()
    await settle(h)
    assert h._voice is None
    answer(h, row.run_id, "forge", "Three PRs are open.")
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce"
    h.on_agent_state("forge", "speaking")  # its audio is out: the news is consumed
    talks(h, P2)  # someone else starts over it
    await settle(h)
    assert h._voice is None and ("interrupt",) in lives[-1].calls
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."] and backend.late_answers == []
    h.on_speech(P2, False)
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce" and len(lives) == 3, "said whole at the next pause"


async def test_its_own_person_talking_over_an_announcement_cuts_it_and_the_hand_goes_back_up():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    h.on_agent_state("forge", "speaking")
    talks(h, P1)  # the person it is pinned to: "Forge, what about the closed ones?"
    await settle(h)
    assert h._voice is None and ("interrupt",) in lives[-1].calls
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."] and backend.late_answers == []


def test_the_unsaid_part_starts_at_the_sentence_that_was_cut():
    answer = "Three PRs are open. The oldest is two days old. One of them is a draft."
    assert h_mod.unsaid_part(answer, "Three PRs are open.") == "The oldest is two days old. One of them is a draft."
    assert h_mod.unsaid_part(answer, "Three PRs are open. The oldest is") == "The oldest is two days old. One of them is a draft."
    assert h_mod.unsaid_part(answer, "") == answer and h_mod.unsaid_part(answer, answer) == ""


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
    h.on_agent_said("forge", "The oldest is", interrupted=True)  # cut here
    talks(h, P2)
    await settle(h)
    assert [n[2] for n in h._news.get("forge", [])] == ["The oldest is two days old. One of them is a draft."]


async def test_a_follow_up_from_its_person_reaches_the_router_and_is_given_to_the_voice_as_text():
    """It heard none of it: /route understands the floor line, and the voice is told what was asked and
    nudged to acknowledge it in its own words."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    before, routes, turns = len(lives[0].calls), len(backend.routes), len(backend.turns)
    backend.route_extra = ops([("forge", "the closed PRs too", "follow")])
    await says(h, P1, "and the closed ones too")
    assert len(backend.routes) == routes + 1 and backend.route_bodies[-1]["floor"]["mine"] is True
    assert len(backend.turns) == turns + 1, "its run is started by the worker"
    after = lives[0].calls[before:]
    assert [c[0] for c in after] == ["thinking", "thinking", "nudge"], after
    assert 'Priya asked: "the closed PRs too"' in after[0][1] and "You are on the closed PRs too" in after[1][1]


async def test_small_talk_from_its_person_is_given_to_the_voice_as_a_typed_line():
    h, backend, lives, clock = await asked()
    before = len(lives[0].calls)
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": []}}
    await says(h, P1, "thanks, that is great")
    assert lives[0].calls[before:] == [("reply_to", "thanks, that is great")]


async def test_what_an_earlier_answer_said_does_not_count_as_this_one_said():
    """Codex on #3792: a sentence repeated from an earlier answer looked already heard."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    quiet(h)
    clock.t += h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    h._voice.said = ["Three PRs are open.", "The oldest is two days old."]  # an earlier answer, same words
    h.on_agent_state("forge", "speaking")  # this answer's audio begins: nothing of it is said yet
    talks(h, P2)
    await settle(h)
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open. The oldest is two days old."]


async def test_an_answer_handed_to_a_later_voice_of_the_agent_starts_its_own_said_list():
    """Codex on #3792: the hand-over path did not restart the list, so old words could count as this answer's."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    assert h._voice is None and row.status == "working"
    await says(h, P1, "Forge, are you there?")  # a new voice of the same agent opens
    h._voice.said = ["Three PRs are open."]  # what it said in this exchange so far, same words by chance
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")  # the earlier run: handed over
    h.on_agent_state("forge", "speaking")
    talks(h, P2)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open. The oldest is two days old."]


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


async def test_its_own_person_interrupting_an_announcement_keeps_the_rest_in_the_hand_and_it_is_said_at_the_next_pause():
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock)
    talks(h, P1)  # its own person: "Forge, what about the closed ones?"
    await settle(h)
    assert h._voice is None and ("interrupt",) in lives[-1].calls
    assert [n[2] for n in h._news.get("forge", [])] == ["The oldest is two days old."], "the rest, from the cut sentence"
    h.on_speech(P1, False)
    quiet(h)
    clock.t += h_mod.MEET_FOLLOWUP_S + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce"
    assert [c for c in lives[-1].calls if c[0] == "say"] == [("say", "The oldest is two days old.")], "only the rest"


async def test_a_hand_the_agent_said_after_all_is_let_go_not_said_again():
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock)
    talks(h, P1)
    await settle(h)
    assert h._news.get("forge")
    h.on_agent_said("forge", "The oldest is two days old.")  # its audio ran on to the end of the sentence
    h.on_speech(P1, False)
    quiet(h)
    clock.t += h_mod.MEET_FOLLOWUP_S + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert not h._news.get("forge") and backend.late_answers == []
    assert len(lives) == 2, "the exchange's voice and the one announcement: no second announcement"


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
    answer(h, row.run_id, "forge", "No PRs are open.")  # the same words, raised later
    talks(h, P2)
    await h.tick()
    await settle(h)
    assert [n[2] for n in h._news.get("forge", [])] == ["No PRs are open."]


async def test_a_stop_that_trails_off_is_a_stop_not_a_fragment():
    """Codex on #3792: "Forge, stop..." held as a fragment was never applied."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Forge, stop...")
    assert row.status == "cancelled" and backend.aborts == [row.run_id]


async def test_a_hand_is_trimmed_to_what_was_not_heard_when_the_cut_message_lands_late():
    """Codex on #3792: LiveKit reports the cut message after the hand went back up, so the worker learns only
    then which sentences were heard. The hand keeps the rest, not the whole."""
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock, "Three PRs are open. The oldest is two days old. One of them is a draft.")
    talks(h, P1)  # its own person interrupts it
    await settle(h)
    assert [n[2] for n in h._news.get("forge", [])] == ["The oldest is two days old. One of them is a draft."]
    clock.t += 1
    h.on_agent_said("forge", "The oldest is two days old. One of", interrupted=True)  # the cut message, late
    await h.tick()
    assert [n[2] for n in h._news.get("forge", [])] == ["One of them is a draft."]


async def test_a_trailing_fragment_still_claims_an_unclaimed_question():
    """Codex on #3792: "Well..." from a person answering a question nobody claimed must count as someone
    answering, or the question is routed to an agent over them."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "what did we ship last week?")
    assert h._unclaimed is not None
    await says(h, P2, "Well...")
    assert h._unclaimed is None and h._last_speaker == P2


async def test_an_answer_cut_before_its_person_turned_away_is_kept_once_in_the_hand_not_also_as_cut():
    """Codex on #3792: the hand took the answer, then the close kept the same answer a second time as cut, so
    the room got it posted while the hand still announced it."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are", interrupted=True)  # their sound cut it before its first sentence
    await says(h, P1, "Dan, did you push that fix last night?")
    await settle(h)
    assert h._voice is None and "forge" not in h._cut_off
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open. The oldest is two days old."]
    quiet(h)
    clock.t += h_mod.CUT_WAIT_S + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert backend.late_answers == [] and h._voice is not None and h._voice.path == "announce"


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
    assert row.status == "cancelled" and backend.aborts == [row.run_id]


def test_the_unsaid_part_spends_the_words_a_sentence_used():
    """Codex on #3792: a later sentence made of the same words is not heard for free."""
    assert h_mod.unsaid_part("Three PRs are open. Three open PRs are in backend.", "Three PRs are open.") == (
        "Three open PRs are in backend."
    )
    assert h_mod.unsaid_part("Three PRs are open. Two PRs are drafts.", "Three PRs are open. Two PRs are drafts.") == ""
    # an answer of function words only ("Yes.", "I can.") is kept unless said word for word (Codex on #3792)
    assert h_mod.unsaid_part("Yes. The build is green.", "") == "Yes. The build is green."
    assert h_mod.unsaid_part("Yes. The build is green.", "Yes.") == "The build is green."
    assert h_mod.unsaid_part("I can.", "I can.") == ""


def test_spoken_words_are_spent_across_queued_answers_in_delivery_order():
    """Codex on #3792 (after the merge): two queued answers share wording and only the first was said."""
    bags = h_mod._said_bags("No PRs are open.")
    assert h_mod._unsaid_with("No PRs are open.", bags)[0] == ""
    assert h_mod._unsaid_with("No PRs are open. The deploy is green.", bags)[0] == "No PRs are open. The deploy is green."
    # Codex on #3800: a "Yes." is spent too, and a phonetic match spends the word that sounded like it
    bags = h_mod._said_bags("Yes.")
    assert h_mod._unsaid_with("Yes.", bags)[0] == "" and h_mod._unsaid_with("Yes.", bags)[0] == "Yes."
    bags = h_mod._said_bags("peers are open")
    assert h_mod._unsaid_with("PRs are open.", bags)[0] == "" and h_mod._unsaid_with("Peers are open.", bags)[0] == "Peers are open."
    bags = h_mod._said_bags("I can deploy.")  # a said sentence spends its function words too
    assert h_mod._unsaid_with("I can deploy.", bags)[0] == "" and h_mod._unsaid_with("I can.", bags)[0] == "I can."
    bags = h_mod._said_bags("build")  # a cut sentence keeps its text whole but its fragment's words are spent
    assert h_mod._unsaid_with("The build failed during deployment.", bags)[0] == "The build failed during deployment."
    assert h_mod._unsaid_with("Build.", bags)[0] == "Build."
    bags = h_mod._said_bags("I can build")  # a cut sentence's function words are spent too
    assert h_mod._unsaid_with("I can build after deployment fails.", bags)[0] == "I can build after deployment fails."
    assert h_mod._unsaid_with("I can.", bags)[0] == "I can."


async def test_news_handed_to_a_named_voice_keeps_its_hand_if_the_voice_closes_before_its_first_word():
    """Codex on #3792 (after the merge): a participant called on Forge's hand, the seed consumed the news, and the
    voice closed before a word (they turned to a colleague), so the answer was gone."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."]
    await says(h, P1, "Forge, go ahead.")  # called on its hand: the news rides its seed
    assert h._voice is not None and not h._news.get("forge")
    await says(h, P1, "Dan, did you push that fix last night?")  # before its first word
    assert h._voice is None and [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."]
    assert backend.late_answers == []


async def test_seeded_news_said_to_its_end_stays_said_when_a_later_answer_resets_the_list():
    """Codex on #3800 (after the merge): a named voice said its seeded news, a second run's answer arrived while
    it still talked and reset `v.said`, and a turn-away then raised the said news again."""
    h, backend, lives, clock = await asked()
    (row1,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    backend.route_extra = ops([])
    row2 = next(r for r in h._requests.items if r is not row1)
    clock.t += h_mod.MEET_FOLLOWUP_S  # the exchange closed while both runs worked
    await h.tick()
    await settle(h)
    answer(h, row1.run_id, "forge", "Three PRs are open.")
    await says(h, P1, "Forge, go ahead.")  # called on its hand: the news rides its seed
    assert h._voice is not None
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs are open.")  # the seed, said to its end
    answer(h, row2.run_id, "forge", "The deploy is green.")  # a second answer lands while it still talks
    await says(h, P1, "Dan, did you push that fix last night?")  # turns away
    assert h._voice is None
    assert [n[2] for n in h._news.get("forge", [])] == ["The deploy is green."]


async def test_a_sentence_cut_by_an_answer_boundary_is_completed_by_its_other_half():
    """Codex on #3803: "Three PRs" before the boundary and "are open." after it, each under the mark on its own."""
    h, backend, lives, clock = await asked()
    (row1,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    backend.route_extra = ops([])
    row2 = next(r for r in h._requests.items if r is not row1)
    clock.t += h_mod.MEET_FOLLOWUP_S
    await h.tick()
    await settle(h)
    answer(h, row1.run_id, "forge", "Three PRs are open.")
    await says(h, P1, "Forge, go ahead.")
    h.on_agent_state("forge", "speaking")
    h.on_agent_said("forge", "Three PRs")  # half the seed, then the boundary
    answer(h, row2.run_id, "forge", "The deploy is green.")
    h.on_agent_said("forge", "are open.")  # the other half
    await says(h, P1, "Dan, did you push that fix last night?")
    assert [n[2] for n in h._news.get("forge", [])] == ["The deploy is green."]


def test_a_word_an_earlier_answer_used_is_not_carried_over_the_boundary():
    """Codex on #3803: "Build passed." heard, "Build failed." not; "build" must not carry, or "failed." alone
    would complete the second answer."""
    bags = h_mod._said_bags("Build passed.")
    assert h_mod._unsaid_with("Build passed.", bags) == ("", [])
    assert h_mod._unsaid_with("Build failed.", bags) == ("Build failed.", [])
    assert h_mod._unsaid_with("Three PRs are open.", h_mod._said_bags("Three PRs")) == ("Three PRs are open.", ["three", "prs"])


async def test_a_line_the_router_understood_as_said_to_a_person_releases_the_floor():
    """10-07 rig sales call: "Monthly, with an annual option. Maya, what is your current tool costing you?" kept
    the floor and the agent said its answer over Maya's reply. /route says who a line is for; no word pattern."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = {"ops": {"requests": [], "social": None, "more": False, "cancel": [], "steer": [], "people": True}}
    await says(h, P1, "Monthly, with an annual option. And what is your current tool costing you?")
    assert h._voice is None
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."], "its hand, not its voice"


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
    assert h._voice is None
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open. The oldest is two days old."]


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
    assert not h._news.get("forge")


async def test_called_on_while_its_work_runs_the_voice_is_told_what_it_is_still_working_on():
    """Codex on #3820: /route's `ops.say` status was read by nobody."""
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
    assert 'You are still working on this for them: "list the open PRs"' in str(lives[-1].seed) + str(lives[-1].calls)
