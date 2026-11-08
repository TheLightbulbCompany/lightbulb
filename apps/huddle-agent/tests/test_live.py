"""live.py: GPT-Live (client delegation) is the voice, the Router is the brain. Tested against a fake transport using
the Task 0 contract's shapes (docs/superpowers/specs/2026-11-08-gpt-live-contract.md)."""

import asyncio
import itertools
import pathlib
import re
from types import SimpleNamespace

from livekit import rtc

from huddle_agent import live

_ids = itertools.count()


class FakeTransport:
    """Stands in for the plugin's GPTLiveSession plus the agent's chat context."""

    def __init__(self):
        self.items: list[tuple[str, str, str]] = []  # (id, role, text), the chat context
        self.appends: list[tuple[str, str | None]] = []  # (text, delegation_id)
        self.closed = False
        self.billed_s = 0.0

    def say(self, role, text):  # a turn lands in the chat context
        self.items.append((f"msg_{next(_ids)}", role, text))

    def history(self):
        return list(self.items)

    def commentary(self, text, delegation_id):
        self.appends.append((text, delegation_id))

    async def close(self):
        self.closed = True
        self.billed_s = 23.0


def delegation(i, pending=" What have we shipped this week"):
    # contract §2: {"id": "item_…", "type": "delegation", "target": "client"} -> GPTLiveDelegation(id, pending_transcript)
    return SimpleNamespace(id=f"item_{i}", pending_transcript=pending)


class Router:
    """`post` returns the posted message's id, as /turn and /ask return messageId."""

    def __init__(self, delay=0.05):
        self.posts, self.done, self.delay = [], [], delay

    async def post(self, text, turns):
        self.posts.append((text, turns))
        await asyncio.sleep(self.delay)
        self.done.append(text)
        return f"m_{len(self.posts)}"


async def _none():
    return
    yield


def queue_events():
    q: asyncio.Queue = asyncio.Queue()

    async def events():
        while True:
            yield await q.get()

    return q, events


def session(router, events=None, transport=None, on_spoken=None):
    return live.LiveSession(transport or FakeTransport(), "owner", router.post, events or _none, on_spoken)


async def test_delegation_posts_once_and_returns_immediately():
    r = Router(delay=0.2)
    s = session(r)
    s.on_delegate(delegation(1))
    assert r.done == []  # returned before post resolved
    await asyncio.sleep(0.3)
    assert len(r.posts) == 1 and r.posts[0][0] == "What have we shipped this week"
    await s.close()


async def test_ask_joins_chat_context_with_partial_pending_transcript():
    # contract §4, run 1: the turn split; "What have we shipped this" went to the chat context, pending was " week"
    t = FakeTransport()
    t.say("user", "Hi, can you hear me?")
    t.say("assistant", "Yeah, I can hear you fine.")
    t.say("user", "What have we shipped this")
    r = Router(delay=0)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, pending=" week"))
    await asyncio.sleep(0.01)
    text, turns = r.posts[0]
    assert text == "What have we shipped this week"
    assert turns == ["user: Hi, can you hear me?", "assistant: Yeah, I can hear you fine.",
                     "user: What have we shipped this"]  # fmt: skip
    # the next ask carries only what was said since this one
    t.say("assistant", "Let me check.")
    t.say("user", "And last week?")
    s.on_delegate(delegation(2, pending=""))
    await asyncio.sleep(0.01)
    assert r.posts[1] == ("And last week?", ["assistant: Let me check.", "user: And last week?"])
    await s.close()


async def test_item_inserted_earlier_by_created_at_is_still_sent_once():
    t = FakeTransport()
    t.say("user", "first")
    r = Router(delay=0)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, ""))
    t.items.insert(0, ("late", "assistant", "an earlier line, inserted by created_at"))
    t.say("user", "second")
    s.on_delegate(delegation(2, ""))
    await asyncio.sleep(0.01)
    assert r.posts[1][1] == ["assistant: an earlier line, inserted by created_at", "user: second"]
    await s.close()


