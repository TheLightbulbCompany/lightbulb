"""The meeting agent (voice-agents spec §4.4, §4.4b, §5 "The meeting agent"): ONE listening Gemini Live
session per Google Meet, in the huddle worker (agent.py hands it every `source: "meet"` dispatch).

It hears the room (every Meet person's track, mixed here: a realtime session takes one input stream) and
speaks only when addressed by the assistant's name. Two gates: Gemini's own decide-not-to-respond, and
`Gate` -- the wake rules of transcript.py over the session's input transcript -- which plays a reply only
for an ask, after a real pause (MEET_QUIET_S), or for a question nobody took in UNCLAIMED_QUESTION_S. A
reply not started within STALE_S of its ask is dropped. The worker never leaves on an empty room: Recall's
call_ended closes the LiveKit room (routers/meetings.py), and an hour of silence pulls the bot.

Reads, chat, work and the meeting's record go to the backend's /internal/voice/meet routes
(apps/backend/routers/voice_meet.py), which also decide who may act.
"""

from __future__ import annotations

import asyncio
import re
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from google.genai import types
from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, function_tool, room_io
from livekit.agents.voice import io
from livekit.plugins import google, noise_cancellation

from huddle_agent.backend import Backend, BackendError
from huddle_agent.transcript import (
    address,
    clause_openers,
    is_question,
    opens_with_person,
    stop_request,
)

logger = logging.getLogger("meet")
MODEL = os.environ.get("MEET_MODEL", "gemini-3.8-live")
VOICE = os.environ.get("MEET_VOICE", "Kore")
ASSISTANT_ID = "assistant"  # the tile's agent id (lightbulb.agent.<id>.*)
TRACK = f"agent-{ASSISTANT_ID}"  # the meet-bot page plays only `agent-*` tracks into the Meet
MEET_QUIET_S = 3.0  # huddle.py: a reply waits for a real pause in the room
UNCLAIMED_QUESTION_S = (
    3.5  # huddle.py: a question naming nobody, if no person takes it first
)
STALE_S = 20.0  # spec §4.4b.4: a reply not started this long after its ask is dropped
FOLLOWUP_S = (
    60.0  # the asker's next line, with nobody else in between, continues the exchange
)
MEETING_IDLE_S = 60 * 60  # spec §4.4b: leave after an hour of silence
TRANSCRIPT_FLUSH_S = 10
RATE, FRAME = 16000, 320  # 20 ms frames at Gemini's input rate
SPEAKER_S = 8.0  # a line's speaker: the loudest track over this window before its transcript
VOICED_RMS = 500  # int16 RMS a frame needs to count as someone speaking (after noise cancellation)
# ponytail: fixed threshold; tune from a real meeting's levels.

INSTRUCTIONS = """You are {name}, the AI assistant of {company}, listening in the Google Meet "{title}". Today is
{today}. Several people talk; most of it is not for you. Stay silent unless someone addresses you by name
("{name}, ..."). When addressed, answer in one or two short spoken sentences. For numbers about calls,
bookings, the calendar or what the team knows, call the read tools first and say only what they return; if
they don't have it, say you don't know. For the team's connected apps (mail, calendar, Drive, Stripe,
Notion, Linear, GitHub, PostHog) use read_integration (say "one moment" first;
a read can take a few seconds); if a read says it needs more, use start_work. Anything longer than a sentence (a draft, a list, notes) goes to the
meeting chat with send_to_chat; say "I've put it in the chat". Anything open-ended, any action item you are
asked to take, anything that sends, spends or publishes: call start_work and say you're on it -- never do or
promise it yourself. If start_work says it needs a member, say you'll need the member's OK. "{name}, stop"
means stop talking. If a tool returns an error, say plainly that you couldn't do it; never say it is done."""


