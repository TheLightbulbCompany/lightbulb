"""The meeting's ears and mouth (meet.py): the wake gate, what is posted to the Router, and the 17 scenarios."""

import asyncio

import numpy as np
from livekit import rtc

from huddle_agent import meet
from replay import SCENARIOS, gate as replay_gate

PEOPLE = [{"name": "Maya"}, {"name": "Leo"}]


def g() -> meet.Gate:
    return meet.Gate(name="Nova", people=PEOPLE)


def test_speaks_only_when_named_after_a_pause():
    gate = g()
    assert gate.heard("maya", "Leo, did you push the fix?", 0, new_turn=True) == ""
    assert (
        gate.heard("leo", "Yes, last night.", 2, new_turn=True) == ""
    )
    assert gate.verdict(10, 8) is False  # nothing asked: a reply is dropped
    assert (
        gate.heard("maya", "Nova, how many calls did we miss?", 20, new_turn=True)
        == "ask"
    )
    assert gate.verdict(21, 1.0) is None  # the room is not quiet yet
    assert gate.verdict(23.5, meet.MEET_QUIET_S) is True


def test_an_unsaid_answer_goes_stale():
    gate = g()
    gate.heard("maya", "Nova, what's on tomorrow?", 0, new_turn=True)
    assert (
        gate.verdict(meet.STALE_S + 0.1, 0) is False
    )  # never quiet in time: dropped, not said late
    assert gate.ask_at is None


def test_a_question_to_the_room_is_not_an_ask():
    gate = g()  # speaks only when named: the 3.5 s unclaimed-question rule is gone (transcripts come late)
    assert gate.heard("maya", "Does anyone know when the deploy finished?", 0, new_turn=True) == ""
    assert gate.verdict(5.0, 5.0) is False


def test_stop_and_mentions():
    gate = g()
    gate.heard("maya", "Nova, read me the notes", 0, new_turn=True)
    assert (
        gate.heard("maya", "Nova, stop", 5, new_turn=True) == "stop"
        and gate.ask_at is None
    )
    assert (
        gate.heard("leo", "I asked Nova what the plan costs.", 9, new_turn=True) == ""
    )
    assert gate.heard("leo", "Hi Nova.", 12, new_turn=True) == "ask"


def test_an_unnamed_follow_up_is_not_an_ask():
    gate = g()  # speaks only when named: "Leo, did you push the fix?" played as a follow-up live (7 Nov)
    gate.heard("maya", "Nova, how many agents come with a workspace?", 0, new_turn=True)
    gate.answered(8)
    assert gate.heard("maya", "And what are they called?", 30, new_turn=True) == ""
    assert gate.heard("maya", "Leo, did you push the fix last night?", 40, new_turn=True) == ""


def test_the_nudge_wakes_only_on_the_name_opening_an_ask():
    for line in ("I asked Nova.", "Nova's great.", "Did Nova do it?", "Lightbulb moment!", "Novak, can you send it?",
                 "No, Nova.", "Nova.", "Thanks, Nova.", "We should ask Nova about it later.",
                 "Nova is the best.", "Nova did it.", "Nova was great today.", "Nova will handle that later.",
                 "Nova what a day."):  # fmt: skip
        assert not meet.wakes("Nova", line), line
    assert meet.wakes("Nova", "Nova is Saturday free?")  # a question word counts with the question mark
    for line in ("Nova, how many calls did we get today?", "Hey Nova, what's on Wednesday?",
                 "Okay, let's start. Nova, read me the bookings.", "Nova what's on tomorrow?",
                 "light bulb How many phone calls did we get today?", "Lightbulb, please draft the post."):  # fmt: skip
        assert meet.wakes("Nova", line), line


def test_the_name_may_open_any_sentence_of_a_joined_turn():
    gate = g()
    text = "Okay, everyone. Let's get started with the check-in. light bulb How many calls did we get?"
    assert meet.Gate(name="Lightbulb", people=PEOPLE).heard("p1", text, 0, new_turn=True) == "ask"
    assert gate.heard("p1", "Leo, did you push it? Nova, what's on tomorrow?", 0, new_turn=True) == "ask"
    assert gate.heard("p2", "We should ask Nova about it later.", 9, new_turn=True) == ""


def test_replay_scenarios_against_the_gate(monkeypatch):
    """The live order: the reply's audio is held until its turn's transcript arrives (TRANSCRIPT_LAG_S). The
    assistant must stay silent where it should, at any lag; it must answer in time (ack_within) at the live lag
    (lag changes how soon, so ack_within is judged only there)."""
    assert len(set(SCENARIOS)) == 17
    live = replay_gate.TRANSCRIPT_LAG_S
    for lag in (0.0, 2.0, live, 8.0):
        monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", lag)
        results = [r for name in SCENARIOS for r in replay_gate.judge(name)]
        assert [r for r in results if r[0] == "FAIL" and r[1] == "silent_between"] == [], lag
    monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", live)
    late = [n for n in SCENARIOS for r in replay_gate.judge(n) if r[:2] == ("FAIL", "ack_within")]
    assert late == []