async def test_pending_transcript_already_in_context_is_not_doubled():
    t = FakeTransport()
    t.say("user", "What have we shipped this week")
    r = Router(delay=0)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, pending=" What have we shipped this week"))
    await asyncio.sleep(0.01)
    assert r.posts[0][0] == "What have we shipped this week"
    await s.close()


async def test_two_pending_delegations_both_answered_in_order():
    t = FakeTransport()
    r = Router(delay=0.02)
    q, events = queue_events()
    s = session(r, events=events, transport=t)
    s.on_delegate(delegation(1, "first ask"))
    s.on_delegate(delegation(2, "second ask"))
    await asyncio.sleep(0.1)
    assert [p[0] for p in r.posts] == ["first ask", "second ask"]  # posted in the order asked
    await q.put({"kind": "agent", "id": "a1", "inReplyTo": "m_1", "text": "first answer"})
    await q.put({"kind": "agent", "id": "a2", "inReplyTo": "m_2", "text": "second answer"})
    await asyncio.sleep(0.02)
    assert t.appends == [("first answer", "item_1"), ("second answer", "item_2")]
    await s.close()


async def test_interim_line_does_not_steal_the_next_delegations_answer():
    # review I1: an ack for ask 1, then ask 1's real answer, must both go to delegation 1, never to delegation 2
    t = FakeTransport()
    q, events = queue_events()
    s = session(Router(delay=0), events=events, transport=t)
    s.on_delegate(delegation(1, "a"))
    s.on_delegate(delegation(2, "b"))
    await asyncio.sleep(0.02)
    for m in ({"kind": "agent", "id": "x1", "inReplyTo": "m_1", "text": "On it."},
              {"kind": "agent", "id": "x2", "inReplyTo": "m_1", "text": "Answer to a."},
              {"kind": "agent", "id": "x3", "inReplyTo": "m_2", "text": "Answer to b."}):  # fmt: skip
        await q.put(m)
    await asyncio.sleep(0.02)
    assert t.appends == [("On it.", "item_1"), ("Answer to a.", "item_1"), ("Answer to b.", "item_2")]
    await s.close()


async def test_unmatched_agent_message_is_unsolicited_never_a_pending_answer():
    # review I1: a DM reply to a typed message (or any message with no matching inReplyTo) is not the voice's answer
    t = FakeTransport()
    q, events = queue_events()
    s = session(Router(delay=0), events=events, transport=t)
    s.on_delegate(delegation(1, "a"))
    await asyncio.sleep(0.02)
    await q.put({"kind": "agent", "id": "y1", "inReplyTo": "typed_msg", "text": "Reply to a typed message."})
    await q.put({"kind": "agent", "id": "y2", "text": "A Worker's update."})
    await asyncio.sleep(0.02)
    assert t.appends == [("Reply to a typed message.", None), ("A Worker's update.", None)]
    assert "item_1" in s.pending
    await s.close()


async def test_post_returning_none_drops_the_delegation():
    # the Meet lane refuses in its own post when the speaker never named the assistant
    t = FakeTransport()
    posted = []

    async def post(text, turns):
        posted.append(text)
        return None if text == "not for you" else "m_ok"

    s = live.LiveSession(t, "meet", post, _none)
    s.on_delegate(delegation(1, "not for you"))
    s.on_delegate(delegation(2, "Nova, what's next?"))
    await asyncio.sleep(0.02)
    assert posted == ["not for you", "Nova, what's next?"]
    assert s.pending == {"item_2": "m_ok"}
    s.send_result("item_1", "a team message")
    assert t.appends == [("a team message", None)]
    await s.close()


async def test_explicit_id_answers_that_delegation():
    t = FakeTransport()
    s = session(Router(delay=0), transport=t)
    s.on_delegate(delegation(1, "a"))
    s.on_delegate(delegation(2, "b"))
    s.send_result("item_2", "answer b")
    s.send_result(None, "update")
    assert t.appends == [("answer b", "item_2"), ("update", None)]
    await s.close()


