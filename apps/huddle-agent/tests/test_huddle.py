import asyncio
import copy
import json

from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from huddle_agent.huddle import HANDOFF_SECONDS, IDLE_SECONDS, THINKING_EVERY, Huddle
from huddle_agent.transcript import address

TEAM = {
    "agents": [
        {"id": "ceo", "name": "Ada", "listenMode": "active", "defaultName": False},
        {"id": "forge", "name": "Forge", "listenMode": "active", "defaultName": False},
        {"id": "gauge", "name": "Gauge", "listenMode": "active", "defaultName": False},
        {"id": "fin", "name": "Finance", "listenMode": "active", "defaultName": True},
    ],
    "people": [{"userId": "u1", "name": "Prasiddha"}, {"userId": "u2", "name": "Aden"}],
    "ceoId": "ceo",
}
SUMMON = {"callId": "call-1", "deadline": 10_000, "budgetSeconds": 600, "joined": False}


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


class FakeRoom:
    def __init__(self):
        self.published: list[tuple[str, dict, list]] = []
        self.shut = False
        self.log: list[str] | None = None
        self.attrs: list[dict] = []

    def set_attributes(self, attrs):
        self.attrs.append(attrs)

    def publish(self, topic, payload, to):
        self.published.append((topic, payload, to))

    def shutdown(self):
        self.shut = True
        self.log.append("shutdown")

    def notices(self):
        return [(p, to) for topic, p, to in self.published if topic == "live.notice"]

    def written(self):
        """Every attribute key ever written, with its last value."""
        out = {}
        for a in self.attrs:
            out.update(a)
        return out


class FakeBackend:
    def __init__(self, log):
        self.log = log
        self.team_data = copy.deepcopy(TEAM)
        self.summons: list[str] = []
        self.summon_names: list[str | None] = []
        self.summon_error: BackendError | None = None
        self.sessions: list[str] = []
        self.session_extra: dict[str, dict] = {}
        self.session_gate: asyncio.Event | None = None
        self.routes: list[tuple] = []
        self.route_to = "forge"
        self.route_humans: list = []
        self.jev_to = (
            None  # a line with no name: Jev picks nobody unless a test says otherwise
        )
        self.route_extra: dict = {}  # e.g. {"hold": True}, {"agentIds": [...]}
        self.route_bodies: list[dict] = []
        self.no_posts: list[str] = []
        self.route_error: BackendError | None = None
        self.route_gate: asyncio.Event | None = None
        self.turns: list[dict] = []
        self.turn_results: list = []  # BackendErrors raised in order, then success
        self.turn_gate: asyncio.Event | None = None
        self.aborts: list[str] = []
        self.steer = False  # the bridge accepts a steer into the running run
        self.late_steers: list[dict] = []
        self.ends: list[tuple[str, int]] = []
        self.presences: list[list[str]] = []
        self.versions: list[int] = []
        self.team_calls = 0
        self.transcripts: list[tuple[str, list[dict]]] = []
        self.finals: list[bool] = []
        self.late_answers: list[tuple[str, str, str, str]] = []
        self.transcript_gate: asyncio.Event | None = None
        self.presence_gate: asyncio.Event | None = None

    async def team(self):
        self.team_calls += 1
        return copy.deepcopy(self.team_data)

    async def route(
        self,
        speaker,
        utterance,
        transcript,
        humans=None,
        *,
        floor=None,
        voice_runs=(),
        at=(),
        setting="live",
        unclaimed=False,
    ):
        self.routes.append((speaker, utterance, transcript))
        self.route_humans.append(humans)
        self.route_bodies.append(
            {
                "floor": floor,
                "voiceRuns": list(voice_runs),
                "at": list(at),
                "setting": setting,
                "unclaimed": unclaimed,
            }
        )
        if self.route_error:
            raise self.route_error
        extra, gate = (
            self.route_extra,
            self.route_gate,
        )  # this call's answer, however long it takes
        if gate:
            await gate.wait()
        agent = self.route_to if humans is None else self.jev_to
        return {"agentId": agent, "agentIds": [agent] if agent else [], **extra}

    async def no_post(self, run_id):
        self.no_posts.append(run_id)
        return {"noPost": True}

    async def summon(self, speaker, speaker_name=None):
        self.summons.append(speaker)
        self.summon_names.append(speaker_name)
        await asyncio.sleep(0)  # a real POST yields
        if self.summon_error:
            raise self.summon_error
        return SUMMON

    async def session(self, agent_id):
        self.sessions.append(agent_id)
        if self.session_gate:
            await self.session_gate.wait()
        name = next(a["name"] for a in self.team_data["agents"] if a["id"] == agent_id)
        return {
            "agentId": agent_id,
            "name": name,
            "voice": f"voice-{agent_id}",
            "persona": f"You are {name}.",
            "askName": False,
            **self.session_extra.get(agent_id, {}),
        }

    async def turn(
        self,
        call_id,
        speaker,
        agent_id,
        run_id,
        utterance,
        transcript,
        steer_run_id=None,
        steer_only=False,
        speaker_name=None,
        requests=(),
    ):
        if (
            steer_only
        ):  # a late-line steer (routing v2 §4.3): kept apart from the real turns
            self.late_steers.append(
                {
                    "speaker": speaker,
                    "agentId": agent_id,
                    "utterance": utterance,
                    "steerRunId": steer_run_id,
                }
            )
            return {"runId": steer_run_id, "agentId": agent_id, "steered": self.steer}
        self.turns.append(
            {
                "callId": call_id,
                "speaker": speaker,
                "speakerName": speaker_name,
                "requests": list(requests),
                "agentId": agent_id,
                "runId": run_id,
                "utterance": utterance,
                "transcript": transcript,
                "steerRunId": steer_run_id,
            }
        )
        if self.turn_gate:
            await self.turn_gate.wait()
        if self.turn_results:
            r = self.turn_results.pop(0)
            if isinstance(r, BackendError):
                raise r
        if steer_run_id and self.steer:
            return {"runId": steer_run_id, "agentId": agent_id, "steered": True}
        return {"runId": run_id, "agentId": agent_id, "steered": False}

    async def abort(self, run_id):
        self.aborts.append(run_id)

    async def end(self, call_id, usage_seconds):
        self.ends.append((call_id, usage_seconds))
        self.log.append("end")

    async def presence(self, user_ids, version=0):
        gate = self.presence_gate
        if gate:
            await gate.wait()
        self.presences.append(user_ids)
        self.versions.append(version)

    async def late_answer(self, agent_id, run_id, request, answer):
        self.late_answers.append((agent_id, run_id, request, answer))

    async def transcript(self, huddle_id, lines, final=False):
        if self.transcript_gate:
            await self.transcript_gate.wait()
        self.transcripts.append((huddle_id, lines))
        self.finals.append(final)

    async def leave_meeting(self):
        self.log.append("leave_meeting")


class FakeLive:
    def __init__(self, log, agent_id, persona, voice, speaker, seed):
        self.log = log
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self.persona, self.voice, self.speaker, self.seed = (
            persona,
            voice,
            speaker,
            seed,
        )
        self.calls: list[tuple] = []
        self.usage = 0.0
        self.final_usage = 42.4  # GPT-Live's last usage delta lands during close

    def _rec(self, *a):
        self.calls.append(a)
        self.log.append(a[0])

    def set_participant(self, identity):
        self._rec("set_participant", identity)

    def append_thinking(self, text, delegation_id=None):
        self._rec("thinking", text, delegation_id)

    def append_commentary(self, text, delegation_id=None):
        self._rec("commentary", text, delegation_id)

    def mute(self):
        self._rec("mute")

    def interrupt(self):
        self._rec("interrupt")

    def nudge(self):
        self._rec("nudge")

    def append_instructions(self, text):
        self._rec("instructions", text)

    def reply_to(self, text):
        self._rec("reply_to", text)

    def usage_seconds(self):
        return self.usage

    async def aclose(self):
        self._rec("close")
        self.usage = self.final_usage


def make(**kw):
    log: list[str] = []
    room, backend, clock = FakeRoom(), FakeBackend(log), Clock()
    room.log = log
    lives: list[FakeLive] = []
    fail: set[str] = (
        set()
    )  # agent ids whose start_live raises (after its own marin retry)
    gate: dict[str, asyncio.Event] = {}  # "live": start_live waits on it

    async def live_factory(*, agent_id, persona, voice, speaker, seed):
        if "live" in gate:
            await gate["live"].wait()
        if agent_id in fail:
            raise RuntimeError("voice refused")
        live = FakeLive(log, agent_id, persona, voice, speaker, seed)
        lives.append(live)
        return live

    async def sleep(seconds):
        clock.t += seconds
        log.append(f"sleep {seconds}")

    vocab: list[str] = []
    h = Huddle(
        room=room,
        backend=backend,
        live_factory=live_factory,
        clock=clock,
        sleep=sleep,
        on_vocabulary=vocab.append,
        **kw,
    )
    h.fail, h.gate, h.vocab = fail, gate, vocab
    return h, room, backend, lives, clock, log


async def settle(h):
    """Name summons run as tasks; wait for them."""
    while h._tasks:
        await asyncio.gather(*list(h._tasks))
        await asyncio.sleep(
            0
        )  # let finished tasks' discard callbacks run (awaiting done tasks never yields)


async def say(h, identity, text):
    name = {"u1": "Prasiddha", "u2": "Aden"}.get(identity, identity)
    await h.on_final_transcript(identity, name, text)
    await settle(h)


async def summoned(name="Forge", drive=False):
    """A named summon. drive=False quietly retires the driven first turn (Task 21 has its own tests), so
    these tests see only what they do next."""
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u2", "we shipped huddles")
    await say(h, "u1", f"hey {name} what did we ship")
    if not drive:
        h._runs.abort(backend.turns[0]["runId"])
        h._voice.driven = None
        backend.turns.clear()
    return h, room, backend, lives, clock, log


def packet(*events):
    return json.dumps({"events": list(events)}).encode()


def frame(run_id, agent_id, f):
    return {
        "schemaVersion": 1,
        "eventId": "e",
        "type": "voice.run.frame",
        "occurredAt": 0,
        "payload": {
            "roomId": "r1",
            "runId": run_id,
            "agentId": agent_id,
            "frame": {**f, "runId": run_id, "agentId": agent_id},
        },
    }


def answer(h, run_id, agent_id, text):
    h.on_data(
        None,
        "call.frame",
        packet(
            frame(run_id, agent_id, {"type": "chunk", "content": text, "seq": 1}),
            frame(run_id, agent_id, {"type": "run_ended", "seq": 2}),
        ),
    )


# -- wake --


async def test_summon_by_name_opens_that_agents_own_voice():
    h, room, backend, lives, clock, log = await summoned("Gauge")
    # Aden's plain line went to Jev (nobody); the line that starts with a name skipped it
    assert (
        backend.summons == ["u1"]
        and backend.sessions == ["gauge"]
        and backend.route_humans == [2]
    )
    live = lives[0]
    assert (live.agent_id, live.voice, live.persona, live.speaker) == (
        "gauge",
        "voice-gauge",
        "You are Gauge.",
        "u1",
    )
    assert live.seed[0] == (
        "user",
        "Aden: we shipped huddles",
    )  # the summoning line is not a user line
    assert live.calls == [("thinking", "Prasiddha is talking to you", None), ("nudge",)]


async def test_plain_line_does_not_summon():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "the deploy went fine")
    assert backend.summons == [] and lives == []


REPRO = ["Hey guys.", "Hey guys, hey Friday.", "Hey guys. Hey Friday, what's up?"]


async def test_a_line_with_no_name_at_the_start_asks_jev():
    # prod 2026-10-02: alone in a 3-agent channel, "Hey guys, hey Friday" summoned nobody
    for text in REPRO:
        h, room, backend, lives, clock, log = make()
        backend.team_data["agents"][1]["name"] = "Friday"
        backend.jev_to = "forge"
        await h.on_participants({"u1": "Prasiddha"})
        await say(h, "u1", text)
        assert backend.route_humans == [1] and backend.routes[0][1] == text
        assert lives[0].agent_id == "forge" and h._voice.path == "jev"
        await h.end_exchange()


