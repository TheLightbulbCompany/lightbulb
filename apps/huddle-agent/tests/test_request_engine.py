"""Conversation engine §3.2-§3.5: a turn naming several agents."""

import asyncio
import copy

from huddle_agent.backend import BackendError
from test_huddle import TEAM, answer, make, say, settle

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
    assert backend.routes == [] and h._requests.items == [] and [l.agent_id for l in lives] == ["forge"]
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


async def test_a_second_request_to_a_working_agent_steers_its_run():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    first = {a: run_of(backend, a) for a in TASKS}
    backend.steer = True
    backend.route_extra = {
        "requests": [{"agentId": "forge", "task": "and the CI"}, {"agentId": "friday", "task": "and the board"}]
    }
    await say(h, "u1", "Forge, and the CI. Friday, and the board")
    assert [(t["agentId"], t["utterance"], t["steerRunId"]) for t in backend.turns[3:]] == [
        ("forge", "and the CI", first["forge"]),
        ("friday", "and the board", first["friday"]),
    ]
    assert [r.run_id for r in h._requests.items if r.agent == "forge"] == [first["forge"]] * 2


async def test_with_no_router_a_multi_name_line_takes_todays_opening_name_path():
    h, room, backend, lives, clock, log = await three({"u1": "Prasiddha"})
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(h, "u1", LINE)
    assert [l.agent_id for l in lives] == ["pulse"] and [t["utterance"] for t in backend.turns] == [LINE]
    assert h._requests.items == [] and not [p for p in room.published if p[0] == "huddle.error"]


async def test_an_old_backend_without_requests_keeps_todays_queue():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = {"agentIds": list(TASKS)}
    await say(h, "u1", LINE)
    assert [l.agent_id for l in lives] == ["pulse"] and [q[0] for q in h._queue] == ["forge", "friday"]
    assert h._requests.items == []


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
    h.on_active_speakers({"u2"})
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
    assert lives[-1].agent_id == "forge" and h_mod.CANT_REACH in lives[-1].seed[-1][1]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_an_answer_whose_asker_left_goes_to_the_room_when_no_pause_comes():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    await h.on_participants({"u2": "Aden"})
    await settle(h)
    h.on_active_speakers({"u2"})
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
    await quiet(h, clock)
    assert lives[-1].agent_id == "forge"


async def test_the_floor_voice_hears_the_list_when_it_moves_at_most_every_three_seconds():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    h._requests.cancel("friday")
    clock.t += THINKING_EVERY
    await h.tick()
    said = [c[1] for c in lives[0].calls if c[0] == "thinking" and c[1].startswith("Requests on this call")]
    assert said == [f'Requests on this call:\nForge is answering "{TASKS["forge"]}"']
    h._requests.cancel("forge")
    clock.t += 1
    await h.tick()
    assert sum(1 for c in lives[0].calls if c[0] == "thinking" and c[1].startswith("Requests on this call")) == 1
