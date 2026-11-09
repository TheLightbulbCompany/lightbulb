"""Owner mode (owner.py) on GPT-Live: GPT-Live is the voice, the DM's Router the brain. Each delegation is posted once
as the member's woken message; the DM's agent messages come back as results. The shared formatting tests below remain with mouth.py
(Task 5 deletes both)."""

import asyncio
import inspect
import types

import pytest

from huddle_agent import live as live_module
from huddle_agent import mouth, owner
from huddle_agent.backend import BackendError


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_API_URL", "http://backend.test")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s")
    monkeypatch.setenv("OPENAI_API_KEY", "k")


class _Backend:
    def __init__(self, delay=0.0, polls=()):
        self.sent, self._delay, self.polls = [], delay, list(polls)

    async def owner(self, op, body):
        await asyncio.sleep(self._delay)
        self.sent.append((op, body))
        if op == "events":
            return (
                self.polls.pop(0)
                if self.polls
                else {"messages": [], "after": body["after"]}
            )
        return {"messageId": f"m{len(self.sent)}"} if op == "turn" else {}


class _LiveSession:
    def __init__(self, kw):
        self.kw, self.closed, self.session, self.close_s = kw, 0, None, 0
        self.billed_s = 0.0

    async def close(self):
        await asyncio.sleep(
            self.close_s
        )  # live.close() lets posts in flight finish first
        self.closed += 1


@pytest.fixture
def live(monkeypatch):
    """live.start stands in (no GPT-Live connection): it records each session it was asked to open."""
    sessions = []

    async def start(
        ctx, *, prompt, surface, post, events, room_options=None, on_spoken=None
    ):
        sessions.append(_LiveSession({"prompt": prompt, "surface": surface, "post": post, "events": events,
                                      "room_options": room_options, "on_spoken": on_spoken}))  # fmt: skip
        return sessions[-1]

    monkeypatch.setattr(live_module, "start", start)
    monkeypatch.setattr(owner, "POLL_S", 0)
    return types.SimpleNamespace(sessions=sessions)


def _talk(backend, after=2):
    return owner.Talk(backend, "user_maya", "job_1", after)


async def test_orb_spoken_line_reaches_router_once():
    b = _Backend()
    t = _talk(b)
    turns = [
        "user: Hi",
        "assistant: Hi Maya!",
        "user: What have we shipped in the last week?",
    ]  # live.py's shape
    mid = await t.post("What have we shipped in the last week?", turns)
    assert mid == "m1"
    [(op, body)] = b.sent
    assert op == "turn" and (body["speakerUserId"], body["sessionId"], body["n"]) == (
        "user_maya",
        "job_1",
        0,
    )
    assert body["text"] == ("What have we shipped in the last week?\n\nSaid in the talk since the last ask:\n"
                            "> user: Hi\n> assistant: Hi Maya!")  # fmt: skip
    await t.post("And the week before?", [])
    assert b.sent[1][1]["text"] == "And the week before?" and b.sent[1][1]["n"] == 1


async def test_orb_posts_in_order_and_a_failure_does_not_stop_the_next():
    sent = []

    class B:
        async def owner(self, op, body):
            await asyncio.sleep(0.01 if body["n"] == 0 else 0)
            sent.append(body)
            if body["n"] == 1:
                raise BackendError(500, "x", None)
            return {"messageId": "m"}

    t = _talk(B())
    out = await asyncio.gather(
        *(
            t.post(text, [])
            for text in ("Draft the post", "On second thought", "Thanks")
        )
    )
    assert [(b["n"], b["text"]) for b in sent] == [
        (0, "Draft the post"),
        (1, "On second thought"),
        (2, "Thanks"),
    ]
    assert out == ["m", None, "m"]
    assert set(t.asked) == {"m"}  # a failed post waits for nothing


