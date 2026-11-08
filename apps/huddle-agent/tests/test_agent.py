from huddle_agent import agent
from huddle_agent.agent import SERVER_VAD


def test_the_one_speech_setting_is_what_the_pinned_plugin_validates():
    """Every call, in a Meet or in the app, transcribes with the short server VAD (owner ruling 10-12)."""
    from livekit.plugins.openai.stt import _as_turn_detection

    assert _as_turn_detection(SERVER_VAD).silence_duration_ms == 700
    assert _as_turn_detection(agent.MEET_VAD).silence_duration_ms == 400


def test_the_meet_bots_page_is_never_a_person():
    from types import SimpleNamespace

    from livekit import rtc

    standard, ingress = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, rtc.ParticipantKind.PARTICIPANT_KIND_INGRESS
    assert not agent.person(SimpleNamespace(identity="meetbot:room-1", kind=standard))
    for identity in ("meet:room-1", "meet:room-1:p1"):  # a mixed Meet, and one person of it
        assert agent.person(SimpleNamespace(identity=identity, kind=ingress))
    assert agent.person(SimpleNamespace(identity="u1", kind=standard))
    assert not agent.person(SimpleNamespace(identity="agent-x", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))


class DrivenSession:
    """An AgentSession as a driven voice uses it: each order is a handle the test finishes by hand."""

    def __init__(self, llm=None, vad=None):
        import asyncio

        owner = self

        class Handle(asyncio.Future):  # a SpeechHandle: awaited to its end, and it says whether it was cut
            interrupted = False

            def interrupt(self, force=False):  # the mouth stops its own unit (no sound in time, or too long)
                assert force
                self.interrupted = True
                owner._cancel()
                self.set_result(None)

            def set_result(self, result):
                # it ends with its audio sent: the unit's meter saw sound (`silent` below ends without it)
                unit = getattr(owner.voice or owner.started.get("agent"), "unit", None)
                if unit is not None and not owner.silent:
                    unit.frames, unit.seconds, unit.loud = 50, 1.0, 1.0
                super().set_result(result)

        # `wire`: a real `_DrivenSession` its orders and cancels go through (it may hold a create). Without
        # one, an order's `response.create` is sent as it is given.
        self.voice, self.silent, self.wire = None, False, None
        self.llm, self.orders, self.handles, self.handlers, self.started = llm, [], [], {}, {}
        self.interrupts, self.closed, self._future = 0, False, Handle

    def on(self, event, fn):
        self.handlers[event] = fn

    async def start(self, **kw):
        self.started = kw

    def generate_reply(self, **kw):
        from openai.types.realtime import RealtimeResponseCreateParams, ResponseCreateEvent

        self.orders.append(kw)
        self.handles.append(self._future())
        if self.wire is not None:
            params = RealtimeResponseCreateParams(instructions=kw["instructions"])
            self.wire._current_generation = None  # (the response before it is over, cut or not)
            self.wire.send_event(ResponseCreateEvent(type="response.create", response=params))
            self.wire._current_generation = object()  # response.created
        elif (sent := getattr(getattr(self.voice, "realtime_llm_session", None), "sent", None)) is not None:
            sent()
        return self.handles[-1]

    def _cancel(self):
        from openai.types.realtime import ResponseCancelEvent

        if self.wire is not None:
            self.wire.send_event(ResponseCancelEvent(type="response.cancel"))

    def interrupt(self, force=False):
        assert force
        self.interrupts += 1
        for h in self.handles:
            if not h.done():  # the speech in the air ends cut, as LiveKit's does
                h.interrupted = True
                self._cancel()
                h.set_result(None)

    async def aclose(self):
        self.closed = True


class DrivenAgent:
    def __init__(self):
        from livekit.agents import llm

        self.instructions, self.ctx = "persona", llm.ChatContext.empty()
        self.realtime_llm_session = self

    @property
    def chat_ctx(self):
        return self.ctx

    async def update_chat_ctx(self, ctx):
        self.ctx = ctx

    async def update_instructions(self, text):
        self.instructions = text


async def _settle():
    import asyncio

    for _ in range(30):
        await asyncio.sleep(0)


def _driven():
    from unittest.mock import MagicMock

    session, voice = DrivenSession(), DrivenAgent()
    session.voice = voice
    said, ended, receipts, asked = [], [], [], []
    live = agent._DrivenLive(
        "forge", session, voice, MagicMock(),
        said=lambda order, index, text, outcome, evidence: (
            said.append((text, outcome != "said")), receipts.append((order, index, outcome, evidence))
        ),
        ended=lambda order, failed: ended.append((order, failed)),
        may=lambda order, index: asked.append((order, index)) or live.allowed,
    )  # fmt: skip
    # its reports: each unit (said to its end, or not), each order's end, and every permission it asked for
    live.said, live.ended, live.receipts, live.asked, live.allowed = said, ended, receipts, asked, True
    return live, session, voice


