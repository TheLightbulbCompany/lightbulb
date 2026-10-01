import asyncio
import json

from huddle_agent.backend import BackendError
from huddle_agent.huddle import IDLE_SECONDS, Huddle

SUMMON = {
    "callId": "call-1",
    "persona": "You are the CEO.",
    "voice": "marin",
    "voiceName": "Lightbulb",
    "deadline": 10_000,
    "team": [{"id": "ceo", "name": "Ada"}, {"id": "fin", "name": "Finance"}],
}


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


class FakeRoom:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
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


class FakeBackend:
    def __init__(self, log):
        self.log = log
        self.summons: list[str] = []
        self.summon_error: BackendError | None = None
        self.turns: list[dict] = []
        self.turn_result: dict | BackendError = {"turnId": "t1", "agentId": "fin"}
        self.ends: list[tuple[str, int]] = []
        self.presences: list[list[str]] = []

    async def summon(self, speaker):
        self.summons.append(speaker)
        await asyncio.sleep(0)  # a real POST yields
        if self.summon_error:
            raise self.summon_error
        return SUMMON

    async def turn(self, call_id, speaker, seq, utterance, transcript):
        self.turns.append(
            {"callId": call_id, "speaker": speaker, "seq": seq, "utterance": utterance, "transcript": transcript}
        )
        if isinstance(self.turn_result, BackendError):
            raise self.turn_result
        return self.turn_result

    async def end(self, call_id, usage_seconds):
        self.ends.append((call_id, usage_seconds))
        self.log.append("end")

    async def presence(self, user_ids):
        self.presences.append(user_ids)


class FakeLive:
    def __init__(self, log, persona, voice, speaker):
        self.log = log
        self.persona, self.voice, self.speaker = persona, voice, speaker
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

    def nudge(self):
        self._rec("nudge")

    def reply_to(self, text):
        self._rec("reply_to", text)

    def usage_seconds(self):
        return self.usage

    async def aclose(self):
        self._rec("close")
        self.usage = self.final_usage


def make():
    log: list[str] = []
    room, backend, clock = FakeRoom(), FakeBackend(log), Clock()
    room.log = log
    lives: list[FakeLive] = []

    async def live_factory(persona, voice, speaker):
        live = FakeLive(log, persona, voice, speaker)
        lives.append(live)
        return live

    h = Huddle(room=room, backend=backend, live_factory=live_factory, clock=clock)
    return h, room, backend, lives, clock, log


async def settle(h):
    """Name summons run as tasks; wait for them."""
    while h._tasks:
        await asyncio.gather(*list(h._tasks))


async def summoned():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await h.on_final_transcript("u2", "Aden", "we shipped huddles")
    await h.on_final_transcript("u1", "Prasiddha", "hey Lightbulb what did we ship")
    await settle(h)
    return h, room, backend, lives, clock, log


async def test_name_summon_opens_one_live_session():
    h, room, backend, lives, clock, log = await summoned()
    assert backend.summons == ["u1"]
    assert len(lives) == 1 and lives[0].persona == "You are the CEO." and lives[0].voice == "marin"
    assert lives[0].speaker == "u1"  # the session starts pinned to the summoner
    kinds = [c[0] for c in lives[0].calls]
    assert kinds == ["thinking", "thinking", "reply_to"]
    calls = lives[0].calls
    assert calls[0][1] == "Transcript so far:\nAden: we shipped huddles\nPrasiddha: hey Lightbulb what did we ship"
    assert calls[1][1] == "Prasiddha is talking to you"
    assert calls[2] == ("reply_to", "hey Lightbulb what did we ship")


async def test_plain_line_does_not_summon():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.on_final_transcript("u1", "Prasiddha", "the deploy went fine")
    await settle(h)
    assert backend.summons == [] and lives == []


async def test_talk_by_another_person_repoints_without_second_summon():
    h, room, backend, lives, clock, log = await summoned()
    await h.talk_start("u2")
    assert backend.summons == ["u1"] and len(lives) == 1
    assert lives[0].calls[-2:] == [("set_participant", "u2"), ("thinking", "Aden is talking to you", None)]


async def test_talk_start_summons_without_request():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("u1")
    assert backend.summons == ["u1"]
    assert "reply_to" not in [c[0] for c in lives[0].calls]


async def test_talk_from_a_non_participant_is_ignored():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.talk_start("agent-other")
    assert backend.summons == []


async def test_voice_busy_publishes_error_and_opens_nothing():
    h, room, backend, lives, clock, log = make()
    backend.summon_error = BackendError(409, "voice_busy", None)
    await h.on_participants({"u1": "Prasiddha"})
    await h.on_final_transcript("u1", "Prasiddha", "Lightbulb, are you there")
    await settle(h)
    assert room.published == [("huddle.error", {"code": "voice_busy", "reason": None}, ["u1"])]
    assert lives == [] and backend.ends == []


