"""Owner mode in the app (one-brain plan, founder ruling 8 Nov 2026, option A): when a member goes Live in their DM
with the assistant (agent.py hands it every `mode: "owner"` dispatch), the voice is ears and a mouth for that DM's
Router, never a second brain.

Ears: speech-to-text (OpenAI's realtime transcription with semantic turn detection; the member can talk over the
voice). Each finished line is posted to the DM exactly as a typed message is (`/owner/{room}/turn`, via voice, wake
on), so the Router decides it like any other. The voice then says a fixed holding line.
Mouth: text-to-speech of each new agent message in the DM (`/owner/{room}/events`): the Router's ack, its replies
and its Workers' answers, word for word (mouth.py). The session has no language model and no tools, so nothing it
says can be its own answer. Approval is the one-tap card; a spoken "yes" is only a message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, StopResponse, room_io
from livekit.plugins import openai

from huddle_agent import end_after
from huddle_agent.backend import Backend, BackendError
from huddle_agent.mouth import Mouth
from huddle_agent.phone import clean_transcript

logger = logging.getLogger("owner")
OWNER_MAX_S = float(os.environ.get("OWNER_MAX_S", 55 * 60))  # a talk's cost cap
QUIET_S = float(os.environ.get("OWNER_QUIET_S", 15))  # mic live, the voice done, nobody spoke: close the mic
AWAY_S = float(os.environ.get("OWNER_AWAY_S", 5 * 60))  # no live mic this long: leave; the next tap rejoins
POLL_S = float(os.environ.get("OWNER_EVENTS_S", 1))  # how often the talk asks what the room's agents posted
HOLD = "One sec."  # said at the end of each spoken line; the answer is the Router's
GOODBYE = "We're out of time. Talk soon."


def voice_session() -> AgentSession:
    """Speech-to-text and text-to-speech, and no language model: with `llm` unset LiveKit never generates a reply
    (agent_activity: "skip response if no llm is set"); the voice says only `session.say` text. Semantic turns, low
    eagerness: owners pause mid-thought ("the invoice for... Acme"). The STT's turn end is the turn end."""
    return AgentSession(
        stt=openai.STT(model=os.environ.get("OWNER_STT_MODEL", "gpt-transcribe"), use_realtime=True,
                       turn_detection={"type": "semantic_vad", "eagerness": "low"}),  # fmt: skip
        tts=openai.TTS(model=os.environ.get("OWNER_TTS_MODEL", "gpt-4o-mini-tts"),
                       voice=os.environ.get("OWNER_VOICE", "marin")),  # fmt: skip
        turn_handling={"turn_detection": "stt", "endpointing": {"min_delay": 0.0}},
        user_away_timeout=None,  # Quiet below
    )