async def test_a_driven_voice_says_a_sentence_an_order_and_reports_each_by_its_own_text():
    live, session, _ = _driven()
    live.say("The Workspace plan costs fifty dollars. It is billed monthly.")
    live.say("Glad to help.")  # small talk written for it (huddle's writer): an order like any other
    await _settle()
    assert session.orders == [
        {"instructions": "Say this line aloud, word for word, and nothing else:\nThe Workspace plan costs fifty dollars."}
    ], "the second sentence waits for the first to be said: two lines never overlap"
    assert live.said == [], "ordered is not said"
    for n in (2, 3):
        session.handles[-1].set_result(None)
        await _settle()
        assert len(session.orders) == n
    session.handles[-1].set_result(None)
    await _settle()
    assert [o["instructions"] for o in session.orders[1:]] == [agent.SAY + "It is billed monthly.", agent.SAY + "Glad to help."]
    # each unit is reported when its playback ends, by the text it was ordered with
    assert live.said == [
        ("The Workspace plan costs fifty dollars.", False), ("It is billed monthly.", False), ("Glad to help.", False)
    ]
    assert not hasattr(live, "reply_to"), "it writes nothing itself: the worker has small talk written and orders it"


async def test_small_talk_is_written_from_the_line_and_who_said_it_with_one_client_for_the_call(monkeypatch):
    """10-07 rig: every voice made its own client at its first reply, about 2 s before "Hi"."""
    from types import SimpleNamespace as NS

    made, asked = [], []

    class Client:
        def __init__(self):
            made.append(self)
            self.chat = NS(completions=NS(create=self.create))

        async def create(self, **kw):
            asked.append(kw["messages"])
            return NS(choices=[NS(message=NS(content="NONE" if "bye" in kw["messages"][1]["content"] else "Glad to help."))])

    monkeypatch.setattr(agent, "AsyncOpenAI", Client)
    monkeypatch.setattr(agent, "_writer", None)
    assert await agent.write_line("You are Forge.", 'Priya just said to you: "thanks"') == "Glad to help."
    assert await agent.write_line("You are Ada.", 'Dan just said to you: "bye"') == "", "NONE: silence"
    assert len(made) == 1, "one client, whichever voice the line is for"
    assert asked[0][0]["content"].startswith("You are Forge.") and asked[0][1]["content"] == 'Priya just said to you: "thanks"'


def test_a_mouth_has_only_the_doors_the_huddle_uses():
    """What a hearing voice was told (thinking, commentary, a nudge, a typed turn) has no door on a mouth: only
    an order makes it speak."""
    from huddle_agent.huddle import Live

    doors = {n for n in vars(Live) if not n.startswith("_")} - {"agent_id", "track_name"}
    assert doors == {"append_instructions", "interrupt", "say", "usage_seconds", "aclose"}
    assert all(callable(getattr(agent._DrivenLive, n)) for n in doors)
    for gone in ("append_thinking", "append_commentary", "nudge", "reply_to", "mute", "set_participant", "hear"):
        assert not hasattr(agent._DrivenLive, gone), gone


async def test_an_interrupt_cuts_the_unit_being_said_and_drops_what_was_queued():
    live, session, _ = _driven()
    live.say("One. Two. Three.")
    await _settle()
    live.interrupt()
    await _settle()
    assert session.interrupts == 1 and len(session.orders) == 1, "two and three were never said"
    assert live.said == [("One.", True)], "the cut unit by its whole text, and nothing for the dropped ones"
    live.say("Four.")  # an order after the stop is a new one
    await _settle()
    assert [o["instructions"].splitlines()[-1] for o in session.orders] == ["One.", "Four."]


async def test_every_order_ends_in_one_receipt_whether_it_was_said_cut_never_started_or_failed():
    """10-07 review: a failed order was logged here and the worker went on waiting for its units for ever."""
    live, session, _ = _driven()
    live.say("One. Two.", 7)
    await _settle()
    session.handles[-1].set_result(None)
    await _settle()
    assert live.ended == [], "not over while a unit is still to be said"
    session.handles[-1].set_result(None)
    await _settle()
    assert live.ended == [(7, False)] and live.said == [("One.", False), ("Two.", False)], "after its last unit"
    live.say("Three. Four.", 8)
    live.say("Five.", 9)  # behind it on the chain
    await _settle()
    live.interrupt()
    await _settle()
    assert live.said[2:] == [("Three.", True)] and len(session.orders) == 3, "the cut unit, and nothing after it"
    assert live.ended[1:] == [(8, False), (9, False)], "the one cut and the one that never started both report"

    def broken(**kw):
        raise RuntimeError("AgentSession isn't running")

    working, session.generate_reply = session.generate_reply, broken
    live.say("Six.", 10)
    await _settle()
    assert live.ended[3:] == [(10, True)] and live.said[3:] == [], "failed, and said so"
    session.generate_reply = working
    live.say("Seven.", 11)  # the chain is not broken by it
    await _settle()
    session.handles[-1].set_result(None)
    await _settle()
    assert live.ended[4:] == [(11, False)] and live.said[3:] == [("Seven.", False)]


