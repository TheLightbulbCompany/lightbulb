"""The voice session every surface shares (orb, Meet, phone): OpenAI GPT-Live is the voice, the room's Router is the
brain (contract: docs/superpowers/specs/2026-11-08-gpt-live-contract.md, Task 0, 8 Nov 2026).

GPT-Live runs with client delegation and no tools (agent tools raise under client delegation, contract §1). It decides
on its own when a line needs the backend and fires `delegation_created`; `LiveSession.on_delegate` posts the ask to the
Router and returns at once. The Router's replies arrive as room messages (`events`) and go back to the model as
commentary for the oldest pending delegation (`send_result`), which it says in its own words. Turn-taking, the
acknowledgement and interruptions are the model's own: no client event can cancel or truncate a response, so nothing
here speaks, and an interruption never cancels a post."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from livekit.agents import Agent, AgentSession, JobContext, room_io
from livekit.plugins.openai.realtime import GPTLiveModel

from huddle_agent.metrics import emit_metric

logger = logging.getLogger("live")

Surface = Literal["owner", "meet", "phone"]
Post = Callable[[str, list[str]], Awaitable[str]]
Events = Callable[[], AsyncIterator[dict]]

#: One append is capped at 500 tokens (contract §2); at ~4 characters a token this stays well under it.
APPEND_CHARS = 1600
CLOSE_WAIT_S = 10.0  # how long close() lets posts in flight finish: each is a request the member made


def delegation_policy(backend_tools: list[str]) -> str:
    """The `Delegation policy` section of OpenAI's live-prompting template (developers.openai.com/api/docs/guides/
    live-prompting), with only the backend's capabilities filled in; the surface supplies them."""
    tools = "\n".join(f"- {t}" for t in backend_tools)
    return f"""Delegation policy:
Backend tools:
{tools}

Generate <|SpawnThinking|> when:
- The request needs a backend capability or careful reasoning.
- A correction changes the work already requested.

Do not generate <|SpawnThinking|> when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Generate <|SpawnThinking|> before giving an answer that depends on backend work.
Do not guess the result while waiting."""


class Transport(Protocol):
    """What LiveSession needs from GPT-Live: the chat context, commentary, close. One real one (`_GPTLive`)."""

    billed_s: float

    def history(self) -> list[tuple[str, str]]: ...
    def commentary(self, text: str, delegation_id: str | None) -> None: ...
    async def close(self) -> None: ...


@dataclass
class Delegation:
    id: str
    text: str  # the ask: the caller's words since the voice last spoke, plus the pending transcript
    turns: list[str]  # every turn since the previous delegation, "role: text"


def _chunks(text: str) -> list[str]:
    """`text` in pieces under APPEND_CHARS, split after a sentence (or a space) where it can be."""
    out = []
    while len(text) > APPEND_CHARS:
        cut = max(
            text.rfind(". ", 0, APPEND_CHARS),
            text.rfind("? ", 0, APPEND_CHARS),
            text.rfind("! ", 0, APPEND_CHARS),
        )
        cut = (
            cut + 1
            if cut > 0
            else (
                text.rfind(" ", 0, APPEND_CHARS)
                if " " in text[:APPEND_CHARS]
                else APPEND_CHARS
            )
        )
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return [*out, text] if text else out