async def test_idle_ends_exchange():
    h, room, backend, lives, clock, log = await summoned()
    clock.t += IDLE_SECONDS - 1
    await h.tick()
    assert backend.ends == []
    clock.t += 1
    await h.tick()
    assert [c[0] for c in lives[0].calls[-2:]] == ["mute", "close"]
    assert backend.ends == [("call-1", 42)]
    assert log[-3:] == ["mute", "close", "end"]
    # state cleared: the next name summons again
    await h.on_final_transcript("u1", "Prasiddha", "hey Lightbulb one more thing")
    await settle(h)
    assert backend.summons == ["u1", "u1"]


async def test_speaker_speech_resets_idle():
    h, room, backend, lives, clock, log = await summoned()
    clock.t += IDLE_SECONDS - 1
    h.on_speaker_activity()
    clock.t += IDLE_SECONDS - 1
    await h.tick()
    assert backend.ends == []


async def test_running_turn_delays_idle_end():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "what did we ship")
    h.on_data(None, "call.frame", packet({"type": "tool_start", "tool": "search", "seq": 1}))
    clock.t += IDLE_SECONDS + 5
    await h.tick()
    assert backend.ends == []
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 2}))
    clock.t += IDLE_SECONDS
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_resolved_turn_without_a_label_delays_idle_end():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "what did we ship")  # resolved, no tool_start yet
    clock.t += IDLE_SECONDS + 5
    await h.tick()
    assert backend.ends == []
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 1}))
    clock.t += IDLE_SECONDS
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_never_ends_while_lightbulb_is_speaking():
    h, room, backend, lives, clock, log = await summoned()
    h.on_agent_state("speaking")
    clock.t += IDLE_SECONDS + 30
    await h.tick()
    assert backend.ends == []
    h.on_agent_state("listening")  # stopping restarts the idle clock
    clock.t += IDLE_SECONDS - 1
    await h.tick()
    assert backend.ends == []
    clock.t += 1
    await h.tick()
    assert backend.ends == [("call-1", 42)]


async def test_another_person_saying_lightbulb_takes_the_floor():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_final_transcript("u2", "Aden", "Lightbulb, and what about billing?")
    await settle(h)
    assert backend.summons == ["u1"] and len(lives) == 1  # same call
    assert lives[0].calls[-3:] == [
        ("set_participant", "u2"),
        ("thinking", "Aden is talking to you", None),
        ("reply_to", "Lightbulb, and what about billing?"),
    ]
    await h.on_delegation("d1", "billing")
    assert backend.turns[-1]["speaker"] == "u2"


async def test_state_published_as_participant_attributes():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    await h.tick()
    assert room.attrs[-1] == {"lightbulb.status": "following", "lightbulb.speaker": "", "lightbulb.working": ""}
    await h.talk_start("u1")
    assert room.attrs[-1]["lightbulb.status"] == "listening" and room.attrs[-1]["lightbulb.speaker"] == "Prasiddha"
    h.on_agent_state("thinking")
    assert room.attrs[-1]["lightbulb.status"] == "thinking"
    h.on_agent_state("listening")
    await h.on_delegation("d1", "q")  # resolved, still running
    await h.tick()
    assert room.attrs[-1]["lightbulb.status"] == "thinking"
    h.on_data(None, "call.frame", packet({"type": "tool_start", "tool": "checking Stripe", "seq": 1}))
    await h.tick()
    assert room.attrs[-1]["lightbulb.status"] == "working"
    assert room.attrs[-1]["lightbulb.working"] == "Finance — checking Stripe"
    h.on_agent_state("speaking")
    assert room.attrs[-1]["lightbulb.status"] == "speaking"
    n = len(room.attrs)
    await h.tick()
    assert len(room.attrs) == n  # unchanged state is not re-published
    await h.end_exchange()
    assert room.attrs[-1]["lightbulb.status"] == "following"


async def test_usage_is_read_after_close():
    h, room, backend, lives, clock, log = await summoned()
    lives[0].usage = 10  # not final: the close adds the tail
    await h.end_exchange()
    assert backend.ends == [("call-1", 42)]


async def test_usage_falls_back_to_wall_clock():
    h, room, backend, lives, clock, log = await summoned()
    lives[0].final_usage = 0
    clock.t += IDLE_SECONDS
    await h.tick()
    assert backend.ends == [("call-1", IDLE_SECONDS)]


