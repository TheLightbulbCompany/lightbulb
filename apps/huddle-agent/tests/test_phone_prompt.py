import os

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

from huddle_agent import mouth, phone  # noqa: E402


def test_dialed_numbers_become_e164():
    assert (
        phone.e164("14842079294") == "+14842079294"
    )  # sip.trunkPhoneNumber, live call 7 Nov
    assert phone.e164("+16105550100") == "+16105550100"
    assert phone.e164("") == ""


GREETING = "Thanks for calling Super Agent Test. This is Lightbulb, Super Agent Test's AI assistant. This call is recorded. How can I help?"
INFO = {
    "assistantName": "Lightbulb",
    "businessName": "Super Agent Test",
    "today": "Saturday",
    "greeting": GREETING,
}


class _Speech:
    def __init__(self, sid="sp_open", allow=True):
        self.id, self.allow_interruptions, self.interrupted = sid, allow, 0

    def interrupt(self, force=False):
        self.interrupted += 1

    def add_done_callback(self, cb):
        pass


def test_the_opening_is_one_short_sentence_with_the_three_disclosures_and_no_narration():
    """Founder, 8 Nov: no script. The prompt names what the opening must say; the owner's greeting is a style.
    8 Nov prod: the opening ran on for up to 20 s and spoke the model's planning aloud."""
    text = " ".join(phone.Receptionist(phone.Backend(), INFO, None).instructions.split())
    assert ("You speak first. Your opening is one short sentence, in your own words, that says all three: your "
            "name, Lightbulb; that you are an AI assistant; and that this call is recorded.") in text
    assert 'Then ask "How can I help?" and stop: say nothing else until the caller speaks.' in text
    assert "It is your very first words: no preamble before it." in text  # gpt-realtime-2 preambles by default
    assert 'Never say your reasoning, plans or notes out loud ("Okay, let me think...", "Game plan:")' in text
    assert "Today is" not in text  # the date set off "it's Sunday, so we're closed" run-ons; the team has it
    assert f'a guide, not a script: "{GREETING}"' in text
    assert "If you are told the disclosure was missed, say the one disclosure sentence exactly as given" in text
    replies = []

    class Session:
        def generate_reply(self, **kw):
            replies.append(kw)
            return _Speech()

    opening = phone.greet(Session())
    # no instructions (Gemini keeps them); tools off: the opening is never a delegate call
    assert replies == [{"tool_choice": "none"}]
    # firm on the handle: generate_reply drops allow_interruptions=False under server-side turns
    assert opening.allow_interruptions is False


def test_the_opening_is_cut_on_narration_or_once_its_disclosure_and_question_are_said():
    cut = lambda said: phone.opening_cut(said, "Nova")  # noqa: E731
    # 8 Nov prod, person-073556: planning before the disclosure
    assert cut("Okay, let me think about how to respond to your request") == "narration"
    assert cut("Game plan: greet them") == "narration"
    # 8 Nov prod, book run 1: the disclosure, its question, then a run-on
    said = "Hi, thanks for calling The Lightbulb Company. This is Nova, the AI assistant. This call is recorded. "
    assert cut(said) == "" and cut(said + "How can I help?") == ""
    assert cut(said + "How can I help? Game plan: it's Sunday") == "run_on"
    assert cut("Hi, I'm Nova, an AI assistant, and this call is recorded, how can I help? If you") == "run_on"
    # no question: the disclosure's sentence and one more, then nothing
    assert cut("I'm Nova, an AI assistant; this call is recorded. Happy to help.") == ""
    assert cut("I'm Nova, an AI assistant; this call is recorded. Happy to help. If you're just checking") == "run_on"
    assert cut("Hi, thanks for calling.") == ""  # no disclosure yet, no narration: it may go on


async def _speak(r, *deltas):
    async def stream():
        for d in deltas:
            yield d

    return "".join([d async for d in r.transcription_node(stream(), None)])


def _receptionist(monkeypatch, speech, user_state="listening", agent_state="listening", mode="live", transfer=None):
    replies = []

    class Session:
        current_speech = speech

        def generate_reply(self, **kw):
            replies.append(kw)
            return _Speech("sp_hold")

    s = Session()
    s.user_state, s.agent_state = user_state, agent_state
    monkeypatch.setattr(phone.Receptionist, "session", property(lambda _self: s))
    b = FakeBackend()
    r = phone.Receptionist(b, {**INFO, "mode": mode}, transfer)
    r.opening = speech.id
    return r, s, b, replies


