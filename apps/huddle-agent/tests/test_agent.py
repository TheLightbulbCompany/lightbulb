from livekit.agents import APIConnectionError, APIError

from huddle_agent import agent
from huddle_agent.agent import SEMANTIC_VAD, SERVER_VAD, retry_without_semantic_vad


class Engine:
    def __init__(self):
        self.updates = []

    def update_options(self, **kw):
        self.updates.append(kw)


def test_semantic_vad_is_what_the_pinned_plugin_validates():
    from livekit.plugins.openai.stt import _as_turn_detection

    assert _as_turn_detection(SEMANTIC_VAD).type == "semantic_vad"
    assert _as_turn_detection(SERVER_VAD).silence_duration_ms == 700


def test_a_refused_semantic_vad_falls_back_to_server_vad_once(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "emit_metric", lambda name, *a, **k: seen.append(name))
    engine, state = Engine(), {"vad": "semantic"}
    refused = APIError("OpenAI Realtime STT error: invalid turn_detection", body={}, retryable=False)
    assert retry_without_semantic_vad(engine, refused, "semantic", state)
    assert state == {"vad": "server"} and engine.updates == [{"turn_detection": SERVER_VAD}]
    assert retry_without_semantic_vad(engine, refused, "semantic", state)  # a second stream refused too: just reopen
    assert engine.updates == [{"turn_detection": SERVER_VAD}] and seen == ["live.stt.semantic_vad_rejected"]
    assert not retry_without_semantic_vad(engine, refused, "server", state)  # server_vad failing is a real error


def test_connection_errors_are_not_a_vad_refusal():
    engine, state = Engine(), {"vad": "semantic"}
    assert not retry_without_semantic_vad(engine, APIConnectionError("down"), "semantic", state)
    assert not retry_without_semantic_vad(engine, RuntimeError("boom"), "semantic", state)
    assert state == {"vad": "semantic"} and engine.updates == []


def test_a_muted_voice_is_muted_again_after_a_reconnect():
    # a reconnect is a new GPT-Live session, which hears: a meeting voice must not start hearing the Meet
    from unittest.mock import MagicMock

    duplex, handlers = MagicMock(), {}
    duplex.on.side_effect = lambda event, cb: handlers.__setitem__(event, cb)
    live = agent._Live("forge", MagicMock(), duplex, MagicMock())
    handlers["session_reconnected"](None)
    assert duplex.mute_input.call_count == 0, "a Live voice hears its speaker: never muted by a reconnect"
    live.mute()
    handlers["session_reconnected"](None)
    assert duplex.mute_input.call_count == 2


async def test_a_meeting_voice_is_muted_as_its_session_starts():
    # review: the huddle's mute came after session.start returned; the Meet could reach the model while it opened
    from unittest.mock import MagicMock

    session = MagicMock(spec=agent.GPTLiveSession)
    for muted, calls in ((True, 1), (False, 0)):
        session.reset_mock()
        voice = agent._Voice("forge", "persona", MagicMock(), [], muted=muted)
        type(voice).duplex_session = property(lambda self: session)
        try:
            await voice.on_enter()
        finally:
            del type(voice).duplex_session
        assert session.mute_input.call_count == calls


def test_the_meet_bots_page_is_never_a_person():
    from types import SimpleNamespace

    from livekit import rtc

    standard, ingress = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, rtc.ParticipantKind.PARTICIPANT_KIND_INGRESS
    assert not agent.person(SimpleNamespace(identity="meetbot:room-1", kind=standard))
    for identity in ("meet:room-1", "meet:room-1:p1"):  # a mixed Meet, and one person of it
        assert agent.person(SimpleNamespace(identity=identity, kind=ingress))
    assert agent.person(SimpleNamespace(identity="u1", kind=standard))
    assert not agent.person(SimpleNamespace(identity="agent-x", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))


def test_a_meeting_voices_rule_is_applied_again_after_a_reconnect():
    """Codex on #3792: a reconnect rebuilds the session from its first instructions, without the rule that
    keeps a per-person voice out of its person's talk to others."""
    from unittest.mock import MagicMock

    duplex, handlers = MagicMock(), {}
    duplex.on.side_effect = lambda event, cb: handlers.__setitem__(event, cb)
    live = agent._Live("forge", MagicMock(), duplex, MagicMock())
    handlers["session_reconnected"](None)
    assert duplex.append_instructions.call_count == 0, "no rule, nothing to re-apply"
    live.meet_rule("You hear only Priya.")
    live.meet_rule("You hear only Dan.")  # the floor moved: the newest rule is the one in force
    handlers["session_reconnected"](None)
    assert [c.args[0] for c in duplex.append_instructions.call_args_list] == [
        "You hear only Priya.", "You hear only Dan.", "You hear only Dan."
    ]


