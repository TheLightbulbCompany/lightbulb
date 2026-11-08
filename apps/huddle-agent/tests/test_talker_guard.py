"""The talker's rules in code (mouth.Talker.transcription_node): an answer given without delegating is cut, a holding
line said and the person's line delegated; a result that adds a number or name is cut and said again word for word."""

import json

import pytest

from huddle_agent import mouth, owner


class _Speech:
    def __init__(self, sid):
        self.id, self.callbacks = sid, []

    def add_done_callback(self, cb):
        self.callbacks.append(cb)


class _Session:
    def __init__(self):
        self.current_speech, self.interrupts, self.replies = _Speech("sp_0"), 0, []

    def interrupt(self, force=False):
        self.interrupts += 1

    def generate_reply(self, instructions):
        self.replies.append(instructions)
        return _Speech(f"sp_{len(self.replies)}")


class _Backend:
    def __init__(self):
        self.sent = []

    async def owner(self, op, body):
        self.sent.append(body["text"])
        return {}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")


@pytest.fixture
def session(monkeypatch):
    s = _Session()
    monkeypatch.setattr(mouth.Talker, "session", property(lambda _self: s))
    return s


@pytest.fixture
def talker(session):
    backend = _Backend()
    a = owner.OwnerAgent(owner.Turns(backend, "user_maya", "job_1"), {"name": "Nova", "who": "Maya", "today": "Sunday"})
    a.backend = backend
    return a


async def _speak(agent, *deltas):
    async def stream():
        for d in deltas:
            yield d

    return "".join([d async for d in agent.transcription_node(stream(), None)])


async def test_an_answer_without_delegate_is_cut_held_and_delegated(talker, session, capsys, caplog):
    talker.heard("Maya", "How much did Acme pay us last month?")
    await _speak(talker, "Acme paid ", "you 4,200 dollars ", "on the third of March.")
    assert session.interrupts == 1 and session.replies == [mouth.HOLD]
    await talker._turns.flush()
    assert talker.backend.sent == ["How much did Acme pay us last month?"]
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["voice.talker.undelegated"] == 1 and line["where"] == "owner"
    assert "Acme" not in caplog.text and "4,200" not in caplog.text  # the warning carries no transcript


async def test_a_greeting_is_allowed_without_delegate(talker, session, capsys):
    talker.heard("Maya", "Hi Nova, are you there?")
    await _speak(talker, "Hi Maya, ", "I'm here. ", "What can I do for you?")
    assert session.interrupts == 0 and session.replies == [] and "undelegated" not in capsys.readouterr().out


async def test_a_line_after_delegate_is_not_cut(talker, session):
    talker.heard("Maya", "What shipped this week?")
    await talker.delegate("What shipped this week?")
    await _speak(talker, "Let me check with the team on what shipped this week, ", "one moment while they look.")
    assert session.interrupts == 0 and session.replies == []


async def test_a_cut_before_the_persons_line_lands_delegates_it_when_it_does(talker, session):
    await _speak(talker, "Your invoice from Acme ", "is 300 dollars.")
    assert session.interrupts == 1
    talker.heard("Maya", "What does the Acme invoice say?")
    await talker._turns.flush()
    assert talker.backend.sent == ["What does the Acme invoice say?"]


async def test_a_result_that_adds_a_fact_is_cut_and_said_word_for_word(talker, session, capsys):
    speech = talker.say(["We merged 5095 today."])
    session.current_speech = speech
    await _speak(talker, "We merged 5095 ", "today, and 5096 ", "ships Tuesday.")
    assert session.interrupts == 1 and session.replies[-1] == mouth.verbatim_instructions(["We merged 5095 today."])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["voice.talker.added_fact"] == 1
    session.current_speech = _Speech("sp_2")  # the word-for-word retelling is not checked again
    await _speak(talker, "We merged 5095 today. ", "Also 77.")
    assert session.interrupts == 1


async def test_a_faithful_result_is_not_cut(talker, session):
    session.current_speech = talker.say(["We merged 5095 today."])
    await _speak(talker, "Maya, we merged ", "5095 today.")
    assert session.interrupts == 0 and len(session.replies) == 1
