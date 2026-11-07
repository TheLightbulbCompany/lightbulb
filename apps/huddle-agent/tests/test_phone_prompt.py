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
    assert "never again" in text and "You already greeted" not in text

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
    assert phone.clean_transcript(items, GREETING) == [
        {"who": "assistant", "text": GREETING, "t": 1.1},
        {"who": "caller", "text": "I didn't hear you respond.", "t": 14.0},
        {"who": "caller", "text": "Hey, how are you?", "t": 29.0},
        {
            "who": "assistant",
            "text": "I'm doing well, thank you for asking.",
            "t": 33.0,
        },
    ]


def test_a_dropped_greeting_is_restored_and_marked():
    items = [{"role": "user", "text": "Hi", "t": 6.0}]
    assert phone.clean_transcript(items, GREETING) == [
        {"who": "assistant", "text": GREETING, "t": 0.0, "assumed": True},
        {"who": "caller", "text": "Hi", "t": 6.0},
    ]