class DrivenSession:
    """An AgentSession as a driven voice uses it: each order is a handle the test finishes by hand."""

    def __init__(self, llm=None, vad=None):
        import asyncio

        class Handle(asyncio.Future):  # a SpeechHandle: awaited to its end, and it says whether it was cut
            interrupted = False

        self.llm, self.orders, self.handles, self.handlers, self.started = llm, [], [], {}, {}
        self.interrupts, self.closed, self._future = 0, False, Handle

    def on(self, event, fn):
        self.handlers[event] = fn

    async def start(self, **kw):
        self.started = kw

    def generate_reply(self, **kw):
        self.orders.append(kw)
        self.handles.append(self._future())
        return self.handles[-1]

    def interrupt(self, force=False):
        assert force
        self.interrupts += 1
        for h in self.handles:
            if not h.done():  # the speech in the air ends cut, as LiveKit's does
                h.interrupted = True
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
    said = []
    live = agent._DrivenLive(
        "forge", session, voice, MagicMock(), said=lambda text, interrupted: said.append((text, interrupted))
    )
    live.said = said  # its reports
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


async def test_what_a_gpt_live_voice_is_told_makes_a_mouth_say_nothing():
    live, session, voice = _driven()
    live.append_commentary("Tell Priya you are on it.")
    live.append_thinking("Priya asked: what shipped", delegation_id="d1")
    live.nudge()
    await _settle()
    assert session.orders == [], "no order"
    assert list(voice.ctx.items) == [], "and nothing is kept for one"


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


async def test_a_standing_rule_is_never_said_and_survives_a_stop():
    live, session, voice = _driven()
    live.say("one")
    live.append_instructions("Speak to Priya.")
    live.meet_rule("Speak to Dan.")
    await _settle()
    live.interrupt()  # a stop drops speech, not a standing rule
    await _settle()
    assert len(session.orders) == 1
    assert voice.instructions == "persona\n\nSpeak to Priya.\n\nSpeak to Dan."
    for name in ("mute", "hear", "set_participant"):  # it has no ears
        getattr(live, name)(*((0.0,) if name == "hear" else ("u2",) if name == "set_participant" else ()))
    assert len(session.orders) == 1 and session.interrupts == 1


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


def test_gpt_live_keeps_the_protocol_say_in_its_own_words_and_hear_is_nothing():
    from unittest.mock import MagicMock

    duplex = MagicMock()
    live = agent._Live("forge", MagicMock(), duplex, MagicMock())
    live.say("Three PRs merged.")
    live.hear(0.0)
    duplex.append_commentary.assert_called_once_with("Three PRs merged.")


async def test_a_meet_speaker_gets_a_driven_voice_and_anyone_else_gpt_live(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    made = []
    monkeypatch.setattr(agent, "AgentSession", lambda **kw: made.append(DrivenSession(**kw)) or made[-1])
    monkeypatch.setattr(agent, "_DrivenModel", lambda **kw: kw)
    monkeypatch.setattr(agent, "_TrimmedGPTLiveModel", lambda **kw: "gpt-live")
    monkeypatch.setattr(agent, "_Voice", lambda *a, **kw: SimpleNamespace(duplex_session=MagicMock()))
    huddle, room = MagicMock(), MagicMock()

    async def start(speaker):
        return await agent.start_live(
            room, huddle, None, agent_id="forge", persona="You are Forge.", voice="cedar", speaker=speaker,
            seed=[("user", "Priya: hi")],
        )

    for speaker in ("meet:room-1:p1", "meet:room-1"):  # a person of a Meet, and a mixed Meet
        made.clear()
        live = await start(speaker)
        assert isinstance(live, agent._DrivenLive) and live.track_name == "agent-forge"
        assert made[0].llm == {"model": "gpt-realtime", "voice": "cedar", "turn_detection": None}
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
    assert [c[0] for c in huddle.method_calls] == ["on_agent_state", "on_agent_said", "on_agent_said", "on_live_closed"]
    assert [c.args for c in huddle.on_agent_said.call_args_list] == [
        ("forge", "On it.", False), ("forge", "Three PRs are open.", True)
    ]
    huddle.on_live_closed.assert_called_once_with(live)
    assert live._closed is not None, "a session that closed on its own stops billing"

    made.clear()
    live = await start("user_1")
    assert isinstance(live, agent._Live) and made[0].llm == "gpt-live"
    assert "conversation_item_added" in made[0].handlers, "GPT-Live is heard by its own transcript"
    assert made[0].started["room_options"].audio_input is not False


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

    assert sent(f"persona\n{agent.SAY}Fifty dollars.")["input"] == []
    # 10-07 rig: with the call in view its own words answered the question ("it's $1,600")
    assert sent("persona\nTell Priya you are on it.")["input"] == []


def test_a_voice_the_realtime_model_lacks_maps_to_one_it_has_the_same_each_time():
    from huddle_agent.agent import REALTIME_VOICES, realtime_voice

    assert realtime_voice("marin") == "marin"
    assert realtime_voice("stone") in REALTIME_VOICES and realtime_voice("stone") == realtime_voice("stone")
