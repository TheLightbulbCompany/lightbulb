"""The meeting agent (voice-agents spec §4.4, §4.4b, §5 "The meeting agent"): ONE listening realtime session per
Google Meet, in the huddle worker (agent.py hands it every `source: "meet"` dispatch). OpenAI's realtime model by
default; MEET_ENGINE=gemini is Gemini Live, the flag-only fallback. OpenAI answers only when the gate asks it to
(create_response
off): Gemini decides itself and the gate drops what it should not say; OpenAI has no decide-not-to-respond and
would reply to every line of side talk, each reply billed.

It hears the room (every Meet person's track, mixed here: a realtime session takes one input stream) and
speaks only when addressed by the assistant's name. Two gates: Gemini's own decide-not-to-respond, and
`Gate` -- the wake rules of transcript.py over each person's own transcript (the huddle's per-person
transcriber, one stream per Meet track, so every line has its exact speaker). Gemini's input transcript is
not used: after it declines to answer side talk it sends none for the talk that follows, for as long as half a
minute (self-test 7 Nov: an ask and a guest's question in that gap were never seen). A reply's audio is HELD
until the latest line's final transcript (about half a second after the person stops) shows an ask and the
room has paused (MEET_QUIET_S); anything else is dropped unheard. When a line asks and Gemini starts no reply
(its own hearing missed the name), it is asked to answer that line (NUDGE_S). A reply not started within
STALE_S is dropped. The worker never leaves on an empty room: Recall's
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
from livekit.agents import Agent, AgentSession, JobContext, StopResponse, function_tool, room_io, stt
from livekit.agents.voice import io
from livekit.plugins import google, noise_cancellation, openai
from openai.types.beta.realtime.session import TurnDetection

from huddle_agent import end_after, usage_log
from huddle_agent.backend import Backend, BackendError
from huddle_agent.transcript import (
    _LEADS,
    _LIGHTBULB,
    _tokens,
    address,
    clause_openers,
    stop_request,
)

logger = logging.getLogger("meet")
OPENAI = os.environ.get("MEET_ENGINE", "openai") == "openai"  # one voice vendor (founder, 7 Nov 2026)
MODEL = os.environ.get("MEET_MODEL", "gpt-realtime-2.1-mini" if OPENAI else "gemini-3.8-live")
VOICE = os.environ.get("MEET_VOICE", "marin" if OPENAI else "Kore")
# the gate's per-person transcriber: gpt-4o-mini-transcribe shuts down 26 Feb 2027; gpt-transcribe is its successor
# for committed turns, $0.0045/min (developers.openai.com/api/docs/deprecations, .../models/gpt-transcribe)
TRANSCRIBE = os.environ.get("MEET_TRANSCRIBE_MODEL", "gpt-transcribe")
ASSISTANT_ID = "assistant"  # the tile's agent id (lightbulb.agent.<id>.*)
TRACK = f"agent-{ASSISTANT_ID}"  # the meet-bot page plays only `agent-*` tracks into the Meet
MEET_QUIET_S = 3.0  # huddle.py: a reply waits for a real pause in the room
STALE_S = 20.0  # spec §4.4b.4: a reply not started this long after its ask is dropped
MEETING_IDLE_S = 60 * 60  # spec §4.4b: leave after an hour of silence
MEETING_MAX_S = float(os.environ.get("MEET_MAX_S", 90 * 60))  # cost cap: leave after 90 minutes whatever happens
TRANSCRIPT_FLUSH_S = 10
RATE, FRAME = 16000, 320  # 20 ms frames at Gemini's input rate
TRANSCRIPT_WAIT_S = 10.0  # a held reply's turn must be transcribed this soon (measured 1.4-1.9 s, 7 Nov)
NUDGE_S = 3.0  # an ask Gemini has not started answering this long after its final: Gemini is told to answer it
ADDRESSED = ("ask",)
VOICED_RMS = 500  # int16 RMS a frame needs to count as someone speaking (after noise cancellation)
STT_VAD = {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 600, "silence_duration_ms": 400}
# agent.MEET_VAD; not imported: agent.py imports this module
# ponytail: fixed threshold; tune from a real meeting's levels.

INSTRUCTIONS = """You are {name}, the AI assistant of {company}, listening in the Google Meet "{title}". Today is
{today}. Several people talk; most of it is not for you. Stay silent unless someone addresses you by name
("{name}, ..."). When addressed, answer in one or two short spoken sentences. For numbers about calls,
bookings, the calendar or what the team knows, call the read tools first and say only what they return; if
they don't have it, say you don't know. What was decided or said before: answer only from a recall_memory fact
that says it; if none does, say you don't know -- never guess a date, a number or a decision. For the business's own services, prices, hours and
Q&A use read_integration("knowledge", ...); for anything public on the web, read_integration("web", ...); for the
team's connected apps (mail, calendar, Drive, Stripe, Notion, Linear, GitHub, PostHog and every other one
list_integrations names) use read_integration (say "one moment" first; a read can take a few seconds). If a read
says it needs more, or returns start_work, call start_work with the question and say "I'll get that". Writing anything new (a draft,
a post, an email, a document), anything open-ended, any action item or follow-up you are asked to take,
anything that sends, spends or publishes: call start_work and say you're on it -- never write, do or promise
it yourself. Only an answer you already have that is longer than a sentence (a list from a read) goes to the
meeting chat with send_to_chat; say "I've put it in the chat". If start_work says it needs a member, say you'll need the member's OK. "{name}, stop"
means stop talking. If a tool returns an error, say plainly that you couldn't do it; never say it is done."""


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
    for sentence in re.split(r"(?<=[.?!])\s+", text.strip()):
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