async def test_only_the_disclosure_is_firm_then_a_caller_talking_over_it_wins(monkeypatch):
    speech = _Speech(allow=False)
    r, s, _b, _ = _receptionist(monkeypatch, speech)
    await _speak(r, "Hi, this is Lightbulb, ", "an AI assistant, ")
    assert speech.allow_interruptions is False and speech.interrupted == 0  # the disclosure is still playing
    await _speak(r, "Hi, this is Lightbulb, an AI assistant, ", "and this call is recorded. ", "How can I help?")
    assert speech.allow_interruptions is True and speech.interrupted == 0  # free now; nothing cut
    speech = _Speech(allow=False)
    r, s, _b, _ = _receptionist(monkeypatch, speech, user_state="speaking")
    await _speak(r, "Hi, this is Lightbulb, an AI assistant, ", "and this call is recorded. ", "How can")
    assert speech.interrupted == 1  # the caller was already talking: the rest of the opening gives way
    speech = _Speech(allow=False)
    r, *_ = _receptionist(monkeypatch, speech)
    await _speak(r, "Okay, let me think ", "about how to respond")
    assert speech.interrupted == 1  # narration: cut, and the guard says the disclosure sentence


async def test_a_line_said_over_the_firm_opening_is_delegated_in_code(monkeypatch):
    """8 Nov prod: LiveKit skipped the reply to every line said over the uninterruptible opening. Now code decides the
    line the moment its transcript lands; the holding line queues behind the opening. A "hi" over it gets nothing."""
    speech = _Speech(allow=False)
    r, _s, b, replies = _receptionist(monkeypatch, speech)
    assert r.line("Caller", "Can I book Saturday afternoon?") == "asked"
    await r.turns.flush()
    assert [(p, body["text"].split("\n\n")[0]) for p, body in b.calls] == [("turn", "Can I book Saturday afternoon?")]
    assert replies == [{"instructions": mouth.HOLD, "tool_choice": "none"}]
    r, _s, b, replies = _receptionist(monkeypatch, _Speech(allow=False))
    assert r.line("Caller", "Hi") == "" and b.calls == [] and replies == []


async def _settle(r):
    import asyncio

    await asyncio.gather(*r._tasks)
    await r.turns.flush()


async def test_the_call_ends_once_at_hang_up_billed_from_answer():
    """8 Nov prod: the end ran at job shutdown, ~22 s after the caller left, and counted from job start."""
    import asyncio

    ended = []

    async def end(duration):
        ended.append(duration)

    ending = phone.Ending(phone.time.monotonic() - 19.4, end)
    first = ending.now()  # the caller's SIP participant left
    await asyncio.sleep(0.01)
    assert ending.now("job shutdown") is first  # the shutdown callback awaits the same end
    await first
    assert ended == [19]
    import inspect

    src = inspect.getsource(phone.entrypoint)
    assert '@ctx.room.on("participant_disconnected")' in src and "participant.identity == caller.identity" in src
    assert "Ending(answered, on_end)" in src and "ctx.add_shutdown_callback(shutdown)" in src


def test_the_transcript_keeps_final_lines_in_spoken_order_once():
    # The founder's call (cdb88d13), as the plugin emitted it: the greeting twice, a re-greet cut off, a reply's
    # partial before the caller's line it answers, and that caller line twice.
    items = [
        {"role": "assistant", "text": GREETING, "t": 1.1},
        {"role": "assistant", "text": GREETING, "t": 1.1},
        {"role": "user", "text": "I didn't hear you respond.", "t": 14.0},
        {
            "role": "assistant",
            "text": "Thanks for calling Super Agent Test. This is Lightbulb",
            "t": 20.0,
            "interrupted": True,
        },
        {
            "role": "assistant",
            "text": "I'm doing well, thank you",
            "t": 31.5,
            "interrupted": True,
        },
        {"role": "user", "text": "Hey, how are you?", "t": 29.0},
        {"role": "user", "text": "Hey, how are you?", "t": 29.0},
        {
            "role": "assistant",
            "text": "I'm doing well, thank you for asking.",
            "t": 33.0,
        },
        {"role": "system", "text": "x", "t": 0.5},
    ]
    assert phone.clean_transcript(items) == [
        {"who": "assistant", "text": GREETING, "t": 1.1},
        {"who": "caller", "text": "I didn't hear you respond.", "t": 14.0},
        {"who": "caller", "text": "Hey, how are you?", "t": 29.0},
        {
            "who": "assistant",
            "text": "I'm doing well, thank you for asking.",
            "t": 33.0,
        },
    ]
    assert phone.disclosure_of(items, "Lightbulb") == "ok"


