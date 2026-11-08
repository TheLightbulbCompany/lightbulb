"""Owner mode in the app (founder ruling 8 Nov 2026, shape 1: talker plus delegated brain, on the DeepSeek one-Router
design; super-agent-design/evidence/voice/voice-brain-architecture-research.md, patterns 1 and 3). When a member goes
Live in their DM with the assistant (agent.py hands it every `mode: "owner"` dispatch), a realtime talker owns the
spoken conversation and the DM's Router is the one brain.

The talker has one tool, `delegate`: every substantive request goes to the DM as the member's message (`/owner/turn`,
via voice, wake on), with the talk's turns since the last delegation, and the talker says a short holding line. It
answers only greetings, thanks and "are you there" itself. The Router's and its Workers' messages in the DM come back
through `/events`, and the talker says each once, briefly, adding nothing (mouth.py). Approval is the one-tap card;
a spoken "yes" is only a delegated message. Interrupting stops the speech, never the post.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, function_tool, room_io
from livekit.plugins import openai
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad

from huddle_agent import end_after
from huddle_agent.backend import Backend, BackendError
from huddle_agent.mouth import DELEGATE_DOC, Context, Mouth, check_said, result_instructions
from huddle_agent.phone import clean_transcript

logger = logging.getLogger("owner")
OWNER_MAX_S = float(os.environ.get("OWNER_MAX_S", 55 * 60))  # under OpenAI's 60-min realtime session cap
QUIET_S = float(os.environ.get("OWNER_QUIET_S", 15))  # mic live, the voice done, nobody spoke: close the mic
AWAY_S = float(os.environ.get("OWNER_AWAY_S", 5 * 60))  # no live mic this long: leave; the next tap rejoins
POLL_S = float(os.environ.get("OWNER_EVENTS_S", 1))  # how often the talk asks what the room's agents posted
WAIT_S = 90.0  # after a delegation the talk stays open this long for its answer (Quiet does not count)

INSTRUCTIONS = """You are {name}, the voice of this business's AI assistant, talking with {who} in the Lightbulb app.
Today is {today}. You own only the conversation; the team behind you owns every fact, tool and decision.
- Greetings, thanks, "are you there?", "can you hear me?": answer yourself, in a few words.
- Everything else -- any question, request, correction, a yes or a no, "stop that thread" -- call delegate with what
  they asked in their own words, then say one short natural holding line ("Let me check.") and stop. Never answer it
  yourself, never guess, never say anything is done or approved. You cannot approve anything: a yes is delegated.