class LiveSession:
    def __init__(
        self, transport: Transport, surface: Surface, post: Post, events: Events
    ) -> None:
        self.transport, self.surface, self._post = transport, surface, post
        self.pending: list[
            str
        ] = []  # delegation ids still waiting for a result, oldest first
        self.billed_s = 0.0
        self.session: AgentSession | None = (
            None  # the real session, for the surface's own event hooks
        )
        self.agent: Agent | None = None
        self._seen = 0  # chat-context items already sent with an ask
        self._last: asyncio.Task | None = (
            None  # the newest post: each waits for the one before it
        )
        self._posts: set[asyncio.Task] = set()
        self._said: set[str] = set()  # event message ids already sent as results
        self._events = asyncio.create_task(self._read(events))

    def ask(self, pending_transcript: str) -> tuple[str, list[str]]:
        """The ask and its context from the chat context plus `pending_transcript`, never the latter alone: a
        transcript lag splits a turn and leaves only " week" pending (contract §4, run 1)."""
        items = [(r, " ".join(t.split())) for r, t in self.transport.history()]
        new, self._seen = items[self._seen :], len(items)
        turns = [f"{r}: {t}" for r, t in new if t]
        said: list[str] = []
        for role, text in reversed(new):
            if role != "user":
                break
            said.insert(0, text)
        tail = " ".join(pending_transcript.split())
        if tail and not " ".join(said).endswith(tail):
            said.append(tail)
        return " ".join(t for t in said if t), turns

    def on_delegate(self, d) -> Delegation:
        """One `delegation_created` (a GPTLiveDelegation: id, pending_transcript): posted to the Router, in the order
        asked, as a task of its own; returns at once. Nothing cancels the post: an interruption stops only speech."""
        text, turns = self.ask(d.pending_transcript)
        out = Delegation(d.id, text, turns)
        self.pending.append(d.id)
        self._last = task = asyncio.create_task(self._send(self._last, out))
        self._posts.add(task)
        task.add_done_callback(self._posts.discard)
        emit_metric("voice.live.delegated", 1, "Count", {"where": self.surface})
        return out

    async def _send(self, prev: asyncio.Task | None, d: Delegation) -> None:
        if prev:
            await asyncio.gather(prev, return_exceptions=True)
        try:
            await self._post(d.text, d.turns)
        except Exception:  # noqa: BLE001 -- a failed post is logged; the talk and the next post go on
            logger.warning(
                "live %s: delegation %s not posted", self.surface, d.id, exc_info=True
            )

    def send_result(self, delegation_id: str | None, text: str) -> None:
        """The team's text to the model as commentary, said once in its own words. `delegation_id` None: the oldest
        pending delegation, or an unsolicited team message when none is pending."""
        if delegation_id in self.pending:
            self.pending.remove(delegation_id)
        elif delegation_id is None and self.pending:
            delegation_id = self.pending.pop(0)
        for chunk in _chunks(
            " ".join(text.split())
        ):  # several appends with one id continue the same work
            self.transport.commentary(chunk, delegation_id)

    async def _read(self, events: Events) -> None:
        """Each new agent message in the room, once by its id, becomes a result."""
        try:
            async for m in events():
                mid = m.get("id")
                if (
                    m.get("kind") != "agent"
                    or not (m.get("text") or "").strip()
                    or (mid and mid in self._said)
                ):
                    continue
                if mid:
                    self._said.add(mid)
                self.send_result(m.get("delegation_id"), m["text"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the voice stays up; results are in the chat anyway
            logger.warning("live %s: events stopped", self.surface, exc_info=True)

    async def close(self) -> None:
        """Lets posts in flight finish, then really ends the GPT-Live session (idle seconds are billed, contract §8)
        and keeps its final usage."""
        self._events.cancel()
        if self._posts:
            await asyncio.wait(set(self._posts), timeout=CLOSE_WAIT_S)
        with contextlib.suppress(asyncio.CancelledError):
            await self._events
        await self.transport.close()
        self.billed_s = self.transport.billed_s
        emit_metric(
            "voice.live.billed_s", self.billed_s, "Seconds", {"where": self.surface}
        )


class _GPTLive:
    """The one adapter over the plugin (livekit-plugins-openai 1.8.11): the agent's chat context, `append_commentary`
    on its duplex session, and usage from its `metrics_collected`."""

    def __init__(
        self, session: AgentSession, agent: Agent, model: GPTLiveModel
    ) -> None:
        self.session, self.agent, self.model, self.billed_s = session, agent, model, 0.0
        self.live = agent.duplex_session
        self.live.on("metrics_collected", self._usage)

    def _usage(self, m) -> None:
        self.billed_s += (
            getattr(m, "session_duration", 0.0) or 0.0
        )  # deltas of the session's cumulative seconds

    def history(self) -> list[tuple[str, str]]:
        return [(i.role, i.text_content or "") for i in self.agent.chat_ctx.items
                if i.type == "message" and i.role in ("user", "assistant")]  # fmt: skip

    def commentary(self, text: str, delegation_id: str | None) -> None:
        self.live.append_commentary(text, delegation_id=delegation_id)

    async def close(self) -> None:
        await (
            self.session.aclose()
        )  # sends session.close and waits for session.closed with the final usage
        await self.model.aclose()


async def start(
    ctx: JobContext,
    *,
    prompt: str,
    surface: Surface,
    post: Post,
    events: Events,
    room_options: room_io.RoomOptions | None = None,
) -> LiveSession:
    """GPT-Live in `ctx.room` with `prompt` (OpenAI's live-prompting template; `delegation_policy` renders its
    delegation section), every delegation posted with `post(text, context_turns)`, every agent message from `events`
    sent back as a result. `room_options`: the surface's participant and track (optional)."""
    model = GPTLiveModel(delegation="client")
    agent = Agent(instructions=prompt)  # no tools: client delegation refuses them
    session = AgentSession(llm=model, vad=None, user_away_timeout=None)
    await session.start(
        agent=agent,
        room=ctx.room,
        record=False,
        **({"room_options": room_options} if room_options else {}),
    )
    live = LiveSession(_GPTLive(session, agent, model), surface, post, events)
    live.session, live.agent = session, agent
    live.transport.live.on("delegation_created", live.on_delegate)
    logger.info("live %s: started", surface)
    return live


__all__ = ["APPEND_CHARS", "Delegation", "LiveSession", "delegation_policy", "start"]