def _guard(greeted=True):
    said = []
    guard = phone.DisclosureGuard("Lightbulb", said.append)
    if greeted:  # the greeting played: listening -> speaking -> listening
        guard.state("listening", "speaking")
        guard.state("speaking", "listening")
    return guard, said


def test_nothing_is_judged_until_the_greeting_has_finished_playing():
    paraphrase = [{"role": "assistant", "text": "Hi there, how can I help?", "t": 1.0}]
    guard, said = _guard(greeted=False)
    guard.state("initializing", "listening")
    guard.seen(paraphrase)
    guard.state("listening", "speaking")  # the greeting is playing
    guard.seen(paraphrase)
    assert said == []
    guard.state("speaking", "listening")
    guard.seen(paraphrase)
    assert said == [guard.line]


def test_a_paraphrased_greeting_is_missing_and_the_disclosure_is_said_once():
    items = [
        {
            "role": "assistant",
            "text": "Hi there, Super Agent Test, how can I help?",
            "t": 1.0,
        }
    ]
    assert phone.disclosure_of(items, "Lightbulb") == "missing"
    guard, said = _guard()
    guard.seen(items)
    items.append({"role": "user", "text": "Hi", "t": 6.0})
    guard.seen(items)
    assert (
        said == ["I'm Lightbulb, an AI assistant, and this call is recorded."]
        and guard.corrected
    )
    cut_off = [
        {"role": "assistant", "text": GREETING[:40], "t": 1.0, "interrupted": True}
    ]
    assert phone.disclosure_of(cut_off, "Lightbulb") == "missing"


def test_a_greeting_or_any_disclosing_variant_is_ok_and_nothing_extra_is_said():
    loud = GREETING.upper().replace(".", "!").replace("  ", " ")
    variant = "Hi, you've reached Lightbulb, the AI assistant here; heads up, this call is recorded."
    for text in (GREETING, loud, variant):
        items = [{"role": "assistant", "text": text, "t": 1.0}]
        assert phone.disclosure_of(items, "Lightbulb") == "ok", text
        guard, said = _guard()
        guard.seen(items)
        assert said == [] and not guard.corrected


def test_nothing_transcribed_before_the_caller_is_unknown_and_nothing_is_inserted():
    items = [
        {"role": "user", "text": "Hi", "t": 6.0},
        {"role": "assistant", "text": "Hello!", "t": 8.0},
    ]
    assert phone.disclosure_of(items, "Lightbulb") == "unknown"
    assert phone.disclosure_of([], "Lightbulb") == "unknown"
    assert phone.clean_transcript(items) == [
        {"who": "caller", "text": "Hi", "t": 6.0},
        {"who": "assistant", "text": "Hello!", "t": 8.0},
    ]
    guard, said = _guard()
    guard.seen(items)
    assert said == []


def test_the_corrective_line_is_one_reply_that_says_never_again():
    calls = []

    class Session:
        def generate_reply(self, **kwargs):
            calls.append(kwargs)
            return _Speech()

    assert phone.say_disclosure(
        Session(), "I'm Lightbulb, an AI assistant, and this call is recorded."
    ).allow_interruptions is False  # the disclosure sentence is firm
    (only,) = calls
    assert (
        '"I\'m Lightbulb, an AI assistant, and this call is recorded."'
        in only["instructions"]
    )
    assert "word for word, and nothing else" in only["instructions"]
    assert "do not say it again" in only["instructions"]
    assert set(only) == {"instructions", "tool_choice"} and only["tool_choice"] == "none"  # never a delegate call


def test_the_scripted_run_flag_fails_the_greeting_once_it_has_played(monkeypatch):
    monkeypatch.setenv("PHONE_TEST_FAIL_DISCLOSURE", "1")
    guard, said = _guard(greeted=False)
    guard.state("listening", "speaking")
    guard.seen([{"role": "assistant", "text": GREETING, "t": 1.0}])
    assert said == []  # still speaking
    guard.state("speaking", "listening")
    guard.seen([{"role": "assistant", "text": GREETING, "t": 1.0}])
    guard.state("listening", "speaking")
    guard.state("speaking", "listening")
    guard.seen([])
    assert said == [guard.line] and guard.corrected


