# The delegation handling (_Voice.on_enter / _on_delegation_created / _on_done) is adapted from
# livekit/agents examples/voice_agents/gpt-live/client_delegation.py,
# Copyright LiveKit, Inc., licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changes: the answer comes from our backend
# (Huddle.on_delegation) instead of a local LLM desk.
"""LiveKit glue for the huddle worker. All behaviour lives in huddle.Huddle."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    APIConnectionError,
    APIError,
    JobContext,
    JobRequest,
    cli,
    llm,
    room_io,
    stt,
    utils,
)
from livekit.agents.metrics import LLMModelUsage
from livekit.plugins import noise_cancellation, openai, silero
from livekit.plugins.openai.realtime import (
    GPTLiveDelegation,
    GPTLiveModel,
    GPTLiveSession,
)
from livekit.plugins.openai.realtime.gpt_live_model import _render_item
from openai import AsyncOpenAI
from openai.types.realtime import ResponseCreateEvent

from huddle_agent.backend import Backend
from huddle_agent.huddle import Huddle, emit_metric, in_meet
from huddle_agent.transcript import trim_items

logger = logging.getLogger("huddle")
AGENT_IDENTITY = "agent-ceo"  # C4: every agent voice publishes as this one participant
WORKER_DISPLAY_NAME = "Lightbulb"  # clients render tiles from attributes, not this name
# LiveKit's default (10 s) would kill a meeting's final transcript retry (huddle.FINAL_TRANSCRIPT_BUDGET_S).
SHUTDOWN_S = 60
server = AgentServer(shutdown_process_timeout=SHUTDOWN_S)


def _seeded(seed: list[tuple[str, str]]) -> llm.ChatContext:
    """The huddle so far, as this agent saw it (Transcript.seed)."""
    ctx = llm.ChatContext.empty()
    for role, text in seed:
        ctx.add_message(role=role, content=text)
    return ctx


class _Voice(Agent):
    """One agent's GPT-Live voice."""

    def __init__(
        self, agent_id: str, persona: str, huddle: Huddle, seed: list[tuple[str, str]], muted: bool = False
    ) -> None:
        super().__init__(
            instructions=persona, chat_ctx=_seeded(seed)
        )  # no tools: client delegation raises on any
        self._agent_id, self._huddle, self._muted = agent_id, huddle, muted
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_enter(self) -> None:
        session = self.duplex_session
        assert isinstance(session, GPTLiveSession)
        session.on("delegation_created", self._on_delegation_created)
        if self._muted:
            # A meeting voice must never hear the Meet. Muted here, as the session starts: the mute the huddle
            # sends comes after session.start returns, and people may be talking while the session opens.
            session.mute_input()

    async def on_exit(self) -> None:
        await utils.aio.cancel_and_wait(*self._tasks)

    def _on_delegation_created(self, d: GPTLiveDelegation) -> None:
        # emitted from the plugin's read loop, so the work has to run in a task of its own
        coro = self._huddle.on_delegation(self._agent_id, d.id, d.pending_transcript)
        task = asyncio.create_task(coro, name=f"turn:{d.id}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "delegation failed", extra={"error_type": type(error).__name__}
            )


class _TrimmedSession(GPTLiveSession):
    """GPT-Live reseeds every (re)connect from its whole `_history` and only caps the item count, so
    a long huddle would outgrow the seed's token limit. Seed the newest items within ours instead.
"""

    # ponytail: private API, re-check on livekit-agents upgrades
    def _session_start_event(self):
        full = self._history
        rendered = [
            (item, r[1]) for item in full.items if (r := _render_item(item)) is not None
        ]
        self._history = llm.ChatContext([item for item, _ in trim_items(rendered)])
        try:
            return super()._session_start_event()
        finally:
            self._history = full


class _TrimmedGPTLiveModel(GPTLiveModel):
    def session(self) -> GPTLiveSession:
        return _TrimmedSession(self)


FALLBACK_VOICE = "marin"
NOTES_N = 14  # the newest notes a written line is made from
WRITE_TIMEOUT_S = 3.0
# The Realtime model's voices. An agent's voice is picked for GPT-Live, which has more ("stone": 10-07 rig, a
# refused session and a second open). One it lacks maps to one it has, the same one every time for that name,
# so two agents still sound different.
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")


