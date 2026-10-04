from livekit.agents import APIConnectionError, APIError

from huddle_agent import agent
from huddle_agent.agent import SEMANTIC_VAD, SERVER_VAD, retry_without_semantic_vad


class Engine:
    def __init__(self):
        self.updates = []

    def update_options(self, **kw):
        self.updates.append(kw)


def test_semantic_vad_is_what_the_pinned_plugin_validates():
    from livekit.plugins.openai.stt import _as_turn_detection

    assert _as_turn_detection(SEMANTIC_VAD).type == "semantic_vad"
    assert _as_turn_detection(SERVER_VAD).silence_duration_ms == 700


def test_a_refused_semantic_vad_falls_back_to_server_vad_once(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "emit_metric", lambda name, *a, **k: seen.append(name))
    engine, state = Engine(), {"vad": "semantic"}
    refused = APIError("OpenAI Realtime STT error: invalid turn_detection", body={}, retryable=False)
    assert retry_without_semantic_vad(engine, refused, "semantic", state)
    assert state == {"vad": "server"} and engine.updates == [{"turn_detection": SERVER_VAD}]
    assert retry_without_semantic_vad(engine, refused, "semantic", state)  # a second stream refused too: just reopen
    assert engine.updates == [{"turn_detection": SERVER_VAD}] and seen == ["live.stt.semantic_vad_rejected"]
    assert not retry_without_semantic_vad(engine, refused, "server", state)  # server_vad failing is a real error


def test_connection_errors_are_not_a_vad_refusal():
    engine, state = Engine(), {"vad": "semantic"}
    assert not retry_without_semantic_vad(engine, APIConnectionError("down"), "semantic", state)
    assert not retry_without_semantic_vad(engine, RuntimeError("boom"), "semantic", state)
    assert state == {"vad": "semantic"} and engine.updates == []


def test_a_muted_voice_is_muted_again_after_a_reconnect():
    # a reconnect is a new GPT-Live session, which hears: a meeting voice must not start hearing the Meet
    from unittest.mock import MagicMock

    duplex, handlers = MagicMock(), {}
    duplex.on.side_effect = lambda event, cb: handlers.__setitem__(event, cb)
    live = agent._Live("forge", MagicMock(), duplex, MagicMock())
    handlers["session_reconnected"](None)
    assert duplex.mute_input.call_count == 0, "a Live voice hears its speaker: never muted by a reconnect"
    live.mute()
    handlers["session_reconnected"](None)
    assert duplex.mute_input.call_count == 2


async def test_a_meeting_voice_is_muted_as_its_session_starts():
    # review: the huddle's mute came after session.start returned; the Meet could reach the model while it opened
    from unittest.mock import MagicMock

    session = MagicMock(spec=agent.GPTLiveSession)
    for muted, calls in ((True, 1), (False, 0)):
        session.reset_mock()
        voice = agent._Voice("forge", "persona", MagicMock(), [], muted=muted)
        type(voice).duplex_session = property(lambda self: session)
        try:
            await voice.on_enter()
        finally:
            del type(voice).duplex_session
        assert session.mute_input.call_count == calls


def test_the_meet_bots_page_is_never_a_person():
    from types import SimpleNamespace

    from livekit import rtc

    standard, ingress = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, rtc.ParticipantKind.PARTICIPANT_KIND_INGRESS
    assert not agent.person(SimpleNamespace(identity="meetbot:room-1", kind=standard))
    for identity in ("meet:room-1", "meet:room-1:p1"):  # a mixed Meet, and one person of it
        assert agent.person(SimpleNamespace(identity=identity, kind=ingress))
    assert agent.person(SimpleNamespace(identity="u1", kind=standard))
    assert not agent.person(SimpleNamespace(identity="agent-x", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
