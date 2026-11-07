"""The meeting agent (voice-agents spec §4.4, §4.4b, §5 "The meeting agent"): ONE listening Gemini Live
session per Google Meet, in the huddle worker (agent.py hands it every `source: "meet"` dispatch).

It hears the room (every Meet person's track, mixed here: a realtime session takes one input stream) and
speaks only when addressed by the assistant's name. Two gates: Gemini's own decide-not-to-respond, and
`Gate` -- the wake rules of transcript.py over the session's input transcript. Gemini sends that transcript
seconds AFTER its reply's audio is ready (live test 7 Nov), so a reply's audio is HELD until its turn's
transcript arrives and shows an ask (then after a real pause, MEET_QUIET_S); anything else is dropped
unheard. A reply not started within STALE_S is dropped. The worker never leaves on an empty room: Recall's
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

from huddle_agent import end_after
from huddle_agent.backend import Backend, BackendError
from huddle_agent.transcript import (
    address,
    clause_openers,
    stop_request,
)

logger = logging.getLogger("meet")
MODEL = os.environ.get("MEET_MODEL", "gemini-3.8-live")
VOICE = os.environ.get("MEET_VOICE", "Kore")
ASSISTANT_ID = "assistant"  # the tile's agent id (lightbulb.agent.<id>.*)
TRACK = f"agent-{ASSISTANT_ID}"  # the meet-bot page plays only `agent-*` tracks into the Meet
MEET_QUIET_S = 3.0  # huddle.py: a reply waits for a real pause in the room
STALE_S = 20.0  # spec §4.4b.4: a reply not started this long after its ask is dropped
MEETING_IDLE_S = 60 * 60  # spec §4.4b: leave after an hour of silence
MEETING_MAX_S = float(os.environ.get("MEET_MAX_S", 90 * 60))  # cost cap: leave after 90 minutes whatever happens
TRANSCRIPT_FLUSH_S = 10
RATE, FRAME = 16000, 320  # 20 ms frames at Gemini's input rate
TRANSCRIPT_WAIT_S = 10.0  # a held reply's turn must be transcribed this soon (measured 1.4-1.9 s, 7 Nov)
ASK_WINDOW_S = 30.0  # who asked: the tracks voiced since the last reply, at most this far back
ADDRESSED = ("ask",)
VOICED_RMS = 500  # int16 RMS a frame needs to count as someone speaking (after noise cancellation)
ATTRIB_RMS = 150  # lower bar for "this track spoke" when deciding who asked: a quiet guest is not missed
# ponytail: fixed threshold; tune from a real meeting's levels.

INSTRUCTIONS = """You are {name}, the AI assistant of {company}, listening in the Google Meet "{title}". Today is
{today}. Several people talk; most of it is not for you. Stay silent unless someone addresses you by name
("{name}, ..."). When addressed, answer in one or two short spoken sentences. For numbers about calls,
bookings, the calendar or what the team knows, call the read tools first and say only what they return; if
they don't have it, say you don't know. For the team's connected apps (mail, calendar, Drive, Stripe,
Notion, Linear, GitHub, PostHog) use read_integration (say "one moment" first;
a read can take a few seconds); if a read says it needs more, use start_work. Writing anything new (a draft,
a post, an email, a document), anything open-ended, any action item or follow-up you are asked to take,
anything that sends, spends or publishes: call start_work and say you're on it -- never write, do or promise
it yourself. Only an answer you already have that is longer than a sentence (a list from a read) goes to the
meeting chat with send_to_chat; say "I've put it in the chat". If start_work says it needs a member, say you'll need the member's OK. "{name}, stop"
means stop talking. If a tool returns an error, say plainly that you couldn't do it; never say it is done."""


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