async def test_wall_clock_fallback_capped_at_deadline():
    h, room, backend, lives, clock, log = await summoned()
    lives[0].final_usage = 0
    clock.t = SUMMON["deadline"] + 500  # e.g. the loop stalled past the deadline
    await h.tick()
    assert backend.ends == [("call-1", SUMMON["deadline"] - 1_000)]


async def test_deadline_ends_exchange():
    h, room, backend, lives, clock, log = await summoned()
    clock.t = SUMMON["deadline"]
    h.on_speaker_activity()
    await h.tick()
    assert len(backend.ends) == 1


async def test_speaker_leaving_ends_exchange_others_stay():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_participants({"u2": "Aden"})
    assert [c[0] for c in lives[0].calls[-2:]] == ["mute", "close"]
    assert backend.ends == [("call-1", 42)] and not room.shut
    await h.talk_end("u1")  # talk state is cleared: nothing to nudge
    assert "nudge" not in [c[0] for c in lives[0].calls]
    await h.talk_start("u2")  # someone else can summon a fresh exchange
    assert backend.summons == ["u1", "u2"]


async def test_shutdown_happens_even_if_end_fails():
    h, room, backend, lives, clock, log = await summoned()

    async def boom(*a):
        raise RuntimeError("non-JSON 2xx")

    backend.presence = boom
    backend.end = boom
    await h.on_participants({})
    assert room.shut


async def test_last_human_leaving_ends_then_shuts_down():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_participants({})
    assert backend.presences[-1] == []
    assert backend.ends == [("call-1", 42)]
    assert log[-2:] == ["end", "shutdown"]


async def test_delegation_posts_turn_and_resolves():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "what did we ship")
    assert backend.turns == [
        {
            "callId": "call-1",
            "speaker": "u1",
            "seq": 1,
            "utterance": "what did we ship",
            "transcript": "Aden: we shipped huddles\nPrasiddha: hey Lightbulb what did we ship",
        }
    ]
    assert lives[0].calls[-1] == ("thinking", "Finance is on it. Not done yet.", "d1")


async def test_delegation_without_pending_transcript_uses_last_line():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "")
    assert backend.turns[0]["utterance"] == "Prasiddha: hey Lightbulb what did we ship"


async def test_turn_conflict_fails_silently_other_errors_apologise():
    h, room, backend, lives, clock, log = await summoned()
    backend.turn_result = BackendError(409, "turn_conflict", None)
    await h.on_delegation("d1", "x")
    assert not [c for c in lives[0].calls if c[0] == "commentary"]
    backend.turn_result = BackendError(503, "runtime_unavailable", None)
    await h.on_delegation("d2", "y")
    assert lives[0].calls[-1] == ("commentary", "Sorry, I couldn't reach the team for that.", "d2")


def packet(frame, call_id="call-1", turn_id="t1"):
    ev = {"type": "call.turn.frame", "payload": {"callId": call_id, "turnId": turn_id, "agentId": "fin", "frame": frame}}
    return json.dumps({"events": [ev]}).encode()


async def test_call_frame_routed_to_turns():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "q")
    h.on_data(None, "call.frame", packet({"type": "chunk", "content": "We shipped huddles.", "seq": 1}))
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 2}))
    assert lives[0].calls[-1] == ("commentary", "We shipped huddles.", "d1")


async def test_call_frame_from_a_participant_is_dropped():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "q")
    before = list(lives[0].calls)
    h.on_data(object(), "call.frame", packet({"type": "chunk", "content": "Forged.", "seq": 1}))
    h.on_data(object(), "call.frame", packet({"type": "run_ended", "seq": 2}))
    assert lives[0].calls == before
    assert h.dropped_frames == 2


async def test_call_frame_for_another_call_is_ignored():
    h, room, backend, lives, clock, log = await summoned()
    await h.on_delegation("d1", "q")
    before = list(lives[0].calls)
    h.on_data(None, "call.frame", packet({"type": "chunk", "content": "Other.", "seq": 1}, call_id="call-2"))
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 2}, call_id="call-2"))
    h.on_data(None, "call.frame", b"not json")
    assert lives[0].calls == before


async def test_talk_end_from_non_speaker_does_nothing():
    h, room, backend, lives, clock, log = await summoned()
    await h.talk_end("u2")
    assert "nudge" not in [c[0] for c in lives[0].calls]
    await h.talk_end("u1")
    assert lives[0].calls[-1] == ("nudge",)


async def test_other_peoples_lines_batched_as_thinking():
    h, room, backend, lives, clock, log = await summoned()
    n = len(lives[0].calls)
    await h.on_final_transcript("u2", "Aden", "and the bar")
    assert len(lives[0].calls) == n  # batched: < 3 s since the last thinking
    clock.t += 3
    await h.tick()
    assert lives[0].calls[-1] == ("thinking", "Aden: and the bar", None)
    await h.on_final_transcript("u1", "Prasiddha", "go on")  # the speaker is heard directly
    clock.t += 3
    await h.tick()
    assert lives[0].calls[-1] == ("thinking", "Aden: and the bar", None)


