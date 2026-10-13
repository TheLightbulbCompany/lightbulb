"""Understanding §4.4-§4.6 (Task B2): /route's `ops` turn a line into rows; small talk starts no run."""

import asyncio
import json

import pytest

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from test_huddle import (
    answer,
    frame,
    make,
    metric_count,
    ops,
    packet,
    say,
    settle,
    spoken,
    talks,
)
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
    written = []

    async def writer(persona, event):
        written.append((persona, event))
        return "Doing well, thanks."

    h._writer = writer
    backend.route_extra = ops(social="pulse")
    await say(h, "u1", "Hey guys, how's it going?")
    # a small text model is given the line and who said it, and the voice says its one sentence word for word
    assert written == [("You are Pulse.", 'Prasiddha just said to you: "Hey guys, how\'s it going?"')]
    assert [x.agent_id for x in lives] == ["pulse"] and lives[0].calls == [("say", "Doing well, thanks.")]
    assert backend.turns == [] and h._requests.items == [] and h._voice.driven_run is None
    assert (
        backend.route_bodies[0]["open"] == []
        and backend.route_bodies[0]["floor"] is None
    )


async def test_an_announced_list_opens_a_voice_that_waits():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(social="pulse", more=True)
    await say(h, "u1", "I got a couple questions, I'm just gonna say them all")
    assert [x.agent_id for x in lives] == ["pulse"] and lives[0].calls == [] and backend.turns == []


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
    assert lives[0].calls == [("say", h_mod.ACKS[0])]  # its own fixed line; Friday's tile shows Friday's
    assert metric_count(capsys, "live.request.created") == 3
    answer(h, first.run_id, "forge", "Two PRs are open.")
    spoken(h, lives[0])
    spoken(h, lives[0])
    await settle(h)
    assert first.status == "delivered" and h._requests.items[1].status == "working"
    assert (backend.turns[-1]["agentId"], backend.turns[-1]["utterance"]) == (
        "forge",
        "what shipped",
    )


