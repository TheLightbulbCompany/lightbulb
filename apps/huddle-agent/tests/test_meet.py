"""The meeting on GPT-Live (meet.py): the notes, what each delegation posts, the team's messages, the mixed audio.
No code gate on speech or delegation (founder ruling 8 Nov 2026): GPT-Live's prompt decides."""

import asyncio
import inspect

import numpy as np
import pytest
from livekit import rtc

from huddle_agent import meet

SAM, MAYA = "meet:r:12", "meet:r:11"


def test_a_voiced_frame_is_someone_speaking():
    loud = rtc.AudioFrame((np.full(320, 2000, dtype=np.int16)).tobytes(), 16000, 1, 320)
    quiet = rtc.AudioFrame((np.full(320, 10, dtype=np.int16)).tobytes(), 16000, 1, 320)
    assert meet.voiced(loud) and not meet.voiced(quiet)


async def test_a_line_split_at_the_name_is_joined():
    """8 Nov prod Meet 12:24:45: "Nova, what have we shipped..." at normal pace came as two finals."""
    got = []
    j = meet.Joiner(lambda who, text: got.append((who, text)), wait=0.05)
    j.final("maya", "Nova.")
    await asyncio.sleep(0.02)
    j.final("maya", "what have we shipped in the last week?")
    j.final("leo", "Sounds good.")
    await asyncio.sleep(0.1)
    assert got == [
        ("maya", "Nova. what have we shipped in the last week?"),
        ("leo", "Sounds good."),
    ]


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


def _meeting(backend=None, **kw):
    m = meet.Meeting(backend or _Backend(), "Nova", poll_s=0, **kw)
    m.person(MAYA, "Maya")
    m.person(SAM, "Sam")
    return m


def test_no_code_gate_on_speech_or_delegation():
    """The simplest documented version: GPT-Live's prompt decides when to speak and delegate. No code path mutes the
    voice track or refuses a delegation; measure before adding one."""
    src = inspect.getsource(meet)
    assert ".mute(" not in src and ".unmute(" not in src and "Gate" not in src
    p = meet.prompt({"name": "Nova", "company": "Acme", "title": "Standup", "today": "Sunday"})
    assert "Backchannel policy: None." in p and "Interruption policy:" in p and "Delegation policy:" in p
    assert "Speak only when someone addresses you by name" in p
    assert "Do not generate <|SpawnThinking|> when:\n- The talk is between other people and does not address you." in p


async def test_every_delegation_is_posted_even_side_talk():
    b = _Backend()
    m = _meeting(b)
    m.line(MAYA, "Leo, did you push the fix?")
    assert await m.post("did Leo push the fix", []) == "m1"
    assert [a["text"] for a in b.asks()] == ["did Leo push the fix\n\nSaid in the talk since the last ask:\n"
                                             "> Maya: Leo, did you push the fix?"]  # fmt: skip


async def test_meet_named_question_delegates_with_speaker():
    b = _Backend()
    m = _meeting(b)
    m.line(MAYA, "We shipped the calendar fix yesterday.")
    m.line(SAM, "Nova, what shipped this week?")
    assert await m.post("What shipped this week?", ["user: Nova, what shipped this week?"]) == "m1"
    [body] = b.asks()
    assert (body["participantId"], body["speakerName"]) == ("12", "Sam")
    assert body["text"] == ("What shipped this week?\n\nSaid in the talk since the last ask:\n"
                            "> Maya: We shipped the calendar fix yesterday.\n> Sam: Nova, what shipped this week?")  # fmt: skip
    assert m.asked[-1]["who"] == "Sam" and m.asked[-1]["q"] == "What shipped this week?"
    m.last = (m.last[0] - meet.SPEAKER_S - 1, SAM)  # an old line names nobody
    await m.post("and the calendar?", [])
    assert (b.asks()[-1]["participantId"], b.asks()[-1]["speakerName"]) == ("", "")


async def test_a_refused_ask_returns_none():
    class B(_Backend):
        async def meet(self, op, body):
            return {"posted": False, "reason": "no_inviter"}

    assert await _meeting(B()).post("hi", []) is None  # live.py drops the delegation


