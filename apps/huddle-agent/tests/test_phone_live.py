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
        self.awaiting = self.pending
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
    monkeypatch.setattr(phone.time, "time", lambda: 1000.0)


async def test_phone_opening_follows_the_businesss_greeting(monkeypatch):
    """The model opens from the owner's greeting (a style guide) once after start (the docs' greeting: commentary
    with delegation_id null). Nothing checks or corrects what it says."""
    call = await Call(monkeypatch).answer()
    assert call.started["surface"] == "phone"
    opts = call.started[
        "room_options"
    ]  # the SIP caller's audio, telephony noise cancellation, ends on hang-up
    assert opts.participant_identity == "sip_caller" and opts.close_on_disconnect
    assert (
        call.started["record"] is True
        and call.started["user_away_timeout"] == phone.AWAY_S
    )
    prompt = call.started["prompt"]
    (first_id, opening), *_ = call.live.sent
    assert "Nova" in prompt and "this call is recorded" not in prompt
    assert "Thanks for calling Glow Nails!" in opening and "recorded" not in opening
    assert first_id is None


async def test_start_sends_the_carriers_stir_shaken_result(monkeypatch):
    """The inbound trunk maps X-Twilio-VerStat to `sip.verstat`; the backend decides what an unattested number gets."""
    call = Call(monkeypatch)
    call.caller.attributes["sip.verstat"] = "TN-Validation-Passed-B"
    await call.answer()
    assert call.backend.calls[0] == ("/internal/voice/calls", {
        "calledNumber": "+14842079294", "callerNumber": "+16105550100", "livekitRoom": "phone-dev-_+16105550100_ab",
        "verstat": "TN-Validation-Passed-B"})  # fmt: skip
    missing = await Call(monkeypatch).answer()  # no header (a LiveKit-number call, a carrier without STIR/SHAKEN)
    assert missing.backend.calls[0][1]["verstat"] == ""


async def test_followup_post_keeps_its_task_receipt_for_terminal_answer_correlation(
    monkeypatch,
):
    call = await Call(
        monkeypatch,
        replies={"turn": {"messageId": "followup", "taskId": "existing-task"}},
    ).answer()
    posted = await call.started["post"]("The first time works", [])
    assert posted == phone.live.Posted("followup", ("existing-task",))


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


async def test_restarting_events_does_not_repeat_an_admitted_transfer(monkeypatch):
    """live.py restarts the events iterator after a transport failure; its spoken-id dedupe must also hold for
    the phone's SIP side effect, which happens before a message is yielded to live.py."""
    message = {
        "id": "transfer-1",
        "seq": 5,
        "text": "Putting you through.",
        "action": "transfer",
    }
    call = await Call(monkeypatch, replies={"events": {"messages": [message], "after": 5},
                                           "transfer": {"transferTo": "+16105559999"}}).answer()  # fmt: skip
    for _ in range(2):
        events = call.started["events"]()
        assert await anext(events) == {"kind": "agent", **message}
        await events.aclose()
        await asyncio.sleep(0.01)
    assert call.transfers == ["tel:+16105559999"]
    assert call.paths().count("transfer") == 1


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


async def test_a_silent_line_is_told_once_then_hung_up_unless_a_request_is_pending(
    monkeypatch,
):
    """Review Focus 5 for a held line, driven by the caller's own state (user_away_timeout): silence while the team
    works on a fresh request is holding, not gone; otherwise GPT-Live is told once (its words, not ours) and the
    call ends AWAY_S later, even when the voice itself talks into the silence ("are you still there?")."""
    monkeypatch.setattr(phone, "AWAY_S", 0.02)
    monkeypatch.setattr(phone, "WAIT_S", 0.05)
    call = await Call(monkeypatch, replies={"turn": {"messageId": "m1"}}).answer()
    call.live.pending["item_1"] = await call.started["post"](
        "Book me Saturday at 3", []
    )
    _away(call)
    await asyncio.sleep(0.03)
    assert (
        call.deleted == 0 and len(call.live.sent) == 1
    )  # holding for the team's answer
    await asyncio.sleep(
        0.08
    )  # the ask is older than WAIT_S: a lost answer never holds the line open
    assert (
        len(call.live.sent) == 2
        and call.deleted == 1
        and call.shutdowns == ["caller silent"]
    )

    quiet = await Call(monkeypatch).answer()
    _away(quiet)
    assert len(quiet.live.sent) == 2 and quiet.live.sent[1][0] is None
    quiet.state("listening", "speaking")  # the voice asks if they are still there
    quiet.item("assistant", "Are you still there?", 5.0)
    quiet.state("speaking", "listening")
    await asyncio.sleep(0.04)
    assert (
        quiet.deleted == 1
        and quiet.live.closed == 1
        and quiet.shutdowns == ["caller silent"]
    )
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


