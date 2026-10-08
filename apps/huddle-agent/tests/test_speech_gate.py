"""What a room of mouths says, and when (a Meet today). Three records and one scheduler:

- the room's one ledger of utterances (`Huddle._ledger`): what each agent has to say, whichever voice is open;
- one scheduler (`Huddle._pump`): one agent at a time, nothing over a person, the same order for everyone;
- an engagement per person and agent (`Engagement`): whether that person is waiting to hear that agent.

Being talked over makes a mouth yield. Stopping its speech is not cancelling its work. 10-07 rig, 15 scenarios."""

import asyncio
import json

from huddle_agent import huddle as h_mod
from huddle_agent.transcript import backchannel, only_name, stop_bare, stop_request, stop_target, withdraws
from test_huddle import TEAM, answer, hand, ops, pause, settle, spoken, talks
from test_meet_exchange import asked, closes, quiet, says_of, talked_over
from test_meeting_mode import MEET_PEOPLE, P1, P2, per_person, says

ACK = h_mod.ACKS[0]
P3 = "meet:room-1:p3"
BREATH = h_mod.SETTLE_S + 0.1  # the room has settled, and the conversation has not paused


async def stops(h, who=P2):
    """They stop talking, and the transcript of it is here: a listening sound that asks nothing. Speech with
    no transcript yet is an open turn, and nothing starts on one."""
    quiet(h)
    await says(h, who, "Mm-hm.")


def said(lives):
    """Every order of the call, in the order it was given: (agent, text)."""
    return [(live.agent_id, c[1]) for live in lives for c in live.calls if c[0] == "say"]


def ledger(h):
    return [(u.agent, u.kind, u.state) for u in h._ledger]


def heard(social=None, **more):
    """A /route answer that made no request."""
    return {"ops": {"requests": [], "social": social, "more": False, "cancel": [], "steer": [], **more}}


async def both_working():
    """Priya asked Forge, then Dan asked Gauge: both runs work, Gauge's voice is open, both acknowledged."""
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("gauge", "check the deploy", "name")])
    await says(h, P2, "Gauge, check the deploy.")
    backend.route_extra = ops([])
    spoken(h, lives[-1])
    forge, gauge = h._requests.items
    return h, backend, lives, clock, forge, gauge


# -- the ledger and its one scheduler --


async def test_answers_are_said_one_at_a_time_in_the_order_they_were_asked_and_a_voice_switch_loses_nothing():
    h, backend, lives, clock, forge, gauge = await both_working()
    talks(h, P2)  # the room is talking when both land
    answer(h, gauge.run_id, "gauge", "The deploy is green.")  # asked second, ready first
    clock.t += 1
    answer(h, forge.run_id, "forge", "Three PRs are open.")
    await settle(h)
    assert said(lives)[2:] == [], "nothing starts over a person"
    assert ledger(h) == [("gauge", "answer", "pending"), ("forge", "answer", "pending")]
    quiet(h)
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open.")], "asked first, said first, and only it"
    assert h._voice.agent_id == "forge" and ("close",) in lives[1].calls, "Gauge's voice closed for it"
    assert hand(h, "gauge") == ["The deploy is green."], "the ledger is the room's: closing a voice changes nothing"
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open.")], "Gauge waits while Forge is speaking"
    spoken(h, lives[-1])
    await pause(h, clock)
    assert said(lives)[2:] == [("forge", "Three PRs are open."), ("gauge", "The deploy is green.")]
    spoken(h, lives[-1])
    assert (forge.status, gauge.status) == ("delivered", "delivered") and h._ledger == []