async def test_a_standing_rule_is_never_said_and_survives_a_stop():
    live, session, voice = _driven()
    live.say("one")
    live.append_instructions("Speak to Priya.")
    live.append_instructions("Speak to Dan.")
    await _settle()
    live.interrupt()  # a stop drops speech, not a standing rule
    await _settle()
    assert len(session.orders) == 1 and session.interrupts == 1
    assert voice.instructions == "persona\n\nSpeak to Priya.\n\nSpeak to Dan."


async def test_a_driven_voice_bills_wall_clock_seconds_to_its_close(monkeypatch):
    live, session, _ = _driven()
    live._opened = agent.time.monotonic() - 10
    assert 10 <= live.usage_seconds() < 11
    live.say("never said")  # queued behind nothing, but the session closes first
    await live.aclose()
    await _settle()
    closed_at = live.usage_seconds()
    monkeypatch.setattr(agent.time, "monotonic", lambda: 1e12)
    assert live.usage_seconds() == closed_at and session.closed, "the clock runs on; the bill does not"
    assert session.orders == [], "nothing is ordered of a closed session"


async def test_every_voice_is_driven_in_a_meet_or_in_the_app(monkeypatch):
    """Owner ruling 10-12: one voice system. A person of a Meet, a mixed Meet and a person in the app all get a
    mouth: no ears, a word-for-word line on each order."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    made = []
    monkeypatch.setattr(agent, "AgentSession", lambda **kw: made.append(DrivenSession(**kw)) or made[-1])
    monkeypatch.setattr(agent, "_DrivenModel", lambda **kw: kw)
    huddle, room = MagicMock(), MagicMock()

    async def start(speaker):
        return await agent.start_live(
            room, huddle, agent_id="forge", persona="You are Forge.", voice="cedar", speaker=speaker,
            seed=[("user", "Priya: hi")],
        )

    for speaker in ("meet:room-1:p1", "meet:room-1", "user_1"):  # a person of a Meet, a mixed Meet, the app
        made.clear()
        live = await start(speaker)
        assert isinstance(live, agent._DrivenLive) and live.track_name == "agent-forge"
        assert made[0].llm == {"model": "gpt-realtime-2.1-mini", "voice": "cedar", "turn_detection": None}
        kw = made[0].started
        assert kw["room_options"].audio_input is False, "a driven voice has no ears"
        assert kw["room_options"].audio_output.track_name == "agent-forge" and kw["record"] is False
        assert kw["agent"].instructions == f"You are Forge.\n\n{agent.DRIVEN}"
        assert [(m.role, m.text_content) for m in kw["agent"].chat_ctx.items] == [("user", "Priya: hi")]
    # The huddle hears its state from the session. What it said it reports itself, a unit at a time by the
    # unit's own text: the model's transcript of itself is not listened to.
    ev = made[0].handlers
    assert "conversation_item_added" not in ev
    ev["agent_state_changed"](SimpleNamespace(new_state="speaking"))
    live.say("On it. Three PRs are open.")
    await _settle()
    made[0].handles[-1].set_result(None)
    await _settle()
    live.interrupt()
    await _settle()
    ev["close"](None)
    assert [c[0] for c in huddle.method_calls] == [
        "on_agent_state", "may_say", "on_unit", "may_say", "on_unit", "on_playback_ended", "on_live_closed"
    ]  # fmt: skip
    assert [c.args[:5] for c in huddle.on_unit.call_args_list] == [
        ("forge", None, 0, "On it.", "said"), ("forge", None, 1, "Three PRs are open.", "cut")
    ]
    huddle.on_live_closed.assert_called_once_with(live)
    assert live._closed is not None, "a session that closed on its own stops billing"


def test_a_word_for_word_line_goes_to_the_model_without_the_conversation():
    # real model, 10-07: with an open question in view it answered the question, with a price of its own
    from unittest.mock import MagicMock

    from openai.types.realtime import RealtimeResponseCreateParams, ResponseCreateEvent

    session = object.__new__(agent._DrivenSession)
    session._msg_ch = MagicMock()

    def sent(instructions):
        params = RealtimeResponseCreateParams(instructions=instructions)
        session.send_event(ResponseCreateEvent(type="response.create", response=params))
        return session._msg_ch.send_nowait.call_args.args[0].response.model_dump(exclude_unset=True)

    # 10-08 spike: the line in the instructions alone came out silent on 5 of 52 short lines; as the one user
    # message of its response too, on none. It is the response's own input: the conversation is still not read.
    assert sent(f"persona\n{agent.SAY}Fifty dollars.")["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": 'Say exactly: "Fifty dollars."'}]}
    ]
    # 10-07 rig: with the call in view its own words answered the question ("it's $1,600")
    assert sent("persona\nTell Priya you are on it.")["input"] == []


async def test_what_a_mouth_said_is_deleted_and_an_order_waits_for_a_cut_response_to_end(monkeypatch):
    """10-08 spike: a `response.create` that raced a `response.cancel` failed outright. And nothing reads a
    mouth's conversation, so what it said is taken out of it: a long call does not pile it up."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from openai.types.realtime import RealtimeResponseCreateParams, ResponseCancelEvent, ResponseCreateEvent

    monkeypatch.setattr(agent.openai.realtime.RealtimeSession, "_handle_response_done", lambda self, event: None)
    monkeypatch.setattr(agent, "CANCEL_WAIT_S", 0.05)
    session = object.__new__(agent._DrivenSession)
    session._msg_ch, session._current_generation = MagicMock(), None

    def sent():
        out = [c.args[0] for c in session._msg_ch.send_nowait.call_args_list]
        session._msg_ch.send_nowait.reset_mock()
        return [(e.type, getattr(e, "item_id", None) or getattr(getattr(e, "response", None), "instructions", None)) for e in out]

    def order(line):
        params = RealtimeResponseCreateParams(instructions=agent.SAY + line)
        session.send_event(ResponseCreateEvent(type="response.create", response=params))

    cancel = ResponseCancelEvent(type="response.cancel")
    order("One.")
    assert sent() == [("response.create", agent.SAY + "One.")], "nothing is being cut: sent at once"
    session._current_generation = SimpleNamespace(messages={"item_1": object()})  # response.created
    session.send_event(cancel)
    order("Two.")
    assert sent() == [("response.cancel", None)], "held: the cut response has not ended"
    session._handle_response_done(None)
    assert sent() == [("conversation.item.delete", "item_1"), ("response.create", agent.SAY + "Two.")]
    session._current_generation = SimpleNamespace(messages={"item_2": object()})
    session._handle_response_done(None)  # said to its end
    assert sent() == [("conversation.item.delete", "item_2")], "every response's item goes, cut or not"
    session.send_event(cancel)
    session.send_event(cancel)
    order("Three.")
    assert sent() == [("response.cancel", None)] * 2
    await asyncio.sleep(0.1)
    assert sent() == [("response.create", agent.SAY + "Three.")], "its end never came: not held for ever"
    session._current_generation = None
    session.send_event(cancel)  # nothing to cut (the plugin's cancel for a response not yet created)
    order("Four.")
    assert sent() == [("response.cancel", None), ("response.create", agent.SAY + "Four.")]
    session.truncate(message_id="item_1", modalities=["audio"], audio_end_ms=200)
    assert sent() == [], "its item is deleted: there is nothing to truncate"


