"""The per-person turn state the scheduler reads, and the permission a mouth plays under.

- A person's speech is an `Epoch`: speaking, awaiting its final, awaiting its route, resolved. Nothing starts
  while any person has one open (`Huddle._turn_open`).
- A mouth asks for permission before every sentence unit (`Huddle.may_say`), and a receipt says what went to
  the track (`Huddle.on_unit`).
- An exchange lives on what was really played, and a line owed to a moment is dropped when the moment passed.

From the 10-08 rig run of 15 scenarios."""

import asyncio
import json

from huddle_agent import agent
from huddle_agent import huddle as h_mod
from test_agent import _driven, _settle
from test_huddle import answer, hand, ops, pause, settle, spoken, talks
from test_meet_exchange import asked, closes, quiet, says_of
from test_meeting_mode import P1, P2, per_person, says
from test_speech_gate import ACK, BREATH, heard, ledger, said

ANSWER = "Three PRs are open."


def turns(h, who=P2):
    return [e.state for e in h._turns.get(who, [])]


async def ready(talking=True):
    """Priya asked Forge and is waiting. Dan is talking when the answer lands."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    if talking:
        talks(h, P2)
    answer(h, row.run_id, "forge", ANSWER)
    return h, backend, lives, clock, row


# -- epochs --


async def test_a_final_resolves_only_the_speech_it_covers():
    """A person's final comes about two seconds after they stop. By then they may be saying the next sentence."""
    h, backend, lives, clock, row = await ready()
    h.on_speech(P2, False)  # the first sentence ends
    clock.t += 0.5
    h.on_speech(P2, True)  # and they go on
    assert turns(h) == ["awaiting-final", "speaking"]
    await says(h, P2, "Mm-hm.")  # the first sentence's words
    assert turns(h) == ["speaking"], "the final closed the speech it was the transcript of, and only that"
    await pause(h, clock, BREATH)
    assert says_of(lives[0]) == [("say", ACK)], "they are still talking"
    quiet(h)
    await pause(h, clock, BREATH)
    assert turns(h) == ["awaiting-final"] and says_of(lives[0]) == [("say", ACK)], "its words are not here yet"
    await says(h, P2, "Yeah.")
    await pause(h, clock, BREATH)
    assert turns(h) == [] and says_of(lives[0])[1:] == [("say", ANSWER)]


async def at_the_router():
    """Priya asked Forge and is waiting. Dan's line is at /route when the answer lands in a quiet room."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    backend.route_gate, backend.route_extra = asyncio.Event(), heard(people=True)
    await h.on_final_transcript(P2, "Dan", "One thing about the budget, Priya.")
    for _ in range(20):
        await asyncio.sleep(0)
    assert turns(h) == ["awaiting-route"]
    answer(h, row.run_id, "forge", ANSWER)
    return h, backend, lives, clock


async def test_an_old_route_reply_does_not_clear_newer_speech():
    h, backend, lives, clock = await at_the_router()
    talks(h, P2)  # he goes on while /route decides
    quiet(h)
    backend.route_gate.set()
    await settle(h)
    assert turns(h) == ["awaiting-final"], "the reply resolved the line it was for. What he said since is open"
    await pause(h, clock, BREATH)
    assert hand(h, "forge") == [ANSWER] and says_of(lives[0]) == [("say", ACK)]


async def test_nothing_starts_while_a_line_is_at_the_router_however_long_that_takes():
    """An answer that is not here is never taken for a yes: FINAL_WAIT_S bounds a missing final, nothing else."""
    h, backend, lives, clock = await at_the_router()
    clock.t += 3 * h_mod.FINAL_WAIT_S
    await h.tick()
    for _ in range(20):
        await asyncio.sleep(0)
    assert says_of(lives[0]) == [("say", ACK)] and turns(h) == ["awaiting-route"], "the room is quiet, and it waits"
    backend.route_gate.set()
    await settle(h)
    await pause(h, clock, BREATH)  # (its voice closed meanwhile: one opens for it)
    assert turns(h) == [] and said(lives)[1:] == [("forge", ANSWER)], "resolved: the room has paused long since"


async def test_a_line_is_registered_before_anything_is_pumped_and_resolved_only_after_its_effects():
    """10-08 rig: speech restarted the instant a person's final arrived, before the router had judged the line."""
    h, backend, lives, clock, row = await ready(talking=False)
    clock.t += h_mod.MEET_QUIET_S  # (the answer is being said: Dan talks over it)
    talks(h, P2)
    clock.t += h_mod.TALK_OVER_S
    await h.tick()
    assert ledger(h) == [("forge", "answer", "paused")]
    quiet(h)
    clock.t += BREATH
    backend.route_gate, backend.route_extra = asyncio.Event(), heard(people=True)
    await h.on_final_transcript(P2, "Dan", "Priya, did you push that fix last night?")
    for _ in range(20):
        await asyncio.sleep(0)
    await h.tick()
    assert ledger(h) == [("forge", "answer", "paused")], "his words are here, and what they mean is not"
    backend.route_gate.set()
    await settle(h)
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.why) == ("suspended", "other"), "the router's answer is applied first"
    await pause(h, clock, BREATH)
    assert ledger(h) == [("forge", "answer", "paused")], "so the answer keeps its hand up, as that answer says"
    await pause(h, clock)
    assert says_of(lives[0])[-1] == ("say", ANSWER)