async def test_unnamed_asks_with_a_voice_open_for_another_speaker_start_nothing():
    """As in a Meet (owner ruling 10-12): a line from someone the open voice is not with, naming no agent, is the
    transcript's. /route reads it only for whether it came between people; it starts nothing."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Pulse, how are we doing")
    backend.turns.clear()
    backend.route_extra = ops(requests=ASKS)
    await say(h, "u1", SAID)
    assert backend.sessions == ["pulse"] and h._queue == [] and backend.turns == [] and rows(h) == []
    assert backend.route_bodies[-1]["floor"] == {
        "agent": "pulse",
        "speaker": "Aden",
        "mine": False,
    }


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


async def test_everyone_stop_said_while_route_decides_starts_nothing():
    # review B2-2: the same speaker stops a line that is still at /route
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
    await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "everyone stop")
    backend.route_gate.set()
    await settle(h)
    assert backend.turns == [] and lives == [] and h._requests.items == []


@pytest.mark.xfail(strict=True, reason="a stop said while /route still decides the line it would stop: the mouth path silences only what is open when it is said (`_hush`), so the line's voice still opens and acknowledges it (owner decision owed)")
async def test_a_named_stop_said_while_route_decides_keeps_the_agent_silent():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    await h.on_final_transcript("u1", "Prasiddha", "what are the open PRs")
    await asyncio.sleep(0)
    await h.on_final_transcript("u1", "Prasiddha", "Forge, stop")
    backend.route_gate.set()
    await settle(h)
    assert not [c for x in lives for c in x.calls if c[0] == "say"]


async def test_a_stop_while_the_voice_opens_silences_it_and_calls_nothing_off():
    """Owner ruling 10-12: "Forge, stop" silences Forge at once; /route alone calls a request off. Its row runs,
    and its voice opens with nothing to say."""
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
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"]
    assert rows(h) == [("forge", "the open PRs", "working")] and h._hushed(h._requests.items[0].id)
    assert lives[0].calls == []


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
    assert [x.agent_id for x in lives] == ["forge"] and h._voice.driven_run is None
    assert lives[0].calls == [("say", h_mod.QUEUED_LINES[0])]  # next in line for it


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
    assert lives[0].calls == [("say", h_mod.ACKS[0])]
    # Jev's pick is summoned with the whole line (test_huddle)
    h, room, backend, lives, clock, log = await three()
    backend.route_extra, backend.jev_to = {}, "forge"
    await say(h, "u1", "can someone check the invoices")
    assert [x.agent_id for x in lives] == ["forge"] and backend.turns[0][
        "utterance"
    ] == "can someone check the invoices"
    # with a voice open, another speaker's unnamed line starts nothing: the Meet's rule (owner ruling 10-12)
    backend.jev_to = "friday"
    await say(h, "u2", "and the board too")
    assert h._queue == [] and len(backend.turns) == 1


async def test_a_driven_row_whose_turn_fails_is_failed_and_its_failure_is_said_once():
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
    spoken(h, lives[0])  # its acknowledgement; then its failure, in its turn
    spoken(h, lives[0])
    assert [c for c in lives[0].calls if c[1] in h_mod.COULD_NOT] == [("say", h_mod.COULD_NOT[0])]
    await h.clear_presence()
    assert not [a for a in backend.late_answers if a[3] == h_mod.FAILED_POST], "said: not posted"


async def test_a_stop_while_the_voice_opens_silences_only_that_agent():
    # owner ruling 10-12: the stop silences Forge and calls nothing off; Friday's row runs as asked
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
    assert [s for _, _, s in rows(h)] == ["working", "working"]
    assert sorted(t["agentId"] for t in backend.turns) == ["forge", "friday"]
    assert lives[0].calls == [], "Forge is silenced: no acknowledgement"


# -- Task B3: the floor speaker's lines are understood; a delegation binds (understanding §4.6) --

PRS = "what are the open PRs"


async def forever(_seconds):
    await asyncio.Event().wait()


async def floor_open():
    """Forge's voice is open for Prasiddha; its named summon's driven run is retired (test_huddle `summoned`)."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", "Forge, are you there")
    spoken(h, lives[0])
    h._runs.abort(backend.turns[0]["runId"])
    backend.turns.clear()
    backend.routes.clear()
    backend.route_extra = ops(requests=[("forge", "the open PRs", "fit")])
    return h, room, backend, lives, clock, log


async def test_the_floor_speakers_row_runs_and_its_answer_is_spoken():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    assert [t["utterance"] for t in backend.turns] == ["the open PRs"]
    assert lives[0].calls[-1] == ("say", h_mod.ACKS[1])
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    spoken(h, lives[0])
    assert lives[0].calls[-1] == ("say", "Two PRs are open.")
    spoken(h, lives[0])
    assert [s for _, _, s in rows(h)] == ["delivered"]


async def test_a_failed_route_for_a_floor_line_leaves_todays_single_run():
    # the voice hears nothing itself: with nothing understood, the whole line is its run (D §5), acknowledged
    for failure in ("error", "no ops"):
        h, room, backend, lives, clock, log = await floor_open()
        if failure == "error":
            backend.route_error = BackendError(503, "huddles_upstream", None)
        else:
            backend.route_extra = {}
        await say(h, "u1", PRS)
        assert h._requests.items == [] and len(backend.routes) == 1
        assert [(t["utterance"], t["steerRunId"]) for t in backend.turns] == [(PRS, None)]
        assert lives[0].calls[-1] == ("say", h_mod.ACKS[1])
        assert not [p for topic, p, _ in room.published if topic == h_mod.ERROR_TOPIC]


async def test_lightbulb_from_someone_else_closes_the_voice_and_routes_as_with_none():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u2", "Lightbulb, check Aden's email")
    assert ("close",) in lives[0].calls and [t["agentId"] for t in backend.turns] == ["friday"]
    assert (lives[-1].agent_id, lives[-1].speaker) == ("friday", "u2")
    assert lives[-1].calls == [("say", h_mod.ACKS[1])]
    # /route down: the line asked nobody, and its speaker is told
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_error = BackendError(503, "huddles_upstream", None)
    await say(h, "u2", "Lightbulb, check Aden's email")
    assert backend.turns == [] and [p for t, p, to in room.published if t == h_mod.ERROR_TOPIC and to == ["u2"]]