async def test_send_result_none_is_unsolicited_and_keeps_the_pending_delegation():
    # the phone's opening, disclosure correction and goodbye go out with None while a delegation waits
    t = FakeTransport()
    s = session(Router(delay=0), transport=t)
    s.on_delegate(delegation(1, "a"))
    await asyncio.sleep(0.01)
    s.send_result(None, "Thanks for calling, goodbye.")
    assert t.appends == [("Thanks for calling, goodbye.", None)] and s.pending == {"item_1": "m_1"}
    s.send_result("item_1", "answer a")
    assert t.appends[-1] == ("answer a", "item_1")
    await s.close()


async def test_router_message_becomes_result():
    t = FakeTransport()
    q, events = queue_events()
    spoken = []

    async def on_spoken(mid):
        spoken.append(mid)

    s = session(Router(delay=0), events=events, transport=t, on_spoken=on_spoken)
    s.on_delegate(delegation(1))
    await asyncio.sleep(0.01)
    for item in ({"kind": "agent", "id": "e1", "inReplyTo": "m_1", "text": "We shipped three things."},
                 {"kind": "agent", "id": "e1", "inReplyTo": "m_1", "text": "We shipped three things."},  # twice
                 {"kind": "user", "id": "e2", "text": "typed by the member"},
                 {"kind": "agent", "id": "e3", "text": ""}):  # fmt: skip
        await q.put(item)
    await asyncio.sleep(0.05)
    assert t.appends == [("We shipped three things.", "item_1")]
    assert spoken == ["e1"]  # reported spoken once, after the append
    await s.close()


async def test_events_loop_retries_after_a_failure(monkeypatch):
    # review I2: the first failure must not end results for the rest of the talk
    monkeypatch.setattr(live, "EVENTS_RETRY_S", 0.01)
    t = FakeTransport()
    calls = []

    async def events():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("backend 502")
        yield {"kind": "agent", "id": "z1", "text": "back again"}
        await asyncio.Event().wait()

    s = session(Router(delay=0), events=events, transport=t)
    await asyncio.sleep(0.2)
    assert len(calls) == 3 and t.appends == [("back again", None)]
    await s.close()


async def test_results_are_made_speakable_once():
    t = FakeTransport()
    s = session(Router(delay=0), transport=t)
    msg = (
        "**Booked.** See [the PR](https://github.com/x/y/pull/1) or https://example.com/raw\n"
        "```python\nprint('code')\n```\n"
        "Which time?\n```openui-lang\nChoice(\"time\", [\"1:00 PM\", \"2:30 PM\", \"4:00 PM\"])\n```"
    )
    s.send_result(None, msg)
    (said, _),  = t.appends
    assert said == "Booked. See the PR or Which time? 1:00 PM, 2:30 PM or 4:00 PM."
    s.send_result(None, said)  # phone.py already calls speakable: a second pass changes nothing
    assert t.appends[1][0] == said
    s.send_result(None, "word " * 400)
    assert len(t.appends[2][0]) <= 600  # mouth.SPOKEN_CHARS
    await s.close()


def test_chunks_split_under_the_append_cap():
    text = " ".join(f"Sentence number {i} says a thing." for i in range(200))
    chunks = live._chunks(text)
    assert len(chunks) > 1 and all(len(c) <= live.APPEND_CHARS for c in chunks) and " ".join(chunks) == text


async def test_close_waits_for_posts_and_ends_the_session():
    r = Router(delay=0.1)
    t = FakeTransport()
    s = session(r, transport=t)
    s.on_delegate(delegation(1))
    await s.close()
    assert r.done == ["What have we shipped this week"] and t.closed and s.billed_s == 23.0