async def test_an_utterance_cut_by_a_switch_to_another_agent_is_said_later_from_its_cursor_exactly_once():
    """The new voice opened beside the old one's close: the old one went on until its session had closed."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_said("forge", "Three PRs are open.")
    backend.route_extra = ops([("gauge", "check the deploy", "name")])
    h.gate["live"] = asyncio.Event()
    task = asyncio.create_task(says(h, P1, "Gauge, check the deploy."))
    for _ in range(20):
        await asyncio.sleep(0)
    assert ("interrupt",) in lives[0].calls and len(lives) == 1, "quiet before Gauge's voice has opened"
    h.gate["live"].set()
    await task
    assert ledger(h) == [("forge", "answer", "pending"), ("gauge", "ack", "speaking")], "the line owed goes first"
    assert h._ledger[0].cursor == 1 and row.status == "ready"
    spoken(h, lives[1])
    await pause(h, clock)
    assert said(lives)[2:] == [("gauge", h_mod.ACKS[1]), ("forge", "The oldest is two days old.")]
    spoken(h, lives[-1])
    for _ in range(3):
        await pause(h, clock, 30)
    assert [t for _, t in said(lives) if "oldest" in t] == [
        "Three PRs are open. The oldest is two days old.", "The oldest is two days old."
    ], "the rest once, and nothing again"  # fmt: skip
    assert row.status == "delivered"


async def test_a_second_answer_of_the_same_agent_is_said_after_the_first():
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
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


async def test_a_line_owed_never_takes_the_voice_from_another_agent_but_goes_before_its_own_agents_answer():
    """10-07 rig: an acknowledgement began over a person, was cut and never said."""
    h, backend, lives, clock = await asked(acked=False)  # Forge's acknowledgement is being said
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    talks(h, P2)
    await says(h, P1, "Forge, also check the deploy.")  # its "next" line is owed while its first one is said
    (first, _) = h._requests.items
    answer(h, first.run_id, "forge", "Three PRs are open.")
    assert ledger(h) == [("forge", "ack", "speaking"), ("forge", "ack", "pending"), ("forge", "answer", "pending")]
    spoken(h, lives[0])
    await stops(h)
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[1:] == [("say", h_mod.QUEUED_LINES[0])], "the line before the answer"
    spoken(h, lives[0])
    assert says_of(lives[0])[2:] == [("say", "Three PRs are open.")]


async def test_a_ready_answer_takes_the_place_of_its_waiting_status_line():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    talks(h, P2)
    say = {"agentId": "forge", "kind": "status", "task": "list the open PRs", "text": None}
    backend.route_extra = heard("forge", say=say)
    await says(h, P1, "Forge, how is it going?")
    assert ledger(h) == [("forge", "status", "pending")], "owed, and held: someone is talking"
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert ledger(h) == [("forge", "answer", "pending")], "the answer says more than 'still working'"
    await stops(h)
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")]


async def test_an_acknowledgement_is_skipped_when_its_answer_is_ready_before_it_could_be_said():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    talks(h, P2)
    await says(h, P1, "Forge, what PRs are open?")
    (row,) = h._requests.items
    assert ledger(h) == [("forge", "ack", "pending")] and says_of(lives[0]) == []
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert ledger(h) == [("forge", "answer", "pending")]
    await stops(h)
    await pause(h, clock, BREATH)
    assert says_of(lives[0]) == [("say", "Three PRs are open.")], "the answer, and no 'I'm on it' before it"


async def test_each_kind_of_line_expires_by_its_own_rule_and_an_answer_never_while_its_asker_is_here():
    h, backend, lives, clock = await asked(acked=False)
    (row,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    talks(h, P2)  # the room talks on and on
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
    assert ledger(h) == [("forge", "ack", "pending")]
    await pause(h, clock, h_mod.EXPIRES_S["ack"] - 1)
    assert ledger(h) == [("forge", "ack", "pending")]
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, 1)
    assert ledger(h) == [("forge", "answer", "pending")], "a stale acknowledgement is dropped, not said late"
    for _ in range(20):
        await pause(h, clock, 60)
    assert hand(h, "forge")[0] == "Three PRs are open." and backend.late_answers == [], "20 minutes of talk: still owed"
    await h.on_participants({P2: "Dan"})  # its asker leaves
    await pause(h, clock, h_mod.ASKER_GONE_S)
    assert h._ledger == [] and "Three PRs are open." in [a[3] for a in backend.late_answers], "the room gets it"


async def test_small_talk_written_for_a_line_that_is_no_longer_the_last_one_is_not_said():
    h, backend, lives, clock = await asked()
    gate, written = asyncio.Event(), []

    async def writer(persona, event):
        written.append(event)
        await gate.wait()
        return "Glad it helps."

    h._writer = writer
    backend.route_extra = heard("forge")
    await h.on_final_transcript(P1, "Priya", "thanks, that is great")
    for _ in range(20):
        await asyncio.sleep(0)
    assert ledger(h) == [("forge", "social", "pending")] and len(written) == 1
    backend.route_extra = heard(people=True)
    await h.on_final_transcript(P1, "Priya", "Dan, did you push that fix last night?")  # she has moved on
    gate.set()
    await settle(h)
    assert h._ledger == [] and "Glad it helps." not in str(lives[0].calls)


async def test_the_scheduler_logs_what_it_saw_when_it_holds_speech_and_when_it_lets_it_start(capsys):
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    capsys.readouterr()
    talks(h, P2)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await stops(h)
    await pause(h, clock, BREATH)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    held = [x for x in lines if "live.speech.held" in x]
    assert [(x["kind"], x["why"]) for x in held] == [("answer", "person"), ("answer", "final"), ("answer", "quiet")]
    (waited,) = [x for x in lines if "live.speech.waited" in x]
    assert waited["live.speech.waited"] == round(BREATH * 1000) and waited["kind"] == "answer"


# -- the answer-start rule: who is waiting to hear the agent (`Engagement`) --


async def test_an_answer_whose_asker_is_waiting_for_it_starts_after_the_room_settles_with_no_three_second_wait():
    """10-07 rig, sales call: "six agents" started over Leonardo because its voice was still open."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    (e,) = h._engagements(h._who(P1))
    assert (e.agent, e.state, e.expects, e.obligation) == ("forge", "active", "answer", row.id)
    talks(h, P2)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, 0.5)
    assert says_of(lives[0]) == [("say", ACK)] and row.status == "ready", "its voice is open, and it still waits"
    await stops(h)
    await pause(h, clock, h_mod.SETTLE_S - 0.1)
    assert says_of(lives[0]) == [("say", ACK)], "sound has not settled"
    await pause(h, clock, 0.2)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")], "well inside MEET_QUIET_S"