# -- review round 1: an understanding belongs to its speech --

ACK = "You are on the open PRs. Say that in one short sentence, then wait."


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
    assert lives[0].calls == [("say", h_mod.ACKS[0])]
    answer(h, first.run_id, "forge", "CI is green.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns] == [
        "check CI",
        "tell me what shipped",
    ]
    spoken(h, lives[0])
    spoken(h, lives[0])
    assert [s for _, _, s in rows(h)] == ["delivered", "working"]


async def test_a_named_greeting_starts_no_run():
    h, room, backend, lives, clock, log = await three()

    async def writer(persona, event):
        return "Going well."

    h._writer = writer
    backend.route_extra = ops(social="forge")
    await say(h, "u1", "Forge, how's it going?")
    assert backend.turns == [] and h._requests.items == [] and lives[0].calls == [("say", "Going well.")]


async def test_a_bare_summons_is_answered_here_after_its_speaker_stays_quiet():
    """A name alone is held for what it is called for (CALL_ON_S); nothing more came: the agent was called on."""
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops()
    await say(h, "u1", "Forge?")
    assert lives == [] and backend.routes == []
    await quiet(h, clock, h_mod.CALL_ON_S)
    assert [x.agent_id for x in lives] == ["forge"] and lives[0].calls == [("say", h_mod.HERE[0])]
    assert backend.turns == []


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
    assert lives[0].calls == [("say", h_mod.ACKS[0])]  # its own; Friday's tile shows Friday's
    assert room.written()["lightbulb.agent.friday.working"] == "the numbers from finance"


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
    # its voice is prepared beside /route too, and is not the room's until the line is applied
    assert h._voice.live is None and backend.turns == [] and not [c for x in lives for c in x.calls]
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
        assert lives[0].calls == [("say", h_mod.ACKS[0])]


async def test_everyone_stop_from_another_speaker_while_the_summon_waits_runs_nothing():
    for answer_ in (ops(requests=TWO), {}):
        h, room, backend, lives, clock, log = await three()
        backend.route_gate = asyncio.Event()
        backend.route_extra = answer_
        await h.on_final_transcript("u1", "Prasiddha", CI)
        for _ in range(5):
            await asyncio.sleep(0)
        await h.on_final_transcript("u2", "Aden", "everyone stop")
        backend.route_gate.set()
        await settle(h)
        assert backend.turns == [] and h._requests.items == [], answer_
        assert [x.agent_id for x in lives] == ["forge"] and lives[0].calls == []


@pytest.mark.xfail(strict=True, reason="a stop said while /route still decides the line it would stop: the mouth path silences only what is open when it is said (`_hush`), so the line's voice still opens and acknowledges it (owner decision owed)")
async def test_a_named_stop_from_another_speaker_while_the_summon_waits_keeps_it_silent():
    h, room, backend, lives, clock, log = await three()
    backend.route_gate = asyncio.Event()
    backend.route_extra = ops(requests=TWO)
    await h.on_final_transcript("u1", "Prasiddha", CI)
    for _ in range(5):
        await asyncio.sleep(0)
    await h.on_final_transcript("u2", "Aden", "Forge, stop")
    backend.route_gate.set()
    await settle(h)
    assert not [c for x in lives for c in x.calls if c[0] == "say"]


