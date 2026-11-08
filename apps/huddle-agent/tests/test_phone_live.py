"""The phone on GPT-Live (plan 2026-11-08-gpt-live-voice, Task 4): GPT-Live talks, the Router decides. The call is
driven end to end through `phone.entrypoint` with a fake LiveKit job, a fake backend and a fake `live.start`."""

import asyncio
import inspect
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

from huddle_agent import phone  # noqa: E402

INFO = {"callId": "call-1", "assistantName": "Nova", "businessName": "Glow Nails", "today": "Sunday",
        "greeting": "Thanks for calling Glow Nails!", "mode": "live", "after": 4}  # fmt: skip


class Emitter:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event, fn=None):
        def add(f):
            self.handlers.setdefault(event, []).append(f)
            return f

        return add(fn) if fn else add

    def emit(self, event, ev):
        for f in self.handlers.get(event, []):
            f(ev)


class FakeLive:
    def __init__(self):
        self.session = Emitter()
        self.session.user_state, self.session.agent_state = "listening", "listening"
        self.sent: list[tuple] = []
        self.pending: dict[str, str | None] = {}
        self.closed = 0

    def send_result(self, delegation_id, text):
        self.sent.append((delegation_id, text))

    async def close(self):
        self.closed += 1


class FakeBackend:
    replies: dict = {}

    def __init__(self):
        self.call_id, self.calls = "", []

    async def post(self, path, body=None):
        self.calls.append((path, body or {}))
        if path == "/internal/voice/calls":
            return dict(INFO)
        out = self.replies.get(path, {})
        return out.pop(0) if isinstance(out, list) else out


class Call:
    """One answered call: the job, the session `live.start` returned and what reached the backend."""

    def __init__(self, monkeypatch, info=None, replies=None):
        self.backend = FakeBackend()
        FakeBackend.replies = replies or {}
        if info:
            monkeypatch.setattr(FakeBackend, "post", _with_info(info))
        monkeypatch.setattr(phone, "Backend", lambda: self.backend)
        self.live, self.started = FakeLive(), {}

        async def start(ctx, **kw):
            self.started = kw
            return self.live

        monkeypatch.setattr(phone.live, "start", start)
        self.caller = SimpleNamespace(identity="sip_caller", attributes={"sip.trunkPhoneNumber": "14842079294",
                                                                         "sip.phoneNumber": "16105550100"})  # fmt: skip
        self.transfers, self.shutdowns, self.on_shutdown, self.deleted = [], [], [], 0
        room = Emitter()
        room.name = "phone-dev-_+16105550100_ab"

        async def connect():
            pass

        async def wait():
            return self.caller

        async def transfer(req):
            self.transfers.append(req.transfer_to)

        async def delete_room():
            self.deleted += 1

        self.ctx = SimpleNamespace(
            connect=connect, wait_for_participant=wait, room=room, job=SimpleNamespace(id="j1", metadata=""),
            api=SimpleNamespace(sip=SimpleNamespace(transfer_sip_participant=transfer)), delete_room=delete_room,
            shutdown=self.shutdowns.append, add_shutdown_callback=self.on_shutdown.append,
        )  # fmt: skip

    async def answer(self):
        await phone.entrypoint(self.ctx)
        return self

    def paths(self):
        return [p for p, _ in self.backend.calls]

    def item(self, role, text, t, interrupted=False):
        ev = SimpleNamespace(item=SimpleNamespace(role=role, text_content=text, interrupted=interrupted,
                                                  metrics={"started_speaking_at": 1000.0 + t}, created_at=1000.0 + t))  # fmt: skip
        self.live.session.emit("conversation_item_added", ev)

    def state(self, old, new):
        self.live.session.emit(
            "agent_state_changed", SimpleNamespace(old_state=old, new_state=new)
        )


def _with_info(info):
    async def post(self, path, body=None):
        self.calls.append((path, body or {}))
        if path == "/internal/voice/calls":
            return {**INFO, **info}
        out = self.replies.get(path, {})
        return out.pop(0) if isinstance(out, list) else out

    return post


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(phone, "POLL_S", 0)
    monkeypatch.setattr(phone, "CONNECT_S", 0)
    monkeypatch.setattr(phone, "OPENING_GAP_S", 0)
    monkeypatch.setattr(phone.time, "time", lambda: 1000.0)


