"""The voice session every surface shares (orb, Meet, phone): OpenAI GPT-Live is the voice, the room's Router is the
brain (contract: docs/superpowers/specs/2026-11-08-gpt-live-contract.md, Task 0, 8 Nov 2026).

GPT-Live runs with client delegation and no tools (agent tools raise under client delegation, contract §1). It decides
on its own when a line needs the backend and fires `delegation_created`; `LiveSession.on_delegate` posts the ask to the
Router and returns at once. The Router's replies arrive as room messages (`events`) and go back to the model as
commentary (`send_result`), which it says in its own words: as the answer to the delegation whose posted message
they reply to (`inReplyTo`), else as an unsolicited team message. Turn-taking, the
acknowledgement and interruptions are the model's own: no client event can cancel or truncate a response, so nothing
here speaks, and an interruption never cancels a post."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from livekit.agents import Agent, AgentSession, JobContext, llm, room_io
from livekit.plugins.openai.realtime import GPTLiveModel

from huddle_agent.metrics import emit_metric
from huddle_agent.mouth import speakable

logger = logging.getLogger("live")

Surface = Literal["owner", "meet", "phone"]


@dataclass(frozen=True)
class Posted:
    message_id: str
    task_ids: tuple[str, ...] = ()


#: Posts one ask; returns its message id and, for an existing task, its task identity.
#: None means the post was refused or failed; nothing will answer that delegation.
Post = Callable[[str, list[str]], Awaitable[str | Posted | None]]
Spoken = Callable[[str], Awaitable[None]]
Events = Callable[[], AsyncIterator[dict]]

#: One append is capped at 500 tokens (contract §2); at ~4 characters a token this stays well under it.
APPEND_CHARS = 1600
EVENTS_RETRY_S, EVENTS_RETRY_MAX_S = (
    0.5,
    30.0,
)  # the events loop restarts after a failure, backing off
#: GPT-Live's startup history cap (contract §1; gpt_live.py:42-44): 128 items and 8192 tokens. The plugin drops items
#: past 128 but has no tokenizer, so the token cap is held here, at a conservative 3 characters a token.
HISTORY_ITEMS, HISTORY_CHARS = 128, 8192 * 3
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
- The answer needs facts about this business or workspace, including its description or what it does.
- A correction changes the work already requested.

Do not generate <|SpawnThinking|> when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Generate <|SpawnThinking|> before giving an answer that depends on backend work.
Business facts come from the backend. A business name or greeting is not evidence of its services, prices,
hours, policies or purpose. If no current backend result answers the question, delegate it before answering.
Do not guess the result while waiting."""


def startup_context(history: list[tuple[str, str]]) -> llm.ChatContext:
    """(speaker, text) pairs as the session's startup chat context: user items named by speaker, the newest that fit
    the startup cap (a reopened Meet session hears the recent transcript)."""
    kept: list[str] = []
    size = 0
    for speaker, text in reversed(history):
        if not (line := " ".join(f"{speaker}: {text}".split())) or not text.strip():
            continue
        if len(kept) >= HISTORY_ITEMS or size + len(line) > HISTORY_CHARS:
            break
        kept.append(line)
        size += len(line)
    ctx = llm.ChatContext.empty()
    for line in reversed(kept):
        ctx.add_message(role="user", content=line)
    return ctx


