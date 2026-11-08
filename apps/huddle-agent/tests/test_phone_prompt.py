import asyncio
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
    """A guard whose opening turn ends as soon as the voice stops (gap 0); `await _turn_over()` lets it."""
    said = []
    guard = phone.DisclosureGuard("Lightbulb", said.append, gap_s=0)
    if greeted:  # the greeting played: listening -> speaking -> listening
        guard.state("listening", "speaking")
        guard.state("speaking", "listening")
    return guard, said


async def _turn_over():
    await asyncio.sleep(0.01)


async def test_nothing_is_judged_until_the_greetings_turn_is_over():
    paraphrase = [{"role": "assistant", "text": "Hi there, how can I help?", "t": 1.0}]
    guard, said = _guard(greeted=False)
    guard.state("initializing", "listening")
    guard.seen(paraphrase)
    guard.state("listening", "speaking")  # the greeting is playing
    guard.seen(paraphrase)
    await _turn_over()
    assert said == []
    guard.state("speaking", "listening")
    await _turn_over()
    assert said == [guard.line]


async def test_a_paraphrased_greeting_is_missing_and_the_disclosure_is_said_once():
    items = [{"role": "assistant", "text": "Hi there, Super Agent Test, how can I help?", "t": 1.0}]
    assert phone.disclosure_of(items, "Lightbulb") == "missing"
    guard, said = _guard()
    guard.seen(items)
    await _turn_over()
    items.append({"role": "user", "text": "Hi", "t": 6.0})
    guard.seen(items)
    assert said == ["I'm Lightbulb, an AI assistant, and this call is recorded."] and guard.corrected
    cut_off = [{"role": "assistant", "text": GREETING[:40], "t": 1.0, "interrupted": True}]
    assert phone.disclosure_of(cut_off, "Lightbulb") == "missing"


async def test_a_greeting_or_any_disclosing_variant_is_ok_and_nothing_extra_is_said():
    loud = GREETING.upper().replace(".", "!").replace("  ", " ")
    variant = "Hi, you've reached Lightbulb, the AI assistant here; heads up, this call is recorded."
    for text in (GREETING, loud, variant):
        items = [{"role": "assistant", "text": text, "t": 1.0}]
        assert phone.disclosure_of(items, "Lightbulb") == "ok", text
        guard, said = _guard()
        guard.seen(items)
        await _turn_over()
        assert said == [] and not guard.corrected


async def test_nothing_transcribed_before_the_caller_is_unknown_for_the_record_and_corrected():
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
    # the caller spoke before any disclosure played (Review Focus 4): the guard has the sentence said
    guard, said = _guard()
    guard.seen(items)
    assert said == [guard.line] and guard.corrected


async def test_the_scripted_run_flag_fails_the_greeting_once_it_has_played(monkeypatch):
    monkeypatch.setenv("PHONE_TEST_FAIL_DISCLOSURE", "1")
    guard, said = _guard(greeted=False)
    guard.state("listening", "speaking")
    guard.seen([{"role": "assistant", "text": GREETING, "t": 1.0}])
    assert said == []  # still speaking
    guard.state("speaking", "listening")
    await _turn_over()
    guard.state("listening", "speaking")
    guard.state("speaking", "listening")
    await _turn_over()
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




def test_a_ui_choice_question_is_said_with_its_options():
    """8 Nov prod re-run #3, move-102705 seq 6, the exact row: the Monday times were only on the card."""
    row = ("Which time works for you on Monday, November 9?\n\n```openui-lang\nroot = Stack([q])\n"
           'q = Choice("Choose a time:", ["1:00 PM", "2:30 PM", "4:00 PM"])\n```')
    assert mouth.speakable(row) == "Which time works for you on Monday, November 9? 1:00 PM, 2:30 PM or 4:00 PM."
    assert mouth.speakable("Done.\n```python\nx = 1\n```") == "Done."  # other code blocks stay unsaid


def test_the_result_rule_never_lets_the_voice_say_there_is_no_update():
    """8 Nov prod re-run #3: each team result came out as "I don't have a new team update yet" and was cut."""
    assert "never say there is no update yet" in mouth.result_instructions(["Moved it."])