async def test_jev_nobody_or_down_opens_no_voice(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "did you see the game last night")
    assert backend.route_humans == [2] and lives == [] and backend.summons == []

    async def down(*_a, **_kw):
        raise BackendError(503, "huddles_upstream", None)

    backend.route = down
    await say(h, "u2", "yeah it was something else")
    assert lives == [] and not [
        p for t, p, _ in room.published if t == "huddle.error"
    ]  # two people: silent
    outcomes = [
        json.loads(x).get("outcome")
        for x in capsys.readouterr().out.splitlines()
        if "live.latency.jev" in x
    ]
    assert outcomes == ["none", "error"]


# The same phrases as the backend's test_opening_name_parity: the worker's fast path must name exactly the
# agent chat's opening-name rule (messaging._addressed_by_name) names, or nobody, so Jev sees the rest.
PARITY = [
    ("Hey guys.", []),
    ("Hey guys, hey Friday.", []),
    ("Hey guys. Hey Friday, what's up?", []),
    ("Hey Friday, what's up?", ["forge"]),
    ("Friday, can you check revenue?", ["forge"]),
    ("Gauge, what's revenue?", ["gauge"]),
]


def test_opening_name_parity_with_chat():
    agents = [dict(a) for a in TEAM["agents"]]
    agents[1]["name"] = "Friday"
    for text, expected in PARITY:
        assert address(text, agents, TEAM["people"]).agent_ids == expected, text


async def test_a_name_at_the_start_skips_jev():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Gauge, what's revenue?")
    assert backend.routes == [] and lives[0].agent_id == "gauge"
    await h.end_exchange()


async def test_the_floor_holder_keeps_follow_ups_without_jev():
    h, room, backend, lives, clock, log = await summoned("Forge")
    routed = len(backend.routes)
    await say(h, "u1", "and what about last week")
    assert len(backend.routes) == routed and h._voice.agent_id == "forge"


async def test_lightbulb_without_a_name_routes_with_a_name_does_not():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Lightbulb, what's the deploy status")
    assert backend.routes == [
        (
            "u1",
            "what's the deploy status",
            "Prasiddha: Lightbulb, what's the deploy status",
        )
    ]
    assert lives[0].agent_id == "forge"
    await h.end_exchange()
    await say(h, "u1", "hey Lightbulb, Gauge, revenue please")
    assert (
        len(backend.routes) == 1 and lives[1].agent_id == "gauge"
    )  # an opening name after the wake word: 0 ms
    await h.end_exchange()
    await say(
        h, "u1", "Lightbulb, ask Gauge about revenue"
    )  # a mid-line name: /route, wake word stripped
    assert (
        backend.routes[-1][1] == "ask Gauge about revenue"
        and backend.route_humans[-1] is None
    )


async def test_a_bare_default_name_does_not_wake():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Finance is fine this month")
    assert backend.summons == [] and lives == []


async def test_an_ambiguous_name_tells_only_the_speaker():
    h, room, backend, lives, clock, log = make()
    backend.team_data["people"].append({"userId": "u3", "name": "Gauge Smith"})
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Gauge, what's up")
    assert backend.summons == [] and lives == []
    assert room.notices() == [({"kind": "ambiguous_name", "agentId": "gauge"}, ["u1"])]


# -- switch, window, budget --


async def test_switch_takes_no_second_summon_and_sums_usage():
    h, room, backend, lives, clock, log = await summoned("Forge")
    lives[0].final_usage = 42
    await say(h, "u1", "Gauge, your turn")
    assert [l.agent_id for l in lives] == ["forge", "gauge"]
    assert lives[0].calls[-2:] == [("mute",), ("close",)]
    assert (
        backend.summons == ["u1"] and backend.ends == []
    )  # a voice is open: the window stays
    lives[1].final_usage = 17
    await h.end_exchange()
    assert backend.ends == [("call-1", 59)]


