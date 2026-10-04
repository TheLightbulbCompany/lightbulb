"""Conversation engine §3.2-§3.5: a turn naming several agents."""

import asyncio
import copy

import pytest

from huddle_agent.backend import BackendError
from test_huddle import TEAM, answer, make, say, settle, talks

LINE = (
    "Pulse, an update on the go-to-market strategy. Forge, what our PRs look like. "
    "Friday, a summary of our rooms and the last conversation from the last day"
)
TASKS = {
    "pulse": "an update on the go-to-market strategy",
    "forge": "what our PRs look like",
    "friday": "a summary of our rooms and the last conversation from the last day",
}


async def three(people=None, setting="live"):
    h, room, backend, lives, clock, log = make(setting=setting)
    agents = [{"id": a, "name": a.title(), "listenMode": "active", "defaultName": False} for a in TASKS]
    backend.team_data = {**copy.deepcopy(TEAM), "agents": agents}
    backend.route_extra = {
        "agentIds": list(TASKS),
        "requests": [{"agentId": a, "task": t} for a, t in TASKS.items()],
    }
    await h.on_participants(people or {"u1": "Prasiddha", "u2": "Aden"})
    return h, room, backend, lives, clock, log


def run_of(backend, agent_id):
    return next(t["runId"] for t in backend.turns if t["agentId"] == agent_id and not t["steerRunId"])


async def quiet(h, clock, seconds=2):
    clock.t += seconds
    await h.tick()
    await settle(h)


async def test_one_name_keeps_the_fast_path_and_makes_no_request():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", "Forge, what our PRs look like")
    # understood beside the voice (understanding §4.6); an answer without `ops` keeps the whole line
    assert len(backend.routes) == 1 and h._requests.items == [] and [l.agent_id for l in lives] == ["forge"]
    assert backend.turns[0]["utterance"] == "Forge, what our PRs look like" and backend.turns[0]["requests"] == []


async def test_an_unnamed_line_makes_no_request():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "what shipped this week?")
    assert len(backend.routes) == 1 and backend.route_humans == [2] and h._requests.items == []


async def test_every_named_agent_shows_working_before_route_answers():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    w = room.written()
    assert all(w[f"lightbulb.agent.{a}.status"] == "working" for a in TASKS)  # spec §3.5: strip <= 1 s
    backend.route_gate.set()
    await settle(h)
    w = room.written()
    assert w["lightbulb.agent.forge.working"] == "what our PRs look like"
    assert w["lightbulb.agent.friday.working"] == TASKS["friday"][:60]


async def test_a_multi_name_turn_starts_every_request_and_one_voice_acknowledges():
    h, room, backend, lives, clock, log = await three()
    t0 = clock.t
    await say(h, "u1", LINE)
    assert {t["agentId"]: t["utterance"] for t in backend.turns} == TASKS and clock.t == t0
    assert backend.summons == ["u1"]  # one metering window for all of them
    assert [l.agent_id for l in lives] == ["pulse"] and ("nudge",) in lives[0].calls
    assert lives[0].seed[-1][1].startswith("You are on an update on the go-to-market strategy. Forge is on")
    assert ("user", f"Prasiddha: {LINE}") not in lives[0].seed  # the voice does not answer the whole line
    pulse = next(t for t in backend.turns if t["agentId"] == "pulse")
    assert pulse["requests"] == [f'Forge is answering "{TASKS["forge"]}"', f'Friday is answering "{TASKS["friday"]}"']


async def test_at_most_four_requests_work_at_once_and_the_fifth_starts_when_a_slot_frees():
    h, room, backend, lives, clock, log = make()
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    agents = [{"id": n.lower(), "name": n, "listenMode": "active", "defaultName": False} for n in names]
    backend.team_data = {**copy.deepcopy(TEAM), "agents": agents}
    backend.route_extra = {"requests": [{"agentId": n.lower(), "task": f"task {i}"} for i, n in enumerate(names)]}
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Alpha, task 0. Bravo, task 1. Charlie, task 2. Delta, task 3. Echo, task 4")
    assert sorted(t["agentId"] for t in backend.turns) == ["alpha", "bravo", "charlie", "delta"]
    assert h._requests.items[4].status == "queued"
    assert room.written()["lightbulb.agent.echo.status"] == "working"  # queued shows as working
    answer(h, run_of(backend, "bravo"), "bravo", "done")
    await h.tick()
    await settle(h)
    assert backend.turns[-1]["agentId"] == "echo" and backend.turns[-1]["utterance"] == "task 4"