async def test_orb_router_reply_sent_as_result(live):
    polls = [
        {"messages": [], "after": 2},  # the session opens
        {"messages": [{"id": "m1", "seq": 5, "text": "Checking GitHub.", "inReplyTo": "h1"}], "after": 5},
        {"messages": [{"id": "m1", "seq": 5, "text": "Checking GitHub.", "inReplyTo": "h1"},
                      {"id": "m2", "seq": 7, "text": "We shipped #5095.", "inReplyTo": "h1"}], "after": 7},
        {"messages": [], "after": 7},
    ]  # fmt: skip
    b = _Backend(polls=polls)
    t = _talk(b)
    got, gen = [], t.results()
    got.append(await anext(gen))
    await t.spoken("m1")  # live.py's on_spoken
    got.append(await anext(gen))
    await t.spoken("m2")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(gen), 0.05)  # polls once more, nothing new
    await gen.aclose()
    assert got == [{"kind": "agent", "id": "m1", "seq": 5, "text": "Checking GitHub.", "inReplyTo": "h1"},
                   {"kind": "agent", "id": "m2", "seq": 7, "text": "We shipped #5095.", "inReplyTo": "h1"}]  # fmt: skip
    asked = [(body["after"], body["spoken"]) for op, body in b.sent if op == "events"]
    assert asked[:4] == [
        (2, 2),
        (2, 2),
        (5, 5),
        (7, 7),
    ]  # `spoken` is reported, so the next Live starts after it


async def test_orb_hears_only_voice_answers_that_landed_while_it_was_closed(
    live, monkeypatch
):
    monkeypatch.setattr(owner, "POLL_S", 10)  # one poll per open here
    b = _Backend(polls=[{"messages": [], "after": 3},
                        {"messages": [{"id": "t1", "seq": 5, "text": "A typed reply.", "inReplyTo": "typed"},
                                      {"id": "q0", "seq": 6, "text": "No reply ref."},
                                      {"id": "w1", "seq": 9, "text": "Pulse: the PR is merged.", "inReplyTo": "m1"}],
                         "after": 9}])  # fmt: skip
    t = _talk(b)
    assert await t.post("Merge the PR", []) == "m1"
    first = t.results()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(first), 0.01)
    await (
        first.aclose()
    )  # the orb closed; a typed reply and the Worker's answer land meanwhile
    second = t.results()  # the next open
    assert (await anext(second))[
        "id"
    ] == "w1"  # the spoken ask's answer, never the typed reply
    await second.aclose()
    assert [body["after"] for op, body in b.sent if op == "events"][
        1
    ] == 3  # the room's cursor


async def test_orb_waits_wait_s_from_each_ask_whatever_replies(live, monkeypatch):
    b = _Backend(polls=[{"messages": [{"id": "y", "seq": 5, "text": "Started a thread.", "inReplyTo": "m1"}],
                         "after": 5}])  # fmt: skip
    t = _talk(b)
    woke = []
    t.rewake = lambda: woke.append(1)
    monkeypatch.setattr(owner, "WAIT_S", 0.05)
    assert await t.post("Fix the deploy", []) == "m1" and t.waiting()
    gen = t.results()
    await anext(gen)  # the Router's ack: the Worker's answer is still to come
    await gen.aclose()
    assert t.waiting()
    await asyncio.sleep(0.2)
    assert not t.waiting() and woke == [1]  # WAIT_S from the post: Quiet re-arms


async def test_orb_has_no_approve_path(live):
    b = _Backend(polls=[{"messages": [], "after": 2}, {"messages": [{"id": "q1", "seq": 3, "text": "**Vera** asks: approve the refund?",
                                       "question": {"taskId": "t1", "qid": "q-2"}}], "after": 3}])  # fmt: skip
    t = _talk(b)
    gen = t.results()
    await anext(gen)
    await gen.aclose()
    await t.post("Yes, approve it.", [])
    await t.post("And what else?", [])
    turns = [body for op, body in b.sent if op != "events"]
    assert [op for op, _ in b.sent if op != "events"] == ["turn", "turn"]
    # the line said after a question carries its ref; the backend refuses an approval by voice (answer_said_question)
    assert (
        turns[0]["answers"] == {"taskId": "t1", "qid": "q-2"}
        and "answers" not in turns[1]
    )
    assert '"approve"' not in inspect.getsource(
        owner
    )  # no route of its own to approve with


def test_orb_prompt_has_no_business_facts(live):
    text = owner.prompt()
    assert text.startswith("You are Lightbulb,")
    for heading in (
        "Backchannel policy:",
        "Interruption policy:",
        "Delegation policy:\nBackend tools:\n",
    ):
        assert heading in text
    for tool in owner.ROUTER_TOOLS:
        assert f"- {tool}" in text
    assert (
        "on the card" in text
        and "Wait for the backend's result before stating a fact" in text
    )
    assert "Speak only English" in text
    # nothing about this business: no name, owner, date or slot
    assert inspect.signature(owner.prompt).parameters == {} and "{" not in text
    for fact in ("Nova", "Maya", "Sunday", "business"):
        assert fact not in text