async def test_a_switch_opens_the_new_voice_before_the_old_one_finishes_closing():
    h, room, backend, lives, clock, log = await summoned("Forge")
    gate = asyncio.Event()
    old_close = lives[0].aclose

    async def slow_close():
        await gate.wait()
        await old_close()

    lives[0].aclose = slow_close
    lives[0].final_usage = 42
    switch = asyncio.create_task(say(h, "u1", "Gauge, your turn"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert [l.agent_id for l in lives] == ["forge", "gauge"] and (
        "close",
    ) not in lives[0].calls
    assert lives[0].calls[-1] == ("mute",)  # the old voice goes quiet at once
    assert room.written()["lightbulb.speaking_agent"] == "gauge"
    clock.t += 5  # the overlap
    gate.set()
    await switch
    assert ("close",) in lives[0].calls
    assert not {("mute",), ("close",)} & set(
        lives[1].calls
    )  # the old close leaves the new voice alone
    assert h._voice.agent_id == "gauge" and h._voice.live is lives[1]
    assert backend.summons == ["u1"]  # a switch takes no new /summon
    lives[1].final_usage = 17
    await h.end_exchange()
    assert backend.ends == [
        ("call-1", 59)
    ]  # both sessions' seconds, the overlap counted twice


async def two_names(text="Forge and Gauge, what shipped?"):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", text)
    return h, room, backend, lives, clock, log


async def test_several_names_open_the_first_and_queue_the_rest():
    h, room, backend, lives, clock, log = await two_names()
    assert [l.agent_id for l in lives] == ["forge"]
    assert room.written()["lightbulb.queue"] == "forge,gauge"
    assert h._queue == [("gauge", "u1", "Forge and Gauge, what shipped?")]


async def test_the_queue_advances_after_the_run_ends_and_speech_stops(monkeypatch):
    h, room, backend, lives, clock, log = await two_names()
    run_id = backend.turns[0]["runId"]
    h.on_agent_state("forge", "speaking")
    clock.t += 5
    await h.tick()
    assert len(lives) == 1  # the driven run is still running
    answer(h, run_id, "forge", "We shipped huddles.")
    h.on_agent_said("forge", "We shipped huddles.")
    clock.t += 5
    await h.tick()
    assert len(lives) == 1  # still speaking
    h.on_agent_state("forge", "listening")
    await h.tick()
    assert len(lives) == 1  # QUEUE_SETTLE has not passed
    clock.t += h_mod.QUEUE_SETTLE
    await h.tick()
    await settle(h)
    assert [l.agent_id for l in lives] == [
        "forge",
        "gauge",
    ] and h._voice.path == "switch"
    assert ("assistant", "We shipped huddles.") not in lives[1].seed
    assert ("developer", "[Forge (agent) said]: We shipped huddles.") in lives[
        1
    ].seed  # the previous answer
    assert backend.turns[-1]["agentId"] == "gauge"  # its own driven turn
    assert h._queue == [] and room.written()["lightbulb.queue"] == ""
    assert backend.summons == ["u1"]


async def test_a_new_name_replaces_the_queue():
    h, room, backend, lives, clock, log = await two_names()
    await say(h, "u2", "Ada, quick one")
    assert lives[-1].agent_id == "ceo" and h._queue == []
    assert room.written()["lightbulb.queue"] == ""


async def test_a_queued_agent_that_leaves_is_dropped():
    h, room, backend, lives, clock, log = await two_names(
        "Forge, Gauge and Ada, what shipped?"
    )
    assert room.written()["lightbulb.queue"] == "forge,gauge,ceo"
    backend.team_data["agents"] = [
        a for a in backend.team_data["agents"] if a["id"] != "gauge"
    ]
    await h._refresh_team()
    assert [q[0] for q in h._queue] == ["ceo"] and room.written()[
        "lightbulb.queue"
    ] == "forge,ceo"


async def test_held_news_seeds_its_own_agent_and_is_consumed():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    h._news["gauge"] = [("r-old", "the revenue", "Revenue is up.", clock.t, None)]
    await say(h, "u1", "Gauge, anything new?")
    assert (
        "developer",
        "You finished the revenue. Your answer: Revenue is up. Deliver it now, briefly and completely, before anything else.",
    ) in lives[0].seed
    assert "gauge" not in h._news


async def test_budget_is_summed_usage_including_the_open_voice():
    h, room, backend, lives, clock, log = make()
    backend_summon = dict(SUMMON, budgetSeconds=120)
    backend.summon = lambda speaker, speaker_name=None: _resolved(backend_summon)
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Forge, hi")
    lives[0].final_usage = 100
    await say(h, "u1", "Gauge, hi")
    lives[1].usage = 19
    h.on_speaker_activity("gauge")
    await h.tick()
    assert backend.ends == []  # 100 + 19 < 120
    lives[1].usage = lives[1].final_usage = 21
    await h.tick()
    assert backend.ends == [("call-1", 121)]


async def _resolved(value):
    return value


async def test_deadline_still_ends_the_window():
    h, room, backend, lives, clock, log = await summoned()
    clock.t = SUMMON["deadline"]
    h.on_speaker_activity("forge")
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_idle_ends_exchange_and_window():
    h, room, backend, lives, clock, log = await summoned()
    clock.t += IDLE_SECONDS - 1
    await h.tick()
    assert backend.ends == []
    clock.t += 1
    await h.tick()
    assert log[-3:] == ["mute", "close", "end"]
    assert backend.ends == [("call-1", 42)]
    await say(h, "u1", "hey Forge one more thing")  # the next summon opens a new window
    assert backend.summons == ["u1", "u1"]


async def test_usage_falls_back_to_wall_clock_capped_at_deadline():
    h, room, backend, lives, clock, log = await summoned()
    lives[0].final_usage = 0
    clock.t = SUMMON["deadline"] + 500
    await h.tick()
    assert backend.ends == [("call-1", SUMMON["deadline"] - 1_000)]


async def test_never_ends_while_the_agent_is_speaking():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_state("forge", "speaking")
    clock.t += IDLE_SECONDS + 30
    await h.tick()
    assert backend.ends == []
    h.on_agent_state("forge", "listening")
    clock.t += IDLE_SECONDS
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_running_run_delays_idle_end():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "what did we ship")
    run_id = backend.turns[0]["runId"]
    clock.t += IDLE_SECONDS + 5
    await h.tick()
    assert backend.ends == []
    answer(h, run_id, "forge", "We shipped huddles.")
    clock.t += IDLE_SECONDS
    await h.tick()
    assert backend.ends == [("call-1", 42)]


# -- delegation --


async def test_delegation_turn_carries_agent_run_and_since_agent():
    h, room, backend, lives, clock, log = await summoned("Gauge")
    await h.on_delegation("gauge", "d1", "what did we ship")
    (t,) = backend.turns
    assert (t["callId"], t["speaker"], t["agentId"], t["utterance"]) == (
        "call-1",
        "u1",
        "gauge",
        "what did we ship",
    )
    assert (
        t["transcript"]
        == "Aden: we shipped huddles\nPrasiddha: hey Gauge what did we ship"
    )
    assert len(t["runId"]) == 36


async def test_a_retried_turn_resends_the_same_run_id():
    h, room, backend, lives, clock, log = await summoned()
    backend.turn_results = [
        BackendError(503, "runtime_unavailable", None),
        BackendError(0, "huddles_upstream", None),
    ]
    await h.on_delegation("forge", "d1", "check the board")
    assert len({t["runId"] for t in backend.turns}) == 1 and len(backend.turns) == 3
    assert [e for e in log if e.startswith("sleep")] == ["sleep 2", "sleep 4"]
    assert [c for c in lives[0].calls if c[1:2] == (h_mod.STILL_WAKING,)] == [
        ("thinking", h_mod.STILL_WAKING, "d1")
    ]
    assert not [c for c in lives[0].calls if c[0] == "commentary"]


async def test_cold_workspace_apologises_only_after_the_budget():
    h, room, backend, lives, clock, log = await summoned()
    backend.turn_results = [BackendError(503, "runtime_unavailable", None)] * 50
    start = clock.t
    await h.on_delegation("forge", "d1", "check the board")
    assert clock.t - start <= h_mod.TURN_RETRY_BUDGET
    assert lives[0].calls[-1] == ("commentary", h_mod.CANT_REACH, "d1")
    assert not h._runs.running("forge")  # the failed run never holds the exchange open


async def test_delegation_without_pending_transcript_uses_the_speakers_own_line():
    h, room, backend, lives, clock, log = await summoned()
    await say(h, "u2", "good question")
    await h.on_delegation("forge", "d1", "")
    assert backend.turns[0]["utterance"] == "hey Forge what did we ship"


async def test_one_agents_line_is_heard_by_the_next():
    h, room, backend, lives, clock, log = await summoned("Forge")
    h.on_agent_said("forge", "Deploy is green.")
    assert "Forge → Prasiddha: Deploy is green." in h._transcript.since_agent(
        "gauge"
    )  # who it answered
    await say(h, "u1", "Gauge, check revenue")
    assert lives[1].seed[-2] == ("developer", "[Forge (agent) said]: Deploy is green.")


# -- the driven first turn (D §5) --


async def test_the_request_is_posted_while_the_voice_is_still_opening():
    h, room, backend, lives, clock, log = make()
    h.gate["live"] = asyncio.Event()
    await h.on_participants({"u1": "Prasiddha"})
    await h.on_final_transcript("u1", "Prasiddha", "Forge, what's the deploy status")
    for _ in range(10):
        await asyncio.sleep(0)
    assert lives == []  # start_live still blocked
    (t,) = backend.turns
    assert (t["agentId"], t["utterance"], t["speaker"]) == (
        "forge",
        "Forge, what's the deploy status",
        "u1",
    )
    assert h._runs.running("forge")[0].delegation_id is None
    h.gate["live"].set()
    await settle(h)
    assert len(backend.turns) == 1


async def test_the_seed_hands_over_the_request_and_nothing_is_typed():
    h, room, backend, lives, clock, log = await summoned("Gauge", drive=True)
    assert lives[0].seed[-1] == (
        "developer",
        "Prasiddha asked: hey Gauge what did we ship — already handed to your backend self; don't delegate it again",
    )
    assert "reply_to" not in [c[0] for c in lives[0].calls]


async def test_an_early_delegation_binds_to_the_driven_run(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        h_mod,
        "emit_metric",
        lambda name, value, unit="Seconds", dims=None: not name.startswith(
            ("live.stt.", "live.wake.", "live.latency.")
        )
        and emitted.append(name),
    )
    h, room, backend, lives, clock, log = await summoned(drive=True)
    run_id = backend.turns[0]["runId"]
    clock.t += h_mod.BIND_SECONDS
    await h.on_delegation("forge", "d1", "what did we ship")
    assert len(backend.turns) == 1 and h._runs.running("forge")[0].delegation_id == "d1"
    assert emitted == ["live.driven.bound"]
    answer(h, run_id, "forge", "We shipped huddles.")
    assert lives[0].calls[-1] == ("commentary", "We shipped huddles.", "d1")


async def test_a_delegation_before_the_post_answers_binds_when_it_does():
    h, room, backend, lives, clock, log = make()
    backend.turn_gate = asyncio.Event()
    await h.on_participants({"u1": "Prasiddha"})
    await h.on_final_transcript("u1", "Prasiddha", "Forge, check the board")
    for _ in range(10):
        await asyncio.sleep(0)
    assert h._voice.live is not None
    await h.on_delegation("forge", "d1", "check the board")
    assert len(backend.turns) == 1 and h._runs.running("forge")[0].delegation_id is None
    backend.turn_gate.set()
    await settle(h)
    assert len(backend.turns) == 1 and h._runs.running("forge")[0].delegation_id == "d1"


async def test_a_delegation_after_the_bind_window_is_a_new_turn():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    clock.t += h_mod.BIND_SECONDS + 1
    await h.on_delegation("forge", "d1", "and the billing fix")
    assert [t["utterance"] for t in backend.turns] == [
        "hey Forge what did we ship",
        "and the billing fix",
    ]


async def test_driven_frames_use_null_then_the_bound_delegation():
    h, room, backend, lives, clock, log = await summoned(drive=True)
    run_id = backend.turns[0]["runId"]
    h.on_data(
        None,
        "call.frame",
        packet(
            frame(run_id, "forge", {"type": "tool_start", "tool": "git log", "seq": 1})
        ),
    )
    assert lives[0].calls[-1] == ("thinking", "Working: git log. Not done yet.", None)
    await h.on_delegation("forge", "d1", "what did we ship")
    h.on_data(
        None,
        "call.frame",
        packet(
            frame(
                run_id, "forge", {"type": "tool_start", "tool": "gh pr list", "seq": 2}
            )
        ),
    )
    assert lives[0].calls[-1] == (
        "thinking",
        "Working: gh pr list. Not done yet.",
        "d1",
    )


async def test_a_driven_turn_that_never_starts_apologises_without_a_delegation():
    h, room, backend, lives, clock, log = make()
    backend.turn_results = [BackendError(409, "agent_not_in_room", None)]
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Forge, check the board")
    assert lives[0].calls[-1] == ("commentary", h_mod.CANT_REACH, None)
    assert not h._runs.running("forge") and h._voice.driven is None


async def test_a_tap_has_no_driven_turn():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1", json.dumps({"agentId": "forge"}))
    await settle(h)
    assert backend.turns == [] and h._voice.driven is None and lives[0].seed == []
    assert h._voice.path == "tap"


# -- frames --


async def test_floor_holder_same_session_speaks_with_its_delegation():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    answer(h, backend.turns[0]["runId"], "forge", "We shipped huddles.")
    assert lives[0].calls[-1] == ("commentary", "We shipped huddles.", "d1")


async def test_floor_holder_earlier_session_gets_it_without_a_delegation():
    h, room, backend, lives, clock, log = await summoned("Forge")
    await h.on_delegation("forge", "d1", "the deploy")
    run_id = backend.turns[0]["runId"]
    await say(h, "u1", "Gauge, hi")
    await say(h, "u1", "Forge, back to you")
    answer(h, run_id, "forge", "It's green.")
    assert lives[2].calls[-1] == (
        "commentary",
        "Your earlier task on the deploy finished: It's green.",
        None,
    )


async def test_off_floor_result_becomes_news():
    h, room, backend, lives, clock, log = await summoned("Forge")
    await h.on_delegation("forge", "d1", "the deploy")
    run_id = backend.turns[0]["runId"]
    await say(h, "u1", "Gauge, hi")
    n = len(lives[1].calls)
    answer(h, run_id, "forge", "It's green.")
    assert len(lives[1].calls) == n  # Gauge is not handed Forge's result
    assert h._news["forge"][0][1:3] == ("the deploy", "It's green.")
    assert room.written()["lightbulb.agent.forge.status"] == "has_news"


async def test_an_unknown_run_becomes_news():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    answer(h, "run-from-before-a-restart", "gauge", "Revenue is up.")
    assert h._news["gauge"][0][2] == "Revenue is up."
    assert room.written()["lightbulb.agent.gauge.status"] == "has_news"


async def test_news_dropped_by_cap_or_ttl_emits_expired(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        h_mod,
        "emit_metric",
        lambda name, value, unit="Seconds", dims=None: name != "live.stt.prompt_updated"
        and emitted.append(name),
    )
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    for i in range(h_mod.NEWS_PER_AGENT + 1):
        answer(h, f"r{i}", "gauge", f"Answer {i}.")
    assert (
        emitted == ["live.announce.expired"]
        and len(h._news["gauge"]) == h_mod.NEWS_PER_AGENT
    )
    clock.t += h_mod.NEWS_TTL
    await h.tick()
    assert (
        emitted.count("live.announce.expired") == 1 + h_mod.NEWS_PER_AGENT
        and "gauge" not in h._news
    )


async def test_frames_from_a_participant_are_dropped():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    before = list(lives[0].calls)
    h.on_data(
        object(),
        "call.frame",
        packet(
            frame(
                backend.turns[0]["runId"],
                "forge",
                {"type": "chunk", "content": "Forged.", "seq": 1},
            )
        ),
    )
    h.on_data(None, "call.frame", b"not json")
    assert lives[0].calls == before and h.dropped_frames == 1


async def test_close_never_stops_runs():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    await say(h, "u1", "Gauge, hi")
    await h.end_exchange()
    await h.on_participants({})
    assert backend.aborts == []
    assert h._runs.running("forge")  # still running, its answer will come as news


# -- follow-ups steer, stop aborts, queued behind chat (Task 23) --


async def test_a_follow_up_steers_the_running_run_and_rebinds_its_delegation():
    h, room, backend, lives, clock, log = await summoned()
    backend.steer = True
    await h.on_delegation("forge", "d1", "check the deploy")
    first = backend.turns[0]["runId"]
    await h.on_delegation("forge", "d2", "and the staging one too")
    assert backend.turns[1]["steerRunId"] == first
    assert lives[0].calls[-1] == ("thinking", h_mod.FOLDED, "d1")
    assert [r.run_id for r in h._runs.running("forge")] == [
        first
    ]  # the minted run never runs
    answer(h, first, "forge", "Both are green.")
    assert lives[0].calls[-1] == ("commentary", "Both are green.", "d2")


async def test_a_refused_steer_starts_a_new_run_and_the_old_one_keeps_running():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "check the deploy")
    first = backend.turns[0]["runId"]
    await h.on_delegation("forge", "d2", "and staging")
    second = backend.turns[1]["runId"]
    assert backend.turns[1]["steerRunId"] == first and second != first
    assert {r.run_id for r in h._runs.running("forge")} == {
        first,
        second,
    } and backend.aborts == []
    answer(h, second, "forge", "Staging is green.")
    assert lives[0].calls[-1] == ("commentary", "Staging is green.", "d2")


async def test_no_running_run_means_no_steer():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    assert backend.turns[0]["steerRunId"] is None


async def test_stop_from_the_speaker_interrupts_and_aborts_that_agents_runs():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "check the deploy")
    run_id = backend.turns[0]["runId"]
    await say(h, "u1", "stop")
    assert ("interrupt",) in lives[0].calls and backend.aborts == [run_id]
    assert len(backend.turns) == 1 and h._runs.running("forge") == []
    n = len(lives[0].calls)
    answer(h, run_id, "forge", "Too late.")
    assert len(lives[0].calls) == n  # an aborted run never speaks


async def test_never_mind_delegated_by_the_voice_is_swallowed():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "check the deploy")
    run_id = backend.turns[0]["runId"]
    await h.on_delegation("forge", "d2", "never mind")
    await settle(h)
    assert (
        len(backend.turns) == 1
        and backend.aborts == [run_id]
        and ("interrupt",) in lives[0].calls
    )


async def test_forge_stop_aborts_only_forges_runs():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "check the deploy")
    forge_run = backend.turns[0]["runId"]
    h.on_data(
        None,
        "call.frame",
        packet(
            frame(
                "gauge-run", "gauge", {"type": "tool_start", "tool": "Stripe", "seq": 1}
            )
        ),
    )
    await say(h, "u2", "Forge, stop")  # not the pinned speaker, but it names Forge
    assert backend.aborts == [forge_run] and [
        r.run_id for r in h._runs.running("gauge")
    ] == ["gauge-run"]
    assert backend.summons == ["u1"] and len(lives) == 1  # a stop summons no one