async def test_meet_link_result_reaches_chat():
    """A thread's line with its link (the backend also posts it in the Meet chat, voice_meet.with_link): GPT-Live gets
    it once, as the team's message, without the URL, when the room is quiet; the notes keep the link."""
    line = "Drafted the holiday-hours post: [holiday-hours-post.md](https://app.x/chat?panel=task&taskId=k9)"
    msg = {"id": "a9", "seq": 9, "text": line}
    quiet = {"v": False}
    b = _Backend([{"messages": [msg], "after": 9}, {"messages": [msg], "after": 9}])
    m = meet.Meeting(b, "Nova", after=3, quiet=lambda: quiet["v"], poll_s=0)
    it = m.events()
    nxt = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.01)
    assert not nxt.done()  # someone is talking: held
    quiet["v"] = True
    assert await nxt == {
        "kind": "agent",
        "id": "a9",
        "text": "Drafted the holiday-hours post: holiday-hours-post.md",
    }
    assert m.lines[-1]["kind"] == "agent" and m.lines[-1]["text"] == line
    assert b.sent[0] == ("events", {"after": 3})
    nxt = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.01)
    assert not nxt.done()  # the same message again: never re-sent
    nxt.cancel()
    assert b.sent[-1] == ("events", {"after": 9})


async def test_meet_spend_goes_to_inviter_card():
    """A spend asked by voice is the inviter's message naming the speaker (voice_meet.ask, confirmAll): its OK is a
    card tap. A spoken yes is one more message to the Router, never an approval; the worker has no approve path."""
    b = _Backend()
    m = _meeting(b)
    for text in ("Nova, buy the $40 plan.", "Nova, yes, approve it."):
        m.line(SAM, text)
        assert await m.post(text, [])
    assert [op for op, _ in b.sent] == ["ask", "ask"]
    assert "card" in meet.prompt({"name": "Nova", "company": "Acme", "title": "T", "today": "d"})
    assert not [n for n in dir(meet) if "approv" in n.lower()]


async def test_the_ask_after_a_thread_question_answers_it():
    """#5112: a Worker's question from a thread is sent with its qid; the next ask within 60 s answers it, a later
    one is a new ask."""
    from huddle_agent import mouth

    q = {
        "id": "q1",
        "seq": 8,
        "text": "**Pulse** asks: What holiday hours?",
        "question": {"taskId": "t1", "qid": "q-1"},
    }
    b = _Backend([{"messages": [q], "after": 8}])
    m = _meeting(b)
    assert (await m.events().__anext__())["question"] == {"taskId": "t1", "qid": "q-1"}
    await m.post("We close at 2 on Christmas Eve.", [])
    await m.post("And what shipped?", [])
    first, second = b.asks()
    assert first["answers"] == {"taskId": "t1", "qid": "q-1"} and "answers" not in second
    m.question, m.question_at = q["question"], m.question_at - mouth.ANSWER_WITHIN_S - 1
    await m.post("What's on my calendar?", [])
    assert "answers" not in b.asks()[-1]


