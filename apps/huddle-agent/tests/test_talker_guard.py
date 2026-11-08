"""The talker's rules in code (mouth.Talker): code decides every line, so a substantive line never yields model speech
other than the fixed holding line (8 Nov prod Meet re-run: a refusal, a made-up draft and 20 s of advice were spoken
before the streamed-words guard cut them); small talk gets a short reply, tools off. The guard stays as a backstop: a
result that adds a number or name, or turns into a refusal, is cut and said word for word."""

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


SUBSTANTIVE = [
    "What have we shipped in the last week?",  # 4b: "I don't have access to a list... ask the team"
    "Nova, draft a holiday-hours post and put the link in chat.",  # 4c: a made-up draft
    "We need more gel colours, can you order them?",  # 4d: 20 s of advice
    "Is",
    "Did Acme pay?",
]


@pytest.mark.parametrize("said", SUBSTANTIVE)
async def test_a_substantive_line_is_posted_and_only_the_holding_line_is_said(talker, session, said):
    assert talker.line("Maya", said) == "asked"
    await talker._turns.flush()
    assert talker.backend.sent == [said]
    assert session.replies == [mouth.HOLD] and session.kw == [{"tool_choice": "none"}]  # the model says nothing else
    assert session.made[-1].id in talker.free  # the fixed line is never cut


@pytest.mark.parametrize("said", ["Hi Nova, are you there?", "Thanks!", "Thank you, Nova.", "Can you hear me?", "Bye"])
async def test_small_talk_gets_a_short_reply_with_tools_off_and_is_not_posted(talker, session, said):
    assert talker.line("Maya", said) == "chat"
    await talker._turns.flush()
    assert talker.backend.sent == [] and session.replies == [mouth.SMALL_TALK_RULE]
    assert session.kw == [{"tool_choice": "none"}]


@pytest.mark.parametrize("said", ["Yes.", "No", "Okay", "Sure, please."])
async def test_a_yes_or_a_no_is_never_small_talk(talker, session, said):
    """It answers what the team last asked ("Shall I book 3 pm?"): the Router gets it."""
    assert talker.line("Maya", said) == "asked" and session.replies == [mouth.HOLD]


async def test_the_context_goes_with_the_next_ask(talker, session):
    talker.line("Maya", "Hi Nova")
    talker.context.add("Nova", "Hi Maya!")
    talker.line("Maya", "What shipped this week?")
    await talker._turns.flush()
    assert talker.backend.sent == ["What shipped this week?"]


async def test_a_small_talk_reply_that_turns_into_an_answer_is_cut(talker, session, capsys, caplog):
    talker.line("Maya", "Thanks!")
    session.current_speech = session.made[-1]
    await _speak(talker, "You're welcome! ", "Acme paid you 4,200 dollars.")
    assert session.current_speech.interrupted == 1 and session.replies == [mouth.SMALL_TALK_RULE]  # nothing more said
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["voice.talker.undelegated"] == 1 and line["where"] == "owner"
    assert "Acme" not in caplog.text and "4,200" not in caplog.text  # the warning carries no transcript


@pytest.mark.parametrize("said", ["Hi Maya, I'm here.", "Thanks! Bye.", "Yes, I can hear you.", "You're welcome!"])
async def test_a_small_talk_reply_is_not_cut(talker, session, said):
    talker.line("Maya", "Hi Nova, are you there?")
    session.current_speech = session.made[-1]
    await _speak(talker, said)
    assert session.current_speech.interrupted == 0


async def test_the_goodbye_is_never_checked(talker, session):
    talker.unchecked(session.current_speech)
    await _speak(talker, "We're out of time, Priya, it was great talking about the 3 invoices.")
    assert session.current_speech.interrupted == 0 and session.replies == []


async def test_sorry_i_didnt_catch_that_is_not_cut(talker, session):
    await _speak(talker, "Sorry, I didn't catch that. Could you say it again, please?")
    assert session.current_speech.interrupted == 0


async def test_the_owner_session_has_no_separate_vad():
    assert owner.talk_session().vad is None


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


