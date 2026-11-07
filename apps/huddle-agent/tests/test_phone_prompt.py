import os

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

from huddle_agent import phone  # noqa: E402


def test_the_prompt_reads_back_before_booking_and_offers_three_slots():
    text = " ".join(phone.INSTRUCTIONS.split())
    assert (
        "read back the service, day, time and name, and wait for the caller to say yes"
        in text
    )
    assert "offer up to three open slots" in text


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


def test_the_greeting_is_tts_of_the_exact_text_not_a_model_turn(monkeypatch):
    """8 Nov sims: an empty generate_reply() let gemini-3.8 invent its first turn (3/15). The greeting is TTS audio of
    the exact text via say(); one prompt covers both paths. No first audio in time: the Live model greets via an empty
    generate_reply() (instructions there become a user turn). A stall mid-stream interrupts the say()."""
    import asyncio

    agent = phone.Receptionist(phone.Backend(), INFO, None)
    text = " ".join(agent.instructions.split())
    assert (
        f'Your greeting is exactly, word for word: "{GREETING}" If it has already been played to the caller, don\'t say it again; otherwise your first words are exactly that greeting.'
        in text
    )
    assert (
        "If you are told the disclosure was missed, say the one disclosure sentence exactly as given"
        in text
    )

    class Handle:
        interrupted = False

        def interrupt(self, force=False):
            self.interrupted = force

    class Session:
        def __init__(self):
            self.audio, self.replies, self.handle = None, [], Handle()

        def say(self, text, audio):
            self.text, self.audio = text, audio
            return self.handle

        def generate_reply(self, **kwargs):
            self.replies.append(kwargs)

    async def run(frames):
        session, stream = Session(), frames()
        await phone.greet(
            session, GREETING, stream, asyncio.ensure_future(anext(stream))
        )
        played = (
            [f async for f in session.audio] if session.audio else []
        )  # what say() would play out
        return session, played

    async def ok():
        yield "f1"
        yield "f2"

    async def broken():
        raise RuntimeError("tts down")
        yield

    async def slow():
        await asyncio.sleep(60)
        yield "f1"

    async def stalls():
        yield "f1"
        await asyncio.sleep(60)
        yield "f2"

    monkeypatch.setattr(phone, "FIRST_AUDIO_S", 0.1)
    monkeypatch.setattr(phone, "STALL_S", 0.1)
    s, played = asyncio.run(run(ok))
    assert (
        s.text == GREETING
        and played == ["f1", "f2"]
        and not s.replies
        and not s.handle.interrupted
    )
    for frames in (broken, slow):
        s, played = asyncio.run(run(frames))
        assert s.audio is None and s.replies == [{}], frames.__name__
    s, played = asyncio.run(run(stalls))
    assert (
        played == ["f1"] and s.handle.interrupted and not s.replies
    )  # cut off: the guard repeats the disclosure
    items = [{"role": "assistant", "text": GREETING, "t": 1.0, "interrupted": True}]
    assert (
        phone.disclosure_of(items, GREETING, "Lightbulb") == "missing"
    )  # what the record then says


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
    assert phone.disclosure_of(items, GREETING, "Lightbulb") == "ok"


def _guard(greeted=True):
    said = []
    guard = phone.DisclosureGuard(GREETING, "Lightbulb", said.append)
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
    assert phone.disclosure_of(items, GREETING, "Lightbulb") == "missing"
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
    assert phone.disclosure_of(cut_off, GREETING, "Lightbulb") == "missing"


def test_the_exact_greeting_or_a_disclosing_variant_is_ok_and_nothing_extra_is_said():
    loud = GREETING.upper().replace(".", "!").replace("  ", " ")
    variant = "Hi, you've reached Lightbulb, the AI assistant here; heads up, this call is recorded."
    for text in (GREETING, loud, variant):
        items = [{"role": "assistant", "text": text, "t": 1.0}]
        assert phone.disclosure_of(items, GREETING, "Lightbulb") == "ok", text
        guard, said = _guard()
        guard.seen(items)
        assert said == [] and not guard.corrected


def test_nothing_transcribed_before_the_caller_is_unknown_and_nothing_is_inserted():
    items = [
        {"role": "user", "text": "Hi", "t": 6.0},
        {"role": "assistant", "text": "Hello!", "t": 8.0},
    ]
    assert phone.disclosure_of(items, GREETING, "Lightbulb") == "unknown"
    assert phone.disclosure_of([], GREETING, "Lightbulb") == "unknown"
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

    phone.say_disclosure(
        Session(), "I'm Lightbulb, an AI assistant, and this call is recorded."
    )
    (only,) = calls
    assert (
        '"I\'m Lightbulb, an AI assistant, and this call is recorded."'
        in only["instructions"]
    )
    assert "word for word, and nothing else" in only["instructions"]
    assert "do not say it again" in only["instructions"]


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
    assert phone.discloses(
        "This is Ava, an AI assistant. This call is recorded.", "x", "Ava"
    )
    assert not phone.discloses(
        "Nobody is available; I'm an AI assistant and this is recorded.", "x", "Ava"
    )
    assert not phone.discloses(
        "This is Ava, an AI assistant. This call is not recorded.", "x", "Ava"
    )
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
        assert not phone.discloses(
            f"This is Ava, an AI assistant. {denial}", "x", "Ava"
        ), denial
    for ok in (
        "Not a problem, this call is recorded.",
        "Don't worry, this call is recorded.",
    ):
        assert phone.discloses(f"This is Ava, an AI assistant. {ok}", "x", "Ava"), ok


def test_message_only_mode_says_to_take_a_message():
    def prompt(**extra):
        r = phone.Receptionist(None, {**INFO, **extra}, None)
        return " ".join(r.instructions.split())

    text = prompt(mode="message_only", can={"book": False, "move": False})
    assert "can only take a message" in text and "owner will call back" in text
    assert "can only take a message" not in prompt(mode="live")


def test_phone_tools_include_remember():
    r = phone.Receptionist(None, INFO, None)
    names = {t.info.name for t in r.tools}
    assert {"remember"} <= names
    assert "only this caller's own preferences" in " ".join(phone.Receptionist.remember.info.description.split())
    text = " ".join(phone.Receptionist(None, {**INFO, "callerMemory": ["Prefers Saturdays"]}, None).instructions.split())
    assert "Prefers Saturdays" in text
