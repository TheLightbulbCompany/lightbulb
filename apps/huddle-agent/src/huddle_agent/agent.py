# The delegation handling (_CEO.on_enter / _on_delegation_created / _on_done) is adapted from
# livekit/agents examples/voice_agents/gpt-live/client_delegation.py,
# Copyright LiveKit, Inc., licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changes: the answer comes from our backend
# (Huddle.on_delegation) instead of a local LLM desk.
"""LiveKit glue for the CEO huddle worker. All behaviour lives in huddle.Huddle."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobRequest, cli, room_io, stt, utils
from livekit.agents.metrics import LLMModelUsage
from livekit.plugins import openai, silero
from livekit.plugins.openai.realtime import GPTLiveDelegation, GPTLiveModel, GPTLiveSession

from huddle_agent.backend import Backend
from huddle_agent.huddle import VOICE_NAME, Huddle, emit_metric

logger = logging.getLogger("huddle")
AGENT_IDENTITY = "agent-ceo"
server = AgentServer()


class _CEO(Agent):
    def __init__(self, persona: str, huddle: Huddle) -> None:
        super().__init__(instructions=persona)  # no tools: client delegation raises on any
        self._huddle = huddle
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_enter(self) -> None:
        session = self.duplex_session
        assert isinstance(session, GPTLiveSession)
        session.on("delegation_created", self._on_delegation_created)

    async def on_exit(self) -> None:
        await utils.aio.cancel_and_wait(*self._tasks)

    def _on_delegation_created(self, d: GPTLiveDelegation) -> None:
        # emitted from the plugin's read loop, so the work has to run in a task of its own
        task = asyncio.create_task(self._huddle.on_delegation(d.id, d.pending_transcript), name=f"turn:{d.id}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("delegation failed", extra={"error_type": type(error).__name__})


AUDIO_TRACK = "roomio_audio"  # RoomIO's default agent audio track name (voice/room_io/_output.py)


class _Live:
    """One summon's GPT-Live session, as huddle.Live: a straight binding onto LiveKit's API."""

    def __init__(self, session: AgentSession, duplex: GPTLiveSession, room: rtc.Room) -> None:
        self._session, self._room = session, room
        self.set_participant = session.room_io.set_participant
        self.append_thinking, self.append_commentary = duplex.append_thinking, duplex.append_commentary
        self.mute = duplex.mute_input
        self.nudge = lambda: session.generate_reply()  # a request the model may decline
        self.reply_to = lambda text: session.generate_reply(user_input=text)  # a typed user turn

    def interrupt(self) -> None:
        # Cuts the playing speech (AgentActivity.interrupt -> SpeechHandle.interrupt -> clear_buffer).
        # The duplex adapter's own interrupt is a no-op: the model is silenced, not cancelled.
        try:
            self._session.interrupt(force=True)
        except RuntimeError:  # the session is not running (closing)
            logger.info("huddle interrupt skipped: session not running")

    def usage_seconds(self) -> float:
        return sum(u.session_duration for u in self._session.usage.model_usage if isinstance(u, LLMModelUsage))

    async def aclose(self) -> None:
        await self._session.aclose()
        # RoomIO never unpublishes its audio track; without this every summon leaves a dead one behind.
        lp = self._room.local_participant
        for pub in list(lp.track_publications.values()):
            if pub.name == AUDIO_TRACK:
                await lp.unpublish_track(pub.sid)


async def start_live(room: rtc.Room, huddle: Huddle, vad, persona: str, voice: str, speaker: str) -> _Live:
    agent = _CEO(persona, huddle)
    session = AgentSession(llm=GPTLiveModel(voice=voice, delegation="client"), vad=vad)
    session.on("user_state_changed", lambda ev: ev.new_state == "speaking" and huddle.on_speaker_activity())
    session.on("agent_state_changed", lambda ev: huddle.on_agent_state(ev.new_state))
    # Lightbulb's own words, once a spoken message ends (emitted after the state leaves "speaking")
    session.on("conversation_item_added", lambda ev: _on_item(huddle, ev.item))
    # record=False: a huddle leaves nothing behind, whatever the LiveKit Cloud project's recording default.
    # Pinned to the speaker, so it never hears whoever RoomIO would auto-link first.
    opts = room_io.RoomOptions(participant_identity=speaker, close_on_disconnect=False)
    await session.start(agent=agent, room=room, room_options=opts, record=False)
    return _Live(session, agent.duplex_session, room)  # type: ignore[arg-type]


def _on_item(huddle: Huddle, item) -> None:
    if getattr(item, "role", None) == "assistant" and (text := item.text_content):
        huddle.on_agent_said(text)


class _Room:
    def __init__(self, ctx: JobContext) -> None:
        self._ctx, self._tasks = ctx, set()

    def publish(self, topic: str, payload: dict, to: list[str]) -> None:
        lp = self._ctx.room.local_participant
        t = asyncio.create_task(lp.publish_data(json.dumps(payload), topic=topic, destination_identities=to))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def set_attributes(self, attrs: dict[str, str]) -> None:
        t = asyncio.create_task(self._ctx.room.local_participant.set_attributes(attrs))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        self._ctx.shutdown("no humans left in the huddle")


async def _on_request(req: JobRequest) -> None:
    await req.accept(identity=AGENT_IDENTITY, name=VOICE_NAME)  # the identity is internal; people see "Lightbulb"


# agent_name is deprecated in 1.8.3 (worker.py rtc_session) and LIVEKIT_AGENT_NAME wins over it; the
# Dockerfile sets LIVEKIT_AGENT_NAME=lightbulb-huddle so both agree. Drop this kwarg once it is removed.
@server.rtc_session(agent_name="lightbulb-huddle", on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata)
    started = time.monotonic()
    usage = {"stt": 0.0}
    emitted = {"stt": 0.0, "agent": 0.0}
    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    vad = silero.VAD.load()
    stt_engine = openai.STT(model="gpt-4o-mini-transcribe", use_realtime=True)  # streaming

    @stt_engine.on("metrics_collected")
    def _stt_metrics(m) -> None:
        usage["stt"] += m.audio_duration

    async def live_factory(persona: str, voice: str, speaker: str) -> _Live:
        return await start_live(ctx.room, huddle, vad, persona, voice, speaker)

    def flush_metrics() -> None:
        # deltas since the last flush, so a crashed job loses at most a minute
        now = {"stt": usage["stt"], "agent": time.monotonic() - started}
        for k in now:
            emit_metric(f"huddle.{k}.seconds", now[k] - emitted[k])
        emitted.update(now)

    huddle = Huddle(room=_Room(ctx), backend=backend, live_factory=live_factory)
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    def humans() -> dict[str, str]:
        return {
            p.identity: p.name or p.identity
            for p in ctx.room.remote_participants.values()
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
        }

    async def transcribe(track: rtc.Track, p: rtc.RemoteParticipant) -> None:
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
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    await huddle.on_final_transcript(p.identity, p.name or p.identity, ev.alternatives[0].text)
                elif ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:  # use_realtime: cumulative, ~0.5 s apart
                    huddle.on_interim_transcript(p.identity, p.name or p.identity, ev.alternatives[0].text)
        finally:
            await utils.aio.cancel_and_wait(pusher)
            await audio.aclose()
            await stream.aclose()

    def on_track(track: rtc.Track, _pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            spawn(transcribe(track, p))

    def rpc_talk(data: rtc.RpcInvocationData, start: bool) -> str:
        # caller_identity is set by the LiveKit server; it is the only speaker identity we trust.
        # Answer at once: a summon outlasts the RPC's 10 s response timeout.
        spawn((huddle.talk_start if start else huddle.talk_end)(data.caller_identity))
        return "ok"

    async def on_shutdown(_reason: str = "") -> None:
        await huddle.end_exchange()
        flush_metrics()
        await backend.aclose()

    ctx.add_shutdown_callback(on_shutdown)
    ctx.room.on("track_subscribed", on_track)
    ctx.room.on("participant_connected", lambda _p: spawn(huddle.on_participants(humans())))
    ctx.room.on("participant_disconnected", lambda _p: spawn(huddle.on_participants(humans())))
    ctx.room.on("data_received", lambda pkt: huddle.on_data(pkt.participant, pkt.topic, pkt.data))
    await ctx.connect()
    ctx.room.local_participant.register_rpc_method("talk_start", lambda d: rpc_talk(d, True))
    ctx.room.local_participant.register_rpc_method("talk_end", lambda d: rpc_talk(d, False))
    await huddle.on_participants(humans())
    for n in itertools.count(1):
        await asyncio.sleep(1)
        await huddle.tick()
        if n % 60 == 0:
            flush_metrics()


if __name__ == "__main__":
    cli.run_app(server)