def test_replay_gate_fails_on_late_ack(monkeypatch):
    monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", 8.0)  # the transcript lands far too late: acks miss their 4 s
    assert any(r[:2] == ("FAIL", "ack_within") for n in SCENARIOS for r in replay_gate.judge(n))
    assert replay_gate.main() != 0


def test_no_nudge_when_someone_in_the_room_has_the_assistants_name():
    for people in ([{"name": "Nova Smith"}], [{"name": "Nova"}], [{"name": "Maya"}, {"name": "nova"}]):
        assert not meet.wakes("Nova", "Nova, can you share your screen?", people), people
    assert meet.wakes("Nova", "Nova, can you share your screen?", [{"name": "Maya"}, {"name": "Novak"}])


def test_the_name_as_its_own_sentence_then_the_ask_wakes():
    """8 Nov sim: the transcriber wrote "Lightbulb. How many phone calls did we get today?" and the nudge never came,
    so OpenAI (which answers only when asked) said nothing."""
    for line in ("Lightbulb. How many phone calls did we get today?", "Light bulb. How many phone calls today?",
                 "Nova. Read me the bookings.", "Hey Nova. What's on Wednesday?"):  # fmt: skip
        assert meet.wakes("Nova", line), line
    for line in ("Nova. That was great.", "Thanks, Nova. How was your weekend?", "I asked Nova. What did she say?",
                 "Nova. Is it."):  # fmt: skip
        assert not meet.wakes("Nova", line), line




def test_only_an_addressed_line_goes_to_the_router():
    gate = g()
    assert meet.addressed(gate, "Nova", "maya", "Nova, what have we shipped this week?", 0) == "ask"
    for side_talk in ("Leo, did you push the fix?", "I asked Nova what it costs.", "Thanks, Nova.",
                      "We should ask Nova about it later."):  # fmt: skip
        assert meet.addressed(gate, "Nova", "leo", side_talk, 1) == "", side_talk
    assert meet.addressed(gate, "Nova", "maya", "Nova, stop", 2) == "stop"


def test_a_voiced_frame_is_someone_speaking():
    loud = rtc.AudioFrame((np.full(320, 2000, dtype=np.int16)).tobytes(), 16000, 1, 320)
    quiet = rtc.AudioFrame((np.full(320, 10, dtype=np.int16)).tobytes(), 16000, 1, 320)
    assert meet.voiced(loud) and not meet.voiced(quiet)


async def test_a_line_split_at_the_name_is_joined_before_the_gate():
    """8 Nov prod Meet 12:24:45: "Nova, what have we shipped..." at normal pace came as two finals, "Nova." and
    "what have we shipped in the last week?", each gated alone (kind=-): missed."""
    got = []
    j = meet.Joiner(lambda who, text: got.append((who, text)), wait=0.05)
    j.final("maya", "Nova.")
    await asyncio.sleep(0.02)
    j.final("maya", "what have we shipped in the last week?")
    j.final("leo", "Sounds good.")
    await asyncio.sleep(0.1)
    assert got == [("maya", "Nova. what have we shipped in the last week?"), ("leo", "Sounds good.")]
    gate = g()
    assert meet.addressed(gate, "Nova", "maya", got[0][1], 0) == "ask"


async def test_a_long_line_with_pauses_is_one_line():
    """8 Nov prod Meet 12:28:35: a long answer was cut at a pause and only its first part was asked."""
    got = []
    j = meet.Joiner(lambda who, text: got.append(text), wait=0.05)
    j.final("maya", "Nova, for the gel colors, they are nail gels.")
    await asyncio.sleep(0.04)
    j.speaking("maya")  # an interim: still talking, so the line waits for its next final
    await asyncio.sleep(0.1)
    assert got == []
    j.final("maya", "We have none of red. Pink are nearly gone.")
    await asyncio.sleep(0.1)
    assert got == ["Nova, for the gel colors, they are nail gels. We have none of red. Pink are nearly gone."]
    j.speaking("leo")  # nothing pending for them: nothing held
    j.close()




# --- the meeting on GPT-Live (Task 3): the voice is GPT-Live, the brain the meeting room's Router --------------------

SAM, MAYA = "meet:r:12", "meet:r:11"


class _Track:
    def __init__(self):
        self.muted = False

    def mute(self):
        self.muted = True

    def unmute(self):
        self.muted = False


