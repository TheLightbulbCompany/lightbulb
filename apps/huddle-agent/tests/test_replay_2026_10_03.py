"""Replay of the 2026-10-03 line in the founder's demo channel (conversation engine spec §1, §4): only
Pulse was summoned and got the whole line; Forge and Friday came 40 s and 60 s later."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, say
from test_request_engine import LINE, TASKS, quiet, run_of, three

ACK = (
    "You are on an update on the go-to-market strategy. Forge is on what our PRs look like. "
    "Friday is on a summary of our rooms and the last conversation from the last day. "
    "Say that in one short sentence, then wait."
)


async def test_the_2026_10_03_line_is_three_requests_started_at_once_with_one_acknowledgement():
    h, room, backend, lives, clock, log = await three()
    t0 = clock.t
    await say(h, "u1", LINE)
    assert len(backend.routes) == 1 and backend.routes[0][1] == LINE
    assert {t["agentId"]: t["utterance"] for t in backend.turns} == TASKS  # 3 requests, the right tasks
    assert clock.t == t0  # all three /turns went out in the tick of the final
    assert [l.agent_id for l in lives] == ["pulse"] and lives[0].seed == [("developer", ACK)]  # one acknowledgement
    assert lives[0].calls.count(("nudge",)) == 1


async def test_answers_come_one_voice_at_a_time_in_the_order_they_are_ready():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    answer(h, run_of(backend, "friday"), "friday", "Two rooms were busy")
    assert room.written()["lightbulb.agent.friday.status"] == "has_news"
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "friday"]  # the floor moves once the room is quiet
    assert ("close",) in lives[0].calls  # one voice at a time: Pulse's session closed as Friday's opened
    assert lives[1].seed[-1] == ("developer", f"You finished {TASKS['friday']}: Two rooms were busy. Tell the room now.")
    h.on_agent_state("friday", "speaking")
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock)
    assert len(lives) == 2  # Friday is still speaking: nobody talks over it
    h.on_agent_state("friday", "listening")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "friday", "forge"]
    assert ("close",) in lives[1].calls
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    answer(h, run_of(backend, "pulse"), "pulse", "Go-to-market is on track")
    await quiet(h, clock)
    assert [l.agent_id for l in lives] == ["pulse", "friday", "forge", "pulse"]
    h.on_agent_state("pulse", "speaking")
    assert [r.status for r in h._requests.items] == ["delivered"] * 3
    assert backend.late_answers == [] and h_mod.REQUEST_QUIET_S == 1