async def test_a_second_request_to_a_working_agent_waits_for_its_run():
    # understanding §4.5: a new request never steers a run; the agent's next row starts when the run ends
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    backend.route_extra = {
        "requests": [{"agentId": "forge", "task": "and the CI"}, {"agentId": "friday", "task": "and the board"}]
    }
    await say(h, "u1", "Forge, and the CI. Friday, and the board")
    assert len(backend.turns) == 3
    assert [(r.agent, r.status) for r in h._requests.items[3:]] == [("forge", "queued"), ("friday", "queued")]


async def test_with_no_router_a_multi_name_line_takes_todays_opening_name_path():
    h, room, backend, lives, clock, log = await three({"u1": "Prasiddha"})
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(h, "u1", LINE)
    assert [l.agent_id for l in lives] == ["pulse"] and [t["utterance"] for t in backend.turns] == [LINE]
    assert h._requests.items == [] and not [p for p in room.published if p[0] == "huddle.error"]


async def test_an_old_backend_without_requests_takes_todays_opening_name_path():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {"agentIds": list(TASKS)}
    await say(h, "u1", LINE)
    assert [l.agent_id for l in lives] == ["pulse"] and h._voice.path == "name"
    assert h._queue == [] and h._requests.items == []  # today: only the opening name is summoned


async def test_the_requests_still_run_when_the_acknowledging_voice_fails():
    h, room, backend, lives, clock, log = await three()
    h.fail.add("pulse")
    await say(h, "u1", LINE)
    assert len(backend.turns) == 3 and lives == []
    assert h._window is not None and backend.ends == []


async def test_the_window_stays_open_for_working_requests_and_ends_after_the_last():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await h.end_exchange()  # the acknowledging voice closes
    assert h._voice is None and h._window is not None and backend.ends == []
    await h.tick()
    assert backend.ends == []  # three requests still work: /turns needs the window
    h._requests.delivered({r.run_id for r in h._requests.items})
    await h.tick()
    assert backend.ends == [("call-1", 42)] and h._window is None


from huddle_agent import huddle as h_mod  # noqa: E402
from huddle_agent.huddle import THINKING_EVERY  # noqa: E402
from test_huddle import frame, packet  # noqa: E402


async def test_a_ready_answer_never_starts_over_a_person():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    talks(h, "u2", "Aden")
    await quiet(h, clock, 5)
    assert len(lives) == 1
    h.on_active_speakers(set())
    await quiet(h, clock, 0.5)
    assert len(lives) == 1  # quiet for less than REQUEST_QUIET_S
    await quiet(h, clock, h_mod.REQUEST_QUIET_S)
    assert [l.agent_id for l in lives] == ["pulse", "forge"]


async def test_a_failed_request_says_cant_reach_in_its_turn_and_is_never_posted():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.on_data(None, "call.frame", packet(frame(run_of(backend, "forge"), "forge", {"type": "error", "seq": 1})))
    assert [r.status for r in h._requests.items if r.agent == "forge"] == ["failed"]
    await quiet(h, clock)
    assert lives[-1].agent_id == "forge" and lives[-1].seed[-1] == (
        "developer",
        f'You could not do "{TASKS["forge"]}". Say exactly this, then stop: {h_mod.CANT_REACH}',
    )
    h.on_agent_state("forge", "speaking")  # said: understanding §4.8 posts only a failure nobody heard
    h.on_agent_state("forge", "listening")  # to its end: one still being said at hang-up is posted (§4.5)
    await h.clear_presence()
    assert backend.late_answers == []


async def test_an_answer_whose_asker_left_goes_to_the_room_when_no_pause_comes():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await h.on_participants({"u2": "Aden"})
    await settle(h)
    talks(h, "u2", "Aden")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    clock.t += h_mod.ASKER_GONE_S - 1
    await h.tick()
    await settle(h)
    assert backend.late_answers == []
    clock.t += 1
    await h.tick()
    await settle(h)
    assert [a[0] for a in backend.late_answers] == ["forge"] and "forge" not in h._news