@dataclass
class Gate:
    """Who the assistant may answer, from the session's input transcript. Pure: the clock is passed in."""

    name: str
    people: list[dict] = field(default_factory=list)
    ask_at: float | None = None  # an open ask: when it was heard
    asker: str = ""  # who made it (Meet participant identity)
    question_at: float | None = (
        None  # a question naming nobody, not yet taken by a person
    )
    last_answer_to: str = ""  # who the assistant last answered
    last_answer_at: float = -1e9
    others_since: bool = False  # someone else spoke after that answer

    def heard(self, who: str, text: str, now: float, *, new_turn: bool) -> str:
        """One (cumulative) transcript of a person's turn. Returns "stop", "ask", "followup", "question", "".
        `new_turn`: the first text of a turn (a later interim of the same turn only upgrades it)."""
        agents = [{"id": ASSISTANT_ID, "name": self.name, "defaultName": False}]
        if stop_request(text, agents):  # "Nova, stop", or a bare "stop" over it
            self.ask_at = self.question_at = None
            return "stop"
        if new_turn and who != self.last_answer_to:
            self.others_since = True
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
            self.ask_at, self.asker, self.question_at = now, who, None
            return "ask"
        if (
            new_turn
            and who == self.last_answer_to
            and not self.others_since
            and now - self.last_answer_at <= FOLLOWUP_S
            and is_question(text)
            and not opens_with_person(
                text, self.people
            )  # "Leo, did you push it?" is to Leo
        ):
            self.ask_at, self.asker = now, who
            return "followup"
        if new_turn:
            # a new line from someone else takes the floor from an unclaimed question (a person answered it)
            self.question_at = now if is_question(text) else None
            return "question" if self.question_at is not None else ""
        if self.question_at is None and is_question(text):
            self.question_at = now
            return "question"
        return ""

    def verdict(self, now: float, quiet_s: float) -> bool | None:
        """May a reply start now? True play, False drop, None wait (the room is not quiet yet)."""
        if self.ask_at is not None:
            if now - self.ask_at > STALE_S:
                self.ask_at = None
                return False
            return True if quiet_s >= MEET_QUIET_S else None
        if self.question_at is not None:
            if now - self.question_at > STALE_S:
                self.question_at = None
                return False
            return True if quiet_s >= UNCLAIMED_QUESTION_S else None
        return False

    def answered(self, now: float) -> None:
        self.last_answer_to, self.last_answer_at, self.others_since = (
            self.asker,
            now,
            False,
        )
        self.ask_at = self.question_at = None


class Mixer(io.AudioInput):
    """Every Meet person's audio, summed into the one stream the session hears, paced at 20 ms. Also who is
    loudest since `reset` (who said a line) and when anyone last spoke (the pause rule)."""

    def __init__(self) -> None:
        super().__init__(label="meet-mix")
        self._q: dict[str, deque[np.ndarray]] = {}
        self._voiced: deque[tuple[float, str, float]] = deque(maxlen=2000)  # (when, who, rms)
        self.voiced_at = time.monotonic()
        self._next = 0.0

    def push(self, identity: str, frame: rtc.AudioFrame) -> None:
        self._q.setdefault(identity, deque(maxlen=25)).append(
            np.frombuffer(frame.data, dtype=np.int16)
        )

    def remove(self, identity: str) -> None:
        self._q.pop(identity, None)

    def mix(self) -> np.ndarray:
        out = np.zeros(FRAME, dtype=np.int32)
        for who, q in self._q.items():
            if not q:
                continue
            a = q.popleft()[:FRAME]
            out[: len(a)] += a
            rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if len(a) else 0.0
            if rms >= VOICED_RMS:
                self.voiced_at = time.monotonic()
                self._voiced.append((self.voiced_at, who, rms))
        return np.clip(out, -32768, 32767).astype(np.int16)

    def speaker(self, within_s: float = SPEAKER_S) -> str:
        """Who was loudest in the last `within_s`; with nobody voiced, the one person on a track."""
        since, energy = time.monotonic() - within_s, {}
        for at, who, rms in self._voiced:
            if at >= since:
                energy[who] = energy.get(who, 0.0) + rms
        if energy:
            return max(energy, key=energy.get)
        return next(iter(self._q)) if len(self._q) == 1 else ""

    async def __anext__(self) -> rtc.AudioFrame:
        now = time.monotonic()
        self._next = max(
            self._next + 0.02, now - 0.1
        )  # a stall is not made up for in a burst
        if self._next > now:
            await asyncio.sleep(self._next - now)
        return rtc.AudioFrame(self.mix().tobytes(), RATE, 1, FRAME)