FIRST, HOLD = 0.1, 0.2  # FIRST_SOUND_S and CANCEL_WAIT_S in the tests below


def _wired(monkeypatch):
    """A mouth whose orders and cancels go through a real `_DrivenSession` (no network: its channel is a mock),
    with a model that never sends a frame and never answers a cancel: every hold lasts CANCEL_WAIT_S."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(agent, "FIRST_SOUND_S", FIRST)
    monkeypatch.setattr(agent, "CANCEL_WAIT_S", HOLD)
    monkeypatch.setattr(agent, "SEND_WAIT_S", 2 * HOLD)
    live, session, voice = _driven()
    wire = object.__new__(agent._DrivenSession)
    wire._msg_ch, wire._current_generation = MagicMock(), None
    session.wire = voice.realtime_llm_session = wire
    session.silent = True
    wire.creates = lambda: sum(c.args[0].type == "response.create" for c in wire._msg_ch.send_nowait.call_args_list)
    return live, session, wire


async def _until(done):
    import asyncio

    async def poll():
        while not done():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), 2)


async def _stopped_for_no_sound(live, session, wire, sent_before):
    """The newest order's create is held behind a cancelled response for longer than half FIRST_SOUND_S, and
    the unit is stopped for no sound FIRST_SOUND_S after the create is sent: the hold is no part of it."""
    import asyncio
    import time

    handle, receipts, ordered = session.handles[-1], len(live.receipts), time.monotonic()
    assert wire.creates() == sent_before and not handle.done(), "held: the cancelled response has not ended"
    await _until(lambda: wire.creates() == sent_before + 1)
    assert time.monotonic() - ordered > FIRST / 2, "(the hold is long enough to tell the two clocks apart)"
    await asyncio.sleep(FIRST / 2)
    assert not handle.interrupted, "half its first-sound time after the send: the hold was not counted"
    await asyncio.sleep(FIRST)
    assert handle.interrupted and live.receipts[receipts][3]["why"] == "no_sound"
    assert wire.sent is None, "the unit is over: a later create times nothing"


async def test_first_sound_is_timed_from_the_send_of_an_order_at_once_and_after_an_interrupt(monkeypatch):
    """#3917 review (P2): the first-sound time counted the wait for a cancelled response to end."""
    import asyncio

    live, session, wire = _wired(monkeypatch)
    live.say("A normal order.", 1)
    await _settle()
    assert wire.creates() == 1, "nothing to wait for: sent as it was ordered"
    await asyncio.sleep(FIRST / 2)
    assert not session.handles[0].interrupted
    live.interrupt()  # someone talks: its response is cancelled, and that never ends here
    await _settle()
    assert live.receipts[0][2] == "cut"
    live.say("After the interruption.", 2)
    await _settle()
    await _stopped_for_no_sound(live, session, wire, sent_before=1)
    assert live.ended == [(1, False), (2, True)]
    await _until(lambda: wire._cancelling is None)  # (order 2 stopped itself, which is a cancel too)
    live.say("An order with nothing before it.", 3)  # no hold: sent at once, and timed from then
    await _settle()
    assert wire.creates() == 3
    await asyncio.sleep(FIRST * 1.5)
    assert live.receipts[2][3]["why"] == "no_sound"


