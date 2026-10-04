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
