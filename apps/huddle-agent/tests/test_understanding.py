"""Understanding §4.4-§4.6 (Task B2): /route's `ops` turn a line into rows; small talk starts no run."""

import asyncio
import json

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from test_huddle import answer, frame, metric_count, ops, packet, say, settle, talks
from test_request_engine import LINE, TASKS, quiet, run_of, three

ASKS = [
    ("forge", "the open PRs", "fit"),
    ("forge", "what shipped", "fit"),
    ("friday", "Aden's email", "fit"),
]
SAID = "what are the open PRs, what shipped, and check Aden's email"


def rows(h):
    return [(r.agent, r.task, r.status) for r in h._requests.items]


async def test_small_talk_opens_the_social_agents_voice_and_starts_no_run():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(social="pulse")
    await say(h, "u1", "Hey guys, how's it going?")
    assert [x.agent_id for x in lives] == ["pulse"] and lives[0].calls[-1] == ("nudge",)
    small = 'Prasiddha said: "Hey guys, how\'s it going?". Small talk: reply yourself in one short sentence. '
    assert ("developer", small + "Do not delegate it.") in lives[0].seed
    assert backend.turns == [] and h._requests.items == [] and h._voice.driven is None
    assert (
        backend.route_bodies[0]["open"] == []
        and backend.route_bodies[0]["floor"] is None
    )


async def test_an_announced_list_opens_a_voice_that_waits():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(social="pulse", more=True)
    await say(h, "u1", "I got a couple questions, I'm just gonna say them all")
    wait = 'Prasiddha will list several requests. Say "go ahead" at most, then stay quiet until they finish.'
    assert ("developer", wait) in lives[0].seed and backend.turns == []


async def test_three_requests_with_no_voice_open_one_row_one_run_each(capsys):
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(requests=ASKS)
    await say(h, "u1", SAID)
    assert [x.agent_id for x in lives] == ["forge"] and h._voice.path == "jev"
    assert [(t["agentId"], t["utterance"]) for t in backend.turns] == [
        ("friday", "Aden's email"),
        ("forge", "the open PRs"),
    ]
    assert rows(h) == [
        ("forge", "the open PRs", "working"),
        ("forge", "what shipped", "queued"),
        ("friday", "Aden's email", "working"),
    ]
    first = h._requests.items[0]
    assert (
        first.run_id == h._voice.driven_run == backend.turns[1]["runId"]
    )  # the voice's driven run
    assert backend.turns[1]["requests"] == ['Friday is answering "Aden\'s email"']
    seed = [text for _, text in lives[0].seed]
    assert (
        "Prasiddha asked: the open PRs — already handed to your backend self; don't delegate it again"
        in seed
    )
    assert seed[-1].startswith(
        "You are on the open PRs. What shipped is next in line for you. Friday is on Aden's email."
    )
    assert metric_count(capsys, "live.request.created") == 3
    answer(h, first.run_id, "forge", "Two PRs are open.")
    await settle(h)
    assert first.status == "delivered" and h._requests.items[1].status == "working"
    assert (backend.turns[-1]["agentId"], backend.turns[-1]["utterance"]) == (
        "forge",
        "what shipped",
    )


async def test_three_requests_with_a_voice_open_for_another_speaker_start_at_once():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Pulse, how are we doing")
    pulse_run = backend.turns[0]["runId"]
    backend.turns.clear()
    backend.route_extra = ops(requests=ASKS)
    await say(h, "u1", SAID)
    assert (
        backend.sessions == ["pulse"] and h._queue == []
    )  # no summon; nothing waits for the floor
    assert sorted((t["agentId"], t["utterance"]) for t in backend.turns) == [
        ("forge", "the open PRs"),
        ("friday", "Aden's email"),
    ]
    assert [s for _, _, s in rows(h)] == ["working", "queued", "working"]
    assert backend.route_bodies[-1]["floor"] == {
        "agent": "pulse",
        "speaker": "Aden",
        "mine": False,
    }
    answer(h, pulse_run, "pulse", "We're fine.")
    for row in (
        h._requests.items
    ):  # each run ends; Forge's second row starts when its first ends
        await settle(h)
        answer(h, row.run_id, row.agent, f"{row.task} done.")
    await settle(h)
    spoken = []
    for _ in range(10):  # each answer is said by its own voice, one voice at a time
        clock.t += 30
        await h.tick()
        await settle(h)
        v = h._voice
        if v is not None and v.path == "announce" and v.live is not None:
            spoken.append(v.agent_id)
            h.on_agent_state(v.agent_id, "speaking")
            h.on_agent_state(v.agent_id, "listening")
    assert spoken == ["forge", "friday"]  # Forge says both its answers in its one turn
    assert [r.status for r in h._requests.items] == ["delivered"] * 3


async def test_two_lines_racing_for_one_agent_both_run_in_order():
    # review B2-1: a voice that opened after the line was said never heard it; every request is a row
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
    await asyncio.sleep(0)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "and what shipped")
    await asyncio.sleep(0)
    backend.route_gate.set()
    await settle(h)
    assert rows(h) == [
        ("forge", "the open PRs", "working"),
        ("forge", "what shipped", "queued"),
    ]
    answer(h, h._requests.items[0].run_id, "forge", "Two are open.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs", "what shipped"]


async def test_a_stop_said_while_route_decides_starts_nothing():
    # review B2-2: the same speaker stops a line that is still at /route
    for stop in ("everyone stop", "Forge, stop"):
        h, room, backend, lives, clock, log = await three()
        backend.route_gate = asyncio.Event()
        backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
        await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
        await asyncio.sleep(0)
        await h.on_final_transcript("u1", "Prasiddha", stop)
        backend.route_gate.set()
        await settle(h)
        assert backend.turns == [] and lives == [] and h._requests.items == []


async def test_a_stop_while_the_voice_opens_runs_nothing():
    # review B2-3: the row was cancelled before its driven run was made
    h, room, backend, lives, clock, log = await three()
    backend.session_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
    for _ in range(20):
        await asyncio.sleep(0)
    assert backend.sessions == ["forge"]
    await h.on_final_transcript("u1", "Prasiddha", "Forge, stop")
    backend.session_gate.set()
    await settle(h)
    assert backend.turns == [] and rows(h) == [("forge", "the open PRs", "cancelled")]
    assert not [
        c for c in lives[0].calls if c[0] in ("nudge", "commentary", "reply_to")
    ]
    assert not any("already handed" in text for _, text in lives[0].seed)


async def test_a_second_request_to_an_agent_working_voicelessly_waits_in_line():
    # review B2-4: one agent works on one row at a time, however its voice opens
    h, room, backend, lives, clock, log = await three()
    h.fail.add(
        "forge"
    )  # the first voice fails to open: its driven row runs without one
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await say(h, "u1", "what are the open PRs")
    h.fail.discard("forge")
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u2", "what shipped this week")
    assert [s for _, _, s in rows(h)] == ["working", "queued"] and len(
        backend.turns
    ) == 1
    assert [x.agent_id for x in lives] == ["forge"] and h._voice.driven is None
    assert lives[0].seed[-1] == (
        "developer",
        "What shipped is next in line for you. Say that in one short sentence, then wait.",
    )


async def test_a_superseded_unnamed_line_starts_nothing_a_named_one_still_starts(
    capsys,
):
    for by, started in (("fit", False), ("name", True)):
        h, room, backend, lives, clock, log = await three()
        backend.route_gate = asyncio.Event()
        backend.route_extra = ops(requests=[("forge", "the open PRs", by)])
        await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
        await asyncio.sleep(0)  # its /route call has read this answer
        backend.route_extra = ops()
        await h.on_final_transcript("u2", "Aden", "I think they're fine")
        backend.route_gate.set()
        await settle(h)
        assert bool(backend.turns) is started and bool(lives) is started
        assert len(h._requests.items) == int(started)
        assert metric_count(capsys, "live.route.superseded") == int(not started)


async def test_without_ops_todays_handling_stands():
    # the legacy `requests` split (test_request_engine)
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    assert {t["agentId"]: t["utterance"] for t in backend.turns} == TASKS
    assert (
        lives[0]
        .seed[-1][1]
        .startswith("You are on an update on the go-to-market strategy. Forge is on")
    )
    # Jev's pick is summoned with the whole line (test_huddle)
    h, room, backend, lives, clock, log = await three()
    backend.route_extra, backend.jev_to = {}, "forge"
    await say(h, "u1", "can someone check the invoices")
    assert [x.agent_id for x in lives] == ["forge"] and backend.turns[0][
        "utterance"
    ] == "can someone check the invoices"
    # with a voice open, another speaker's pick waits in the queue (test_huddle)
    backend.jev_to = "friday"
    clock.t += h_mod.THINKING_EVERY
    await say(h, "u2", "and the board too")
    assert h._queue == [("friday", "u2", "and the board too")]


async def test_a_driven_row_whose_turn_fails_is_failed_and_cant_reach_is_said_once():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("forge", "what shipped", "fit")]
    )
    # every retry of a waking workspace, until the clock passes TURN_RETRY_BUDGET
    backend.turn_results = [BackendError(503, "runtime_unavailable", None)] * 11
    await say(h, "u1", "what are the open PRs and what shipped")
    # one more wait would pass the budget: the run never started
    assert clock.t - 1_000 + h_mod.TURN_RETRY_MAX_DELAY > h_mod.TURN_RETRY_BUDGET
    assert [s for _, _, s in rows(h)] == ["failed", "working"]
    assert (
        backend.turns[-1]["utterance"] == "what shipped"
    )  # the next row starts at once
    assert (
        sum(c[:2] == ("commentary", h_mod.CANT_REACH) for c in lives[0].calls) == 1
        and h._news == {}
    )