async def test_anyone_naming_a_default_named_agent_stops_its_runs():
    h, room, backend, lives, clock, log = await summoned()
    h.on_data(
        None,
        "call.frame",
        packet(
            frame("fin-run", "fin", {"type": "tool_start", "tool": "Stripe", "seq": 1})
        ),
    )
    await say(
        h, "u2", "Finance, stop"
    )  # Aden is not pinned; Finance is a default name and off the floor
    assert backend.aborts == ["fin-run"] and h._runs.running("fin") == []
    assert ("interrupt",) not in lives[0].calls and h._voice.agent_id == "forge"


async def test_a_plain_line_from_someone_else_is_not_a_stop():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    await say(h, "u2", "stop")  # Aden is not the pinned speaker and names nobody
    assert backend.aborts == [] and ("interrupt",) not in lives[0].calls


async def test_a_stop_while_turns_is_in_flight_aborts_again_once_it_lands():
    h, room, backend, lives, clock, log = await summoned()
    backend.turn_gate = asyncio.Event()
    deleg = asyncio.create_task(h.on_delegation("forge", "d1", "check the deploy"))
    for _ in range(5):
        await asyncio.sleep(0)
    run_id = backend.turns[0]["runId"]
    await say(h, "u1", "never mind")
    backend.turn_gate.set()
    await deleg
    await settle(h)
    assert backend.aborts == [run_id, run_id]
    assert not [c for c in lives[0].calls if c[0] == "commentary"]


async def test_run_queued_is_said_once_and_the_floor_holder_stays_listening():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "check the deploy")
    run_id = backend.turns[0]["runId"]
    h.on_data(
        None,
        "call.frame",
        packet(frame(run_id, "forge", {"type": "run_queued", "seq": 1})),
    )
    h.on_data(
        None,
        "call.frame",
        packet(frame(run_id, "forge", {"type": "run_queued", "seq": 2})),
    )
    assert [c for c in lives[0].calls if c[1:2] == (h_mod.QUEUED_BEHIND,)] == [
        ("thinking", h_mod.QUEUED_BEHIND, "d1")
    ]
    assert (
        room.written()["lightbulb.agent.forge.status"] == "listening"
    )  # C4: the floor-holder is never "working"


async def test_leaving_or_the_window_ending_never_aborts():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("forge", "d1", "q")
    clock.t += h_mod.IDLE_SECONDS + 1
    h._runs._runs[backend.turns[0]["runId"]].seen = clock.t  # still running
    await h.end_exchange()
    await h.on_participants({})
    assert backend.aborts == [] and backend.ends and h._runs.running("forge")


# -- attributes --


async def test_state_follows_the_floor_holder():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    backend.session_gate = asyncio.Event()
    start = asyncio.create_task(h.talk_start("u1", json.dumps({"agentId": "forge"})))
    for _ in range(5):
        await asyncio.sleep(0)
    s = h._state()
    assert (
        s["lightbulb.speaking_agent"] == "forge"
        and s["lightbulb.agent.forge.status"] == "joining"
    )
    backend.session_gate.set()
    await start
    h.on_agent_state("forge", "speaking")
    assert room.written()["lightbulb.agent.forge.status"] == "speaking"
    h.on_agent_state("forge", "listening")
    h.on_speaker_activity("forge")
    s = room.written()
    assert (
        s["lightbulb.agent.forge.status"] == "listening"
        and s["lightbulb.speaking_agent"] == "forge"
    )
    assert (
        s["lightbulb.listening_to"] == "Prasiddha"
        and s["lightbulb.agent.gauge.status"] == "idle"
    )
    await h.on_delegation("forge", "d1", "q")
    h.on_data(
        None,
        "call.frame",
        packet(
            frame(
                backend.turns[0]["runId"],
                "forge",
                {"type": "tool_start", "tool": "checking Stripe", "seq": 1},
            )
        ),
    )
    assert room.written()["lightbulb.agent.forge.working"] == "checking Stripe"
    await h.end_exchange()
    s = room.written()
    assert (
        s["lightbulb.speaking_agent"] == ""
        and s["lightbulb.agent.forge.status"] == "working"
    )
    assert not {"lightbulb.status", "lightbulb.speaker", "lightbulb.working"} & set(s)
    n = len(room.attrs)
    await h.tick()
    assert len(room.attrs) == n  # unchanged state is not re-published


async def test_a_departed_agents_keys_are_cleared():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    assert room.written()["lightbulb.agent.gauge.status"] == "idle"
    backend.team_data["agents"] = [
        a for a in backend.team_data["agents"] if a["id"] != "gauge"
    ]
    clock.t += h_mod.TEAM_REFRESH_S
    await h.tick()
    await settle(h)
    assert room.attrs[-1] == {"lightbulb.agent.gauge.status": ""}


async def test_naming_is_set_when_the_agent_asks_and_cleared_on_rename():
    h, room, backend, lives, clock, log = make()
    backend.session_extra["forge"] = {"askName": True}
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1", json.dumps({"agentId": "forge"}))
    assert room.written()["lightbulb.agent.forge.naming"] == "1"
    renamed = {
        "type": "voice.notice",
        "payload": {
            "kind": "renamed",
            "agentId": "forge",
            "from": "Forge",
            "to": "Bolt",
            "by": "u1",
            "collides": False,
        },
    }
    h.on_data(None, "call.frame", packet(renamed))
    await settle(h)
    assert room.written()["lightbulb.agent.forge.naming"] == ""


# -- STT vocabulary, rename and voice-note notices (Task 27) --


async def test_stt_prompt_names_the_team_and_a_join_updates_it_once(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        h_mod,
        "emit_metric",
        lambda name, value, unit="Seconds", dims=None: emitted.append(name),
    )
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    assert h.vocab == [
        "A team voice call. Names: Ada, Forge, Gauge, Finance, Lightbulb, Prasiddha, Aden."
    ]
    await h.on_participants({"u1": "Prasiddha", "u3": "Zoe"})
    assert len(h.vocab) == 2 and h.vocab[-1].endswith("Prasiddha, Aden, Zoe.")
    await h.on_participants({"u1": "Prasiddha", "u3": "Zoe"})
    assert (
        len(h.vocab) == 2 and emitted.count("live.stt.prompt_updated") == 2
    )  # unchanged: not sent again


def renamed(agent_id="forge", to="Nova", collides=False):
    return {
        "type": "voice.notice",
        "payload": {
            "kind": "renamed",
            "agentId": agent_id,
            "from": "Forge",
            "to": to,
            "by": "u1",
            "collides": collides,
        },
    }


async def test_a_rename_refreshes_the_matcher_and_tells_the_open_voice():
    h, room, backend, lives, clock, log = await summoned()
    next(a for a in backend.team_data["agents"] if a["id"] == "forge")["name"] = "Nova"
    h.on_data(None, "call.frame", packet(renamed()))
    await settle(h)
    assert ("instructions", "You are now called Nova.") in lives[0].calls
    assert "Nova" in h.vocab[-1] and "Forge" not in h.vocab[-1]
    await h.end_exchange()
    await say(h, "u2", "Forge, check")
    assert h._voice is None
    await say(h, "u2", "Nova, check")
    assert h._voice.agent_id == "forge"


async def test_the_rename_notice_reaches_every_person_with_collides():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    h.on_data(None, "call.frame", packet(renamed(to="Aden", collides=True)))
    await settle(h)
    assert room.notices() == [
        (
            {
                "kind": "renamed",
                "agentId": "forge",
                "from": "Forge",
                "to": "Aden",
                "by": "u1",
                "collides": True,
            },
            ["u1", "u2"],
        )
    ]
    assert lives == []  # no voice open: nothing to tell


async def test_a_voice_note_is_appended_capped_and_forwarded():
    h, room, backend, lives, clock, log = await summoned()
    note = {
        "kind": "voice_note_saved",
        "agentId": "forge",
        "name": "Forge",
        "text": "be blunt " * 300,
        "by": "u2",
    }
    h.on_data(None, "call.frame", packet({"type": "voice.notice", "payload": note}))
    instr = [c[1] for c in lives[0].calls if c[0] == "instructions"]
    assert (
        len(instr) == 1
        and instr[0].startswith("Speaking preference from Aden: be blunt")
        and len(instr[0]) == 1_800
    )
    assert room.notices()[-1] == (note, ["u1", "u2"])


async def test_a_voice_note_for_another_agent_is_only_forwarded():
    h, room, backend, lives, clock, log = await summoned()
    note = {
        "kind": "voice_note_saved",
        "agentId": "gauge",
        "name": "Gauge",
        "text": "slower",
        "by": "u1",
    }
    h.on_data(None, "call.frame", packet({"type": "voice.notice", "payload": note}))
    assert not [c for c in lives[0].calls if c[0] == "instructions"] and room.notices()[
        -1
    ] == (note, ["u1", "u2"])


async def test_a_notice_from_a_participant_is_dropped():
    h, room, backend, lives, clock, log = await summoned()
    h.on_data(object(), "call.frame", packet(renamed()))
    await settle(h)
    assert room.notices() == [] and h.dropped_frames == 1


async def test_one_team_refresh_in_flight_reruns_for_a_request_during_it():
    h, room, backend, lives, clock, log = make()
    gate = asyncio.Event()
    team = backend.team

    async def slow_team():
        await gate.wait()
        return await team()

    backend.team = slow_team
    first = asyncio.create_task(h._refresh_team())
    await asyncio.sleep(0)
    await h._refresh_team()  # returns at once; the one in flight runs again
    await h._refresh_team()
    gate.set()
    await first
    assert backend.team_calls == 2


# -- talk_start --


async def test_talk_start_with_an_agent_id():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1", json.dumps({"agentId": "gauge"}))
    assert lives[0].agent_id == "gauge" and "reply_to" not in [
        c[0] for c in lives[0].calls
    ]


async def test_empty_talk_start_is_the_last_agent_then_the_ceo():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1", "")
    assert lives[0].agent_id == "ceo"
    await say(h, "u1", "Gauge, hi")
    await h.end_exchange()
    await h.talk_start("u1", "")
    assert lives[-1].agent_id == "gauge"


async def test_talk_start_unknown_agent_or_floor_holder_or_stranger_is_ignored():
    h, room, backend, lives, clock, log = await summoned("Forge")
    await h.talk_start("u1", json.dumps({"agentId": "nobody"}))
    await h.talk_start("u1", json.dumps({"agentId": "forge"}))
    await h.talk_start("u2", json.dumps({"agentId": "forge"}))
    await h.talk_start("agent-other", json.dumps({"agentId": "gauge"}))
    await h.talk_start("u1", "not json")
    assert len(lives) == 1 and lives[0].calls[-1] == ("nudge",)


async def test_concurrent_talk_starts_summon_once():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await asyncio.gather(
        h.talk_start("u1", '{"agentId": "forge"}'),
        h.talk_start("u2", '{"agentId": "forge"}'),
    )
    assert backend.summons == ["u1"] and len(lives) == 1


async def test_release_before_the_summon_finishes_is_honoured():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    start = asyncio.create_task(h.talk_start("u1", '{"agentId": "forge"}'))
    await asyncio.sleep(0)
    await h.talk_end("u1")
    await start
    assert lives[0].calls[-1] == ("nudge",)


async def test_talk_end_from_non_speaker_does_nothing():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await h.talk_start("u1", '{"agentId": "forge"}')
    await h.talk_end("u2")
    assert "nudge" not in [c[0] for c in lives[0].calls]
    await h.talk_end("u1")
    assert lives[0].calls[-1] == ("nudge",)


# -- failures and leaving --


async def test_voice_failed_after_both_starts_fail_ends_the_window():
    h, room, backend, lives, clock, log = make()
    h.fail.add("forge")
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Forge, hi")
    assert room.notices() == [
        ({"kind": "voice_failed", "agentId": "forge"}, ["u1", "u2"])
    ]
    assert backend.ends == [("call-1", 0)] and h._voice is None


