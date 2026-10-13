"""Conversation engine §3.6: the spoken commands on the request list."""

import asyncio
import json

import pytest

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from test_huddle import FakeLive, answer, hand, metric_count, ops, say, settle, summoned
from test_request_engine import LINE, five, pump, quiet, run_of, status, three


async def test_a_named_withdrawal_route_cancels_cancels_its_request_and_aborts_only_its_run():
    """Owner ruling 10-12: /route decides whether a stop calls work off (`ops.cancel`); only its run is aborted."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    (row,) = [r for r in h._requests.items if r.agent == "forge"]
    backend.route_extra = ops(cancel=[row.id])
    await say(h, "u1", "Forge, never mind")
    assert backend.aborts == [run_of(backend, "forge")]
    assert {r.agent: r.status for r in h._requests.items} == {"pulse": "working", "forge": "cancelled", "friday": "working"}
    answer(h, run_of(backend, "forge"), "forge", "Three PRs")
    assert hand(h, "forge") == []
    await h.clear_presence()
    assert "forge" not in [a[0] for a in backend.late_answers]


async def test_a_named_stop_calls_nothing_off():
    """"Forge, stop" silences Forge (`_stop_said`); its request goes on, and /route is not asked."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    routes = len(backend.routes)
    await say(h, "u1", "Forge, stop")
    assert backend.aborts == [] and status(h, "forge") == ["working"] and len(backend.routes) == routes
    assert h._hushed(run_of(backend, "forge"))


async def test_a_cancel_drops_an_answer_that_was_ready_and_never_posts_it():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs")
    (row,) = [r for r in h._requests.items if r.agent == "forge"]
    backend.route_extra = ops(cancel=[row.id])
    await say(h, "u1", "Forge, never mind")
    assert hand(h, "forge") == []
    await h.clear_presence()
    assert "forge" not in [a[0] for a in backend.late_answers]


async def test_everyone_stop_cancels_every_request_aborts_every_run_and_cuts_the_floor(capsys):
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    capsys.readouterr()
    await say(h, "u1", "everyone stop")
    assert sorted(backend.aborts) == sorted(t["runId"] for t in backend.turns)
    assert {r.status for r in h._requests.items} == {"cancelled"}
    assert ("interrupt",) in lives[0].calls
    assert len(backend.turns) == 3  # the command is never a run
    assert metric_count(capsys, "live.request.stop_all") == 1


async def test_everyone_stop_means_a_queued_request_never_starts():
    h, room, backend, lives, clock, log = await five()
    await say(h, "u1", "stop all")
    assert status(h, "echo") == ["cancelled"] and len(backend.turns) == 4
    assert sorted(backend.aborts) == sorted(t["runId"] for t in backend.turns)


async def test_everyone_stop_drops_ready_answers_unspoken_and_unposted():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs")
    answer(h, run_of(backend, "friday"), "friday", "Two rooms")
    await say(h, "u1", "everyone, please stop")
    await quiet(h, clock, 5)
    assert h._ledger == [] and [x.agent_id for x in lives] == ["pulse"]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_everyone_stop_while_route_decides_starts_nothing():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await h.on_final_transcript("u2", "Aden", "everyone stop")
    backend.route_gate.set()
    await settle(h)
    assert backend.turns == [] and lives == [] and h._requests.open() == []


async def test_dont_post_that_names_the_agents_newest_request_and_marks_its_run(capsys):
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    capsys.readouterr()
    await say(h, "u1", "Forge, don't post that")
    forge = run_of(backend, "forge")
    assert backend.no_posts == [forge] and [r.no_post for r in h._requests.items] == [False, True, False]
    assert len(backend.turns) == 3  # the command is never a run
    assert metric_count(capsys, "live.request.no_post") == 1
    answer(h, forge, "forge", "Three PRs")
    await h.clear_presence()
    assert backend.late_answers == []
    assert metric_count(capsys, "live.request.post_skipped") == 1


async def test_dont_post_that_with_no_name_is_the_rooms_newest_request():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await say(h, "u1", "don't post that")
    assert backend.no_posts == [run_of(backend, "friday")]


async def test_dont_post_that_reaches_a_plain_run_with_no_request():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    run = backend.turns[0]["runId"]
    await say(h, "u1", "don't post that")
    assert backend.no_posts == [run] and not h._requests.postable(run)


