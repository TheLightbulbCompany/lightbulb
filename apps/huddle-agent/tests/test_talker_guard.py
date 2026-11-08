"""The talker's rules in code (mouth.Talker.transcription_node): an answer given without delegating is cut, a holding
line said and the person's line delegated, once; a result that adds a number or name is cut and said word for word."""

import json

import pytest

from huddle_agent import mouth, owner


class _Speech:
    def __init__(self, sid):
        self.id, self.interrupted = sid, 0

    def add_done_callback(self, cb):
        pass

    def interrupt(self, force=False):
        self.interrupted += 1


class _Session:
    def __init__(self):
        self.current_speech, self.replies, self.made, self.kw = _Speech("sp_0"), [], [], []

    def generate_reply(self, instructions, **kw):
        self.replies.append(instructions)
        self.kw.append(kw)
        self.made.append(_Speech(f"sp_{len(self.replies)}"))
        return self.made[-1]


class _Backend:
    def __init__(self):
        self.sent = []

    async def owner(self, op, body):
        self.sent.append(body["text"].split("\n\n")[0])
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
    first = session.current_speech
    await _speak(talker, "Acme paid ", "you 4,200 dollars ", "on the third of March.")
    assert first.interrupted == 1 and session.replies == [mouth.HOLD]
    await talker._turns.flush()
    assert talker.backend.sent == ["How much did Acme pay us last month?"]
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["voice.talker.undelegated"] == 1 and line["where"] == "owner"
    assert "Acme" not in caplog.text and "4,200" not in caplog.text  # the warning carries no transcript


@pytest.mark.parametrize("said", ["Yes, it's paid.", "It shipped.", "Acme owes you."])
async def test_a_short_answer_is_still_an_answer(talker, session, said):
    talker.heard("Maya", "Did the invoice go out?")
    await _speak(talker, said)
    assert session.current_speech.interrupted == 1


@pytest.mark.parametrize("said", ["Hi Maya, I'm here.", "Thanks! Bye.", "Yes, I can hear you."])
async def test_a_greeting_is_allowed_without_delegate(talker, session, capsys, said):
    talker.heard("Maya", "Hi Nova, are you there?")
    await _speak(talker, said)
    assert session.current_speech.interrupted == 0 and session.replies == []
    assert "undelegated" not in capsys.readouterr().out


async def test_a_line_after_delegate_is_not_cut(talker, session):
    talker.heard("Maya", "What shipped this week?")
    await talker.delegate("What shipped this week?")
    await _speak(talker, "Let me check with the team on what shipped this week, ", "one moment while they look.")
    assert session.current_speech.interrupted == 0 and session.replies == []


async def test_a_late_transcript_after_delegate_is_not_delegated_again(talker, session):
    await talker.delegate("What shipped this week?")  # the model called delegate before the transcript landed
    talker.heard("Maya", "What shipped this week?")
    await _speak(talker, "Let me check with the team on what shipped this week, ", "one moment while they look.")
    await talker._turns.flush()
    assert session.current_speech.interrupted == 0 and talker.backend.sent == ["What shipped this week?"]


async def test_a_tool_call_after_the_cut_posts_nothing(talker, session):
    talker.heard("Maya", "Did Acme pay?")
    await _speak(talker, "Yes, Acme paid.")
    assert await talker.delegate("Did Acme pay?") == {"sent": True}
    await talker._turns.flush()
    assert talker.backend.sent == ["Did Acme pay?"]


async def test_a_cut_before_the_persons_line_lands_delegates_it_when_it_does(talker, session):
    await _speak(talker, "Your invoice from Acme ", "is 300 dollars.")
    assert session.current_speech.interrupted == 1
    await talker.delegate("What does the Acme invoice say?")  # the model's own call, after the cut
    talker.heard("Maya", "What does the Acme invoice say?")
    await talker._turns.flush()
    assert talker.backend.sent == ["What does the Acme invoice say?"]


async def test_the_holding_line_is_said_once(talker, session):
    talker.heard("Maya", "How much did Acme pay?")
    await _speak(talker, "Let me check. ", "Acme paid 300.")
    assert session.current_speech.interrupted == 1 and session.replies == []  # it already said one


async def test_the_goodbye_is_never_checked(talker, session):
    talker.unchecked(session.current_speech)
    await _speak(talker, "We're out of time, Priya, it was great talking about the 3 invoices.")
    assert session.current_speech.interrupted == 0 and session.replies == []