class _Pub:
    def __init__(self, muted):
        self.kind, self.muted = owner.rtc.TrackKind.KIND_AUDIO, muted


class _Ctx:
    def __init__(self):
        self.handlers, self.rpc, self.shutdowns, self.data, self.callbacks = (
            {},
            {},
            [],
            [],
            [],
        )
        self.member = types.SimpleNamespace(
            identity="user_maya", name="Maya", track_publications={}
        )
        self.unpublished = []
        lp = types.SimpleNamespace(
            set_attributes=self._noop, publish_data=self._data, unpublish_track=self._unpublish,
            register_rpc_method=lambda name, fn: self.rpc.__setitem__(name, fn), track_publications={},
        )  # fmt: skip
        self.room = types.SimpleNamespace(
            local_participant=lp, on=lambda ev, fn: self.handlers.setdefault(ev, fn)
        )
        self.job = types.SimpleNamespace(id="job_1")

    async def _noop(self, *_a, **_k):
        return None

    async def _data(self, payload, **_k):
        self.data.append(payload)

    async def _unpublish(self, sid):
        self.unpublished.append(sid)
        self.room.local_participant.track_publications.pop(sid)

    async def connect(self):
        pass

    async def wait_for_participant(self):
        return self.member

    def add_shutdown_callback(self, cb):
        self.callbacks.append(cb)

    def shutdown(self, reason=""):
        self.shutdowns.append(reason)

    def mic(self, live):
        self.member.track_publications = (
            {"a": _Pub(muted=not live)} if live is not None else {}
        )
        self.handlers["track_published"]()


async def test_orb_opens_gpt_live_only_while_the_mic_is_live_and_closes_it(
    live, monkeypatch
):
    class B(_Backend):
        async def owner(self, op, body):
            return (
                {"assistantName": "Nova", "after": 2}
                if op == "start"
                else await super().owner(op, body)
            )

        async def aclose(self):
            pass

    b = B()
    monkeypatch.setattr(owner, "Backend", lambda *_a: b)
    ctx = _Ctx()
    await owner.run(ctx, {"roomId": "r1", "ownerId": "o1", "roomToken": "t"})
    assert live.sessions == []  # the warm room, no mic: nothing billed
    ctx.mic(True)
    await asyncio.sleep(0)
    [s] = live.sessions
    assert (s.kw["surface"], s.kw["prompt"]) == ("owner", owner.prompt())
    assert (
        s.kw["room_options"].audio_output.track_name == "agent-assistant"
    )  # the strip's bars read it
    ctx.mic(True)  # a repeat does not open a second session
    await asyncio.sleep(0)
    assert len(live.sessions) == 1
    lp = ctx.room.local_participant
    lp.track_publications["TR_1"] = types.SimpleNamespace(
        sid="TR_1", name="agent-assistant"
    )
    ctx.mic(
        False
    )  # the orb closed: GPT-Live closes, and its track goes, so the strip finds the next one
    await asyncio.sleep(0.01)
    assert s.closed == 1 and ctx.unpublished == ["TR_1"]
    ctx.mic(True)
    await asyncio.sleep(0)
    assert len(live.sessions) == 2 and live.sessions[1].kw["on_spoken"] is not None
    live.sessions[1].close_s = 0.03
    lp.track_publications["TR_2"] = types.SimpleNamespace(
        sid="TR_2", name="agent-assistant"
    )
    ctx.mic(False)
    ctx.mic(True)  # a quick reopen while the old session is still closing
    await asyncio.sleep(0)
    lp.track_publications["TR_3"] = types.SimpleNamespace(
        sid="TR_3", name="agent-assistant"
    )  # the new voice
    await asyncio.sleep(0.05)
    assert (
        live.sessions[1].closed == 1
        and ctx.unpublished == ["TR_1", "TR_2"]
        and "TR_3" in lp.track_publications
    )
    for cb in ctx.callbacks:  # the room left: closed too, and the talk kept
        await cb()
    assert live.sessions[2].closed == 1 and [op for op, _ in b.sent][-1] == "end"


