"""The meeting agent on OpenAI GPT-Live (plan 2026-11-08-gpt-live-voice, Task 3): GPT-Live is the voice, the meeting
room's Router the one brain (agent.py hands it every `source: "meet"` dispatch). live.py runs the session; this file is
the meeting's ears and its bridge to the backend.

The simplest documented version (founder ruling, 8 Nov 2026): OpenAI publishes no multi-party guidance, and its lever
is the prompt. "Speak only when someone addresses you by name" is in the prompt's own policies (no backchannels, the
interruption policy, a "Do not generate <|SpawnThinking|> when" line); there is no code gate on speech or delegation.
Every delegation is posted (`/meet/{room}/ask`); GPT-Live decides. Measure it before adding anything on top.

Ears: GPT-Live hears every Meet person mixed (`Mix`; Recall gives one track a person). A per-person transcriber keeps
the meeting notes (who said what) and the talk sent with each ask. Team messages (`/meet/{room}/events`) go to GPT-Live
as results when the room has paused; a Worker's question carries its qid, and the next ask within 60 s answers it.

Sleep and wake (founder ruling, 8 Nov 2026): GPT-Live bills every open second, so it opens when someone first says
the assistant's name (the transcriber's only use of name detection: any interim or final line containing it), and
sleeps after SLEEP_S with no delegation and no speech of its own. Each open carries the recent meeting transcript as
`history`, and the team messages from while it slept, so it knows what people refer to; it hears the line that woke
it from the Mix's pre-roll (the wake-word pattern: the last PREROLL_S of audio first, then live). While open, GPT-Live
decides.
The meeting itself ends on Recall's call_ended (it closes the LiveKit room, routers/meetings.py), an hour of silence or
the 90-minute cap, and the voice is closed with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

import numpy as np
from livekit import rtc
from livekit.agents import JobContext, room_io, stt
from livekit.agents.voice.io import AudioInput
from livekit.plugins import noise_cancellation, openai

from huddle_agent import live
from huddle_agent.backend import Backend, BackendError
from huddle_agent.mouth import ANSWER_WITHIN_S, ENGLISH, LANGUAGE, Context, speakable
from huddle_agent.transcript import _LIGHTBULB, _tokens

logger = logging.getLogger("meet")
# the per-person transcriber: gpt-4o-mini-transcribe shuts down 26 Feb 2027; gpt-transcribe is its successor
# for committed turns, $0.0045/min (developers.openai.com/api/docs/deprecations, .../models/gpt-transcribe)
TRANSCRIBE = os.environ.get("MEET_TRANSCRIBE_MODEL", "gpt-transcribe")
ASSISTANT_ID = "assistant"  # the tile's agent id (lightbulb.agent.<id>.*)
TRACK = f"agent-{ASSISTANT_ID}"  # the meet-bot page plays only `agent-*` tracks into the Meet
MEET_QUIET_S = 3.0  # a team message waits for a real pause in the room
SLEEP_S = 5 * 60  # the voice closes after this long with no delegation and no speech of its own
HISTORY_S, HISTORY_LINES = 10 * 60, 60  # what of the meeting a reopened voice is told
SPEAKER_S = 20.0  # an ask names the newest line's speaker when that line is this recent
MEETING_IDLE_S = 60 * 60  # spec §4.4b: leave after an hour of silence
MEETING_MAX_S = float(os.environ.get("MEET_MAX_S", 90 * 60))  # cost cap: leave after 90 minutes whatever happens
TRANSCRIPT_FLUSH_S = 10
POLL_S = 1.0  # how often the meeting asks what the room's agents posted
RATE = 16000
FRAME = RATE // 50  # 20 ms
# The pre-roll a waking voice hears first, at once. Not more: the service rejects an input burst over 5 s of audio and
# the plugin keeps only the last 4 s queued before session.started (gpt_live.py:52-54, _trim_queued_audio).
PREROLL_S = 4.0
VOICED_RMS = 500  # int16 RMS a frame needs to count as someone speaking (after noise cancellation)
# ponytail: fixed threshold; tune from a real meeting's levels.
STT_VAD = {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 600, "silence_duration_ms": 400}
# STT_VAD's 400 ms silence splits a line at normal pace ("Nova." + "what have we shipped...?", 8 Nov prod Meet): a
# person's finals are one line until they stay quiet this long after one
JOIN_S = 1.5
STT_LANGUAGE = LANGUAGE  # pinned, as the phone and the orb (openai.STT defaults to it today)
TOOLS = [
    "The Router: the team's brain in this meeting's room. It knows the team's work across GitHub, email, calendar and docs.",
    "Memory: the Router remembers and recalls what the team has said and decided.",
    "Work threads: the Router starts work threads that hand tasks to the team's agents, such as a draft or a post, and "
    "puts their links in the meeting chat.",
]


def prompt(info: dict) -> str:
    """OpenAI's live-prompting template (developers.openai.com/api/docs/guides/live-prompting) with its three
    headings; the meeting's rule lives in them. The Meet-only "Do not generate" condition sits right under the rendered
    Delegation policy: live.delegation_policy takes no extra conditions, and live.py is Task 1's."""
    n = info["name"]
    return f"""You are {n}, a calm, friendly voice assistant for {info["company"]}, in the Google Meet "{info["title"]}". Today is {info["today"]}.
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
Several people are talking with each other in this meeting. Speak only when someone addresses you by name, "{n}". Stay silent for everything else, even a question to the room or a line that only mentions you.
Keep each reply to one or two short sentences. {ENGLISH}

Backchannel policy: None. Do not produce any backchannel interjections. Stay silent until someone addresses you by name.

Interruption policy: Stop speaking when anyone speaks over you, and when someone says "{n}, stop". Listen to what they say.

{live.delegation_policy(TOOLS)}
Do not generate <|SpawnThinking|> when:
- The talk is between other people and does not address you.

Anything that spends, sends or publishes waits for the inviting member's OK on a card in the chat. Never approve, confirm or promise one by voice."""


