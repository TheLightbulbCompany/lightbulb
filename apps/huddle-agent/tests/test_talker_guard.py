"""The talker's rules in code (mouth.Talker): code decides every line, so a substantive line never yields model speech
other than the fixed holding line (8 Nov prod Meet re-run: a refusal, a made-up draft and 20 s of advice were spoken
before the streamed-words guard cut them); small talk gets a short reply, tools off. The guard stays as a backstop: a
result that adds a number or name, or turns into a refusal, is cut and said word for word."""

import json

import pytest

from huddle_agent import mouth, owner


class _Speech:
    def __init__(self, sid):
        self.id, self.interrupted, self.callbacks = sid, 0, []

    def add_done_callback(self, cb):
        self.callbacks.append(cb)

    def done(self):
        for cb in self.callbacks:
            cb(self)

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
    assert session.replies == [mouth.HOLD] and session.kw == [{"user_input": mouth.line_input(mouth.HOLD), "tool_choice": "none"}]  # the model says nothing else
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
    session.current_speech = session.made[-1]  # the word-for-word retelling is checked, and never retried again
    await _speak(talker, "We merged 5095 today. ", "Also 77.")
    assert session.current_speech.interrupted == 1 and len(session.replies) == 2


async def test_a_swapped_name_in_a_result_is_cut(talker, session):
    session.current_speech = talker.say(["Priya merged 5095."])
    await _speak(talker, "Sam merged 5095.")
    assert session.current_speech.interrupted == 1


async def test_a_faithful_result_is_not_cut(talker, session):
    session.current_speech = talker.say(["We merged 5095 today."])
    await _speak(talker, "Maya, we merged ", "5095 today.")
    assert session.current_speech.interrupted == 0 and len(session.replies) == 1


SLOTS = "Yes, Taylor, we have Saturday afternoon availability at 1:00, 3:00, or 5:00 PM. Which time works best?"


@pytest.mark.parametrize("said", [
    # 8 Nov prod runs: each was cut as an added fact before a word played, and its word-for-word retry too, so the
    # caller never heard the times (book-112005: the 1/3/5 PM slots and the read-back, four cuts)
    "Got it. We have Saturday afternoon at 1, 3 or 5 PM.",
    "Noted, thanks. We have 1:00, 3:00 or 5:00 PM on Saturday.",
    "Great news, Taylor! We have Saturday afternoon at 1:00, 3:00, or 5:00 PM. Which works best?",
    "Thank you for holding. We have Saturday at 1:00, 3:00 or 5:00 PM.",
    "Perfect. Saturday afternoon has 1:00, 3:00, or 5:00 PM. Please pick one.",
    "Your options Saturday afternoon are 1:00, 3:00, or 5:00 PM.",
    "Hello again, TaylorTest. Saturday afternoon is open at 1:00, 3:00, or 5:00 PM.",  # the name, joined
])  # fmt: skip
def test_a_sentences_first_word_is_grammar_not_a_fact(said):
    assert mouth.added_facts(said, SLOTS, ("Nova", "Glow Nails", "Taylor Test")) == []


def test_a_new_name_or_time_at_a_sentences_start_is_still_a_fact():
    assert mouth.added_facts("Sam merged 5095.", "Priya merged 5095.") == ["Sam"]
    assert mouth.added_facts("Got it. 12:00 PM works.", SLOTS) == ["12:00"]
    assert mouth.added_facts("Vera found 1:00, 3:00 or 5:00 PM.", SLOTS) == ["Vera"]


async def test_a_booking_the_message_never_made_is_cut(talker, session, capsys):
    """8 Nov prod final smoke: "Vera is booking… 12:00 PM" was said and nothing was booked. Only a message that
    says it is booked (the backend passes one only once the call has a booking) may be said as booked."""
    ask = "Just to confirm: Saturday at 1:00 PM. Should I book it?"
    session.current_speech = talker.say([ask])
    await _speak(talker, "You're booked for Saturday at 1:00 PM.")
    assert session.current_speech.interrupted == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["voice.talker.claimed"] == 1
    session.current_speech = talker.say(["You're booked for Saturday at 1:00 PM."])
    await _speak(talker, "You're all set: booked for Saturday at 1:00 PM.")
    assert session.current_speech.interrupted == 0


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


# --- follow-ups to #5115 -------------------------------------------------------------------------------------
@pytest.mark.parametrize("said", ["can you check it again", "could you check on it", "Can you help?", "Let me see it",
                                  "Say it in Spanish"])  # fmt: skip
async def test_a_request_built_from_friendly_words_is_delegated(talker, session, said):
    assert talker.line("Maya", said) == "asked"
    await talker._turns.flush()
    assert talker.backend.sent == [said] and session.replies == [mouth.HOLD]


@pytest.mark.parametrize("said", ["Let me check.", "One sec, let me check on it.", "Sorry, I didn't catch that."])
async def test_the_assistants_own_holding_words_are_still_small_talk(talker, session, said):
    talker.line("Maya", "Hi Nova")
    session.current_speech = session.made[-1]
    await _speak(talker, said)
    assert session.current_speech.interrupted == 0


