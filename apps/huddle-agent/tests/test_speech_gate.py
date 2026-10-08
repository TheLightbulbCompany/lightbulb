"""One gate for everything a meeting voice (a mouth) says (`Huddle._pump`): one agent at a time, nothing over a
person, nothing until the room is quiet, and every ready answer said at the first opening. A person who keeps
talking over it is the signal to yield. A stop cancels. 10-07 rig, 15 scenarios."""

import asyncio
import json

from huddle_agent import huddle as h_mod
from huddle_agent.transcript import only_name, stop_bare, stop_request, stop_target
from test_huddle import TEAM, answer, ops, pause, settle, spoken, talks
from test_meet_exchange import asked, closes, quiet, says_of, talked_over
from test_meeting_mode import P1, P2, per_person, says

ACK = "Okay, list the open PRs. I'm on it now."


def said(lives):
    """Every order of the call, in the order it was given: (agent, text)."""
    return [(live.agent_id, c[1]) for live in lives for c in live.calls if c[0] == "say"]


async def both_working():
    """Forge was asked, then Gauge: both runs work, Gauge's voice is open, both acknowledgements are said."""
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("gauge", "check the deploy", "name")])
    await says(h, P1, "Gauge, check the deploy.")
    backend.route_extra = ops([])
    spoken(h, lives[-1])
    forge, gauge = h._requests.items
    return h, backend, lives, clock, forge, gauge


async def test_two_agents_with_ready_answers_speak_one_after_the_other_in_the_order_they_became_ready():
    h, backend, lives, clock, forge, gauge = await both_working()
    talks(h, P2)  # the room is talking when both land
    answer(h, forge.run_id, "forge", "Three PRs are open.")
    clock.t += 1
    answer(h, gauge.run_id, "gauge", "The deploy is green.")
    await settle(h)
    assert said(lives)[2:] == [], "nothing starts over a person"
    quiet(h)
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open.")], "the older answer first, and only it"
    assert h._voice.agent_id == "forge" and ("close",) in lives[1].calls, "Gauge's voice closed for it"
    assert [n[2] for n in h._news["gauge"]] == ["The deploy is green."], "what Gauge had to say is its hand"
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open.")], "Gauge waits while Forge is speaking"
    spoken(h, lives[-1])
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open."), ("gauge", "The deploy is green.")]
    spoken(h, lives[-1])
    assert (forge.status, gauge.status) == ("delivered", "delivered") and not h._news


async def test_an_answer_ready_while_its_voice_is_closed_is_said_at_once_in_a_quiet_room():
    """10-07 rig: an answer whose voice had closed waited for the next tick and a second pause."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await closes(h, clock)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await settle(h)  # no tick: the answer's own arrival opens its voice
    assert len(lives) == 2 and says_of(lives[1]) == [("say", "Three PRs are open.")]
    spoken(h, lives[1])
    assert row.status == "delivered"


async def test_an_answer_ready_while_a_person_speaks_waits_for_the_room_to_be_quiet():
    """10-07 rig, sales call: "six agents" started over Leonardo because its voice was still open."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    talks(h, P2)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, 0.5)
    assert says_of(lives[0]) == [("say", ACK)] and row.status == "ready", "its voice is open, and it still waits"
    quiet(h)
    await pause(h, clock, h_mod.MEET_QUIET_S - 0.5)
    assert says_of(lives[0]) == [("say", ACK)], "a breath is not a pause"
    await pause(h, clock, 0.5)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")]


async def test_a_second_answer_of_the_same_agent_is_said_after_the_first():
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
    clock.t += h_mod.MEET_QUIET_S
    first, second = h._requests.items
    answer(h, first.run_id, "forge", "Three PRs are open.")
    await settle(h)
    assert second.status == "working", "its next request starts when the first run ends"
    answer(h, second.run_id, "forge", "The deploy is green.")
    assert says_of(lives[0])[2:] == [("say", "Three PRs are open.")], "the second waits for the first to be said"
    spoken(h, lives[0])
    assert says_of(lives[0])[2:] == [("say", "Three PRs are open."), ("say", "The deploy is green.")]
    spoken(h, lives[0])
    assert (first.status, second.status) == ("delivered", "delivered")


