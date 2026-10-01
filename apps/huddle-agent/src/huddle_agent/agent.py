# The delegation handling (_CEO.on_enter / _on_delegation_created / _on_done) is adapted from
# livekit/agents examples/voice_agents/gpt-live/client_delegation.py,
# Copyright LiveKit, Inc., licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changes: the answer comes from our backend
# (Huddle.on_delegation) instead of a local LLM desk.
"""LiveKit glue for the CEO huddle worker. All behaviour lives in huddle.Huddle."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobRequest, cli, room_io, stt, utils
from livekit.agents.metrics import LLMModelUsage
from livekit.plugins import openai, silero
from livekit.plugins.openai.realtime import GPTLiveDelegation, GPTLiveModel, GPTLiveSession

from huddle_agent.backend import Backend
from huddle_agent.huddle import Huddle, emit_metric

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


class _Live:
    """One summon's GPT-Live session, as huddle.Live: a straight binding onto LiveKit's API."""

    def __init__(self, session: AgentSession, duplex: GPTLiveSession) -> None:
        self.set_participant = session.room_io.set_participant
        self.append_thinking, self.append_commentary = duplex.append_thinking, duplex.append_commentary
        self.mute, self.unmute = duplex.mute_input, duplex.unmute_input
        self.nudge = lambda: session.generate_reply()  # a request the model may decline
        self.reply_to = lambda text: session.generate_reply(user_input=text)  # a typed user turn
        self.usage_seconds = lambda: sum(
            u.session_duration for u in session.usage.model_usage if isinstance(u, LLMModelUsage)
        )
        self.aclose = session.aclose


class _Room:
    def __init__(self, ctx: JobContext) -> None:
        self._ctx, self._tasks = ctx, set()

    def publish(self, topic: str, payload: dict) -> None:
        t = asyncio.create_task(self._ctx.room.local_participant.publish_data(json.dumps(payload), topic=topic))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        self._ctx.shutdown("no humans left in the huddle")


async def _on_request(req: JobRequest) -> None:
    meta = json.loads(req.job.metadata or "{}")
    await req.accept(identity=AGENT_IDENTITY, name=meta.get("ceoName") or "CEO")


@server.rtc_session(agent_name="lightbulb-huddle", on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata)
    started = time.monotonic()
    usage = {"stt": 0.0}
    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    vad = silero.VAD.load()
    stt_engine = openai.STT(model="gpt-4o-mini-transcribe", use_realtime=True)  # streaming

    @stt_engine.on("metrics_collected")
    def _stt_metrics(m) -> None:
        usage["stt"] += m.audio_duration

    async def live_factory(persona: str, voice: str) -> _Live:
        agent = _CEO(persona, huddle)
        session = AgentSession(llm=GPTLiveModel(voice=voice, delegation="client"), vad=vad)
        session.on("user_state_changed", lambda ev: ev.new_state == "speaking" and huddle.on_speaker_activity())
        await session.start(agent=agent, room=ctx.room, room_options=room_io.RoomOptions(close_on_disconnect=False))
        return _Live(session, agent.duplex_session)  # type: ignore[arg-type]

    huddle = Huddle(room=_Room(ctx), backend=backend, live_factory=live_factory, ceo_name=meta["ceoName"])
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
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                    await huddle.on_final_transcript(p.identity, p.name or p.identity, ev.alternatives[0].text)
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
        emit_metric("huddle.stt.seconds", usage["stt"])
        emit_metric("huddle.agent.seconds", time.monotonic() - started)
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
    while True:
        await asyncio.sleep(1)
        await huddle.tick()


if __name__ == "__main__":
    cli.run_app(server)
