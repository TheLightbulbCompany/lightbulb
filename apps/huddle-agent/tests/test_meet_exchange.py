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
