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


def test_the_greeting_comes_from_the_system_prompt_not_an_injected_turn():
    """Live call 7 Nov: generate_reply(instructions="say exactly ...") became a user turn on gemini-3.8 and the agent
    re-greeted on the caller's first turn. The rule lives in the instructions; the first reply gets none."""
    agent = phone.Receptionist(phone.Backend(), INFO, None)
    text = " ".join(agent.instructions.split())
    assert (
        f'Your first words on this call are exactly, word for word: "{GREETING}"'
        in text
    )
    assert (
        "Say the greeting once, at the start, and don't repeat it." in text
        and "You already greeted" not in text
    )
    assert (
        "If you are told the disclosure was missed, say the one disclosure sentence exactly as given"
        in text
    )

    calls = []

    class Session:
        def generate_reply(self, **kwargs):
            calls.append(kwargs)

    import asyncio

    asyncio.run(phone.greet(Session()))
    assert calls == [{}]  # no instructions: nothing lands in the Live history as a turn


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
    assert not phone.discloses(
        "This is Ava, an AI assistant, and this call isn't being recorded.", "x", "Ava"
    )