@pytest.mark.parametrize("said", ["Say that again?", "Can you repeat that?", "What was that?", "Sorry, come again?"])
async def test_a_repeat_request_says_the_last_result_again_word_for_word(talker, session, said):
    talker.say(["We shipped phone reception in 4966."])
    assert talker.line("Maya", said) == "repeat"
    await talker._turns.flush()
    assert talker.backend.sent == []  # never posted to the Router
    assert session.replies[-1] == mouth.verbatim_instructions(["We shipped phone reception in 4966."])
    assert session.kw[-1] == {"tool_choice": "none", "user_input": f"{mouth.TEAM}\nWe shipped phone reception in 4966."}


async def test_a_repeat_request_with_nothing_said_yet_is_small_talk(talker, session):
    assert talker.line("Maya", "Can you repeat that?") == "chat"
    assert session.replies == [mouth.SMALL_TALK_RULE] and talker.backend.sent == []


async def test_the_holding_line_is_said_once_per_turn(talker, session):
    """8 Nov prod Meet re-run #3: "Let me check. Let me check." in one turn. Once until a result is said."""
    talker.line("Maya", "What shipped this week?")
    talker.line("Maya", "And did Acme pay?")
    assert session.replies == [mouth.HOLD]
    talker.say(["We shipped phone reception."])
    talker.line("Maya", "What about Friday?")
    assert session.replies.count(mouth.HOLD) == 2 and session.replies[-1] == mouth.HOLD


async def test_a_cut_result_is_said_again_at_most_once(talker, session, capsys):
    """8 Nov prod Meet re-run #3: the card line was replaced by a stale question ("Let me check. Pulse asks...").
    The word-for-word retry is checked too, and cut without a third try."""
    card = "Vera needs your OK to buy from Beyond Polish for 215.90 dollars."
    session.current_speech = talker.say([card])
    await _speak(talker, "Let me check. Pulse asks what exact holiday hours...")
    assert session.replies[-1] == mouth.verbatim_instructions([card])
    session.current_speech = session.made[-1]
    await _speak(talker, "Let me check. Pulse asks what exact holiday hours...")
    assert session.current_speech.interrupted == 1 and len(session.replies) == 2  # cut, never said a third time
    session.current_speech = talker.say([card], verbatim=True)
    await _speak(talker, card)  # a faithful word-for-word line is never cut
    assert session.current_speech.interrupted == 0


async def test_queued_messages_are_said_one_at_a_time_in_order_and_a_question_once():
    """8 Nov prod Meet re-run #3: Pulse's question was said about 5 times and crowded out Vera's question and the
    card line. Each message is its own speech, oldest first; a Worker question (by qid) is said once."""
    pulse = {"id": "q1", "seq": 8, "text": "Pulse asks: what holiday hours?", "question": {"taskId": "t1", "qid": "q-1"}}
    polls = [
        {"messages": [pulse, {"id": "q2", "seq": 9, "text": "Vera asks: which brand?",
                              "question": {"taskId": "t2", "qid": "q-2"}}], "after": 9},
        {"messages": [{**pulse, "id": "q1-again", "seq": 10}, {"id": "c1", "seq": 11, "text": "Vera needs your OK."}],
         "after": 11},
    ]  # fmt: skip

    async def fetch(after, handled):
        return polls.pop(0) if polls else {"messages": [], "after": after}

    m, said = mouth.Mouth(fetch, 7), []
    await m.poll()
    assert m.speak(said.append, ready=True, live=True) == 1 and m.question["qid"] == "q-1"
    await m.poll()
    while m.speak(said.append, ready=True, live=True):
        await m.poll()
    assert said == [["Pulse asks: what holiday hours?"], ["Vera asks: which brand?"], ["Vera needs your OK."]]
    assert m.question["qid"] == "q-2" and m.handled == 11


async def test_a_dropped_result_never_silences_the_next_holding_line(talker, session):
    """Review of #5119: `held` reset only on a result, so a result that never came silenced every later hold. The
    turn ends when the holding line has played: the next ask says it again."""
    talker.line("Maya", "What shipped this week?")
    session.made[-1].done()  # the holding line played out; no result ever came
    talker.line("Maya", "And did Acme pay?")
    assert session.replies == [mouth.HOLD, mouth.HOLD]


#: The Front desk thread's lines as posted in the call rooms (8 Nov prod booking smoke after #5122).
FRONT_DESK = [
    "Just to confirm, Saturday, November 14 at 12:00 PM for the 45-minute founding team setup—is that right?",
    "Monday, November 16 has openings at 1:00, 3:00, and 5:00 PM. Which time would you prefer?",
    "You’re booked for the founding team setup on Saturday, November 14 at 12:00 PM.",
]


@pytest.mark.parametrize("text", FRONT_DESK)
def test_a_team_message_is_the_conversations_newest_line(talker, session, text):
    """Handed only in the response's instructions, the realtime model answered the caller's last line instead ("OK.
    Great. I'll pass that along to the team." for "You're booked", "Got it… nothing is finalized yet" for the
    read-back) or was cut as a refusal twice (Monday's slots). The message is the newest conversation item."""
    talker.say([text])
    said = session.kw[-1]["user_input"]
    assert said.startswith(mouth.TEAM) and text in said and mouth.team_line(said)
    assert text in session.replies[-1]  # still in the rule's <message> too
    talker.say([text], verbatim=True)
    assert text in session.kw[-1]["user_input"]


def test_a_team_line_is_never_taken_for_the_callers():
    assert mouth.team_line(f"{mouth.TEAM}\n{FRONT_DESK[0]}") and not mouth.team_line("Yes, that's right.")
