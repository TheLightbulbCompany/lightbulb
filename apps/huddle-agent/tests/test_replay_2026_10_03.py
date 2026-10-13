"""Replay of the 2026-10-03 line in the founder's demo channel (conversation engine spec §1, §4): only
Pulse was summoned and got the whole line; Forge and Friday came 40 s and 60 s later.

Since 10-12 every voice is a mouth (owner ruling): the one acknowledgement is the first agent's fixed line for its
own request, and the other agents' tiles show theirs. Each answer is said word for word by its own agent's voice,
one voice at a time, at the pause after the line before it."""

from huddle_agent import huddle as h_mod
from test_huddle import answer, say, spoken
from test_request_engine import LINE, TASKS, quiet, run_of, three


async def test_the_2026_10_03_line_is_three_requests_started_at_once_with_one_acknowledgement():
    h, room, backend, lives, clock, log = await three()
    t0 = clock.t
    await say(h, "u1", LINE)
    assert len(backend.routes) == 1 and backend.routes[0][1] == LINE
    assert {t["agentId"]: t["utterance"] for t in backend.turns} == TASKS  # 3 requests, the right tasks
    assert clock.t == t0  # all three /turns went out in the tick of the final
    assert [x.agent_id for x in lives] == ["pulse"] and lives[0].calls == [("say", h_mod.ACKS[0])]  # one acknowledgement
    w = room.written()
    assert w["lightbulb.agent.forge.status"] == w["lightbulb.agent.friday.status"] == "working"  # their tiles say it


async def test_answers_come_one_voice_at_a_time_in_the_order_they_are_ready():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    spoken(h, lives[0])  # Pulse's acknowledgement
    answer(h, run_of(backend, "friday"), "friday", "Two rooms were busy")
    assert room.written()["lightbulb.agent.friday.status"] == "has_news"
    await quiet(h, clock, h_mod.MEET_QUIET_S)
    assert [x.agent_id for x in lives] == ["pulse", "friday"]  # the floor moves once the room is quiet
    assert ("close",) in lives[0].calls  # one voice at a time: Pulse's session closed as Friday's opened
    assert lives[1].calls == [("say", "Two rooms were busy")]  # word for word
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock)
    assert len(lives) == 2  # Friday is still speaking: nobody talks over it
    spoken(h, lives[1])
    await quiet(h, clock)
    assert [x.agent_id for x in lives] == ["pulse", "friday", "forge"]
    assert ("close",) in lives[1].calls
    spoken(h, lives[2])
    answer(h, run_of(backend, "pulse"), "pulse", "Go-to-market is on track")
    await quiet(h, clock)
    assert [x.agent_id for x in lives] == ["pulse", "friday", "forge", "pulse"]
    spoken(h, lives[3])
    assert [r.status for r in h._requests.items] == ["delivered"] * 3
    assert backend.late_answers == []