async def test_an_acknowledgement_goes_before_another_agents_waiting_answer():
    """10-07 rig: an acknowledgement began over a person, was cut and never said."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await closes(h, clock)
    talks(h, P1)  # Priya is asking Gauge as Forge's answer lands
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await settle(h)
    quiet(h)
    backend.route_extra = ops([("gauge", "check the deploy", "name")])
    await says(h, P1, "Gauge, check the deploy.")
    assert said(lives)[1:] == [], "not in the same breath as her last word"
    await pause(h, clock, h_mod.LINE_QUIET_S + 0.1)
    assert said(lives)[1:] == [("gauge", "Sure, let me work on that: check the deploy.")]
    spoken(h, lives[-1])
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open.")], "then the answer that waited, at the pause"


async def test_an_agent_named_while_another_is_speaking_does_not_speak_together_with_it():
    """The new voice opened beside the old one's close: the old one went on until its session had closed."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    backend.route_extra = ops([("gauge", "check the deploy", "name")])
    h.gate["live"] = asyncio.Event()
    task = asyncio.create_task(says(h, P1, "Gauge, check the deploy."))
    for _ in range(20):
        await asyncio.sleep(0)
    assert ("interrupt",) in lives[0].calls and len(lives) == 1, "quiet before Gauge's voice has opened"
    h.gate["live"].set()
    await task
    spoken(h, lives[1])
    await pause(h, clock)
    assert said(lives)[2:] == [("gauge", "Sure, let me work on that: check the deploy."), ("forge", "Three PRs are open.")]


async def test_talk_that_lasts_stops_the_mouth_on_its_own_timer_and_the_rest_is_said_later_exactly_once(monkeypatch):
    """10-07 rig: the 1 Hz tick stopped it up to a second late, or (a 1.3 s "Friday, stop.") not at all."""
    monkeypatch.setattr(h_mod, "TALK_OVER_S", 0.05)
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_said("forge", "Three PRs are open.")
    talks(h, P2)
    assert ("interrupt",) not in lives[0].calls, "not at the first sound"
    await asyncio.sleep(0.2)  # no tick: the timer set when the talk began
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None
    assert [n[2] for n in h._news["forge"]] == ["The oldest is two days old."] and row.status == "ready"
    quiet(h)
    await pause(h, clock)
    assert says_of(lives[-1]) == [("say", "The oldest is two days old.")]
    spoken(h, lives[-1])
    assert row.status == "delivered"
    await pause(h, clock, 30)
    await pause(h, clock, 30)
    assert [t for _, t in said(lives) if "oldest" in t] == [
        "Three PRs are open. The oldest is two days old.", "The oldest is two days old."
    ], "the rest once, and nothing again"


async def test_a_backchannel_does_not_stop_it(monkeypatch):
    monkeypatch.setattr(h_mod, "TALK_OVER_S", 0.05)
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    talks(h, P2)  # "mm-hm"
    quiet(h)
    await asyncio.sleep(0.2)
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and h._voice is not None
    spoken(h, lives[0])
    assert row.status == "delivered"


async def test_talk_that_breaks_for_a_moment_is_one_stretch_of_talk():
    """The SFU's level drops between words: each word would start the count again and never reach TALK_OVER_S."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    for _ in range(3):
        talks(h, P2)
        clock.t += 0.4
        h.on_active_speakers(set())
        clock.t += h_mod.TALK_GAP_S - 0.1
    talks(h, P2)
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None


def test_a_stop_is_read_from_the_last_sentence_when_that_sentence_names_the_agent():
    agents = TEAM["agents"]
    assert stop_request("Perfect. Forge, stop.", agents) and stop_target("Perfect. Forge, stop.", agents) == "forge"
    assert stop_target("Thanks, that is all I need! Forge, never mind.", agents) == "forge"
    assert stop_target("Forge, stop.", agents) == "forge" and stop_bare("Stop.")
    # a last sentence that names nobody is people talking
    assert not stop_request("We tried the other vendor. Never mind.", agents)
    assert stop_target("Perfect. Dan, stop.", agents) is None


async def test_a_stop_after_another_sentence_cancels_and_opens_no_voice_and_writes_no_reply():
    """10-07 rig: "Perfect. Friday, stop." read "perfect friday" as the name, summoned her, and she said
    "Got it; I will stop now." five seconds later."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    written = []

    async def writer(persona, event):
        written.append(event)
        return "Got it; I will stop now."

    h._writer = writer
    await closes(h, clock)
    routes, sessions = len(backend.routes), len(backend.sessions)
    backend.route_extra = {"ops": {"requests": [], "social": "forge", "more": False, "cancel": [], "steer": []}}
    await says(h, P1, "Perfect. Forge, stop.")
    assert row.status == "cancelled" and backend.aborts == [row.run_id]
    assert (len(backend.routes), len(backend.sessions), len(lives), written) == (routes, sessions, 1, [])
    await says(h, P2, "Perfect Forge, stop")  # no comma where the sentence ends: the line still names one agent
    await pause(h, clock, 30)
    assert (len(backend.routes), len(backend.sessions), len(lives), written) == (routes, sessions, 1, [])