async def test_a_stop_while_the_voice_opens_leaves_only_open_rows_in_the_acknowledgement():
    # re-review N1: the acknowledgement is built after the voice's awaits, from the rows still open
    h, room, backend, lives, clock, log = await three()
    backend.session_gate = asyncio.Event()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("friday", "Aden's email", "fit")]
    )
    await h.on_final_transcript("u1", "Prasiddha", "the open PRs and Aden's email")
    for _ in range(20):
        await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "Forge, stop")
    backend.session_gate.set()
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["cancelled", "working"]
    assert [t["agentId"] for t in backend.turns] == ["friday"]
    seed = [text for _, text in lives[0].seed]
    assert not any("open PRs" in text for text in seed)
    assert (
        seed[-1]
        == "Friday is on Aden's email. Say that in one short sentence, then wait."
    )


async def test_a_stop_delegated_by_the_floor_voice_spares_another_persons_line():
    # re-review N2: a delegated "stop" covers only its speaker's lines
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", "Forge, what shipped")
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u2", "Aden", "what are the open PRs")
    await asyncio.sleep(0)
    await h.on_delegation("forge", "d1", "stop")
    backend.route_gate.set()
    await settle(h)
    assert rows(h) == [("forge", "the open PRs", "working")]
    assert backend.turns[-1]["utterance"] == "the open PRs"


async def test_two_answers_for_one_agent_are_said_in_asked_order():
    # re-review N5: the announce seed lists an agent's answers in request order
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Pulse, how are we doing")
    answer(h, backend.turns[0]["runId"], "pulse", "We're fine.")
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("forge", "what shipped", "fit")]
    )
    await say(h, "u1", "the open PRs and what shipped")
    for row in h._requests.items:
        await settle(h)
        clock.t += 1
        answer(h, row.run_id, "forge", f"{row.task} done.")
    await settle(h)
    for _ in range(5):
        clock.t += 30
        await h.tick()
        await settle(h)
        if h._voice and h._voice.agent_id == "forge":
            break
    seed = [text for _, text in lives[-1].seed if text.startswith("You finished")]
    assert [("open PRs" in t, "what shipped" in t) for t in seed] == [
        (True, False),
        (False, True),
    ]


# -- Task B3: the floor speaker's lines are understood; a delegation binds (understanding §4.6) --

PRS = "what are the open PRs"


async def forever(_seconds):
    await asyncio.Event().wait()