class MeetAgent(Agent):
    def __init__(
        self, b: Backend, gate: Gate, mixer: Mixer, info: dict, names: dict[str, str]
    ) -> None:
        super().__init__(instructions=INSTRUCTIONS.format(**info))
        self._b, self.gate, self._mixer, self._names = b, gate, mixer, names
        self.played = False  # the current reply passed the gate (its text goes on the record)
        self.provisional = False  # playing with no ask known yet
        self.vetoed = False  # its turn's transcript arrived and asked nothing: stop

    async def realtime_audio_output_node(self, audio, model_settings):
        """Gemini's input transcript lands only when its turn completes, seconds after the reply's first audio
        (live test 7 Nov), so the wake rules cannot clear a reply before it plays. A reply plays once the room
        is quiet; a known ask or question keeps its own rules (`Gate.verdict`); otherwise it plays on
        Gemini's own judgement and is cut off when its turn's transcript shows nobody asked (`veto`)."""
        started = time.monotonic()
        while True:
            now = time.monotonic()
            quiet = now - self._mixer.voiced_at
            if now - started > STALE_S:
                v = False
            elif self.gate.ask_at is not None or self.gate.question_at is not None:
                v = self.gate.verdict(now, quiet)
            else:
                v = True if quiet >= MEET_QUIET_S else None
            if v is not None:
                break
            await asyncio.sleep(0.1)
        if not v:
            logger.info("meet: reply dropped (stale) ask_at=%s", self.gate.ask_at)
            self.session.interrupt()
            return
        self.played, self.vetoed = True, False
        self.provisional = self.gate.ask_at is None and self.gate.question_at is None
        try:
            async for frame in Agent.default.realtime_audio_output_node(
                self, audio, model_settings
            ):
                if self.vetoed:
                    logger.info("meet: reply cut off: its turn did not address the assistant")
                    self.played = False
                    self.session.interrupt()
                    break
                yield frame
        finally:  # said in full or cut off: the ask is answered either way
            self.provisional = False
            self.gate.answered(time.monotonic())

    async def _post(self, op: str, body: dict) -> dict:
        if self.gate.ask_at is not None:  # a tool working on the ask: its reply is not stale yet
            self.gate.ask_at = time.monotonic()
        try:
            out = await self._b.meet(op, body)
        except BackendError as e:
            out = {"error": e.code}
        logger.info("meet: tool %s -> %s", op, str(out)[:300])
        if self.gate.ask_at is not None:
            self.gate.ask_at = time.monotonic()
        return out

    @function_tool
    async def recall_memory(self, question: str) -> dict:
        """What the team knows: decisions, facts, past meetings. Ask a plain question."""
        return await self._post("read", {"kind": "memory", "question": question})

    @function_tool
    async def call_stats(self, days: int = 7) -> dict:
        """How many phone calls the assistant answered in the last `days`, by outcome (booked, message, ...)."""
        return await self._post("read", {"kind": "calls", "days": days})

    @function_tool
    async def calendar(self, date: str = "") -> dict:
        """Bookings in the assistant's calendar. date: "YYYY-MM-DD" (local), or "" for all upcoming."""
        return await self._post("read", {"kind": "calendar", "date": date})

    @function_tool
    async def list_integrations(self) -> dict:
        """The team's connected apps you can read (gmail, calendar, drive, stripe, notion, linear, github, ...)."""
        return await self._post("integrations", {})

    @function_tool
    async def read_integration(self, provider: str, query: str) -> dict:
        """One quick read of a connected app: provider is a name from list_integrations; query is what to
        search for (mail search words, an event name, an issue keyword). For anything bigger, use start_work."""
        who = self.gate.asker.rsplit(":", 1)[-1]
        return await self._post(
            "integration-read",
            {"provider": provider, "query": query, "participantId": who},
        )

    @function_tool
    async def send_to_chat(self, text: str) -> dict:
        """Put text in the meeting chat: drafts, lists, anything longer than a sentence."""
        return await self._post("chat", {"text": text})

    @function_tool
    async def start_work(self, title: str, brief: str) -> dict:
        """Hand open-ended work or an action item to the team: a short title and a self-contained brief.
        Don't wait for it; say you're on it."""
        who = self.gate.asker
        out = await self._post(
            "start-work",
            {"title": title, "brief": brief, "participantId": who.rsplit(":", 1)[-1],
             "speakerName": self._names.get(who, "")},
        )  # fmt: skip
        if out.get("already"):
            out["say"] = "already on it"
        return out


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
    mixer, names = Mixer(), {}
    gate = Gate(name=name)
    agent = MeetAgent(backend, gate, mixer, {
        "name": name, "company": meta.get("company") or "the team", "title": info["title"],
        "today": time.strftime("%A %Y-%m-%d"),
    }, names)  # fmt: skip
    readers: dict[str, asyncio.Task] = {}

    async def read(track: rtc.Track, identity: str) -> None:
        stream = rtc.AudioStream.from_track(
            track=track, sample_rate=RATE, num_channels=1, frame_size_ms=20,
            noise_cancellation=noise_cancellation.NC(),
        )  # fmt: skip
        try:
            async for ev in stream:
                mixer.push(identity, ev.frame)
        finally:
            mixer.remove(identity)
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

    key = os.environ["GOOGLE_API_KEY"]
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL,
            voice=VOICE,
            api_key=key,
            api_version="v1beta",
            # a meeting outlives Gemini's 10-minute connection and 15-minute audio session
            session_resumption=types.SessionResumptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=16000,
                sliding_window=types.SlidingWindow(target_tokens=8000),
            ),
        )  # fmt: skip
    )
    lines: list[dict] = []
    asked: list[dict] = []
    pending: dict = {}
    said: dict = {}  # a played reply whose ask has not been heard yet
    turn = {"id": None}

    def on_heard(ev) -> None:
        new_turn = ev.item_id != turn["id"] if ev.item_id else ev.is_final
        if new_turn:
            turn["id"] = ev.item_id
        who = mixer.speaker() or gate.asker
        now = time.monotonic()
        kind = gate.heard(who, ev.transcript, now, new_turn=new_turn)
        if kind in ("ask", "followup", "question"):
            agent.vetoed = False
        elif kind == "" and ev.is_final and agent.provisional and gate.ask_at is None:
            agent.vetoed = True
        if kind or ev.is_final:
            logger.info("meet: heard kind=%s who=%s final=%s %r", kind, who, ev.is_final, ev.transcript[:200])
        if kind == "stop":
            session.interrupt()
        elif kind in ("ask", "followup", "question") and said and now - said["at"] <= 30:
            # the reply played before its turn's transcript came (Gemini's order): pair them now
            asked.append({"t": int(now - started), "who": names.get(who, "Someone"), "q": ev.transcript,
                          "a": said.pop("text")})  # fmt: skip
            said.clear()
        elif kind in ("ask", "followup", "question"):
            pending.clear()
            pending.update(
                t=int(now - started), who=names.get(who, "Someone"), q=ev.transcript
            )
        elif pending and who == gate.asker:
            pending["q"] = ev.transcript  # the ask, said in full
        if ev.is_final:
            lines.append({"mark": len(lines), "kind": "person", "name": names.get(who, "Someone")[:200],
                          "text": ev.transcript[:4000], "at": time.time()})  # fmt: skip

    def on_item(ev) -> None:
        text = getattr(ev.item, "text_content", None)
        if getattr(ev.item, "role", None) == "assistant" and text:
            logger.info("meet: said played=%s %r", agent.played, text[:200])
        if getattr(ev.item, "role", None) != "assistant" or not text or not agent.played:
            return
        agent.played = False
        lines.append({"mark": len(lines), "kind": "agent", "name": name[:200], "text": text[:4000],
                      "at": time.time(), "to": pending.get("who", "")[:200]})  # fmt: skip
        if pending:
            asked.append({**pending, "a": text})
            pending.clear()
        else:
            said.update(text=text, at=time.monotonic())

    def on_state(ev) -> None:
        status = {"speaking": "speaking", "thinking": "working"}.get(
            ev.new_state, "listening"
        )
        asyncio.create_task(ctx.room.local_participant.set_attributes(
            {f"lightbulb.agent.{ASSISTANT_ID}.status": status, f"lightbulb.agent.{ASSISTANT_ID}.name": name}
        ))  # fmt: skip

    session.on("user_input_transcribed", on_heard)
    session.on("conversation_item_added", on_item)
    session.on("agent_state_changed", on_state)
    sent = {"n": 0}

    async def flush(final: bool = False) -> None:
        batch = lines if final else lines[sent["n"] :]
        if batch:
            await backend.transcript(f"{room_id}:{int(wall)}", batch, final=final)
            sent["n"] = len(lines)

    async def on_end(_reason: str = "") -> None:
        for t in readers.values():
            t.cancel()
        summary = f"Asked {len(asked)} time{'s' if len(asked) != 1 else ''}"
        for step in (
            flush(final=True),
            backend.meet("end", {"asked": asked, "summary": summary}),
        ):
            try:
                await step
            except Exception:  # noqa: BLE001 -- one lost write must not cost the other
                logger.exception("meet: end write failed room=%s", room_id)
        await backend.aclose()

    ctx.add_shutdown_callback(on_end)
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=False,
            text_input=False,
            close_on_disconnect=False,
            audio_output=room_io.AudioOutputOptions(track_name=TRACK),
        ),  # fmt: skip
        record=False,  # the transcript is kept (meeting_notes); no recording of guests
    )
    session.input.audio = mixer
    await ctx.room.local_participant.set_attributes(
        {
            f"lightbulb.agent.{ASSISTANT_ID}.status": "listening",
            f"lightbulb.agent.{ASSISTANT_ID}.name": name,
        }
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
        if time.monotonic() - mixer.voiced_at > MEETING_IDLE_S:
            logger.info("meet: an hour of silence, leaving room %s", room_id)
            await (
                backend.leave_meeting()
            )  # the backend ends the meeting and closes the room
            ctx.shutdown("meeting idle")
            return