class Mixer(io.AudioInput):
    """Every Meet person's audio, summed into the one stream the session hears, paced at 20 ms. Also who is
    which tracks carried speech when (who asked: `sole`) and when anyone last spoke (the pause rule)."""

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
            if rms >= ATTRIB_RMS:  # quieter than speech counts for who-asked: a soft guest makes it ambiguous
                self._voiced.append((time.monotonic(), who, rms))
            if rms >= VOICED_RMS:
                self.voiced_at = time.monotonic()
        return np.clip(out, -32768, 32767).astype(np.int16)

    def voiced(self, since: float) -> set[str]:
        """The tracks that carried any voice (ATTRIB_RMS) since `since` (monotonic)."""
        return {who for at, who, _ in self._voiced if at >= since}

    def sole(self, since: float) -> str:
        """The one track that spoke since `since`, else "" (nobody, or several: never a guess)."""
        tracks = self.voiced(since)
        return next(iter(tracks)) if len(tracks) == 1 else ""

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
        self.turns: list[dict] = []  # each person turn Gemini transcribed: seq, kind, final, who, text
        self.consumed = 0  # turns up to this seq have had their reply (or were refused one)
        self.reply_turn: dict | None = None  # the turn the reply now playing answers
        self.reply_end = time.monotonic()  # when the last reply ended: the start of the next ask window
        self.tool_at = -1.0  # when a tool last ran: Gemini's answer after it belongs to the turn it ran for
        self.ends_at = time.monotonic() + MEETING_MAX_S  # from here tools are refused and replies dropped...
        self.goodbye_id = ""  # ...except the time limit's goodbye: the SpeechHandle id end_after made

    def heard(self, item_id: str | None, text: str, final: bool, now: float) -> dict:
        """One input-transcript event: the turn it belongs to, created or updated. Whether it asked is decided
        on the FINAL transcript only; a partial can only make it a stop."""
        t = self.turns[-1] if self.turns else None
        if t is None or (item_id and t["item"] != item_id) or (not item_id and t["final"]):
            who = self._mixer.sole(max(self.reply_end, now - ASK_WINDOW_S))
            t = {"seq": len(self.turns) + 1, "item": item_id, "kind": "", "final": False, "final_at": None,
                 "who": who, "text": "", "at": now}  # fmt: skip
            self.turns.append(t)
        t["text"] = text
        if not final:
            # a partial is a few words that keep growing ("We should ask Nova" -> "... about it later."): it may
            # stop the assistant, never wake it
            if stop_request(text, [{"id": ASSISTANT_ID, "name": self.gate.name, "defaultName": False}]):
                t["kind"] = "stop"
            return t
        if not t["final"]:
            t["final"], t["final_at"] = True, now
            self.gate.ask_at = None  # the final is judged whole, on its own words
            kind = self.gate.heard(t["who"], text, now, new_turn=True)
            if t["kind"] != "stop":
                t["kind"] = kind
        return t

    def asker(self) -> str:
        """The Meet participant id whose ask this is: the ONE track voiced since the last reply; "" when
        nobody or several spoke (the backend then treats it as a guest's: needs_member)."""
        who = self._mixer.sole(max(self.reply_end, time.monotonic() - ASK_WINDOW_S))
        return who.rsplit(":", 1)[-1] if who else ""

    def _authorize(self, now: float, speech_id: str | None = None) -> tuple[bool | None, str]:
        """(True play | False drop | None wait, why) for the reply now held. Only a FINAL transcript decides:
        Gemini's partials grow while the person talks on, and a reply held for an early part is not the answer."""
        if self.goodbye_id and speech_id == self.goodbye_id:
            return True, "goodbye"
        if now >= self.ends_at:
            return False, "time up"
        t = self.turns[-1] if self.turns else None
        if t is not None and t["seq"] > self.consumed:
            if not t["final"]:
                return (False, "stop") if t["kind"] == "stop" else (None, "turn still coming")
            if t["kind"] not in ADDRESSED:
                return False, "stop" if t["kind"] == "stop" else "not addressed"
            quiet = now - self._mixer.voiced_at
            return (True, "ask") if quiet >= MEET_QUIET_S else (None, "room not quiet")
        if (
            t is not None
            and t is self.reply_turn
            and self.tool_at >= (t["final_at"] or 0)  # Gemini's answer after a tool it called for this turn
            and self._mixer.voiced_at <= (t["final_at"] or 0)  # nobody spoke since: it cannot be for later talk
            and now - (t["final_at"] or 0) < STALE_S
        ):
            return True, "tool follow-on"
        return None, "no transcript yet"

    async def realtime_audio_output_node(self, audio, model_settings):
        """A reply's audio is held until its turn's final transcript says the assistant was addressed (see the
        module docstring); nothing is heard before that."""
        started = time.monotonic()
        while True:
            now = time.monotonic()
            ok, why = self._authorize(now, getattr(self.session.current_speech, "id", None))
            waiting_for_words = why in ("no transcript yet", "turn still coming")
            if ok is None and waiting_for_words and now - started > TRANSCRIPT_WAIT_S:
                ok, why = False, "no transcript in time"  # a turn transcribed later is not this reply's
            elif ok is None and now - started > STALE_S:
                ok, why = False, "stale"
            if ok is not None:
                break
            await asyncio.sleep(0.05)
        held = time.monotonic() - started
        speech = self.session.current_speech
        if ok and (speech is None or speech.interrupted or speech.done()):
            # Gemini cut this reply itself (the person talked on) while it was held: nothing to say, and the
            # turn stays for the reply Gemini makes to the whole of it
            logger.info("meet: held reply was interrupted (%s) after %.1fs", why, held)
            return
        if not ok:
            if why in ("stop", "not addressed"):
                self.consumed = self.turns[-1]["seq"]  # that turn had its say: no later reply claims it
            logger.info("meet: reply dropped (%s) after %.1fs", why, held)
            self.session.interrupt()
            return
        if why == "ask":
            self.reply_turn, self.consumed = self.turns[-1], self.turns[-1]["seq"]
        logger.info("meet: reply played (%s) for turn %s, held %.1fs", why, (self.reply_turn or {}).get("seq"), held)
        if why != "goodbye":
            speech.add_done_callback(lambda h, turn=self.reply_turn: self.on_reply(turn, h.chat_items))
        try:
            async for frame in Agent.default.realtime_audio_output_node(
                self, audio, model_settings
            ):
                yield frame
        finally:
            self.reply_end = time.monotonic()
            self.gate.answered(self.reply_end)

    on_reply = staticmethod(lambda turn, items: None)  # run() records each played reply with its turn

    async def _post(self, op: str, body: dict) -> dict:
        if time.monotonic() >= self.ends_at:
            logger.info("meet: tool %s refused (time up)", op)
            return {"error": "time_up"}
        self.tool_at = time.monotonic()
        try:
            out = await self._b.meet(op, body)
        except BackendError as e:
            out = {"error": e.code}
        logger.info("meet: tool %s %s", op, out.get("error") or out.get("reason") or "ok")
        return out

    @function_tool
    async def recall_memory(self, question: str) -> dict:
        """What the team knows: decisions, facts, past meetings. Ask a plain question."""
        return await self._post("read", {"kind": "memory", "question": question, "participantId": self.asker()})

    @function_tool
    async def call_stats(self, days: int = 7) -> dict:
        """How many phone calls the assistant answered in the last `days` (days=1 means today), by outcome
        (booked, message, ...)."""
        return await self._post("read", {"kind": "calls", "days": days})

    @function_tool
    async def calendar(self, date: str = "") -> dict:
        """Bookings in the assistant's calendar. date: "YYYY-MM-DD" (local), or "" for all upcoming."""
        return await self._post("read", {"kind": "calendar", "date": date, "participantId": self.asker()})

    @function_tool
    async def list_integrations(self) -> dict:
        """The team's connected apps you can read (gmail, calendar, drive, stripe, notion, linear, github, ...)."""
        return await self._post("integrations", {})

    @function_tool
    async def read_integration(self, provider: str, query: str) -> dict:
        """One quick read of a connected app: provider is a name from list_integrations; query is what to
        search for (mail search words, an event name, an issue keyword). For anything bigger, use start_work."""
        return await self._post(
            "integration-read",
            {"provider": provider, "query": query, "participantId": self.asker()},
        )

    @function_tool
    async def send_to_chat(self, text: str) -> dict:
        """Put text in the meeting chat: drafts, lists, anything longer than a sentence."""
        return await self._post("chat", {"text": text, "participantId": self.asker()})

    @function_tool
    async def start_work(self, title: str, brief: str) -> dict:
        """Hand open-ended work or an action item to the team: a short title and a self-contained brief.
        Don't wait for it; say you're on it."""
        pid = self.asker()
        who = next((i for i in self._names if pid and i.rsplit(":", 1)[-1] == pid), "")
        out = await self._post(
            "start-work",
            {"title": title, "brief": brief, "participantId": pid, "speakerName": self._names.get(who, "")},
        )
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
    asked: list[dict] = []  # {turn, a}: built into the record at the end, when the turn's text is whole

    def label(identity: str) -> str:
        return names.get(identity, "Someone") if identity else "Several people"

    def on_heard(ev) -> None:
        t = agent.heard(ev.item_id, ev.transcript, ev.is_final, time.monotonic())
        logger.info("meet: turn %d kind=%s final=%s", t["seq"], t["kind"] or "-", ev.is_final)
        if t["kind"] == "stop":
            session.interrupt()
        if ev.is_final:
            lines.append({"mark": len(lines), "kind": "person", "name": label(t["who"])[:200],
                          "text": ev.transcript[:4000], "at": time.time()})  # fmt: skip

    def on_reply(turn: dict, items: list) -> None:
        """A played reply, done: its words go on the record with the turn it answered (never another's)."""
        text = " ".join(
            t for i in items if getattr(i, "role", None) == "assistant" and (t := getattr(i, "text_content", None))
        ).strip()
        logger.info("meet: reply for turn %d done, text=%s", turn["seq"], bool(text))
        if not text:
            return
        lines.append({"mark": len(lines), "kind": "agent", "name": name[:200], "text": text[:4000],
                      "at": time.time(), "to": label(turn["who"])[:200]})  # fmt: skip
        if asked and asked[-1]["turn"] is turn:  # a tool follow-on: the same turn's answer, continued
            asked[-1]["a"] += " " + text
        else:
            asked.append({"turn": turn, "a": text})

    agent.on_reply = on_reply

    def on_state(ev) -> None:
        status = {"speaking": "speaking", "thinking": "working"}.get(
            ev.new_state, "listening"
        )
        asyncio.create_task(ctx.room.local_participant.set_attributes(
            {f"lightbulb.agent.{ASSISTANT_ID}.status": status, f"lightbulb.agent.{ASSISTANT_ID}.name": name}
        ))  # fmt: skip

    session.on("user_input_transcribed", on_heard)
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

    limit = asyncio.create_task(end_after(MEETING_MAX_S, session, time_up, on_speech=lambda h: setattr(agent, "goodbye_id", h.id)))

    async def on_end(_reason: str = "") -> None:
        limit.cancel()
        for t in readers.values():
            t.cancel()
        record = [{"t": int(a["turn"]["at"] - started), "who": label(a["turn"]["who"]), "q": a["turn"]["text"],
                   "a": a["a"]} for a in asked]  # fmt: skip
        summary = f"Asked {len(record)} time{'s' if len(record) != 1 else ''}"
        for step in (
            flush(final=True),
            backend.meet("end", {"asked": record, "summary": summary}),
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
