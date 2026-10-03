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
from livekit.plugins import openai, silero
from livekit.plugins.openai.realtime import (
    GPTLiveDelegation,
    GPTLiveModel,
    GPTLiveSession,
)
from livekit.plugins.openai.realtime.gpt_live_model import _render_item

from huddle_agent.backend import Backend
from huddle_agent.huddle import Huddle, emit_metric
from huddle_agent.transcript import trim_items

logger = logging.getLogger("huddle")
AGENT_IDENTITY = "agent-ceo"  # C4: every agent voice publishes as this one participant
WORKER_DISPLAY_NAME = "Lightbulb"  # clients render tiles from attributes, not this name
server = AgentServer()


class _Voice(Agent):
    """One agent's voice. The seed is the huddle so far, as this agent saw it (Transcript.seed)."""

    def __init__(
        self, agent_id: str, persona: str, huddle: Huddle, seed: list[tuple[str, str]]
    ) -> None:
        ctx = llm.ChatContext.empty()
        for role, text in seed:
            ctx.add_message(role=role, content=text)
        super().__init__(
            instructions=persona, chat_ctx=ctx
        )  # no tools: client delegation raises on any
        self._agent_id, self._huddle = agent_id, huddle
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_enter(self) -> None:
        session = self.duplex_session
        assert isinstance(session, GPTLiveSession)
        session.on("delegation_created", self._on_delegation_created)

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
    a long huddle would outgrow the seed's token limit. Seed the newest items within ours instead."""

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
    ) -> None:
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self._session, self._room = session, room
        self.set_participant = session.room_io.set_participant
        self.append_thinking, self.append_commentary = (
            duplex.append_thinking,
            duplex.append_commentary,
        )
        self.append_instructions = duplex.append_instructions
        self.mute = duplex.mute_input
        self.nudge = lambda: session.generate_reply()  # a request the model may decline
        self.reply_to = lambda text: session.generate_reply(
            user_input=text
        )  # a typed user turn

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


def room_options(agent_id: str, speaker: str) -> room_io.RoomOptions:
    # Pinned to the speaker, so it never hears whoever RoomIO would auto-link first. No text input
    # (people type into the room, not to the voice) and no pre-connect buffer (the worker is not a
    # client that holds audio for it).
    return room_io.RoomOptions(
        participant_identity=speaker,
        close_on_disconnect=False,
        text_input=False,
        audio_input=room_io.AudioInputOptions(pre_connect_audio=False),
        audio_output=room_io.AudioOutputOptions(track_name=f"agent-{agent_id}"),
    )


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
            huddle.on_agent_said(agent_id, text)

    session.on("conversation_item_added", said)
    try:
        # record=False: a huddle leaves nothing behind, whatever the LiveKit Cloud project's recording default.
        await session.start(
            agent=agent,
            room=room,
            room_options=room_options(agent_id, speaker),
            record=False,
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await session.aclose()
            await _unpublish(room, f"agent-{agent_id}")
        raise
    live = _Live(agent_id, session, agent.duplex_session, room)  # type: ignore[arg-type]
    # An unrecoverable realtime error closes the session under us: end the exchange then, not on the next call.
    session.on("close", lambda _ev: huddle.on_live_closed(live))
    return live


async def start_live(
    room, huddle: Huddle, vad, *, agent_id, persona, voice, speaker, seed
) -> _Live:
    try:
        return await _start(room, huddle, vad, agent_id, persona, voice, speaker, seed)
    except Exception:
        if voice == FALLBACK_VOICE:
            raise
        # a voice GPT-Live no longer accepts must not cost the agent its voice altogether
        logger.warning(
            "huddle voice %s refused; retrying with %s",
            voice,
            FALLBACK_VOICE,
            exc_info=True,
        )
        emit_metric("live.voice.fallback", 1, "Count")
        return await _start(
            room, huddle, vad, agent_id, persona, FALLBACK_VOICE, speaker, seed
        )


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
@server.rtc_session(agent_name="lightbulb-huddle", on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata)
    started = time.monotonic()
    usage = {"stt": 0.0}
    emitted = {"stt": 0.0, "agent": 0.0, "gptlive": 0.0}
    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    vad = silero.VAD.load()
    # streaming; interims still drive barge-in, semantic_vad only decides when a final lands
    stt_engine = openai.STT(
        model="gpt-4o-mini-transcribe", use_realtime=True, turn_detection=SEMANTIC_VAD
    )
    vad_state = {"vad": "semantic"}

    @stt_engine.on("metrics_collected")
    def _stt_metrics(m) -> None:
        usage["stt"] += m.audio_duration

    async def live_factory(**kw) -> _Live:
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

    def person(p: rtc.Participant) -> bool:
        # the Recall bot's `meet:<roomId>` participant is the whole meeting: it counts as a person
        return (
            p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            or p.identity.startswith("meet:")
        )

    def humans() -> dict[str, str]:
        return {
            p.identity: p.name or p.identity
            for p in ctx.room.remote_participants.values()
            if person(p)
        }

    async def transcribe(track: rtc.Track, p: rtc.RemoteParticipant) -> None:
        while True:
            started_with = vad_state["vad"]
            stream, audio = stt_engine.stream(), rtc.AudioStream(track)

            async def push() -> None:
                async for ev in audio:
                    stream.push_frame(ev.frame)
                stream.end_input()

            pusher = asyncio.create_task(push())
            try:
                async for ev in stream:
                    if not ev.alternatives:
                        continue
                    try:
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
                if not retry_without_semantic_vad(
                    stt_engine, e, started_with, vad_state
                ):
                    raise
                huddle.stt_stopped(
                    p.identity
                )  # the reopened stream starts a new utterance
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
        await huddle.end_exchange()
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