async def test_speech_that_goes_on_is_never_timed_out_and_a_missing_final_takes_the_degraded_path(capsys):
    h, backend, lives, clock, row = await ready()
    for _ in range(4):
        clock.t += h_mod.FINAL_WAIT_S
        await h.tick()
        await settle(h)
    assert turns(h) == ["speaking"] and says_of(lives[0]) == [("say", ACK)], "talk that goes on holds, however long"
    quiet(h)
    await pause(h, clock, h_mod.FINAL_WAIT_S - 0.5)
    assert turns(h) == ["awaiting-final"] and says_of(lives[0]) == [("say", ACK)]
    capsys.readouterr()
    await pause(h, clock, 0.6)
    assert turns(h) == [] and h._unknown_at > 0, "no transcript came: closed as unknown human speech"
    out = capsys.readouterr().out
    assert '"live.turn.degraded": 1' in out and '"why": "final_timeout"' in out, "named and counted"
    assert said(lives)[1:] == [("forge", ANSWER)], "the answer its asker is waiting for goes"


async def test_after_speech_nobody_understood_a_line_waits_for_a_real_pause():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    talks(h, P2)
    await says(h, P1, "Forge, what PRs are open?")
    assert ledger(h) == [("forge", "ack", "pending")]
    clock.t += 1
    quiet(h)  # Dan stops, and no transcript of what he said ever comes
    await pause(h, clock, h_mod.FINAL_WAIT_S - 0.5)
    await says(h, P1, "Mm-hm.")  # (the room has not paused)
    await pause(h, clock, 0.6)
    assert turns(h) == [] and ledger(h) == [("forge", "ack", "pending")], "timed out, and the line is not let go"
    await pause(h, clock)
    assert says_of(lives[0]) == [("say", ACK)], "said at the pause"


async def test_a_sound_the_sfu_never_heard_holds_nothing_until_it_has_words():
    h, backend, lives, clock, row = await ready(talking=False)
    clock.t += h_mod.MEET_QUIET_S
    spoken(h, lives[0])
    answer(h, row.run_id, "forge", "The oldest is two days old.")
    h.on_speech(P2, True)  # the detector alone: no sound from him at the SFU
    assert turns(h) == ["speaking"] and h._speaking == set()
    h.on_speech(P2, False)
    await pause(h, clock, BREATH)
    assert ("say", "The oldest is two days old.") in lives[0].calls, "not credible: it holds nothing"


# -- who is talking, and the pause --