async def test_nothing_is_said_after_a_stop_also_when_the_mouth_had_already_yielded_to_it():
    """The person says "stop" over it: it yields about a second in, and the words come later. By then what it
    had not said is on its hand, and the stop takes that too."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_said("forge", "Three PRs are open.")
    await talked_over(h, clock, P1)
    assert [n[2] for n in h._news["forge"]] == ["The oldest is two days old."]
    quiet(h)
    await says(h, P1, "Stop.")
    assert not h._news and row.status == "cancelled"
    for _ in range(3):
        await pause(h, clock, 30)
    assert len(lives) == 1 and backend.late_answers == [], "neither said nor posted"

    # by name, with the rest on the hand and a second request waiting
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    await talked_over(h, clock, P2)
    quiet(h)
    await says(h, P2, "Forge, stop.")
    for _ in range(3):
        await pause(h, clock, 30)
    assert not h._news and len(lives) == 1 and backend.late_answers == []


def test_only_a_name():
    agents = TEAM["agents"]
    assert only_name("Forge.", agents) == only_name("Um, Forge...", agents) == only_name("Hey Forge", agents) == "forge"
    assert only_name("Forge?", agents) is None, "a question put to it"
    assert only_name("Forge, what PRs are open?", agents) is None and only_name("Dan.", agents) is None


async def test_a_name_said_alone_is_held_and_joined_with_what_comes_next_into_one_request():
    """10-07 rig: "Friday." was answered "Yes, I'm here." over the question, and the question, with no name on
    it, went to another agent."""
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge.")
    assert backend.routes == [] and lives == [] and backend.sessions == [], "held: nothing answers a name"
    clock.t += 3
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "What PRs are open?")
    assert [r[1] for r in backend.routes] == ["Forge. What PRs are open?"], "one line, with its name"
    assert [r.agent for r in h._requests.items] == ["forge"] and len(backend.turns) == 1
    assert says_of(lives[0]) == [("say", ACK)] and len(lives) == 1, "one request, one acknowledgement"
    text = h._transcript.text()
    assert text.count("Forge.") == 1 and "What PRs are open?" in text, "each piece on the record once"


async def test_a_name_that_nothing_follows_is_a_call_on_and_gets_a_fixed_line_or_its_status():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge.")
    await pause(h, clock, h_mod.TRAIL_S - 1)
    assert lives == []
    await pause(h, clock, 1)
    assert says_of(lives[0]) == [("say", h_mod.HERE[0])] and backend.routes == [] and backend.turns == []

    h, backend, lives, clock = await asked()  # its run still works
    await closes(h, clock)
    await says(h, P1, "Forge...")
    await pause(h, clock, h_mod.TRAIL_S)
    assert says_of(lives[-1]) == [("say", "I'm still working on this: list the open PRs.")]
    assert len(backend.turns) == 1, "no new run"


async def test_the_askers_next_line_after_their_answer_goes_to_the_router_at_once():
    """10-07 rig: "And what are they called?" 22 s after the answer waited out the unclaimed delay (6.8 s)."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    spoken(h, lives[0])
    await closes(h, clock)
    clock.t += 10
    routes = len(backend.routes)
    backend.route_extra = ops([("forge", "the names of the open PRs", "follow")])
    await says(h, P1, "And what are they called?")  # no tick, no wait
    assert len(backend.routes) == routes + 1
    assert backend.route_bodies[-1]["floor"] == {"agent": "forge", "speaker": "Priya", "mine": True}
    assert backend.route_bodies[-1]["unclaimed"] is False, "the router decides whether it is for the agent"
    assert len(backend.turns) == 2 and backend.turns[-1]["agentId"] == "forge" and len(lives) == 2

    # someone else spoke in between: it is a question to the room again
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    spoken(h, lives[0])
    await closes(h, clock)
    routes = len(backend.routes)
    await says(h, P2, "we should look at those today")
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes and h._unclaimed is not None


async def test_a_line_that_waited_too_long_for_its_pause_is_dropped_not_said_late():
    h, backend, lives, clock = await asked(acked=False)
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    talks(h, P2)
    await says(h, P1, "Forge, also check the deploy.")  # its "next" line waits behind the acknowledgement
    spoken(h, lives[0])
    assert len(says_of(lives[0])) == 1, "held: someone is talking"
    clock.t += h_mod.LINE_TTL_S
    quiet(h)
    await pause(h, clock, 1)
    assert len(says_of(lives[0])) == 1 and h._voice.utterances == []


async def test_the_gate_logs_what_it_saw_when_it_holds_speech_and_when_it_lets_it_start(capsys):
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    capsys.readouterr()
    talks(h, P2)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    quiet(h)
    await pause(h, clock)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    held = [x for x in lines if "live.speech.held" in x]
    assert [(x["kind"], x["why"]) for x in held] == [("answer", "person"), ("answer", "quiet")]
    (waited,) = [x for x in lines if "live.speech.waited" in x]
    assert waited["live.speech.waited"] == h_mod.MEET_QUIET_S * 1000 and waited["kind"] == "answer"