async def test_voice_busy_publishes_error_and_opens_nothing():
    h, room, backend, lives, clock, log = make()
    backend.summon_error = BackendError(409, "voice_busy", None)
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "Forge, are you there")
    assert (
        "huddle.error",
        {"code": "voice_busy", "reason": None},
        ["u1"],
    ) in room.published
    assert lives == [] and backend.ends == [] and h._voice is None
    assert room.written()["lightbulb.agent.forge.status"] == "idle"


async def test_the_speaker_leaving_closes_the_voice():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_participants({"u2": "Aden"})
    assert lives[0].calls[-2:] == [("mute",), ("close",)]
    assert backend.ends == [("call-1", 42)] and not room.shut


async def test_another_person_naming_the_floor_holder_takes_the_floor():
    h, room, backend, lives, clock, log = await summoned()
    await say(h, "u2", "Forge, and what about billing?")
    assert len(lives) == 1 and backend.summons == ["u1"]
    assert lives[0].calls[-3:] == [
        ("set_participant", "u2"),
        ("thinking", "Aden is talking to you", None),
        ("reply_to", "Forge, and what about billing?"),
    ]
    await h.on_delegation("forge", "d1", "billing")
    assert backend.turns[-1]["speaker"] == "u2"


async def test_other_peoples_lines_batched_as_thinking():
    h, room, backend, lives, clock, log = await summoned()
    n = len(lives[0].calls)
    await say(h, "u2", "and the bar")
    assert len(lives[0].calls) == n
    clock.t += 3
    await h.tick()
    assert lives[0].calls[-1] == ("thinking", "Aden: and the bar", None)


async def test_dead_live_session_closes_the_voice_and_never_raises():
    h, room, backend, lives, clock, log = await summoned()

    def dead(*a, **k):
        raise RuntimeError("AgentSession isn't running")

    lives[0].reply_to = dead
    await say(h, "u2", "Forge, and the billing fix?")  # must not raise into STT
    assert backend.ends == [("call-1", 42)] and h._voice is None
    await say(h, "u2", "Forge, are you there?")
    assert backend.summons == ["u1", "u2"]


async def test_live_session_closing_on_its_own_closes_the_voice():
    h, room, backend, lives, clock, log = await summoned()
    h.on_live_closed(lives[0])
    await settle(h)
    assert backend.ends == [("call-1", 42)]


async def leave_all(h, clock):
    """The last human leaves; the worker re-checks after EMPTY_ROOM_GRACE and then leaves too."""
    from huddle_agent.huddle import EMPTY_ROOM_GRACE

    await h.on_participants({})
    clock.t += EMPTY_ROOM_GRACE
    await h.tick()


async def test_last_human_leaving_during_a_summon_still_ends_the_call():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    summon = asyncio.create_task(h.talk_start("u1", '{"agentId": "forge"}'))
    await asyncio.sleep(0)
    await leave_all(h, clock)
    await summon
    assert backend.ends == [("call-1", 42)] and room.shut


async def test_shutdown_happens_even_if_end_fails():
    h, room, backend, lives, clock, log = await summoned()

    async def boom(*a):
        raise RuntimeError("non-JSON 2xx")

    backend.presence = boom
    backend.end = boom
    await leave_all(h, clock)
    assert room.shut


async def test_last_human_leaving_ends_then_shuts_down():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_participants({})
    assert backend.presences[-1] == [] and backend.ends == [("call-1", 42)]
    assert (
        log[-1] == "end" and not room.shut
    )  # the call ends at once; leaving waits for the re-check
    await leave_all(h, clock)
    assert log[-1] == "shutdown"


async def test_backend_client_shapes_requests_and_errors(monkeypatch):
    import httpx

    from huddle_agent.backend import Backend

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(
            (
                req.url.path,
                req.headers.get("X-Lightbulb-Huddle-Secret"),
                json.loads(req.content),
            )
        )
        if req.url.path.endswith("/summon"):
            return httpx.Response(
                402, json={"detail": {"code": "voice_limit", "reason": "credits_low"}}
            )
        if req.url.path.endswith("/end"):
            return httpx.Response(404, json={"detail": {"code": "call_not_found"}})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("LIGHTBULB_API_URL", "https://api-dev.example/api/v1/")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s3cret")
    b = Backend("r1", "org_1", "tok")
    b._http._transport = httpx.MockTransport(handler)
    try:
        await b.summon("u1")
        raise AssertionError("summon should raise")
    except BackendError as e:
        assert (e.status, e.code, e.reason) == (402, "voice_limit", "credits_low")
    await b.end("c1", 7)  # 404 = already closed
    await b.presence(["u1"], 3)
    assert seen[0] == (
        "/api/v1/internal/huddles/r1/summon",
        "s3cret",
        {"speakerUserId": "u1", "ownerId": "org_1", "roomToken": "tok"},
    )
    assert (
        seen[1][0] == "/api/v1/internal/huddles/calls/c1/end"
        and seen[1][2]["usageSeconds"] == 7
    )
    assert seen[2][2] == {
        "userIds": ["u1"],
        "version": 3,
        "ownerId": "org_1",
        "roomToken": "tok",
    }


def _backend(monkeypatch, handler):
    import httpx

    from huddle_agent.backend import Backend

    monkeypatch.setenv("LIGHTBULB_API_URL", "https://api-dev.example")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s3cret")
    b = Backend("r1", "org_1", "tok")
    b._http._transport = httpx.MockTransport(handler)
    return b


async def test_backend_keeps_real_error_codes(monkeypatch):
    import httpx

    responses = iter(
        [
            httpx.Response(
                400,
                json={"code": "INVALID_REQUEST", "message": "bad", "retryable": False},
            ),
            httpx.Response(400, text="nope"),
            httpx.Response(401, json={"detail": "invalid huddle room token"}),
            httpx.Response(409, json={"detail": {"code": "turn_conflict"}}),
        ]
    )
    b = _backend(monkeypatch, lambda req: next(responses))
    codes = []
    for _ in range(4):
        try:
            await b.turn("c1", "u1", "fin", "r1", "hi", "")
        except BackendError as e:
            codes.append((e.status, e.code))
    assert codes == [
        (400, "invalid_request"),
        (400, "invalid_request"),
        (401, "unauthorized"),
        (409, "turn_conflict"),
    ]


async def test_end_retries_transport_failures(monkeypatch):
    import types

    import httpx

    import huddle_agent.backend as backend_mod

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(backend_mod, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"closed": True})

    await _backend(monkeypatch, handler).end("c1", 5)
    assert len(attempts) == 3 and sleeps == [1, 2]

    attempts.clear()
    gateway = iter(
        [
            httpx.Response(503, text="deploying"),
            httpx.Response(502),
            httpx.Response(200, json={}),
        ]
    )
    await _backend(monkeypatch, lambda req: (attempts.append(1), next(gateway))[1]).end(
        "c1", 5
    )
    assert len(attempts) == 3  # a gateway 5xx during a backend deploy is retried too

    attempts.clear()
    try:
        await _backend(
            monkeypatch, lambda req: (attempts.append(1), httpx.Response(500))[1]
        ).end("c1", 5)
        raise AssertionError("a 500 is not retried")
    except BackendError as e:
        assert e.status == 500
    assert len(attempts) == 1

    attempts.clear()
    b = _backend(
        monkeypatch,
        lambda req: (
            attempts.append(1),
            (_ for _ in ()).throw(httpx.ConnectError("down")),
        )[1],
    )
    try:
        await b.end("c1", 5)
        raise AssertionError("should raise after retries")
    except BackendError as e:
        assert e.code == "huddles_upstream"
    assert len(attempts) == 4  # first try + 3 retries


class _Pub:
    def __init__(self, sid, name):
        self.sid, self.name = sid, name


class _LocalParticipant:
    def __init__(self):
        self.track_publications = {
            "TR_f": _Pub("TR_f", "agent-forge"),
            "TR_g": _Pub("TR_g", "agent-gauge"),
            "TR_r": _Pub("TR_r", "roomio_audio"),
        }
        self.unpublished = []

    async def unpublish_track(self, sid):
        self.unpublished.append(sid)
        self.track_publications.pop(sid)


class _FakeRoom:
    def __init__(self):
        self.local_participant = _LocalParticipant()


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


def _fake_live_stack(monkeypatch, refuse=()):
    """start_live against fakes: `refuse` is the voices whose session.start raises."""
    import huddle_agent.agent as agent_mod

    made = []

    class FakeSession:
        def __init__(self, llm, vad):
            self.voice, self.handlers, self.closed = llm["voice"], {}, False
            self.room_io = types_ns(set_participant=lambda i: None)
            self.usage = types_ns(model_usage=[])
            self.started = {}
            self.interrupts = []
            made.append(self)

        def on(self, event, fn):
            self.handlers[event] = fn

        async def start(self, **kw):
            self.started = kw
            if self.voice in refuse:
                raise RuntimeError(f"voice {self.voice} refused")

        def interrupt(self, force=False):
            self.interrupts.append(force)

        def generate_reply(self, **kw):
            pass

        async def aclose(self):
            self.closed = True

    class FakeVoice:
        def __init__(self, agent_id, persona, huddle, seed):
            self.duplex_session = types_ns(
                append_thinking=None,
                append_commentary=None,
                append_instructions=None,
                mute_input=None,
            )

    monkeypatch.setattr(agent_mod, "AgentSession", FakeSession)
    monkeypatch.setattr(agent_mod, "_TrimmedGPTLiveModel", lambda **kw: kw)
    monkeypatch.setattr(agent_mod, "_Voice", FakeVoice)
    return agent_mod, made


async def _start(agent_mod, room, huddle, voice="cedar"):
    return await agent_mod.start_live(
        room,
        huddle,
        None,
        agent_id="forge",
        persona="p",
        voice=voice,
        speaker="u1",
        seed=[],
    )


async def test_room_options_per_agent_track_no_text_no_preconnect():
    from huddle_agent.agent import room_options

    o = room_options("forge", "u1")
    assert o.text_input is False
    assert o.audio_input.pre_connect_audio is False
    assert o.audio_output.track_name == "agent-forge"
    assert o.participant_identity == "u1" and o.close_on_disconnect is False


async def test_start_live_disables_recording_and_unpublishes_only_its_own_track(
    monkeypatch,
):
    agent_mod, made = _fake_live_stack(monkeypatch)
    room = _FakeRoom()
    live = await _start(agent_mod, room, _Recorder())
    assert made[0].started["record"] is False
    assert made[0].started["room_options"].audio_output.track_name == "agent-forge"
    assert live.agent_id == "forge" and live.track_name == "agent-forge"
    live.interrupt()
    assert made[0].interrupts == [True]
    await live.aclose()
    assert room.local_participant.unpublished == ["TR_f"]


async def test_session_events_carry_the_agent_id(monkeypatch):
    from livekit.agents.llm import ChatMessage
    from livekit.agents.llm.chat_context import AgentHandoff

    agent_mod, made = _fake_live_stack(monkeypatch)
    huddle = _Recorder()
    live = await _start(agent_mod, _FakeRoom(), huddle)
    ev = made[0].handlers
    ev["conversation_item_added"](
        types_ns(item=AgentHandoff(new_agent_id="forge"))
    )  # no .role: crashed in prod
    ev["conversation_item_added"](
        types_ns(item=ChatMessage(role="assistant", content=["shipped it"]))
    )
    ev["conversation_item_added"](
        types_ns(item=ChatMessage(role="user", content=["what shipped"]))
    )
    ev["agent_state_changed"](types_ns(new_state="speaking"))
    ev["user_state_changed"](types_ns(new_state="speaking"))
    ev["close"](None)
    assert huddle.calls == [
        ("on_agent_said", "forge", "shipped it"),
        ("on_agent_state", "forge", "speaking"),
        ("on_speaker_activity", "forge"),
        ("on_live_closed", live),
    ]