class Transport(Protocol):
    """What LiveSession needs from GPT-Live: the chat context, commentary, close. One real one (`_GPTLive`)."""

    billed_s: float

    def history(
        self,
    ) -> list[tuple[str, str, str]]: ...  # (item id, role, text), the chat context
    def commentary(self, text: str, delegation_id: str | None) -> None: ...
    def context(self, text: str) -> None: ...
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
        self,
        transport: Transport,
        surface: Surface,
        post: Post,
        events: Events,
        on_spoken: Spoken | None = None,
    ) -> None:
        self.transport, self.surface, self._post, self._on_spoken = (
            transport,
            surface,
            post,
            on_spoken,
        )
        self._waiting_tasks: dict[str, set[str]] = {}
        self.awaiting: dict[
            str, str | None
        ] = {}  # unfinished work; correlation survives its terminal reply
        self.pending: dict[
            str, str | None
        ] = {}  # delegation id -> its posted message id (None until posted)
        self._by_message: dict[
            str, str
        ] = {}  # posted message id -> delegation id: a reply's inReplyTo finds it
        self.billed_s = 0.0
        self.session: AgentSession | None = (
            None  # the real session, for the surface's own event hooks
        )
        self.agent: Agent | None = None
        self._closed = False
        self._seen: set[str] = set()  # chat-context item ids already sent with an ask
        self._last: asyncio.Task | None = (
            None  # the newest post: each waits for the one before it
        )
        self._posts: set[asyncio.Task] = set()
        self._said: set[str] = set()  # event message ids already sent as results
        self._events = asyncio.create_task(self._read(events))

    def ask(self, pending_transcript: str) -> tuple[str, list[str]]:
        """The ask and its context from the chat context plus `pending_transcript`, never the latter alone: a
        transcript lag splits a turn and leaves only " week" pending (contract §4, run 1). Items are tracked by id:
        the chat context inserts by created_at, so a count would skip or repeat one."""
        new = [
            (r, " ".join(t.split()))
            for i, r, t in self.transport.history()
            if i not in self._seen
        ]
        self._seen.update(i for i, _r, _t in self.transport.history())
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
        self.pending[d.id] = self.awaiting[d.id] = None
        self._last = task = asyncio.create_task(self._send(self._last, out))
        self._posts.add(task)
        task.add_done_callback(self._posts.discard)
        emit_metric("voice.live.delegated", 1, "Count", {"where": self.surface})
        return out

    async def _send(self, prev: asyncio.Task | None, d: Delegation) -> None:
        if prev:
            await asyncio.gather(prev, return_exceptions=True)
        try:
            posted = await self._post(d.text, d.turns)
            message_id = posted.message_id if isinstance(posted, Posted) else posted
            if isinstance(posted, Posted) and posted.task_ids:
                self._waiting_tasks[d.id] = set(posted.task_ids)
        except Exception:  # noqa: BLE001 -- a failed post is logged; the talk and the next post go on
            logger.warning(
                "live %s: delegation %s not posted", self.surface, d.id, exc_info=True
            )
            message_id = None
        if (
            d.id not in self.pending
        ):  # the session reconnected meanwhile: the new one never asked it
            return
        if (
            message_id is None
        ):  # refused or failed: nothing will answer it, so no team message is attached to it
            self.pending.pop(d.id)
            self.awaiting.pop(d.id, None)
        else:
            self.pending[d.id], self._by_message[message_id] = message_id, d.id
            self.awaiting[d.id] = message_id

    def send_result(self, delegation_id: str | None, text: str) -> None:
        """The team's text to the model as commentary, said once in its own words: the answer to `delegation_id`
        when it is pending, else (None, or one the model no longer has) an unsolicited team message. None never uses
        up a pending delegation: the phone's opening and goodbye go out this way while one waits. Several results
        may answer one delegation (an ack, then the answer): each is appended with its id."""
        if delegation_id not in self.pending:
            delegation_id = None
        # as it can be heard: links as labels, no URLs or code, a Choice card as its options, capped (idempotent)
        for chunk in _chunks(
            speakable(text)
        ):  # several appends with one id continue the same work
            self.transport.commentary(chunk, delegation_id)

    def send_context(self, text: str) -> None:
        """Silent context for a session already open: e.g. results fetched while a Meet voice was asleep. Thinking
        appends update GPT-Live without asking it to say old replies or answering a current delegation."""
        if not self._closed:
            for chunk in _chunks(text.strip()):
                self.transport.context(chunk)

    async def _read(self, events: Events) -> None:
        """Each new agent message in the room, once by its id, becomes a result: the answer to the delegation whose
        posted message it replies to (`inReplyTo`), else unsolicited. A failure restarts `events` after a capped
        backoff; it ends only when `events` does."""
        failures = 0
        while True:
            try:
                async for m in events():
                    failures = 0
                    await self._result(m)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the voice stays up; results are in the chat anyway
                failures += 1
                logger.warning(
                    "live %s: events failed (%d), retrying",
                    self.surface,
                    failures,
                    exc_info=True,
                )
                await asyncio.sleep(
                    min(EVENTS_RETRY_MAX_S, EVENTS_RETRY_S * 2 ** (failures - 1))
                )

    async def _result(self, m: dict) -> None:
        if self._last is not None and not self._last.done():
            await asyncio.shield(self._last)  # settle post receipts before correlating a fast task result
        mid, text = m.get("id"), (m.get("text") or "").strip()
        # /events items carry no kind: every one is an agent line
        if m.get("kind", "agent") != "agent" or not text or (mid and mid in self._said):
            return
        if mid:
            self._said.add(mid)
        delegation_id = self._by_message.get(m.get("inReplyTo") or "")
        task_ids = set(m.get("taskIds") or [])
        related = [key for key in self.awaiting if self._waiting_tasks.get(key, set()) & task_ids]
        if related:
            delegation_id = related[-1]  # a follow-up in the same task answers its newest spoken turn
        self.send_result(delegation_id, text)
        if delegation_id and m.get("waiting") is True:
            self._waiting_tasks.setdefault(delegation_id, set()).update(
                m.get("taskIds") or []
            )
        if m.get("waiting") is False:
            for key in set(related) | {delegation_id}:
                remaining = self._waiting_tasks.get(key, set())
                remaining.difference_update(task_ids)
                if not remaining:
                    self.awaiting.pop(key, None)
        if mid and self._on_spoken:
            try:
                await self._on_spoken(mid)
            except Exception:  # noqa: BLE001 -- the cursor is advisory: the next session may say it again
                logger.warning(
                    "live %s: spoken not reported", self.surface, exc_info=True
                )

    def reconnected(self, *_args) -> None:
        """A reconnect opens a new GPT-Live session, which never asked the old delegations: their results go out
        unsolicited."""
        self.pending.clear()
        self.awaiting.clear()
        self._waiting_tasks.clear()
        self._by_message.clear()

    def ended(self, *_args) -> None:
        """The AgentSession closed (the member left, an error): stop reading results and keep the usage so far."""
        self._closed = True
        self._events.cancel()
        self.billed_s = self.transport.billed_s

    async def close(self) -> None:
        """Lets posts in flight finish, then really ends the GPT-Live session (idle seconds are billed, contract §8)
        and keeps its final usage."""
        self._closed = True
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

    def history(self) -> list[tuple[str, str, str]]:
        return [(i.id, i.role, i.text_content or "") for i in self.agent.chat_ctx.items
                if i.type == "message" and i.role in ("user", "assistant")]  # fmt: skip

    def commentary(self, text: str, delegation_id: str | None) -> None:
        self.live.append_commentary(text, delegation_id=delegation_id)

    def context(self, text: str) -> None:
        self.live.append_thinking(text)

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
    on_spoken: Spoken | None = None,
    record: bool = False,
    user_away_timeout: float | None = None,
    history: list[tuple[str, str]] | None = None,
) -> LiveSession:
    """GPT-Live in `ctx.room` with `prompt` (OpenAI's live-prompting template; `delegation_policy` renders its
    delegation section), every delegation posted with `post(text, context_turns)`, every agent message from `events`
    sent back as a result. `room_options`: the surface's participant and track. `on_spoken(event id)`: called once a
    result was appended, so the surface reports it spoken to the backend (Mouth's `spoken` cursor). `record`: LiveKit's
    session recording (the phone kept it on). `user_away_timeout`: seconds of silence before LiveKit marks the user
    away (its `user_state_changed`); None: never. `history`: (speaker, text) pairs the session starts with
    (`startup_context`); never sent to the Router as an ask's context."""
    model = GPTLiveModel(delegation="client")
    # no tools: client delegation refuses them. The agent's chat context is the plugin's startup history (session.start)
    agent = Agent(
        instructions=prompt,
        **({"chat_ctx": startup_context(history)} if history else {}),
    )
    session = AgentSession(llm=model, vad=None, user_away_timeout=user_away_timeout)
    try:
        await session.start(
            agent=agent,
            room=ctx.room,
            record=record,
            **({"room_options": room_options} if room_options else {}),
        )
    except (Exception, asyncio.CancelledError):
        # Startup can fail after opening the paid model session. Close both resources even if one cleanup fails,
        # and preserve the original error/cancellation. No delegations are wired until startup succeeds.
        for resource in (session, model):
            try:
                await asyncio.wait_for(resource.aclose(), timeout=CLOSE_WAIT_S)
            except (Exception, asyncio.CancelledError):
                logger.warning(
                    "live %s: startup cleanup failed", surface, exc_info=True
                )
        raise
    live = LiveSession(
        _GPTLive(session, agent, model), surface, post, events, on_spoken
    )
    live.session, live.agent = session, agent
    live._seen.update(
        i for i, _r, _t in live.transport.history()
    )  # the startup history was never asked
    live.transport.live.on("delegation_created", live.on_delegate)
    live.transport.live.on("session_reconnected", live.reconnected)
    session.on("close", live.ended)
    logger.info("live %s: started", surface)
    return live


__all__ = ["APPEND_CHARS", "Delegation", "LiveSession", "delegation_policy", "start"]