async def test_an_asker_who_turned_to_a_colleague_gets_the_answer_at_a_pause_or_at_once_when_they_turn_back():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Dan, did you push that fix last night?")
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.why) == ("suspended", "people")
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, BREATH)
    assert hand(h, "forge") == ["Three PRs are open."] and says_of(lives[0]) == [("say", ACK)], "its hand is up"
    backend.route_extra = heard(called="forge")
    await says(h, P1, "Forge, go ahead.")  # an invitation: no pause is waited for
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")] and len(backend.turns) == 1, "and no new run"

    # nobody turns back: it is said when the conversation pauses
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P1, "Dan, did you push that fix last night?")
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, h_mod.MEET_QUIET_S - 0.5)
    assert says_of(lives[0]) == [("say", ACK)], "a breath is not a pause"
    await pause(h, clock, 0.5)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")]


async def test_what_a_line_of_someone_else_is_and_not_how_many_there_were_says_another_exchange_came_between():
    """10-07 review: two lines of other people suspended the asker's exchange, whatever they were. Two
    endorsements and one unrelated exchange mean different things: /route reads each line (`ops.people`)."""
    h, backend, lives, clock = await asked()
    routes, turns = len(backend.routes), len(backend.turns)
    backend.route_extra = heard()  # about the request: not said to a person
    for line in ("Good question, I wondered too.", "Yes, I would like to know that as well.", "Same here."):
        await says(h, P2, line)
    assert len(backend.routes) == routes + 3 and h._engagements(h._who(P1))[0].state == "active", (
        "endorsements, however many, come between nobody"
    )
    await says(h, P2, "Mm-hm, yeah.")
    assert len(backend.routes) == routes + 3, "a listening sound is not even read"
    backend.route_extra = heard(people=True)
    await says(h, P2, "Lee, where did we land on the budget for next quarter?")
    e = h._engagements(h._who(P1))[0]
    assert (e.state, e.why) == ("suspended", "other"), "one line said to a person: another exchange"
    assert len(backend.turns) == turns and len(h._requests.items) == 1, "read as evidence only: it started nothing"
    await says(h, P2, "I think we cut it by a third.")
    assert len(backend.routes) == routes + 4, "nobody is talking with an agent now: not read"
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, BREATH)
    assert says_of(lives[0]) == [("say", ACK)], "its asker is not waiting on it: a breath is not a pause"
    await pause(h, clock)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")]


async def test_a_late_evidence_reply_does_not_suspend_an_exchange_taken_up_again_after_the_line():
    """10-07 review: /route answered late, and the line it read suspended an exchange renewed since."""
    for renewed in (False, True):
        h, backend, lives, clock = await asked()
        backend.route_extra, backend.route_gate = heard(people=True), asyncio.Event()
        await h.on_final_transcript(P2, "Dan", "Where did we land on the budget for next quarter?")
        for _ in range(5):
            await asyncio.sleep(0)  # the line is at /route, which has not answered
        clock.t += 2
        if renewed:
            h._engage(h._who(P1), "forge")  # Priya turned to Forge again, after Dan's line
        backend.route_gate.set()
        await settle(h)
        e = h._engagements(h._who(P1))[0]
        assert (e.state, e.why) == (("active", "") if renewed else ("suspended", "other"))


async def test_a_line_read_as_evidence_never_changes_a_request_of_someone_else():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = heard(cancel=[row.id], steer=[{"requestId": row.id, "text": "only the stale ones"}])
    await says(h, P2, "We probably do not need that list anyway.")
    assert row.status == "working" and backend.aborts == [] and len(backend.turns) == 1