async def test_a_refused_voice_retries_once_with_marin(monkeypatch):
    agent_mod, made = _fake_live_stack(monkeypatch, refuse={"cedar"})
    room = _FakeRoom()
    live = await _start(agent_mod, room, _Recorder())
    assert [m.voice for m in made] == ["cedar", "marin"]
    assert made[0].closed and room.local_participant.unpublished == [
        "TR_f"
    ]  # the failed one is cleaned up
    assert live._session is made[1]


async def test_marin_failing_too_raises(monkeypatch):
    import pytest

    agent_mod, made = _fake_live_stack(monkeypatch, refuse={"cedar", "marin"})
    with pytest.raises(RuntimeError):
        await _start(agent_mod, _FakeRoom(), _Recorder())
    assert [m.voice for m in made] == ["cedar", "marin"]


async def test_marin_itself_is_not_retried(monkeypatch):
    import pytest

    agent_mod, made = _fake_live_stack(monkeypatch, refuse={"marin"})
    with pytest.raises(RuntimeError):
        await _start(agent_mod, _FakeRoom(), _Recorder(), voice="marin")
    assert len(made) == 1


async def test_seed_becomes_the_agents_chat_ctx():
    from huddle_agent.agent import _Voice

    seed = [
        ("user", "Ada: hi"),
        ("assistant", "hello"),
        ("developer", "[Gauge (agent) said]: up 4%"),
    ]
    agent = _Voice("forge", "persona", None, seed)
    assert [(m.role, m.text_content) for m in agent.chat_ctx.items] == seed


async def test_trimmed_session_seeds_at_most_128_items():
    from livekit.agents import llm
    from livekit.plugins.openai.realtime import GPTLiveModel

    from huddle_agent.agent import _TrimmedSession

    s = object.__new__(
        _TrimmedSession
    )  # no connection: only the start event is under test
    s._opts, s._instructions = (
        GPTLiveModel(api_key="x", delegation="client")._opts,
        "persona",
    )
    s._history = llm.ChatContext.empty()
    for i in range(200):
        s._history.add_message(role="user", content=f"line {i}")
    start = s._session_start_event()
    assert len(start.session.input) == 128
    assert start.session.input[-1].content[0].text == "line 199"
    assert (
        len(s._history.items) == 200
    )  # the full history is kept for the next reconnect


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


async def test_empty_room_before_anyone_joins_does_not_shut_down():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({})
    assert not room.shut
    await h.on_participants({"u1": "Ana"})
    await leave_all(h, clock)
    assert room.shut


async def test_nobody_arrives_within_grace_shuts_down_once():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({})
    clock.t += 59
    await h.tick()
    assert not room.shut
    clock.t += 2
    await h.tick()
    assert room.shut
    assert log.count("shutdown") == 1


async def test_an_idle_room_ends_and_says_why():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    clock.t += h_mod.ROOM_IDLE_SECONDS - 1
    await h.tick()
    assert not room.published
    clock.t += 1
    await h.tick()
    assert room.published == [
        ("huddle.error", {"code": "idle_ended", "reason": None}, ["u1", "u2"])
    ]
    assert backend.presences[-1] == [] and not room.shut
    await h.tick()
    assert room.shut


async def test_the_last_human_leaving_clears_the_marker_first_even_if_the_post_hangs(
    monkeypatch,
):
    h, room, backend, lives, clock, log = await summoned()

    async def hang(user_ids, version=0):
        log.append(("presence", tuple(user_ids)))
        await asyncio.sleep(60)

    monkeypatch.setattr(h_mod, "PRESENCE_TIMEOUT", 0.01)
    backend.presence = hang
    await leave_all(h, clock)
    tail = log[log.index(("presence", ())) + 1 :]
    assert (
        tail.index("end") < tail.index("shutdown") == len(tail) - 1
    )  # timed out, ended, then left


async def test_shutdown_clears_the_marker_only_when_nobody_is_left():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await (
        h.clear_presence()
    )  # a worker restart with people in the call: their marker stays
    assert backend.presences == [["u1"]]
    h._people = {}  # the job was cancelled before the leave's own presence went out
    await h.clear_presence()
    assert backend.presences[-1] == [] and backend.versions[-1] > backend.versions[0]


async def test_presence_versions_increase():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    assert backend.versions[0] < backend.versions[1]


async def test_backend_client_option_d_routes(monkeypatch):
    import httpx

    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(
            200, json={"agentId": "gauge", "runId": "r1", "steered": False}
        )

    b = _backend(monkeypatch, handler)
    await b.team()
    assert (await b.route("u1", "what's revenue", "t"))["agentId"] == "gauge"
    await b.session("gauge")
    await b.turn("c1", "u1", "gauge", "r1", "hi", "since")
    await b.abort("r1")
    base = {"ownerId": "org_1", "roomToken": "tok"}
    assert seen == [
        ("/api/v1/internal/huddles/r1/team", base),
        (
            "/api/v1/internal/huddles/r1/route",
            {
                "speakerUserId": "u1",
                "utterance": "what's revenue",
                "transcript": "t",
                "humans": None,
                "floor": None,
                "voiceRuns": [],
                "at": [],
                "setting": "live",
                "unclaimed": False,
                **base,
            },
        ),
        ("/api/v1/internal/huddles/r1/agents/gauge/session", base),
        (
            "/api/v1/internal/huddles/calls/c1/turns",
            {
                "speakerUserId": "u1",
                "agentId": "gauge",
                "runId": "r1",
                "utterance": "hi",
                "transcript": "since",
                "steerRunId": None,
                **base,
            },
        ),
        ("/api/v1/internal/huddles/r1/runs/r1/abort", base),
    ]


# -- barge-in from anyone, per agent (D §9; ported from #3543) --


def kinds(live):
    return [c[0] for c in live.calls]


async def speaking():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_state("forge", "speaking")
    return h, room, backend, lives, clock, log


def metric_count(capsys, name):
    return sum(
        name in json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    )


async def test_backchannels_never_interrupt():
    h, room, backend, lives, clock, log = await speaking()
    for line in [
        "yeah",
        "mm-hmm",
        "okay sure",
        "uh-huh right",
        "got it, cool",
        "yeah yeah yeah",
    ]:
        h.on_interim_transcript("u2", "Aden", line)
        await say(h, "u2", line)
    assert "interrupt" not in kinds(lives[0]) and "set_participant" not in kinds(
        lives[0]
    )
    assert backend.turns == []


async def test_non_speakers_sentence_cuts_repoints_and_drives_the_turn(capsys):
    h, room, backend, lives, clock, log = await speaking()
    h.on_interim_transcript("u2", "Aden", "actually the numbers")
    assert lives[0].calls[-1] == ("interrupt",)
    assert room.written()["lightbulb.agent.forge.status"] == "listening"
    h.on_interim_transcript("u2", "Aden", "actually the numbers are off")  # cut once
    await say(h, "u2", "actually the numbers are off by a week")
    assert kinds(lives[0]).count("interrupt") == 1
    note = 'Aden cut in: "actually the numbers are off by a week" — already handed to your backend self; don\'t delegate it again'
    assert lives[0].calls[-4:] == [
        ("set_participant", "u2"),
        ("thinking", "Aden is talking to you", None),
        ("thinking", note, None),
        ("nudge",),
    ]
    assert [(t["speaker"], t["agentId"], t["utterance"]) for t in backend.turns] == [
        ("u2", "forge", "actually the numbers are off by a week")
    ]
    await h.on_delegation(
        "forge", "d1", "the numbers"
    )  # the voice delegates it: bound, never a second POST
    assert len(backend.turns) == 1
    assert metric_count(capsys, "live.bargein.cut") == 1


async def test_cut_in_on_a_final_transcript_only():
    h, room, backend, lives, clock, log = await speaking()
    await say(h, "u2", "that is not what we shipped")
    assert kinds(lives[0])[-5:] == [
        "interrupt",
        "set_participant",
        "thinking",
        "thinking",
        "nudge",
    ]
    assert (
        backend.turns[-1]["speaker"] == "u2"
        and backend.turns[-1]["utterance"] == "that is not what we shipped"
    )


async def test_stop_interrupts_and_takes_the_floor_without_a_turn():
    h, room, backend, lives, clock, log = await speaking()
    h.on_interim_transcript("u2", "Aden", "stop")
    assert lives[0].calls[-1] == ("interrupt",)
    await say(h, "u2", "stop")
    assert lives[0].calls[-1] == ("thinking", 'Aden cut in: "stop"', None)
    assert ("set_participant", "u2") in lives[0].calls
    assert (
        backend.turns == [] and backend.aborts == []
    )  # a non-speaker's bare stop names nobody's run


async def test_hold_on_interrupts():
    h, room, backend, lives, clock, log = await speaking()
    await say(h, "u2", "hold on")
    assert ("interrupt",) in lives[0].calls and backend.turns == []


async def test_current_speaker_does_not_cross_cut():
    h, room, backend, lives, clock, log = await speaking()
    h.on_interim_transcript("u1", "Prasiddha", "wait, that is wrong")
    await say(h, "u1", "wait, that is wrong")
    assert "interrupt" not in kinds(
        lives[0]
    )  # GPT-Live hears the speaker and handles barge-in itself


async def test_no_interrupt_when_the_floor_holder_is_not_speaking():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_state("forge", "thinking")
    # not "stop everything please": that is now "everyone stop" (engine §3.6), which cuts the floor
    h.on_interim_transcript("u2", "Aden", "wait, check the other branch please")
    await say(h, "u2", "wait, check the other branch please")
    assert "interrupt" not in kinds(lives[0]) and "set_participant" not in kinds(
        lives[0]
    )


async def test_empty_transcript_noise_is_ignored():
    h, room, backend, lives, clock, log = await speaking()
    n = len(lives[0].calls)
    h.on_interim_transcript("u2", "Aden", "  ")
    await say(h, "u2", "")
    assert len(lives[0].calls) == n


async def test_naming_another_agent_over_the_floor_holder_cuts_then_switches():
    h, room, backend, lives, clock, log = await speaking()
    h.on_interim_transcript("u2", "Aden", "Gauge what")
    assert lives[0].calls[-1] == ("interrupt",)
    await say(h, "u2", "Gauge what's the latency")
    assert [l.agent_id for l in lives] == ["forge", "gauge"] and lives[
        1
    ].speaker == "u2"
    assert ("close",) in lives[0].calls
    assert kinds(lives[0]).count("interrupt") == 1


async def test_one_cut_per_utterance():
    h, room, backend, lives, clock, log = await speaking()
    for partial in ["no no", "no no that", "no no that is wrong"]:
        h.on_interim_transcript("u2", "Aden", partial)
    h.on_agent_state(
        "forge", "speaking"
    )  # still mid-sentence: the same utterance never cuts twice
    h.on_interim_transcript("u2", "Aden", "no no that is wrong because")
    assert kinds(lives[0]).count("interrupt") == 1
    await say(h, "u2", "no no that is wrong because of billing")
    h.on_agent_state("forge", "speaking")
    h.on_interim_transcript(
        "u2", "Aden", "hang on"
    )  # Aden is the pinned speaker now: GPT-Live's own barge-in
    assert kinds(lives[0]).count("interrupt") == 1
    h.on_interim_transcript(
        "u1", "Prasiddha", "hang on"
    )  # a new utterance from someone else cuts again
    assert kinds(lives[0]).count("interrupt") == 2


async def test_an_empty_final_after_a_cut_resumes_with_a_nudge(capsys):
    h, room, backend, lives, clock, log = await speaking()
    h.on_interim_transcript("u2", "Aden", "but the")
    await say(h, "u2", "")
    assert lives[0].calls[-2:] == [("interrupt",), ("nudge",)]
    h.on_agent_state("forge", "speaking")
    h.on_interim_transcript("u2", "Aden", "wait")
    await say(
        h, "u2", "yeah"
    )  # a backchannel final: carry on, still pinned to Prasiddha
    assert lives[0].calls[-2:] == [("interrupt",), ("nudge",)]
    assert h._voice.speaker == "u1" and backend.turns == []
    assert metric_count(capsys, "live.bargein.empty") == 2