async def test_first_sound_is_timed_from_the_send_of_a_retry_after_the_unit_stopped_itself(monkeypatch):
    """#3920 review (P1): a unit that stops itself (`no_sound`, `long`) cancels its response with no interrupt
    from the worker, so nothing marked the cut and the retry's time ran through its hold."""
    live, session, wire = _wired(monkeypatch)
    live.say("Got it.", 1)
    await _until(lambda: live.receipts)
    assert live.receipts[0][3]["why"] == "no_sound" and live._cut_at is None, "no interrupt of the worker's"
    live.say("Got it.", 2)  # the worker's retry, at once (on_playback_ended)
    await _settle()
    await _stopped_for_no_sound(live, session, wire, sent_before=1)
    await _until(lambda: wire._cancelling is None)
    live.say("Got it.", 3)
    await _settle()
    live._agent.unit.stop("long")  # more sound than its words need: stopped by the meter, the same cancel
    await _settle()
    assert live.receipts[2][2] == "long"
    live.say("Got it.", 4)
    await _settle()
    await _stopped_for_no_sound(live, session, wire, sent_before=3)


async def test_first_sound_is_timed_from_the_send_of_the_first_order_of_a_parked_mouth(monkeypatch):
    """#3920 review (P1): parking a kept mouth interrupts it, and opening it again forgot when."""
    live, session, wire = _wired(monkeypatch)
    live.kept = {"forge": live}
    live.say("One.", 1)
    await _settle()
    await live.aclose()  # parked: what it was saying is cancelled
    await _settle()
    assert live.reopen() is live and live._cut_at is None and not session.closed
    live.say("Two.", 2)
    await _settle()
    await _stopped_for_no_sound(live, session, wire, sent_before=1)


async def test_an_order_whose_create_is_never_sent_is_stopped_and_says_so(monkeypatch):
    from unittest.mock import MagicMock

    live, session, wire = _wired(monkeypatch)
    wire._cancelling = MagicMock()  # a hold that never ends
    live.say("Got it.", 1)
    await _until(lambda: live.receipts)
    assert wire.creates() == 0 and live.receipts[0][2] == "silent" and live.receipts[0][3]["why"] == "unsent"
    assert live.ended == [(1, True)] and FIRST < live.receipts[0][3]["tookMs"] / 1000 < 2 * HOLD + FIRST


async def test_a_unit_with_no_sound_in_time_is_stopped_and_fails_and_says_why(monkeypatch):
    """10-08 rig: 3 of 83 units were silent, and each cost the room the 3 s the mouth then waited."""
    import asyncio

    assert agent.FIRST_SOUND_S == 1.0
    monkeypatch.setattr(agent, "FIRST_SOUND_S", 0.02)
    live, session, _ = _driven()
    session.silent = True
    live.say("Got it. Give me a moment.", 3)
    await asyncio.sleep(0.1)  # the model sends nothing, and never ends the response
    order, index, outcome, evidence = live.receipts[0]
    assert (order, index, outcome, evidence["why"]) == (3, 0, "silent", "no_sound") and session.handles[0].interrupted
    assert live.ended == [(3, True)] and len(session.orders) == 1, "failed: the worker says it again (on_playback_ended)"
    session.silent = False
    live.say("Got it.", 4)  # its next try: the same unit, a new order
    await _settle()
    session.handles[-1].set_result(None)
    await _settle()
    assert live.receipts[1][:3] == (4, 0, "said") and "why" not in live.receipts[1][3] and live.ended[1:] == [(4, False)]
    session.silent = True
    live.say("Got it.", 5)
    await _settle()
    session.handles[-1].set_result(None)  # it ended on its own with nothing audible
    await _settle()
    assert live.receipts[2][2] == "silent" and live.receipts[2][3]["why"] == "no_frames"