async def test_in_a_meeting_a_requested_answer_is_spoken_not_posted_at_once():
    h, room, backend, lives, clock, log = await three({"meet:room-1": "Google Meet"}, setting="meeting")
    await h.on_final_transcript("meet:room-1", "Google Meet", LINE)
    await settle(h)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    assert backend.late_answers == []
    await quiet(h, clock, h_mod.MEET_QUIET_S)  # a real pause, not Live's breath
    assert lives[-1].agent_id == "forge"


async def test_the_floor_voice_hears_the_list_when_it_moves_at_most_every_three_seconds():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h._requests.cancel("friday")
    clock.t += THINKING_EVERY
    await h.tick()
    said = [c[1] for c in lives[0].calls if c[0] == "thinking" and c[1].startswith("Requests on this call")]
    assert said == [f'{h_mod.REQUESTS_NOTE}Forge is answering "{TASKS["forge"]}"']
    h._requests.cancel("forge")
    clock.t += 1
    await h.tick()
    assert sum(1 for c in lives[0].calls if c[0] == "thinking" and c[1].startswith("Requests on this call")) == 1


# -- fix round 1 --

from huddle_agent.frames import STALE_TURN_SECONDS  # noqa: E402


async def pump():
    for _ in range(30):
        await asyncio.sleep(0)


async def five():
    h, room, backend, lives, clock, log = make()
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    agents = [{"id": n.lower(), "name": n, "listenMode": "active", "defaultName": False} for n in names]
    backend.team_data = {**copy.deepcopy(TEAM), "agents": agents}
    backend.route_extra = {"requests": [{"agentId": n.lower(), "task": f"task {i}"} for i, n in enumerate(names)]}
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Alpha, task 0. Bravo, task 1. Charlie, task 2. Delta, task 3. Echo, task 4")
    return h, room, backend, lives, clock, log


def status(h, agent):
    return [r.status for r in h._requests.items if r.agent == agent]


async def test_an_error_frame_fails_the_request_and_frees_its_slot():
    h, room, backend, lives, clock, log = await five()
    h.on_data(None, "call.frame", packet(frame(run_of(backend, "bravo"), "bravo", {"type": "error", "seq": 1})))
    await settle(h)
    assert status(h, "bravo") == ["failed"] and backend.turns[-1]["agentId"] == "echo"


async def test_a_stop_cancels_a_working_request():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await say(h, "u2", "Forge, stop")
    assert status(h, "forge") == ["cancelled"] and run_of(backend, "forge") in backend.aborts
    assert room.written()["lightbulb.agent.forge.status"] == "idle"


async def test_a_stop_while_turns_is_in_flight_leaves_the_request_cancelled():
    h, room, backend, lives, clock, log = await three()
    backend.turn_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await pump()
    assert len(backend.turns) == 3
    await h.on_final_transcript("u2", "Aden", "Forge, stop")
    backend.turn_gate.set()
    await settle(h)
    assert status(h, "forge") == ["cancelled"] and run_of(backend, "forge") in backend.aborts


async def test_a_stop_while_route_decides_drops_only_that_agents_request():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await h.on_final_transcript("u2", "Aden", "Forge, stop")
    backend.route_gate.set()
    await settle(h)
    assert sorted(t["agentId"] for t in backend.turns) == ["friday", "pulse"] and status(h, "forge") == []


async def test_one_addressed_agent_and_one_mentioned_is_todays_name_summon():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "Forge, what did Friday say?")
    assert len(backend.routes) == 1  # the round trip happened
    assert [l.agent_id for l in lives] == ["forge"] and h._voice.path == "name" and h._queue == []
    assert room.written()["lightbulb.agent.friday.status"] == "idle" and h._requests.items == []


async def test_the_floor_holders_own_speaker_mentioning_another_agent_gets_no_second_answer():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "Forge, what our PRs look like")
    turns = len(backend.turns)
    backend.jev_to = "forge"  # /route picks the one addressed agent, Forge itself
    await say(h, "u1", "Forge, what did Friday say?")
    assert [l.agent_id for l in lives] == ["forge"] and h._queue == [] and len(backend.turns) == turns


async def test_a_name_with_a_mention_takes_the_floor_from_another_agent_as_today():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "Pulse, an update on the go-to-market strategy")
    await say(h, "u1", "Forge, what did Friday say?")
    assert [l.agent_id for l in lives] == ["pulse", "forge"] and h._voice.agent_id == "forge" and h._queue == []