async def test_a_named_line_to_the_agent_holding_the_floor_for_another_speaker():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Forge, are you there")
    spoken(h, lives[0])
    h._runs.abort(backend.turns[0]["runId"])
    backend.turns.clear()
    backend.route_extra = ops(requests=TWO)
    await say(h, "u1", CI)
    assert len(lives) == 1 and h._voice.speaker == "u1"
    assert [t["utterance"] for t in backend.turns] == ["check CI"]
    assert [s for _, _, s in rows(h)] == ["working", "queued"]
    assert h._requests.items[0].run_id == h._voice.driven_run
    assert lives[0].calls[-1] == ("say", h_mod.ACKS[1])
    # without ops: nothing understood, so the whole line is its run (a mouth writes no small talk for a request)
    h, room, backend, lives, clock, log = await three()
    await say(h, "u2", "Forge, are you there")
    spoken(h, lives[0])
    backend.turns.clear()
    await say(h, "u1", CI)
    assert [t["utterance"] for t in backend.turns] == [CI] and h._requests.items == []
    assert lives[0].calls[-1] == ("say", h_mod.ACKS[1])


# -- review round 2: a delegation is matched to its speech by text --


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


async def test_a_rowless_driven_run_keeps_its_agent_busy():
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

    async def refused(agent_id, silent=False):
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
    """The fixed lines a voice was ordered to say about requests (`Huddle._say_ack`, `_steer_ack`)."""
    lines = (*h_mod.ACKS, *h_mod.QUEUED_LINES, *h_mod.STEER_ACKS)
    return [c[1] for c in live.calls if c[0] == "say" and c[1] in lines]


async def test_each_ask_of_the_floor_speaker_is_acknowledged_once_for_the_voices_own_rows():
    """A mouth says one fixed line per ask, about its own row: on it, or next. Another agent's rows show on that
    agent's tile; nothing reads them out (10-07 rig: a written acknowledgement read the worker's notes out)."""
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[("forge", "the open PRs", "fit"), ("friday", "Aden's email", "fit")]
    )
    await say(h, "u1", "what are the open PRs and check Aden's email")
    spoken(h, lives[0])
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    assert [s for _, _, s in rows(h)] == ["working", "working", "queued"]
    spoken(h, lives[0])
    await quiet(h, clock, 2)
    await quiet(h, clock, 2)
    assert acks(lives[0]) == [h_mod.ACKS[0], h_mod.ACKS[1], h_mod.QUEUED_LINES[0]]
    assert room.written()["lightbulb.agent.friday.working"] == "Aden's email"


async def test_a_single_own_working_row_gets_one_fixed_line():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    spoken(h, lives[0])
    await quiet(h, clock, 2)
    assert acks(lives[0]) == [h_mod.ACKS[0], h_mod.ACKS[1]] and [s for _, _, s in rows(h)] == ["working"]


async def test_a_row_queued_behind_the_floor_agents_first_is_next_in_line_once():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    spoken(h, lives[0])
    await quiet(h, clock, 2)
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    spoken(h, lives[0])
    await quiet(h, clock, 2)
    await quiet(h, clock, 2)
    assert acks(lives[0]) == [h_mod.ACKS[0], h_mod.ACKS[1], h_mod.QUEUED_LINES[0]]


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
    # its voice was saying its acknowledgement: cut, as a stop cuts it (review B5-1)
    assert lives[0].calls[-2:] == [("say", h_mod.ACKS[1]), ("interrupt",)]
    n = len(lives[0].calls)
    answer(h, run_id, "forge", "Two PRs are open.")
    await settle(h)
    await quiet(h, clock, 2)
    assert not [c for c in lives[0].calls[n:] if c[0] == "say"]
    await h.clear_presence()
    assert backend.late_answers == [] and len(backend.turns) == 1
    assert metric_count(capsys, "live.request.cancelled") == 1