async def test_a_voice_start_failure_ends_the_call_and_requests_a_callback(monkeypatch):
    call = Call(monkeypatch)

    async def broken(*args, **kwargs):
        raise RuntimeError("voice unavailable")

    monkeypatch.setattr(phone.live, "start", broken)
    await call.answer()
    assert call.deleted == 1 and call.shutdowns == ["voice failed"]
    assert call.paths().count("unknown") == call.paths().count("end") == 1
    (end,) = [body for path, body in call.backend.calls if path == "end"]
    assert end["durationSec"] >= 0


async def test_completed_work_restores_normal_silence_timeout_without_losing_correlation(
    monkeypatch,
):
    monkeypatch.setattr(phone, "AWAY_S", 0.02)
    monkeypatch.setattr(phone, "WAIT_S", 90)
    monkeypatch.setattr(phone, "POLL_S", 0)
    call = await Call(
        monkeypatch,
        replies={
            "turn": {"messageId": "m1"},
            "events": [
                {
                    "messages": [
                        {
                            "id": "ack",
                            "inReplyTo": "m1",
                            "text": "On it.",
                            "waiting": True,
                        }
                    ],
                    "after": 5,
                },
                {
                    "messages": [
                        {
                            "id": "answer",
                            "inReplyTo": "m1",
                            "text": "Booked.",
                            "waiting": False,
                        }
                    ],
                    "after": 6,
                },
            ],
        },
    ).answer()
    call.live.awaiting = {"item_1": await call.started["post"]("Book Saturday", [])}
    call.live.pending = dict(call.live.awaiting)
    _away(call)
    events = call.started["events"]()
    assert (await anext(events))["waiting"] is True
    assert (await anext(events))["waiting"] is False
    assert call.deleted == 0 and len(call.live.sent) == 1  # ack never completes work
    call.live.awaiting.clear()  # LiveSession._result's terminal receipt, independently tested
    reader = asyncio.create_task(anext(events))
    await asyncio.sleep(0.04)
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    assert call.live.pending == {"item_1": "m1"}
    assert call.deleted == 1 and call.shutdowns == ["caller silent"]


async def test_bilingual_bootstrap_sets_voice_and_language_without_changing_delegation(
    monkeypatch,
):
    call = await Call(monkeypatch, info={"voice": "quartz", "spanish": True}).answer()
    assert call.started["voice"] == "quartz"
    assert "following the caller's choice" in call.started["prompt"]
    assert "Speak only English" not in call.started["prompt"]
    assert "Speak first in English" in call.started["opening"]
    assert call.live.sent[0] == (None, call.started["opening"])
    call.state("listening", "speaking")
    call.item(
        "assistant",
        "Soy Nova, un asistente de inteligencia artificial. Esta llamada se está grabando.",
        1,
    )
    call.state("speaking", "listening")
    await asyncio.sleep(0.01)
    await call.on_shutdown[0]()


async def test_legacy_bootstrap_keeps_current_default_and_english_policy(monkeypatch):
    call = await Call(monkeypatch, info={"voice": "Warm"}).answer()
    assert call.started["voice"] == "marin"
    assert "Speak only English" in call.started["prompt"]
    await call.on_shutdown[0]()


def _keys(call, keys, who="sip_caller"):
    for k in keys:
        call.ctx.room.emit("sip_dtmf_received", SimpleNamespace(digit=k, participant=SimpleNamespace(identity=who)))