class Mixer(io.AudioInput):
    """Every Meet person's audio, summed into the one stream the session hears, paced at 20 ms. Also when
    anyone last spoke (the pause rule)."""

    def __init__(self) -> None:
        super().__init__(label="meet-mix")
        self._q: dict[str, deque[np.ndarray]] = {}
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
        for q in self._q.values():
            if not q:
                continue
            a = q.popleft()[:FRAME]
            out[: len(a)] += a
            rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if len(a) else 0.0
            if rms >= VOICED_RMS:
                self.voiced_at = time.monotonic()
        return np.clip(out, -32768, 32767).astype(np.int16)

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
        self.turns: list[dict] = []  # each person's line, in the order they began: seq, kind, final, who, text
        self._by_item: dict[str, dict] = {}
        self.consumed = 0  # turns up to this seq have had their reply (or were refused one)
        self.reply_turn: dict | None = None  # the turn the reply now playing answers
        self.replying = False  # a reply for reply_turn is under way (its tools are that turn's)
        self.reply_end = time.monotonic()  # when the last reply ended: the start of the next ask window
        self.asked = None  # the SpeechHandle of the last reply asked for: one at a time
        self.tool_at = -1.0  # when a tool last ran: Gemini's answer after it belongs to the turn it ran for
        self.ends_at = time.monotonic() + MEETING_MAX_S  # from here tools are refused and replies dropped...
        self.goodbye_id = ""  # ...except the time limit's goodbye: the SpeechHandle id end_after made

    def heard(self, item_id: str, text: str, final: bool, now: float, who: str = "") -> dict:
        """One transcript event of `who`'s line `item_id`: the turn, created or updated. Whether it asked is
        decided on the FINAL transcript only; a partial (a prefix as it streams) can only make it a stop."""
        t = self._by_item.get(item_id)
        if t is None:
            t = {"seq": len(self.turns) + 1, "item": item_id, "kind": "", "final": False, "final_at": None,
                 "who": who, "text": "", "at": now}  # fmt: skip
            self.turns.append(t)
            self._by_item[item_id] = t
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
        """The Meet participant id of the asking line a tool call answers -- never whoever is talking now. The
        line is the reply's turn while a reply is under way, else the open asks (final asks since the last
        answered turn, younger than STALE_S). Every final line from that line on must be one person's: another
        person's line after it, or two askers, and the call cannot be tied to one line, so it is "" (the backend
        treats it as a guest's, needs_member). A partial counts too: Gemini hears the audio and may call the tool
        before the line's final, so anyone else talking -- since the ask, or still mid-line from before it --
        makes it "" (fails closed)."""
        now = time.monotonic()
        if self.replying and self.reply_turn is not None:
            start = self.reply_turn["seq"]
        else:
            open_asks = [t for t in self.turns if t["seq"] > self.consumed and t["final"] and t["kind"] in ADDRESSED
                         and now - (t["final_at"] or 0) < STALE_S]  # fmt: skip
            if not open_asks:
                return ""
            start = open_asks[0]["seq"]
        # every line from the asking one on, and any line still in progress that began before it
        who = {t["who"] for t in self.turns
               if t["seq"] >= start or (not t["final"] and now - t["at"] < STALE_S)}  # fmt: skip
        return next(iter(who)).rsplit(":", 1)[-1] if len(who) == 1 and "" not in who else ""

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

    def ask(self, session, line: str) -> None:
        """The reply to an ask, uninterruptible by room sound: LiveKit cut a reply held for quiet at the next voice
        or end of turn, and nothing asked again (#5025 review). Only the gate's stop cuts it (force)."""
        self.asked = session.generate_reply(user_input=line, allow_interruptions=False)

    def asking(self) -> bool:
        """A reply asked for is still under way. It ends when it played, was dropped or was cut: a dropped reply
        must not hold up the next ask (#5025 review)."""
        return self.asked is not None and not self.asked.done()

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """OpenAI with create_response off hands turn-taking to LiveKit, which would answer every line of side talk
        (8 Nov self-test: replies made for lines nobody addressed, dropped by the gate, each billed). The gate asks
        for each reply instead (nudge). Gemini answers by itself."""
        if OPENAI:
            raise StopResponse()

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
            # unheard, to its end: session.interrupt() cannot cancel a Gemini reply (with server VAD the
            # plugin's interrupt sends nothing), so the speech then hung for LiveKit's 5 s timeout and the
            # next reply queued behind it
            async for _ in audio:
                pass
            return
        if why == "ask":
            self.reply_turn, self.consumed = self.turns[-1], self.turns[-1]["seq"]
        if why != "goodbye":
            self.replying = True  # until the speech is done: a tool call inside it outlives its audio
            speech.add_done_callback(lambda _h: setattr(self, "replying", False))
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
        out = await self._post("read", {"kind": "memory", "question": question, "participantId": self.asker()})
        if "facts" in out:  # the rule rides with the facts: from the prompt alone it still guessed (self-test)
            out["rule"] = "Say only what one of these facts states. If none answers the question, say you don't know."
        return out

    @function_tool
    async def call_stats(self, days: int = 7, from_date: str = "", to_date: str = "") -> dict:
        """How many phone calls the assistant answered in the last `days` (days=1 means today), by outcome
        (booked, message, ...). For a range ("this month versus last"), from_date and to_date as "YYYY-MM-DD"
        (inclusive), once per range."""
        return await self._post("read", {"kind": "calls", "days": days, "from": from_date, "to": to_date})

    @function_tool
    async def calendar(self, date: str = "") -> dict:
        """Bookings in the assistant's calendar. date: "YYYY-MM-DD" (local), or "" for all upcoming."""
        return await self._post("read", {"kind": "calendar", "date": date, "participantId": self.asker()})

    @function_tool
    async def list_integrations(self) -> dict:
        """What you can read: knowledge, web, and the team's connected apps (gmail, calendar, drive, stripe, ...)."""
        return await self._post("integrations", {})

    @function_tool
    async def read_integration(self, provider: str, query: str) -> dict:
        """One quick read: provider is a name from list_integrations ("knowledge" is the business's own facts,
        "web" the public web); query is what to search for (mail search words, an event name, an issue keyword).
        Say "one moment" first: a connected app's read can take up to six seconds. For anything bigger, or when it
        returns start_work, call start_work with the question."""
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
    # the huddle's per-person transcriber (agent.py): the gate's ears, with the exact speaker of each line
    # the name in its prompt: unprompted, an accented "Lightbulb, can you..." came out as "la lampe" (self-test)
    stt_engine = openai.STT(
        model=TRANSCRIBE, use_realtime=True, turn_detection=STT_VAD,
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
            n = 0
            async for ev in words:
                if ev.alternatives and ev.type in (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT):
                    final = ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT
                    on_heard(identity, f"{identity}#{n}", ev.alternatives[0].text, final)
                    n += final

        listener = asyncio.create_task(listen())
        try:
            async for ev in stream:
                mixer.push(identity, ev.frame)
                words.push_frame(ev.frame)
        finally:
            mixer.remove(identity)
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

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=MODEL,
            voice=VOICE,
            # replies only when the gate asks (nudge); the gate hears through the per-person transcriber
            turn_detection=TurnDetection(type="server_vad", threshold=0.5, prefix_padding_ms=600,
                                         silence_duration_ms=400, create_response=False, interrupt_response=False),
            # interrupt_response off too: a voice in the room cancelled the reply asked for, and nothing re-asks
            # (#5025 review); the gate's stop still cuts it (session.interrupt(force=True))
            input_audio_transcription=None,
        ) if OPENAI else google.realtime.RealtimeModel(
            model=MODEL,
            voice=VOICE,
            api_version="v1beta",
            # a meeting outlives Gemini's 10-minute connection and 15-minute audio session
            session_resumption=types.SessionResumptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=16000,
                sliding_window=types.SlidingWindow(target_tokens=8000),
            ),
        )  # fmt: skip
    )
    usage_log(session, f"meet {room_id}")
    lines: list[dict] = []
    asked: list[dict] = []  # {turn, a}: built into the record at the end, when the turn's text is whole

    def label(identity: str) -> str:
        return names.get(identity, "Someone") if identity else "Several people"

    def on_heard(who: str, item: str, text: str, final: bool) -> None:
        if not text.strip():
            return
        t = agent.heard(item, text, final, time.monotonic(), who=who)
        logger.info("meet: turn %d kind=%s final=%s", t["seq"], t["kind"] or "-", final)
        if t["kind"] == "stop":
            session.interrupt(force=True)  # the reply asked for is not interruptible by room noise; "stop" is
        # the nudge, on the strict wake only (no billed reply to "Thanks, Nova."); on OpenAI at once: it answers
        # only when asked to
        if final and t["kind"] in ADDRESSED and wakes(name, text, gate.people):
            asyncio.create_task(nudge(t, text))
        if final:
            lines.append({"mark": len(lines), "kind": "person", "name": label(t["who"])[:200],
                          "text": text[:4000], "at": time.time()})  # fmt: skip

    async def nudge(t: dict, text: str) -> None:
        """The line opens with the name and asks (`wakes`); Gemini's own hearing may have missed the name and
        stayed silent (self-test 7 Nov: a guest's two asks, unanswered). If nothing is under way for this
        line by NUDGE_S, Gemini is given the line, as said, to answer."""
        await asyncio.sleep(0 if OPENAI else NUDGE_S)
        busy = session.current_speech is not None or agent.tool_at >= (t["final_at"] or 0)
        # a reply asked for and not yet over: a second ask while it (or its tool) runs gave "the tool is still
        # running" answers, said twice (self-test 8 Nov)
        busy = busy or agent.asking() or agent.replying
        if busy or agent.consumed >= t["seq"] or agent.turns[-1] is not t or time.monotonic() >= agent.ends_at:
            return
        logger.info("meet: turn %d nudged", t["seq"])
        try:
            agent.ask(session, f'{label(t["who"])}: {text}')
        except RuntimeError:  # the session is closed (Gemini refused it): nothing to nudge
            logger.warning("meet: nudge refused, the session is not running")

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