async def test_cancel_of_a_queued_row_never_runs_it_and_drops_its_ready_answer():
    h, room, backend, lives, clock, log = await floor_open()
    await say(h, "u1", PRS)
    spoken(h, lives[0])
    backend.route_extra = ops(requests=[("forge", "what shipped", "fit")])
    await say(h, "u1", "and what shipped")
    spoken(h, lives[0])
    backend.route_extra = ops(cancel=["r2", "r9"])  # an unknown id is ignored
    await say(h, "u1", "skip the shipped one")
    answer(h, backend.turns[0]["runId"], "forge", "Two PRs are open.")
    await quiet(h, clock, h_mod.MEET_QUIET_S)  # (its request no longer the newest: it waits for a real pause)
    spoken(h, lives[0])  # its voice said it: nothing of it is left to post
    await settle(h)
    assert [s for _, _, s in rows(h)] == ["delivered", "cancelled"] and len(
        backend.turns
    ) == 1
    # a ready answer someone cancels is dropped too: never spoken, never posted
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "and check Aden's email")
    friday = h._requests.items[-1]
    answer(h, friday.run_id, "friday", "One new email.")
    assert friday.status == "ready" and [u.units for u in h._ledger if u.agent == "friday"] == [["One new email."]]
    backend.route_extra = ops(cancel=[friday.id])
    await say(h, "u1", "never mind the email")
    assert friday.status == "cancelled" and not [u for u in h._ledger if u.agent == "friday"]
    assert ("say", "One new email.") not in [c for x in lives for c in x.calls]
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
    spoken(h, lives[0])  # its first acknowledgement; then its word for the follow-up (10-08: there was none)
    assert lives[0].calls[-1] == ("say", h_mod.STEER_ACKS[0])
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
    """Forge's answer to the three-name line, said by Forge's own voice at the pause after Pulse's acknowledgement."""
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    spoken(h, lives[0])
    run = run_of(backend, "forge")
    answer(h, run, "forge", text)
    await quiet(h, clock, h_mod.MEET_QUIET_S)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce" and lives[-1].calls == [("say", text)]
    return h, room, backend, lives, clock, run


async def failed_forge():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    run = run_of(backend, "forge")
    h.on_data(
        None, "call.frame", packet(frame(run, "forge", {"type": "error", "seq": 1}))
    )
    talks(h, "u2", "Aden")  # someone talks on: its CANT_REACH line waits, unsaid
    return h, backend, clock, run


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


async def test_a_cancel_while_its_answer_is_being_said_cuts_the_voice():
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(requests=[("friday", "Aden's email", "fit")])
    await say(h, "u1", "check Aden's email")
    friday = h._requests.items[-1]
    answer(h, friday.run_id, "friday", "One new email.")
    await settle(h)
    await quiet(h, clock, 3)
    v = h._voice
    assert v.agent_id == "friday" and v.path == "announce" and v.speaker == "u1"
    assert lives[-1].calls == [("say", "One new email.")]
    backend.route_extra = ops(cancel=[friday.id])
    await say(h, "u1", "never mind the email")
    assert friday.status == "cancelled" and lives[-1].calls[-1] == ("interrupt",)
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


def test_wire_limits_a_long_teammate_name_and_task():
    from huddle_agent.request_list import RequestList

    rl = RequestList()
    rl.settle(1, "u1", [{"agentId": "forge", "task": "x" * 300, "by": "fit"}], 0.0)
    assert all(len(line) <= 200 for line in rl.lines(lambda _a: "N" * 90))


# -- last re-review: an unfinished delegation before its final runs once; a delegated call-off is told so --


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
    h, room, backend, lives, clock, log = await floor_open()  # Forge's voice is open for Prasiddha
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
    spoken(h, lives[0])  # Forge's acknowledgement
    answer(h, run_of(backend, "pulse"), "pulse", "GTM is on track.")
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "pulse" and h._voice.path == "announce", (
        "Pulse says it; Forge's run goes on"
    )
    spoken(h, lives[-1])
    assert rows(h)[1][2] == "delivered" and rows(h)[0][2] == "working"
    answer(h, run_of(backend, "forge"), "forge", "No new mail.")
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce", (
        "the floor agent's answer in its turn"
    )
    spoken(h, lives[-1])
    assert [r[2] for r in rows(h)] == ["delivered", "delivered"]
    await h.clear_presence()
    assert backend.late_answers == []


async def test_noise_never_holds_a_ready_answer_back():
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    spoken(h, lives[0])
    h.on_active_speakers({"u1"})  # an open laptop microphone: typing, a fan. No words.
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    await quiet(h, clock, h_mod.MEET_QUIET_S)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce"


