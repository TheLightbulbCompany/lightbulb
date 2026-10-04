"""Conversation engine §3.6: the spoken commands on the request list."""

import asyncio
import json

from test_huddle import answer, metric_count, say, settle, summoned
from test_request_engine import LINE, TASKS, five, pump, quiet, run_of, status, three


async def test_naming_an_agent_and_stop_cancels_its_requests_and_aborts_only_its_run():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await say(h, "u1", "Forge, stop")
    assert backend.aborts == [run_of(backend, "forge")]
    assert {r.agent: r.status for r in h._requests.items} == {"pulse": "working", "forge": "cancelled", "friday": "working"}
    answer(h, run_of(backend, "forge"), "forge", "Three PRs")
    assert "forge" not in h._news


async def test_stop_drops_an_answer_that_was_ready_and_never_posts_it():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs")
    await say(h, "u1", "Forge, never mind")
    assert "forge" not in h._news
    await h.clear_presence()
    assert backend.late_answers == []


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
    assert h._news == {} and [l.agent_id for l in lives] == ["pulse"]
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
    await say(h, "u1", "Forge, stop")
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


async def test_a_delegated_command_never_becomes_a_run():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await h.on_delegation("pulse", "d1", "don't post that")
    await h.on_delegation("pulse", "d2", "everyone stop")
    await settle(h)
    assert len(backend.turns) == len(TASKS)
    assert {r.status for r in h._requests.items} == {"cancelled"}


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


async def test_naming_the_agent_and_stop_while_route_decides_a_one_ask_line_summons_nobody():
    h, backend, lives = await _one_ask_line_while_route_decides("Forge, stop")
    assert backend.turns == [] and lives == [] and backend.summons == []


async def _plain_answer_waiting_as_news():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    run = backend.turns[0]["runId"]
    await h.end_exchange()  # Forge leaves the floor: its answer is held as news
    answer(h, run, "forge", "We shipped huddles")
    assert [n[0] for n in h._news["forge"]] == [run]
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
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await pump()
    assert len(backend.turns) == 3 and lives == []
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    h.gate["live"].set()
    await settle(h)
    assert ("nudge",) not in lives[0].calls and ("close",) in lives[0].calls
    assert h._voice is None and h._window is None and len(backend.ends) == 1


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