async def test_a_non_speaker_mentioning_two_agents_over_a_speaking_voice_gets_todays_barge():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {}
    await say(h, "u1", "Pulse, an update on the go-to-market strategy")
    h.on_agent_state("pulse", "speaking")
    line = "yeah Forge and Friday both did that"
    await say(h, "u2", line)
    assert ("interrupt",) in lives[0].calls and h._voice.speaker == "u2"
    assert backend.turns[-1]["agentId"] == "pulse" and backend.turns[-1]["utterance"] == line


async def test_shutdown_with_a_working_request_posts_end_once():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await h.end_exchange(force=True)
    await h.tick()
    assert len(backend.ends) == 1 and h._window is None


async def test_a_stale_run_fails_its_request():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    clock.t += STALE_TURN_SECONDS + 1
    await h.tick()
    assert status(h, "forge") == ["failed"]


async def test_another_persons_line_during_route_does_not_drop_a_named_ask():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {"requests": backend.route_extra["requests"]}
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await h.on_final_transcript("u2", "Aden", "yeah sounds good")
    backend.route_gate.set()
    await settle(h)
    assert sorted(t["agentId"] for t in backend.turns) == sorted(TASKS) and lives[0].agent_id == "pulse"


async def test_an_older_lines_acknowledgement_does_not_take_a_newer_voices_floor():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what our PRs look like")
    await pump()
    assert h._voice.agent_id == "forge"
    backend.route_gate.set()
    await settle(h)
    assert [l.agent_id for l in lives] == ["forge"] and h._voice.agent_id == "forge"
    assert sorted(r.agent for r in h._requests.items) == sorted(TASKS)


from huddle_agent import gate  # noqa: E402


async def test_a_refused_delivery_is_not_retried_on_the_next_tick():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.fail.add("forge")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock)
    assert backend.sessions.count("forge") == 1
    await quiet(h, clock, 1)
    assert backend.sessions.count("forge") == 1  # no retry every tick
    await quiet(h, clock, gate.ANNOUNCE_QUIET)
    assert backend.sessions.count("forge") == 2


async def test_after_three_refused_deliveries_an_answer_is_posted_and_its_row_closes():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.fail.add("forge")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    for _ in range(4):
        await quiet(h, clock, gate.ANNOUNCE_QUIET)
    assert backend.sessions.count("forge") == h_mod.DELIVERY_TRIES
    assert [a[0] for a in backend.late_answers] == ["forge"] and "forge" not in h._news
    assert [r.status for r in h._requests.items if r.agent == "forge"] == ["delivered"]


async def test_after_three_refused_deliveries_a_cant_reach_line_is_posted_as_one_line():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.fail.add("forge")
    h.on_data(None, "call.frame", packet(frame(run_of(backend, "forge"), "forge", {"type": "error", "seq": 1})))
    for _ in range(4):
        await quiet(h, clock, gate.ANNOUNCE_QUIET)
    assert backend.sessions.count("forge") == h_mod.DELIVERY_TRIES
    # understanding §4.8: never said, so the room gets one line
    assert backend.late_answers == [("forge", run_of(backend, "forge"), TASKS["forge"], h_mod.FAILED_POST)]
    assert "forge" not in h._news
    assert [r.status for r in h._requests.items if r.agent == "forge"] == ["failed"]


async def _asker_gone(h, clock):
    await h.on_participants({"u2": "Aden"})
    await settle(h)
    talks(h, "u2", "Aden")


async def _window_ends(h, backend):
    for r in h._requests.items:
        if r.agent != "forge":
            h._requests.delivered({r.run_id})
    await h.end_exchange()
    await h.tick()
    assert backend.ends and h._window is None


async def test_a_no_post_answer_whose_asker_left_closes_its_row_and_the_window_can_end():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    h._requests.no_post("forge")
    await _asker_gone(h, clock)
    clock.t += h_mod.ASKER_GONE_S
    await h.tick()
    await settle(h)
    assert backend.late_answers == [] and "forge" not in h._news
    assert not [r for r in h._requests.open() if r.agent == "forge"]
    await _window_ends(h, backend)


async def test_an_answer_whose_post_fails_closes_its_row_and_the_window_can_end():
    h, room, backend, lives, clock, log = await three()

    async def down(*a):
        raise RuntimeError("backend down")

    backend.late_answer = down
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await _asker_gone(h, clock)
    clock.t += h_mod.ASKER_GONE_S
    await h.tick()
    await settle(h)
    assert "forge" not in h._news and not [r for r in h._requests.open() if r.agent == "forge"]
    await _window_ends(h, backend)