async def test_who_is_talking_follows_the_speech_detector():
    h, room, backend, lives, clock, log = await three()
    h.on_active_speakers({"u2"})
    h.on_interim_transcript("u2", "Aden", "words on their own")
    assert h._speaking == set(), (
        "an interim alone is not talking: the detector says who talks"
    )
    h.on_speech("u2", True)
    assert h._speaking == {"u2"}
    h.on_speech("u2", False)
    assert h._speaking == set(), "the detector's stop ends it"
    talks(h, "u2", "Aden")
    h.stt_stopped("u2")
    assert h._speaking == set(), "a transcriber that stopped holds nobody as talking"
    h.on_speech("u9", True)
    assert h._voiced == set(), "only people in the call"
    talks(h, "u2", "Aden")
    await h.on_participants({"u1": "Prasiddha"})
    assert h._speaking == set() and h._voiced == set(), "nobody who left is talking"


async def test_a_second_sentence_keeps_its_speaker_talking_past_the_first_ones_final():
    # review: the final of sentence A arrives while B is being said; nothing may start over B
    h, room, backend, lives, clock, log = await three()
    await say(h, "u1", LINE)
    spoken(h, lives[0])
    answer(h, run_of(backend, "forge"), "forge", "Three PRs are open")
    talks(h, "u2", "Aden")  # sentence A
    h.on_speech("u2", False)
    h.on_speech("u2", True)  # sentence B begins
    await h.on_final_transcript("u2", "Aden", "that was sentence A")
    assert h._speaking == {"u2"}
    await quiet(h, clock, 3)
    assert len(lives) == 1, "Forge's answer waits: Aden is still talking"
    h.on_speech("u2", False)
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "forge" and h._voice.path == "announce"


async def test_a_narrating_run_never_holds_the_floor():
    """Nothing of a run is said as it streams: a teammate's ready answer is said while it narrates."""
    h, room, backend, lives, clock, log = await floor_open()
    backend.route_extra = ops(
        requests=[
            ("forge", "Aden's email", "floor"),
            ("pulse", "the GTM update", "fit"),
        ]
    )
    await say(h, "u1", "check Aden's email, and what is the GTM update")
    spoken(h, lives[0])
    forge_run = run_of(backend, "forge")
    h.on_data(None, "call.frame", packet(frame(forge_run, "forge", {"type": "chunk", "content": "Let me look. ", "seq": 1})))
    h.on_data(None, "call.frame", packet(frame(forge_run, "forge", {"type": "tool_start", "tool": "mail", "seq": 2})))
    answer(h, run_of(backend, "pulse"), "pulse", "GTM is on track.")
    await quiet(h, clock, 3)
    assert lives[-1].agent_id == "pulse" and h._voice.path == "announce"
    assert not [c for c in lives[0].calls if "Let me look" in str(c)]


def test_a_slow_run_is_not_lost():
    from huddle_agent import frames

    assert frames.STALE_TURN_SECONDS >= 300, (
        "a run silent for 135 s then answered (10-04 prod test)"
    )


# -- 10-04 prod Meet + Live run 3: an answer that beats its voice is still said; a called-off cut answer is not
# posted; a meeting's speaker is not called "Meeting" --


async def test_an_answer_that_arrives_while_its_voice_opens_is_said_when_it_is_live():
    # the first Meet question got a greeting and no answer: sentences that beat the voice were dropped as "spoken"
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    h.gate["live"] = asyncio.Event()
    await h.on_final_transcript(
        "u1", "Prasiddha", "Forge, what PRs are open right now?"
    )
    for _ in range(30):
        await asyncio.sleep(0)
    assert lives == [] and [t["utterance"] for t in backend.turns] == [
        "what PRs are open right now"
    ]
    answer(
        h, backend.turns[0]["runId"], "forge", "Six PRs are open."
    )  # the whole answer, before the voice is live
    h.gate["live"].set()
    await settle(h)
    # said by the voice once it is live, in the place of its acknowledgement
    assert lives[0].calls == [("say", "Six PRs are open.")]
    spoken(h, lives[0])
    assert rows(h) == [("forge", "what PRs are open right now", "delivered")]
    await h.clear_presence()
    assert backend.late_answers == [], "said, so not posted"