def test_pause_rules():
    from huddle_agent.transcript import pause

    assert pause("hold on") and pause("Wait, um.") and pause("stop")
    assert not pause("wait, the numbers are off") and not pause("yeah")


# -- hand-offs per agent (D §9; ported from #3543) --


async def test_handoff_repoints_and_the_named_person_keeps_the_floor():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    h.on_agent_said(
        "forge", "We moved the launch to Friday. Aden, does that timeline work?"
    )
    assert lives[0].calls[-2:] == [
        ("set_participant", "u2"),
        ("thinking", "Aden is talking to you", None),
    ]
    clock.t += HANDOFF_SECONDS - 1
    await say(h, "u2", "yes that works")  # answered without naming anyone
    clock.t += HANDOFF_SECONDS
    await h.tick()
    assert (
        lives[0].calls[-1][0] != "set_participant"
    )  # no return: Aden is the speaker now
    assert h._voice.speaker == "u2" and h._voice.handoff is None


async def test_handoff_question_form_mid_sentence():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_said("forge", "That covers billing, so what do you think, aden?")
    assert ("set_participant", "u2") in lives[0].calls


async def test_handoff_window_lapses_and_returns():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_said("forge", "Aden, can you confirm?")
    clock.t += HANDOFF_SECONDS - 1
    await h.tick()
    assert lives[0].calls[-2][0:2] == ("set_participant", "u2")
    clock.t += 1
    await h.tick()
    assert lives[0].calls[-2:] == [
        ("set_participant", "u1"),
        ("thinking", "Prasiddha is talking to you", None),
    ]
    clock.t += IDLE_SECONDS  # normal idle rules again
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_handoff_previous_speaker_speaking_first_takes_it_back():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_said("forge", "Aden, does that work?")
    await say(h, "u1", "actually let me answer that")
    assert lives[0].calls[-3:] == [
        ("set_participant", "u1"),
        ("thinking", "Prasiddha is talking to you", None),
        (
            "reply_to",
            "actually let me answer that",
        ),  # the session was pinned away from them: it never heard it
    ]


async def test_handoff_someone_else_first_returns_and_their_line_is_context():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden", "u3": "Leo"})
    h.on_agent_said("forge", "Aden, does that work?")
    clock.t += THINKING_EVERY
    await h.on_final_transcript("u3", "Leo", "I think it does")
    assert lives[0].calls[-3:] == [
        ("set_participant", "u1"),
        ("thinking", "Prasiddha is talking to you", None),
        ("thinking", "Leo: I think it does", None),
    ]


async def test_handoff_only_from_the_floor_holder_and_ignores_statements():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_said("forge", "Aden shipped that last week.")
    h.on_agent_said("forge", "Prasiddha, does that help?")  # already the speaker
    h.on_agent_said("forge", "Does that work for Adenine?")  # whole word only
    h.on_agent_said("gauge", "Aden, does that work?")  # not the floor-holder
    assert "set_participant" not in kinds(lives[0])


async def test_human_naming_a_human_does_nothing():
    h, room, backend, lives, clock, log = await summoned()
    await say(h, "u1", "Aden, does that timeline work?")
    await say(h, "u2", "Prasiddha, can you confirm?")
    clock.t += 3
    await h.tick()
    assert "set_participant" not in kinds(lives[0])


def test_addressed_by_name_rules():
    from huddle_agent.transcript import addressed_by_name

    people = {"u1": "Prasiddha Parthsarthy", "u2": "Aden"}
    assert addressed_by_name("Aden, can you confirm?", people) == "u2"
    assert addressed_by_name("Done. What do you think, Prasiddha?", people) == "u1"
    assert (
        addressed_by_name("Aden and Prasiddha, any thoughts?", people) == "u1"
    )  # the last one named is asked
    assert (
        addressed_by_name("Aden, can you confirm? Great, moving on.", people) is None
    )  # last sentence only
    assert addressed_by_name("Aden shipped it.", people) is None
    assert addressed_by_name("", people) is None


# -- wake-word quality metrics (D Unknown 5) --


def metric_lines(out, name):
    return [
        m
        for m in (json.loads(x) for x in out.splitlines() if x.startswith("{"))
        if name in m
    ]


async def test_wake_fired_carries_name_kind_and_match_and_no_text(capsys):
    h, room, backend, lives, clock, log = make()
    backend.team_data["people"].append({"userId": "u3", "name": "Gauge Smith"})
    await h.on_participants({"u1": "Prasiddha"})
    for line in (
        "hey Forge check the zebra deploy",
        "hey Forje check the zebra deploy",
        "hey Finance check the zebra deploy",
        "hey Gauge check the zebra deploy",
    ):
        await say(h, "u1", line)
        await h.end_exchange()
    out = capsys.readouterr().out
    fired = [(m["name_kind"], m["match"]) for m in metric_lines(out, "live.wake.fired")]
    assert fired == [
        ("custom", "exact"),
        ("custom", "fuzzy"),
        ("default", "exact"),
        ("collision", "exact"),
    ]
    assert metric_lines(out, "live.wake.fired")[0]["_aws"]["CloudWatchMetrics"][0][
        "Dimensions"
    ] == [["name_kind", "match"]]
    assert "zebra" not in out


async def test_a_woken_voice_used_for_nothing_is_counted_unused(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await say(h, "u1", "hey Forge zebra")
    await say(
        h, "u1", "never mind"
    )  # the driven run is stopped: the wake bought nothing
    await h.end_exchange()
    out = capsys.readouterr().out
    assert [
        (m["name_kind"], m["match"]) for m in metric_lines(out, "live.wake.unused")
    ] == [("custom", "exact")]
    assert "zebra" not in out


async def test_a_woken_voice_that_was_used_is_not_unused(capsys):
    h, room, backend, lives, clock, log = await summoned(
        drive=True
    )  # the driven run was accepted
    await h.end_exchange()
    (
        h,
        room,
        backend,
        lives,
        clock,
        log,
    ) = await summoned()  # driven retired, then a delegation
    await h.on_delegation("forge", "d1", "check it")
    await h.end_exchange()
    (
        h,
        room,
        backend,
        lives,
        clock,
        log,
    ) = await summoned()  # driven retired, then the speaker talks
    await say(h, "u1", "and the logs too")
    await h.end_exchange()
    h, room, backend, lives, clock, log = make()  # a tap is not a wake
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1", json.dumps({"agentId": "forge"}))
    await h.end_exchange()
    assert metric_lines(capsys.readouterr().out, "live.wake.unused") == []


async def test_ambiguous_and_repeat_after_voice_failed(capsys):
    h, room, backend, lives, clock, log = make()
    backend.team_data["people"].append({"userId": "u3", "name": "Gauge Smith"})
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "Gauge, what's up")
    h.fail.add("forge")
    await say(h, "u1", "Forge, hi")
    clock.t += 5
    await say(h, "u2", "Forge, hi")  # another person: not a repeat
    await say(h, "u1", "Forge, hi again")
    clock.t += 11
    await say(h, "u1", "Forge, hi")  # u1's last failure was 11 s ago
    out = capsys.readouterr().out
    assert len(metric_lines(out, "live.wake.ambiguous")) == 1
    assert len(metric_lines(out, "live.wake.repeat")) == 1


async def test_a_switch_during_a_cold_start_still_delivers_the_request_as_news():
    h, room, backend, lives, clock, log = await summoned()
    backend.turn_results = [BackendError(503, "runtime_unavailable", None)]

    async def sleep(
        seconds,
    ):  # the floor moves to Gauge while Forge's turn waits out the cold start
        clock.t += seconds
        await h.talk_start("u1", json.dumps({"agentId": "gauge"}))

    h._sleep = sleep
    await h.on_delegation("forge", "d1", "check the board")
    assert h._voice.agent_id == "gauge" and len(backend.turns) == 2
    run_id = backend.turns[0]["runId"]
    assert backend.turns[1]["runId"] == run_id and backend.aborts == []
    answer(h, run_id, "forge", "Board is green.")
    assert h._news["forge"][0][2] == "Board is green."
    assert not [c for c in lives[0].calls if c[0] == "commentary"]


async def test_names_said_to_the_floor_holder_wait_for_its_driven_answer():
    h, room, backend, lives, clock, log = await summoned()
    await say(h, "u1", "Forge and Gauge, what shipped?")
    (t,) = backend.turns
    assert t["agentId"] == "forge" and h._voice.driven_run == t["runId"]
    assert room.written()["lightbulb.queue"] == "forge,gauge"
    assert any(c[0] == "thinking" and "already handed" in c[1] for c in lives[0].calls)
    await h.on_delegation(
        "forge", "d1", "what shipped"
    )  # GPT-Live's own delegation binds, no second POST
    assert len(backend.turns) == 1
    clock.t += 5
    await h.tick()
    assert len(lives) == 1  # Forge's answer is still running
    answer(h, t["runId"], "forge", "Huddles.")
    clock.t += h_mod.QUEUE_SETTLE
    await h.tick()
    await settle(h)
    assert [l.agent_id for l in lives] == ["forge", "gauge"]


async def test_latency_route_session_and_first_audio_once(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    h.gate["live"] = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "hey Forge what shipped")
    for _ in range(10):
        await asyncio.sleep(0)
    clock.t += 1.5
    h.gate["live"].set()
    await settle(h)
    clock.t += 0.6
    h.on_agent_state("forge", "speaking")
    h.on_agent_state("forge", "listening")
    h.on_agent_state("forge", "speaking")
    lines = [
        json.loads(x)
        for x in capsys.readouterr().out.splitlines()
        if "live.latency" in x
    ]
    got = [
        (
            next(k for k in x if k.startswith("live.latency")),
            x[next(k for k in x if k.startswith("live.latency"))],
            x["path"],
        )
        for x in lines
    ]
    assert got == [
        ("live.latency.route", 0, "name"),
        ("live.latency.session_started", 1500, "name"),
        ("live.latency.first_audio", 2100, "name"),
    ]
    assert lines[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["path"]]


async def test_gptlive_seconds_sum_closed_and_open_voices():
    h, room, backend, lives, clock, log = await summoned()
    lives[0].final_usage = 42
    await h.talk_start("u1", json.dumps({"agentId": "gauge"}))
    lives[1].usage = 5
    assert h.gptlive_seconds() == 47


