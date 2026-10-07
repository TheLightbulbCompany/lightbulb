"""A per-person Google Meet: GPT-Live hears the one person it is talking with and runs the exchange itself
(owner ruling 10-06: duplex, not a muted model fed sentences). The worker gives and takes the floor: a name
opens it, the person turning to a colleague closes it, someone else talking over it stops it, and what was
left unsaid keeps the agent's hand up until a pause or a name."""

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
    """The one risk of a duplex voice in a meeting: it answers what its person says to someone else. The
    worker takes the floor away on the name (the model's own rule covers the rest)."""
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
    assert h._voice is not None and h._voice.path == "announce" and "Three PRs are open." in str(lives[-1].seed)


async def test_its_own_person_interrupting_is_the_models_business_but_someone_else_talking_over_it_stops_it():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    h.on_agent_state("forge", "speaking")
    talks(h, P1)  # its own person: GPT-Live hears them and stops by itself
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and h._voice is not None
    talks(h, P2)  # someone it cannot hear
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None
    assert [n[2] for n in h._news.get("forge", [])] == ["Three PRs are open."], "its hand is up again"


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


async def test_its_own_person_interrupting_an_announcement_is_left_to_the_model():
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
    assert h._voice is not None and ("interrupt",) not in lives[-1].calls


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


async def test_a_follow_up_its_voice_heard_is_not_fed_to_it_again_as_text():
    """Codex on #3792: the muted design told the voice what its person said; a duplex voice heard it."""
    h, backend, lives, clock = await asked()
    kinds = lambda: [c[0] for c in lives[0].calls]  # noqa: E731
    before, routes = kinds(), len(backend.routes)
    backend.route_extra = ops([("forge", "the closed PRs too", "follow")])
    await says(h, P1, "and the closed ones too")
    assert len(backend.routes) == routes + 1, "the worker still understands the line"
    assert [k for k in kinds()[len(before):] if k in ("reply_to", "nudge", "commentary")] == []


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


async def test_the_talk_over_clock_runs_from_when_someone_else_began_not_from_its_own_person():
    """Codex on #3792: its person had talked a while, then a brief interjection by someone else cut it at once."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    h.on_agent_state("forge", "speaking")
    talks(h, P1)  # its own person, for a while
    clock.t += h_mod.TALK_OVER_S + 5
    await h.tick()
    await settle(h)
    assert h._voice is not None
    talks(h, P2)  # someone else begins now
    await h.tick()
    await settle(h)
    assert h._voice is not None and ("interrupt",) not in lives[0].calls, "a word in: not a talk-over yet"
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    await settle(h)
    assert h._voice is None and ("interrupt",) in lives[0].calls


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


async def test_its_own_person_interrupting_an_announcement_keeps_the_rest_in_the_hand_and_the_voice_theirs():
    """Codex on #3792: left to the model, the remainder had no hand, retry or record."""
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock)
    talks(h, P1)  # its own person: "Forge, what about the closed ones?"
    await settle(h)
    assert h._voice is not None and ("interrupt",) not in lives[-1].calls, "the voice stays theirs"
    assert [n[2] for n in h._news.get("forge", [])] == ["The oldest is two days old."], "the rest, from the cut sentence"
    h.on_speech(P1, False)
    quiet(h)
    clock.t += h_mod.MEET_FOLLOWUP_S + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert h._voice is not None and h._voice.path == "announce" and "The oldest is two days old." in str(lives[-1].seed)


async def test_a_hand_the_agent_said_after_all_is_let_go_not_said_again():
    h, backend, lives, clock = await asked()
    await announced(h, backend, lives, clock)
    talks(h, P1)
    await settle(h)
    assert h._news.get("forge")
    h.on_agent_said("forge", "The oldest is two days old.")  # it went on by itself after the interruption
    h.on_speech(P1, False)
    quiet(h)
    clock.t += h_mod.MEET_FOLLOWUP_S + h_mod.MEET_QUIET_S + h_mod.gate.ANNOUNCE_QUIET
    await h.tick()
    await settle(h)
    assert not h._news.get("forge") and backend.late_answers == []
    assert len(lives) == 2, "the exchange's voice and the one announcement: no second announcement"


async def test_a_per_person_answer_cut_by_its_persons_noise_is_said_once_more_like_a_live_call():
    """Codex on #3792: the per-person voice took the mixed-meet gate and skipped the Live recovery."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    text = "Three PRs are open."
    answer(h, row.run_id, "forge", text)
    h.on_agent_said("forge", "Three", interrupted=True)  # their sound stopped it after a word
    await says(h, P1, "Hello, guys.")  # not for it
    quiet(h)
    clock.t += h_mod.CUT_HANDLED_S + h_mod.MEET_QUIET_S
    await h.tick()
    await settle(h)
    resays = [c for c in lives[0].calls if c[0] == "commentary" and c[1].startswith("Your answer was cut off")]
    assert len(resays) == 1 and h_mod.RESAY.format(answer=text) == resays[0][1]
