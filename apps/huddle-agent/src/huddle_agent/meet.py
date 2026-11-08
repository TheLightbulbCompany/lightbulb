"""The meeting agent on OpenAI GPT-Live (plan 2026-11-08-gpt-live-voice, Task 3): GPT-Live is the voice, the meeting
room's Router the one brain (agent.py hands it every `source: "meet"` dispatch). live.py runs the session; this file is
the meeting's ears, its permission gate and its playback gate.

Ears: a per-person transcriber (one stream per Meet track from Recall, so every line has its exact speaker) feeds the
wake rules (`Gate`, `wakes`, over transcript.py's), the meeting notes and the delegation's source line; the same
frames, mixed (`Mix`), are what GPT-Live hears. Speak only when named rests on three things: GPT-Live's prompt, a
delegation is posted (`/meet/{room}/ask`, naming the speaker) only for a line that named the assistant, and GPT-Live's
audio reaches the meeting only while the current line named it or a team message is being said (application-
controlled playback, developers.openai.com/api/docs/guides/voice-server-controls#control-playback-when-needed: the
client cannot cancel a response, so the voice track is muted at the relay). Team messages (`/meet/{room}/events`) go
to GPT-Live as results when the room has paused.

The worker never leaves on an empty room: Recall's call_ended closes the LiveKit room (routers/meetings.py), and an
hour of silence pulls the bot. The GPT-Live session is closed with the meeting: it bills every open second.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from livekit import rtc
from livekit.agents import JobContext, room_io, stt
from livekit.agents.voice.io import AudioInput
from livekit.plugins import noise_cancellation, openai

from huddle_agent import live
from huddle_agent.backend import Backend, BackendError
from huddle_agent.mouth import ANSWER_WITHIN_S, ENGLISH, LANGUAGE, Context, speakable
from huddle_agent.transcript import (
    _LEADS,
    _LIGHTBULB,
    _tokens,
    address,
    clause_openers,
    stop_request,
)

logger = logging.getLogger("meet")
# the gate's per-person transcriber: gpt-4o-mini-transcribe shuts down 26 Feb 2027; gpt-transcribe is its successor
# for committed turns, $0.0045/min (developers.openai.com/api/docs/deprecations, .../models/gpt-transcribe)
TRANSCRIBE = os.environ.get("MEET_TRANSCRIBE_MODEL", "gpt-transcribe")
ASSISTANT_ID = "assistant"  # the tile's agent id (lightbulb.agent.<id>.*)
TRACK = f"agent-{ASSISTANT_ID}"  # the meet-bot page plays only `agent-*` tracks into the Meet
MEET_QUIET_S = 3.0  # a line waits for a real pause in the room
STALE_S = 20.0  # spec §4.4b.4: the gate drops an ask this old
MEETING_IDLE_S = 60 * 60  # spec §4.4b: leave after an hour of silence
MEETING_MAX_S = float(os.environ.get("MEET_MAX_S", 90 * 60))  # cost cap: leave after 90 minutes whatever happens
TRANSCRIPT_FLUSH_S = 10
POLL_S = 1.0  # how often the meeting asks what the room's agents posted
RATE = 16000
ADDRESSED = ("ask",)
VOICED_RMS = 500  # int16 RMS a frame needs to count as someone speaking (after noise cancellation)
STT_VAD = {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 600, "silence_duration_ms": 400}
# STT_VAD's 400 ms silence splits a line at normal pace ("Nova." + "what have we shipped...?", 8 Nov prod Meet): a
# person's finals are one line until they stay quiet this long after one
JOIN_S = 1.5
ASK_WAIT_S = 4.0  # a delegation may land before its line is judged (JOIN_S plus the transcriber's lag): it waits this long
FRAME = RATE // 50  # 20 ms
TOOLS = [
    "The Router: the team's brain in this meeting's room. It knows the team's work across GitHub, email, calendar and docs.",
    "Memory: the Router remembers and recalls what the team has said and decided.",
    "Work threads: the Router starts work threads that hand tasks to the team's agents, such as a draft or a post, and "
    "puts their links in the meeting chat.",
]
STT_LANGUAGE = LANGUAGE  # pinned, as the phone and the orb (openai.STT defaults to it today)
# ponytail: fixed threshold; tune from a real meeting's levels.


REQUEST_WORDS = {
    "please", "tell", "give", "show", "read", "draft", "write", "add", "start", "make", "put", "send", "check",
    "find", "look", "pull", "book", "move", "cancel", "remind", "help", "list", "summarize", "schedule",
}  # fmt: skip
QUESTION_WORDS = {
    "how", "what", "what's", "when", "where", "who", "why", "which", "is", "are", "was", "were", "do", "does",
    "did", "can", "could", "would", "will", "should",
}  # fmt: skip


def wakes(name: str, text: str, people: list[dict] | None = None) -> bool:
    """The strict wake (the nudge's): a sentence OPENS with the name (after an optional "hey"/"ok") and asks --
    a comma and two words or more; straight into a request word ("Nova read me..."); or straight into a question
    word in a sentence ending "?" ("Nova is the best." is a statement). Never a mention, a short line or a near
    name ("I asked Nova.", "Nova's great.", "Novak, can you..."), and never when someone in the room is called
    that (a participant "Nova Smith": "Nova, can you share your screen?" is to them)."""
    mine = tuple(_tokens(name))
    if any(tuple(_tokens(p.get("name") or ""))[: len(mine)] == mine for p in people or []):
        return False
    sentences = re.split(r"(?<=[.?!])\s+", text.strip())
    for k, sentence in enumerate(sentences):
        tok = _tokens(sentence)
        i = 1 if len(tok) > 1 and tok[0] in _LEADS else 0
        i += 1 if i and tok[i] == "," else 0
        for n in (mine, *_LIGHTBULB):
            if not n or tuple(tok[i : i + len(n)]) != n or len(tok) <= i + len(n):
                continue
            rest = tok[i + len(n) :]
            said = [w for w in rest if w[0].isalnum()]
            if rest[0] == "," and len(said) >= 2:
                return True
            if rest[0] in REQUEST_WORDS or (rest[0] in QUESTION_WORDS and sentence.rstrip().endswith("?")):
                return True
            # the name as a sentence of its own, the ask in the next ("Lightbulb. How many calls today?": the
            # transcriber's full stop after the name, 8 Nov sim)
            nxt = _tokens(sentences[k + 1]) if rest[0] == "." and len(said) == 0 and k + 1 < len(sentences) else []
            if nxt and (nxt[0] in REQUEST_WORDS or (nxt[0] in QUESTION_WORDS and sentences[k + 1].rstrip().endswith("?"))):
                return True
    return False


@dataclass
class Gate:
    """Who the assistant may answer, from the session's input transcript. Pure: the clock is passed in."""

    name: str
    people: list[dict] = field(default_factory=list)
    ask_at: float | None = None  # an open ask: when it was heard
    asker: str = ""  # who made it (Meet participant identity)

    def heard(self, who: str, text: str, now: float, *, new_turn: bool) -> str:
        """One (cumulative) transcript of a person's turn. Returns "stop", "ask" or "".
        `new_turn`: the first text of a turn (a later interim of the same turn only upgrades it)."""
        agents = [{"id": ASSISTANT_ID, "name": self.name, "defaultName": False}]
        if stop_request(text, agents):  # "Nova, stop", or a bare "stop" over it
            self.ask_at = None
            return "stop"
        if self.ask_at is not None and self.asker == who:
            return ""  # the same ask, still being said
        hit = address(text, agents, self.people)
        # Gemini's input transcript joins several sentences into one turn ("Okay, let's start. Lightbulb, how
        # many calls ...", live test 7 Nov): the name may open any sentence of it
        opens = any(
            (h := address(part, agents, self.people)).lightbulb or h.agent_ids
            for part in re.split(r"(?<=[.?!])\s+", text)[1:]
        )
        # named in a wake position, at a clause start ("..., Nova, what does it cost?"), or in a short line
        # ("Hi Nova", "Thanks, Nova"); a longer line that only mentions it ("I asked Nova ...") is not an ask
        if (
            hit.lightbulb
            or hit.agent_ids
            or opens
            or clause_openers(text, [{**agents[0], "defaultName": True}])
            or (hit.anywhere and len(text.split()) <= 5)
        ):
            self.ask_at, self.asker = now, who
            return "ask"
        # No follow-up rule either (huddle's unnamed follow-up): the 7 Nov live run answered "Leo, did you push
        # the fix?" as one. The assistant speaks only when named (spec §5).
        # No "question nobody took" rule (huddle's 3.5 s): with Gemini's transcript seconds behind the audio,
        # who took a question is not known in time, and the replay showed answers to questions a person had
        # already answered. The assistant speaks only when named (spec §5).
        return ""

    def verdict(self, now: float, quiet_s: float) -> bool | None:
        """May a reply start now? True play, False drop, None wait (the room is not quiet yet)."""
        if self.ask_at is not None:
            if now - self.ask_at > STALE_S:
                self.ask_at = None
                return False
            return True if quiet_s >= MEET_QUIET_S else None
        return False

    def answered(self, now: float) -> None:
        self.ask_at = None


class Joiner:
    """One person's consecutive final transcripts as one line: each final waits JOIN_S for the next, and an interim
    (`speaking`: they are talking again) holds it until that next final lands. `on_line(who, text)` gets the whole
    line once, so the gate judges "Nova." + "what have we shipped?" together and a long answer is never cut."""

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

    def held(self, who: str) -> str:
        """The finals of `who`'s line not yet handed on."""
        return " ".join(self._parts.get(who, []))

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


def addressed(gate: Gate, name: str, who: str, text: str, now: float) -> str:
    """"stop", "ask" (the line is for the assistant: the gate's ask and the strict wake) or "" (side talk). A
    final transcript, judged whole on its own words."""
    gate.ask_at = None
    kind = gate.heard(who, text, now, new_turn=True)
    if kind == "stop":
        return "stop"
    return "ask" if kind in ADDRESSED and wakes(name, text, gate.people) else ""


def prompt(info: dict) -> str:
    """OpenAI's live-prompting template (developers.openai.com/api/docs/guides/live-prompting): its three headings, the
    meeting's rule of speaking only when named, and the Router's capabilities. Task procedure stays with the Router."""
    n = info["name"]
    return f"""You are {n}, a calm, friendly voice assistant for {info["company"]}, in the Google Meet "{info["title"]}". Today is {info["today"]}.
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
Several people are talking with each other in this meeting. Speak only when someone says your name, "{n}", to ask or tell you something. Stay silent for everything else, even a question to the room or a line that mentions you. "{n}, stop" means stop speaking at once.
Keep each reply to one or two short sentences. {ENGLISH}

Backchannel policy: Do not produce any backchannel interjections while anyone is speaking. Backchanneling is entirely disallowed: stay silent until someone names you.

Interruption policy: Stop speaking when anyone speaks. Listen to what they say.

{live.delegation_policy(TOOLS)}
Do not generate <|SpawnThinking|> for talk that does not name you.

Anything that spends, sends or publishes waits for the inviting member's OK on a card in the chat. Never approve, confirm or promise one by voice."""


class Mix(AudioInput):
    """Every Meet person's audio as the one stream GPT-Live hears (RoomIO links one participant; Recall gives one
    track a person): 20 ms frames summed, silence when nobody speaks, paced in real time."""

    def __init__(self) -> None:
        super().__init__(label="meet-mix")
        self._q: dict[str, deque[np.ndarray]] = {}
        self._next = 0.0

    def push(self, who: str, frame: rtc.AudioFrame) -> None:
        self._q.setdefault(who, deque(maxlen=50)).append(np.frombuffer(frame.data, dtype=np.int16))  # 1 s backlog

    def drop(self, who: str) -> None:
        self._q.pop(who, None)

    async def __anext__(self) -> rtc.AudioFrame:
        now = asyncio.get_running_loop().time()
        self._next = max(self._next + FRAME / RATE, now - 0.2)  # ponytail: catch up at most 200 ms behind
        await asyncio.sleep(self._next - now)
        out = np.zeros(FRAME, np.int32)
        for q in list(self._q.values()):
            if q and len(f := q.popleft()) == FRAME:
                out += f
        return rtc.AudioFrame(np.clip(out, -32768, 32767).astype(np.int16).tobytes(), RATE, 1, FRAME)


def named(gate: Gate, who: str, text: str) -> str:
    """The loose check (the gate's ask: a greeting or thanks to it counts): "stop", "ask" or "". The gate's state is
    left as it was."""
    return Gate(name=gate.name, people=gate.people).heard(who, text, 0, new_turn=True)


class Meeting:
    """The meeting's state between the transcriber, GPT-Live and the backend: who said what (`lines`, `asked`), the
    playback gate on the voice track, the delegation gate (`post`) and the team's messages (`events`)."""

    def __init__(self, backend, name: str, room, after: int = 0, quiet=lambda: True, poll_s: float = POLL_S,
                 wait_s: float = ASK_WAIT_S) -> None:  # fmt: skip
        self._b, self.name, self.room, self.after = backend, name, room, after
        self.quiet, self.poll_s, self.wait_s = quiet, poll_s, wait_s
        self.gate = Gate(name=name)
        self.names: dict[str, str] = {}
        self.context = Context()
        self.lines: list[dict] = []
        self.asked: list[dict] = []  # {t, who, q, a}: the meeting's record
        self.last: dict | None = None  # the newest judged line: {at, who, text, ask}
        self.talking: set[str] = set()  # people heard whose line is not judged yet
        self.judged = asyncio.Event()
        self.question: dict | None = None  # {taskId, qid}: the Worker question last sent to the voice
        self.question_at = 0.0
        self.open: bool | None = None
        self.started = time.monotonic()
        self.joiner = Joiner(self.line)
        room.on("local_track_published", lambda *_: self._apply())
        self.play(False)

    def person(self, identity: str, name: str) -> None:
        self.names[identity] = name or "Someone"
        self.gate.people = [{"name": n} for n in self.names.values()]

    def label(self, identity: str) -> str:
        return self.names.get(identity, "Someone") if identity else "Several people"

    def play(self, on: bool) -> None:
        """The voice reaches the meeting only while on: GPT-Live's response cannot be cancelled, its track can."""
        if on != self.open:
            self.open = on
            self._apply()

    def _apply(self) -> None:
        for pub in self.room.local_participant.track_publications.values():
            if pub.track is not None and (pub.name or "").startswith("agent-"):
                pub.track.unmute() if self.open else pub.track.mute()

    def heard(self, who: str, text: str, final: bool) -> None:
        """A transcript as it comes: the voice track opens as soon as the words name the assistant, closes when they
        do not (someone else has the floor), and a stop shuts it at once."""
        if not text.strip():
            return
        self.talking.add(who)
        kind = named(self.gate, who, f"{self.joiner.held(who)} {text}".strip())
        self.play(kind == "ask")
        if final:
            self.joiner.final(who, text)
        else:
            self.joiner.speaking(who)

    def line(self, who: str, text: str) -> None:
        """A person's whole line (Joiner): judged once, for the playback gate, the delegation gate and the notes."""
        self.talking.discard(who)
        kind = addressed(self.gate, self.name, who, text, time.monotonic())
        logger.info("meet: line kind=%s", kind or "-")
        self.play(kind != "stop" and named(self.gate, who, text) == "ask")
        if kind == "ask":
            self.asked.append({"t": int(time.monotonic() - self.started), "who": self.label(who), "q": text, "a": ""})
        self.context.add(self.label(who), text)
        self.last = {"at": time.monotonic(), "who": who, "text": text, "ask": kind == "ask"}
        self.lines.append({"mark": len(self.lines), "kind": "person", "name": self.label(who)[:200],
                           "text": text[:4000], "at": time.time()})  # fmt: skip
        self.judged.set()

    def _current(self) -> bool:
        return self.last is not None and time.monotonic() - self.last["at"] <= STALE_S

    async def post(self, _text: str, _turns: list[str]) -> str:
        """GPT-Live's delegation (live.py): posted only when the current line named the assistant (a permission
        gate, not speech), as that line, by its speaker, with the talk since the last ask. One line, one post.
        GPT-Live's own wording of the ask is not used: it hears the meeting mixed, the transcriber knows who spoke."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.wait_s
        while self.talking or not self._current():  # the line it delegated on is not judged yet
            self.judged.clear()
            try:
                await asyncio.wait_for(self.judged.wait(), max(0.0, end - loop.time()))
            except TimeoutError:
                break
        line = self.last
        if not (line and line["ask"] and self._current()):
            logger.info("meet: delegation not posted (no line named the assistant)")
            return ""
        self.last = None
        body = {"text": self.context.take(line["text"])[:4000], "participantId": line["who"].rsplit(":", 1)[-1],
                "speakerName": self.label(line["who"])}  # fmt: skip
        if self.question and time.monotonic() - self.question_at <= ANSWER_WITHIN_S:
            body["answers"] = self.question
        self.question = None
        try:
            out = await self._b.meet("ask", body)
        except BackendError as e:
            logger.warning("meet: ask not posted (%s)", e.code)
            return ""
        return str(out.get("messageId") or "")

    async def events(self):
        """Each new agent message in the meeting's room, once, when the room has paused: the voice track opens for
        it. A Worker's question carries its ref: the next line posted within ANSWER_WITHIN_S answers it."""
        seen: set[str] = set()
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                out = await self._b.meet("events", {"after": self.after})
            except BackendError as e:
                logger.warning("meet: events failed (%s)", e.code)
                continue
            for m in out.get("messages", []):
                q = m.get("question")
                keys = {m["id"], (q or {}).get("qid")} - {None}
                if keys & seen or not (text := speakable(m.get("text") or "")):
                    continue
                seen |= keys
                while not self.quiet():
                    await asyncio.sleep(max(self.poll_s, 0.01))
                if q:
                    self.question, self.question_at = q, time.monotonic()
                self.play(True)
                self.lines.append({"mark": len(self.lines), "kind": "agent", "name": self.name[:200],
                                   "text": m["text"][:4000], "at": time.time()})  # fmt: skip
                if self.asked:
                    self.asked[-1]["a"] = f"{self.asked[-1]['a']} {text}".strip()[:2000]
                yield {"kind": "agent", "id": m["id"], "text": text, **({"question": q} if q else {})}
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
    meeting = Meeting(backend, name, ctx.room, int(info.get("after") or 0),
                      quiet=lambda: time.monotonic() - heard_at["voiced"] >= MEET_QUIET_S)  # fmt: skip
    mix = Mix()
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
    )
    if voice.session is not None:
        voice.session.input.audio = mix
    meeting._apply()  # the voice track exists now: muted until someone names the assistant
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
        meeting.joiner.close()
        for t in readers.values():
            t.cancel()
        asked = meeting.asked
        summary = f"Asked {len(asked)} time{'s' if len(asked) != 1 else ''}"
        for step in (voice.close(), flush(final=True), backend.meet("end", {"asked": asked, "summary": summary})):
            try:
                await step
            except Exception:  # noqa: BLE001 -- one lost write must not cost the others
                logger.exception("meet: end step failed room=%s", room_id)
        await backend.aclose()

    ctx.add_shutdown_callback(on_end)
    await ctx.room.local_participant.set_attributes(
        {f"lightbulb.agent.{ASSISTANT_ID}.status": "listening", f"lightbulb.agent.{ASSISTANT_ID}.name": name}
    )
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
        if time.monotonic() - heard_at["voiced"] > MEETING_IDLE_S:
            logger.info("meet: an hour of silence, leaving room %s", room_id)
            await backend.leave_meeting()  # the backend ends the meeting and closes the room
            ctx.shutdown("meeting idle")
            return