async def test_gpt_live_hears_every_meet_person_mixed():
    """RoomIO links one participant; Recall gives one track a person. GPT-Live hears their sum, and silence between;
    a person's queue holds 3 frames, so nothing old is mixed in late."""
    mix = meet.Mix()
    frame = lambda v: rtc.AudioFrame(np.full(meet.FRAME, v, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME)  # noqa: E731
    for _ in range(10):
        mix.push(MAYA, frame(1000))
    mix.push(SAM, frame(32000))
    mix.tick()
    assert len(mix._q[MAYA]) == 2
    mix.drop(MAYA)
    mix.tick()
    first, second = [np.frombuffer((await mix.__anext__()).data, dtype=np.int16) for _ in range(2)]
    assert first.tolist() == [32767] * meet.FRAME and not second.any()  # summed and clipped; then silence


async def test_a_waking_voice_hears_the_pre_roll_first_then_live():
    """The wake-word pattern: asleep, the mix keeps the newest PREROLL_S; the new session reads it at once (within the
    service's 5 s burst and the plugin's 4 s pre-start queue), then each live frame as it is mixed."""
    mix = meet.Mix()
    frame = lambda v: rtc.AudioFrame(np.full(meet.FRAME, v, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME)  # noqa: E731
    n = int(meet.PREROLL_S * meet.RATE / meet.FRAME)
    for i in range(n + 50):  # asleep for 5 s: nobody reads, the oldest second falls off
        mix.push(SAM, frame(i))
        mix.tick()
    assert meet.PREROLL_S <= 4.0 and len(mix.ring) == n
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = [int(np.frombuffer((await mix.__anext__()).data, dtype=np.int16)[0]) for _ in range(n)]
    assert got == list(range(50, n + 50)) and loop.time() - t0 < 0.5  # the newest 4 s, in order, faster than real time
    nxt = asyncio.create_task(mix.__anext__())
    await asyncio.sleep(0.01)
    assert not nxt.done()  # caught up: now live
    mix.push(SAM, frame(7))
    mix.tick()
    assert int(np.frombuffer((await nxt).data, dtype=np.int16)[0]) == 7


class _Voice:
    def __init__(self, history):
        self.history, self.closed = history, False
        self.contexts = []
        self.session = type("S", (), {"input": type("I", (), {"audio": "mix"})()})()

    def send_context(self, text):
        self.contexts.append(text)

    async def close(self):
        self.closed = True


def _sleepy(backend=None):
    opened = []

    async def open_voice(history):
        opened.append(_Voice(history))
        return opened[-1]

    return _meeting(backend, open_voice=open_voice), opened


async def test_meet_sleeps_after_5_minutes_without_activity():
    """GPT-Live bills every open second: no delegation and no speech of its own for SLEEP_S, and it closes."""
    m, opened = _sleepy()
    m.heard(SAM, "Nova, what", final=False)
    await m.opening
    assert m.voice is opened[0] and meet.SLEEP_S == 300
    await m.sleep()
    assert m.voice is not None  # just opened
    m.active_at -= meet.SLEEP_S - 10
    await m.post("what shipped", [])  # a delegation is activity
    await m.sleep()
    assert m.voice is not None
    m.active_at -= meet.SLEEP_S + 1
    m.activity()  # its own speech (agent_state speaking/thinking) is activity too
    await m.sleep()
    assert m.voice is not None
    m.active_at -= meet.SLEEP_S + 1
    await m.sleep()
    assert m.voice is None and opened[0].closed


async def test_meet_wakes_on_the_name_carrying_history():
    """Asleep, the first interim or final line with the name reopens it, told the recent meeting; the team messages
    from while it slept are fetched alongside, into the notes (not said), and the next wake's history has them as
    "Team". The words that named it come as the Mix pre-roll's audio."""
    b = _Backend(
        [
            {
                "messages": [{"id": "a7", "seq": 7, "text": "Pulse posted the holiday hours."}],
                "after": 7,
            }
        ]
    )
    m, opened = _sleepy(b)
    m.line(MAYA, "Let's look at the holiday hours.")
    m.heard(SAM, "Lightbulb, are those", final=False)
    await m.opening
    [voice] = opened
    assert voice.history == [("Maya", "Let's look at the holiday hours.")]  # the fetch is off the critical path
    assert voice.contexts == ["Team: Pulse posted the holiday hours."]
    assert m.after == 7 and "a7" in m.seen and m.lines[-1]["text"] == "Pulse posted the holiday hours."
    m.heard(SAM, "Nova, and the other thing?", final=True)  # awake: GPT-Live decides, nothing reopens
    assert m.opening is None and len(opened) == 1
    m.active_at -= meet.SLEEP_S + 1
    await m.sleep()
    m.heard(MAYA, "Nova, one more", final=True)  # asleep again: the name wakes it with the meeting so far
    await m.opening
    assert len(opened) == 2 and opened[0].session.input.audio is None  # the mix was taken off the closed session
    assert ("Team", "Pulse posted the holiday hours.") in opened[1].history


async def test_meet_no_reopen_on_unaddressed_talk_while_asleep():
    m, opened = _sleepy()
    for who, text in [(MAYA, "Leo, did you push the fix?"), (SAM, "Does anyone know what shipped?"),
                      (MAYA, "Novak said the light is fine."), (SAM, "Let's move on.")]:  # fmt: skip
        m.heard(who, text, final=False)
        m.heard(who, text, final=True)
    await asyncio.sleep(0)
    assert m.opening is None and opened == [] and meet.names_it("Nova", "I asked Nova.")  # contains the name: wakes


async def test_meet_runs_asleep_until_named_and_closes_the_voice_with_the_meeting(
    monkeypatch,
):
    """Nothing is billed until someone names it; the open voice is closed with the meeting. The tile shows its
    state."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    b, started, shutdown, attrs, hooks, made = _Backend(), {}, [], [], {}, []

    class Session:
        input = type("In", (), {"audio": None})()

        def on(self, ev, fn):
            hooks[ev] = fn

    class Live:
        closed, session = False, Session()

        async def close(self):
            Live.closed = True

    async def start(ctx, **kw):
        started.update(kw)
        return Live()

    async def noop(*_a, **_k):
        return None

    async def set_attributes(a):
        attrs.append(a)

    class Room:
        remote_participants = {}
        local_participant = type("LP", (), {"set_attributes": staticmethod(set_attributes)})()
        handlers = {}

        def on(self, ev, fn=None):
            self.handlers[ev] = fn

    real = meet.Meeting
    b.transcript = b.aclose = b.leave_meeting = noop
    room = Room()
    ctx = type("Ctx", (), {"room": room, "connect": noop, "add_shutdown_callback": lambda _s, cb: shutdown.append(cb),
                           "shutdown": lambda _s, *_: None})()  # fmt: skip
    monkeypatch.setattr(meet.live, "start", start)
    monkeypatch.setattr(meet, "Backend", lambda *_a: b)
    monkeypatch.setattr(meet, "Meeting", lambda *a, **k: made.append(real(*a, **k)) or made[-1])
    task = asyncio.create_task(meet.run(ctx, {"roomId": "r", "ownerId": "o", "roomToken": "t", "company": "Acme"}))
    while not attrs:
        await asyncio.sleep(0.01)
    assert started == {}  # asleep at join
    [m] = made
    m.heard("meet:r:12", "Nova, what", final=False)
    await m.opening
    assert started["surface"] == "meet" and "Standup" in started["prompt"] and started["history"] == []
    assert started["room_options"].audio_output.track_name == meet.TRACK
    assert isinstance(Live.session.input.audio, meet.Mix)
    hooks["agent_state_changed"](type("Ev", (), {"old_state": "listening", "new_state": "speaking"})())
    await asyncio.sleep(0)
    assert attrs[-1]["lightbulb.agent.assistant.status"] == "speaking"
    room.handlers["disconnected"]()
    await task
    for cb in shutdown:
        await cb()
    assert Live.closed and ("end", {"asked": [], "summary": "Asked 0 times"}) in b.sent


async def test_a_result_cancelled_in_the_quiet_wait_stays_in_the_notes_across_a_sleep():
    """I1: the voice slept while a team message waited for a pause: it is in the notes and the next wake's history,
    and the wake's catch-up does not add it twice."""
    msg = {"id": "a8", "seq": 8, "text": "We shipped #5095."}
    b = _Backend([{"messages": [msg], "after": 8}, {"messages": [msg], "after": 8}])
    opened = []

    async def open_voice(history):
        opened.append(_Voice(history))
        return opened[-1]

    m = meet.Meeting(b, "Nova", quiet=lambda: False, poll_s=0, open_voice=open_voice)
    waiting = asyncio.create_task(m.events().__anext__())
    await asyncio.sleep(0.02)
    waiting.cancel()  # live.close() cancels its events reader mid-wait
    m.heard(SAM, "Nova, did it ship?", final=False)
    await m.opening
    assert opened[0].history == [("Team", "We shipped #5095.")]
    assert [ln["text"] for ln in m.lines] == ["We shipped #5095."]


async def test_a_wake_holds_the_pre_roll_and_paces_what_is_past_the_burst():
    """I2: from the wake, the ring stops rolling, so a slow start evicts nothing; the new session reads PREROLL_S at
    once and the rest at real time (the service rejects a burst over 5 s). Asleep again, it rolls."""
    mix = meet.Mix()
    n = int(meet.PREROLL_S * meet.RATE / meet.FRAME)
    for _ in range(n):
        mix.tick()
    mix.hold()
    for _ in range(10):  # 200 ms of startup
        mix.tick()
    assert len(mix.ring) == n + 10
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for _ in range(n):
        await mix.__anext__()
    assert loop.time() - t0 < 0.5  # the first PREROLL_S at once
    for _ in range(10):
        await mix.__anext__()
    assert loop.time() - t0 >= 0.15  # past it, real time
    mix.release()
    for _ in range(n + 5):
        mix.tick()
    assert len(mix.ring) == n


async def test_the_wake_catch_up_records_a_question_without_answering_it():
    """A sleeping Worker's question is silent context, not a question the voice asked the room. An unrelated new
    delegation must never be submitted as its answer."""
    q = {
        "id": "q1",
        "seq": 9,
        "text": "**Pulse** asks: What holiday hours?",
        "question": {"taskId": "t1", "qid": "q-1"},
    }
    b = _Backend([{"messages": [q], "after": 9}])
    m, opened = _sleepy(b)
    m.asked.append({"t": 0, "who": "Sam", "q": "draft it", "a": ""})
    m.heard(SAM, "Nova, any news?", final=True)
    await m.opening
    assert m.question is None and m.asked[-1]["a"] == "Pulse asks: What holiday hours?"
    assert opened[0].contexts == ["Team: **Pulse** asks: What holiday hours?"]
    await m.post("What is on my calendar?", [])
    assert "answers" not in b.asks()[-1]


@pytest.mark.parametrize("backlog_first", [True, False])
async def test_waking_voice_receives_backlog_without_waiting_to_open(backlog_first):
    """Either order of the fetch and session startup preserves the result in the current voice, without holding
    startup on a slow backend or speaking old results as fresh replies."""
    fetched, opened = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    msg = {"id": "a9", "seq": 9, "text": "We shipped #5095."}

    class B(_Backend):
        async def meet(self, op, body):
            if op == "events":
                if not backlog_first:
                    await release.wait()
                fetched.set()
                return {"messages": [msg], "after": 9}
            return await super().meet(op, body)

    voices = []

    async def open_voice(history):
        if backlog_first:
            await fetched.wait()
        voices.append(_Voice(history))
        opened.set()
        return voices[-1]

    m = _meeting(B(), open_voice=open_voice)
    m.heard(SAM, "Nova, did it ship?", final=False)
    opening = m.opening
    await asyncio.wait_for(opened.wait(), timeout=1)
    assert voices[0].history == []  # the snapshot preceded this wake's fetch
    release.set()
    await opening
    assert voices[0].contexts == ["Team: We shipped #5095."]
    assert m.history() == [("Team", "We shipped #5095.")]
    # The backend may repeat the result; the sleeping catch-up is still one copy.
    assert await m._backlog() == []


async def test_wake_backlog_does_not_append_to_a_voice_that_already_ended():
    release, opened = asyncio.Event(), asyncio.Event()
    msg = {"id": "a10", "seq": 10, "text": "The calendar is updated."}

    class B(_Backend):
        async def meet(self, op, body):
            await release.wait()
            return {"messages": [msg], "after": 10}

    voices = []

    async def open_voice(history):
        voices.append(_Voice(history))
        opened.set()
        return voices[-1]

    m = _meeting(B(), open_voice=open_voice)
    m.heard(SAM, "Nova, any news?", final=False)
    opening = m.opening
    await opened.wait()
    m.ended(voices[0])
    release.set()
    await opening
    assert m.voice is None and voices[0].contexts == []
    assert m.history() == [("Team", "The calendar is updated.")]


async def test_slow_wake_catch_up_precedes_the_live_events_reader():
    """live.start starts its events reader before returning. That reader must not race the wake's slow HTTP fetch
    and turn an old, unheard question into spoken commentary and an answer binding."""
    fetching, opened, reading, release = (asyncio.Event() for _ in range(4))
    q = {
        "id": "q11",
        "seq": 11,
        "text": "What holiday hours?",
        "question": {"taskId": "t1", "qid": "q-11"},
    }

    class B(_Backend):
        async def meet(self, op, body):
            if op == "events":
                self.sent.append((op, body))
                if len(self.sent) == 1:
                    fetching.set()
                    await release.wait()
                    return {"messages": [q], "after": 11}
                # Without the barrier this live read wins the race and speaks the sleeping question.
                return {"messages": [q], "after": 11}
            return await super().meet(op, body)

    b, readers, voices = B(), [], []

    async def open_voice(history):
        voices.append(_Voice(history))

        async def next_event():
            reading.set()
            return await m.events().__anext__()

        readers.append(asyncio.create_task(next_event()))
        opened.set()
        return voices[-1]

    m = _meeting(b, open_voice=open_voice)
    m.heard(SAM, "Nova, what shipped?", final=False)
    opening = m.opening
    await fetching.wait()
    await opened.wait()
    await reading.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert len(b.sent) == 1 and not readers[0].done()
        assert m.question is None
        release.set()
        await opening
        assert voices[0].contexts == ["Team: What holiday hours?"]
        await m.post("What shipped?", [])
        assert "answers" not in b.asks()[-1]
        assert not readers[0].done()
    finally:
        release.set()
        readers[0].cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await opening


async def test_a_voice_that_closed_itself_is_reopened_by_the_name():
    """M4: the AgentSession closed on its own (live.ended): asleep, so the next name opens a new one."""
    m, opened = _sleepy()
    m.heard(SAM, "Nova, hi", final=False)
    await m.opening
    m.ended(opened[0])
    assert m.voice is None
    m.heard(SAM, "Nova, are you there?", final=False)
    await m.opening
    assert len(opened) == 2 and m.voice is opened[1]


@pytest.mark.parametrize(
    "text",
    [
        "Nova, don't transcribe me",
        "Hey Nova, can you not transcribe me?",
        "Lightbulb, turn off transcription for my voice",
        "Nova, opt me out",
    ],
)
def test_opt_out_is_a_request_to_the_assistant_about_this_speaker(text):
    assert meet.opts_out("Nova", text)


@pytest.mark.parametrize(
    "text",
    [
        "Customers can opt out of reminder texts",
        "Nova, don't record my screen",
        "Don't transcribe me",
        "Sam said Nova should not transcribe me",
    ],
)
def test_ordinary_talk_and_other_peoples_requests_do_not_opt_anyone_out(text):
    assert not meet.opts_out("Nova", text)


async def test_opt_out_removes_only_that_person_from_notes_and_audio_and_persists():
    b, mix, stopped = _Backend(), meet.Mix(), []
    m = _meeting(b, mix=mix, on_opt_out=stopped.append)
    m.joiner.final(SAM, "Nova.")  # split STT final at the name
    frame = rtc.AudioFrame(np.full(meet.FRAME, 2000, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME)
    mix.push(SAM, frame)
    mix.tick()
    m.heard(SAM, "Please stop transcribing my voice", final=True)
    await asyncio.sleep(0)
    assert stopped == [SAM] and m.excluded(SAM) and not m.excluded(MAYA)
    assert not mix.ring and SAM not in mix._q  # mixed pre-roll contains their voice too
    mix.push(SAM, frame)  # a frame already in flight must not get back in
    mix.push(MAYA, frame)
    mix.tick()
    assert np.frombuffer((await mix.__anext__()).data, dtype=np.int16).tolist() == [2000] * meet.FRAME
    m.heard(SAM, "I should never enter the notes", final=True)
    m.joiner._flush(SAM)
    assert not m.lines and not m.joiner._parts
    assert b.sent == [("transcription", {"participantId": "12", "name": "Sam", "optOut": True})]
    # A new worker restores the persisted name when Recall gives the person another id.
    restored = _meeting(not_transcribed=m.not_transcribed)
    assert restored.excluded("meet:r:99", "sAm") and not restored.excluded(MAYA, "Maya")
    restored.person("meet:r:99", "Sam")
    restored.heard("meet:r:99", "This is excluded after restart", final=True)
    assert not restored.joiner._parts
    m.joiner.close()
    restored.joiner.close()