async def test_phone_opening_carries_three_facts(monkeypatch):
    """The first utterance names the assistant, says "AI assistant" and "this call is recorded": asked for in the
    prompt and once after start (the docs' greeting: commentary with delegation_id null), checked from the
    transcript by DisclosureGuard, which has the exact sentence said when the opening missed it."""
    call = await Call(monkeypatch).answer()
    assert call.started["surface"] == "phone"
    opts = call.started[
        "room_options"
    ]  # the SIP caller's audio, telephony noise cancellation, ends on hang-up
    assert opts.participant_identity == "sip_caller" and opts.close_on_disconnect
    assert call.started["record"] is True and call.started["user_away_timeout"] == phone.AWAY_S
    prompt = call.started["prompt"]
    (first_id, opening), *_ = call.live.sent
    for said in (prompt, opening):
        assert (
            "Nova" in said
            and "AI assistant" in said
            and "this call is recorded" in said
        )
    assert first_id is None
    call.state("listening", "speaking")
    call.item(
        "assistant", "Hi there, thanks for calling Glow Nails, how can I help?", 1.0
    )
    call.state("speaking", "listening")
    await asyncio.sleep(0.01)  # the opening's turn is over
    fix = [t for d, t in call.live.sent[1:] if d is None]
    assert (
        len(fix) == 1
        and '"I\'m Nova, an AI assistant, and this call is recorded."' in fix[0]
    )

    good = await Call(monkeypatch).answer()
    good.state("listening", "speaking")
    good.item(
        "assistant",
        "Hi, I'm Nova, Glow Nails's AI assistant, and this call is recorded. How can I help?",
        1.0,
    )
    good.state("speaking", "listening")
    await asyncio.sleep(0.01)
    assert len(good.live.sent) == 1  # only the opening: nothing corrected
    good.ctx.room.emit("participant_disconnected", good.caller)
    await asyncio.sleep(0.01)
    (end,) = [b for p, b in good.backend.calls if p == "end"]
    assert end["disclosure"] == "ok" and end["disclosureRepeated"] is False


async def test_phone_caller_talks_over_opening(monkeypatch):
    """Review Focus 4: the caller's "hi" cuts the opening before its disclosure. The prompt asks GPT-Live to finish
    the disclosure first; when the transcript shows it did not, the guard has the sentence said once. The caller's
    line itself is GPT-Live's to answer: code posts nothing for it."""
    call = await Call(monkeypatch).answer()
    assert "finish saying" in call.started["prompt"]
    call.state("listening", "speaking")
    call.item("user", "Hi, I need to book a pedicure", 1.5)
    call.item("assistant", "Hi, this is Nova, an AI", 1.0, interrupted=True)
    call.state("speaking", "listening")
    call.item("assistant", "Sure, let me check.", 3.0)
    fixes = [t for d, t in call.live.sent[1:] if d is None]
    assert (
        len(fixes) == 1
        and "I'm Nova, an AI assistant, and this call is recorded." in fixes[0]
    )
    assert (
        "turn" not in call.paths()
    )  # GPT-Live delegates the request; code never routes a line


async def test_the_opening_said_in_bursts_is_judged_whole(monkeypatch):
    """The opening's name, then a pause, then the rest: one turn, judged once it is over (review M5)."""
    monkeypatch.setattr(phone, "OPENING_GAP_S", 0.05)
    call = await Call(monkeypatch).answer()
    call.state("listening", "speaking")
    call.item("assistant", "Hi, thanks for calling Glow Nails, I'm Nova.", 1.0)
    call.state("speaking", "listening")
    await asyncio.sleep(0.01)
    call.state("listening", "speaking")
    call.item("assistant", "I'm an AI assistant and this call is recorded. How can I help?", 2.0)
    call.state("speaking", "listening")
    await asyncio.sleep(0.1)
    assert len(call.live.sent) == 1  # the opening only: the two bursts disclose together


async def test_phone_person_and_message_are_delegations_with_kind(monkeypatch):
    """ "Talk to a person" and "leave a message" are GPT-Live delegations like any other: posted to the caller's
    `/turn` (the Router, then the call's open thread), never matched in our code. The backend decides the kind:
    the Router's transfer_call line (`action: "transfer"`) is said, then the existing /transfer route and the SIP
    transfer run; a message is the Front desk's own `message`; a paused line's not_allowed is /message."""
    call = await Call(monkeypatch, replies={"turn": {"messageId": "m9"}}).answer()
    post = call.started["post"]
    assert await post("Can I talk to a real person?", []) == "m9"
    await post(
        "I'd like to leave a message for Sam",
        ["Caller: hi", "Nova: Hi, how can I help?"],
    )
    turns = [b for p, b in call.backend.calls if p == "turn"]
    assert [b["n"] for b in turns] == [0, 1]
    assert turns[0]["text"] == "Can I talk to a real person?"
    assert (
        turns[1]["text"].startswith("I'd like to leave a message for Sam\n\n")
        and "> Nova: Hi" in turns[1]["text"]
    )
    assert "transfer" not in call.paths() and "message" not in call.paths()
    for gone in ("PERSON", "MESSAGE", "YES", "YES_ONLY", "CANCEL", "HOLD_AGAIN", "OFFER_MESSAGE", "STILL_THERE",
                 "Receptionist", "Hold"):  # fmt: skip
        assert not hasattr(phone, gone), gone
    assert "re.compile" not in inspect.getsource(phone.entrypoint)

    FakeBackend.replies = {"events": [{"messages": [{"id": "m10", "seq": 5, "text": "Putting you through.",
                                                     "action": "transfer"},
                                                    {"id": "m11", "seq": 6, "text": "Closed Sundays."}],
                                       "after": 6}],
                           "transfer": {"transferTo": "+16105559999"}}  # fmt: skip
    events = call.started["events"]()
    said = [await anext(events), await anext(events)]
    assert said == [{"kind": "agent", "id": "m10", "seq": 5, "text": "Putting you through.", "action": "transfer"},
                    {"kind": "agent", "id": "m11", "seq": 6, "text": "Closed Sundays."}]  # fmt: skip
    await asyncio.sleep(0.01)
    assert call.transfers == ["tel:+16105559999"]
    assert [b for p, b in call.backend.calls if p == "events"][0] == {"after": 4}

    paused = await Call(monkeypatch, info={"mode": "message_only", "callerName": "Priya"},
                        replies={"turn": {"error": "not_allowed"}, "message": {"taken": True}}).answer()  # fmt: skip
    assert await paused.started["post"]("Tell Sam I'll be late", []) is None
    (msg,) = [b for p, b in paused.backend.calls if p == "message"]
    assert msg == {
        "text": "Tell Sam I'll be late",
        "name": "Priya",
        "callbackNumber": "",
    }
    assert "only takes messages" in paused.started["prompt"]
    assert any(d is None and "message" in t for d, t in paused.live.sent[1:])