async def floor_open():
    """Forge's voice is open for Prasiddha; its named summon's driven run is retired (test_huddle `summoned`)."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", "Forge, are you there")
    h._runs.abort(backend.turns[0]["runId"])
    h._voice.driven = None
    backend.turns.clear()
    backend.routes.clear()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    return h, room, backend, lives, clock, log


def bound_and_spoken(h, backend, lives):
    assert [(t["agentId"], t["utterance"]) for t in backend.turns] == [
        ("forge", "the open PRs")
    ]
    run = h._runs.get(backend.turns[0]["runId"])
    assert run.delegation_id == "deleg-1" and rows(h) == [
        ("forge", "the open PRs", "working")
    ]
    answer(h, run.run_id, "forge", "Two PRs are open.")
    assert ("commentary", "Two PRs are open.", "deleg-1") in lives[0].calls


async def test_a_delegation_binds_to_the_floor_speakers_row_whenever_it_arrives(capsys):
    # (c) after the final
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    bound_and_spoken(h, backend, lives)
    assert metric_count(capsys, "live.driven.bound") == 1
    # (b) while /route is in flight
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", PRS)
    await asyncio.sleep(0)
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-1", PRS))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not deleg.done() and backend.turns == []
    backend.route_gate.set()
    await settle(h)
    await deleg
    await settle(h)
    bound_and_spoken(h, backend, lives)
    # (a) before the final, while the speech is still being transcribed
    h, room, backend, lives, clock, log = await floor_open()
    h.on_interim_transcript("u1", "Prasiddha", "what are")
    h._sleep = forever  # the wait for the final ends only when the final comes
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-1", PRS))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not deleg.done()
    await say(h, "u1", PRS)
    await deleg
    await settle(h)
    bound_and_spoken(h, backend, lives)
    assert (
        len(backend.routes) == 1
        and metric_count(capsys, "live.delegation.unbound") == 0
    )


async def test_three_asks_from_the_floor_speaker_one_driven_run_and_the_delegation_binds():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[
            ("forge", "the open PRs", "fit"),
            ("friday", "Aden's email", "fit"),
            ("friday", "what shipped", "fit"),
        ]
    )
    await say(h, "u1", SAID)
    await h.on_delegation("forge", "deleg-1", SAID)
    await settle(h)
    assert sorted((t["agentId"], t["utterance"]) for t in backend.turns) == [
        ("forge", "the open PRs"),
        ("friday", "Aden's email"),
    ]
    assert [s for _, _, s in rows(h)] == ["working", "working", "queued"]
    assert h._runs.get(h._requests.items[0].run_id).delegation_id == "deleg-1"
    assert h._requests.items[0].run_id == h._voice.driven_run


async def test_a_second_ask_while_the_first_runs_waits_and_the_delegation_is_told_so():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await h.on_delegation("forge", "deleg-1", PRS)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    await h.on_delegation("forge", "deleg-2", "and what shipped")
    await settle(h)
    assert len(backend.turns) == 1 and [s for _, _, s in rows(h)] == [
        "working",
        "queued",
    ]
    note = '"what shipped" is next in line for you. Say so in a few words.'
    assert lives[0].calls[-1] == ("commentary", note, "deleg-2")
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["delivered", "working"]
    assert backend.turns[-1]["utterance"] == "what shipped"
    answer(h, backend.turns[-1]["runId"], "forge", "Huddles shipped.")
    spoken = "Your earlier task on what shipped finished: Huddles shipped."
    assert ("commentary", spoken, None) in lives[0].calls


async def test_asks_only_for_other_agents_are_acknowledged_with_no_floor_run():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "check Aden's email")
    await h.on_delegation("forge", "deleg-1", "check Aden's email")
    await settle(h)
    assert [t["agentId"] for t in backend.turns] == ["friday"]
    ack = "Friday is on Aden's email. Say that in one short sentence, then wait."
    assert lives[0].calls[-1] == ("commentary", ack, "deleg-1")


async def test_small_talk_from_the_floor_speaker_is_answered_by_the_voice():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(social="forge")
    await say(h, "u1", "haha that's great")
    await h.on_delegation("forge", "deleg-1", "haha that's great")
    await settle(h)
    note = "Nothing to look up for that. Reply yourself in one short sentence."
    assert backend.turns == [] and lives[0].calls[-1] == ("commentary", note, "deleg-1")
    backend.route_extra = ops(social="forge", more=True)
    await say(h, "u1", "I got a couple questions")
    await h.on_delegation("forge", "deleg-2", "I got a couple questions")
    wait = 'They will list several requests. Say "go ahead" at most, then stay quiet until they finish.'
    assert backend.turns == [] and lives[0].calls[-1] == ("commentary", wait, "deleg-2")


async def test_with_no_final_the_delegations_own_transcript_is_understood_once(capsys):
    h, room, backend, lives, clock, log = await floor_open()
    h.on_interim_transcript(
        "u1", "Prasiddha", "what are"
    )  # its speech began before the delegation
    clock.t += 0.5
    t0 = clock.t
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert (
        clock.t - t0 >= h_mod.DELEGATION_WAIT_S
    )  # it waited for the final on the injected clock
    assert [r[1] for r in backend.routes] == [PRS]
    bound_and_spoken(h, backend, lives)
    assert metric_count(capsys, "live.delegation.unbound") == 1
    await say(h, "u1", PRS)
    assert len(backend.routes) == 1 and len(h._requests.items) == 1
    assert h._transcript.text().endswith(PRS)


async def test_with_no_delegation_the_row_still_runs_and_its_answer_is_spoken():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"]
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    assert ("commentary", "Two PRs are open.", None) in lives[0].calls
    assert [s for _, _, s in rows(h)] == ["delivered"]


async def test_a_failed_route_for_a_floor_line_leaves_todays_single_run():
    for failure in ("error", "no ops"):
        h, room, backend, lives, clock, log = await floor_open()
        if failure == "error":
            backend.route_error = BackendError(503, "huddles_upstream", None)
        else:
            backend.route_extra = {}
        await say(h, "u1", PRS)
        assert (
            h._requests.items == [] and backend.turns == [] and len(backend.routes) == 1
        )
        assert not [p for topic, p, _ in room.published if topic == h_mod.ERROR_TOPIC]
        await h.on_delegation("forge", "deleg-1", PRS)
        assert [(t["utterance"], t["steerRunId"]) for t in backend.turns] == [
            (PRS, None)
        ]
        assert h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"


async def test_a_barge_is_understood_after_the_repoint():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("friday", "Aden's email", "fit")]
    )
    h.on_agent_state("forge", "speaking")
    await say(h, "u2", "the open PRs and Aden's email")
    assert h._voice.speaker == "u2" and ("set_participant", "u2") in lives[0].calls
    assert sorted(t["utterance"] for t in backend.turns) == [
        "Aden's email",
        "the open PRs",
    ]
    assert [s for _, _, s in rows(h)] == ["working", "working"]
    assert h._requests.items[0].run_id == h._voice.driven_run
    assert lives[0].calls[-1] == ("nudge",)
    # /route down: today's whole-line driven run
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_error = BackendError(503, "huddles_upstream", None)
    h.on_agent_state("forge", "speaking")
    await say(h, "u2", "the open PRs and Aden's email")
    assert [t["utterance"] for t in backend.turns] == ["the open PRs and Aden's email"]
    assert h._requests.items == [] and lives[0].calls[-1] == ("nudge",)


async def test_everyone_stop_between_the_final_and_the_delegation_runs_nothing():
    h, room, backend, lives, clock, log = await floor_open()
    backend.turn_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", PRS)
    for _ in range(5):
        await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    await h.on_delegation("forge", "deleg-1", PRS)
    backend.turn_gate.set()
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["cancelled"]
    assert len(backend.turns) == 1 and set(backend.aborts) == {
        backend.turns[0]["runId"]
    }
    assert not [c for c in lives[0].calls if c[0] == "commentary"]


async def test_a_bare_lightbulb_repoint_is_understood_not_typed():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u2", "Lightbulb, check Aden's email")
    assert h._voice.speaker == "u2" and [t["agentId"] for t in backend.turns] == [
        "friday"
    ]
    ack = "Friday is on Aden's email. Say that in one short sentence, then wait."
    assert lives[0].calls[-2:] == [("thinking", ack, None), ("nudge",)]
    assert not [c for c in lives[0].calls if c[0] == "reply_to"]
    # /route down: today's typed user turn
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(h, "u2", "Lightbulb, check Aden's email")
    assert (
        lives[0].calls[-1] == ("reply_to", "Lightbulb, check Aden's email")
        and backend.turns == []
    )


# -- review round 1: an understanding belongs to its speech --

ACK = "You are on the open PRs. Say that in one short sentence, then wait."


async def test_two_delegations_of_one_speech_make_one_run_and_the_second_gets_a_note():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await h.on_delegation("forge", "deleg-1", PRS)
    clock.t += 1
    await h.on_delegation("forge", "deleg-2", PRS)
    await settle(h)
    assert (
        len(backend.routes) == 1
        and len(h._requests.items) == 1
        and len(backend.turns) == 1
    )
    assert lives[0].calls[-1] == ("commentary", ACK, "deleg-2")
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    assert [
        c for c in lives[0].calls if c[:2] == ("commentary", "Two PRs are open.")
    ] == [("commentary", "Two PRs are open.", "deleg-1")]


async def test_a_delegation_long_after_the_answer_is_told_not_to_repeat_it():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    run_id = backend.turns[0]["runId"]
    answer(h, run_id, "forge", "Two PRs are open.")  # said before any delegation came
    clock.t += 30
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert (
        len(backend.routes) == 1
        and len(h._requests.items) == 1
        and len(backend.turns) == 1
    )
    assert h._runs.get(run_id).delegation_id is None
    assert lives[0].calls[-1] == (
        "commentary",
        "You already gave that answer. Do not repeat it.",
        "deleg-1",
    )


async def test_a_final_with_no_interim_after_an_unbound_delegation_is_new_speech_unless_it_matches():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 3
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "check Aden's email")
    assert [r[1] for r in backend.routes] == [PRS, "check Aden's email"]
    assert [(a, t) for a, t, _ in rows(h)] == [
        ("forge", "the open PRs"),
        ("friday", "Aden's email"),
    ]
    # the same words: that speech's final, arriving late
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 3
    await say(h, "u1", "What are the open PRs?")
    assert len(backend.routes) == 1 and len(h._requests.items) == 1


async def test_a_stop_before_the_delegations_answer_says_nothing():
    # the stop lands while the speech's /route is in flight
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", PRS)
    await asyncio.sleep(0)
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-1", PRS))
    await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    backend.route_gate.set()
    await settle(h)
    await deleg
    assert len(backend.routes) == 1 and backend.turns == [] and h._requests.items == []
    assert not [c for c in lives[0].calls if c[0] == "commentary"]
    # the stop is a new speech (an interim came): no record, and no case-f /route for the stopped speech
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    h.on_interim_transcript("u1", "Prasiddha", "everyone")
    await say(h, "u1", "everyone stop")
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert len(backend.routes) == 1 and len(backend.turns) == 1
    assert not [c for c in lives[0].calls if c[0] == "commentary"]


async def test_a_delegation_for_a_newer_speech_never_takes_the_older_record():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)  # never delegated
    clock.t += 5
    h.on_interim_transcript("u1", "Prasiddha", "and what")
    h._sleep = forever
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-2", "and what shipped"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not deleg.done()
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    await deleg
    await settle(h)
    first = backend.turns[0]["runId"]
    assert h._runs.get(first).delegation_id is None and len(backend.turns) == 1
    note = '"what shipped" is next in line for you. Say so in a few words.'
    assert lives[0].calls[-1] == ("commentary", note, "deleg-2")
    answer(h, first, "forge", "Two PRs are open.")
    assert ("commentary", "Two PRs are open.", None) in lives[0].calls
    # no line-2 final at all: after the wait, the delegation's own transcript is understood
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    clock.t += 5
    h.on_interim_transcript("u1", "Prasiddha", "and what")
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await h.on_delegation("forge", "deleg-2", "and what shipped")
    await settle(h)
    assert [r[1] for r in backend.routes] == [PRS, "and what shipped"]
    assert [s for _, _, s in rows(h)] == ["working", "queued"]
    assert h._runs.get(backend.turns[0]["runId"]).delegation_id is None
    assert lives[0].calls[-1] == ("commentary", note, "deleg-2")


async def test_route_has_its_own_short_timeout_and_turns_keep_the_clients(monkeypatch):
    import httpx

    from huddle_agent.backend import ROUTE_TIMEOUT_S, Backend

    monkeypatch.setenv("LIGHTBULB_API_URL", "http://backend")
    seen = {}

    def handle(request):
        seen[request.url.path.rsplit("/", 1)[-1]] = request.extensions["timeout"][
            "read"
        ]
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(
        base_url="http://backend/api/v1",
        transport=httpx.MockTransport(handle),
        timeout=100,
    )
    b = Backend("room", "owner", "token", client=client)
    await b.route("u1", "hi", "")
    await b.turn("call", "u1", "forge", "run", "hi", "")
    assert seen == {"route": ROUTE_TIMEOUT_S, "turns": 100}


# -- Task B4: a named line is understood while its voice opens (understanding §4.6) --

CI = "Forge, check CI, then tell me what shipped"
TWO = [("forge", "check CI", "name"), ("forge", "tell me what shipped", "name")]


def seeded(live):
    return [text for _, text in live.seed]


async def test_a_named_line_with_two_asks_drives_the_first_and_queues_the_second():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(requests=TWO)
    await say(h, "u1", CI)
    assert [r[1] for r in backend.routes] == [CI] and [x.agent_id for x in lives] == [
        "forge"
    ]
    assert [t["utterance"] for t in backend.turns] == [
        "check CI"
    ] and h._voice.path == "name"
    assert rows(h) == [
        ("forge", "check CI", "working"),
        ("forge", "tell me what shipped", "queued"),
    ]
    first = h._requests.items[0]
    assert (
        first.run_id == h._voice.driven_run == backend.turns[0]["runId"]
        and h._queue == []
    )
    seed = seeded(lives[0])
    assert (
        "Prasiddha asked: check CI — already handed to your backend self; don't delegate it again"
        in seed
    )
    assert "next in line" in seed[-1].lower() and lives[0].calls[-1] == ("nudge",)
    # the voice delegates the line it just heard described: it binds, never a second /turns
    await h.on_delegation("forge", "deleg-1", CI)
    await settle(h)
    assert (
        len(backend.turns) == 1 and h._runs.get(first.run_id).delegation_id == "deleg-1"
    )
    answer(h, first.run_id, "forge", "CI is green.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns] == [
        "check CI",
        "tell me what shipped",
    ]
    assert [s for _, _, s in rows(h)] == ["delivered", "working"]


async def test_a_named_greeting_starts_no_run():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(social="forge")
    await say(h, "u1", "Forge, how's it going?")
    small = 'Prasiddha said: "Forge, how\'s it going?". Small talk: reply yourself in one short sentence. '
    assert ("developer", small + "Do not delegate it.") in lives[0].seed
    assert (
        backend.turns == []
        and h._requests.items == []
        and lives[0].calls[-1] == ("nudge",)
    )
    await h.on_delegation("forge", "deleg-1", "Forge, how's it going?")
    note = "Nothing to look up for that. Reply yourself in one short sentence."
    assert backend.turns == [] and lives[0].calls[-1] == ("commentary", note, "deleg-1")


async def test_a_bare_summons_opens_the_voice_with_the_line():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops()
    await say(h, "u1", "Forge?")
    assert lives[0].seed[-1] == ("developer", 'Prasiddha said: "Forge?"')
    assert backend.turns == [] and lives[0].calls[-1] == ("nudge",)


async def test_a_named_line_with_an_ask_for_another_agent_starts_it_voicelessly():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[
            ("friday", "the numbers from finance", "fit"),
            ("forge", "check CI", "name"),
        ]
    )
    await say(h, "u1", "Forge, get the numbers from finance and check CI")
    assert sorted((t["agentId"], t["utterance"]) for t in backend.turns) == [
        ("forge", "check CI"),
        ("friday", "the numbers from finance"),
    ]
    assert [x.agent_id for x in lives] == ["forge"]
    assert h._requests.items[1].run_id == h._voice.driven_run
    ack = seeded(lives[0])[-1]
    assert "check CI" in ack and "Friday is on the numbers from finance" in ack


async def test_a_slow_route_holds_the_voice_until_it_answers():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=TWO)
    await h.on_final_transcript("u1", "Prasiddha", CI)
    for _ in range(20):
        await asyncio.sleep(0)
    assert backend.sessions == ["forge"] and backend.summons == [
        "u1"
    ]  # fetched beside /route
    assert lives == [] and backend.turns == []
    backend.route_gate.set()
    await settle(h)
    assert [t["utterance"] for t in backend.turns] == ["check CI"] and len(lives) == 1


async def test_a_named_line_route_down_or_without_ops_is_todays_whole_line_run():
    for failure in ("error", "no ops"):
        h, room, backend, lives, clock, log = await three()
        if failure == "error":
            backend.route_error = BackendError(503, "huddles_upstream", None)
        await say(h, "u1", CI)
        assert [t["utterance"] for t in backend.turns] == [
            CI
        ] and h._requests.items == []
        assert not [p for topic, p, _ in room.published if topic == h_mod.ERROR_TOPIC]
        await h.on_delegation("forge", "deleg-1", CI)
        await settle(h)
        assert len(backend.turns) == 1
        assert h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"


async def test_a_stop_from_another_speaker_while_the_summon_waits_runs_nothing():
    for stop in ("Forge, stop", "everyone stop"):
        for answer_ in (ops(requests=TWO), {}):
            h, room, backend, lives, clock, log = await three()
            backend.route_gate = asyncio.Event()
            backend.route_extra = answer_
            await h.on_final_transcript("u1", "Prasiddha", CI)
            for _ in range(5):
                await asyncio.sleep(0)
            await h.on_final_transcript("u2", "Aden", stop)
            backend.route_gate.set()
            await settle(h)
            assert backend.turns == [] and h._requests.items == [], (stop, answer_)
            assert [x.agent_id for x in lives] == ["forge"]
            assert not any("already handed" in text for text in seeded(lives[0]))


async def test_a_named_line_to_the_agent_holding_the_floor_for_another_speaker():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Forge, are you there")
    h._runs.abort(backend.turns[0]["runId"])
    h._voice.driven = None
    backend.turns.clear()
    backend.route_extra = ops(requests=TWO)
    await say(h, "u1", CI)
    assert len(lives) == 1 and h._voice.speaker == "u1"
    assert [t["utterance"] for t in backend.turns] == ["check CI"]
    assert [s for _, _, s in rows(h)] == ["working", "queued"]
    assert h._requests.items[0].run_id == h._voice.driven_run
    assert not [c for c in lives[0].calls if c[0] == "reply_to"] and lives[0].calls[
        -1
    ] == ("nudge",)
    # without ops: today's typed line
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Forge, are you there")
    backend.turns.clear()
    await say(h, "u1", CI)
    assert lives[0].calls[-1] == ("reply_to", CI) and h._requests.items == []


# -- review round 2: a delegation is matched to its speech by text --


async def test_two_delegations_with_no_final_and_an_interim_open_make_one_run():
    h, room, backend, lives, clock, log = await floor_open()
    h.on_interim_transcript("u1", "Prasiddha", "what are")
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 1
    await h.on_delegation("forge", "deleg-2", PRS)
    await settle(h)
    assert (
        len(backend.routes) == 1
        and len(h._requests.items) == 1
        and len(backend.turns) == 1
    )
    assert h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"
    assert lives[0].calls[-1] == ("commentary", ACK, "deleg-2")


async def test_a_late_final_that_adds_an_ask_is_understood():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 3
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("friday", "Aden's email", "fit")]
    )
    await say(h, "u1", "what are the open PRs and also check Aden's email")
    assert len(backend.routes) == 2 and ("friday", "Aden's email") in [
        (a, t) for a, t, _ in rows(h)
    ]
    # documents item 5: a backend that lists the delegated ask again makes it a second row (queued behind the first)
    assert [s for a, t, s in rows(h) if t == "the open PRs"] == ["working", "queued"]


async def test_a_delegation_before_a_final_with_no_interim_never_binds_the_older_line():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)  # never delegated
    clock.t += 5
    h._sleep = forever
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-2", "and what shipped"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not deleg.done()
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    await deleg
    await settle(h)
    assert (
        len(backend.turns) == 1
        and h._runs.get(backend.turns[0]["runId"]).delegation_id is None
    )
    note = '"what shipped" is next in line for you. Say so in a few words.'
    assert lives[0].calls[-1] == ("commentary", note, "deleg-2")
    # no line-2 final at all: after the wait, its own transcript makes its row
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    clock.t += 5
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await h.on_delegation("forge", "deleg-2", "and what shipped")
    await settle(h)
    assert [r[1] for r in backend.routes] == [PRS, "and what shipped"]
    assert [s for _, _, s in rows(h)] == ["working", "queued"]
    assert h._runs.get(backend.turns[0]["runId"]).delegation_id is None
    assert lives[0].calls[-1] == ("commentary", note, "deleg-2")


async def test_a_delegation_of_an_older_speech_while_a_newer_one_is_said_runs_nothing_twice():
    # N2
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    h.on_interim_transcript("u1", "Prasiddha", "and what")
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 3
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    assert [r[1] for r in backend.routes] == [PRS, "and what shipped"] and len(
        backend.turns
    ) == 1
    assert [(t, s) for _, t, s in rows(h)] == [
        ("the open PRs", "working"),
        ("what shipped", "queued"),
    ]
    assert h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"


async def test_an_older_route_finishing_last_does_not_hide_the_newer_line():
    # N3. The older line is applied first all the same: one speaker's lines start in said order (Codex P1).
    h, room, backend, lives, clock, log = await floor_open()
    older = backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", PRS)
    await asyncio.sleep(0)
    backend.route_gate = None
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "and what shipped")
    await asyncio.sleep(0)
    older.set()
    await settle(h)
    assert [(t, s) for _, t, s in rows(h)] == [
        ("the open PRs", "working"),
        ("what shipped", "queued"),
    ]
    await h.on_delegation("forge", "deleg-2", "and what shipped")
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert (
        len(backend.turns) == 1
        and h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"
    )
    note = '"what shipped" is next in line for you. Say so in a few words.'
    assert ("commentary", note, "deleg-2") in lives[0].calls


async def test_a_named_summons_delegation_binds_its_own_run_not_the_old_voices_record():
    # N1
    for with_ops in (True, False):
        h, room, backend, lives, clock, log = await floor_open()
        await say(h, "u1", PRS)
        backend.route_extra = (
            ops(requests=[("friday", "summarize my rooms", "name")]) if with_ops else {}
        )
        await say(h, "u1", "Friday, summarize my rooms")
        assert h._voice.agent_id == "friday"
        run_id = h._voice.driven.run_id
        await h.on_delegation("friday", "deleg-f", "Friday, summarize my rooms")
        await settle(h)
        assert h._runs.get(run_id).delegation_id == "deleg-f", with_ops
        assert not [c for c in lives[-1].calls if c[0] == "commentary"]


async def test_a_participant_who_leaves_takes_their_records():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert h._understood.get("u1") and "u1" in h._unbound
    await h.on_participants({"u2": "Aden"})
    assert (
        "u1" not in h._understood
        and "u1" not in h._unbound
        and "u1" not in h._final_heard
    )


async def test_a_named_line_after_the_wake_word_reaches_route_without_it():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "Lightbulb, Forge, check CI")
    assert backend.routes[-1][1] == "Forge, check CI" and backend.route_humans[-1] == 2


# -- Task B4 review 1 --


async def forge_for_aden(line="Forge, are you there"):
    """Forge's voice is open for Aden (u2) on a whole-line driven run (its /route answered without ops)."""
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u2", line)
    return h, room, backend, lives, clock, log