def test_the_disclosure_matches_whole_words_and_not_a_denial():
    """#4996 review: "Ava" must not match inside "available", and "not recorded" is no disclosure."""
    assert phone.discloses("This is Ava, an AI assistant. This call is recorded.", "Ava")
    assert not phone.discloses("Nobody is available; I'm an AI assistant and this is recorded.", "Ava")
    assert not phone.discloses("This is Ava, an AI assistant. This call is not recorded.", "Ava")
    for denial in (
        "this call isn't being recorded.",
        "This call is not going to be recorded.",
        "This call isn't currently recorded.",
        "This call is not ever recorded.",
        "This call is not really recorded.",
        "Nothing is recorded.",
        "No part of this call is recorded.",
        "This call won’t be recorded.",
    ):
        assert not phone.discloses(f"This is Ava, an AI assistant. {denial}", "Ava"), denial
    for ok in (
        "Not a problem, this call is recorded.",
        "Don't worry, this call is recorded.",
    ):
        assert phone.discloses(f"This is Ava, an AI assistant. {ok}", "Ava"), ok


def test_message_only_mode_says_to_take_a_message():
    def prompt(**extra):
        r = phone.Receptionist(None, {**INFO, **extra}, None)
        return " ".join(r.instructions.split())

    text = prompt(mode="message_only", can={"book": False, "move": False})
    assert "it only takes messages" in text and "owner will call back" in text
    assert "only takes messages" not in prompt(mode="live")


def test_the_talker_has_no_tools_and_says_the_teams_messages_faithfully():
    """8 Nov prod phone re-run: "the front desk team owns every booking... never say anything is booked or moved" made
    the talker say "Customer Operations will move your booking" as "please contact our front desk"."""
    r = phone.Receptionist(None, INFO, None)
    assert r.tools == []
    text = " ".join(r.instructions.split())
    assert "never say anything is booked, moved or done" not in text and "delegate" not in text
    assert "Never turn it into a refusal, never tell the caller to contact anyone else" in text
    assert '("I\'m moving it to Sunday now.", "You\'re booked for 3 pm Saturday.")' in text
    assert "never say an outcome the message does not say" in text
    assert "What you may tell callers" not in text  # no facts of its own: the Router and Front desk own them
    assert phone.SERVER_TURNS.create_response is False  # code answers every line, never the model on its own


class FakeBackend:
    call_id = "call-1"

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    async def post(self, path, body=None):
        self.calls.append((path, body or {}))
        return self.answers.get(path, {"ok": True})


async def test_delegate_posts_the_callers_line_with_the_talk_since_the_last_ask():
    b = FakeBackend()
    r = phone.Receptionist(b, {**INFO, "mode": "live"}, None)
    r.context.add("Caller", "Hi, it's Priya.")
    r.context.add("Lightbulb", "Hi Priya!")
    r.delegate("Can I book a gel manicure Saturday at 2?")
    r.delegate("And how much is it?")
    await r.turns.flush()
    assert [p for p, _ in b.calls] == ["turn", "turn"]
    first, second = (body for _, body in b.calls)
    assert first["n"] == 0 and first["text"].startswith("Can I book a gel manicure Saturday at 2?")
    assert "> Caller: Hi, it's Priya." in first["text"] and "> Lightbulb: Hi Priya!" in first["text"]
    assert second == {"n": 1, "text": "And how much is it?"}  # the context went with the first ask only


async def test_a_paused_line_takes_every_line_as_a_message(monkeypatch):
    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"), mode="message_only")
    b.answers = {"message": {"taken": True}}
    assert r.line("Caller", "Are you open Sunday? Call me back please, it's Priya.") == "message"
    await _settle(r)
    assert b.calls == [("message", {"text": "Are you open Sunday? Call me back please, it's Priya.", "name": "",
                                    "callbackNumber": ""})]  # fmt: skip
    assert r.did == ["Message: Are you open Sunday? Call me back please, it's Priya."]
    assert replies == [{"instructions": mouth.fixed(phone.PAUSED), "tool_choice": "none"}]


async def test_a_message_asked_for_is_taken_on_the_next_line(monkeypatch):
    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"))
    assert r.line("Caller", "Can I leave a message?") == "ask_message"
    assert replies[-1]["instructions"] == mouth.fixed(phone.ASK_MESSAGE)
    assert r.line("Caller", "Tell Maya I'll be ten minutes late. It's Priya.") == "message"
    await _settle(r)
    assert [(p, body["text"]) for p, body in b.calls] == [("message", "Tell Maya I'll be ten minutes late. It's Priya.")]
    assert replies[-1]["instructions"] == mouth.fixed(phone.TAKEN)
    r, _s, b, _ = _receptionist(monkeypatch, _Speech("sp_x"))  # the message in the same line: taken at once
    assert r.line("Caller", "Can I leave a message for Maya that I'll be ten minutes late?") == "message"
    await _settle(r)
    assert [p for p, _ in b.calls] == ["message"]