async def test_an_agents_mouth_opens_once_in_a_call_and_is_kept_until_the_call_ends(monkeypatch):
    """10-08 rig: 15 calls opened a voice session 24 times, 0.4-1.0 s each, and every silent unit was the
    first or second after an open."""
    from unittest.mock import MagicMock

    made = []
    monkeypatch.setattr(agent, "AgentSession", lambda **kw: made.append(DrivenSession(**kw)) or made[-1])
    monkeypatch.setattr(agent, "_DrivenModel", lambda **kw: kw)
    huddle, room, mouths = MagicMock(), MagicMock(), {}

    async def start(agent_id, mouths=mouths):
        return await agent.start_live(
            room, huddle, agent_id=agent_id, persona="You are it.", voice="cedar", speaker="meet:room-1:p1",
            seed=[], mouths=mouths,
        )  # fmt: skip

    live = await start("forge")
    live.say("One. Two.", 1)
    await _settle()
    await live.aclose()  # the worker closes its voice (the follow-up window, or another agent speaks)
    await _settle()
    assert not made[0].closed and made[0].interrupts == 1, "the session stays, and what it was saying stops"
    billed = live.usage_seconds()
    assert live._closed is not None and live.usage_seconds() == billed, "the worker's voice is billed to its close"
    live.say("Never.", 2)
    await _settle()
    assert len(made[0].orders) == 1, "closed: it says nothing until the worker opens it again"
    again = await start("forge")
    assert again is live and len(made) == 1 and live._closed is None, "the same session, with no open to wait for"
    live.say("Three.", 3)
    await _settle()
    made[0].handles[-1].set_result(None)
    await _settle()
    assert huddle.on_unit.call_args.args[:5] == ("forge", 3, 0, "Three.", "said")
    other = await start("friday")
    assert other is not live and len(made) == 2 and set(mouths) == {"forge", "friday"}, "one for each agent"
    await live.aclose()
    made[0].handlers["close"](None)  # it died while it was kept
    assert set(mouths) == {"friday"}
    assert await start("forge") is not live and len(made) == 3, "a dead one is not reused"
    await other.close()  # the call is over (entrypoint on_shutdown)
    assert made[1].closed and set(mouths) == {"forge"}
    alone = await start("forge", mouths=None)
    await alone.aclose()
    assert made[-1].closed, "a mouth nobody keeps closes as it always did"


def test_a_voice_the_realtime_model_lacks_maps_to_one_it_has_the_same_each_time():
    from huddle_agent.agent import REALTIME_VOICES, realtime_voice

    assert realtime_voice("marin") == "marin"
    assert realtime_voice("stone") in REALTIME_VOICES and realtime_voice("stone") == realtime_voice("stone")


# -- the meter, through a real session --


def _tone(level):
    import struct

    from livekit import rtc

    return rtc.AudioFrame(struct.pack("<480h", *([level] * 480)), 24000, 1, 480)  # 20 ms


def _real_session(levels):
    """A real AgentSession with a real `_Mouth`, and no network: the model only makes audio frames of the
    given levels, and the track is a list. Everything between the two is LiveKit's own code."""
    import asyncio

    from livekit.agents import AgentSession, llm
    from livekit.agents.voice import io

    class Track(io.AudioOutput):
        def __init__(self):
            super().__init__(label="test", capabilities=io.AudioOutputCapabilities(pause=False), sample_rate=24000)
            self.frames, self.playing = [], False

        async def capture_frame(self, frame):
            await super().capture_frame(frame)
            self.frames.append(frame)
            self.playing = True

        def flush(self):
            super().flush()
            if self.playing:
                self.playing = False
                self.on_playback_finished(playback_position=0.02 * len(self.frames), interrupted=False)

        def clear_buffer(self):
            if self.playing:
                self.playing = False
                self.on_playback_finished(playback_position=0.0, interrupted=True)

    class Realtime(llm.RealtimeSession):
        ctx = llm.ChatContext.empty()
        chat_ctx = property(lambda self: self.ctx)
        tools = property(lambda self: llm.ToolContext.empty())

        async def update_instructions(self, instructions): ...
        async def update_chat_ctx(self, chat_ctx): ...
        async def update_tools(self, tools): ...
        def update_options(self, **kw): ...
        def push_audio(self, frame): ...
        def push_video(self, frame): ...
        def commit_audio(self): ...
        def clear_audio(self): ...
        def interrupt(self): ...
        def truncate(self, **kw): ...
        async def aclose(self): ...

        def generate_reply(self, **kw):
            async def audio():
                for level in levels:
                    yield _tone(level)

            async def text():
                yield "ok"

            async def messages():
                modalities = asyncio.get_running_loop().create_future()
                modalities.set_result(["audio"])
                yield llm.MessageGeneration("m1", text(), audio(), modalities)

            async def no_calls():
                return
                yield

            created = asyncio.get_running_loop().create_future()
            created.set_result(llm.GenerationCreatedEvent(messages(), no_calls(), user_initiated=True))
            return created

    class Model(llm.RealtimeModel):
        def __init__(self):
            super().__init__(
                capabilities=llm.RealtimeCapabilities(
                    message_truncation=False, turn_detection=False, user_transcription=False,
                    auto_tool_reply_generation=False, audio_output=True, manual_function_calls=False,
                )
            )  # fmt: skip

        def session(self, *, turn_detection_disabled=False):
            return Realtime(self)

        async def aclose(self): ...

    session, track = AgentSession(llm=Model()), Track()
    session.output.audio = track
    return session, track