class _Room:
    """The worker's LiveKit room: its one published voice track (`agent-assistant`, what the meet-bot page plays)."""

    def __init__(self):
        self.handlers = {}
        self.voice = _Track()
        pub = type("Pub", (), {"name": meet.TRACK, "track": self.voice})()
        self.local_participant = type("LP", (), {"track_publications": {"pub": pub}})()
        self.remote_participants = {}

    def on(self, ev, fn=None):
        self.handlers[ev] = fn
        return fn


class _Backend:
    def __init__(self, events=()):
        self.sent, self.events = [], list(events)

    async def meet(self, op, body):
        self.sent.append((op, body))
        if op == "events":
            return self.events.pop(0) if self.events else {"messages": [], "after": 0}
        if op == "start":
            return {"assistantName": "Nova", "title": "Standup", "after": 3}
        return {"posted": True, "messageId": f"m{len(self.sent)}"}

    def asks(self):
        return [body for op, body in self.sent if op == "ask"]


def _meeting(backend=None):
    room = _Room()
    m = meet.Meeting(backend or _Backend(), "Nova", room, poll_s=0, wait_s=0.05)
    m.person(MAYA, "Maya")
    m.person(SAM, "Sam")
    return m, room


async def test_meet_unaddressed_speech_never_delegates():
    """Review Focus 3: side talk, a mention, a question to the room: GPT-Live may delegate on any of them, but no
    line reaches the Router and no audio reaches the meeting."""
    b = _Backend()
    m, room = _meeting(b)
    assert room.voice.muted  # closed from the start: nobody has named it
    for who, text in [(MAYA, "Leo, did you push the fix?"), (SAM, "I asked Nova what it costs."),
                      (MAYA, "Does anyone know what shipped?"), (SAM, "We should ask Nova about it later.")]:  # fmt: skip
        m.heard(who, text, final=False)
        assert room.voice.muted, text
        m.line(who, text)
        assert await m.post(text, []) == "" and room.voice.muted, text
    assert await m.post("what shipped", []) == ""  # a delegation with no line at all
    assert b.asks() == []


async def test_meet_an_addressed_line_permits_one_delegation_and_side_talk_closes_the_voice():
    b = _Backend()
    m, room = _meeting(b)
    m.line(SAM, "Nova, what shipped this week?")
    assert not room.voice.muted
    m.line(MAYA, "Sounds good.")  # side talk after it: the voice is off, and the ask is no longer the current turn
    assert room.voice.muted and await m.post("what shipped", []) == ""
    m.line(SAM, "Nova, what shipped this week?")
    assert await m.post("what shipped", []) != ""
    assert await m.post("and the calendar", []) == ""  # one line named it, one delegation
    assert len(b.asks()) == 1


async def test_meet_named_question_delegates_with_speaker():
    b = _Backend()
    m, room = _meeting(b)
    m.line(MAYA, "We shipped the calendar fix yesterday.")
    m.heard(SAM, "Nova, what", final=False)  # the voice opens on the words as they come, before the line is judged
    assert not room.voice.muted
    pending = asyncio.create_task(m.post("what shipped this week", ["what shipped this week"]))
    await asyncio.sleep(0)  # GPT-Live delegated before the per-speaker line was judged: the post waits for it
    m.line(SAM, "Nova, what shipped this week?")
    assert await pending == "m1"
    [body] = b.asks()
    assert (body["participantId"], body["speakerName"]) == ("12", "Sam")
    assert body["text"] == ("Nova, what shipped this week?\n\nSaid in the talk since the last ask:\n"
                            "> Maya: We shipped the calendar fix yesterday.")  # fmt: skip
    assert m.asked[-1]["who"] == "Sam" and m.asked[-1]["q"] == "Nova, what shipped this week?"


async def test_meet_stop_closes_the_voice_at_once():
    m, room = _meeting()
    m.line(SAM, "Nova, what shipped this week?")
    m.heard(MAYA, "Nova, stop", final=False)  # a partial is enough
    assert room.voice.muted


async def test_meet_link_result_reaches_chat():
    """A thread's line with its link (the backend also posts it in the Meet chat, voice_meet.with_link): GPT-Live gets
    it once, as the team's message, spoken without the URL, when the room is quiet; the notes keep the link."""
    line = "Drafted the holiday-hours post: [holiday-hours-post.md](https://app.x/chat?panel=task&taskId=k9)"
    msg = {"id": "a9", "seq": 9, "text": line}
    quiet = {"v": False}
    b = _Backend([{"messages": [msg], "after": 9}, {"messages": [msg], "after": 9}])
    room = _Room()
    m = meet.Meeting(b, "Nova", room, after=3, quiet=lambda: quiet["v"], poll_s=0)
    it = m.events()
    nxt = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.01)
    assert not nxt.done() and room.voice.muted  # someone is talking: held
    quiet["v"] = True
    assert await nxt == {"kind": "agent", "id": "a9", "text": "Drafted the holiday-hours post: holiday-hours-post.md"}
    assert not room.voice.muted  # the team's message is heard
    assert m.lines[-1]["kind"] == "agent" and m.lines[-1]["text"] == line
    assert b.sent[0] == ("events", {"after": 3})
    nxt = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.01)
    assert not nxt.done()  # the same message again: never re-sent
    nxt.cancel()
    assert b.sent[-1] == ("events", {"after": 9})


