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


def test_a_talked_over_answer_stays_and_a_bare_backchannel_goes():
    """10 Nov prod: "Hmm." stood as the whole reply to "what time do you open tomorrow": the answer was talked over,
    so it was dropped as an interrupted line, and the backchannel stayed."""
    items = [
        {"role": "user", "text": "What time do you open tomorrow?", "t": 3.0},
        {"role": "assistant", "text": "Hmm.", "t": 4.0},
        {"role": "assistant", "text": "We open at 9 tomorrow and close at", "t": 7.0, "interrupted": True},
        {"role": "user", "text": "Mhm.", "t": 8.0},  # the caller's own words are never dropped
    ]
    assert phone.clean_transcript(items) == [
        {"who": "caller", "text": "What time do you open tomorrow?", "t": 3.0},
        {"who": "assistant", "text": "We open at 9 tomorrow and close at", "t": 7.0},
        {"who": "caller", "text": "Mhm.", "t": 8.0},
    ]


def test_the_caller_is_never_sent_to_another_number():
    """10 Nov prod: a refused cancel told the caller to call the salon's listed number."""
    p = phone.prompt(INFO)
    assert "Never give the caller a phone number to call" in p and "offer to take a message for the owner" in p
    assert "Never promise a waitlist spot" in p


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
    assert "cannot look up, move or cancel bookings from earlier calls" in unverified and "take a message" in unverified
    assert "a booking made on this call can still be moved or cancelled" in unverified  # 10 Nov: no second booking
    verified = phone.prompt({**INFO, "callerVerified": True, "callerName": "Priya"})
    assert "list, move or cancel the caller's own bookings" in verified and "Priya" in verified
    assert "cannot look up" not in verified


def test_recording_objections_do_not_promise_unrecorded_service():
    instructions = phone.prompt({**INFO, "spanish": True})
    assert "may keep a transcript" in instructions and "this call is recorded" not in instructions.lower()
    assert "sincerely asks whether they are speaking to a person" in instructions
    assert "Wait for the backend's result" in instructions
    # 10 Nov prod call: "They're finalizing the cancellation" was said while the cancel had been refused
    assert "say only neutral holding words" in instructions and "or finalized" in instructions




def test_static_facts_are_answered_directly_and_dynamic_claims_still_wait():
    """A recent prod call: "what services do you offer?" was delegated and the caller heard 19 s of silence. The Agent page's
    facts come with start and the voice states them itself; open times, bookings, messages and transfers still
    wait for the backend (#5215)."""
    services = "; ".join(f"Service number {i} ${40 + i} ({30 + 5 * i} min)" for i in range(18))
    answers = " ".join(f"Q: Question {i}? A: Saved answer {i}." for i in range(15))
    facts = f"City: Austin. Services: {services}. Opening hours: Mon 09:00–18:00. {answers}"
    p = phone.prompt({**INFO, "facts": facts, "instructions": "Do not state a street number.", "transfer": True})
    assert facts in p  # whole, never cut
    assert "Do not state a street number." in p
    assert p.index("Business facts") < p.index("Do not state a street number.")  # the owner's rule is read last
    assert "take priority over Business facts" in p
    assert "Business profile" not in p  # nothing to delegate for what the prompt already holds
    assert "You may state anything in Business facts directly" in p
    assert (
        "Wait for the backend's result before stating an open appointment time, confirming a booking, a change or a cancellation,"
        " or saying that a message was taken or a transfer started." in p
    )
    assert "facts about this business, including its description" not in p  # the shared Meet/orb rule is not here
    assert "Do not guess" in p


def test_no_facts_means_nothing_about_the_business_is_stated():
    """Answering questions off or a paused line: start sends no facts, and the voice must not invent any."""
    p = phone.prompt(INFO)
    assert "Business facts: none for this call." in p
    assert "Business profile" not in p
