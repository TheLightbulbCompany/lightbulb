"""Owner mode in the app on GPT-Live (8 Nov 2026 plan, docs/superpowers/plans/2026-11-08-gpt-live-voice.md, Task 2;
contract docs/superpowers/specs/2026-11-08-gpt-live-contract.md). When a member goes Live in their DM with the
assistant (agent.py hands it every `mode: "owner"` dispatch), GPT-Live is the voice and the DM's Router the one brain.

GPT-Live answers small talk itself and delegates the rest (live.py, client delegation, no agent tools). Each
delegation is posted once to the DM as the member's message (`/owner/turn`, via voice, wake on), with the talk's turns
since the last ask, so it shows in the chat as it is said. The Router's and its Workers' messages in the DM come back
through `/events`, once each by message id, as results. Approval is the one-tap card; a spoken "yes" is only a
delegated message. GPT-Live bills every open second, silence too: a session is open only while the member's mic is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from collections.abc import AsyncIterator, Callable

from livekit import rtc
from livekit.agents import JobContext, room_io

from huddle_agent import live
from huddle_agent.backend import Backend, BackendError
from huddle_agent.phone import clean_transcript

logger = logging.getLogger("owner")
OWNER_MAX_S = float(os.environ.get("OWNER_MAX_S", 55 * 60))  # cost cap on one Live
QUIET_S = float(os.environ.get("OWNER_QUIET_S", 15))  # mic live, the voice done, nobody spoke: close the mic
AWAY_S = float(os.environ.get("OWNER_AWAY_S", 5 * 60))  # no live mic this long: leave; the next tap rejoins
POLL_S = float(os.environ.get("OWNER_EVENTS_S", 1))  # how often the talk asks what the room's agents posted
WAIT_S = 90.0  # after a delegation the talk stays open this long for its answer (Quiet does not count)
ANSWER_WITHIN_S = 60.0  # a line delegated this soon after a Worker's question came back answers it; later, a new ask

#: The Router's capabilities, as the contract's prompt (§3) lists them: GPT-Live delegates by this list.
ROUTER_TOOLS = [
    "The Router: the team's brain. It knows the team's work across GitHub, email, calendar and docs.",
    "Memory: the Router remembers and recalls what the team has said and decided.",
    "Work threads: the Router starts work threads that hand tasks to the team's agents.",
]


def prompt() -> str:
    """OpenAI's live-prompting template (developers.openai.com/api/docs/guides/live-prompting), as the contract ran it:
    style, delegation and the approval card only. No fact about the business: every fact comes from the Router."""
    return f"""You are Lightbulb, a calm, friendly voice assistant for this team, talking in the Lightbulb app.
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
If the user is frustrated, acknowledge it briefly and focus on the next helpful step.
Speak only English, whatever language a line seems to be in.

Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the user interrupts. Listen to what they say.

{live.delegation_policy(ROUTER_TOOLS)}

Approvals happen on the card in the chat, never by voice: when something needs approval, say it is waiting on the card.
A spoken yes approves nothing.
Wait for the backend's result before stating a fact, confirming an action or saying something is done."""


class Quiet:
    """Ends the talk on quiet, from LiveKit's agent and user states. Not `user_away_timeout`: in livekit-agents 1.8.11
    its timer starts once the member subscribes to the voice (agent_session._set_user_away_timer), mic or not, so it
    would end the pre-connected room, which publishes no mic, 15 s after it joins. Not OpenAI's `idle_timeout_ms`:
    it makes the model speak, it ends nothing. With the mic live, QUIET_S after the voice finished and nobody spoke
    calls `on_quiet` (the web closes the mic, the room stays warm); with no live mic, AWAY_S calls `on_away`."""

    def __init__(self, on_quiet, on_away, quiet_s: float = QUIET_S, away_s: float = AWAY_S) -> None:
        self._fire, self._s = (
            {True: on_quiet, False: on_away},
            {True: quiet_s, False: away_s},
        )
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