async def test_nobody_to_take_the_call_is_told_to_gpt_live(monkeypatch):
    call = await Call(monkeypatch, replies={"transfer": {"transferTo": None}}).answer()
    FakeBackend.replies["events"] = [{"messages": [{"id": "m1", "seq": 5, "text": "One moment.", "action": "transfer"}], "after": 5},
                                     {"messages": [], "after": 5}]  # fmt: skip
    task = asyncio.ensure_future(anext(call.started["events"]()))
    await asyncio.sleep(0.02)
    task.cancel()
    assert call.transfers == [] and any(
        d is None and "Nobody" in t for d, t in call.live.sent[1:]
    )


async def test_phone_hangup_ends_session_and_billing(monkeypatch):
    """Review Focus 5: the caller hanging up closes the GPT-Live session (billed per open second) and posts the
    end once, billed from answer; the job's later shutdown awaits the same end."""
    call = await Call(monkeypatch).answer()
    call.ctx.room.emit(
        "participant_disconnected", SimpleNamespace(identity="someone_else")
    )
    assert call.live.closed == 0
    call.ctx.room.emit("participant_disconnected", call.caller)
    await asyncio.sleep(0.01)
    (shutdown,) = call.on_shutdown
    await shutdown("job shutdown")
    assert call.live.closed == 1
    (end,) = [b for p, b in call.backend.calls if p == "end"]
    assert isinstance(end["durationSec"], int) and end["durationSec"] >= 0
    assert call.shutdowns == ["caller hung up"]


def _away(call, state="away"):
    call.live.session.user_state = state
    call.live.session.emit("user_state_changed", SimpleNamespace(new_state=state))


async def test_a_silent_line_is_told_once_then_hung_up_unless_a_request_is_pending(monkeypatch):
    """Review Focus 5 for a held line, driven by the caller's own state (user_away_timeout): silence while the team
    works on a fresh request is holding, not gone; otherwise GPT-Live is told once (its words, not ours) and the
    call ends AWAY_S later, even when the voice itself talks into the silence ("are you still there?")."""
    monkeypatch.setattr(phone, "AWAY_S", 0.02)
    monkeypatch.setattr(phone, "WAIT_S", 0.05)
    call = await Call(monkeypatch, replies={"turn": {"messageId": "m1"}}).answer()
    call.live.pending["item_1"] = await call.started["post"]("Book me Saturday at 3", [])
    _away(call)
    await asyncio.sleep(0.03)
    assert call.deleted == 0 and len(call.live.sent) == 1  # holding for the team's answer
    await asyncio.sleep(0.08)  # the ask is older than WAIT_S: a lost answer never holds the line open
    assert len(call.live.sent) == 2 and call.deleted == 1 and call.shutdowns == ["caller silent"]

    quiet = await Call(monkeypatch).answer()
    _away(quiet)
    assert len(quiet.live.sent) == 2 and quiet.live.sent[1][0] is None
    quiet.state("listening", "speaking")  # the voice asks if they are still there
    quiet.item("assistant", "Are you still there?", 5.0)
    quiet.state("speaking", "listening")
    await asyncio.sleep(0.04)
    assert quiet.deleted == 1 and quiet.live.closed == 1 and quiet.shutdowns == ["caller silent"]
    (end,) = [b for p, b in quiet.backend.calls if p == "end"]
    assert end["silent"] is True

    back = await Call(monkeypatch).answer()
    _away(back)
    _away(back, "speaking")  # the caller answers: the hang-up is off
    await asyncio.sleep(0.04)
    assert back.deleted == 0


async def test_the_end_is_posted_even_when_the_close_fails(monkeypatch):
    call = await Call(monkeypatch).answer()

    async def broken():
        raise RuntimeError("socket gone")

    call.live.close = broken
    call.ctx.room.emit("participant_disconnected", call.caller)
    await asyncio.sleep(0.01)
    assert "end" in call.paths() and call.shutdowns == ["caller hung up"]
