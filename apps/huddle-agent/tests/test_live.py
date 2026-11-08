"""live.py: GPT-Live (client delegation) is the voice, the Router is the brain. Tested against a fake transport using
the Task 0 contract's shapes (docs/superpowers/specs/2026-11-08-gpt-live-contract.md)."""

import asyncio
import pathlib
import re
from types import SimpleNamespace

from huddle_agent import live


class FakeTransport:
    """Stands in for the plugin's GPTLiveSession plus the agent's chat context."""

    def __init__(self):
        self.items: list[tuple[str, str]] = []  # (role, text), the chat context
        self.appends: list[tuple[str, str | None]] = []  # (text, delegation_id)
        self.closed = False
        self.billed_s = 0.0

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
    def __init__(self, delay=0.05):
        self.posts, self.done, self.delay = [], [], delay

    async def post(self, text, turns):
        self.posts.append((text, turns))
        await asyncio.sleep(self.delay)
        self.done.append(text)
        return f"turn_{len(self.posts)}"


def session(router, events=None, transport=None):
    async def none():
        return
        yield

    return live.LiveSession(
        transport or FakeTransport(), "owner", router.post, events or none
    )


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
    t.items = [("user", "Hi, can you hear me?"), ("assistant", "Yeah, I can hear you fine."),
               ("user", "What have we shipped this")]  # fmt: skip
    r = Router(delay=0)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, pending=" week"))
    await asyncio.sleep(0.01)
    text, turns = r.posts[0]
    assert text == "What have we shipped this week"
    assert turns == ["user: Hi, can you hear me?", "assistant: Yeah, I can hear you fine.",
                     "user: What have we shipped this"]  # fmt: skip
    # the next ask carries only what was said since this one
    t.items += [("assistant", "Let me check."), ("user", "And last week?")]
    s.on_delegate(delegation(2, pending=""))
    await asyncio.sleep(0.01)
    assert r.posts[1] == (
        "And last week?",
        ["assistant: Let me check.", "user: And last week?"],
    )
    await s.close()


async def test_pending_transcript_already_in_context_is_not_doubled():
    t = FakeTransport()
    t.items = [("user", "What have we shipped this week")]
    r = Router(delay=0)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, pending=" What have we shipped this week"))
    await asyncio.sleep(0.01)
    assert r.posts[0][0] == "What have we shipped this week"
    await s.close()


async def test_two_pending_delegations_both_answered_in_order():
    t = FakeTransport()
    r = Router(delay=0.05)
    s = session(r, transport=t)
    s.on_delegate(delegation(1, "first ask"))
    s.on_delegate(delegation(2, "second ask"))
    await asyncio.sleep(0.2)
    assert [p[0] for p in r.posts] == [
        "first ask",
        "second ask",
    ]  # posted in the order asked
    s.send_result(None, "first answer")
    s.send_result(None, "second answer")
    s.send_result(None, "a team update")
    assert t.appends == [
        ("first answer", "item_1"),
        ("second answer", "item_2"),
        ("a team update", None),
    ]
    await s.close()


async def test_explicit_id_answers_that_delegation():
    t = FakeTransport()
    s = session(Router(delay=0), transport=t)
    s.on_delegate(delegation(1, "a"))
    s.on_delegate(delegation(2, "b"))
    s.send_result("item_2", "answer b")
    s.send_result(None, "answer a")
    assert t.appends == [("answer b", "item_2"), ("answer a", "item_1")]
    await s.close()


async def test_router_message_becomes_result():
    t = FakeTransport()
    q: asyncio.Queue = asyncio.Queue()

    async def events():
        while True:
            yield await q.get()

    s = session(Router(delay=0), events=events, transport=t)
    s.on_delegate(delegation(1))
    for item in ({"kind": "agent", "id": "m1", "text": "We shipped three things."},
                 {"kind": "agent", "id": "m1", "text": "We shipped three things."},  # same message twice
                 {"kind": "user", "id": "m2", "text": "typed by the member"},
                 {"kind": "agent", "id": "m3", "text": ""}):  # fmt: skip
        await q.put(item)
    await asyncio.sleep(0.05)
    assert t.appends == [("We shipped three things.", "item_1")]
    await s.close()


async def test_long_result_is_split_under_the_append_cap():
    t = FakeTransport()
    s = session(Router(delay=0), transport=t)
    s.on_delegate(delegation(1))
    text = " ".join(f"Sentence number {i} says a thing." for i in range(200))
    s.send_result(None, text)
    assert len(t.appends) > 1 and all(
        len(a) <= live.APPEND_CHARS and d == "item_1" for a, d in t.appends
    )
    assert " ".join(a for a, _ in t.appends) == text
    await s.close()


async def test_interrupt_never_cancels_post():
    r = Router(delay=0.1)
    t = FakeTransport()
    s = session(r, transport=t)
    s.on_delegate(delegation(1, "do the thing"))
    t.items.append(
        ("user", "Wait, hold on")
    )  # the caller cuts in; the model stops itself, the app sends nothing
    s.on_delegate(delegation(2, ""))
    await asyncio.sleep(0.3)
    assert r.done == ["do the thing", "Wait, hold on"]
    await s.close()


async def test_close_waits_for_posts_and_ends_the_session():
    r = Router(delay=0.1)
    t = FakeTransport()
    s = session(r, transport=t)
    s.on_delegate(delegation(1))
    await s.close()
    assert (
        r.done == ["What have we shipped this week"] and t.closed and s.billed_s == 23.0
    )


async def test_a_failed_post_does_not_break_the_next():
    calls = []

    async def post(text, turns):
        calls.append(text)
        if text == "a":
            raise RuntimeError("backend down")
        return ""

    s = live.LiveSession(FakeTransport(), "phone", post, lambda: _empty())
    s.on_delegate(delegation(1, "a"))
    s.on_delegate(delegation(2, "b"))
    await asyncio.sleep(0.01)
    assert calls == ["a", "b"]
    await s.close()


async def _empty():
    return
    yield


def test_no_scripted_speech():
    src = pathlib.Path(live.__file__).read_text()
    assert not re.search(r"\.(say|generate_reply|speak)\(", src)
    # results reach the model only as commentary built from the Router's text
    assert re.findall(r"append_\w+\(", src) == ["append_commentary("]


def test_delegation_policy_renders_the_template():
    p = live.delegation_policy(
        ["The Router: the team's brain.", "Memory: recalls what the team decided."]
    )
    assert p.startswith(
        "Delegation policy:\nBackend tools:\n- The Router: the team's brain.\n- Memory:"
    )
    for label in ("Generate <|SpawnThinking|> when:", "Do not generate <|SpawnThinking|> when:",
                  "Do not guess the result while waiting."):  # fmt: skip
        assert label in p