class Talk:
    """One Live in the DM: posts each delegation (`/owner/{room}/turn`) in order, so typed and spoken are one
    conversation, and yields the DM's agent messages (`/events`) once each. `n` numbers the posts: with the job id,
    each one's one message. A failed post is logged and the talk goes on. `items` is the talk's record."""

    def __init__(self, backend: Backend, speaker: str, session_id: str, after: int) -> None:
        self._b, self._speaker, self._sid = backend, speaker, session_id
        self.after = after  # the events cursor; kept across GPT-Live sessions in one room
        self._n, self._lock = (
            0,
            asyncio.Lock(),
        )  # asyncio.Lock wakes waiters in order: posts land as asked
        self.question: dict | None = None  # {taskId, qid}: the last Worker question handed to the voice
        self.question_at = 0.0
        self.asked: dict[str, float] = {}  # this room's voice-asked message ids -> when posted, kept while warm
        self.rewake: Callable[[], None] | None = None  # run's recount: once WAIT_S is up, Quiet counts again
        self.spoken_seq = after  # the last seq handed to the voice: reported, so the next Live starts there
        self._handing = after  # the seq of the message being handed over now
        self.items: list[dict] = []
        self.t0 = time.time()

    def waiting(self) -> bool:
        """An ask posted under WAIT_S ago: its answer may be on the way. A reply never ends the wait: the Router's
        "Started a thread" ack would let the talk close before the Worker answers."""
        now = time.monotonic()
        return any(now - t < WAIT_S for t in self.asked.values())

    async def post(self, text: str, turns: list[str]) -> str | None:
        """live.py's `post`: one delegation, the member's message. Returns its message id, or None if it failed."""
        n, self._n = self._n, self._n + 1
        q = self.question
        answers = q if q and time.monotonic() - self.question_at <= ANSWER_WITHIN_S else None
        said = [f"> {t}" for t in turns if t.strip() and t.partition(": ")[2] not in text]  # the ask once
        body = {"speakerUserId": self._speaker, "sessionId": self._sid, "n": n,
                "text": (text.strip() + ("\n\nSaid in the talk since the last ask:\n" + "\n".join(said) if said else ""))[:4000]}  # fmt: skip
        async with self._lock:
            try:
                out = await self._b.owner("turn", {**body, "answers": answers} if answers else body)
            except BackendError as e:
                logger.warning("owner: turn %d not posted (%s)", n, e.code)
                return None
        if answers is not None and self.question is q:
            self.question = None  # answered: the next line is a new ask
        if not (mid := out.get("messageId")):
            return None
        self.asked[str(mid)] = time.monotonic()
        if self.rewake:
            # no reply by then: Quiet re-arms (the lambda: a talk that ended meanwhile cleared `rewake`)
            asyncio.get_running_loop().call_later(WAIT_S + 0.1, lambda: self.rewake and self.rewake())
        return str(mid)

    async def spoken(self, event_id: str) -> None:
        """live.py's `on_spoken`: the voice was handed the message `event_id`."""
        self.spoken_seq = max(self.spoken_seq, self._handing)

    async def results(self) -> AsyncIterator[dict]:
        """live.py's `events`: each agent message the DM gets after the room's cursor, once, as
        {kind: "agent", id, seq, text[, inReplyTo, question]}. The cursor is the room's, across sessions: of what
        landed while the orb was closed, an answer to one of this room's spoken asks is heard on the next open; a
        typed reply stays in the chat, never read out later."""
        said: set[str] = set()
        carried = True  # the first poll: what landed before this session opened
        while True:
            try:
                out = await self._b.owner("events", {"speakerUserId": self._speaker, "after": self.after,
                                                     "spoken": self.spoken_seq})  # fmt: skip
            except BackendError as e:
                logger.warning("owner: events failed (%s)", e.code)
                out = {}
            for m in out.get("messages", []):
                if m["id"] in said:
                    continue
                said.add(m["id"])
                if carried and m.get("inReplyTo") not in self.asked:
                    continue
                if m.get("question"):
                    self.question, self.question_at = m["question"], time.monotonic()
                self._handing = int(m.get("seq") or 0)
                yield {"kind": "agent", **m}
            self.after, carried = max(self.after, int(out.get("after") or 0)), False
            await asyncio.sleep(POLL_S)