async def test_the_owner_types_a_pin_and_the_backend_alone_decides(monkeypatch):
    """Board row 3.29: keypad digits go to /owner-pin, never to the model or the log; a yes moves /events to the
    member's DM cursor and tells GPT-Live it now talks to the owner; a no is said as a no, nothing more."""
    call = await Call(monkeypatch, info={"ownerPin": True},
                      replies={"owner-pin": [{"ok": False, "locked": False}, {"ok": True, "after": 41}]}).answer()  # fmt: skip
    instructions = []
    call.live.transport = SimpleNamespace(instructions=instructions.append)
    assert "phone PIN on the keypad" in call.started["prompt"]
    _keys(call, "12", who="someone_else")  # not the caller's keypad
    _keys(call, "99*0000#")  # `*` starts over
    await asyncio.sleep(0.01)
    assert [b for p, b in call.backend.calls if p == "owner-pin"] == [{"pin": "0000"}]
    assert call.live.sent[-1] == (None, phone.PIN_WRONG) and instructions == []
    _keys(call, "4821#")
    await asyncio.sleep(0.01)
    assert call.live.sent[-1] == (None, phone.PIN_OK) and instructions == [phone.OWNER_NOW]
    assert all("4821" not in t for _d, t in call.live.sent) and "4821" not in call.started["prompt"]
    FakeBackend.replies["events"] = {"messages": [], "after": 41}
    task = asyncio.ensure_future(anext(call.started["events"]()))
    await asyncio.sleep(0.01)
    task.cancel()
    assert [b for p, b in call.backend.calls if p == "events"][-1] == {"after": 41}  # the DM's cursor
    await call.on_shutdown[0]()


async def test_owner_mode_answers_the_question_just_said_and_ends_when_removed(monkeypatch):
    """The Worker question the voice was given goes back with the next line (`answers`, as owner.py), once; when the
    backend says owner mode ended, the cursor is the call room's again and the caller prompt is back."""
    ref = {"taskId": "t-1", "qid": "q-1"}
    call = await Call(monkeypatch, info={"ownerPin": True}, replies={
        "events": [{"messages": [{"id": "q", "seq": 42, "text": "Which day?", "question": ref}], "after": 42},
                   {"messages": [], "after": 3, "ownerModeEnded": True}, {"messages": [], "after": 3}],
        "turn": {"messageId": "m"}}).answer()  # fmt: skip
    instructions = []
    call.live.transport = SimpleNamespace(instructions=instructions.append)
    events = call.started["events"]()
    assert (await anext(events))["question"] == ref
    await call.started["post"]("Tuesday", [])
    await call.started["post"]("And book a cut", [])
    turns = [b for p, b in call.backend.calls if p == "turn"]
    assert turns[0]["answers"] == ref and "answers" not in turns[1]
    task = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0.01)
    task.cancel()
    assert instructions == [call.started["prompt"]] and (None, phone.OWNER_ENDED) in call.live.sent
    assert [b for p, b in call.backend.calls if p == "events"][-1] == {"after": 3}
    await call.on_shutdown[0]()


async def test_a_turn_that_ends_owner_mode_moves_the_cursor_back(monkeypatch):
    call = await Call(monkeypatch, info={"ownerPin": True}, replies={
        "turn": {"messageId": "m", "ownerModeEnded": True, "after": 6}, "events": {"messages": [], "after": 6}}).answer()  # fmt: skip
    instructions = []
    call.live.transport = SimpleNamespace(instructions=instructions.append)
    await call.started["post"]("What's on today?", [])
    assert instructions == [call.started["prompt"]] and (None, phone.OWNER_ENDED) in call.live.sent
    task = asyncio.ensure_future(anext(call.started["events"]()))
    await asyncio.sleep(0.01)
    task.cancel()
    assert [b for p, b in call.backend.calls if p == "events"][-1] == {"after": 6}  # the call room's, not the DM's
    await call.on_shutdown[0]()


async def test_a_number_with_no_pin_ignores_the_keypad(monkeypatch):
    call = await Call(monkeypatch).answer()
    assert "keypad" not in call.started["prompt"]
    _keys(call, "4821#")
    await asyncio.sleep(0.01)
    assert "owner-pin" not in call.paths()
    await call.on_shutdown[0]()
