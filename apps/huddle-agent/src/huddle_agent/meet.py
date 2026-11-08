"""The meeting agent (one-brain plan, founder ruling 8 Nov 2026, option A): ears and a mouth in a Google Meet for
the meeting room's Router, never a second brain (agent.py hands it every `source: "meet"` dispatch).

Ears: a per-person transcriber (one stream per Meet track, so every line has its exact speaker) and the wake gate
(`Gate` and `wakes`, over transcript.py's rules): a line that opens with the assistant's name and asks is posted to
the meeting's room as a message (`/meet/{room}/ask`), which the Router decides like any other. Side talk is never
posted. "<name>, stop" cuts the voice off.
Mouth: text-to-speech of each new agent message in the meeting's room (`/meet/{room}/events`, mouth.py), word for
word, once the room has paused. No language model and no tools: nothing said is the voice's own answer.

The worker never leaves on an empty room: Recall's call_ended closes the LiveKit room (routers/meetings.py), and an
hour of silence pulls the bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import numpy as np
from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, room_io, stt
from livekit.plugins import noise_cancellation, openai

from huddle_agent import end_after
from huddle_agent.backend import Backend, BackendError
from huddle_agent.mouth import Mouth
from huddle_agent.transcript import (
    _LEADS,
    _LIGHTBULB,
    _tokens,
    address,
    clause_openers,
    stop_request,
)

logger = logging.getLogger("meet")
VOICE = os.environ.get("MEET_VOICE", "marin")
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
GOODBYE = "We're out of time. Goodbye, everyone."
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


def voice_session() -> AgentSession:
    """Text-to-speech only: no language model, no speech-to-text of its own (the per-person transcriber hears)."""
    return AgentSession(tts=openai.TTS(model=os.environ.get("MEET_TTS_MODEL", "gpt-4o-mini-tts"), voice=VOICE))


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


async def run(ctx: JobContext, meta: dict) -> None:
    """The meeting, from the dispatch until the room closes."""
    started, wall = time.monotonic(), time.time()
    room_id = meta["roomId"]
    backend = Backend(room_id, meta["ownerId"], meta["roomToken"])
    await ctx.connect()
    try:
        info = await backend.meet("start", {"company": meta.get("company", "")})
    except BackendError as e:
        logger.error("meet: no meeting for room %s (%s)", room_id, e.code)
        return
    name = info["assistantName"]
    names: dict[str, str] = {}
    gate = Gate(name=name)
    heard_at = {"voiced": time.monotonic()}  # when anyone last spoke
    readers: dict[str, asyncio.Task] = {}
    # the name in its prompt: unprompted, an accented "Lightbulb, can you..." came out as "la lampe" (self-test)
    stt_engine = openai.STT(
        model=TRANSCRIBE, use_realtime=True, turn_detection=STT_VAD,
        prompt=f'A meeting at {meta.get("company") or "a business"}. People ask the AI assistant "{name}" things '
        f'by name: "{name}, ...".',
    )  # fmt: skip
    session = voice_session()
    speeches: list = []  # what the voice is saying or has queued: "<name>, stop" cuts them all
    lines: list[dict] = []
    asked: list[dict] = []  # {t, who, q, a}: the meeting's record

    def label(identity: str) -> str:
        return names.get(identity, "Someone") if identity else "Several people"

    def stop() -> None:
        mouth.pending.clear()
        for h in speeches:
            h.interrupt(force=True)
        speeches.clear()

    def on_heard(who: str, text: str, final: bool) -> None:
        if not text.strip():
            return
        agents = [{"id": ASSISTANT_ID, "name": name, "defaultName": False}]
        if not final:
            if stop_request(text, agents):  # a partial may stop the voice, never wake it
                stop()
            return
        kind = addressed(gate, name, who, text, time.monotonic())
        logger.info("meet: line kind=%s", kind or "-")
        if kind == "stop":
            stop()
        elif kind == "ask":
            pid = who.rsplit(":", 1)[-1]
            asked.append({"t": int(time.monotonic() - started), "who": label(who), "q": text, "a": ""})
            asyncio.create_task(ask(text, pid, label(who)))
        lines.append({"mark": len(lines), "kind": "person", "name": label(who)[:200], "text": text[:4000],
                      "at": time.time()})  # fmt: skip

    async def ask(text: str, pid: str, who: str) -> None:
        try:
            await backend.meet("ask", {"text": text, "participantId": pid, "speakerName": who})
        except BackendError as e:
            logger.warning("meet: ask not posted (%s)", e.code)

    async def read(track: rtc.Track, identity: str) -> None:
        stream = rtc.AudioStream.from_track(
            track=track, sample_rate=RATE, num_channels=1, frame_size_ms=20,
            noise_cancellation=noise_cancellation.NC(),
        )  # fmt: skip
        words = stt_engine.stream()

        async def listen() -> None:
            async for ev in words:
                if ev.alternatives and ev.type in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT):
                    on_heard(identity, ev.alternatives[0].text, ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT)

        listener = asyncio.create_task(listen())
        try:
            async for ev in stream:
                if voiced(ev.frame):
                    heard_at["voiced"] = time.monotonic()
                words.push_frame(ev.frame)
        finally:
            listener.cancel()
            await words.aclose()
            await stream.aclose()

    def on_track(track: rtc.Track, _pub, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.identity.startswith("meet:"):
            names[p.identity] = p.name or "Someone"
            gate.people = [{"name": n} for n in names.values()]
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

    def say(line: str) -> None:
        speeches.append(session.say(line))
        lines.append({"mark": len(lines), "kind": "agent", "name": name[:200], "text": line[:4000], "at": time.time()})
        if asked:
            asked[-1]["a"] = f"{asked[-1]['a']} {line}".strip()[:2000]

    async def fetch(after: int) -> dict:
        return await backend.meet("events", {"after": after})

    mouth = Mouth(fetch, int(info.get("after") or 0))

    async def speak_loop() -> None:
        while True:
            await asyncio.sleep(POLL_S)
            await mouth.poll()
            speeches[:] = [h for h in speeches if not h.done()]
            mouth.speak(say, time.monotonic() - heard_at["voiced"] >= MEET_QUIET_S)

    def on_state(ev) -> None:
        status = {"speaking": "speaking", "thinking": "working"}.get(ev.new_state, "listening")
        asyncio.create_task(ctx.room.local_participant.set_attributes(
            {f"lightbulb.agent.{ASSISTANT_ID}.status": status, f"lightbulb.agent.{ASSISTANT_ID}.name": name}
        ))  # fmt: skip

    session.on("agent_state_changed", on_state)
    sent = {"n": 0}

    async def flush(final: bool = False) -> None:
        batch = lines if final else lines[sent["n"] :]
        if batch:
            await backend.transcript(f"{room_id}:{int(wall)}", batch, final=final)
            sent["n"] = len(lines)

    async def time_up() -> None:
        logger.info("meet: max length reached, leaving room %s", room_id)
        await backend.leave_meeting()
        ctx.shutdown("meeting max length")

    limit = asyncio.create_task(end_after(MEETING_MAX_S, session, time_up, line=GOODBYE))
    speaking = asyncio.create_task(speak_loop())

    async def on_end(_reason: str = "") -> None:
        limit.cancel()
        speaking.cancel()
        for t in readers.values():
            t.cancel()
        summary = f"Asked {len(asked)} time{'s' if len(asked) != 1 else ''}"
        for step in (flush(final=True), backend.meet("end", {"asked": asked, "summary": summary})):
            try:
                await step
            except Exception:  # noqa: BLE001 -- one lost write must not cost the other
                logger.exception("meet: end write failed room=%s", room_id)
        await backend.aclose()

    ctx.add_shutdown_callback(on_end)
    await session.start(
        agent=Agent(instructions=""),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=False,
            text_input=False,
            close_on_disconnect=False,
            audio_output=room_io.AudioOutputOptions(track_name=TRACK),
        ),  # fmt: skip
        record=False,  # the transcript is kept (meeting_notes); no recording of guests
    )
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
