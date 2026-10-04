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
        "You are on the open PRs and on what shipped. Friday is on Aden's email."
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
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    backend.turn_results = [BackendError(400, "bad_request", None)]
    await say(h, "u1", "what are the open PRs")
    assert rows(h) == [("forge", "the open PRs", "failed")]
    assert (
        sum(c[:2] == ("commentary", h_mod.CANT_REACH) for c in lives[0].calls) == 1
        and h._news == {}
    )