class Joiner:
    """One person's consecutive final transcripts as one line: each final waits JOIN_S for the next, and an interim
    (`speaking`: they are talking again) holds it until that next final lands. `on_line(who, text)` gets the whole
    line once, so the notes keep "Nova." + "what have we shipped?" together and a long answer is never cut."""

    def __init__(self, on_line, wait: float = JOIN_S) -> None:
        self._on_line, self._wait = on_line, wait
        self._parts: dict[str, list[str]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    def final(self, who: str, text: str) -> None:
        self._parts.setdefault(who, []).append(text.strip())
        self._hold(who)
        self._timers[who] = asyncio.get_running_loop().call_later(self._wait, self._flush, who)

    def speaking(self, who: str) -> None:
        if who in self._parts:
            self._hold(who)

    def _hold(self, who: str) -> None:
        if (t := self._timers.pop(who, None)) is not None:
            t.cancel()

    def _flush(self, who: str) -> None:
        self._timers.pop(who, None)
        if parts := self._parts.pop(who, None):
            self._on_line(who, " ".join(parts))

    def close(self) -> None:
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()


def voiced(frame: rtc.AudioFrame) -> bool:
    """Someone is speaking in this 20 ms frame (the pause rule)."""
    a = np.frombuffer(frame.data, dtype=np.int16).astype(np.float64)
    return bool(len(a)) and float(np.sqrt(np.mean(a**2))) >= VOICED_RMS


class Mix(AudioInput):
    """Every Meet person's audio as the one stream GPT-Live hears (RoomIO links one participant; Recall gives one
    track a person): mixed every 20 ms by `run`, summed and clipped, silence when nobody speaks, into a rolling
    PREROLL_S ring. Asleep, nobody reads it and it keeps only the newest PREROLL_S; on wake the new session reads that
    pre-roll at once and then the live mix, so it hears the whole "Nova, what shipped this week?" itself."""

    def __init__(self) -> None:
        super().__init__(label="meet-mix")
        self._q: dict[str, deque[np.ndarray]] = {}
        self.ring: deque[rtc.AudioFrame] = deque(maxlen=int(PREROLL_S * RATE / FRAME))
        self._new = asyncio.Event()
        self._read_s, self._attached_at = 0.0, None  # audio read since attach, and when

    def hold(self) -> None:
        """The wake: the ring stops rolling, so the startup wait never evicts the line that woke it."""
        self.ring = deque(self.ring)

    def release(self) -> None:
        """Asleep again: the ring rolls, and the next attach starts its own pacing."""
        self.ring, self._read_s, self._attached_at = deque(self.ring, maxlen=self.ring.maxlen or int(PREROLL_S * RATE / FRAME)), 0.0, None

    def push(self, who: str, frame: rtc.AudioFrame) -> None:
        self._q.setdefault(who, deque(maxlen=3)).append(np.frombuffer(frame.data, dtype=np.int16))  # 60 ms, no backlog

    def drop(self, who: str) -> None:
        self._q.pop(who, None)

    def tick(self) -> None:
        """One 20 ms frame of the room into the ring."""
        out = np.zeros(FRAME, np.int32)
        for q in list(self._q.values()):
            if q and len(f := q.popleft()) == FRAME:
                out += f
        self.ring.append(rtc.AudioFrame(np.clip(out, -32768, 32767).astype(np.int16).tobytes(), RATE, 1, FRAME))
        self._new.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        at = loop.time()
        while True:
            at = max(at + FRAME / RATE, loop.time() - 0.2)  # ponytail: catch up at most 200 ms behind
            await asyncio.sleep(at - loop.time())
            self.tick()

    async def __anext__(self) -> rtc.AudioFrame:
        """At once while under PREROLL_S ahead of real time (the service rejects a burst over 5 s), then real time."""
        while not self.ring:
            self._new.clear()
            await self._new.wait()
        now = asyncio.get_running_loop().time()
        if self._attached_at is None:
            self._attached_at = now
        if (ahead := self._read_s - (now - self._attached_at)) > PREROLL_S:
            await asyncio.sleep(ahead - PREROLL_S)
        self._read_s += FRAME / RATE
        return self.ring.popleft()


def names_it(name: str, text: str) -> bool:
    """The line contains the assistant's name (or "Lightbulb") anywhere: what wakes a sleeping voice."""
    tok = _tokens(text)
    return any(n and tuple(tok[i : i + len(n)]) == n for n in (tuple(_tokens(name)), *_LIGHTBULB) for i in range(len(tok)))


class Meeting:
    """The meeting between the transcriber, GPT-Live and the backend: who said what (`lines`, `asked`), each
    delegation posted (`post`) and the team's messages (`events`)."""

    def __init__(self, backend, name: str, after: int = 0, quiet=lambda: True, poll_s: float = POLL_S,
                 open_voice=None, mix: Mix | None = None) -> None:  # fmt: skip
        self._b, self.name, self.after, self.quiet, self.poll_s = backend, name, after, quiet, poll_s
        self._open_voice, self.mix = open_voice, mix  # open_voice: async (history) -> LiveSession
        self._turn = asyncio.Lock()  # a wake waits for a sleep's close in flight
        self.voice = None  # the LiveSession while awake
        self.opening: asyncio.Task | None = None
        self.active_at = time.monotonic()  # GPT-Live's last delegation or speech
        self.names: dict[str, str] = {}
        self.context = Context()
        self.lines: list[dict] = []
        self.asked: list[dict] = []  # {t, who, q, a}: the meeting's record
        self.last: tuple[float, str] | None = None  # (when, who): the newest line
        self.question: dict | None = None  # {taskId, qid}: the Worker question last sent to the voice
        self.question_at = 0.0
        self.seen: set[str] = set()  # message ids and qids already handed to the voice
        self.started = time.monotonic()
        self.joiner = Joiner(self.line)

    def person(self, identity: str, name: str) -> None:
        self.names[identity] = name or "Someone"

    def label(self, identity: str) -> str:
        return self.names.get(identity, "Someone") if identity else "Several people"

    def heard(self, who: str, text: str, final: bool) -> None:
        if not text.strip():
            return
        if self.voice is None and self.opening is None and self._open_voice and names_it(self.name, text):
            self.opening = asyncio.create_task(self._wake())
        if final:
            self.joiner.final(who, text)
        else:
            self.joiner.speaking(who)

    def line(self, who: str, text: str) -> None:
        """A person's whole line (Joiner): the notes and the talk sent with the next ask."""
        self.last = (time.monotonic(), who)
        self.context.add(self.label(who), text)
        self.lines.append({"mark": len(self.lines), "kind": "person", "name": self.label(who)[:200],
                           "text": text[:4000], "at": time.time()})  # fmt: skip

    # --- sleep and wake -------------------------------------------------------------------------------------------

    def activity(self) -> None:
        """GPT-Live delegated or spoke: the sleep timer starts again."""
        self.active_at = time.monotonic()

    def history(self) -> list[tuple[str, str]]:
        """The recent meeting as (speaker, text), oldest first; the team's messages as "Team"."""
        since = time.time() - HISTORY_S
        return [("Team" if ln["kind"] == "agent" else ln["name"], ln["text"]) for ln in self.lines
                if ln["at"] >= since][-HISTORY_LINES:]  # fmt: skip

    async def _wake(self) -> None:
        """Opens the voice with the recent meeting. The words that named it reach it as audio: the Mix pre-roll, held
        from this moment, then the live mix. Team messages from while it slept are fetched alongside and appended as
        silent context once open, so they reach this wake without delaying it or replaying old replies aloud."""
        if self.mix is not None:
            self.mix.hold()
        woke = time.monotonic()
        backlog = None
        try:
            async with self._turn:
                backlog = asyncio.create_task(self._backlog())
                self.activity()
                self.voice = voice = await self._open_voice(self.history())
                self.activity()
                logger.info("meet: voice awake %.2f s after the name", time.monotonic() - woke)
                for text in await backlog:
                    if self.voice is voice:  # a spontaneous close while the fetch waited: keep only the notes
                        voice.send_context(text)
        except Exception:  # noqa: BLE001 -- the next line with the name tries again
            logger.exception("meet: voice not opened")
            if self.mix is not None:
                self.mix.release()
        finally:
            if backlog is not None and not backlog.done():
                backlog.cancel()
                await asyncio.gather(backlog, return_exceptions=True)
            self.opening = None

    async def _backlog(self) -> list[str]:
        """Record missed messages and return silent context. A question not said by the voice must not arm the
        next delegation as its answer; normal live events still carry that binding when presented."""
        try:
            out = await self._b.meet("events", {"after": self.after})
        except BackendError as e:
            logger.warning("meet: events failed (%s)", e.code)
            return []
        context = []
        for m in out.get("messages", []):
            if (text := speakable(m.get("text") or "")) and not self._keys(m) & self.seen:
                self._record(m, text)
                context.append(f"Team: {m['text'].strip()}")
        self.after = max(self.after, int(out.get("after") or 0))
        return context

    def ended(self, voice) -> None:
        """The voice's AgentSession closed (on its own, or ours): asleep, so a name opens it again."""
        if self.voice is voice:
            self.voice = None
            if self.mix is not None:
                self.mix.release()

    async def _close(self) -> None:
        voice, self.voice = self.voice, None
        if voice.session is not None:
            voice.session.input.audio = None  # the mix is free for the next session
        if self.mix is not None:
            self.mix.release()
        await voice.close()

    async def sleep(self) -> None:
        """Closes the voice after SLEEP_S with no delegation and no speech of its own."""
        if self.voice is not None and self.opening is None and time.monotonic() - self.active_at > SLEEP_S:
            logger.info("meet: voice asleep")
            async with self._turn:
                await self._close()

    async def close(self) -> None:
        if self.opening is not None:
            await asyncio.gather(self.opening, return_exceptions=True)
        if self.voice is not None:
            await self._close()

    @staticmethod
    def _keys(m: dict) -> set[str]:
        return {m["id"], (m.get("question") or {}).get("qid")} - {None}

    def _record(self, m: dict, text: str) -> None:
        """A team message into the record once: seen, the notes, the last ask's answer."""
        self.seen |= self._keys(m)
        self._note(m["text"])
        if self.asked:
            self.asked[-1]["a"] = f"{self.asked[-1]['a']} {text}".strip()[:2000]

    def _note(self, text: str) -> None:
        self.lines.append({"mark": len(self.lines), "kind": "agent", "name": self.name[:200], "text": text[:4000],
                           "at": time.time()})  # fmt: skip

    async def post(self, text: str, _turns: list[str]) -> str | None:
        """GPT-Live's delegation (live.py), posted as it asked it, with the transcriber's talk since the last ask (who
        said what: GPT-Live hears the meeting mixed) and the newest line's speaker. Returns the message id, or None
        when the backend did not post it."""
        self.activity()
        pid = ""
        if self.last and time.monotonic() - self.last[0] <= SPEAKER_S:
            pid = self.last[1]
        body = {"text": self.context.take(text)[:4000], "participantId": pid.rsplit(":", 1)[-1],
                "speakerName": self.label(pid) if pid else ""}  # fmt: skip
        if self.question and time.monotonic() - self.question_at <= ANSWER_WITHIN_S:
            body["answers"] = self.question
        self.question = None
        self.asked.append({"t": int(time.monotonic() - self.started), "who": body["speakerName"] or "Someone",
                           "q": text[:2000], "a": ""})  # fmt: skip
        try:
            out = await self._b.meet("ask", body)
        except BackendError as e:
            logger.warning("meet: ask not posted (%s)", e.code)
            return None
        return str(out["messageId"]) if out.get("messageId") else None

    async def events(self):
        """Each new agent message in the meeting's room, once, when the room has paused. A Worker's question carries
        its ref: the next ask within ANSWER_WITHIN_S answers it."""
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                out = await self._b.meet("events", {"after": self.after})
            except BackendError as e:
                logger.warning("meet: events failed (%s)", e.code)
                continue
            for m in out.get("messages", []):
                if self._keys(m) & self.seen or not (text := speakable(m.get("text") or "")):
                    continue
                self._record(m, text)  # before the wait: a sleep that cancels it never loses the note
                while not self.quiet():
                    await asyncio.sleep(max(self.poll_s, 0.01))
                if q := m.get("question"):
                    self.question, self.question_at = q, time.monotonic()
                yield {"kind": "agent", "id": m["id"], "text": text,
                       **{k: m[k] for k in ("question", "inReplyTo") if m.get(k)}}  # fmt: skip
            self.after = max(self.after, int(out.get("after") or 0))


async def run(ctx: JobContext, meta: dict) -> None:
    """The meeting, from the dispatch until the room closes."""
    wall = time.time()
    room_id = meta["roomId"]
    backend = Backend(room_id, meta["ownerId"], meta["roomToken"])
    await ctx.connect()
    try:
        info = await backend.meet("start", {"company": meta.get("company", "")})
    except BackendError as e:
        logger.error("meet: no meeting for room %s (%s)", room_id, e.code)
        return
    name = info["assistantName"]
    heard_at = {"voiced": time.monotonic()}  # when anyone last spoke
    mix = Mix()
    mixing = asyncio.create_task(mix.run())
    tile = {f"lightbulb.agent.{ASSISTANT_ID}.name": name}

    def on_state(ev) -> None:
        if ev.new_state in ("speaking", "thinking"):
            meeting.activity()
        status = {"speaking": "speaking", "thinking": "working"}.get(ev.new_state, "listening")
        asyncio.create_task(ctx.room.local_participant.set_attributes(
            {**tile, f"lightbulb.agent.{ASSISTANT_ID}.status": status}
        ))  # fmt: skip

    async def open_voice(history: list[tuple[str, str]]):
        voice = await live.start(
            ctx,
            prompt=prompt({"name": name, "company": meta.get("company") or "the team", "title": info["title"],
                           "today": time.strftime("%A %Y-%m-%d")}),  # fmt: skip
            surface="meet",
            post=meeting.post,
            events=meeting.events,
            room_options=room_io.RoomOptions(
                audio_input=False,  # GPT-Live hears `mix`: every Meet person, not one linked participant
                text_input=False,
                close_on_disconnect=False,
                audio_output=room_io.AudioOutputOptions(track_name=TRACK),
            ),  # fmt: skip
            history=history,
        )
        if voice.session is not None:
            voice.session.input.audio = mix
            voice.session.on("agent_state_changed", on_state)
            voice.session.on("close", lambda *_: meeting.ended(voice))
        return voice

    meeting = Meeting(backend, name, int(info.get("after") or 0), open_voice=open_voice, mix=mix,
                      quiet=lambda: time.monotonic() - heard_at["voiced"] >= MEET_QUIET_S)  # fmt: skip
    readers: dict[str, asyncio.Task] = {}
    # the name in its prompt: unprompted, an accented "Lightbulb, can you..." came out as "la lampe" (self-test)
    stt_engine = openai.STT(
        model=TRANSCRIBE, use_realtime=True, turn_detection=STT_VAD, language=STT_LANGUAGE,
        prompt=f'A meeting at {meta.get("company") or "a business"}. People ask the AI assistant "{name}" things '
        f'by name: "{name}, ...".',
    )  # fmt: skip

    async def read(track: rtc.Track, identity: str) -> None:
        stream = rtc.AudioStream.from_track(
            track=track, sample_rate=RATE, num_channels=1, frame_size_ms=20,
            noise_cancellation=noise_cancellation.NC(),
        )  # fmt: skip
        words = stt_engine.stream()

        async def listen() -> None:
            async for ev in words:
                if ev.alternatives and ev.type in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT):
                    meeting.heard(identity, ev.alternatives[0].text, ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT)

        listener = asyncio.create_task(listen())
        try:
            async for ev in stream:
                if voiced(ev.frame):
                    heard_at["voiced"] = time.monotonic()
                words.push_frame(ev.frame)
                mix.push(identity, ev.frame)
        finally:
            listener.cancel()
            mix.drop(identity)
            await words.aclose()
            await stream.aclose()

    def on_track(track: rtc.Track, _pub, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.identity.startswith("meet:"):
            meeting.person(p.identity, p.name)
            if (old := readers.pop(p.identity, None)) is not None:
                old.cancel()
            readers[p.identity] = asyncio.create_task(read(track, p.identity))

    def off_track(_track, _pub, p: rtc.RemoteParticipant) -> None:
        if (t := readers.pop(p.identity, None)) is not None:
            t.cancel()

    ctx.room.on("track_subscribed", on_track)
    ctx.room.on("track_unsubscribed", off_track)
    for p in ctx.room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track is not None:
                on_track(pub.track, pub, p)

    sent = {"n": 0}

    async def flush(final: bool = False) -> None:
        batch = meeting.lines if final else meeting.lines[sent["n"] :]
        if batch:
            await backend.transcript(f"{room_id}:{int(wall)}", batch, final=final)
            sent["n"] = len(meeting.lines)

    async def time_up() -> None:
        await asyncio.sleep(MEETING_MAX_S)
        logger.info("meet: max length reached, leaving room %s", room_id)
        await backend.leave_meeting()
        ctx.shutdown("meeting max length")

    limit = asyncio.create_task(time_up())

    async def on_end(_reason: str = "") -> None:
        limit.cancel()
        mixing.cancel()
        meeting.joiner.close()
        for t in readers.values():
            t.cancel()
        asked = meeting.asked
        summary = f"Asked {len(asked)} time{'s' if len(asked) != 1 else ''}"
        for step in (meeting.close(), flush(final=True), backend.meet("end", {"asked": asked, "summary": summary})):
            try:
                await step
            except Exception:  # noqa: BLE001 -- one lost write must not cost the others
                logger.exception("meet: end step failed room=%s", room_id)
        await backend.aclose()

    ctx.add_shutdown_callback(on_end)
    await ctx.room.local_participant.set_attributes({**tile, f"lightbulb.agent.{ASSISTANT_ID}.status": "listening"})
    logger.info("meet: listening in room %s as %s", room_id, name)
    gone = asyncio.Event()
    ctx.room.on("disconnected", lambda *_: gone.set())
    while not gone.is_set():
        try:
            await asyncio.wait_for(gone.wait(), TRANSCRIPT_FLUSH_S)
        except TimeoutError:
            pass
        try:
            await flush()
        except Exception:  # noqa: BLE001 -- the final flush resends everything
            logger.warning("meet: transcript flush failed", exc_info=True)
        try:
            await meeting.sleep()
        except Exception:  # noqa: BLE001 -- the next tick tries again
            logger.warning("meet: voice sleep failed", exc_info=True)
        if time.monotonic() - heard_at["voiced"] > MEETING_IDLE_S:
            logger.info("meet: an hour of silence, leaving room %s", room_id)
            await backend.leave_meeting()  # the backend ends the meeting and closes the room
            ctx.shutdown("meeting idle")
            return