async def test_orb_closes_gpt_live_when_the_talk_goes_quiet(live, monkeypatch):
    class B(_Backend):
        async def owner(self, op, body):
            return (
                {"assistantName": "Nova", "after": 2}
                if op == "start"
                else await super().owner(op, body)
            )

    monkeypatch.setattr(owner, "Backend", lambda *_a: B())
    monkeypatch.setattr(owner, "QUIET_S", 0.02)
    ctx = _Ctx()
    await owner.run(ctx, {"roomId": "r1", "ownerId": "o1", "roomToken": "t"})
    ctx.mic(True)
    await asyncio.sleep(0.05)  # mic live, nobody spoke: quiet
    [s] = live.sessions
    assert s.closed == 1 and '"quiet_ended"' in ctx.data[-1]
    ctx.mic(
        True
    )  # the mic is still live until the web closes it: no new billed session
    await asyncio.sleep(0)
    assert len(live.sessions) == 1
    ctx.mic(False)
    ctx.mic(True)  # the next tap
    await asyncio.sleep(0)
    assert len(live.sessions) == 2
    ctx.mic(False)


async def test_owner_usage_waits_for_all_closes_and_excludes_muted_room_time(live, monkeypatch):
    class B(_Backend):
        async def owner(self, op, body):
            return {"assistantName": "Nova"} if op == "start" else await super().owner(op, body)

        async def aclose(self):
            pass

    b, ctx = B(), _Ctx()
    monkeypatch.setattr(owner, "Backend", lambda *_a: b)
    await owner.run(ctx, {"roomId": "r1", "ownerId": "o1", "roomToken": "t"})
    ctx.mic(True)
    await asyncio.sleep(0)
    live.sessions[0].billed_s, live.sessions[0].close_s = 12.4, 0.03
    ctx.mic(False)
    ctx.mic(True)
    await asyncio.sleep(0)
    live.sessions[1].billed_s = 7.8
    await ctx.callbacks[0]()  # the first session is still closing when the room ends
    await ctx.callbacks[0]()  # repeated shutdown cannot double-close or double-report
    assert [s.closed for s in live.sessions] == [1, 1]
    [(_, body)] = [(op, body) for op, body in b.sent if op == "end"]
    assert body["durationSec"] == 0 and body["transcript"] == []
    assert sorted((u["id"], u["seconds"]) for u in body["usageSessions"]) == [("0", 12), ("1", 7)]
    assert all(u["startedAt"].endswith("+00:00") for u in body["usageSessions"])


def test_a_message_is_cleaned_for_speech():
    assert (
        mouth.speakable(
            "**Shipped** [#5095](https://github.com/x/pull/5095): tap to talk."
        )
        == "Shipped 5095: tap to talk."
    )
    assert mouth.speakable("- one\n- two") == "one two"
    assert len(mouth.speakable("x" * 5000)) == mouth.SPOKEN_CHARS


async def test_quiet_ends_the_talk_only_while_the_mic_is_live():
    ended = []
    q = owner.Quiet(
        lambda: ended.append("quiet"),
        lambda: ended.append("away"),
        quiet_s=0.02,
        away_s=0.05,
    )
    q.update(mic_live=False, user="listening", agent="listening")
    await asyncio.sleep(0.03)
    assert ended == []
    q.update(
        mic_live=True, user="listening", agent="thinking"
    )  # waiting on a delegated answer: no count
    await asyncio.sleep(0.03)
    assert ended == []
    q.update(mic_live=True, user="listening", agent="listening")
    q.update(
        mic_live=True, user="listening", agent="listening"
    )  # a repeat does not restart the count
    await asyncio.sleep(0.01)
    q.update(
        mic_live=True, user="speaking", agent="listening"
    )  # the member talks: reset
    q.update(mic_live=True, user="listening", agent="listening")
    await asyncio.sleep(0.015)
    assert ended == []
    await asyncio.sleep(0.02)
    assert ended == ["quiet"]
    q.update(mic_live=False, user="listening", agent="listening")
    await asyncio.sleep(0.07)
    assert ended == ["quiet", "away"]
    q.cancel()


def test_the_talk_is_kept_with_its_transcript():
    items = [{"role": "assistant", "text": "We shipped 5095.", "t": 2.0, "interrupted": False},
             {"role": "user", "text": "What shipped?", "t": 0.5, "interrupted": False}]  # fmt: skip
    assert owner.talk_record("user_maya", items, "job_1", 42.7) == {
        "speakerUserId": "user_maya", "sessionId": "job_1", "durationSec": 42,
        "transcript": [{"who": "caller", "text": "What shipped?", "t": 0.5},
                       {"who": "assistant", "text": "We shipped 5095.", "t": 2.0}],
    }  # fmt: skip