class Quiet:
    """Ends the talk on quiet, from LiveKit's agent and user states. Not `user_away_timeout`: in livekit-agents 1.8.11
    its timer starts once the member subscribes to the voice (agent_session._set_user_away_timer), mic or not, so it
    would end the pre-connected room, which publishes no mic, 15 s after it joins. Not OpenAI's `idle_timeout_ms`:
    it makes the model speak, it ends nothing. With the mic live, QUIET_S after the voice finished and nobody spoke
    calls `on_quiet` (the web closes the mic, the room stays warm); with no live mic, AWAY_S calls `on_away`."""

    def __init__(self, on_quiet, on_away, quiet_s: float = QUIET_S, away_s: float = AWAY_S) -> None:
        self._fire, self._s = {True: on_quiet, False: on_away}, {True: quiet_s, False: away_s}
        self._timer: asyncio.TimerHandle | None = None
        self._key: bool | None = None

    def update(self, mic_live: bool, user: str, agent: str) -> None:
        if user == "speaking" or agent != "listening":
            return self.cancel()
        if self._timer and self._key == mic_live:
            return  # already counting
        self.cancel()
        self._key = mic_live
        self._timer = asyncio.get_running_loop().call_later(self._s[mic_live], self._fire[mic_live])

    def cancel(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = self._key = None


class OwnerAgent(Agent):
    """No tools, no instructions to follow: each finished line goes to the DM, and the voice says the holding line."""

    def __init__(self, turns: "Turns") -> None:
        super().__init__(instructions="")
        self._turns = turns

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if text := (new_message.text_content or "").strip():
            self._turns.add(text)
            self.session.say(HOLD, add_to_chat_ctx=False)
        raise StopResponse()


class Turns:
    """Each spoken line posted to the DM in order (`/owner/{room}/turn`), so typed and spoken are one conversation.
    `n` numbers the lines: with the job id, each line's one message. A failed post is logged; the talk goes on."""

    def __init__(self, backend: Backend, speaker: str, session_id: str) -> None:
        self._b, self._speaker, self._sid = backend, speaker, session_id
        self._n = 0
        self._last: asyncio.Task | None = None
        self.said: list[dict] = []  # the member's lines, for the talk's record

    def add(self, text: str) -> None:
        self.said.append({"role": "user", "text": text, "t": time.time(), "interrupted": False})
        self._last = asyncio.create_task(self._post(self._last, self._n, text[:4000]))
        self._n += 1

    async def _post(self, prev: asyncio.Task | None, n: int, text: str) -> None:
        if prev:
            await asyncio.gather(prev, return_exceptions=True)  # in the order said
        try:
            await self._b.owner("turn", {"speakerUserId": self._speaker, "sessionId": self._sid, "n": n, "text": text})
        except BackendError as e:
            logger.warning("owner: turn %d not posted (%s)", n, e.code)

    async def flush(self) -> None:
        if self._last:
            await asyncio.gather(self._last, return_exceptions=True)


def talk_record(speaker: str, items: list[dict], session_id: str, seconds: float) -> dict:
    """What `/owner/{room}/end` keeps as the "Yours" row: the transcript as phone calls keep it."""
    return {"speakerUserId": speaker, "sessionId": session_id, "durationSec": int(seconds),
            "transcript": clean_transcript(items)}  # fmt: skip


async def run(ctx: JobContext, meta: dict) -> None:
    """The member's Live in the DM, from the dispatch until they leave, go quiet or the cap."""
    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    await ctx.connect()
    member = await ctx.wait_for_participant()  # the DM's one person; LiveKit set the identity from Clerk
    try:
        info = await backend.owner("start", {"speakerUserId": member.identity})
    except BackendError as e:
        logger.error("owner: refused room %s (%s)", meta["roomId"], e.code)
        return ctx.shutdown("refused")
    name, tile = info["assistantName"], info.get("agentId") or "assistant"
    turns = Turns(backend, member.identity, ctx.job.id)
    agent = OwnerAgent(turns)
    session = voice_session()
    items: list[dict] = []  # the voice's lines (the member's are turns.said)
    t0 = time.time()

    @session.on("conversation_item_added")
    def _item(ev) -> None:
        if ev.item.role == "assistant" and (text := getattr(ev.item, "text_content", None)):
            at = (ev.item.metrics or {}).get("started_speaking_at") or ev.item.created_at
            items.append({"role": "assistant", "text": text, "t": at - t0, "interrupted": ev.item.interrupted})

    attrs = ctx.room.local_participant.set_attributes

    @session.on("agent_state_changed")
    def _state(ev) -> None:
        status = {"speaking": "speaking", "thinking": "working"}.get(ev.new_state, "listening")
        asyncio.create_task(attrs({f"lightbulb.agent.{tile}.status": status}))
        recount()

    async def leave(reason: str) -> None:
        """The web leaves on idle_ended, so the next press joins again and dispatches a fresh session."""
        with contextlib.suppress(Exception):
            await ctx.room.local_participant.publish_data(
                json.dumps({"code": "idle_ended"}), reliable=True, topic="huddle.error"
            )
        ctx.shutdown(reason)

    def quiet() -> None:
        """The web closes the mic on quiet_ended; the room stays, so the next tap is instant. Tasks run on."""
        asyncio.create_task(ctx.room.local_participant.publish_data(
            json.dumps({"code": "quiet_ended"}), reliable=True, topic="huddle.error"))  # fmt: skip

    quiet_timer = Quiet(quiet, lambda: asyncio.create_task(leave("member quiet")))

    def recount(*_args) -> None:
        mic = any(p.kind == rtc.TrackKind.KIND_AUDIO and not p.muted for p in member.track_publications.values())
        quiet_timer.update(mic, session.user_state, session.agent_state)

    session.on("user_state_changed", recount)
    for event in ("track_published", "track_unpublished", "track_muted", "track_unmuted"):
        ctx.room.on(event, recount)

    async def fetch(after: int) -> dict:
        return await backend.owner("events", {"speakerUserId": member.identity, "after": after})

    async def speak_loop(mouth: Mouth) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            await mouth.poll()
            mouth.speak(session.say, session.user_state != "speaking")

    session.on("close", lambda _ev: ctx.shutdown("session closed"))
    limit = asyncio.create_task(end_after(OWNER_MAX_S, session, lambda: leave("owner max length"), line=GOODBYE))
    reports = asyncio.create_task(speak_loop(Mouth(fetch, int(info.get("after") or 0))))

    def record() -> dict:
        said = [{**line, "t": line["t"] - t0} for line in turns.said]
        return talk_record(member.identity, items + said, ctx.job.id, time.time() - t0)

    ctx.add_shutdown_callback(lambda _reason="": (quiet_timer.cancel(), _close(limit, backend, record, turns, reports))[1])
    # the web's Talk RPC (HuddleProvider.talkStart): nothing to summon here, the session already listens
    ctx.room.local_participant.register_rpc_method("talk_start", lambda _data: "ok")
    # a tap on the orb while the voice speaks: stop the line, keep the talk
    ctx.room.local_participant.register_rpc_method("interrupt", lambda _data: (session.interrupt(), "ok")[1])
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=member.identity,
            text_input=False,
            audio_output=room_io.AudioOutputOptions(track_name=f"agent-{tile}"),  # the strip's bars read it
        ),
        record=False,
    )
    await attrs({f"lightbulb.agent.{tile}.status": "listening", f"lightbulb.agent.{tile}.name": name})
    recount()
    logger.info("owner: listening in room %s as %s", meta["roomId"], name)


async def _close(limit: asyncio.Task, backend: Backend, record, turns: Turns, reports: asyncio.Task) -> None:
    limit.cancel()
    reports.cancel()
    try:
        await turns.flush()
        await backend.owner("end", record())  # the talk becomes a "Yours" row on Home
    except BackendError as e:
        logger.warning("owner: talk not kept (%s)", e.code)
    finally:
        await backend.aclose()