async def test_a_failed_post_does_not_break_the_next():
    calls = []

    async def post(text, turns):
        calls.append(text)
        if text == "a":
            raise RuntimeError("backend down")
        return "m_b"

    s = live.LiveSession(FakeTransport(), "phone", post, _none)
    s.on_delegate(delegation(1, "a"))
    s.on_delegate(delegation(2, "b"))
    await asyncio.sleep(0.01)
    assert calls == ["a", "b"] and s.pending == {"item_2": "m_b"}  # a failed post answers nothing
    await s.close()


# start() and _GPTLive against stubs of the plugin: the AgentSession, the agent and its duplex session


class StubDuplex(rtc.EventEmitter):
    def __init__(self):
        super().__init__()
        self.appends = []

    def append_commentary(self, text, *, delegation_id=None):
        self.appends.append((text, delegation_id))


class StubAgent:
    def __init__(self, instructions, chat_ctx=None):
        self.instructions, self.duplex_session = instructions, StubDuplex()
        self.chat_ctx = chat_ctx or SimpleNamespace(items=[])


class StubSession(rtc.EventEmitter):
    def __init__(self, **kw):
        super().__init__()
        self.kw, self.started, self.closed, self.interrupted = kw, None, False, 0

    async def start(self, **kw):
        self.started = kw

    def interrupt(self):
        self.interrupted += 1

    async def aclose(self):
        self.closed = True
        self.emit("close", SimpleNamespace(reason="closed"))


class StubModel:
    def __init__(self, **kw):
        self.kw, self.closed = kw, False

    async def aclose(self):
        self.closed = True


def _msg(i, role, text):
    return SimpleNamespace(id=i, type="message", role=role, text_content=text)


async def _start(monkeypatch, router, events=_none, **kw):
    monkeypatch.setattr(live, "GPTLiveModel", StubModel)
    monkeypatch.setattr(live, "Agent", StubAgent)
    monkeypatch.setattr(live, "AgentSession", StubSession)
    ctx = SimpleNamespace(room="the-room")
    return await live.start(ctx, prompt="the prompt", surface="owner", post=router.post, events=events, **kw)


async def test_start_wires_gpt_live_to_the_router(monkeypatch):
    r = Router(delay=0)
    q, events = queue_events()
    s = await _start(monkeypatch, r, events)
    assert s.transport.model.kw == {"delegation": "client"}
    assert s.agent.instructions == "the prompt"
    assert s.session.started == {"agent": s.agent, "room": "the-room", "record": False}
    duplex = s.agent.duplex_session
    s.agent.chat_ctx.items += [_msg("u1", "user", "What have we shipped this"),
                               SimpleNamespace(id="f1", type="function_call", role=None, text_content=None)]  # fmt: skip
    duplex.emit("delegation_created", SimpleNamespace(id="item_A", pending_transcript=" week"))
    await asyncio.sleep(0.01)
    assert r.posts == [("What have we shipped this week", ["user: What have we shipped this"])]
    await q.put({"kind": "agent", "id": "r1", "inReplyTo": "m_1", "text": "Three things."})
    await asyncio.sleep(0.01)
    assert duplex.appends == [("Three things.", "item_A")]
    duplex.emit("session_reconnected", SimpleNamespace())  # the new session never asked item_A
    assert s.pending == {}
    duplex.emit("metrics_collected", SimpleNamespace(session_duration=12.0))
    duplex.emit("metrics_collected", SimpleNamespace(session_duration=8.0))
    await s.close()
    assert s.session.closed and s.transport.model.closed and s.billed_s == 20.0


async def test_start_passes_record(monkeypatch):
    s = await _start(monkeypatch, Router(delay=0), record=True)
    assert s.session.started["record"] is True and s.session.kw["user_away_timeout"] is None
    await s.close()


async def test_start_passes_user_away_timeout(monkeypatch):
    s = await _start(monkeypatch, Router(delay=0), user_away_timeout=15.0)
    assert s.session.kw == {"llm": s.transport.model, "vad": None, "user_away_timeout": 15.0}
    await s.close()