def realtime_voice(voice: str) -> str:
    return voice if voice in REALTIME_VOICES else REALTIME_VOICES[sum(voice.encode()) % len(REALTIME_VOICES)]
# Routing v2 §6 / R6: end a turn when it sounds finished, not after 350 ms of silence, so a mid-thought pause is
# not routed as half a line. The plugin accepts it (validated against the OpenAI SDK union); whether OpenAI
# accepts it on a gpt-4o-mini-transcribe transcription session is unverified, hence SERVER_VAD.
SEMANTIC_VAD = {"type": "semantic_vad", "eagerness": "auto"}
SERVER_VAD = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 600,
    "silence_duration_ms": 700,
}


# A meeting: the plain silence rule, and a short one. semantic_vad gave a line's words 1.5-5.8 s after it was
# said, this gives them in about 1.3 s (rig, 10-06). A sentence it splits at a pause is still one request: the
# next line of someone an agent is attending to needs no name (huddle._attend).
MEET_VAD = {**SERVER_VAD, "silence_duration_ms": 400}


def retry_without_semantic_vad(
    engine, error: BaseException, started_with: str, state: dict
) -> bool:
    """A transcription stream that started on semantic_vad failed with an API error (not a connection
    one): the first such failure switches the engine, and so every stream, to SERVER_VAD; the caller
    reopens the stream. False: a real failure, raise it."""
    if (
        started_with != "semantic"
        or not isinstance(error, APIError)
        or isinstance(error, APIConnectionError)
    ):
        return False
    if state["vad"] == "semantic":
        state["vad"] = "server"
        logger.warning(
            "huddle STT refused semantic_vad; using server_vad", exc_info=error
        )
        emit_metric("live.stt.semantic_vad_rejected", 1, "Count")
        engine.update_options(turn_detection=SERVER_VAD)
    return True


async def _unpublish(room: rtc.Room, track_name: str) -> None:
    # RoomIO never unpublishes its audio track; only this agent's, another agent may be talking.
    lp = room.local_participant
    for pub in list(lp.track_publications.values()):
        if pub.name == track_name:
            try:
                await lp.unpublish_track(pub.sid)
            except rtc.participant.UnpublishTrackError:
                pass  # already gone (the room is closing): nothing left to bill


class _Live:
    """One agent's GPT-Live session, as huddle.Live: a straight binding onto LiveKit's API."""

    def __init__(
        self,
        agent_id: str,
        session: AgentSession,
        duplex: GPTLiveSession,
        room: rtc.Room,
        muted: bool = False,
    ) -> None:
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self._session, self._room = session, room
        self.append_thinking, self.append_commentary = (
            duplex.append_thinking,
            duplex.append_commentary,
        )
        self.append_instructions = duplex.append_instructions
        self._duplex, self._muted = duplex, muted  # muted from its start: a meeting voice (see _Voice.on_enter)
        self._rule: str | None = None  # the meeting rule in force (huddle.MEET_RULES), re-applied on a reconnect
        # A reconnect is a new GPT-Live session, and a new session hears: a muted voice (a meeting's, which
        # must never hear the Meet) is muted again on each one.
        duplex.on("session_reconnected", self._on_reconnected)
        self.nudge = lambda: session.generate_reply()  # a request the model may decline
        self.reply_to = lambda text: session.generate_reply(
            user_input=text
        )  # a typed user turn

    def set_participant(self, identity: str) -> None:
        self._session.room_io.set_participant(identity)

    def hear(self, since: float) -> None:
        pass  # it hears its speaker itself

    def say(self, text: str) -> None:
        self.append_commentary(text)  # GPT-Live has no word-for-word speech: its own words for it

    def mute(self) -> None:
        self._muted = True
        self._duplex.mute_input()

    def meet_rule(self, text: str) -> None:
        """The standing rule of a per-person meeting voice: who it hears. The newest supersedes (its wording
        says so), and a reconnect, which rebuilds the session from its first instructions, gets it again."""
        self._rule = text
        self._duplex.append_instructions(text)

    def _on_reconnected(self, _ev=None) -> None:
        if self._muted:
            self._duplex.mute_input()
        if self._rule:
            self._duplex.append_instructions(self._rule)

    def interrupt(self):
        return self._session.interrupt(force=True)

    def usage_seconds(self) -> float:
        return sum(
            u.session_duration
            for u in self._session.usage.model_usage
            if isinstance(u, LLMModelUsage)
        )

    async def aclose(self) -> None:
        await self._session.aclose()
        await _unpublish(self._room, self.track_name)