def talk_record(
    speaker: str,
    items: list[dict],
    session_id: str,
    seconds: float,
    *,
    usage_seconds: float | None = None,
    usage_started_at: float | None = None,
) -> dict:
    """What `/owner/{room}/end` keeps as the "Yours" row: the transcript as phone calls keep it."""
    out = {"speakerUserId": speaker, "sessionId": session_id, "durationSec": int(seconds),
           "transcript": clean_transcript(items)}  # fmt: skip
    if usage_seconds is not None:
        out["usageSec"] = int(max(0, usage_seconds))
        out["usageStartedAt"] = datetime.fromtimestamp(usage_started_at or time.time(), UTC).isoformat()
    return out


async def run(ctx: JobContext, meta: dict) -> None:
    """The member's Live in the DM, from the dispatch until they leave, go quiet or the cap. GPT-Live is open only
    while the mic is live (the orb open): the warm room costs nothing, and closing the orb ends the billed session."""

    backend = Backend(meta["roomId"], meta["ownerId"], meta["roomToken"])
    await ctx.connect()
    member = await ctx.wait_for_participant()  # the DM's one person; LiveKit set the identity from Clerk
    try:
        info = await backend.owner("start", {"speakerUserId": member.identity})
    except BackendError as e:
        logger.error("owner: refused room %s (%s)", meta["roomId"], e.code)
        return ctx.shutdown("refused")
    name, tile = info["assistantName"], info.get("agentId") or "assistant"
    talk = Talk(backend, member.identity, ctx.job.id, int(info.get("after") or 0))
    talk.rewake = lambda: recount()  # noqa: PLW0108 -- recount is defined below
    state: dict = {"open": None, "quiet": False, "ending": False}
    openings: dict[asyncio.Task, float] = {}
    closing: dict[asyncio.Task, asyncio.Task] = {}
    usage: list[tuple[float, float]] = []  # actual provider seconds, never the muted warm-room wall clock
    attrs = ctx.room.local_participant.set_attributes

    async def leave(reason: str) -> None:
        """The web leaves on idle_ended, so the next press joins again and dispatches a fresh session."""
        with contextlib.suppress(Exception):
            await ctx.room.local_participant.publish_data(
                json.dumps({"code": "idle_ended"}), reliable=True, topic="huddle.error"
            )
        ctx.shutdown(reason)

    def quiet() -> None:
        """GPT-Live closes now (idle seconds are billed) and the web closes the mic on quiet_ended; the room stays
        warm, and the next tap opens a new session. Tasks run on."""
        opening, state["open"], state["quiet"] = state["open"], None, True
        close_live(opening)
        asyncio.create_task(ctx.room.local_participant.publish_data(
            json.dumps({"code": "quiet_ended"}), reliable=True, topic="huddle.error"))  # fmt: skip

    quiet_timer = Quiet(quiet, lambda: asyncio.create_task(leave("member quiet")), QUIET_S, AWAY_S)

    def current():
        t = state["open"]
        return t.result() if t and t.done() and not t.cancelled() and not t.exception() else None

    def hooks(opening: asyncio.Task) -> None:
        """The live session's own events: the talk's record, the strip's status and the quiet count."""
        if opening.cancelled() or opening.exception() or not (session := opening.result().session):
            return

        @session.on("conversation_item_added")  # LiveKit's documented per-turn event: each final user/agent line
        def _item(ev) -> None:
            text = getattr(ev.item, "text_content", None)
            if text and ev.item.role in ("user", "assistant"):
                at = (ev.item.metrics or {}).get("started_speaking_at") or ev.item.created_at
                talk.items.append({"role": ev.item.role, "text": text, "t": at - talk.t0,
                                   "interrupted": ev.item.interrupted})  # fmt: skip

        @session.on("agent_state_changed")
        def _state(ev) -> None:
            status = {"speaking": "speaking", "thinking": "working"}.get(ev.new_state, "listening")
            asyncio.create_task(attrs({f"lightbulb.agent.{tile}.status": status}))
            recount()

        session.on("user_state_changed", recount)
        recount()

    async def shut(opening: asyncio.Task | None) -> None:
        if opening is None:
            return
        lp = ctx.room.local_participant  # this session's track, taken now: close() takes up to ~10 s, and a quick
        old = [p.sid for p in lp.track_publications.values() if p.name == f"agent-{tile}"]  # reopen publishes anew
        session = None
        try:
            session = await opening
            await session.close()
        except Exception:  # noqa: BLE001 -- a failed start or close: nothing left open to bill
            logger.warning("owner: live session not closed cleanly", exc_info=True)
        finally:
            if session is not None:
                seconds = getattr(
                    getattr(session, "transport", None),
                    "billed_s",
                    getattr(session, "billed_s", 0.0),
                )
                usage.append((openings[opening], float(seconds or 0)))
        for sid in old:  # the closed session's track goes, so the strip finds the next one's
            with contextlib.suppress(Exception):
                await lp.unpublish_track(sid)

    def close_live(opening: asyncio.Task | None) -> None:
        if opening is not None and opening not in closing:
            closing[opening] = asyncio.create_task(shut(opening))

    def recount(*_args) -> None:
        if state["ending"]:
            return
        mic = any(p.kind == rtc.TrackKind.KIND_AUDIO and not p.muted for p in member.track_publications.values())
        state["quiet"] = state["quiet"] and mic  # after quiet, a new session only once the mic is off and on again
        if mic and state["open"] is None and not state["quiet"]:
            state["open"] = asyncio.create_task(live.start(
                ctx, prompt=prompt(), surface="owner", post=talk.post, events=talk.results, on_spoken=talk.spoken,
                room_options=room_io.RoomOptions(
                    participant_identity=member.identity,
                    text_input=False,
                    audio_output=room_io.AudioOutputOptions(track_name=f"agent-{tile}"),  # the strip's bars read it
                ),
            ))  # fmt: skip
            openings[state["open"]] = time.time()
            state["open"].add_done_callback(hooks)
        elif not mic and state["open"] is not None:
            opening, state["open"] = state["open"], None
            close_live(opening)
        s = current()
        session = s.session if s else None
        user, agent = (session.user_state, session.agent_state) if session else ("listening", "listening")
        quiet_timer.update(mic, user, "thinking" if talk.waiting() else agent)  # an answer on its way: stay open

    for event in (
        "track_published",
        "track_unpublished",
        "track_muted",
        "track_unmuted",
    ):
        ctx.room.on(event, recount)
    limit = asyncio.create_task(asyncio.sleep(OWNER_MAX_S))
    limit.add_done_callback(lambda t: t.cancelled() or asyncio.create_task(leave("owner max length")))

    async def close() -> None:
        if state["ending"]:
            return
        state["ending"] = True
        talk.rewake = None
        limit.cancel()
        quiet_timer.cancel()
        close_live(state["open"])
        state["open"] = None
        if closing:
            await asyncio.gather(*closing.values())
        billed = sum(seconds for _, seconds in usage)
        first_open = min((at for at, seconds in usage if seconds > 0), default=talk.t0)
        try:
            await backend.owner(
                "end",
                talk_record(
                    member.identity,
                    talk.items,
                    ctx.job.id,
                    time.time() - talk.t0,
                    usage_seconds=billed,
                    usage_started_at=first_open,
                ),
            )
        except BackendError as e:
            logger.warning("owner: talk not kept (%s)", e.code)
        finally:
            await backend.aclose()

    ctx.add_shutdown_callback(lambda _reason="": close())
    # the web's RPCs (HuddleProvider): the session already listens, and GPT-Live stops itself when talked over
    ctx.room.local_participant.register_rpc_method("talk_start", lambda _data: "ok")
    ctx.room.local_participant.register_rpc_method("interrupt", lambda _data: "ok")
    await attrs(
        {
            f"lightbulb.agent.{tile}.status": "listening",
            f"lightbulb.agent.{tile}.name": name,
        }
    )
    recount()
    logger.info("owner: listening in room %s as %s", meta["roomId"], name)