async def test_meet_spend_goes_to_inviter_card():
    """A spend asked by voice is the inviter's message naming the speaker (voice_meet.ask, confirmAll): its OK is a
    card tap. A spoken yes is one more line to the Router, never an approval; the worker has no approve path."""
    b = _Backend()
    m, _ = _meeting(b)
    for text in ("Nova, buy the $40 plan.", "Nova, yes, approve it."):
        m.line(SAM, text)
        assert await m.post(text, [])
    assert [op for op, _ in b.sent] == ["ask", "ask"] and [a["text"] for a in b.asks()][1] == "Nova, yes, approve it."
    assert "card" in meet.prompt({"name": "Nova", "company": "Acme", "title": "T", "today": "d"})
    assert not [n for n in dir(meet) if "approv" in n.lower()]


async def test_the_line_after_a_spoken_question_answers_it():
    """#5112: a Worker's question from a thread is sent with its qid; the next addressed line within 60 s answers it,
    a line later than that is a new ask."""
    from huddle_agent import mouth

    q = {"id": "q1", "seq": 8, "text": "**Pulse** asks: What holiday hours?", "question": {"taskId": "t1", "qid": "q-1"}}
    b = _Backend([{"messages": [q], "after": 8}])
    m, _ = _meeting(b)
    assert (await m.events().__anext__())["question"] == {"taskId": "t1", "qid": "q-1"}
    m.line(SAM, "Nova, we close at 2 on Christmas Eve.")
    await m.post("we close at 2", [])
    m.line(SAM, "Nova, and what shipped?")
    await m.post("what shipped", [])
    first, second = b.asks()
    assert first["answers"] == {"taskId": "t1", "qid": "q-1"} and "answers" not in second
    m.question, m.question_at = q["question"], m.question_at - mouth.ANSWER_WITHIN_S - 1
    m.line(SAM, "Nova, what's on my calendar?")
    await m.post("calendar", [])
    assert "answers" not in b.asks()[-1]


async def test_meet_runs_on_gpt_live_and_closes_it_when_the_bot_leaves(monkeypatch):
    """GPT-Live bills every second it is open: the session is closed with the meeting."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    b, room, started, shutdown = _Backend(), _Room(), {}, []

    class Live:
        closed, session = False, None

        async def close(self):
            Live.closed = True

    async def start(ctx, **kw):
        started.update(kw)
        return Live()

    async def noop(*_a, **_k):
        return None

    b.transcript = b.aclose = b.leave_meeting = noop
    room.local_participant.set_attributes = noop
    ctx = type("Ctx", (), {"room": room, "connect": noop, "add_shutdown_callback": lambda _s, cb: shutdown.append(cb),
                           "shutdown": lambda _s, *_: None})()  # fmt: skip
    monkeypatch.setattr(meet.live, "start", start)
    monkeypatch.setattr(meet, "Backend", lambda *_a: b)
    task = asyncio.create_task(meet.run(ctx, {"roomId": "r", "ownerId": "o", "roomToken": "t", "company": "Acme"}))
    while not started:
        await asyncio.sleep(0.01)
    assert started["surface"] == "meet"
    assert "Speak only when someone says your name" in started["prompt"] and "Standup" in started["prompt"]
    assert started["post"].__self__.name == "Nova" and started["events"].__self__.name == "Nova"
    room.handlers["disconnected"]()
    await task
    for cb in shutdown:
        await cb()
    assert Live.closed and ("end", {"asked": [], "summary": "Asked 0 times"}) in b.sent


async def test_gpt_live_hears_every_meet_person_mixed():
    """RoomIO links one participant; Recall gives one track a person. GPT-Live hears their sum, and silence between."""
    mix = meet.Mix()
    frame = lambda v: rtc.AudioFrame(np.full(meet.FRAME, v, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME)  # noqa: E731
    mix.push(MAYA, frame(1000))
    mix.push(SAM, frame(32000))
    out = np.frombuffer((await mix.__anext__()).data, dtype=np.int16)
    assert out.tolist() == [32767] * meet.FRAME  # summed, clipped
    assert not np.frombuffer((await mix.__anext__()).data, dtype=np.int16).any()  # nobody: silence, still a frame
    mix.drop(SAM)