#: Added to a driven voice's persona: every word it says is ordered by the worker.
DRIVEN = "You read lines aloud. You are given each line, and you say exactly that line and nothing else."
SAY = "Say this line aloud, word for word, and nothing else:\n"
# The agent's short lines (an acknowledgement, a status, a reply to small talk) are written by a small text
# model from what the worker knows (who said what, what was asked, what is open), then read aloud word for
# word. The speech model never words anything: with a question in view it answered from its own head ("it's
# $1,600", 10-07 rig). Measured 10-07: about 0.6 s to write, 10 of 10 lines with no invented fact.
WRITER_MODEL = "gpt-4.1-mini"
WRITER = (
    "You write the one short sentence this teammate says aloud next on a voice call, in the first person, in "
    "their own voice. The notes say who said what and what is being worked on. This teammate's answers to real "
    "questions come from work that takes time and arrive separately: never answer a question yourself, and "
    "never state a number, price, date, name or fact that is not in the notes. Acknowledge what was asked "
    "specifically, or say truthfully how the open work stands, or reply to small talk the way a person would. "
    "One sentence, at most 16 words, natural and plain, a different way each time. No filler, no \"backend\", "
    "no mention of notes or instructions. Output the sentence only."
)


class _DrivenSession(openai.realtime.RealtimeSession):
    """A line to say word for word goes to the model without the conversation (`input: []`), and what it said
    still joins the conversation. With the conversation in view the model answers the last thing a person
    said instead, from its own head (10-07, real model: 5 of 12 lines exact after an open question, "$12 per
    user per month" among the rest; 12 of 12 without)."""

    # ponytail: private API (the plugin builds response.create itself), re-check on livekit-agents upgrades
    def send_event(self, event) -> None:
        if isinstance(event, ResponseCreateEvent):
            event.response.input = []  # every order carries all it needs: the voice never reads the conversation
        super().send_event(event)


class _DrivenModel(openai.realtime.RealtimeModel):
    def session(self, *, turn_detection_disabled: bool = False) -> _DrivenSession:
        sess = _DrivenSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(sess)
        return sess