async def test_a_turn_said_in_pieces_is_one_turn_and_the_mouth_pauses_within_the_debounce_of_its_first_sign():
    """10-08 rig: three talk-overs never made the mouth yield. Each piece of a sentence started the count
    again, and no piece alone lasted long enough."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", ANSWER)
    talks(h, P2)  # "Oh wait,"
    clock.t += 0.3
    quiet(h)
    await h.tick()
    assert ledger(h) == [("forge", "answer", "speaking")], "a short sound pauses nothing"
    clock.t += 0.4
    talks(h, P2)  # "sorry, one more thing": the same turn, begun 0.7 s ago
    await h.tick()
    assert ledger(h) == [("forge", "answer", "paused")] and ("interrupt",) in lives[0].calls
    assert h_mod.TALK_OVER_S <= 0.5, "the debounce is short: detection and the interrupt come on top of it"


async def test_the_sfus_level_may_drop_between_words_but_a_stuck_detector_holds_nobody_for_long():
    h, backend, lives, clock, row = await ready()
    h.on_active_speakers(set())  # the level drops between two words. The transcriber still hears him
    clock.t += h_mod.LEVEL_FRESH_S - 0.1
    h._talking_changed()
    assert h._speaking == {P2}, "still talking"
    clock.t += 0.2
    h._talking_changed()
    assert h._speaking == set(), "no sound from him this long: the detector alone is nobody talking"


async def test_a_room_of_live_voices_keeps_the_plain_rule_sound_and_speech_together():
    from test_huddle import summoned

    h, room, backend, lives, clock, log = await summoned()
    talks(h, "u2")
    h.on_active_speakers(set())
    assert h._speaking == set()


# -- permission --


async def test_the_second_sentence_does_not_start_over_a_person():
    """10-08 rig: an order's sentences were looped with no check, and sentence two began over a talker."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    (u,) = h._ledger
    order = lives[0].order
    assert h.may_say("forge", order, 0), "nobody is talking: the first unit may play"
    assert not h.may_say("forge", order + 1, 0) and not h.may_say("forge", order, 1), "another order or unit: no"
    h.on_unit("forge", order, 0, ANSWER, "said", {})
    assert u.cursor == 1 and h.may_say("forge", order, 1)
    talks(h, P2)  # he begins as the first sentence ends: too short yet to pause anything
    assert u.state == "speaking"
    assert not h.may_say("forge", order, 1), "asked before every unit, and refused while a person talks"
    assert (u.state, u.cursor, u.over) == ("paused", 1, {P2}) and ("interrupt",) in lives[0].calls
    assert not h.may_say("forge", order, 1), "revoked: its first audio would be refused too"
    h.on_playback_ended("forge", order)  # the order's end: nothing was left speaking
    assert ledger(h) == [("forge", "answer", "paused")]
    quiet(h)
    await says(h, P2, "Mm-hm.")  # only a listening sound: it goes on from the second sentence
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[-1] == ("say", "The oldest is two days old.") and lives[0].first == 1


async def test_a_mouth_asks_before_every_unit_and_stops_at_a_refusal():
    live, session, _ = _driven()
    live.say("One. Two. Three.", 7, 2)  # from the third unit of the worker's utterance
    await _settle()
    assert live.asked == [(7, 2)] and len(session.orders) == 1
    live.allowed = False
    session.handles[-1].set_result(None)
    await _settle()
    assert live.asked == [(7, 2), (7, 3)] and len(session.orders) == 1, "asked, refused, and nothing more ordered"
    assert live.said == [("One.", False)] and live.ended == [(7, False)]
    assert live.receipts[0][:3] == (7, 2, "said") and live.receipts[0][3]["loudMs"] == 1000


async def test_permission_is_asked_again_as_the_first_audio_is_about_to_go_out():
    """The model takes a second to start a unit. Someone may have begun talking in that second."""
    import struct

    from livekit import rtc

    mouth = agent._Mouth(instructions="x")
    frames = [rtc.AudioFrame(struct.pack("<480h", *([v] * 480)), 24000, 1, 480) for v in (0, 4000, -4000)]

    async def audio():
        for f in frames:
            yield f

    mouth.unit = agent._Unit(7, 0, lambda: True)
    assert len([f async for f in mouth.realtime_audio_output_node(audio(), None)]) == 3
    assert (mouth.unit.frames, round(mouth.unit.seconds, 2), round(mouth.unit.loud, 2)) == (3, 0.06, 0.04)
    mouth.unit = agent._Unit(7, 0, lambda: False)
    assert [f async for f in mouth.realtime_audio_output_node(audio(), None)] == [], "nothing reaches the track"
    assert mouth.unit.revoked and mouth.unit.frames == 0


# -- receipts --


async def test_a_unit_that_ends_with_no_sound_on_the_track_is_not_said_and_fails_its_order():
    """10-08 rig: "Got it." was reported said twice, and neither time was there any sound."""
    live, session, _ = _driven()
    session.silent = True
    live.say("Got it. Give me a moment.", 3)
    await _settle()
    session.handles[-1].set_result(None)
    await _settle()
    assert live.said == [("Got it.", True)] and live.receipts[0][:3] == (3, 0, "silent")
    assert live.ended == [(3, True)] and len(session.orders) == 1, "failed, and the next unit was not said"


