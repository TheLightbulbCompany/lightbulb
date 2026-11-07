"""Owner mode in the app (voice-agents spec §4.1b, §4.5, §5 Home): ONE realtime session -- the Router -- when a
member goes Live in their DM with the assistant (agent.py hands it every `mode: "owner"` dispatch).

It listens to that one member (the hold-to-talk orb mutes the mic between turns; the model's own turn detection
ends each turn), answers reads itself through the backend in a few seconds, and hands anything open-ended to a
Worker with start_work without waiting: the result lands in the DM as a task. The member has full authority, as
when they type in the DM; the backend checks who is speaking (apps/backend/routers/voice_owner.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from livekit.agents import Agent, AgentSession, JobContext, function_tool, room_io
from livekit.plugins import google, openai

from huddle_agent import end_after
from huddle_agent.backend import Backend, BackendError

logger = logging.getLogger("owner")
OWNER_MAX_S = float(os.environ.get("OWNER_MAX_S", 20 * 60))  # cost cap, like a phone call
AWAY_S = float(os.environ.get("OWNER_AWAY_S", 5 * 60))  # nobody spoke this long: leave; the next press rejoins

INSTRUCTIONS = """You are {name}, the AI assistant of this business, talking with {who}, a member, in the Lightbulb
app. Today is {today}. Answer in one or two short spoken sentences. For anything that can change -- their
calendar, mail, documents, payments, issues, calls, bookings, what the team decided -- call a read tool first and
say only what it returns; if it has nothing, say you don't know. The workspace's connected calendar, mail and Drive
(the account the workspace connected, which may not be {who}'s own): read_integration with provider calendar,
gmail or drive (say "one moment" first). Other connected apps: list_integrations, then
read_integration. Writing anything (a draft, a post, an email), anything open-ended, anything that sends, spends
or publishes, and anything a read says needs more: call start_work with a short title and a self-contained brief,
then say you're on it and that the result will be in your chat with them. Never wait for it, never do it yourself,
never say it is done. If a tool returns an error, say plainly that you couldn't do it."""


def realtime_model():
    """OWNER_ENGINE=gemini runs the spec's Gemini Live; by default gpt-realtime (the key the prod worker has)."""
    if os.environ.get("OWNER_ENGINE", "openai") == "gemini":
        from google.genai import types

        return google.realtime.RealtimeModel(
            model=os.environ.get("OWNER_MODEL", "gemini-3.8-live"),
            voice=os.environ.get("OWNER_VOICE", "Kore"),
            api_key=os.environ["GOOGLE_API_KEY"],
            api_version="v1beta",
            session_resumption=types.SessionResumptionConfig(),  # outlives Gemini's 10-minute connection
        )
    return openai.realtime.RealtimeModel(
        model=os.environ.get("OWNER_MODEL", "gpt-realtime"), voice=os.environ.get("OWNER_VOICE", "marin")
    )


class OwnerAgent(Agent):
    def __init__(self, backend: Backend, speaker: str, info: dict) -> None:
        super().__init__(instructions=INSTRUCTIONS.format(**info))
        self._b, self._speaker = backend, speaker

    async def _post(self, op: str, body: dict) -> dict:
        try:
            out = await self._b.owner(op, {**body, "speakerUserId": self._speaker})
        except BackendError as e:
            out = {"error": e.code}
        logger.info("owner: tool %s %s", op, out.get("error") or "ok")
        return out

    @function_tool
    async def recall_memory(self, question: str) -> dict:
        """What the team knows: decisions, facts, past conversations. Ask a plain question."""
        out = await self._post("read", {"kind": "memory", "question": question})
        if "facts" in out:
            out["rule"] = "Say only what one of these facts states. If none answers the question, say you don't know."
        return out

    @function_tool
    async def call_stats(self, days: int = 7) -> dict:
        """How many phone calls the assistant answered in the last `days` (1 = today), by outcome."""
        return await self._post("read", {"kind": "calls", "days": days})

    @function_tool
    async def calendar(self, date: str = "") -> dict:
        """Bookings the assistant made for customers. date: "YYYY-MM-DD", or "" for all upcoming. Not the
        workspace's connected calendar: that is read_integration("calendar", ...)."""
        return await self._post("read", {"kind": "calendar", "date": date})

    @function_tool
    async def list_integrations(self) -> dict:
        """The connected apps you can read (gmail, calendar, drive, stripe, notion, linear, github, ...)."""
        return await self._post("integrations", {})

    @function_tool
    async def read_integration(self, provider: str, query: str) -> dict:
        """One quick read of a connected app: provider is a name from list_integrations; query is what to search
        for (mail search words, an event name, an issue keyword; "" for the next events). Bigger: start_work."""
        return await self._post("integration-read", {"provider": provider, "query": query})

    @function_tool
    async def start_work(self, title: str, brief: str) -> dict:
        """Hand open-ended work to the team: a short title and a self-contained brief. Don't wait for it; say
        you're on it and that the result will be in your chat."""
        out = await self._post("start-work", {"title": title, "brief": brief})
        if out.get("already"):
            out["say"] = "already on it"
        return out


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
    agent = OwnerAgent(backend, member.identity, {
        "name": name, "who": member.name or "the owner", "today": time.strftime("%A %Y-%m-%d"),
    })  # fmt: skip
    session = AgentSession(llm=realtime_model(), user_away_timeout=AWAY_S)
    attrs = ctx.room.local_participant.set_attributes

    @session.on("agent_state_changed")
    def _state(ev) -> None:
        status = {"speaking": "speaking", "thinking": "working"}.get(ev.new_state, "listening")
        asyncio.create_task(attrs({f"lightbulb.agent.{tile}.status": status}))

    async def leave(reason: str) -> None:
        """The web leaves on idle_ended, so the next press joins again and dispatches a fresh session."""
        with contextlib.suppress(Exception):
            await ctx.room.local_participant.publish_data(
                json.dumps({"code": "idle_ended"}), reliable=True, topic="huddle.error"
            )
        ctx.shutdown(reason)

    @session.on("user_state_changed")
    def _away(ev) -> None:
        if ev.new_state == "away":
            asyncio.create_task(leave("member quiet"))

    session.on("close", lambda _ev: ctx.shutdown("session closed"))
    limit = asyncio.create_task(end_after(OWNER_MAX_S, session, lambda: leave("owner max length")))
    ctx.add_shutdown_callback(lambda _reason="": _close(limit, backend))
    # the web's Talk RPC (HuddleProvider.talkStart): nothing to summon here, the session already listens
    ctx.room.local_participant.register_rpc_method("talk_start", lambda _data: "ok")
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
    logger.info("owner: listening in room %s as %s", meta["roomId"], name)


async def _close(limit: asyncio.Task, backend: Backend) -> None:
    limit.cancel()
    await backend.aclose()