async def test_a_stop_while_a_repoint_waits_types_nothing_into_the_voice():
    h, room, backend, lives, clock, log = await forge_for_aden()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", CI)
    for _ in range(5):
        await asyncio.sleep(0)
    await h.on_final_transcript("u2", "Aden", "everyone stop")
    backend.route_gate.set()
    await settle(h)
    assert not [c for c in lives[0].calls if c[0] == "reply_to"]


async def test_a_repoint_releases_the_floor_lock_while_route_answers():
    h, room, backend, lives, clock, log = await forge_for_aden()
    h._runs.abort(backend.turns[0]["runId"])  # Forge is free for the new rows
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=TWO)
    await h.on_final_transcript("u1", "Prasiddha", CI)
    for _ in range(5):
        await asyncio.sleep(0)
    assert h._voice.speaker == "u1" and not h._lock.locked()
    backend.route_gate.set()
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["working", "queued"]


async def test_a_rowless_driven_run_keeps_its_agent_busy_and_its_delegation():
    h, room, backend, lives, clock, log = await forge_for_aden(
        "Forge, how is revenue looking"
    )
    aden_run = backend.turns[0]["runId"]
    backend.route_extra = ops(requests=TWO)
    await say(h, "u1", CI)
    assert [t["runId"] for t in backend.turns] == [aden_run]  # one Forge run at a time
    assert [s for _, _, s in rows(h)] == [
        "queued",
        "queued",
    ] and h._voice.driven_run == aden_run
    await h.on_delegation("forge", "deleg-1", "Forge, how is revenue looking")
    await settle(h)
    assert h._runs.get(aden_run).delegation_id == "deleg-1" and len(backend.turns) == 1
    answer(h, aden_run, "forge", "Revenue is up.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns[1:]] == ["check CI"]


async def test_a_raise_in_the_repoint_leaves_no_understanding_pending(monkeypatch):
    for where in ("_drive_request", "_apply"):  # after the record is made, and before
        h, room, backend, lives, clock, log = await forge_for_aden()
        h._runs.abort(backend.turns[0]["runId"])
        backend.route_extra = ops(requests=TWO)

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(h, where, boom)
        await h.on_final_transcript("u1", "Prasiddha", CI)
        while h._tasks:
            await asyncio.gather(*list(h._tasks), return_exceptions=True)
            await asyncio.sleep(0)
        assert not h._understanding.get("u1") and not h._lock.locked(), where


async def test_a_refused_voice_still_runs_every_understood_request():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "check CI", "name"), ("friday", "the numbers", "fit")]
    )

    async def refused(agent_id):
        raise BackendError(409, "agent_not_in_room", None)

    backend.session = refused
    await say(h, "u1", "Forge, check CI and get the numbers")
    assert lives == [] and [
        p["code"] for t, p, _ in room.published if t == h_mod.ERROR_TOPIC
    ] == ["agent_not_in_room"]
    assert sorted((t["agentId"], t["utterance"]) for t in backend.turns) == [
        ("forge", "check CI"),
        ("friday", "the numbers"),
    ]
    assert [s for _, _, s in rows(h)] == ["working", "working"]