async def test_a_silent_receipt_moves_no_cursor_and_the_unit_is_tried_again():
    h, backend, lives, clock, row = await ready(talking=False)
    clock.t += h_mod.MEET_QUIET_S
    await h.tick()
    (u,) = h._ledger
    first = lives[0].order
    h.on_unit("forge", first, 0, ANSWER, "silent", {"frames": 40, "audioMs": 4000, "loudMs": 0})
    assert (u.cursor, u.state) == (0, "speaking") and row.status == "ready", "frames are not sound: nothing was said"
    h.on_playback_ended("forge", first, True)
    assert says_of(lives[0])[1:] == [("say", ANSWER)] * 2 and lives[0].order == first + 1 and u.tries == 1
    h.on_unit("forge", first, 0, ANSWER, "said", {})
    assert u.cursor == 0, "a receipt moves only the playback it names"
    h.on_unit("forge", first + 1, 0, ANSWER, "said", {"loudMs": 900})
    assert h._ledger == [] and row.status == "delivered"


# -- the exchange and the lines owed to it --


async def test_an_exchange_survives_a_long_answer_and_the_follow_up_routes_at_once():
    """10-08 rig: the exchange was ended for its age as a long answer finished, and "and what are they called?"
    waited out the unclaimed-question delay."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    clock.t += h_mod.ENGAGE_GAP_S + 5  # the work took long
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    await h.tick()
    await settle(h)
    assert says_of(lives[-1])[-1] == ("say", "Three PRs are open. The oldest is two days old.")
    clock.t += h_mod.ENGAGE_GAP_S  # and it is a long answer
    (e,) = h._engagements(h._who(P1))
    assert e is not None, "never ended while its agent is speaking to its person"
    spoken(h, lives[-1])
    (e,) = h._engagements(h._who(P1))
    assert (e.state, e.last, e.at) == ("active", "delivered", clock.t), "refreshed by what was played"
    await closes(h, clock)
    routes = len(backend.routes)
    await says(h, P1, "And what are they called?")
    assert len(backend.routes) == routes + 1 and h._unclaimed is not None, "at /route now, not after the delay"
    assert backend.route_bodies[-1]["engagements"][0]["agentId"] == "forge"


async def test_an_acknowledgement_is_dropped_once_someone_else_has_taken_a_turn():
    """10-08 rig: "Yes, I'll have that for you shortly." was said after someone else's six second turn."""
    h, room, backend, lives, clock, log = await per_person()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    talks(h, P2)  # Dan starts as Priya finishes her question
    await says(h, P1, "Forge, what PRs are open?")
    backend.route_extra = heard()
    assert ledger(h) == [("forge", "ack", "pending")] and says_of(lives[0]) == []
    clock.t += 6
    quiet(h)
    await says(h, P2, "That was what I was just doing, I have never seen anything like it.")
    await pause(h, clock, BREATH)
    assert h._ledger == [] and says_of(lives[0]) == [], "the moment it was owed to has passed"
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", ANSWER)
    await pause(h, clock)
    assert says_of(lives[0]) == [("say", ANSWER)], "the answer is what is said"


async def test_a_status_line_is_dropped_when_its_person_has_turned_to_someone_else():
    h, backend, lives, clock = await asked()
    talks(h, P2)
    say = {"agentId": "forge", "kind": "status", "task": "list the open PRs", "text": None}
    backend.route_extra = heard("forge", say=say)
    await says(h, P1, "Forge, how is it going?")
    assert ledger(h) == [("forge", "status", "pending")]
    await says(h, P1, "Dan, did you push that fix last night?")  # she is not talking with it now
    quiet(h)
    await says(h, P2, "Mm-hm.")
    await pause(h, clock)
    assert h._ledger == [] and says_of(lives[0]) == [("say", ACK)], "obsolete, and never said"


# -- the voice is prepared beside the router, and speaks only after it --


async def test_a_voice_is_prepared_while_the_router_decides_and_says_nothing_before_it_has():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops([("forge", "list the open PRs", "name")])
    task = asyncio.create_task(says(h, P1, "Forge, what PRs are open?"))
    for _ in range(30):
        await asyncio.sleep(0)
    assert len(lives) == 1, "its voice is open before /route has answered"
    assert h._voice.live is None and lives[0].calls == [] and h._ledger == [], "and it is not the room's voice yet"
    backend.route_gate.set()
    await task
    assert h._voice.live is lives[0] and says_of(lives[0]) == [("say", ACK)], "the line is applied: now it speaks"
    assert len(lives) == 1, "the prepared voice is the one used"