async def test_a_thinking_floor_voice_is_not_preempted_by_a_ready_answer():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h.on_agent_state("pulse", "thinking")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock, 5)
    assert len(lives) == 1
    h.on_agent_state("pulse", "listening")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge"]


async def test_a_floor_voice_waiting_on_its_driven_run_gives_the_floor_to_a_ready_answer():
    # 10-04 prod test: a teammate's answer sat unheard for two minutes behind the floor agent's own run
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h._voice.driven_run = run_of(backend, "pulse")  # the floor voice's own run is still working
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    h.on_agent_state("pulse", "thinking")
    await quiet(h, clock, 5)
    assert len(lives) == 1, "never while the floor voice is about to speak"
    h.on_agent_state("pulse", "listening")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge"]
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    answer(h, run_of(backend, "pulse"), "pulse", "Go-to-market is on track")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge", "pulse"], "its own answer in its turn"


async def test_a_delivery_waits_for_the_floor_lock():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await h._lock.acquire()  # a tap is opening a voice
    try:
        await asyncio.wait_for(quiet(h, clock), 1)
    finally:
        h._lock.release()
    assert len(lives) == 1
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge"]


async def test_a_stop_after_the_answer_is_ready_means_it_is_neither_spoken_nor_posted():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await say(h, "u1", "Forge, never mind")
    await quiet(h, clock, 5)
    assert "forge" not in [l.agent_id for l in lives] and "forge" not in h._news
    await h.clear_presence()
    assert backend.late_answers == []


async def test_a_two_name_line_that_route_holds_waits_and_merges_with_the_next_line():
    h, room, backend, lives, clock, log = await three()
    split = backend.route_extra
    backend.route_extra = {"hold": True}
    await say(h, "u1", "Pulse, Forge, we are trying to")
    assert lives == [] and backend.turns == [] and backend.summons == []
    backend.route_extra = split
    await say(h, "u1", "find the launch checklist")
    assert backend.routes[-1][1] == "Pulse, Forge, we are trying to find the launch checklist"
    assert len(backend.turns) == 3 and [l.agent_id for l in lives] == ["pulse"]


async def test_two_answers_ready_in_the_same_tick_go_in_request_order():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "friday"), "friday", "Two rooms were busy")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge"]  # Forge was asked before Friday
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "forge", "friday"]


async def test_a_second_role_named_agent_reaches_the_splitter():
    h, room, backend, lives, clock, log = make()
    roster = [{"id": a.lower(), "name": a, "listenMode": "active", "defaultName": True} for a in ("Finance", "Engineering")]
    backend.team_data = {**copy.deepcopy(TEAM), "agents": roster}
    backend.route_extra = {
        "requests": [{"agentId": "finance", "task": "check burn"}, {"agentId": "engineering", "task": "check deploy"}]
    }
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Finance, can you check burn? Engineering, can you check deploy?")
    assert len(backend.routes) == 1
    assert {t["agentId"]: t["utterance"] for t in backend.turns} == {"finance": "check burn", "engineering": "check deploy"}


@pytest.mark.parametrize("opening", ["session", "live"])  # before its seed is built, and after
async def test_an_answer_ready_while_the_ack_voice_opens_waits_for_the_ack(opening):
    h, room, backend, lives, clock, log = await three()
    gate = asyncio.Event()
    if opening == "session":
        backend.session_gate = gate
    else:
        h.gate["live"] = gate
    await h.on_final_transcript("u1", "Prasiddha", LINE)
    await pump()
    pulse = run_of(backend, "pulse")
    answer(h, pulse, "pulse", "GTM is on track")
    gate.set()
    await settle(h)
    seed = lives[0].seed
    assert seed[-1][1].startswith("You are on") and not any("GTM is on track" in x[1] for x in seed)
    assert status(h, "pulse") == ["ready"] and [n[0] for n in h._news["pulse"]] == [pulse]
    h.on_agent_state("pulse", "speaking")  # the acknowledgement
    h.on_agent_state("pulse", "listening")
    await quiet(h, clock)
    said = [c for c in lives[0].calls if c[0] == "commentary" and "GTM is on track" in c[1]]
    assert len(said) == 1 and status(h, "pulse") == ["delivered"] and "pulse" not in h._news