# -- Task B5: one acknowledgement per turn; cancel and steer; a held line is never lost (understanding §4.4-§4.6) --

WAIT = "Say that in one short sentence, then wait."


def acks(live):
    return [c[1] for c in live.calls if c[0] == "thinking" and c[1].endswith(WAIT)]


async def test_two_finals_get_one_acknowledgement_after_the_quiet_second_of_open_rows_only():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("friday", "Aden's email", "fit")]
    )
    await say(h, "u1", "what are the open PRs and check Aden's email")
    clock.t += 0.5
    await h.tick()
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    assert [s for _, _, s in rows(h)] == ["working", "working", "queued"]
    await quiet(h, clock, 0.5)
    assert acks(lives[0]) == []  # still within the quiet second of the second final
    answer(
        h, h._requests.items[0].run_id, "forge", "Two PRs are open."
    )  # said: no longer open
    await settle(h)
    await quiet(h, clock, 0.5)
    assert acks(lives[0]) == [
        "You are on what shipped. Friday is on Aden's email. " + WAIT
    ]
    i = next(
        i
        for i, c in enumerate(lives[0].calls)
        if c[0] == "thinking" and c[1].endswith(WAIT)
    )
    assert lives[0].calls[i + 1] == ("nudge",)
    await quiet(h, clock, 2)
    assert len(acks(lives[0])) == 1


async def test_a_single_own_working_row_gets_no_acknowledgement():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await quiet(h, clock, 2)
    assert acks(lives[0]) == [] and [s for _, _, s in rows(h)] == ["working"]


async def test_a_row_queued_behind_the_floor_agents_first_is_next_in_line_once():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await quiet(h, clock, 2)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    await quiet(h, clock, 2)
    await quiet(h, clock, 2)
    assert acks(lives[0]) == ["What shipped is next in line for you. " + WAIT]


async def test_a_delegation_answered_with_the_acknowledgement_is_not_acknowledged_again():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "check Aden's email")
    await h.on_delegation("forge", "deleg-1", "check Aden's email")
    await quiet(h, clock, 2)
    assert acks(lives[0]) == [] and lives[0].calls[-1][0] == "commentary"


async def test_cancel_of_a_working_row_aborts_it_and_its_answer_is_never_said_or_posted(
    capsys,
):
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    run_id = backend.turns[0]["runId"]
    backend.route_extra = ops(cancel=["r1"])
    await say(h, "u1", "forget the PRs")
    assert backend.aborts == [run_id] and rows(h) == [
        ("forge", "the open PRs", "cancelled")
    ]
    assert lives[0].calls[
        -3:
    ] == [  # its voice was saying it: cut, as a stop cuts it (review B5-1)
        ("interrupt",),
        ("thinking", 'Forge dropped: "the open PRs".', None),
        ("nudge",),
    ]
    n = len(lives[0].calls)
    answer(h, run_id, "forge", "Two PRs are open.")
    await settle(h)
    await quiet(h, clock, 2)
    assert not [c for c in lives[0].calls[n:] if c[0] == "commentary"]
    await h.clear_presence()
    assert backend.late_answers == [] and len(backend.turns) == 1
    assert metric_count(capsys, "live.request.cancelled") == 1


async def test_cancel_of_a_queued_row_never_runs_it_and_drops_its_ready_answer():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    backend.route_extra = ops(cancel=["r2", "r9"])  # an unknown id is ignored
    await say(h, "u1", "skip the shipped one")
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["delivered", "cancelled"] and len(
        backend.turns
    ) == 1
    # a ready answer someone cancels is dropped too: never spoken, never posted
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "and check Aden's email")
    friday = h._requests.items[-1]
    answer(h, friday.run_id, "friday", "One new email.")
    await settle(h)
    assert friday.status == "ready" and "friday" in h._news
    backend.route_extra = ops(cancel=[friday.id])
    await say(h, "u1", "never mind the email")
    assert friday.status == "cancelled" and "friday" not in h._news
    await h.clear_presence()
    assert backend.late_answers == []


async def test_steer_of_a_working_row_goes_into_its_run(capsys):
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    run_id = backend.turns[0]["runId"]
    backend.steer = True
    backend.route_extra = ops(steer=[("r1", "make that last month")])
    await say(h, "u1", "make that last month")
    assert [
        (s["steerRunId"], s["utterance"], s["agentId"]) for s in backend.late_steers
    ] == [(run_id, "make that last month", "forge")]
    assert len(h._requests.items) == 1 and len(backend.turns) == 1
    assert ("thinking", 'Added to "the open PRs": make that last month', None) in lives[
        0
    ].calls
    assert metric_count(capsys, "live.request.steered") == 1


async def test_a_refused_steer_becomes_a_new_row_for_the_same_agent(capsys):
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    capsys.readouterr()
    backend.route_extra = ops(steer=[("r1", "make that last month")])
    await say(h, "u1", "make that last month")
    assert metric_count(capsys, "live.request.created") == 1  # review B5-8
    assert len(backend.late_steers) == 1
    assert rows(h) == [
        ("forge", "the open PRs", "working"),
        ("forge", "make that last month", "queued"),
    ]
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await settle(h)
    assert backend.turns[-1]["utterance"] == "make that last month"


async def test_steer_of_a_queued_row_adds_to_its_task():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    backend.route_extra = ops(steer=[("r2", "only for the web app")])
    await say(h, "u1", "only for the web app")
    assert backend.late_steers == [] and len(h._requests.items) == 2
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await settle(h)
    assert backend.turns[-1]["utterance"] == "what shipped. only for the web app"


async def test_steer_of_an_unknown_row_is_a_new_row_for_the_floor_agent_or_ignored():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(steer=[("r7", "the web app too")])
    await say(h, "u1", "the web app too")
    assert rows(h) == [("forge", "the web app too", "working")]
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(steer=[("r7", "the web app too")])
    await say(h, "u1", "the web app too")
    assert h._requests.items == [] and backend.turns == [] and lives == []


async def test_a_held_line_is_applied_after_four_quiet_seconds(capsys):
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {
        **ops(requests=[("forge", "the open PRs", "fit")]),
        "hold": True,
    }
    await say(h, "u1", "what are the open PRs and")
    assert backend.turns == [] and "u1" in h._held
    await quiet(h, clock, 3.5)
    assert backend.turns == []
    await quiet(h, clock, 0.5)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"] and len(
        backend.routes
    ) == 1
    assert h._held == {} and metric_count(capsys, "live.hold.applied") == 1
    await quiet(h, clock, 5)
    assert len(backend.turns) == 1


async def test_a_held_line_the_speaker_continues_is_merged_not_applied():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {
        **ops(requests=[("forge", "the open PRs", "fit")]),
        "hold": True,
    }
    await say(h, "u1", "what are the open PRs and")
    await quiet(h, clock, 1)
    backend.route_extra = ops(
        requests=[("forge", "the open PRs and the closed ones", "fit")]
    )
    await say(h, "u1", "the closed ones")
    assert backend.routes[-1][1] == "what are the open PRs and the closed ones"
    await quiet(h, clock, 5)
    assert [t["utterance"] for t in backend.turns] == [
        "the open PRs and the closed ones"
    ]


# -- review round 3: content words; a named line's delegation binds at once; an acknowledgement is said once --

ALREADY = ("commentary", h_mod.ALREADY_SAID)


def said_already(lives):
    return [c for c in lives[0].calls if c[:2] == ALREADY]


async def test_a_short_record_of_function_words_never_takes_a_longer_delegation():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("forge", "and what", "fit")])
    await say(h, "u1", "and what")
    first = backend.turns[0]["runId"]
    backend.route_extra = ops(requests=[("forge", "the newsletter open rate", "fit")])
    await h.on_delegation("forge", "deleg-1", "and what about the newsletter open rate")
    await settle(h)
    assert h._runs.get(first).delegation_id is None and len(backend.routes) == 2
    assert ("forge", "the newsletter open rate", "queued") in rows(h)


async def test_a_new_ask_sharing_function_words_with_an_answered_one_is_not_already_said():
    for old, new, task in (
        ("what about the PRs", "what about the newsletter", "the newsletter"),
        (PRS, "what are the open issues", "the open issues"),
    ):
        h, room, backend, lives, clock, log = await floor_open()
        await say(h, "u1", old)
        answer(h, backend.turns[0]["runId"], "forge", "Done.")
        clock.t += 5
        backend.route_extra = ops(requests=[("forge", task, "fit")])
        await h.on_delegation("forge", "deleg-2", new)  # no final comes
        await settle(h)
        assert not said_already(lives) and len(backend.routes) == 2, new
        assert ("forge", task, "working") in rows(h)


async def test_a_late_final_with_a_different_ask_is_understood():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    clock.t += 3
    backend.route_extra = ops(requests=[("forge", "the open issues", "fit")])
    await say(h, "u1", "what are the open issues")
    assert len(backend.routes) == 2 and ("forge", "the open issues", "queued") in rows(
        h
    )


async def test_a_named_summons_without_ops_binds_at_once_and_never_runs_twice():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = {}
    await say(h, "u1", "Friday, what are the open issues")
    run_id = h._voice.driven.run_id
    log.clear()
    await h.on_delegation("friday", "deleg-f", "Friday, what are the open issues")
    assert h._runs.get(run_id).delegation_id == "deleg-f" and not [
        e for e in log if e.startswith("sleep")
    ]
    answer(h, run_id, "friday", "Two issues.")
    await settle(h)
    assert [t["agentId"] for t in backend.turns] == ["forge", "friday"]
    assert not [
        c
        for c in lives[-1].calls
        if c[0] == "commentary" and c[2] == "deleg-f" and c[1] != "Two issues."
    ]


