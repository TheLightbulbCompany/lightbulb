"""Owner mode (owner.py, mouth.py): ears and a mouth for the DM's Router. A spoken line is a message; the voice says
only a fixed holding line and the agent messages the room gets, each once; it has no model and no tools."""

import asyncio

import pytest
from livekit.agents import StopResponse
from livekit.agents.llm import ChatContext, ChatMessage

from huddle_agent import mouth, owner
from huddle_agent.backend import BackendError


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_API_URL", "http://backend.test")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s")
    monkeypatch.setenv("OPENAI_API_KEY", "k")


class _Session:
    def __init__(self):
        self.said = []

    def say(self, text, **_kw):
        self.said.append(text)


class _Backend:
    def __init__(self):
        self.sent = []

    async def owner(self, op, body):
        self.sent.append((op, body))
        return {}


def _agent(monkeypatch, backend):
    s = _Session()
    monkeypatch.setattr(owner.OwnerAgent, "session", property(lambda _self: s))
    return owner.OwnerAgent(owner.Turns(backend, "user_maya", "job_1")), s


async def _speak_line(agent, text):
    with pytest.raises(StopResponse):  # LiveKit makes no reply of its own to the line
        await agent.on_user_turn_completed(ChatContext(), ChatMessage(role="user", content=[text]))
    await agent._turns.flush()


def test_the_voice_has_no_model_and_no_tools():
    s = owner.voice_session()
    assert s.llm is None and s.stt is not None and s.tts is not None
    assert owner.OwnerAgent(owner.Turns(None, "u", "j")).tools == []
    assert s.stt._opts.turn_detection.type == "semantic_vad" and s.stt._opts.turn_detection.eagerness == "low"
    assert owner.OWNER_MAX_S == 55 * 60


async def test_a_spoken_line_is_a_message_and_the_voice_only_holds(monkeypatch):
    b = _Backend()
    a, s = _agent(monkeypatch, b)
    await _speak_line(a, "What have we shipped in the last week?")
    assert b.sent == [("turn", {"speakerUserId": "user_maya", "sessionId": "job_1", "n": 0,
                                "text": "What have we shipped in the last week?"})]  # fmt: skip
    assert s.said == [owner.HOLD]  # its own words are never an answer


async def test_a_fragment_is_only_a_message(monkeypatch):
    b = _Backend()
    a, s = _agent(monkeypatch, b)
    await _speak_line(a, "Is")
    assert [op for op, _ in b.sent] == ["turn"]  # no approve, no other route exists
    assert s.said == [owner.HOLD]


async def test_spoken_lines_post_in_order_and_a_failure_does_not_stop_the_next():
    sent = []

    class B:
        async def owner(self, op, body):
            await asyncio.sleep(0.01 if body["n"] == 0 else 0)  # the first post is the slow one
            sent.append(body)
            if body["n"] == 1:
                raise BackendError(500, "x", None)
            return {}

    t = owner.Turns(B(), "user_maya", "job_1")
    for text in ("Draft the post", "On second thought", "Thanks"):
        t.add(text)
    await t.flush()
    assert [(b["n"], b["text"]) for b in sent] == [(0, "Draft the post"), (1, "On second thought"), (2, "Thanks")]


async def test_an_agent_message_is_said_once_and_never_over_the_member():
    polls = [
        {"messages": [{"id": "m1", "text": "Checking GitHub."}], "after": 5},
        {"messages": [{"id": "m1", "text": "Checking GitHub."}, {"id": "m2", "text": "We shipped #5095."}], "after": 7},
    ]
    cursors = []

    async def fetch(after):
        cursors.append(after)
        return polls.pop(0)

    m, said = mouth.Mouth(fetch, 4), []
    await m.poll()
    assert m.speak(said.append, ready=False) == 0 and said == []  # the member is talking: wait
    assert m.speak(said.append, ready=True) == 1
    await m.poll()  # m1 again, and a new one
    m.speak(said.append, ready=True)
    assert said == ["Checking GitHub.", "We shipped 5095."]
    assert cursors == [4, 5]


def test_a_long_message_is_condensed_for_speech():
    assert mouth.speakable("**Shipped** [#5095](https://github.com/x/pull/5095): tap to talk.") == "Shipped 5095: tap to talk."
    assert mouth.speakable("- one\n- two") == "one two"
    long = "First thing shipped. " * 30
    out = mouth.speakable(long)
    assert len(out) <= mouth.SPOKEN_CHARS + 30 and out.endswith("The rest is in the chat.")
    assert out.startswith("First thing shipped. First thing shipped.")


async def test_quiet_ends_the_talk_only_while_the_mic_is_live():
    ended = []
    q = owner.Quiet(lambda: ended.append("quiet"), lambda: ended.append("away"), quiet_s=0.02, away_s=0.05)
    q.update(mic_live=False, user="listening", agent="listening")  # pre-connected, no mic: only the long wait
    await asyncio.sleep(0.03)
    assert ended == []
    q.update(mic_live=True, user="listening", agent="speaking")  # tapped; the voice is talking: no count
    await asyncio.sleep(0.03)
    assert ended == []
    q.update(mic_live=True, user="listening", agent="listening")  # it finished speaking
    q.update(mic_live=True, user="listening", agent="listening")  # a repeat does not restart the count
    await asyncio.sleep(0.01)
    q.update(mic_live=True, user="speaking", agent="listening")  # the member talks: reset
    q.update(mic_live=True, user="listening", agent="listening")
    await asyncio.sleep(0.015)
    assert ended == []
    await asyncio.sleep(0.02)
    assert ended == ["quiet"]
    q.update(mic_live=False, user="listening", agent="listening")  # the web closed the mic: the long wait
    await asyncio.sleep(0.07)
    assert ended == ["quiet", "away"]
    q.cancel()


def test_the_talk_is_kept_with_its_transcript():
    items = [{"role": "assistant", "text": "We shipped 5095.", "t": 2.0, "interrupted": False},
             {"role": "user", "text": "What shipped?", "t": 0.5, "interrupted": False}]  # fmt: skip
    assert owner.talk_record("user_maya", items, "job_1", 42.7) == {
        "speakerUserId": "user_maya", "sessionId": "job_1", "durationSec": 42,
        "transcript": [{"who": "caller", "text": "What shipped?", "t": 0.5},
                       {"who": "assistant", "text": "We shipped 5095.", "t": 2.0}],
    }  # fmt: skip