def test_startup_context_names_speakers_and_keeps_the_newest_under_the_cap():
    ctx = live.startup_context([("Maya", "Let's ship Friday."), ("Sam", "  "), ("Raj", "Agreed.")])
    assert [(i.role, i.text_content) for i in ctx.items] == [("user", "Maya: Let's ship Friday."),
                                                               ("user", "Raj: Agreed.")]  # fmt: skip
    many = [("Maya", f"line {i}") for i in range(300)]
    ctx = live.startup_context(many)
    assert len(ctx.items) == live.HISTORY_ITEMS and ctx.items[-1].text_content == "Maya: line 299"
    long = [("Sam", "x" * 5000) for _ in range(10)]
    ctx = live.startup_context(long)
    assert sum(len(i.text_content) for i in ctx.items) <= live.HISTORY_CHARS and len(ctx.items) == 4


async def test_start_loads_history_as_startup_context_and_never_posts_it(monkeypatch):
    r = Router(delay=0)
    s = await _start(monkeypatch, r, history=[("Maya", "We moved the launch."), ("Raj", "To Friday.")])
    assert [i.text_content for i in s.agent.chat_ctx.items] == ["Maya: We moved the launch.", "Raj: To Friday."]
    s.agent.chat_ctx.add_message(role="user", content="Nova, when is the launch?")
    s.agent.duplex_session.emit("delegation_created", SimpleNamespace(id="item_1", pending_transcript=""))
    await asyncio.sleep(0.01)
    assert r.posts == [("Nova, when is the launch?", ["user: Nova, when is the launch?"])]
    await s.close()


async def test_session_close_event_stops_results_and_keeps_usage(monkeypatch):
    s = await _start(monkeypatch, Router(delay=0), queue_events()[1])
    s.agent.duplex_session.emit("metrics_collected", SimpleNamespace(session_duration=5.0))
    s.session.emit("close", SimpleNamespace(reason="participant left"))
    await asyncio.sleep(0)
    assert s._events.cancelled() and s.billed_s == 5.0
    await s.close()


async def test_interrupt_never_cancels_post(monkeypatch):
    r = Router(delay=0.1)
    s = await _start(monkeypatch, r)
    duplex = s.agent.duplex_session
    duplex.emit("delegation_created", SimpleNamespace(id="item_1", pending_transcript="do the thing"))
    await asyncio.sleep(0.01)
    # the caller cuts in while the voice speaks: the model stops itself (the app sends nothing), the orb tap
    # interrupts the session, and the caller's new line delegates again
    duplex.emit("input_speech_started", SimpleNamespace())
    s.session.interrupt()
    s.agent.chat_ctx.items.append(_msg("u2", "user", "Wait, hold on"))
    duplex.emit("delegation_created", SimpleNamespace(id="item_2", pending_transcript=""))
    await asyncio.sleep(0.3)
    assert s.session.interrupted == 1
    assert r.done == ["do the thing", "Wait, hold on"]
    assert s.pending == {"item_1": "m_1", "item_2": "m_2"}
    await s.close()


def test_no_scripted_speech():
    src = pathlib.Path(live.__file__).read_text()
    assert not re.search(r"\.(say|generate_reply|speak)\(", src)
    # results reach the model only as commentary built from the Router's text
    assert re.findall(r"append_\w+\(", src) == ["append_commentary("]


def test_delegation_policy_renders_the_template():
    p = live.delegation_policy(["The Router: the team's brain.", "Memory: recalls what the team decided."])
    assert p.startswith("Delegation policy:\nBackend tools:\n- The Router: the team's brain.\n- Memory:")
    for label in ("Generate <|SpawnThinking|> when:", "Do not generate <|SpawnThinking|> when:",
                  "Do not guess the result while waiting."):  # fmt: skip
        assert label in p