async def test_matching_still_takes_two_finals_a_repeat_and_wording_drift():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "and check Aden's email")
    await h.on_delegation(
        "forge", "deleg-1", "what are the open PRs and check Aden's email"
    )
    await settle(h)
    assert (
        len(backend.routes) == 2
        and h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"
    )
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", "What are the open PRs right now?")
    await h.on_delegation("forge", "deleg-1", "what're the open p r's right now")
    await settle(h)
    assert (
        len(backend.routes) == 1
        and h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"
    )


async def test_a_repeated_question_delegated_before_its_final_runs_again():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    clock.t += 60
    h._sleep = forever
    deleg = asyncio.create_task(h.on_delegation("forge", "deleg-2", PRS))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not deleg.done()
    await say(h, "u1", PRS)
    await deleg
    await settle(h)
    assert (
        len(backend.turns) == 2
        and h._runs.get(backend.turns[1]["runId"]).delegation_id == "deleg-2"
    )
    assert not said_already(lives)


async def test_an_empty_delegation_with_nothing_to_match_is_todays_run_at_once():
    h, room, backend, lives, clock, log = await floor_open()
    clock.t += 30
    log.clear()
    await h.on_delegation("forge", "deleg-1", "")
    await settle(h)
    assert backend.routes == [] and not h._understood.get("u1")
    assert not [e for e in log if e.startswith("sleep")]
    assert [t["utterance"] for t in backend.turns] == ["Forge, are you there"]


async def test_the_acknowledgement_is_said_once_when_the_voice_delegates_after_it():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[("friday", "Aden's email", "fit"), ("pulse", "the newsletter", "fit")]
    )
    await say(h, "u1", "check Aden's email and the newsletter")
    clock.t += 2
    await h.tick()
    await settle(h)
    ack = "Friday is on Aden's email. Pulse is on the newsletter. Say that in one short sentence, then wait."
    assert [c for c in lives[0].calls if c[1:2] == (ack,)] == [("thinking", ack, None)]
    await h.on_delegation("forge", "deleg-1", "check Aden's email and the newsletter")
    await settle(h)
    assert lives[0].calls[-1] == (
        "commentary",
        "That is already in hand. Wait for the result.",
        "deleg-1",
    )


# -- Task B6: every request ends where the person can see it (understanding §4.5, §4.8, §5.3) --

FAILED = "I could not finish this on the call. Ask me again here."


def metrics(capsys, name):
    """(value, dimensions) of every `name` metric printed so far."""
    out = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("{") and name in (m := json.loads(line)):
            out.append(
                (m[name], {k: v for k, v in m.items() if k not in ("_aws", name)})
            )
    return out