async def test_a_prepared_voice_whose_line_came_to_nothing_is_closed_unused():
    h, room, backend, lives, clock, log = await per_person()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops([])  # /route: the line asked nothing of anyone
    task = asyncio.create_task(says(h, P1, "Forge."))
    for _ in range(30):
        await asyncio.sleep(0)
    backend.route_gate.set()
    await task
    assert all(c[0] != "say" for live in lives for c in live.calls)
    assert h._voice is None or h._voice.live in lives


# -- the log --


async def test_the_controller_logs_each_decision_on_one_line_with_its_ids(caplog):
    caplog.set_level("INFO", logger="huddle")
    h, backend, lives, clock, row = await ready()
    quiet(h)
    await says(h, P2, "Mm-hm.")
    await pause(h, clock, BREATH)
    order = lives[0].order
    h.may_say("forge", order, 0)
    h.on_unit("forge", order, 0, ANSWER, "said", {"frames": 40, "audioMs": 1200, "loudMs": 900})
    lines = [m.getMessage().split(" ", 2) for m in caplog.records if m.getMessage().startswith("huddle ")]
    seen = [(what, json.loads(body)) for _, what, body in lines]
    assert all(isinstance(body["t"], int) for _, body in seen), "monotonic milliseconds on every line"
    speech = [b for w, b in seen if w == "speech"]
    assert {"src": "level", "who": P2, "on": True} == {k: speech[0][k] for k in ("src", "who", "on")}
    assert [b["event"] for w, b in seen if w == "turn" and b["who"] == P2] == ["credible", "final", "resolved"]
    gate = [(b["event"], b["why"]) for w, b in seen if w == "gate" and b["kind"] == "answer"]
    assert gate == [("hold", "person"), ("hold", "final"), ("hold", "quiet"), ("start", "quiet")]
    start = next(b for w, b in seen if w == "gate" and b["event"] == "start" and b["kind"] == "answer")
    assert (start["utterance"], start["unit"]) == (h._uid, 0)
    (permit,) = [b for w, b in seen if w == "permit"]
    assert (permit["ok"], permit["order"], permit["unit"], permit["utterance"]) == (True, order, 0, h._uid)
    (receipt,) = [b for w, b in seen if w == "receipt"]
    assert (receipt["outcome"], receipt["order"], receipt["unit"], receipt["loudMs"]) == ("said", order, 0, 900)


# -- 10-09 review: one barrier for a start and for every sentence, held by every task, and never by a clock alone --


async def test_a_short_interruption_nobody_has_understood_yet_refuses_the_next_sentence():
    """Dan says a few words inside the first sentence and stops. Nobody is talking when the second is due,
    and what he said is not known: it may have been "stop". The utterance pauses and goes on once it is."""
    h, backend, lives, clock = await asked()
    (row,) = h._requests.items
    answer(h, row.run_id, "forge", "Three PRs are open. The oldest is two days old.")
    (u,) = h._ledger
    order, orders = lives[0].order, len(says_of(lives[0]))
    assert h.may_say("forge", order, 0)
    talks(h, P2)
    clock.t += h_mod.TALK_OVER_S / 2  # too short to make it yield
    quiet(h)
    h.on_unit("forge", order, 0, "Three PRs are open.", "said", {})
    assert (u.state, u.cursor) == ("speaking", 1) and h._speaking == set() and turns(h) == ["awaiting-final"]
    assert not h.may_say("forge", order, 1), "nobody is talking, and his turn is not resolved"
    assert (u.state, u.cursor, u.tries, u.over) == ("paused", 1, 0, {P2}), "paused at its cursor. Not failed"
    assert ("interrupt",) in lives[0].calls
    h.on_playback_ended("forge", order)  # the order's end: nothing was left speaking, so nothing is retried
    assert (u.state, u.tries) == ("paused", 0)
    await pause(h, clock, BREATH)
    assert turns(h) == ["awaiting-final"] and u.state == "paused", "the room is quiet, and his words are not here"
    assert len(says_of(lives[0])) == orders, "so it waits: nothing is said on speech nobody has understood"
    await says(h, P2, "Mm-hm.")  # they come, and they were only a listening sound
    assert turns(h) == [] and u in h._ledger, "resolved, and the utterance was never dropped"
    await pause(h, clock, BREATH)
    assert says_of(lives[0])[-1] == ("say", "The oldest is two days old.") and lives[0].first == 1
    assert len(says_of(lives[0])) == orders + 1 and u.tries == 0, "said on from its cursor, once"