async def test_two_askers_with_two_agents_keep_separate_engagements():
    h, backend, lives, clock, forge, gauge = await both_working()
    answer(h, forge.run_id, "forge", "Three PRs are open.")
    await settle(h)
    spoken(h, lives[-1])
    answer(h, gauge.run_id, "gauge", "The deploy is green.")
    await settle(h)
    spoken(h, lives[-1])
    assert {e.person[1]: (e.agent, e.last) for e in h._engagements()} == {
        "Priya": ("forge", "delivered"), "Dan": ("gauge", "delivered")
    }  # fmt: skip
    await says(h, P1, "And what are they called?")
    mine = backend.route_bodies[-1]["engagements"]
    assert [(e["agentId"], e["state"], e["last"]) for e in mine] == [("forge", "active", "delivered")]
    await says(h, P2, "And when did it go out?")
    theirs = backend.route_bodies[-1]["engagements"]
    assert [(e["agentId"], e["state"], e["last"]) for e in theirs] == [("gauge", "active", "delivered")]


async def test_the_askers_next_line_goes_to_the_router_at_once_with_the_exchange_as_evidence():
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
    body = backend.route_bodies[-1]
    assert len(backend.routes) == routes + 1 and body["unclaimed"] is False
    assert body["floor"] == {"agent": "forge", "speaker": "Priya", "mine": True}
    assert body["engagements"] == [
        {"agentId": "forge", "obligation": "r1", "task": "list the open PRs", "state": "active", "expects": "nothing",
         "why": "", "last": "delivered", "lastBy": "", "seconds": 20}
    ]  # fmt: skip
    assert body["open"] == [
        {"id": "r1", "agentId": "forge", "task": "list the open PRs", "status": "delivered", "asker": "Priya",
         "delivery": "delivered"}
    ]  # fmt: skip
    assert len(backend.turns) == 2 and backend.turns[-1]["agentId"] == "forge" and len(lives) == 2

    # someone else took a turn, then a long gap with nothing owed: they are no longer talking, and it is a
    # question to the room again (silence alone ends no exchange: test_voice_rules)
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    spoken(h, lives[0])
    await closes(h, clock)
    await says(h, P2, "I will send the plan on Thursday.")
    clock.t += h_mod.ENGAGE_GAP_S
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes and h._unclaimed is not None and h._engaged == {}


async def test_an_agent_that_ends_on_a_question_expects_the_persons_reply():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. Do you want the drafts too?")
    spoken(h, lives[0])
    await says(h, P1, "yes, the drafts too")
    (e,) = backend.route_bodies[-1]["engagements"]
    assert (e["expects"], e["last"]) == ("reply", "delivered")


# -- unfinished turns --


def test_only_a_name():
    agents = TEAM["agents"]
    assert only_name("Forge.", agents) == only_name("Um, Forge...", agents) == only_name("Hey Forge", agents) == "forge"
    assert only_name("Forge?", agents) == "forge", "the transcriber's mark decides nothing"
    assert only_name("Forge, what PRs are open?", agents) is None and only_name("Dan.", agents) is None


async def test_a_name_said_alone_is_an_unfinished_turn_joined_with_what_comes_next_into_one_request():
    """10-07 rig: "Friday." was answered "Yes, I'm here." over the question, and the question, with no name on
    it, went to another agent."""
    for name in ("Forge.", "Forge?"):
        h, room, backend, lives, clock, log = await per_person()
        await says(h, P1, name)
        assert backend.routes == [] and lives == [] and backend.sessions == [], "held: nothing answers a name"
        assert h._incomplete[P1][3] == "forge"
        clock.t += 2
        backend.route_extra = ops([("forge", "list the open PRs", "name")])
        await says(h, P1, "What PRs are open?")
        assert [r[1] for r in backend.routes] == [f"{name} What PRs are open?"], "one line, with its name"
        assert [r.agent for r in h._requests.items] == ["forge"] and len(backend.turns) == 1
        assert says_of(lives[0]) == [("say", ACK)] and len(lives) == 1, "one request, one acknowledgement"
        text = h._transcript.text()
        assert text.count(name) == 1 and "What PRs are open?" in text, "each piece on the record once"
        assert h._incomplete == {}


async def test_a_name_then_silence_is_a_call_on_and_the_agent_says_what_it_has_its_status_or_one_short_line():
    h, room, backend, lives, clock, log = await per_person()
    await says(h, P1, "Forge.")
    await pause(h, clock, h_mod.CALL_ON_S - 1)
    assert lives == []
    await pause(h, clock, 1)
    assert says_of(lives[0]) == [("say", h_mod.HERE[0])] and backend.routes == [] and backend.turns == []
    assert h._engagements(h._who(P1))[0].agent == "forge", "they are talking with it now"

    h, backend, lives, clock = await asked()  # its run still works
    await closes(h, clock)
    await says(h, P1, "Forge...")
    await pause(h, clock, h_mod.CALL_ON_S)
    assert says_of(lives[-1]) == [("say", h_mod.STILL_WORKING[0])]
    assert len(backend.turns) == 1, "no new run"

    h, backend, lives, clock = await asked()  # its answer is ready, and she had turned away
    (row,) = h._requests.items
    await says(h, P1, "Dan, did you push that fix last night?")
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await says(h, P1, "Forge?")
    clock.t += h_mod.CALL_ON_S - 0.5
    talks(h, P1)  # she is still talking: her silence has not begun
    await pause(h, clock, 0.5)
    assert says_of(lives[0]) == [("say", ACK)]
    quiet(h)
    await pause(h, clock, h_mod.CALL_ON_S)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")], "the answer, and no status line"