async def test_concurrent_talk_starts_summon_once():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    await asyncio.gather(h.talk_start("u1"), h.talk_start("u2"))
    assert backend.summons == ["u1"] and len(lives) == 1


async def test_last_human_leaving_during_a_summon_still_ends_the_call():
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    summon = asyncio.create_task(h.talk_start("u1"))
    await asyncio.sleep(0)  # the summon holds the lock
    await h.on_participants({})
    await summon
    assert backend.ends == [("call-1", 42)] and room.shut


async def test_backend_client_shapes_requests_and_errors(monkeypatch):
    import httpx

    from huddle_agent.backend import Backend

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("X-Lightbulb-Huddle-Secret"), json.loads(req.content)))
        if req.url.path.endswith("/summon"):
            return httpx.Response(402, json={"detail": {"code": "voice_limit", "reason": "credits_low"}})
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
    await b.presence(["u1"])
    assert seen[0] == (
        "/api/v1/internal/huddles/r1/summon",
        "s3cret",
        {"speakerUserId": "u1", "ownerId": "org_1", "roomToken": "tok"},
    )
    assert seen[1][0] == "/api/v1/internal/huddles/calls/c1/end" and seen[1][2]["usageSeconds"] == 7
    assert seen[2][2] == {"userIds": ["u1"], "ownerId": "org_1", "roomToken": "tok"}


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
            httpx.Response(400, json={"code": "INVALID_REQUEST", "message": "bad", "retryable": False}),
            httpx.Response(400, text="nope"),
            httpx.Response(401, json={"detail": "invalid huddle room token"}),
            httpx.Response(409, json={"detail": {"code": "turn_conflict"}}),
        ]
    )
    b = _backend(monkeypatch, lambda req: next(responses))
    codes = []
    for _ in range(4):
        try:
            await b.turn("c1", "u1", 1, "hi", "")
        except BackendError as e:
            codes.append((e.status, e.code))
    assert codes == [(400, "invalid_request"), (400, "invalid_request"), (401, "unauthorized"), (409, "turn_conflict")]


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
    gateway = iter([httpx.Response(503, text="deploying"), httpx.Response(502), httpx.Response(200, json={})])
    await _backend(monkeypatch, lambda req: (attempts.append(1), next(gateway))[1]).end("c1", 5)
    assert len(attempts) == 3  # a gateway 5xx during a backend deploy is retried too

    attempts.clear()
    try:
        await _backend(monkeypatch, lambda req: (attempts.append(1), httpx.Response(500))[1]).end("c1", 5)
        raise AssertionError("a 500 is not retried")
    except BackendError as e:
        assert e.status == 500
    assert len(attempts) == 1

    attempts.clear()
    b = _backend(monkeypatch, lambda req: (attempts.append(1), (_ for _ in ()).throw(httpx.ConnectError("down")))[1])
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
        self.track_publications = {"TR_a": _Pub("TR_a", "roomio_audio"), "TR_b": _Pub("TR_b", "other")}
        self.unpublished = []

    async def unpublish_track(self, sid):
        self.unpublished.append(sid)
        self.track_publications.pop(sid)


class _FakeRoom:
    def __init__(self):
        self.local_participant = _LocalParticipant()


async def test_start_live_disables_recording_pins_speaker_and_unpublishes_on_close(monkeypatch):
    import huddle_agent.agent as agent_mod

    started = {}

    class FakeSession:
        def __init__(self, **kw):
            self.room_io = types_ns(set_participant=lambda i: None)
            self.usage = types_ns(model_usage=[])
            self.closed = False

        def on(self, *a):
            pass

        async def start(self, **kw):
            started.update(kw)

        def generate_reply(self, **kw):
            pass

        async def aclose(self):
            self.closed = True

    class FakeAgent:
        def __init__(self, persona, huddle):
            self.duplex_session = types_ns(
                append_thinking=None, append_commentary=None, mute_input=None, unmute_input=None
            )

    monkeypatch.setattr(agent_mod, "AgentSession", FakeSession)
    monkeypatch.setattr(agent_mod, "GPTLiveModel", lambda **kw: kw)
    monkeypatch.setattr(agent_mod, "_CEO", FakeAgent)
    room = _FakeRoom()
    live = await agent_mod.start_live(room, None, None, "persona", "marin", "u1")
    assert started["record"] is False
    opts = started["room_options"]
    assert opts.participant_identity == "u1" and opts.close_on_disconnect is False
    await live.aclose()
    assert room.local_participant.unpublished == ["TR_a"]


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)