async def _say_through_a_real_session(levels, may=lambda order, index: True):
    import asyncio
    from unittest.mock import MagicMock

    session, track = _real_session(levels)
    mouth, receipts, ended = agent._Mouth(instructions="x"), [], []
    await session.start(agent=mouth, record=False)
    try:
        live = agent._DrivenLive(
            "forge", session, mouth, MagicMock(),
            said=lambda *receipt: receipts.append(receipt), ended=lambda *end: ended.append(end), may=may,
        )  # fmt: skip
        live.say("Got it.", 3)
        await asyncio.wait_for(live._tail, 5)
    finally:
        await session.aclose()
    return receipts, ended, track


async def test_the_session_passes_a_units_audio_through_the_mouths_meter():
    """10-09 review: the driven-voice tests write the meter's fields themselves (`DrivenSession.Handle`), so
    none of them showed that LiveKit calls the hook at all. Here nothing sets them: a real session takes
    the model's frames through `_Mouth.realtime_audio_output_node` to the track, and the receipt is what
    the hook counted. Not covered without a network: the OpenAI plugin's own session, and the room's track."""
    receipts, ended, track = await _say_through_a_real_session([4000] * 10)
    order, index, text, outcome, evidence = receipts[0]
    assert (order, index, text, outcome) == (3, 0, "Got it.", "said") and ended == [(3, False)]
    assert (evidence["frames"], evidence["audioMs"], evidence["loudMs"]) == (10, 200, 200), "counted by the hook"
    assert len(track.frames) == 10, "and every frame it counted went on to the track"


async def test_through_the_session_a_unit_with_far_more_sound_than_its_words_need_is_stopped_and_fails():
    """10-08 rig: 8 of 83 units were far longer than their text ("I have it." took 4.05 s). "Got it." may
    have max(1.5 s, 0.155 s a spoken character) of sound: 1.5 s, 75 frames of these."""
    receipts, ended, track = await _say_through_a_real_session([4000] * 200)
    order, index, text, outcome, evidence = receipts[0]
    assert (order, index, text, outcome, evidence["why"]) == (3, 0, "Got it.", "long", "long")
    assert ended == [(3, True)], "a failed playback: the worker tries the unit again"
    assert abs(len(track.frames) - 75) <= 1, "and no more of it reached the track"
    receipts, ended, track = await _say_through_a_real_session([4000] * 70)
    assert receipts[0][3] == "said" and "why" not in receipts[0][4] and len(track.frames) == 70, "1.4 s: said"


def test_the_sound_limit_is_of_the_line_as_it_is_spoken_not_as_it_is_written():
    """#3920 review (P2): by written characters, "$1,249.50" had the time of nine letters, was stopped as
    `long` while it was being said right, and was stopped again on every retry."""
    from huddle_agent.agent import LONG_MIN_S, LONG_PER_CHAR_S, sound_limit, spoken_chars

    assert (LONG_MIN_S, LONG_PER_CHAR_S) == (1.5, 0.155)
    assert spoken_chars("got it.") == 7 and spoken_chars("Got it.") == 9, "a capital may be a spelled letter: 3"
    assert sound_limit("Got it.") == 1.5 and round(sound_limit("I have it."), 2) == 1.86, '4.05 s of "I have it." is caught'
    money = "It costs $1,249.50 per month, or $14,994 a year."
    # aloud: "It costs one thousand two hundred forty-nine dollars and fifty cents per month, or fourteen
    # thousand nine hundred ninety-four dollars a year." 141 characters, 8.7 s at the rig's median 62 ms.
    assert len(money) == 48 and spoken_chars(money) == 162 and round(sound_limit(money), 1) == 25.1
    assert 0.112 * len(money) < 0.062 * 141 < sound_limit(money), "the old limit was under its normal length"
    assert spoken_chars("3 PRs") == 8 + 1 + 3 + 3 + 1 and spoken_chars("see example.com/a") == 4 + 7 + 8 + 3 + 8 + 1
    assert spoken_chars("I'm on it - well, \"yes\" (now).") == len("I'm on it - well, \"yes\" (now).") + 2


async def test_a_unit_stopped_as_long_is_not_limited_on_its_next_try_and_the_receipt_says_how_far_over(caplog):
    """#3920 review (P2): the retry had the limit that had just cut it, so a unit the estimate was wrong for
    was cut PLAYBACK_TRIES times and then went out in writing. The limit is for audio that runs away once."""
    import asyncio
    from unittest.mock import MagicMock

    session, track = _real_session([4000] * 200)  # 4 s of sound for "Got it.", every time
    mouth, receipts, ended = agent._Mouth(instructions="x"), [], []
    await session.start(agent=mouth, record=False)
    try:
        live = agent._DrivenLive(
            "forge", session, mouth, MagicMock(),
            said=lambda *receipt: receipts.append(receipt), ended=lambda *end: ended.append(end),
        )  # fmt: skip
        with caplog.at_level("WARNING"):
            for order, line in enumerate(("Got it.", "Got it.", "Got it.", "Got it.", "Sure, fine.", "Got it."), 1):
                live.say(line, order)
                await asyncio.wait_for(live._tail, 5)
    finally:
        await session.aclose()
    assert [r[3] for r in receipts] == ["long", "said", "long", "said", "long", "long"]
    assert ended == [(1, True), (2, False), (3, True), (4, False), (5, True), (6, True)]
    # its first try is stopped at 1.5 s. Its second plays out, and says so. Said, it is limited again.
    assert "overMs" not in receipts[0][4] and receipts[1][4]["loudMs"] == 4000 and receipts[1][4]["overMs"] == 2500
    assert sum("over its sound limit" in r.getMessage() for r in caplog.records) == 2
    assert receipts[5][3] == "long", "another unit was tried between: its own next try is limited as a first"