async def test_a_turn_someone_has_not_finished_holds_speech_until_its_speaker_has_been_silent():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    await says(h, P2, "Um, so...")  # trailing off: more is coming
    assert P2 in h._incomplete
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, h_mod.CALL_ON_S - 0.5)
    assert says_of(lives[0]) == [("say", ACK)], "Dan has not finished"
    await pause(h, clock, 0.5)
    assert says_of(lives[0])[1:] == [("say", "Three PRs are open.")]


# -- no utterance is `speaking` without a live playback behind it --


async def test_a_playback_that_fails_returns_the_utterance_and_the_room_goes_on():
    """10-07 review: the order's exception was logged in the voice, the utterance stayed `speaking`, and
    `_pump` started nothing more for anyone."""
    h, backend, lives, clock, forge, gauge = await both_working()
    answer(h, forge.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    answer(h, gauge.run_id, "gauge", "The deploy is green.")
    await pause(h, clock)
    live = lives[-1]
    assert live.agent_id == "forge" and ledger(h) == [("forge", "answer", "speaking"), ("gauge", "answer", "pending")]
    h.on_agent_said("forge", "Three PRs are open.")
    first = live.order
    h.on_playback_ended("forge", first, failed=True)  # the second sentence could not be said
    await settle(h)
    (u, _) = h._ledger
    assert (u.state, u.cursor, u.tries) == ("speaking", 1, 1) and live.order != first, "tried again, from its cursor"
    assert says_of(live)[-1] == ("say", "The oldest is two days old.")
    h.on_playback_ended("forge", first, failed=True)  # a receipt of the playback before: not this one's
    h.on_playback_ended("gauge", live.order, failed=True)
    assert u.state == "speaking" and u.tries == 1
    orders = len(says_of(live))
    for _ in range(h_mod.PLAYBACK_TRIES - 1):
        h.on_playback_ended("forge", live.order)  # over, and its unit was never reported said
        await settle(h)
    assert len(says_of(live)) == orders + h_mod.PLAYBACK_TRIES - 2, "tried a bounded number of times"
    assert [a[3] for a in backend.late_answers] == ["The oldest is two days old."], "then the rest is written"
    assert forge.status == "delivered"
    await pause(h, clock)
    assert said(lives)[-1] == ("gauge", "The deploy is green."), "and the room is not frozen: the next one speaks"
    spoken(h, lives[-1])
    assert gauge.status == "delivered" and h._ledger == []


async def test_a_receipt_for_an_utterance_that_was_said_paused_or_struck_changes_nothing():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    order = lives[0].order
    await talked_over(h, clock, P2)  # it yields: its order ends cut, and says so
    h.on_playback_ended("forge", order)
    (u,) = h._ledger
    assert (u.state, u.tries) == ("paused", 0), "the yield settled it. Its receipt is no failure"
    quiet(h)
    await pause(h, clock, BREATH)
    await h.on_final_transcript(P2, "Dan", "Mm-hm.")
    await settle(h)
    assert u.state == "speaking" and lives[0].order != order
    h.on_playback_ended("forge", order)  # the cut order's receipt, late: another playback is live now
    assert u.state == "speaking"
    spoken(h, lives[0])
    h.on_playback_ended("forge", lives[0].order)  # said to its end, then over
    assert row.status == "delivered" and h._ledger == [] and backend.late_answers == []


async def test_a_voice_that_dies_while_it_speaks_leaves_its_utterance_pending_and_the_next_voice_says_the_rest():
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    h.on_agent_said("forge", "Three PRs are open.")
    h.on_live_closed(lives[0])  # its session closed on its own, and no receipt will come from it
    assert [(u.state, u.cursor) for u in h._ledger] == [("pending", 1)], "at once: nothing is playing"
    await settle(h)
    await pause(h, clock)
    assert len(lives) == 2 and says_of(lives[1]) == [("say", "The oldest is two days old.")]
    spoken(h, lives[1])
    assert row.status == "delivered" and backend.late_answers == []

    # the order itself raises: its session is dead
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items

    def dead(text, order=None):
        raise RuntimeError("AgentSession isn't running")

    lives[0].say = dead
    answer(h, row.run_id, "forge", "Three PRs are open.")
    assert ledger(h) == [("forge", "answer", "pending")]
    await settle(h)
    await pause(h, clock)
    assert says_of(lives[-1]) == [("say", "Three PRs are open.")] and len(lives) == 2
    spoken(h, lives[-1])
    assert row.status == "delivered"


# -- yield, hush, withdraw --


def test_listening_sounds_and_the_two_kinds_of_stop():
    assert backchannel("Mm-hm, yeah.") and backchannel("Right. Okay.") and backchannel("") and backchannel("uh-huh")
    assert not backchannel("yeah but wait") and not backchannel("right, the budget") and not backchannel("stop")
    assert withdraws("Never mind.") and withdraws("Forge, cancel that") and withdraws("ok forget it")
    assert not withdraws("Stop.") and not withdraws("Forge, that's enough") and not withdraws("never mind the budget")


def test_only_a_line_that_is_nothing_but_a_stop_is_the_fast_path():
    agents = TEAM["agents"]
    assert stop_target("Forge, stop.", agents) == "forge" and stop_bare("Stop.")
    # a stop at the end of a longer turn is /route's to understand, with what it was said over
    assert not stop_request("Thanks, that is all I need! Forge, never mind.", agents)
    assert not stop_request("We tried the other vendor. Never mind.", agents)


async def test_talk_that_lasts_makes_the_mouth_yield_on_its_own_timer_and_it_resumes_from_its_cursor(monkeypatch):
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
    assert ("interrupt",) in lives[0].calls and ("close",) not in lives[0].calls, "it yields. Its voice stays open"
    (u,) = h._ledger
    assert (u.state, u.cursor, u.over) == ("paused", 1, {P2}) and row.status == "ready"
    assert not [x for x in backend.aborts], "its work is not touched"
    quiet(h)
    await says(h, P2, "sorry, one thing about the budget")  # the floor was taken: its hand is up
    await pause(h, clock, BREATH)
    assert len(says_of(lives[0])) == 2
    await pause(h, clock)
    assert says_of(lives[0])[2:] == [("say", "The oldest is two days old.")] and len(lives) == 1
    spoken(h, lives[0])
    assert row.status == "delivered"
    for _ in range(2):
        await pause(h, clock, 30)
    assert [t for _, t in said(lives) if "oldest" in t] == [
        "Three PRs are open. The oldest is two days old.", "The oldest is two days old."
    ], "the rest once, and nothing again"  # fmt: skip


async def test_a_short_sound_does_not_make_it_yield(monkeypatch):
    monkeypatch.setattr(h_mod, "TALK_OVER_S", 0.05)
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    talks(h, P2)  # a cough
    quiet(h)
    await asyncio.sleep(0.2)
    await settle(h)
    assert ("interrupt",) not in lives[0].calls and ledger(h) == [("forge", "answer", "speaking")]
    spoken(h, lives[0])
    assert row.status == "delivered"


async def test_talk_that_turns_out_to_be_only_a_backchannel_resumes_it_at_once_from_its_cursor():
    """A one-second "mm-hm, yeah" is as long as an interruption. Its words say which it was."""
    for words in ("Mm-hm, yeah.", ""):  # (no words at all: noise the speech detector took for talk)
        h, backend, lives, clock = await asked()
        (row,) = h._requests.items
        answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
        h.on_agent_said("forge", "Three PRs are open.")
        await talked_over(h, clock, P2)
        h.on_playback_ended("forge", lives[0].order)  # the cut playback is over, as a mouth reports every order
        quiet(h)
        await pause(h, clock, BREATH)
        assert len(says_of(lives[0])) == 2, "paused until the talker's words are known"
        await h.on_final_transcript(P2, "Dan", words)
        await settle(h)
        assert says_of(lives[0])[2:] == [("say", "The oldest is two days old.")], "at once, from the cursor"
        assert h._engagements(h._who(P1))[0].state == "active", "nobody took the floor"


async def test_a_stop_silences_the_agents_speech_and_leaves_its_work_running():
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
    first, second = h._requests.items
    answer(h, first.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    await settle(h)
    assert second.status == "working"
    talks(h, P1)
    await says(h, P1, "Forge, stop.")
    assert ("interrupt",) in lives[0].calls and h._ledger == [], "nothing of what it was saying resumes"
    assert second.status == "working" and backend.aborts == [], "speech was stopped, not the task"
    quiet(h)
    assert h._utter("forge", "status", h_mod.STILL_WORKING[0], obligation=second.run_id) is None, "no word on it"
    answer(h, second.run_id, "forge", "The deploy is green.")
    for _ in range(3):
        await pause(h, clock, 30)
    # 10-07 review: the stop struck what was in the ledger then, and this later answer was said at a pause
    assert h._ledger == [] and len(said(lives)) == 3, "the stop holds for what the work produces later"
    assert [a[3] for a in backend.late_answers] == [
        "Three PRs are open. The oldest is two days old.", "The deploy is green."
    ], "asked for and not withdrawn: both in writing, each once"  # fmt: skip
    assert second.status == "delivered" and backend.aborts == []

    # the same from /route, for a stop inside a longer turn: `ops.hush` names the agent, `ops.cancel` nothing
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
    first, second = h._requests.items
    answer(h, first.run_id, "forge", "Three PRs are open.")
    await settle(h)
    backend.route_extra = heard(hush=[{"agentId": "forge", "requestIds": []}])
    await says(h, P1, "Okay Forge, that is plenty, not now please.")
    assert ("interrupt",) in lives[0].calls and h._ledger == []
    assert second.status == "working" and backend.aborts == []


async def test_a_stop_about_one_request_silences_that_one_and_turning_to_the_agent_again_lets_it_speak():
    h, backend, lives, clock = await asked()
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    await says(h, P1, "Forge, also check the deploy.")
    spoken(h, lives[0])
    first, second = h._requests.items
    answer(h, first.run_id, "forge", "Three PRs are open.")
    await settle(h)
    assert second.status == "working" and ledger(h) == [("forge", "answer", "speaking")]
    # "stop talking about the deploy": /route names the request, and withdraws nothing
    backend.route_extra = heard(hush=[{"agentId": "forge", "requestIds": [second.id]}])
    await says(h, P2, "Forge, stop talking about the deploy.")
    assert ledger(h) == [("forge", "answer", "speaking")], "what it is saying is about something else"
    assert second.status == "working" and backend.aborts == [], "the run is alive"
    spoken(h, lives[0])
    assert first.status == "delivered" and backend.late_answers == []
    # ..."never mind the deploy" is the other thing: `ops.cancel` aborts it (the withdrawal test below)
    # they turn to it again before the work ends: it speaks about it after all
    backend.route_extra = heard(called="forge")
    await says(h, P1, "Forge, go ahead.")
    answer(h, second.run_id, "forge", "The deploy is green.")
    await pause(h, clock)
    assert says_of(lives[-1])[-1] == ("say", "The deploy is green.") and backend.late_answers == []

    # nobody turns to it: the answer of the silenced request is written, and nothing of it is said
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = heard(hush=[{"agentId": "forge", "requestIds": [row.id]}])
    await says(h, P1, "Okay, enough about the PRs for now.")
    assert row.status == "working" and backend.aborts == []
    backend.route_extra = heard()
    answer(h, row.run_id, "forge", "Three PRs are open.")
    for _ in range(3):
        await pause(h, clock, 30)
    assert [c for live in lives for c in says_of(live)] == [("say", ACK)]
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."] and row.status == "delivered"

    # a new request to the agent releases it too, and a stop on the same line does not release itself
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = ops([("forge", "check the deploy", "name")])
    backend.route_extra["ops"]["hush"] = [{"agentId": "forge", "requestIds": []}]
    await says(h, P1, "Forge, stop, check the deploy instead.")
    new = h._requests.items[-1]
    assert h._hushed(row.id) and not h._hushed(new.id), "what was open is silenced, what the line asks is not"
    backend.route_extra = ops([("forge", "list the branches", "name")])
    await says(h, P1, "Forge, list the branches too.")
    assert not h._hushed(row.id), "asked something new: they want to hear it again"


async def test_a_withdrawal_cancels_the_task_and_aborts_its_run():
    h, backend, lives, clock = await asked(acked=False)  # its acknowledgement is being said
    (row,) = h._requests.items
    talks(h, P1)
    backend.route_extra = heard(cancel=[row.id])
    await says(h, P1, "Never mind.")  # said over it, and /route names the request: it is withdrawn
    assert row.status == "cancelled" and backend.aborts == [row.run_id] and h._ledger == []
    assert ("interrupt",) in lives[0].calls

    h, backend, lives, clock = await asked()  # it is working, and silent
    (row,) = h._requests.items
    backend.route_extra = heard(cancel=[row.id])
    await says(h, P2, "Forge, cancel that.")
    assert row.status == "cancelled" and backend.aborts == [row.run_id]

    # /route could not say which request "that" is: the agent is silenced, and no run is aborted
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_extra = heard(hush=[{"agentId": "forge", "requestIds": []}])
    await says(h, P2, "Forge, cancel that.")
    assert row.status == "working" and backend.aborts == [] and h._hushed(row.id)
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await pause(h, clock, 30)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."], "not withdrawn: in writing"

    # an answer cut by a withdrawal /route did not confirm is not lost: it is written
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    await settle(h)
    talks(h, P1)
    backend.route_extra = heard()
    await says(h, P1, "Never mind.")
    assert h._ledger == [] and backend.aborts == []
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."]


async def test_a_named_stop_to_a_silent_agent_leaves_its_run_alive_and_its_answer_is_written():
    """10-07 review: "Forge, stop" aborted Forge's run when Forge was saying nothing. Silence is not a
    withdrawal."""
    h, backend, lives, clock = await asked()  # it is working, and silent
    (row,) = h._requests.items
    routes = len(backend.routes)
    await says(h, P2, "Forge, stop.")
    assert row.status == "working" and backend.aborts == [] and len(backend.routes) == routes
    answer(h, row.run_id, "forge", "Three PRs are open.")
    for _ in range(3):
        await pause(h, clock, 30)
    assert h._ledger == [] and len(said(lives)) == 1, "its answer is not said"
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open."] and row.status == "delivered"

    h, backend, lives, clock = await asked()  # /route's `ops.cancel` names the request
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    backend.route_extra = heard(cancel=[row.id])
    await says(h, P1, "Actually Forge, forget the PR list, we do not need it.")
    await settle(h)
    assert row.status == "cancelled" and h._ledger == [] and backend.late_answers == [], "neither said nor posted"


async def test_a_stop_is_for_the_utterance_its_speaker_talked_over_and_nobody_elses():
    h, room, backend, lives, clock, log = await per_person()
    await h.on_participants({**MEET_PEOPLE, P3: "Lee"})
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    await says(h, P1, "Forge, what PRs are open?")
    spoken(h, lives[0])
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    await talked_over(h, clock, P2)  # Dan talks over it, and it yields
    quiet(h)
    await h.on_final_transcript(P3, "Lee", "Stop.")  # Lee, to someone else: he talked over nothing
    await settle(h)
    assert ledger(h) == [("forge", "answer", "paused")] and backend.late_answers == []
    await says(h, P2, "Stop.")  # the one who interrupted it: it is for what he interrupted
    assert h._ledger == [] and backend.aborts == []
    for _ in range(3):
        await pause(h, clock, 30)
    assert len(says_of(lives[0])) == 2 and len(lives) == 1, "nothing of it is said again"


async def test_a_stop_after_another_sentence_silences_and_opens_no_voice_and_writes_no_reply():
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
    backend.route_extra = heard("forge")
    await says(h, P1, "Perfect. Forge, stop.")
    assert h._hushed(row.id) and row.status == "working" and backend.aborts == [], "silenced, not aborted"
    assert (len(backend.routes), len(backend.sessions), len(lives), written) == (routes, sessions, 1, [])
    await says(h, P2, "Perfect Forge, stop")  # no comma where the sentence ends: the line still names one agent
    await pause(h, clock, 30)
    assert (len(backend.routes), len(backend.sessions), len(lives), written) == (routes, sessions, 1, [])


async def test_talk_that_breaks_for_a_moment_is_one_stretch_of_talk():
    """The SFU's level drops between words: each word would start the count again and never reach TALK_OVER_S."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open.")
    for _ in range(3):
        talks(h, P2)
        clock.t += 0.2
        h.on_active_speakers(set())
        clock.t += 0.2
    talks(h, P2)
    await h.tick()
    await settle(h)
    assert ("interrupt",) in lives[0].calls and ledger(h) == [("forge", "answer", "paused")]


async def test_a_request_its_voice_could_not_start_is_said_as_its_outcome_once_the_acknowledgement_is_done():
    from huddle_agent.backend import BackendError

    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    backend.turn_results = [BackendError(409, "agent_not_in_room", None)]
    backend.turn_gate = asyncio.Event()  # the refusal comes after the line is applied, as a network answer does
    await h.on_final_transcript(P1, "Priya", "Forge, what PRs are open?")
    for _ in range(30):
        await asyncio.sleep(0)
    backend.turn_gate.set()
    await settle(h)
    (row,) = h._requests.items
    assert row.status == "failed" and ledger(h) == [("forge", "ack", "speaking"), ("forge", "answer", "pending")]
    spoken(h, lives[0])
    assert says_of(lives[0]) == [("say", ACK), ("say", h_mod.COULD_NOT[0])]


async def test_a_bare_stop_with_no_agent_speaking_stops_nobodys_work_and_a_bare_never_mind_only_the_speakers_own():
    h, backend, lives, clock, forge, gauge = await both_working()  # Priya waits on Forge, Dan on Gauge
    await says(h, P1, "Stop.")  # to a colleague: nothing is being said to her
    assert (forge.status, gauge.status) == ("working", "working") and backend.aborts == []
    backend.route_extra = heard(cancel=[forge.id])  # /route names the request she takes back
    await says(h, P1, "Never mind.")  # she withdraws what she is waiting on, and nothing of Dan's
    assert (forge.status, gauge.status) == ("cancelled", "working") and backend.aborts == [forge.run_id]
    await h.on_participants({**MEET_PEOPLE, P3: "Lee"})
    await h.on_final_transcript(P3, "Lee", "Never mind.")  # Lee is waiting on nobody: people talking
    await settle(h)
    assert gauge.status == "working"