class _DrivenLive:
    """One agent's voice in a meeting, as huddle.Live: a Realtime model with no ears and no turn detection, so
    it is silent until the worker orders it to speak, and what it knows arrives as text. GPT-Live answers
    whatever it hears and cannot be told to listen without answering (10-07 rig sales call).

    Orders run one after the other on one chain, so two lines never overlap and what it is told to know
    lands before the order after it. `interrupt` stops the speech and drops the orders not yet started."""

    def __init__(
        self, agent_id: str, session: AgentSession, agent: Agent, room: rtc.Room, persona: str = ""
    ) -> None:
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self._session, self._agent, self._room = session, agent, room
        self._tail: asyncio.Task | None = None
        self._epoch = 0  # raised by interrupt: a speech order queued under an older one is dropped
        # The Realtime plugin reports tokens, not session seconds (`session_duration` is 0): wall clock, open to
        # close, is what a GPT-Live session bills by too.
        self._opened, self._closed = time.monotonic(), None
        # What it was told to know since it last spoke (its seed first): a nudge's order is made of these.
        self._notes: list[str] = [
            m.text_content or "" for m in agent.chat_ctx.items if getattr(m, "role", "") == "developer"
        ]
        self._persona = persona  # who it is, for the lines written for it
        self._writer: AsyncOpenAI | None = None  # made at the first line: a test has no key

    def _order(self, do, keep: bool = False) -> None:
        """`do()` after every order before it. `keep`: something to know, which an interrupt does not drop."""
        prior, epoch = self._tail, self._epoch

        async def run() -> None:
            if prior is not None:
                await asyncio.wait([prior])
            if self._closed is not None or (not keep and epoch != self._epoch):
                return
            try:
                await do()  # a speech handle: done when the line is said, or cut. It never raises.
            except Exception:
                logger.exception("huddle driven voice order failed")

        self._tail = asyncio.create_task(run())

    def say(self, text: str) -> None:
        self._order(lambda: self._session.generate_reply(instructions=SAY + text))

    def _own(self, text: str) -> None:
        async def do() -> None:
            line = await self._write(text)
            if line:
                await self._session.generate_reply(instructions=SAY + line)

        self._order(do)

    async def _write(self, notes: str) -> str:
        try:
            self._writer = self._writer or AsyncOpenAI()
            r = await asyncio.wait_for(
                self._writer.chat.completions.create(
                    model=WRITER_MODEL,
                    max_tokens=48,
                    temperature=0.8,
                    messages=[
                        {"role": "system", "content": f"{self._persona}\n\n{WRITER}"},
                        {"role": "user", "content": notes},
                    ],
                ),
                WRITE_TIMEOUT_S,
            )
            return (r.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001 -- no line is better than a late or a made-up one
            logger.warning("huddle driven voice: no line written", exc_info=True)
            return ""

    def append_commentary(self, text: str, *, delegation_id: str | None = None) -> None:
        self._own("\n".join([*self._notes[-NOTES_N:], text]))  # said once, in its own words

    def append_thinking(self, text: str, *, delegation_id: str | None = None) -> None:
        self._notes.append(text)

        async def know() -> None:
            ctx = self._agent.realtime_llm_session.chat_ctx.copy()
            ctx.add_message(role="developer", content=text)
            await self._agent.update_chat_ctx(ctx)  # an item of the conversation: no response is asked for

        self._order(know, keep=True)

    def append_instructions(self, text: str) -> None:
        # Part of the session's instructions, so a reconnect, which sends them again, keeps the rule.
        self._order(lambda: self._agent.update_instructions(f"{self._agent.instructions}\n\n{text}"), keep=True)

    meet_rule = append_instructions

    def nudge(self) -> None:
        if self._notes:  # nothing to go on: nothing to say
            self._own("\n".join(self._notes[-NOTES_N:]) + "\n\nSay your line now.")

    def reply_to(self, text: str) -> None:
        self._own("\n".join([*self._notes[-NOTES_N:], f'They just said to you: "{text}"']))

    def interrupt(self):
        self._epoch += 1
        return self._session.interrupt(force=True)

    def mute(self) -> None:
        pass  # it has no ears

    def hear(self, since: float) -> None:
        pass

    def set_participant(self, identity: str) -> None:
        pass

    def usage_seconds(self) -> float:
        return (self._closed if self._closed is not None else time.monotonic()) - self._opened

    def _stop(self) -> None:
        if self._closed is None:
            self._closed = time.monotonic()

    async def aclose(self) -> None:
        self._stop()
        await self._session.aclose()
        await _unpublish(self._room, self.track_name)


def noise_filter():
    """LiveKit Cloud's background noise model (Krisp NC, no extra cost), on everything a person's microphone
    sends before the transcriber and the voice hear it: music, a fan or a street is not speech, and the
    transcriber invents words for it (10-04 prod Meet: "Hello, guys." from a room with a TV on). It keeps
    all speech, a second person's too; removing other voices is the paid voice-isolation model."""
    return noise_cancellation.NC()


def room_options(agent_id: str, speaker: str, ears: bool = True) -> room_io.RoomOptions:
    # Pinned to the speaker, so it never hears whoever RoomIO would auto-link first. No text input
    # (people type into the room, not to the voice) and no pre-connect buffer (the worker is not a
    # client that holds audio for it). `ears=False`: a driven voice, which takes no audio at all.
    return room_io.RoomOptions(
        participant_identity=speaker,
        close_on_disconnect=False,
        text_input=False,
        audio_input=room_io.AudioInputOptions(pre_connect_audio=False, noise_cancellation=noise_filter())
        if ears
        else False,
        audio_output=room_io.AudioOutputOptions(track_name=f"agent-{agent_id}"),
    )


async def _open(session: AgentSession, agent: Agent, room, huddle: Huddle, agent_id, speaker, ears=True) -> None:
    """Start `session` in the room with what every voice reports to the huddle: its state and what it said."""
    session.on(
        "agent_state_changed", lambda ev: huddle.on_agent_state(agent_id, ev.new_state)
    )

    def said(ev) -> None:
        # ChatItem is a union: an AgentHandoff / FunctionCall / AgentConfigUpdate has no role.
        if (
            ev.item.type == "message"
            and ev.item.role == "assistant"
            and (text := ev.item.text_content)
        ):
            huddle.on_agent_said(agent_id, text, bool(getattr(ev.item, "interrupted", False)))

    session.on("conversation_item_added", said)
    try:
        # record=False: a huddle leaves nothing behind, whatever the LiveKit Cloud project's recording default.
        await session.start(
            agent=agent,
            room=room,
            room_options=room_options(agent_id, speaker, ears),
            record=False,
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await session.aclose()
            await _unpublish(room, f"agent-{agent_id}")
        raise


async def _start(
    room, huddle: Huddle, vad, agent_id, persona, voice, speaker, seed
) -> _Live:
    agent = _Voice(agent_id, persona, huddle, seed)
    session = AgentSession(
        llm=_TrimmedGPTLiveModel(voice=voice, delegation="client"), vad=vad
    )
    session.on(
        "user_state_changed",
        lambda ev: ev.new_state == "speaking" and huddle.on_speaker_activity(agent_id),
    )
    await _open(session, agent, room, huddle, agent_id, speaker)
    live = _Live(agent_id, session, agent.duplex_session, room)  # type: ignore[arg-type]
    # An unrecoverable realtime error closes the session under us: end the exchange then, not on the next call.
    session.on("close", lambda _ev: huddle.on_live_closed(live))
    return live


async def _start_driven(
    room, huddle: Huddle, vad, agent_id, persona, voice, speaker, seed
) -> _DrivenLive:
    agent = Agent(instructions=f"{persona}\n\n{DRIVEN}", chat_ctx=_seeded(seed))
    # turn_detection=None and no audio input: nothing but an order makes it speak. No vad: nothing to detect.
    session = AgentSession(
        llm=_DrivenModel(model="gpt-realtime", voice=realtime_voice(voice), turn_detection=None)
    )
    await _open(session, agent, room, huddle, agent_id, speaker, ears=False)
    live = _DrivenLive(agent_id, session, agent, room, persona=persona)

    def closed(_ev) -> None:
        live._stop()  # billed to here, whoever closed it
        huddle.on_live_closed(live)

    session.on("close", closed)
    return live


async def start_live(
    room, huddle: Huddle, vad, *, agent_id, persona, voice, speaker, seed
) -> _Live | _DrivenLive:
    # Every Meet voice is a mouth: the worker listens, /route and the agent decide, the voice says it.
    start = _start_driven if in_meet(speaker) else _start
    try:
        return await start(room, huddle, vad, agent_id, persona, voice, speaker, seed)
    except Exception:
        if voice == FALLBACK_VOICE:
            raise
        # a voice the model no longer accepts must not cost the agent its voice altogether
        logger.warning(
            "huddle voice %s refused; retrying with %s",
            voice,
            FALLBACK_VOICE,
            exc_info=True,
        )
        emit_metric("live.voice.fallback", 1, "Count")
        return await start(
            room, huddle, vad, agent_id, persona, FALLBACK_VOICE, speaker, seed
        )


def person(p: rtc.Participant) -> bool:
    """A human of the call. A Meet person (`meet:<roomId>:<participantId>`) or a mixed Meet (`meet:<roomId>`, an
    older Recall bot) counts; the bot's own page (`meetbot:<roomId>`) publishes no audio and is nobody."""
    if p.identity.startswith("meetbot:"):
        return False
    return p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD or p.identity.startswith("meet:")


class _Room:
    def __init__(self, ctx: JobContext) -> None:
        self._ctx, self._tasks = ctx, set()
        self.closed = False  # the entrypoint loop returns on its own, before LiveKit has to cancel it

    def publish(self, topic: str, payload: dict, to: list[str]) -> None:
        lp = self._ctx.room.local_participant
        t = asyncio.create_task(
            lp.publish_data(json.dumps(payload), topic=topic, destination_identities=to)
        )
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def set_attributes(self, attrs: dict[str, str]) -> None:
        t = asyncio.create_task(self._ctx.room.local_participant.set_attributes(attrs))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        self.closed = True
        self._ctx.shutdown("no humans left in the huddle")


async def _on_request(req: JobRequest) -> None:
    await req.accept(identity=AGENT_IDENTITY, name=WORKER_DISPLAY_NAME)


# agent_name is deprecated in 1.8.3 (worker.py rtc_session) and LIVEKIT_AGENT_NAME wins over it; the
# Dockerfile sets LIVEKIT_AGENT_NAME=lightbulb-huddle so both agree. Drop this kwarg once it is removed.
# HUDDLE_RIG_AGENT_NAME: the multi-voice test rig runs this worker on a laptop under its own dispatch name, so no
# real call reaches it. Unset everywhere else.
@server.rtc_session(agent_name=os.environ.get("HUDDLE_RIG_AGENT_NAME", "lightbulb-huddle"), on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata)
    started = time.monotonic()
    usage = {"stt": 0.0}
    emitted = {"stt": 0.0, "agent": 0.0, "gptlive": 0.0}
    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    vad = silero.VAD.load()
    # streaming; interims still drive barge-in, semantic_vad only decides when a final lands
    meeting = meta.get("source") == "meet"
    stt_engine = openai.STT(
        model="gpt-4o-mini-transcribe", use_realtime=True, turn_detection=MEET_VAD if meeting else SEMANTIC_VAD
    )
    vad_state = {"vad": "server" if meeting else "semantic"}

    @stt_engine.on("metrics_collected")
    def _stt_metrics(m) -> None:
        usage["stt"] += m.audio_duration

    async def live_factory(**kw) -> _Live | _DrivenLive:
        return await start_live(ctx.room, huddle, vad, **kw)

    def flush_metrics() -> None:
        # deltas since the last flush, so a crashed job loses at most a minute
        now = {
            "stt": usage["stt"],
            "agent": time.monotonic() - started,
            "gptlive": huddle.gptlive_seconds(),
        }
        # a voice mid-close or a failed usage read never sends a negative delta
        now["gptlive"] = max(now["gptlive"], emitted["gptlive"])
        for k in now:
            emit_metric(
                "huddle.gptlive.open_seconds"
                if k == "gptlive"
                else f"huddle.{k}.seconds",
                now[k] - emitted[k],
            )
        emitted.update(now)

    room = _Room(ctx)
    huddle = Huddle(
        room=room,
        backend=backend,
        live_factory=live_factory,
        on_vocabulary=lambda prompt: stt_engine.update_options(
            prompt=prompt
        ),  # open streams take it in place
        # a Google Meet bridged in by a Recall bot (the meeting dispatch)
        setting="meeting" if meta.get("source") == "meet" else "live",
    )
    tasks: set[asyncio.Task] = set()

    def _done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and (error := t.exception()) is not None:
            logger.error("huddle task failed", exc_info=error)

    def spawn(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(_done)
        return t

    def humans() -> dict[str, str]:
        return {
            p.identity: p.name or p.identity
            for p in ctx.room.remote_participants.values()
            if person(p)
        }

    async def transcribe(track: rtc.Track, p: rtc.RemoteParticipant) -> None:
        while True:
            started_with = vad_state["vad"]
            stream = stt_engine.stream()
            audio = rtc.AudioStream.from_track(track=track, noise_cancellation=noise_filter())

            async def push() -> None:
                async for ev in audio:
                    stream.push_frame(ev.frame)
                stream.end_input()

            pusher = asyncio.create_task(push())
            try:
                async for ev in stream:
                    try:
                        # the transcriber's own speech detector: it answers to speech, not to sound, and it
                        # fires before any word is transcribed (who is talking, for the floor)
                        if ev.type == stt.SpeechEventType.START_OF_SPEECH:
                            huddle.on_speech(p.identity, True)
                        elif ev.type == stt.SpeechEventType.END_OF_SPEECH:
                            huddle.on_speech(p.identity, False)
                        if not ev.alternatives:
                            continue
                        if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                            await huddle.on_final_transcript(
                                p.identity,
                                p.name or p.identity,
                                ev.alternatives[0].text,
                            )
                        elif (
                            ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT
                        ):  # use_realtime: cumulative, ~0.5 s apart
                            huddle.on_interim_transcript(
                                p.identity,
                                p.name or p.identity,
                                ev.alternatives[0].text,
                            )
                    except (
                        Exception
                    ):  # one bad event never ends this person's transcription
                        logger.exception("huddle transcript handling failed")
                return
            except Exception as e:
                # no transcriber now: nobody is held as talking, and a reopened stream starts a new utterance
                huddle.stt_stopped(p.identity)
                if not retry_without_semantic_vad(
                    stt_engine, e, started_with, vad_state
                ):
                    raise
            finally:
                await utils.aio.cancel_and_wait(pusher)
                await audio.aclose()
                await stream.aclose()

    stt_tasks: dict[str, asyncio.Task] = {}  # identity -> that person's transcription

    def stop_stt(identity: str) -> None:
        huddle.stt_stopped(identity)
        if (t := stt_tasks.pop(identity, None)) is not None:
            t.cancel()

    def start_stt(track: rtc.Track | None, p: rtc.Participant) -> None:
        if track is None or track.kind != rtc.TrackKind.KIND_AUDIO or not person(p):
            return
        stop_stt(p.identity)
        stt_tasks[p.identity] = spawn(transcribe(track, p))

    def on_track(
        track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
    ) -> None:
        if (
            not pub.muted
        ):  # a muted mic is not transcribed (or paid for); unmute starts it
            start_stt(track, p)

    def on_muted(p: rtc.Participant, pub: rtc.TrackPublication) -> None:
        if pub.kind == rtc.TrackKind.KIND_AUDIO and isinstance(
            p, rtc.RemoteParticipant
        ):
            stop_stt(p.identity)

    def on_unmuted(p: rtc.Participant, pub: rtc.TrackPublication) -> None:
        if isinstance(p, rtc.RemoteParticipant):
            start_stt(pub.track, p)

    def rpc_talk(data: rtc.RpcInvocationData, start: bool) -> str:
        # caller_identity is set by the LiveKit server; it is the only speaker identity we trust.
        # Answer at once: a summon outlasts the RPC's 10 s response timeout.
        spawn(
            huddle.talk_start(data.caller_identity, data.payload)
            if start
            else huddle.talk_end(data.caller_identity)
        )
        return "ok"

    async def on_shutdown(_reason: str = "") -> None:
        await (
            huddle.clear_presence()
        )  # first: a stale "Live" marker outlives everything below
        await huddle.end_exchange(force=True)  # a request still open must not hold the call's window
        flush_metrics()
        await backend.aclose()

    ctx.add_shutdown_callback(on_shutdown)
    ctx.room.on("track_subscribed", on_track)
    ctx.room.on("track_unsubscribed", lambda _t, _pub, p: stop_stt(p.identity))
    ctx.room.on("track_muted", on_muted)
    ctx.room.on("track_unmuted", on_unmuted)
    ctx.room.on(
        "participant_connected", lambda _p: spawn(huddle.on_participants(humans()))
    )
    ctx.room.on(
        "participant_disconnected", lambda _p: spawn(huddle.on_participants(humans()))
    )
    ctx.room.on(
        "data_received",
        lambda pkt: huddle.on_data(pkt.participant, pkt.topic, pkt.data),
    )
    # the SFU's speaking set: an agent announces its news only into a quiet room (gate.py)
    ctx.room.on(
        "active_speakers_changed",
        lambda speakers: huddle.on_active_speakers({p.identity for p in speakers}),
    )
    await ctx.connect()
    # LiveKit sends a participant's attribute changes at once only while it publishes a track; one that does
    # not is batched every 3 s (measured on the rig: a tile went to "working" 3.0 s after the worker wrote it).
    # The worker publishes only while a voice is open, so it holds one silent track for its whole job.
    presence = rtc.LocalAudioTrack.create_audio_track("presence", rtc.AudioSource(48000, 1))
    await ctx.room.local_participant.publish_track(presence, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_UNKNOWN))
    ctx.room.local_participant.register_rpc_method(
        "talk_start", lambda d: rpc_talk(d, True)
    )
    ctx.room.local_participant.register_rpc_method(
        "talk_end", lambda d: rpc_talk(d, False)
    )
    await huddle.on_participants(humans())
    for n in itertools.count(1):
        if room.closed:
            return  # shutdown callbacks run next; LiveKit no longer has to cancel this loop
        await asyncio.sleep(1)
        await huddle.tick()
        if n % 60 == 0:
            flush_metrics()


if __name__ == "__main__":
    cli.run_app(server)