async def test_dont_post_that_naming_an_agent_with_nothing_open_touches_no_other_answer():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    (row,) = [r for r in h._requests.items if r.agent == "forge"]
    backend.route_extra = ops(cancel=[row.id])
    await say(h, "u1", "Forge, never mind")
    await say(h, "u1", "Forge, don't post that")
    assert backend.no_posts == []
    assert all(h._requests.postable(run_of(backend, a)) for a in ("pulse", "friday"))


async def test_a_turns_retry_cannot_wash_out_no_post():
    h, room, backend, lives, clock, log = await three()
    backend.turn_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    for _ in range(200):
        if len(backend.turns) == 3:
            break
        await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "Forge, don't post that")
    backend.turn_gate.set()
    await settle(h)
    forge = run_of(backend, "forge")
    assert backend.no_posts == [forge, forge]  # at once, and again after /turns rewrote the VOICERUN row


async def _one_ask_line_while_route_decides(command):
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}  # /route answers one ask: today's dispatch of the line
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what did Friday say?")
    await h.on_final_transcript("u1", "Prasiddha", command)
    backend.route_gate.set()
    await settle(h)
    return h, backend, lives


async def test_everyone_stop_while_route_decides_a_one_ask_line_summons_nobody():
    h, backend, lives = await _one_ask_line_while_route_decides("everyone stop")
    assert backend.turns == [] and lives == [] and backend.summons == []


@pytest.mark.xfail(strict=True, reason="a stop said while /route still decides the line it would stop: the mouth path silences only what is open when it is said (`_hush`), so the line's voice still opens and acknowledges it (owner decision owed)")
async def test_naming_the_agent_and_stop_while_route_decides_a_one_ask_line_keeps_it_silent():
    h, backend, lives = await _one_ask_line_while_route_decides("Forge, stop")
    assert not [c for x in lives for c in x.calls if c[0] == "say"]


async def _plain_answer_waiting_as_news():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    run = backend.turns[0]["runId"]
    await h.end_exchange()  # Forge leaves the floor: its answer waits as its raised hand
    answer(h, run, "forge", "We shipped huddles")
    assert hand(h, "forge") == ["We shipped huddles"]
    return h, backend, run


async def test_dont_post_that_naming_an_agent_reaches_its_answer_waiting_as_news(capsys):
    h, backend, run = await _plain_answer_waiting_as_news()
    capsys.readouterr()
    await say(h, "u1", "Forge, don't post that")
    assert backend.no_posts == [run] and metric_count(capsys, "live.request.no_post") == 1
    await h.clear_presence()
    assert backend.late_answers == []


async def test_dont_post_that_unnamed_reaches_the_newest_answer_waiting_as_news():
    h, backend, run = await _plain_answer_waiting_as_news()
    await say(h, "u1", "don't post that")
    assert backend.no_posts == [run]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_dont_post_that_with_nothing_to_mark_counts_nothing(capsys):
    h, room, backend, lives, clock, log = await three()
    capsys.readouterr()
    await say(h, "u1", "don't post that")
    assert backend.no_posts == [] and metric_count(capsys, "live.request.no_post") == 0


async def test_a_line_with_two_mentions_and_no_wake_name_calls_route_once():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "I think Forge and Friday both did good work, what shipped?")
    assert len(backend.routes) == 1


async def test_everyone_stop_while_the_acknowledging_voice_joins_closes_it_unsaid():
    h, room, backend, lives, clock, log = await three()
    h.gate["live"] = asyncio.Event()
    factory = h._live_factory

    async def unbilled(**kw):  # a session closed right after joining reports no usage
        live = await factory(**kw)
        live.final_usage = 0.0
        return live

    h._live_factory = unbilled
    t0 = clock.t
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await pump()
    assert len(backend.turns) == 3 and lives == []
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    h.gate["live"].set()
    await settle(h)
    assert ("nudge",) not in lives[0].calls and ("close",) in lives[0].calls
    assert h._voice is None and h._window is None and len(backend.ends) == 1
    assert backend.ends[0][1] <= clock.t - t0 + 1  # the voice's seconds, never the epoch


async def test_name_latency_includes_the_route_detour_of_a_one_ask_line(capsys):
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what did Friday say?")
    await pump()  # /route is in flight
    clock.t += 2
    backend.route_gate.set()
    await settle(h)
    capsys.readouterr()
    h.on_agent_state("forge", "speaking")
    (ms,) = [
        json.loads(x)["live.latency.first_audio"]
        for x in capsys.readouterr().out.splitlines()
        if "live.latency.first_audio" in x
    ]
    assert ms >= 2000