- When a message from the team arrives, say it as you are told, adding nothing.
Speak briefly and naturally, one or two short sentences."""


def realtime_model():
    """gpt-realtime-2.1-mini (gpt-realtime and -mini shut down 20 Jan 2027, developers.openai.com/api/docs/deprecations).
    Semantic turns, low eagerness: owners pause mid-thought ("the invoice for... Acme"); silence-only VAD cuts them
    off. interrupt_response: talking over the voice stops it (developers.openai.com/api/docs/guides/realtime-vad)."""
    return openai.realtime.RealtimeModel(
        model=os.environ.get("OWNER_MODEL", "gpt-realtime-2.1-mini"), voice=os.environ.get("OWNER_VOICE", "marin"),
        turn_detection=SemanticVad(type="semantic_vad", eagerness="low", create_response=True, interrupt_response=True),
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
    """The talker: no tool but `delegate`."""

    def __init__(self, turns: "Turns", info: dict) -> None:
        super().__init__(instructions=INSTRUCTIONS.format(**info))
        self._turns = turns
        self.context = Context()
        self.delegated_at = 0.0  # when the last request went to the Router (the talk waits for its answer)

    @function_tool(description=DELEGATE_DOC)
    async def delegate(self, request: str) -> dict:
        # a task of its own: an interruption cancels the talker's reply, never the post
        self._turns.add(self.context.take(request))
        self.delegated_at = time.monotonic()
        return {"sent": True}


class Turns:
    """Each delegated request posted to the DM in order (`/owner/{room}/turn`), so typed and spoken are one
    conversation. `n` numbers them: with the job id, each one's one message. A failed post is logged; the talk goes
    on (the web's bubble clears on its own)."""

    def __init__(self, backend: Backend, speaker: str, session_id: str) -> None:
        self._b, self._speaker, self._sid = backend, speaker, session_id
        self._n = 0
        self._last: asyncio.Task | None = None

    def add(self, text: str) -> None:
        self._last = asyncio.create_task(self._post(self._last, self._n, text[:4000]))
        self._n += 1

    async def _post(self, prev: asyncio.Task | None, n: int, text: str) -> None:
        if prev:
            await asyncio.gather(prev, return_exceptions=True)  # in the order asked
        try:
            await self._b.owner("turn", {"speakerUserId": self._speaker, "sessionId": self._sid, "n": n, "text": text})
        except BackendError as e:
            logger.warning("owner: turn %d not posted (%s)", n, e.code)

    async def flush(self) -> None:
        if self._last:
            await asyncio.gather(self._last, return_exceptions=True)


def interrupt(session) -> str:
    """A tap on the orb while the voice speaks: the speech stops; delegated work runs on."""
    session.interrupt()
    return "ok"


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
    name, tile, who = info["assistantName"], info.get("agentId") or "assistant", member.name or "the owner"
    turns = Turns(backend, member.identity, ctx.job.id)
    agent = OwnerAgent(turns, {"name": name, "who": who, "today": time.strftime("%A %Y-%m-%d")})
    session = AgentSession(llm=realtime_model(), user_away_timeout=None)  # Quiet below
    items: list[dict] = []
    t0 = time.time()
    talk = {"live": False}  # the mic is open: only then is anything said

    @session.on("conversation_item_added")  # LiveKit's documented per-turn event: each final user/agent line
    def _item(ev) -> None:
        if (text := getattr(ev.item, "text_content", None)) and ev.item.role in ("user", "assistant"):
            at = (ev.item.metrics or {}).get("started_speaking_at") or ev.item.created_at
            items.append({"role": ev.item.role, "text": text, "t": at - t0, "interrupted": ev.item.interrupted})
            if not ev.item.interrupted:
                agent.context.add(who if ev.item.role == "user" else name, text)

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
        talk["live"] = any(p.kind == rtc.TrackKind.KIND_AUDIO and not p.muted for p in member.track_publications.values())
        waiting = time.monotonic() - agent.delegated_at < WAIT_S  # an answer is on its way: keep the talk open
        quiet_timer.update(talk["live"], session.user_state, "thinking" if waiting else session.agent_state)

    session.on("user_state_changed", recount)
    for event in ("track_published", "track_unpublished", "track_muted", "track_unmuted"):
        ctx.room.on(event, recount)

    async def fetch(after: int, handled: int) -> dict:
        return await backend.owner("events", {"speakerUserId": member.identity, "after": after, "spoken": handled})

    def say(texts: list[str]) -> None:
        agent.delegated_at = 0.0  # the answer came: Quiet counts again
        speech = session.generate_reply(instructions=result_instructions(texts))
        speech.add_done_callback(lambda h: check_said(
            " ".join(t for i in h.chat_items if getattr(i, "role", None) == "assistant"
                     and (t := getattr(i, "text_content", None))), texts, (name, who), "owner"))  # fmt: skip

    async def speak_loop(mouth: Mouth) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            await mouth.poll()
            recount()
            idle = session.user_state != "speaking" and session.agent_state not in ("speaking", "thinking")
            mouth.speak(say, ready=idle, live=talk["live"])

    session.on("close", lambda _ev: ctx.shutdown("session closed"))
    limit = asyncio.create_task(end_after(OWNER_MAX_S, session, lambda: leave("owner max length")))
    reports = asyncio.create_task(speak_loop(Mouth(fetch, int(info.get("after") or 0))))
    record = lambda: talk_record(member.identity, items, ctx.job.id, time.time() - t0)  # noqa: E731
    ctx.add_shutdown_callback(lambda _reason="": (quiet_timer.cancel(), _close(limit, backend, record, turns, reports))[1])
    # the web's Talk RPC (HuddleProvider.talkStart): nothing to summon here, the session already listens
    ctx.room.local_participant.register_rpc_method("talk_start", lambda _data: "ok")
    ctx.room.local_participant.register_rpc_method("interrupt", lambda _data: interrupt(session))
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