async def test_a_result_that_adds_a_fact_is_cut_and_said_word_for_word(talker, session, capsys):
    session.current_speech = talker.say(["We merged 5095 today."])
    await _speak(talker, "We merged 5095 ", "today, and 5096 ", "ships Tuesday.")
    assert session.current_speech.interrupted == 1
    assert session.replies[-1] == mouth.verbatim_instructions(["We merged 5095 today."])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["voice.talker.added_fact"] == 1
    session.current_speech = session.made[-1]  # the word-for-word retelling is not checked again
    await _speak(talker, "We merged 5095 today. ", "Also 77.")
    assert session.current_speech.interrupted == 0


async def test_a_swapped_name_in_a_result_is_cut(talker, session):
    session.current_speech = talker.say(["Priya merged 5095."])
    await _speak(talker, "Sam merged 5095.")
    assert session.current_speech.interrupted == 1


async def test_a_faithful_result_is_not_cut(talker, session):
    session.current_speech = talker.say(["We merged 5095 today."])
    await _speak(talker, "Maya, we merged ", "5095 today.")
    assert session.current_speech.interrupted == 0 and len(session.replies) == 1


async def test_a_new_turn_that_answers_without_delegate_is_cut_after_a_delegated_turn(talker, session):
    talker.turn_ended()  # turn 1
    await talker.delegate("What shipped this week?")
    talker.heard("Maya", "What shipped this week?")
    talker.turn_ended()  # turn 2's speech ends, then its transcript lands
    talker.heard("Maya", "Did Acme pay?")
    await _speak(talker, "Yes, Acme paid on Monday.")
    assert session.current_speech.interrupted == 1
    await talker.delegate("Did Acme pay?")  # the model's own late call posts nothing
    await talker._turns.flush()
    assert talker.backend.sent == ["What shipped this week?", "Did Acme pay?"]


async def test_a_late_transcript_while_idle_is_not_delegated_by_the_next_cut(talker, session):
    talker.turn_ended()  # turn N, delegated before its transcript
    await talker.delegate("What shipped this week?")
    talker.heard("Maya", "What shipped this week?")  # lands late, nothing playing
    talker.turn_ended()  # turn N+1: the reply is cut before its transcript lands
    await _speak(talker, "Acme paid 300 dollars.")
    assert session.current_speech.interrupted == 1
    talker.heard("Maya", "Did Acme pay?")
    await talker._turns.flush()
    assert talker.backend.sent == ["What shipped this week?", "Did Acme pay?"]


async def test_a_pending_cut_does_not_post_the_next_line_twice(talker, session):
    talker.turn_ended()  # a noise turn: the reply is cut before any transcript lands
    await _speak(talker, "Acme paid 300 dollars.")
    assert session.replies == [mouth.HOLD]
    talker.turn_ended()  # turn B, delegated by the model, then its transcript lands
    await talker.delegate("Did Acme pay?")
    talker.heard("Maya", "Did Acme pay?")
    await talker._turns.flush()
    assert talker.backend.sent == ["Did Acme pay?"]


async def test_sorry_i_didnt_catch_that_is_not_cut(talker, session):
    talker.heard("Maya", "Mmhm")
    await _speak(talker, "Sorry, I didn't catch that. Could you say it again, please?")
    assert session.current_speech.interrupted == 0


async def test_the_owner_session_has_no_separate_vad():
    assert owner.talk_session().vad is None  # the turn order Talker.turn_ended relies on


async def test_a_result_and_the_holding_line_are_said_with_no_tools(talker, session):
    """8 Nov prod Meet: with `delegate` on during the result turn, the talker said "On it." / "Let me check." in place
    of the answer and once called delegate with the person's last line again (a duplicate ask). livekit-agents
    1.8.11 sends tool_choice per response on OpenAI realtime (per_response_tool_choice)."""
    talker.say(["We shipped phone reception."])
    talker.heard("Maya", "Did Acme pay?")
    session.current_speech = _Speech("sp_x")
    await _speak(talker, "Yes, Acme paid.")  # undelegated: cut, then the holding line
    assert session.replies[-1] == mouth.HOLD
    assert [kw.get("tool_choice") for kw in session.kw] == ["none", "none"]