async def test_a_yes_to_the_message_offer_takes_a_message(monkeypatch):
    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"))
    r.offered = True  # the hold's OFFER_MESSAGE, or a failed hand-off, was said
    assert r.line("Caller", "Yes please.") == "ask_message"
    assert r.line("Caller", "Ask her to call me about the bridal party.") == "message"
    await _settle(r)
    assert [p for p, _ in b.calls] == ["message"]
    r, _s, b, _ = _receptionist(monkeypatch, _Speech("sp_x"))
    assert r.line("Caller", "Yes please.") == "asked"  # no offer: a yes answers the team (the Router)


async def test_a_person_is_the_existing_transfer(monkeypatch):
    sip = []

    async def transfer(to):
        sip.append(to)

    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"), transfer=transfer)
    b.answers = {"transfer": {"transferTo": "+14155550199"}}
    assert r.line("Caller", "Can I talk to someone?") == "person"
    await _settle(r)
    assert b.calls == [("transfer", {})] and sip == ["+14155550199"] and r.did == ["Transferred to a person"]
    assert replies[0]["instructions"] == mouth.fixed(phone.CONNECTING)

    async def refused(_to):
        raise RuntimeError("SIP 486")

    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"), transfer=refused)
    b.answers = {"transfer": {"transferTo": "+14155550199"}}
    r.line("Caller", "I want to speak to a real person.")
    await _settle(r)
    assert b.calls == [("transfer", {}), ("transfer", {"failed": True})]
    assert replies[-1]["instructions"] == mouth.fixed(phone.NOBODY) and r.offered
    r, _s, b, replies = _receptionist(monkeypatch, _Speech("sp_x"), transfer=transfer)
    b.answers = {"transfer": {"transferTo": None}}
    r.line("Caller", "Transfer me please")
    await _settle(r)
    assert b.calls == [("transfer", {})] and replies[-1]["instructions"] == mouth.fixed(phone.NOBODY)


async def test_holding_lines_fire_on_time_and_stop_when_the_team_speaks():
    import asyncio

    said = []
    hold = phone.Hold(said.append, hold_s=0.05, offer_s=0.12)
    hold.start()
    assert hold.waiting
    await asyncio.sleep(0.08)
    assert said == [phone.HOLD_AGAIN]
    await asyncio.sleep(0.08)
    assert said == [phone.HOLD_AGAIN, phone.OFFER_MESSAGE]
    hold.start()
    await asyncio.sleep(0.02)
    hold.answered()
    await asyncio.sleep(0.15)
    assert said == [phone.HOLD_AGAIN, phone.OFFER_MESSAGE]  # answered before 8 s: nothing more
    assert hold.waiting  # the ack came; a Front desk answer may still: a quiet caller is holding
    assert "take a message" in phone.OFFER_MESSAGE and "add no facts" in phone.HOLD_AGAIN.lower()


def test_the_wait_defaults_are_8_and_20_seconds():
    assert (phone.HOLD_S, phone.OFFER_S) == (8.0, 20.0)


async def test_delegate_starts_the_hold():
    started = []

    class H:
        def start(self):
            started.append(True)

    r = phone.Receptionist(FakeBackend(), INFO, None, H())
    r.delegate("Open Sunday?")
    await r.turns.flush()
    assert started == [True]


def test_every_scripted_line_is_said_with_tools_off():
    """With tools on, the model calls delegate instead of speaking an instruction line (Meet lane, 8 Nov)."""
    import inspect

    made = []

    class Session:
        def generate_reply(self, **kw):
            made.append(kw)
            return _Speech()

    for text in (phone.STILL_THERE, phone.SILENT_BYE, phone.HOLD_AGAIN, phone.OFFER_MESSAGE):
        phone.line(Session(), text)
    assert made[:4] == [{"instructions": t, "tool_choice": "none"} for t in
                    (phone.STILL_THERE, phone.SILENT_BYE, phone.HOLD_AGAIN, phone.OFFER_MESSAGE)]  # fmt: skip
    phone.greet(Session())
    phone.say_disclosure(Session(), "I'm Nova, an AI assistant, and this call is recorded.")
    assert [kw["tool_choice"] for kw in made[4:]] == ["none", "none"]  # the opening and its correction too
    src = inspect.getsource(phone.entrypoint)
    for used in ("line(session, STILL_THERE)", "line(session, SILENT_BYE)", "line(session, text)"):
        assert used in src  # every phone-only instruction line goes through `line`
    assert "generate_reply(instructions=" not in src
