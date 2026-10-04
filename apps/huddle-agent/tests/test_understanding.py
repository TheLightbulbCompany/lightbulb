"""Understanding §4.4-§4.6 (Task B2): /route's `ops` turn a line into rows; small talk starts no run."""

import asyncio

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from test_huddle import answer, metric_count, ops, say, settle
from test_request_engine import LINE, TASKS, three

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
    # (a) before the final
    h, room, backend, lives, clock, log = await floor_open()
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
    t0 = clock.t
    await h.on_delegation("forge", "deleg-1", PRS)
    await settle(h)
    assert clock.t - t0 >= h_mod.DELEGATION_WAIT_S
    assert [r[1] for r in backend.routes] == [PRS]
    bound_and_spoken(h, backend, lives)
    assert metric_count(capsys, "live.delegation.unbound") == 1
    h._utter_from["u1"] = t0 - 1  # the late final's speech began before the delegation
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