async def test_an_answer_whose_voice_never_opens_is_not_lost():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    h.gate["live"] = asyncio.Event()
    h.fail.add("forge")
    await h.on_final_transcript(
        "u1", "Prasiddha", "Forge, what PRs are open right now?"
    )
    for _ in range(30):
        await asyncio.sleep(0)
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")
    h.gate["live"].set()
    while h._tasks:  # (the voice prepared beside /route raises: settle() would re-raise it)
        await asyncio.gather(*list(h._tasks), return_exceptions=True)
        await asyncio.sleep(0)
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == ["Six PRs are open."], (
        "its voice failed: the room gets it"
    )


async def test_a_meetings_speaker_is_named_or_someone_never_meeting():
    h, room, backend, lives, clock, log = make(setting="meeting")
    await h.on_participants({"meet:r1": "Meeting"})
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    await say(h, "meet:r1", "Forge, what PRs are open right now?")
    said = " ".join(str(c) for c in lives[0].calls) + " ".join(
        str(s) for s in lives[0].seed
    )
    assert "Meeting is talking" not in said and "Meeting asked" not in said
    # (a mouth's voice is prepared while /route decides, so no note about the asker is written into its seed)
    assert backend.turns[0]["speakerName"] == "Someone"


def held(h):
    return [r.run_id for r in h._runs._runs.values() if r.held]


async def test_a_voice_that_fails_at_once_never_leaves_its_run_held():
    # review: the voice failed before the spawned drive ran; the run was held afterwards and never released
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    h.fail.add("forge")
    await say(h, "u1", "Forge, what PRs are open right now?")
    assert held(h) == []
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")
    await settle(h)
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == ["Six PRs are open."]


async def test_a_held_answer_is_said_once_the_voice_is_live_and_a_stop_leaves_nothing_held():
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    h.gate["live"] = asyncio.Event()
    await h.on_final_transcript(
        "u1", "Prasiddha", "Forge, what PRs are open right now?"
    )
    for _ in range(30):
        await asyncio.sleep(0)
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")
    h.gate["live"].set()
    await settle(h)
    assert lives[0].calls == [("say", "Six PRs are open.")]
    assert held(h) == []
    h2, room, backend2, lives2, clock2, log2 = await three()
    backend2.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    h2.gate["live"] = asyncio.Event()
    await h2.on_final_transcript(
        "u1", "Prasiddha", "Forge, what PRs are open right now?"
    )
    for _ in range(30):
        await asyncio.sleep(0)
    await h2.on_final_transcript("u1", "Prasiddha", "Forge, stop")
    h2.gate["live"].set()
    await settle(h2)
    assert held(h2) == [] and backend2.late_answers == []


async def test_an_answer_for_a_voice_that_dies_as_it_speaks_goes_to_the_room_once():
    # review: the voice's first order raised (dead session): its answer is never spoken into it, and not lost
    h, room, backend, lives, clock, log = await three()
    backend.route_extra = ops(
        requests=[("forge", "what PRs are open right now", "name")]
    )
    opened = h._live_factory

    async def dead_on_arrival(**kw):
        live = await opened(**kw)

        def dead(*a, **k):
            raise RuntimeError("AgentSession isn't running")

        live.say = dead
        return live

    h._live_factory = dead_on_arrival
    h.gate["live"] = asyncio.Event()
    await h.on_final_transcript(
        "u1", "Prasiddha", "Forge, what PRs are open right now?"
    )
    for _ in range(30):
        await asyncio.sleep(0)
    answer(h, backend.turns[0]["runId"], "forge", "Six PRs are open.")
    h.gate["live"].set()
    await settle(h)
    assert held(h) == [] and not [c for c in lives[0].calls if c[0] == "say"]
    await h.clear_presence()
    assert [a[3] for a in backend.late_answers] == ["Six PRs are open."]

