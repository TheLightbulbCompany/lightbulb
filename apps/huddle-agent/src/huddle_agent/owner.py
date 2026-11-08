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
from collections.abc import AsyncIterator

from livekit import rtc
from livekit.agents import JobContext, room_io

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
    from huddle_agent import live  # noqa: PLC0415 -- Task 1's module; imported where used so tests stand it in

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


class Talk:
    """One Live in the DM: posts each delegation (`/owner/{room}/turn`) in order, so typed and spoken are one
    conversation, and yields the DM's agent messages (`/events`) once each. `n` numbers the posts: with the job id,
    each one's one message. A failed post is logged and the talk goes on. `items` is the talk's record."""

    def __init__(self, backend: Backend, speaker: str, session_id: str, after: int) -> None:
        self._b, self._speaker, self._sid = backend, speaker, session_id
        self.after = after  # the events cursor; kept across GPT-Live sessions in one room
        self._n, self._lock = 0, asyncio.Lock()  # asyncio.Lock wakes waiters in order: posts land as asked
        self.question: dict | None = None  # {taskId, qid}: the last Worker question handed to the voice
        self.question_at = 0.0
        self.asked_at = 0.0  # when the last delegation was posted: the talk waits WAIT_S for its answer
        self.spoken_seq = after  # the last seq said or passed over: reported, so the next session starts there
        self._unsaid: dict[str, int] = {}  # message id -> seq, handed to the voice but not yet said
        self.items: list[dict] = []
        self.t0 = time.time()

    async def post(self, text: str, turns: list[str]) -> str:
        """live.py's `post`: one delegation, the member's message. Returns its message id ("" if it failed)."""
        n, self._n = self._n, self._n + 1
        q, self.question = self.question, None
        answers = q if q and time.monotonic() - self.question_at <= ANSWER_WITHIN_S else None
        said = [f"> {t}" for t in turns if t.strip() and t.partition(": ")[2] not in text]  # the ask once
        body = {"speakerUserId": self._speaker, "sessionId": self._sid, "n": n,
                "text": (text.strip() + ("\n\nSaid in the talk since the last ask:\n" + "\n".join(said) if said else ""))[:4000]}  # fmt: skip
        self.asked_at = time.monotonic()
        async with self._lock:
            try:
                out = await self._b.owner("turn", {**body, "answers": answers} if answers else body)
            except BackendError as e:
                logger.warning("owner: turn %d not posted (%s)", n, e.code)
                return ""
        return str(out.get("messageId") or "")

    def spoken(self, event_id: str) -> None:
        """live.py's `on_spoken`: the voice said the message `event_id`."""
        if (seq := self._unsaid.pop(event_id, None)) is not None:
            self.spoken_seq = self.after if not self._unsaid else max(self.spoken_seq, seq)

    async def results(self) -> AsyncIterator[dict]:
        """live.py's `events`: each agent message the DM gets while this session is open, once, as
        {kind: "agent", id, seq, text[, question]}. What landed before the session opened is passed over, as typed
        replies were (a reply read in the chat is never read out)."""
        said: set[str] = set()
        first = True
        self._unsaid.clear()  # handed to a closed session and never said: passed over, or `spoken` sticks
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
                if first:
                    continue
                if m.get("question"):
                    self.question, self.question_at = m["question"], time.monotonic()
                self.asked_at = 0.0  # an answer came: Quiet counts again
                self._unsaid[m["id"]] = int(m.get("seq") or 0)
                yield {"kind": "agent", **m}
            self.after, first = max(self.after, int(out.get("after") or 0)), False
            if not self._unsaid:
                self.spoken_seq = self.after
            await asyncio.sleep(POLL_S)


def talk_record(speaker: str, items: list[dict], session_id: str, seconds: float) -> dict:
    """What `/owner/{room}/end` keeps as the "Yours" row: the transcript as phone calls keep it."""
    return {"speakerUserId": speaker, "sessionId": session_id, "durationSec": int(seconds),
            "transcript": clean_transcript(items)}  # fmt: skip


async def run(ctx: JobContext, meta: dict) -> None:
    """The member's Live in the DM, from the dispatch until they leave, go quiet or the cap. GPT-Live is open only
    while the mic is live (the orb open): the warm room costs nothing, and closing the orb ends the billed session."""
    from huddle_agent import live  # noqa: PLC0415

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
    state: dict = {"open": None, "quiet": False}  # the GPT-Live session's start task, while the mic is live
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
        asyncio.create_task(shut(opening))
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
        try:
            await (await opening).close()
        except Exception:  # noqa: BLE001 -- a failed start or close: nothing left open to bill
            logger.warning("owner: live session not closed cleanly", exc_info=True)

    def recount(*_args) -> None:
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
            state["open"].add_done_callback(hooks)
        elif not mic and state["open"] is not None:
            opening, state["open"] = state["open"], None
            asyncio.create_task(shut(opening))
        s = current()
        session = s.session if s else None
        user, agent = (session.user_state, session.agent_state) if session else ("listening", "listening")
        waiting = time.monotonic() - talk.asked_at < WAIT_S  # an answer is on its way: keep the talk open
        quiet_timer.update(mic, user, "thinking" if waiting else agent)

    for event in ("track_published", "track_unpublished", "track_muted", "track_unmuted"):
        ctx.room.on(event, recount)
    limit = asyncio.create_task(asyncio.sleep(OWNER_MAX_S))
    limit.add_done_callback(lambda t: t.cancelled() or asyncio.create_task(leave("owner max length")))

    async def close() -> None:
        limit.cancel()
        quiet_timer.cancel()
        await shut(state["open"])
        try:
            await backend.owner("end", talk_record(member.identity, talk.items, ctx.job.id, time.time() - talk.t0))
        except BackendError as e:
            logger.warning("owner: talk not kept (%s)", e.code)
        finally:
            await backend.aclose()

    ctx.add_shutdown_callback(lambda _reason="": close())
    # the web's RPCs (HuddleProvider): the session already listens, and GPT-Live stops itself when talked over
    ctx.room.local_participant.register_rpc_method("talk_start", lambda _data: "ok")
    ctx.room.local_participant.register_rpc_method("interrupt", lambda _data: "ok")
    await attrs({f"lightbulb.agent.{tile}.status": "listening", f"lightbulb.agent.{tile}.name": name})
    recount()
    logger.info("owner: listening in room %s as %s", meta["roomId"], name)