async def two_names_at_the_router(summon):
    """Priya names two agents. /route answers one ask, so the line is dispatched after the round trip
    (`Huddle._dispatch`), by a task that starts another one: `summon` stands in for that one's work."""
    from test_huddle import make

    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    backend.route_gate, go, real = asyncio.Event(), asyncio.Event(), h.summon

    async def nested(*a, **kw):
        await go.wait()
        return await summon(real, *a, **kw)

    h.summon = nested
    await h.on_final_transcript("u1", "Prasiddha", "Forge and Gauge, what shipped?")
    for _ in range(20):
        await asyncio.sleep(0)
    assert turns(h, "u1") == ["awaiting-route"]
    (ep,) = h._turns["u1"]
    backend.route_gate.set()
    for _ in range(50):
        await asyncio.sleep(0)
    return h, lives, ep, go


async def test_a_line_that_names_two_agents_holds_its_speech_until_every_task_it_started_has_ended():
    h, lives, ep, go = await two_names_at_the_router(lambda real, *a, **kw: real(*a, **kw))
    assert (ep.state, ep.held) == ("awaiting-route", 1), "the round trip is over. The summon it started is not"
    assert lives == [] and h._turn_open(h._clock()) == ("route", None), "so nothing may be said yet"
    go.set()
    await settle(h)
    assert turns(h, "u1") == [] and ep.state == "resolved" and [live.agent_id for live in lives] == ["forge"]


async def test_a_nested_task_that_raises_still_resolves_the_speech_it_held():
    async def fails(real, *a, **kw):
        raise RuntimeError("no voice")

    h, lives, ep, go = await two_names_at_the_router(fails)
    assert (ep.state, ep.held) == ("awaiting-route", 1)
    go.set()
    for _ in range(50):  # (not `settle`: it would raise the task's error here)
        await asyncio.sleep(0)
    assert (ep.state, ep.held) == ("resolved", 0) and turns(h, "u1") == [], "a failure never holds the room"


async def test_speech_the_detector_still_hears_holds_the_floor_when_its_sound_goes_stale_then_degrades(capsys):
    """The detector's stop event can be lost. Stale sound is no proof that Dan stopped, so nothing is said
    on it. The wait is bounded, and its end is the named degraded path."""
    h, backend, lives, clock, row = await ready()
    began = clock.t
    h.on_active_speakers(set())  # the SFU hears him no more, and the detector never says he stopped
    clock.t = began + h_mod.LEVEL_FRESH_S + 0.1
    h._talking_changed()  # (the timer set when his sound stopped)
    await pause(h, clock, h_mod.MEET_QUIET_S)
    assert h._speaking == set() and turns(h) == ["speaking"], "nobody is heard, and his speech is still open"
    assert says_of(lives[0]) == [("say", ACK)] and h._turn_open(clock.t)[0] == "person", "it holds the floor"
    clock.t = began + h_mod.DETECTOR_LOST_S - 0.1
    await h.tick()
    await settle(h)
    assert turns(h) == ["speaking"] and says_of(lives[0]) == [("say", ACK)]
    capsys.readouterr()
    await pause(h, clock, 0.2)
    assert turns(h) == ["awaiting-final"] and says_of(lives[0]) == [("say", ACK)], "ended, and still not approved"
    out = capsys.readouterr().out
    assert '"live.turn.degraded": 1' in out and '"why": "detector_lost"' in out, "named and counted"
    await pause(h, clock, h_mod.FINAL_WAIT_S - 0.2)  # (its voice has said its piece, and closes meanwhile)
    assert turns(h) == ["awaiting-final"] and said(lives) == [("forge", ACK)]
    await pause(h, clock, 0.3)
    assert turns(h) == [] and h._unknown_at > 0, "no transcript came either: unknown human speech"
    assert said(lives)[1:] == [("forge", ANSWER)], "the answer its asker is waiting for goes"