async def test_through_the_session_silent_frames_are_a_failed_unit_and_a_revoked_one_sends_nothing():
    receipts, ended, track = await _say_through_a_real_session([0] * 10)
    assert receipts[0][3] == "silent" and (receipts[0][4]["frames"], receipts[0][4]["loudMs"]) == (10, 0)
    assert receipts[0][4]["why"] == "quiet", "frames came, with no sound in them"
    assert ended == [(3, True)], "frames with no sound in them: the hook saw it, and the order failed"
    asked = []
    # allowed before the unit is ordered, and not when its first audio is about to go out
    receipts, ended, track = await _say_through_a_real_session(
        [4000] * 10, may=lambda order, index: asked.append((order, index)) or len(asked) == 1
    )
    assert asked == [(3, 0), (3, 0)] and receipts[0][3] == "revoked" and receipts[0][4]["frames"] == 0
    assert track.frames == [], "the hook is where the audio stops: nothing reached the track"
    asked = []
    # allowed when ordered and at its first (quiet) frame, and not before its first frame with sound in it
    receipts, ended, track = await _say_through_a_real_session(
        [0, 0, 4000, 4000], may=lambda order, index: asked.append((order, index)) or len(asked) < 3
    )
    assert len(asked) == 3 and receipts[0][3] == "revoked" and receipts[0][4]["loudMs"] == 0
    assert [max(f.data) for f in track.frames] == [0, 0], "the quiet lead passed, and no sound reached the track"
    assert ended == [(3, False)], "paused as not said: a refusal is not a failed playback"


async def test_one_deployed_agent_routes_a_phone_call_a_meet_and_a_huddle(monkeypatch):
    """The LiveKit plan allows one deployed agent: `lightbulb-huddle` takes phone calls (the SIP dispatch rule's
    `phone-` room prefix), Meets and in-app huddles."""
    import json
    from types import SimpleNamespace

    went = []

    async def to(where, *_):
        went.append(where)

    monkeypatch.setattr(agent.phone, "entrypoint", lambda ctx: to("phone"))
    monkeypatch.setattr(agent.meet, "run", lambda ctx, meta: to("meet"))
    monkeypatch.setattr(agent.record, "start", lambda room_id: went.append("huddle") or 1 / 0)

    def job(room, meta):
        return SimpleNamespace(job=SimpleNamespace(room=SimpleNamespace(name=room), metadata=meta))

    await agent.entrypoint(job("phone-prod-_+15551234567_aB3x", ""))  # a SIP dispatch carries no metadata
    await agent.entrypoint(job("meet-prod-r1", json.dumps({"source": "meet", "roomId": "r1"})))
    try:
        await agent.entrypoint(job("huddle-prod-r1", json.dumps({"roomId": "r1", "ownerId": "u", "roomToken": "t"})))
    except ZeroDivisionError:
        pass
    assert went == ["phone", "meet", "huddle"]


def test_no_retiring_openai_models():
    # developers.openai.com/api/docs/deprecations: gpt-realtime and -mini shut down 20 Jan 2027, gpt-4o-mini-transcribe 26 Feb 2027
    import pathlib
    import re

    src = pathlib.Path(agent.__file__).parent
    bad = re.compile(r"gpt-realtime(?![-\w.])|gpt-realtime-mini|gpt-4o-mini-transcribe|gpt-4o-transcribe")
    hits = [f"{p.name}:{n}" for p in src.glob("*.py") for n, line in enumerate(p.read_text().splitlines(), 1)
            if bad.search(line) and not line.lstrip().startswith("#") and "deprecated" not in line and "shut" not in line]
    assert not hits, hits


def test_default_engines_are_openai(monkeypatch):
    import importlib

    from huddle_agent import meet, phone

    for k in ("PHONE_ENGINE", "MEET_ENGINE", "OWNER_ENGINE", "PHONE_MODEL", "MEET_MODEL", "OWNER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(phone)
    importlib.reload(meet)
    assert phone.OPENAI and phone.MODEL == "gpt-realtime-2.1-mini"
    assert meet.OPENAI and meet.MODEL == "gpt-realtime-2.1-mini"
    from huddle_agent import owner

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert owner.realtime_model().model == "gpt-realtime-2.1"