async def test_an_ack_said_as_a_refusal_is_cut_and_said_word_for_word(talker, session, capsys):
    """8 Nov prod phone re-run: "Customer Operations will check your booking and move it to Sunday." was said as "I
    can't change that booking directly from this call. Please contact our front desk or support team"."""
    ack = "Customer Operations will check your booking and move it to Sunday."
    session.current_speech = talker.say([ack])
    await _speak(talker, "I can't change that booking directly from this call. ", "Please contact our front desk.")
    assert session.current_speech.interrupted == 1
    assert session.replies[-1] == mouth.verbatim_instructions([ack])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["voice.talker.refused"] == 1
    session.current_speech = talker.say([ack])  # said faithfully: not cut
    await _speak(talker, "Customer Operations will move your booking to Sunday.")
    assert session.current_speech.interrupted == 0
    session.current_speech = talker.say(["We can't open on Sundays."])  # a refusal the message makes is said
    await _speak(talker, "We can't open on Sundays.")
    assert session.current_speech.interrupted == 0
    assert "never turn it into a refusal" in mouth.RESULT_RULE


async def test_a_result_is_said_with_no_tools(talker, session):
    talker.say(["We shipped phone reception."])
    talker.say(["We shipped phone reception."], verbatim=True)
    assert [kw.get("tool_choice") for kw in session.kw] == ["none", "none"]
    assert "Say only this message" in session.replies[0]


@pytest.mark.parametrize("said", ["Let me check.", "On it.", "One sec, let me check."])
async def test_a_holding_line_in_place_of_a_result_is_cut_and_said_word_for_word(talker, session, capsys, said):
    session.current_speech = talker.say(["In the last week we shipped phone reception."])
    await _speak(talker, said)
    assert session.current_speech.interrupted == 1
    assert session.replies[-1] == mouth.verbatim_instructions(["In the last week we shipped phone reception."])
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])  # "On " is cut as an added name first
    assert line.get("voice.talker.unsaid_result", line.get("voice.talker.added_fact")) == 1


async def test_a_result_that_says_on_it_itself_is_not_cut(talker, session):
    session.current_speech = talker.say(["Pulse is on it."])
    await _speak(talker, "Pulse is on it.")
    assert session.current_speech.interrupted == 0


# --- the phone talker (phone.Receptionist) runs under the same guard ----------------------------------------
class _PhoneBackend:
    call_id = "call-1"

    def __init__(self):
        self.calls = []

    async def post(self, path, body=None):
        self.calls.append((path, body or {}))
        return {"taken": True} if path == "message" else {}


@pytest.fixture
def receptionist(session, monkeypatch):
    monkeypatch.setenv("LIGHTBULB_API_URL", "http://x")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "x")
    from huddle_agent import phone

    info = {"assistantName": "Nova", "businessName": "Glow Nails", "today": "Sunday", "greeting": "Hi",
            "callerName": "Priya", "mode": "live"}  # fmt: skip
    return phone.Receptionist(_PhoneBackend(), info, None)


async def test_a_phone_question_is_posted_and_only_the_holding_line_is_said(receptionist, session):
    assert receptionist.line("Caller", "How much is a pedicure?") == "asked"
    await receptionist.turns.flush()
    assert [(p, b["text"]) for p, b in receptionist._b.calls] == [("turn", "How much is a pedicure?")]
    assert session.replies == [mouth.HOLD]


async def test_the_phone_greeting_and_holding_lines_are_never_cut(receptionist, session):
    greeting = _Speech("sp_greet")
    session.current_speech = greeting
    receptionist.unchecked(greeting)  # phone.entrypoint marks the opening, hold lines and goodbyes so
    await _speak(receptionist, "Hi, I'm Nova, Glow Nails's AI assistant, and this call is recorded.")
    assert greeting.interrupted == 0 and session.replies == []


async def test_a_phone_result_that_adds_a_price_is_said_word_for_word(receptionist, session):
    speech = receptionist.say(["We're closed on Sundays."])
    session.current_speech = speech
    await _speak(receptionist, "We're closed on Sundays, ", "and a pedicure is 45 dollars.")
    assert speech.interrupted == 1 and mouth.VERBATIM_RULE in session.replies[-1]