async def test_a_route_landing_during_shutdown_opens_no_window():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what did Friday say?")
    await h.end_exchange(force=True)
    backend.route_gate.set()
    await settle(h)
    assert backend.summons == [] and h._window is None


async def test_hang_up_waits_for_a_no_post_mark_in_flight():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    gate, real = asyncio.Event(), backend.no_post

    async def no_post(run_id):
        await gate.wait()
        return await real(run_id)

    backend.no_post = no_post
    await h.on_final_transcript("u1", "Prasiddha", "don't post that")
    closing = asyncio.create_task(h.clear_presence())
    await pump()
    assert not closing.done()
    gate.set()
    await closing
    assert backend.no_posts == [backend.turns[0]["runId"]]


async def test_a_voice_that_never_opened_bills_nothing():
    h, room, backend, lives, clock, log = await three()
    live = FakeLive([], "forge", "", "", "u1", [])
    live.final_usage = 0.0
    await h._close_live(h_mod._Voice("forge", live, 1, "u1", clock.t))  # opened stays 0.0
    assert h._closed_seconds == 0.0


TWO_MENTIONS = "I think Forge and Friday both did good work, what shipped?"


async def test_a_line_naming_two_agents_asks_the_first_whatever_route_says():
    """Every call follows the Meet's name rule (owner ruling 10-12): with nobody on the floor, the names in the
    line decide. A /route that fails is not asked again: the line is the first agent's, the next is queued."""
    h, room, backend, lives, clock, log = await three()
    backend.route_extra, backend.jev_to = {}, "forge"
    real, calls = backend.route, []

    async def route(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise BackendError(503, "huddles_upstream", None)
        return await real(*a, **k)

    backend.route = route
    await say(h, "u1", TWO_MENTIONS)
    assert len(calls) == 1 and [x.agent_id for x in lives] == ["forge"]
    assert [q[0] for q in h._queue] == ["friday"]


async def test_a_failed_route_on_a_line_naming_agents_tells_nobody():
    h, room, backend, lives, clock, log = await three(people={"u1": "Prasiddha"})
    backend.route_extra = {}
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(h, "u1", TWO_MENTIONS)
    assert len(backend.routes) == 1 and [x.agent_id for x in lives] == ["forge"]  # the names decide
    assert [p for p in room.published if p[0] == h_mod.ERROR_TOPIC] == []


@pytest.mark.xfail(strict=True, reason="a stop said while /route still decides the line it would stop: the mouth path silences only what is open when it is said (`_hush`), so the line's voice still opens and acknowledges it (owner decision owed)")
async def test_an_agent_stopped_while_route_decides_a_mentions_line_says_nothing():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra, backend.jev_to = {}, "forge"
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", TWO_MENTIONS)
    await h.on_final_transcript("u1", "Prasiddha", "Forge, stop")
    backend.route_gate.set()
    await settle(h)
    assert not [c for x in lives for c in x.calls if c[0] == "say"]


async def test_unnamed_dont_post_that_reaches_only_the_speakers_own_held_answer():
    h, backend, run = await _plain_answer_waiting_as_news()  # Prasiddha (u1) asked Forge
    await say(h, "u2", "don't post that")
    assert backend.no_posts == [] and h._requests.postable(run)
    await say(h, "u1", "don't post that")
    assert backend.no_posts == [run]


async def test_no_window_opens_once_hang_up_begins():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what did Friday say?")
    closing = asyncio.create_task(h.clear_presence())
    await pump()
    backend.route_gate.set()
    await closing
    await settle(h)
    assert backend.summons == [] and h._window is None


async def test_a_cancel_aborts_its_run_and_the_queued_request_starts():
    h, room, backend, lives, clock, log = await five()
    alpha = run_of(backend, "alpha")
    (row,) = [r for r in h._requests.items if r.agent == "alpha"]
    backend.route_extra = ops(cancel=[row.id])
    await say(h, "u1", "Alpha, never mind")
    assert backend.aborts == [alpha] and status(h, "alpha") == ["cancelled"]
    assert backend.turns[-1]["agentId"] == "echo" and status(h, "echo") == ["working"]

