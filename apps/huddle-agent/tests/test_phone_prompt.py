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


async def test_backend_sends_the_call_token_on_every_per_call_route():
    """M4: start's callToken goes on every later route for the call; start itself carries only the worker secret."""
    import httpx

    seen = []

    def answer(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("X-Lightbulb-Call-Token")))
        return httpx.Response(200, json={"callId": "c1", "callToken": "tok"})

    b = phone.Backend()
    b._http = httpx.AsyncClient(base_url="http://x/api/v1", transport=httpx.MockTransport(answer))
    info = await b.post("/internal/voice/calls", {})
    b.call_id, b.call_token = info["callId"], info["callToken"]
    await b.post("turn", {"n": 0, "text": "hi"})
    await b.post("end", {"durationSec": 1})
    assert seen == [
        ("/api/v1/internal/voice/calls", None),
        ("/api/v1/internal/voice/calls/c1/turn", "tok"),
        ("/api/v1/internal/voice/calls/c1/end", "tok"),
    ]


GREETING = "Thanks for calling Super Agent Test. This is Lightbulb, Super Agent Test's AI assistant. How can I help?"
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
    assert (
        ending.now("job shutdown") is first
    )  # the shutdown callback awaits the same end
    await first
    assert ended == [19]
    import inspect

    src = inspect.getsource(phone.entrypoint)
    assert (
        '@ctx.room.on("participant_disconnected")' in src
        and "participant.identity == caller.identity" in src
    )
    assert (
        "Ending(answered, on_end)" in src
        and "ctx.add_shutdown_callback(shutdown)" in src
    )


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


def test_a_ui_choice_question_is_said_with_its_options():
    """8 Nov prod re-run #3, move-102705 seq 6, the exact row: the Monday times were only on the card."""
    row = (
        "Which time works for you on Monday, November 9?\n\n```openui-lang\nroot = Stack([q])\n"
        'q = Choice("Choose a time:", ["1:00 PM", "2:30 PM", "4:00 PM"])\n```'
    )
    assert (
        mouth.speakable(row)
        == "Which time works for you on Monday, November 9? 1:00 PM, 2:30 PM or 4:00 PM."
    )
    assert (
        mouth.speakable("Done.\n```python\nx = 1\n```") == "Done."
    )  # other code blocks stay unsaid


def test_an_unverified_caller_is_offered_a_message_for_existing_bookings():
    """Founder 9 Nov: without STIR/SHAKEN A the number proves nothing. The backend refuses; the voice words it."""
    unverified = phone.prompt({**INFO, "callerVerified": False})
    assert "list, move or cancel" not in unverified
    assert "cannot look up, move or cancel existing bookings" in unverified and "take a message" in unverified
    verified = phone.prompt({**INFO, "callerVerified": True, "callerName": "Priya"})
    assert "list, move or cancel the caller's own bookings" in verified and "Priya" in verified
    assert "cannot look up" not in verified


def test_recording_objections_do_not_promise_unrecorded_service():
    instructions = phone.prompt({**INFO, "spanish": True})
    assert "may keep a transcript" in instructions and "this call is recorded" not in instructions.lower()
    assert "sincerely asks whether they are speaking to a person" in instructions
    assert "Wait for the backend's result" in instructions