async def announced(text="Three PRs are open"):
    """Forge's answer to the three-name line, announced by Forge's own voice once the room is quiet."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    run = run_of(backend, "forge")
    answer(h, run, "forge", text)
    await quiet(h, clock)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce"
    return h, room, backend, lives, clock, run


async def test_an_announcement_cut_after_its_audio_began_is_posted_once():
    h, room, backend, lives, clock, run = await announced()
    h.on_agent_state("forge", "speaking")
    talks(h, "u2", "Aden")
    await settle(h)
    assert backend.late_answers == [], "held until the words that cut it are handled"
    h.on_active_speakers(set())
    await quiet(h, clock, h_mod.ACK_QUIET_S)  # quiet, no final: the room gets it
    assert backend.late_answers == [
        ("forge", run, TASKS["forge"], "Three PRs are open")
    ]
    talks(h, "u2", "Aden")
    await h.clear_presence()
    assert len(backend.late_answers) == 1, "a cut announcement is posted once"


async def test_an_announcement_that_finishes_uncut_is_not_posted():
    h, room, backend, lives, clock, run = await announced()
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    talks(h, "u2", "Aden")
    await h.clear_presence()
    assert backend.late_answers == []


async def test_a_cut_announcement_dont_post_that_covers_is_not_posted():
    h, room, backend, lives, clock, run = await announced()
    h._requests.no_post("forge")
    h.on_agent_state("forge", "speaking")
    talks(h, "u2", "Aden")
    await h.clear_presence()
    assert backend.late_answers == []


async def failed_forge():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    run = run_of(backend, "forge")
    h.on_data(
        None, "call.frame", packet(frame(run, "forge", {"type": "error", "seq": 1}))
    )
    talks(h, "u2", "Aden")  # someone talks on: its CANT_REACH line waits, unsaid
    return h, backend, clock, run


async def test_a_failure_never_said_is_posted_in_one_line_when_its_news_expires():
    h, backend, clock, run = await failed_forge()
    clock.t += h_mod.NEWS_TTL
    await h.tick()
    await settle(h)
    assert backend.late_answers == [("forge", run, TASKS["forge"], FAILED)]
    assert h._requests.by_run(run)[0].status == "failed"
    await (
        h.clear_presence()
    )  # the other two runs went stale meanwhile: their failures are posted now
    assert [a[1] for a in backend.late_answers].count(run) == 1, (
        "a failure is posted once"
    )


async def test_a_failure_never_said_is_posted_when_the_call_ends():
    h, backend, clock, run = await failed_forge()
    await h.clear_presence()
    assert backend.late_answers == [("forge", run, TASKS["forge"], FAILED)]


async def test_a_refused_failure_post_is_logged_not_raised():
    h, backend, clock, run = await failed_forge()

    async def refuse(*_a):
        raise BackendError(404, "run_not_found", None)

    backend.late_answer = refuse
    await h.clear_presence()  # never raises


async def test_each_row_ends_once_and_nothing_is_open_at_the_end(capsys):
    h, backend, clock, run = await failed_forge()
    answer(h, run_of(backend, "pulse"), "pulse", "On track")
    backend.route_extra = ops(cancel=[h._requests.items[2].id])  # Friday's
    await say(h, "u1", "never mind the rooms")
    await h.clear_presence()  # Forge's failure and Pulse's answer go to the room
    ended = sorted(d["status"] for _, d in metrics(capsys, "live.request.ended"))
    assert ended == ["cancelled", "delivered", "failed"], (
        "each row ends once, a posted failure stays failed"
    )
    capsys.readouterr()


async def test_open_at_end_counts_the_rows_still_open_after_the_sweep(capsys):
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "pulse"), "pulse", "On track")
    await h.clear_presence()
    assert metrics(capsys, "live.request.open_at_end") == [(2, {})]


async def test_every_understood_line_counts_one_ops_kind(capsys):
    cases = [
        (ops(social="pulse"), "social"),
        (ops(requests=[("forge", "x", "fit")]), "request"),
        (ops(more=True), "more"),
        (ops(), "none"),
    ]
    for answer_, kind in cases:
        h, room, backend, lives, clock, log = await three()
        backend.route_extra = answer_
        await say(h, "u1", "a line")
        assert [d["kind"] for _, d in metrics(capsys, "live.understand.ops")] == [kind]
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    backend.route_extra = ops(steer=[("r2", "only this week")])
    await say(h, "u1", "only this week")
    backend.route_extra = ops(cancel=["r2"])
    await say(h, "u1", "skip the shipped one")
    kinds = [d["kind"] for _, d in metrics(capsys, "live.understand.ops")]
    assert kinds[-3:] == ["request", "steer", "cancel"]


async def test_each_row_change_is_one_transcript_line_never_read_back_as_speech():
    h, room, backend, lives, clock, log = await three()
    long = "the open PRs " + "x" * 200
    backend.route_extra = ops(requests=[("forge", long, "fit")])
    await say(h, "u1", "what are the open PRs")
    backend.route_extra = ops(social="forge")
    await say(h, "u1", "thanks")
    await h._flush_transcript()
    notes = [
        x["text"]
        for _, lines in backend.transcripts
        for x in lines
        if x["kind"] == "note"
    ]
    assert notes[:2] == [
        f"[request r1 Forge queued] {long[:120]}",
        f"[request r1 Forge working] {long[:120]}",
    ]
    spoken = [r[2] for r in backend.routes] + [t["transcript"] for t in backend.turns]
    spoken += [text for live in lives for _, text in live.seed]
    assert not [s for s in spoken if "[request r" in s], (
        "a row change is never read to a model as speech"
    )


# -- Task B5 review 1 --


async def test_a_cancel_during_an_announcement_cuts_the_voice():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "check Aden's email")
    friday = h._requests.items[-1]
    answer(h, friday.run_id, "friday", "One new email.")
    await settle(h)
    await quiet(h, clock, 3)
    v = h._voice
    assert v.agent_id == "friday" and v.announce is not None and v.speaker == "u1"
    backend.route_extra = ops(cancel=[friday.id])
    await say(h, "u1", "never mind the email")
    assert friday.status == "cancelled" and v.announce is None
    assert lives[-1].calls[-3:] == [
        ("interrupt",),
        ("thinking", 'Friday dropped: "Aden\'s email".', None),
        ("nudge",),
    ]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_a_steer_in_flight_while_its_row_is_cancelled_does_nothing():
    for steered in (False, True):
        h, room, backend, lives, clock, log = await floor_open()
        await say(h, "u1", PRS)
        gate, turn = asyncio.Event(), backend.turn

        async def held(*a, steer_only=False, **k):
            if steer_only:
                await gate.wait()
            return await turn(*a, steer_only=steer_only, **k)

        backend.turn, backend.steer = held, steered
        backend.route_extra = ops(steer=[("r1", "make that last month")])
        await h.on_final_transcript("u1", "Prasiddha", "make that last month")
        for _ in range(10):
            await asyncio.sleep(0)
        backend.route_extra = ops(cancel=["r1"])
        await h.on_final_transcript("u1", "Prasiddha", "forget the PRs")
        for _ in range(10):
            await asyncio.sleep(0)
        assert rows(h) == [("forge", "the open PRs", "cancelled")]
        gate.set()
        await settle(h)
        assert (
            rows(h) == [("forge", "the open PRs", "cancelled")]
            and len(backend.turns) == 1
        )
        assert not [
            c
            for c in lives[0].calls
            if c[0] == "thinking" and c[1].startswith("Added to")
        ]


HELD = {**ops(requests=[("forge", "the open PRs", "fit")]), "hold": True}


async def test_a_held_line_stands_when_another_person_speaks():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = HELD
    await say(h, "u1", "what are the open PRs and")
    backend.route_extra = ops()
    await say(h, "u2", "yeah I was wondering that too")
    await quiet(h, clock, 4)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"]


async def test_a_held_line_of_someone_who_left_is_not_applied(capsys):
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = HELD
    await say(h, "u1", "what are the open PRs and")
    await h.on_participants({"u2": "Aden"})
    await quiet(h, clock, 5)
    assert backend.turns == [] and h._held == {} and "u1" not in h._final_at
    assert metric_count(capsys, "live.hold.applied") == 0


async def test_an_unrelated_delegation_does_not_drop_a_held_line():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = {
        **ops(requests=[("friday", "Aden's email", "fit")]),
        "hold": True,
    }
    await say(h, "u1", "check Aden's email and")
    backend.route_extra = ops(requests=[("pulse", "the GTM", "fit")])
    await h.on_delegation("forge", "d1", "what about the GTM plan")
    await settle(h)
    await quiet(h, clock, 4)
    assert sorted(t["utterance"] for t in backend.turns) == ["Aden's email", "the GTM"]


async def test_a_steer_before_the_run_id_is_bound_amends_the_row():
    h, room, backend, lives, clock, log = await three()
    backend.session_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", PRS)
    for _ in range(20):
        await asyncio.sleep(0)
    assert backend.sessions == ["forge"] and h._requests.items[0].run_id is None
    backend.route_extra = ops(steer=[("r1", "only the web app")])
    await h.on_final_transcript("u1", "Prasiddha", "only the web app")
    for _ in range(20):
        await asyncio.sleep(0)
    backend.session_gate.set()
    await settle(h)
    assert len(h._requests.items) == 1 and backend.late_steers == []
    assert [t["utterance"] for t in backend.turns] == ["the open PRs. only the web app"]


async def test_a_superseded_lines_steer_of_an_unknown_row_makes_no_row():
    h, room, backend, lives, clock, log = await three(
        people={"u1": "Prasiddha", "u2": "Aden", "u3": "Leo"}
    )
    await say(h, "u2", "Pulse, are you there")
    h._runs.abort(backend.turns[0]["runId"])
    backend.turns.clear()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(steer=[("r9", "the web app too")])
    await h.on_final_transcript("u1", "Prasiddha", "the web app too")
    await asyncio.sleep(0)
    backend.route_gate, gate = None, backend.route_gate
    backend.route_extra = ops()
    await h.on_final_transcript(
        "u3", "Leo", "I think we should look at the numbers first"
    )
    gate.set()
    await settle(h)
    assert h._requests.items == [] and backend.turns == []


# -- Task B6 review 1 --


async def test_a_cut_announcement_is_posted_once_its_cutters_line_is_handled():
    h, room, backend, lives, clock, run = await announced()
    h.on_agent_state("forge", "speaking")
    talks(h, "u2", "Aden")
    h.on_active_speakers(set())
    await say(h, "u2", "sorry, go on")
    await h.tick()
    await settle(h)
    assert [a[3] for a in backend.late_answers] == ["Three PRs are open"]
    await h.clear_presence()
    assert len(backend.late_answers) == 1


async def test_dont_post_that_said_over_the_cut_posts_nothing():
    for line in ("Forge, don't post that", "don't post that"):
        h, room, backend, lives, clock, run = await announced()
        h.on_agent_state("forge", "speaking")
        talks(h, "u1", "Prasiddha")
        await say(h, "u1", line)
        h.on_active_speakers(set())
        await quiet(h, clock, 5)
        await h.clear_presence()
        assert backend.late_answers == [], line
        assert h._cut_off == {}, "dropped, never held to hang-up"


async def test_a_stop_said_over_the_cut_posts_nothing():
    for line in ("Forge, stop", "everyone stop"):
        h, room, backend, lives, clock, run = await announced()
        h.on_agent_state("forge", "speaking")
        talks(h, "u1", "Prasiddha")
        await say(h, "u1", line)
        h.on_active_speakers(set())
        await quiet(h, clock, 5)
        await h.clear_presence()
        assert backend.late_answers == [], line


async def test_a_cut_held_at_hang_up_is_posted_then():
    h, room, backend, lives, clock, run = await announced()
    h.on_agent_state("forge", "speaking")
    talks(h, "u2", "Aden")
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == [
        "Three PRs are open"
    ] and h._cut_off == {}


async def test_a_stop_then_its_voices_delegation_starts_nothing():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await say(h, "u1", "Forge, stop")
    n = len(backend.turns)
    backend.route_extra = ops(requests=[("forge", "check the CI", "fit")])
    await h.on_delegation("forge", "d9", "check the CI status for me")
    await settle(h)
    assert len(backend.turns) == n, "a row-change note never moves the stop's mark"
    assert not [r for r in h._requests.items if r.status in ("queued", "working")]


async def test_a_failed_row_whose_run_then_answers_posts_the_answer_once():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    run = run_of(backend, "forge")
    h._request_failed(h._requests.by_run(run), run)
    talks(h, "u2", "Aden")
    answer(h, run, "forge", "Three PRs are open")
    await h.clear_presence()
    assert [a for a in backend.late_answers if a[1] == run] == [
        ("forge", run, TASKS["forge"], "Three PRs are open")
    ]


async def test_an_unrouted_row_never_counts_as_ended(capsys):
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    backend.route_gate.set()
    await settle(h)
    await h.clear_presence()
    out = capsys.readouterr().out
    assert "live.request.ended" not in out and "live.request.created" not in out


async def test_a_superseded_line_counts_what_it_was_understood_as(capsys):
    for answer_, kind in (
        (ops(requests=[("forge", "x", "fit")]), "request"),
        (ops(social="pulse"), "social"),
    ):
        h, room, backend, lives, clock, log = await three()
        backend.route_extra = answer_
        backend.route_gate = asyncio.Event()
        await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
        await h.on_final_transcript("u2", "Aden", "hang on a second, let me think")
        backend.route_gate.set()
        await settle(h)
        kinds = [d["kind"] for _, d in metrics(capsys, "live.understand.ops")]
        assert kinds[0] == kind, kind
        assert not [t for t in backend.turns if t["speaker"] == "u1"], (
            "the first line was superseded"
        )


# -- re-review 4: a match that makes the worker do nothing is exact; bind-at-once never takes a newer line --

OPEN_PRS, CLOSED_PRS = "check the open PRs", "check the closed PRs"


async def test_an_ask_one_word_from_an_answered_one_is_not_already_said():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", OPEN_PRS)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    clock.t += 5
    backend.route_extra = ops(requests=[("forge", "the closed PRs", "fit")])
    await h.on_delegation("forge", "deleg-2", CLOSED_PRS)  # no final comes
    await settle(h)
    assert not said_already(lives) and len(backend.routes) == 2
    assert ("forge", "the closed PRs", "working") in rows(h)


async def test_a_late_final_one_word_from_the_delegated_ask_is_understood():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation(
        "forge", "deleg-1", OPEN_PRS
    )  # no final: its own transcript is understood
    await settle(h)
    clock.t += 3
    backend.route_extra = ops(requests=[("forge", "the closed PRs", "fit")])
    await say(h, "u1", CLOSED_PRS)
    assert len(backend.routes) == 2 and ("forge", "the closed PRs", "queued") in rows(h)


async def test_a_misheard_word_still_binds_the_delegation_to_its_open_row():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    await h.on_delegation("forge", "deleg-1", "what are the open peers")
    await settle(h)
    assert len(backend.routes) == 1 and len(backend.turns) == 1
    assert h._runs.get(backend.turns[0]["runId"]).delegation_id == "deleg-1"


async def test_a_newer_lines_delegation_never_binds_an_older_summons_run():
    for gated in (False, True):
        h, room, backend, lives, clock, log = await three()
        backend.route_extra = {}
        await say(
            h, "u1", "Forge, check CI"
        )  # no ops: the whole line is the driven run, no record
        run_id = h._voice.driven.run_id
        clock.t += 2
        backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
        if gated:  # the newer line is still at /route when the voice delegates it
            backend.route_gate = asyncio.Event()
            await h.on_final_transcript("u1", "Prasiddha", "also what are the open PRs")
            await asyncio.sleep(0)
            deleg = asyncio.create_task(
                h.on_delegation("forge", "deleg-2", "also what are the open PRs")
            )
            await asyncio.sleep(0)
            backend.route_gate.set()
            await deleg
        else:
            await say(h, "u1", "also what are the open PRs")
            await h.on_delegation("forge", "deleg-2", "also what are the open PRs")
        await settle(h)
        assert h._runs.get(run_id).delegation_id is None, gated
        assert [
            c
            for c in lives[-1].calls
            if c[0] == "commentary" and c[2] == "deleg-2" and "next in line" in c[1]
        ], gated


# -- whole-branch review: nothing queued is lost at hang-up; a held line runs once; an old delegation never
# eats a new line --


async def test_a_row_still_queued_at_hang_up_is_handed_to_the_room(capsys):
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("forge", "what shipped", "fit")]
    )
    await say(h, "u1", "what are the open PRs and what shipped")
    assert rows(h) == [
        ("forge", "the open PRs", "working"),
        ("forge", "what shipped", "queued"),
    ]
    capsys.readouterr()
    await h.clear_presence()
    assert [(a[0], a[2], a[3]) for a in backend.late_answers] == [
        ("forge", "what shipped", h_mod.NOT_STARTED_POST)
    ]
    assert (
        len(backend.turns) == 1 and rows(h)[1][2] == "failed"
    )  # never done: not "delivered"
    assert metrics(capsys, "live.request.open_at_end") == [
        (1, {})
    ]  # the working row: its run posts its answer


async def test_an_answer_being_said_at_hang_up_is_posted():
    h, room, backend, lives, clock, run = await announced()
    h.on_agent_state("forge", "speaking")
    await h.clear_presence()
    assert backend.late_answers == [
        ("forge", run, TASKS["forge"], "Three PRs are open")
    ]


async def test_a_held_line_its_voice_delegates_runs_once():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = HELD
    await say(h, "u1", "what are the open PRs and")
    await h.on_delegation("forge", "d1", "what are the open PRs and")
    await settle(h)
    assert backend.turns == [] and lives[0].calls[-1] == (
        "commentary",
        h_mod.UNFINISHED,
        "d1",
    )
    await quiet(h, clock, 5)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"]


async def test_a_held_line_a_longer_understood_delegation_covers_is_not_applied_again():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = HELD
    await say(h, "u1", "what are the open PRs and")
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("forge", "what shipped", "fit")]
    )
    await h.on_delegation(
        "forge", "d1", "what are the open PRs and what shipped"
    )  # no final for the rest
    await settle(h)
    await quiet(h, clock, 5)
    assert [r[:2] for r in rows(h)] == [
        ("forge", "the open PRs"),
        ("forge", "what shipped"),
    ]


async def test_an_old_unbound_delegation_never_makes_a_new_final_late():
    h, room, backend, lives, clock, log = await floor_open()
    await h.on_delegation("forge", "d1", PRS)  # no final ever comes for it
    await settle(h)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    clock.t += 300
    await say(h, "u1", PRS)
    assert len(backend.routes) == 2 and len(backend.turns) == 2


async def test_a_final_that_negates_the_delegated_ask_is_understood():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("forge", "send the email to Aden", "fit")])
    await h.on_delegation("forge", "d1", "send the email to Aden")
    await settle(h)
    backend.route_extra = ops(cancel=[h._requests.items[0].id])
    await say(h, "u1", "no, do not send the email to Aden")
    assert len(backend.routes) == 2 and rows(h)[0][2] == "cancelled"


async def test_a_longer_ask_holding_an_answered_one_is_not_already_said():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    clock.t += 60
    backend.route_extra = ops(requests=[("forge", "the closed issues", "fit")])
    await h.on_delegation(
        "forge", "d2", "what are the open PRs and the closed issues"
    )  # no final comes
    await settle(h)
    assert not said_already(lives) and (
        "forge",
        "the closed issues",
        "working",
    ) in rows(h)


async def test_a_summons_run_binds_its_delegation_whatever_older_record_matches():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await quiet(h, clock, 120)
    assert h._voice is None
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(
        h, "u1", "Forge, what are the open PRs"
    )  # /route is down: the whole line, a run with no row
    run_id = h._voice.driven.run_id
    await h.on_delegation("forge", "d2", PRS)
    await settle(h)
    assert h._runs.get(run_id).delegation_id == "d2" and not [
        c for c in lives[-1].calls if c[:2] == ALREADY
    ]


async def test_a_correction_the_voice_delegates_is_told_it_was_made():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    backend.route_extra = ops(
        steer=[(h._requests.items[0].id, "only the backend repo")]
    )
    await say(h, "u1", "only the backend repo")
    await h.on_delegation("forge", "d2", "only the backend repo")
    await settle(h)
    assert lives[0].calls[-1] == ("commentary", h_mod.CHANGED, "d2")


def test_wire_limits_a_long_teammate_name_and_task():
    from huddle_agent.request_list import RequestList

    rl = RequestList()
    rl.settle(1, "u1", [{"agentId": "forge", "task": "x" * 300, "by": "fit"}], 0.0)
    assert all(len(line) <= 200 for line in rl.lines(lambda _a: "N" * 90))


# -- last re-review: an unfinished delegation before its final runs once; a delegated call-off is told so --


async def test_an_unfinished_delegation_before_its_final_runs_once():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    h.on_agent_said(
        "forge", "Two PRs are open."
    )  # a transcript line after the speaker's last final
    backend.route_extra = {
        **ops(requests=[("forge", "the deploy status", "fit")]),
        "hold": True,
    }
    await h.on_delegation(
        "forge", "d2", "and what is the deploy status of"
    )  # its final is still to come
    await settle(h)
    assert len(backend.turns) == 1 and lives[0].calls[-1] == (
        "commentary",
        h_mod.UNFINISHED,
        "d2",
    )
    backend.route_extra = ops(
        requests=[("forge", "the deploy status of the backend", "fit")]
    )
    await say(h, "u1", "and what is the deploy status of the backend")
    assert backend.routes[-1][1] == "and what is the deploy status of the backend", (
        "the held words are in it"
    )
    assert [t["utterance"] for t in backend.turns[1:]] == [
        "the deploy status of the backend"
    ]


async def test_a_delegated_call_off_is_told_it_was_made():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("forge", "send the email to Aden", "fit")])
    await say(h, "u1", "send the email to Aden")
    backend.route_extra = ops(cancel=[h._requests.items[0].id])
    await say(h, "u1", "no, do not send the email to Aden")
    await h.on_delegation("forge", "d2", "no, do not send the email to Aden")
    await settle(h)
    assert lives[0].calls[-1] == ("commentary", h_mod.CHANGED, "d2")


async def test_one_speakers_lines_start_in_the_order_they_were_said():
    # Codex P1: each final has its own /route call; the second answering first must not run first
    h, room, backend, lives, clock, log = await three()
    older = backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "draft the email", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "draft the email to Aden")
    await asyncio.sleep(0)
    backend.route_gate = None
    backend.route_extra = ops(requests=[("forge", "send it", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "then send it")
    for _ in range(5):
        await asyncio.sleep(0)
    assert rows(h) == [], "the later line waits for the earlier one's answer"
    older.set()
    await settle(h)
    assert rows(h) == [
        ("forge", "draft the email", "working"),
        ("forge", "send it", "queued"),
    ]


async def test_a_named_lines_requests_settle_before_a_later_line_of_its_speaker():
    # Codex P1: the named line's rows are made when its voice has opened; a later line must not start first
    h, room, backend, lives, clock, log = await three()
    backend.session_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "draft the email", "name")])
    await h.on_final_transcript("u1", "Prasiddha", "Forge, draft the email to Aden")
    for _ in range(10):
        await asyncio.sleep(0)
    backend.route_extra = ops(requests=[("forge", "send it", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "then send it")
    for _ in range(10):
        await asyncio.sleep(0)
    assert rows(h) == [], "the later line waits for the named line's rows"
    backend.session_gate.set()
    await settle(h)
    assert [r[:2] for r in rows(h)] == [
        ("forge", "draft the email"),
        ("forge", "send it"),
    ]
    assert [t["utterance"] for t in backend.turns] == ["draft the email"]


async def test_a_line_never_waits_past_the_route_timeout_for_an_earlier_one(
    monkeypatch,
):
    h, room, backend, lives, clock, log = await three()
    monkeypatch.setattr(h_mod, "SAID_ORDER_WAIT_S", 0.01)
    stuck = (
        asyncio.get_running_loop().create_future()
    )  # an earlier line that never resolves
    h._routing["u1"] = [stuck]
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await say(h, "u1", "what are the open PRs")
    assert rows(h) == [("forge", "the open PRs", "working")] and h._routing["u1"] == []


# -- 10-04 prod voice test: a ready answer is said while the floor agent still works; noise is nobody talking --


async def test_a_ready_answer_is_said_while_the_floor_agent_still_works():
    (
        h,
        room,
        backend,
        lives,
        clock,
        log,
    ) = await floor_open()  # Forge's voice is open for Prasiddha
    backend.route_extra = ops(
        requests=[
            ("forge", "Aden's email", "floor"),
            ("pulse", "the GTM update", "fit"),
        ]
    )
    await say(h, "u1", "check Aden's email, and what is the GTM update")
    assert rows(h) == [
        ("forge", "Aden's email", "working"),
        ("pulse", "the GTM update", "working"),
    ]
    answer(h, run_of(backend, "pulse"), "pulse", "GTM is on track.")
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "pulse" and h._voice.path == "announce", (
        "Pulse says it; Forge's run goes on"
    )
    h.on_agent_state("pulse", "speaking")
    h.on_agent_state("pulse", "listening")
    assert rows(h)[1][2] == "delivered" and rows(h)[0][2] == "working"
    answer(h, run_of(backend, "forge"), "forge", "No new mail.")
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce", (
        "the floor agent's answer in its turn"
    )
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    assert [r[2] for r in rows(h)] == ["delivered", "delivered"]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_noise_never_holds_a_ready_answer_back():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.on_active_speakers({"u1"})  # an open laptop microphone: typing, a fan. No words.
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce"


async def test_noise_never_drops_an_announcement_and_real_words_do(capsys):
    h, room, backend, lives, clock, run = await announced()
    h.on_active_speakers({"u2"})  # sound without words
    assert h._voice is not None and h._voice.announce is not None
    assert metric_count(capsys, "live.announce.aborted") == 0
    h.on_interim_transcript("u2", "Aden", "hang on a second")
    assert metric_count(capsys, "live.announce.aborted") == 1
    assert h._news.get("forge"), "kept: said later"