def test_emit_metric_dims_shape(capsys):
    h_mod.emit_metric("x.y", 3, "Milliseconds", {"path": "jev"})
    h_mod.emit_metric("x.z", 1)
    a, b = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert (
        a["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["path"]]
        and a["path"] == "jev"
        and a["x.y"] == 3
    )
    assert b["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]] and b["x.z"] == 1


# -- routing v2 (worker, §4.2-4.3, §6) --


async def test_a_mid_line_name_no_longer_summons():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "let's ask Forge later")
    assert (
        backend.routes[0][1] == "let's ask Forge later" and lives == []
    )  # /route's addressing stage decides


async def test_a_superseded_route_is_dropped_but_the_same_speaker_continues(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    backend.jev_to, backend.route_gate = "forge", asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "what's our MRR this month")
    await h.on_final_transcript(
        "u2", "Aden", "it's about 42k I think"
    )  # someone answered meanwhile
    backend.route_gate.set()
    await settle(h)
    assert [l.speaker for l in lives] == ["u2"]  # the newer line routed itself
    assert metric_count(capsys, "live.route.superseded") == 1
    await h.end_exchange()
    backend.route_gate = asyncio.Event()
    await h.on_final_transcript("u1", "Prasiddha", "what's our MRR this month")
    await h.on_final_transcript(
        "u1", "Prasiddha", "and last month too"
    )  # a split utterance: not superseded
    backend.route_gate.set()
    await settle(h)
    assert (
        lives[1].speaker == "u1" and metric_count(capsys, "live.route.superseded") == 0
    )


async def test_a_second_speaker_is_routed_into_the_queue(capsys):
    h, room, backend, lives, clock, log = await summoned("Forge")
    backend.jev_to = "gauge"
    clock.t += THINKING_EVERY
    await say(h, "u2", "can someone check the invoices")
    assert h._queue == [("gauge", "u2", "can someone check the invoices")]
    assert backend.route_bodies[-1]["floor"] == {
        "agent": "forge",
        "speaker": "Prasiddha",
    }
    assert ("thinking", "Aden: can someone check the invoices", None) in lives[
        0
    ].calls  # still heard
    assert metric_count(capsys, "live.route.queued") == 1
    clock.t += 2  # the floor settles: Gauge answers Aden
    await h.tick()
    await settle(h)
    assert (lives[1].agent_id, lives[1].speaker) == ("gauge", "u2")
    assert backend.turns[-1]["utterance"] == "can someone check the invoices"


async def test_a_second_speaker_nobody_picks_is_thinking_only():
    h, room, backend, lives, clock, log = await summoned("Forge")
    clock.t += THINKING_EVERY
    await say(h, "u2", "did you see the game last night")
    assert h._queue == [] and len(lives) == 1
    assert lives[0].calls[-1] == (
        "thinking",
        "Aden: did you see the game last night",
        None,
    )


async def test_hold_waits_for_the_speakers_next_final_and_routes_the_whole_line():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    backend.jev_to, backend.route_extra = "forge", {"hold": True}
    await say(h, "u1", "we're trying to")
    assert lives == [] and backend.summons == []
    backend.route_extra = {}
    await say(h, "u1", "find the launch checklist")
    assert backend.routes[-1][1] == "we're trying to find the launch checklist"
    assert (
        lives[0].agent_id == "forge"
        and backend.turns[0]["utterance"] == "we're trying to find the launch checklist"
    )
    await h.end_exchange()


async def test_a_hold_that_lands_after_the_speakers_next_final_is_dropped(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    backend.jev_to, backend.route_extra, backend.route_gate = (
        "forge",
        {"hold": True},
        asyncio.Event(),
    )
    slow = backend.route_gate
    await h.on_final_transcript("u1", "Prasiddha", "about the invoices")
    await asyncio.sleep(0)  # /route is asked and is still deciding...
    backend.route_extra, backend.route_gate = {}, None
    await h.on_final_transcript(
        "u1", "Prasiddha", "are they paid"
    )  # ...when the rest arrives: it routes on its own
    slow.set()
    await settle(h)
    assert backend.turns[0]["utterance"] == "are they paid"
    assert (
        h._held == {} and metric_count(capsys, "live.route.hold_stale") == 1
    )  # nothing stale to merge later
    await h.end_exchange()


async def test_every_addressed_agent_is_queued_in_order():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    backend.route_extra = {"agentIds": ["forge", "gauge"]}
    await say(h, "u1", "Lightbulb, have Forge and Gauge check CI")
    assert lives[0].agent_id == "forge"
    assert h._queue == [("gauge", "u1", "Lightbulb, have Forge and Gauge check CI")]
    await h.end_exchange()


async def test_late_lines_steer_the_floor_holders_run(capsys):
    h, room, backend, lives, clock, log = await summoned("Forge", drive=True)
    backend.steer = True
    clock.t += THINKING_EVERY
    await say(h, "u2", "it's already green, I checked")
    assert backend.late_steers == [
        {
            "speaker": "u1",
            "agentId": "forge",
            "steerRunId": backend.turns[0]["runId"],
            "utterance": h_mod.LATE_LINES + "Aden: it's already green, I checked",
        }
    ]
    assert len(backend.turns) == 1  # never a new run
    assert metric_count(capsys, "live.steer.late_line") == 1


async def test_missed_and_unwanted_metrics(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "can you check the invoices")  # nobody
    clock.t += 5
    await say(h, "u1", "Gauge, check the invoices")
    assert metric_count(capsys, "live.route.missed") == 1
    await h.end_exchange()
    await h.on_participants({"u1": "Prasiddha"})
    backend.jev_to = "forge"
    await say(h, "u1", "what's the deploy status")
    await say(h, "u1", "stop")  # the Jev pick was wrong
    assert metric_count(capsys, "live.route.unwanted") == 1


async def test_route_sends_line_times_for_the_backend_reask_rule():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "what's our MRR")
    clock.t += 7
    await say(h, "u2", "no idea honestly")
    assert backend.route_bodies[-1]["at"] == [1000, 1007]


async def test_the_transcript_reaches_the_backend_in_batches_and_at_shutdown():
    """What people and agents said is persisted (the backend logs it) so a bad call can be read
    back: every TRANSCRIPT_FLUSH_S while lines accumulate, and whatever is left when the job ends."""
    from huddle_agent.huddle import TRANSCRIPT_FLUSH_S

    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", "hey everyone")
    await say(h, "u2", "morning")
    await h.tick()
    await settle(h)
    assert backend.transcripts == []  # not yet: a batch every TRANSCRIPT_FLUSH_S
    clock.t += TRANSCRIPT_FLUSH_S
    await h.tick()
    await settle(h)
    huddle_id, lines = backend.transcripts[0]
    assert [(x["mark"], x["name"], x["text"]) for x in lines] == [
        (0, "Prasiddha", "hey everyone"),
        (1, "Aden", "morning"),
    ]
    await say(h, "u1", "one more thing")
    clock.t += TRANSCRIPT_FLUSH_S
    backend.transcript_gate = (
        asyncio.Event()
    )  # a batch is in flight when the job shuts down...
    await h.tick()
    await asyncio.sleep(0)  # the batch snapshots its lines and stalls on the gate
    await h.on_final_transcript(
        "u2", "Aden", "bye"
    )  # not say(): settle would wait on that batch
    shutdown = asyncio.ensure_future(h.clear_presence())
    await asyncio.sleep(0)
    backend.transcript_gate.set()
    await shutdown  # ...shutdown waits for it, then sends what was said meanwhile
    assert [x["text"] for x in backend.transcripts[1][1]] == ["one more thing"]
    assert [x["text"] for x in backend.transcripts[2][1]] == ["bye"]
    assert len(backend.transcripts) == 3 and backend.transcripts[-1][0] == huddle_id


async def test_a_startup_snapshot_finishing_after_the_first_join_does_not_end_the_huddle():
    """10-02: the entrypoint's startup on_participants (empty) and the first participant_connected
    event run concurrently; when the empty one finished second it read as "everyone left" and shut
    a two-person huddle down 1 s in. An empty read only schedules a re-check (EMPTY_ROOM_GRACE)
    against the latest snapshot; a room that is really empty still ends."""
    from huddle_agent.huddle import EMPTY_ROOM_GRACE

    h, room, backend, lives, clock, log = make()
    gate = backend.presence_gate = asyncio.Event()
    startup = asyncio.ensure_future(
        h.on_participants({})
    )  # the snapshot: empty, stalls on its presence post
    await asyncio.sleep(0)
    backend.presence_gate = None
    await h.on_participants({"u1": "Prasiddha"})  # the first join lands meanwhile
    gate.set()
    await startup  # the snapshot finishes second
    assert room.shut is False
    for _ in range(EMPTY_ROOM_GRACE + 1):
        clock.t += 1
        await h.tick()
    assert room.shut is False  # the latest snapshot has a person in it
    await h.on_participants({})  # now they really leave
    assert room.shut is False  # not yet: the grace
    clock.t += EMPTY_ROOM_GRACE
    await h.tick()
    assert room.shut is True and log[-1] == "shutdown"


async def test_a_mid_sentence_name_switches_the_floor_when_the_backend_says_it_was_addressed(
    capsys,
):
    """10-02 21:23 EDT: "Cool, and Friday, what about you?" while Forge held the floor went to Forge. The
    worker's fast path only matches a name at the start of a line; a name anywhere in the floor-holder's
    speaker's line now goes to /route's addressing stage, and an addressed agent takes the floor."""
    h, room, backend, lives, clock, log = await summoned("Forge")
    backend.jev_to = "gauge"  # /route: Gauge was addressed
    await say(h, "u1", "Cool, and Gauge, what about you?")
    assert backend.routes[-1][1] == "Cool, and Gauge, what about you?"
    assert (lives[-1].agent_id, lives[-1].speaker) == ("gauge", "u1")
    assert metric_count(capsys, "live.route.switch") == 1


async def test_a_mid_sentence_mention_keeps_the_floor():
    h, room, backend, lives, clock, log = await summoned("Forge")
    backend.jev_to = None  # /route: a mention, not a summons
    await say(h, "u1", "let's ask Gauge later")
    assert [x.agent_id for x in lives] == ["forge"]
    await say(
        h, "u1", "Finance looks fine to me"
    )  # a default name mid-sentence never goes to /route
    assert backend.routes[-1][1] == "let's ask Gauge later"


async def test_news_nobody_hears_is_posted_to_the_room_not_dropped():
    """10-02: Forge's answer landed after hang-up and went nowhere. News pushed out by the cap, expired by
    NEWS_TTL, or still pending when the huddle ends is posted by its agent into the room."""
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    for i in range(h_mod.NEWS_PER_AGENT + 1):
        answer(h, f"r{i}", "gauge", f"Answer {i}.")
    await settle(h)
    assert backend.late_answers == [
        ("gauge", "r0", "", "Answer 0.")
    ]  # the one the cap pushed out
    clock.t += h_mod.NEWS_TTL + 1
    await h.tick()
    await settle(h)
    assert [x[1] for x in backend.late_answers] == [
        f"r{i}" for i in range(h_mod.NEWS_PER_AGENT + 1)
    ]  # expired
    answer(h, "r-last", "forge", "Done at last.")
    await h.clear_presence()  # the huddle ends with it unheard
    assert (
        backend.late_answers[-1] == ("forge", "r-last", "", "Done at last.")
        and h._news == {}
    )


async def test_a_slow_driven_run_gets_one_holding_line(capsys):
    h, room, backend, lives, clock, log = await summoned(
        "Forge", drive=True
    )  # the driven run keeps running
    assert ("thinking", h_mod.HOLDING_LINE, None) not in lives[0].calls
    clock.t += h_mod.HOLDING_LINE_S
    await h.tick()
    assert ("thinking", h_mod.HOLDING_LINE, None) in lives[0].calls
    clock.t += 5
    await h.tick()
    assert (
        lives[0].calls.count(("thinking", h_mod.HOLDING_LINE, None)) == 1
    )  # once per run
    assert metric_count(capsys, "live.holding_line") == 1


async def test_a_ghost_final_reaches_neither_transcript_nor_router(capsys):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await say(h, "u1", h._vocabulary)
    await say(h, "u1", "A team voice call. Names: Ada, Forge")
    assert backend.routes == [] and h._transcript.text() == "" and lives == []
    assert metric_count(capsys, "live.stt.ghost_dropped") == 2
    await say(h, "u2", "Forge, what did we ship")  # real speech after a ghost still summons
    assert [l.agent_id for l in lives] == ["forge"]


def test_the_holding_line_promises_no_room_post():
    assert "room" not in h_mod.HOLDING_LINE and "taking a moment" in h_mod.HOLDING_LINE


async def test_backend_turn_carries_the_turns_requests_and_no_post_marks_a_run(monkeypatch):
    import httpx

    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})

    b = _backend(monkeypatch, handler)
    await b.turn("c1", "u1", "forge", "r1", "the PRs", "", requests=['Friday is answering "the rooms"'])
    await b.turn("c1", "u1", "forge", "r2", "hi", "")
    await b.no_post("r1")
    assert seen[0][1]["requests"] == ['Friday is answering "the rooms"'] and "requests" not in seen[1][1]
    assert seen[2] == ("/api/v1/internal/huddles/r1/runs/r1/no-post", {"ownerId": "org_1", "roomToken": "tok"})
