"""The Lightbulb huddle state machine: follow along, get summoned, run one exchange, go quiet again.

Everything LiveKit-shaped is injected (`room`, `live_factory`), so this runs against fakes in tests;
agent.py is the thin glue onto the real room and GPT-Live session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from huddle_agent import gate
from huddle_agent.backend import BackendError
from huddle_agent.frames import Turns
from huddle_agent.transcript import Transcript, summons_lightbulb

logger = logging.getLogger("huddle")

IDLE_SECONDS = 20  # no speech from the speaker and no running turn -> the exchange ends
FIRST_JOIN_GRACE = 60  # join dispatches the worker before the browser connects: wait for the first human
THINKING_EVERY = 3  # other people's lines reach the live session at most this often
ERROR_TOPIC = "huddle.error"
FRAME_TOPIC = "call.frame"
MAX_UTTERANCE = 4_000  # TurnBody.utterance
MAX_TRANSCRIPT = 12_000  # TurnBody.transcript
VOICE_NAME = "Lightbulb"
ROOM_IDLE_SECONDS = 15 * 60  # nobody has spoken this long: the worker leaves (and stops paying for STT)
IDLE_ENDED = "idle_ended"  # huddle.error code: the web leaves and says why
SUMMON_APPEND_CHARS = 1_800  # GPT-Live caps each append at 500 tokens
TURN_RETRY_BUDGET = 75  # seconds a turn keeps retrying a waking workspace before apologising
TURN_RETRY_MAX_DELAY = 8
STILL_WAKING = "The team is still waking up; say briefly that you're getting them and it'll take a moment."
NEWS_PROMPT = (
    "You have news for the room that nobody has heard yet. Announce it now, briefly, in one or two "
    "sentences, saying who did the work, then stop:"
)
GATE_CHARS = 3_500  # the gate route caps the transcript at 4,000 characters, 1,000 per line, 40 lines
MAX_ORPHANS = 3  # ended exchanges whose turns are still running: their results become news


def _retryable(e: BackendError) -> bool:
    """A cold workspace (503 runtime_unavailable), the API gateway's own 503/504, or no answer at all."""
    return e.status in (0, 503, 504)


class Live(Protocol):
    """One GPT-Live session (agent.py `_Live`)."""

    def set_participant(self, identity: str) -> None: ...
    def append_thinking(self, text: str, *, delegation_id: str | None = None) -> None: ...
    def append_commentary(self, text: str, *, delegation_id: str | None = None) -> None: ...
    def mute(self) -> None: ...
    def nudge(self) -> None: ...  # generate_reply(): a request the model may decline
    def reply_to(self, text: str) -> None: ...  # generate_reply(user_input=text): a typed user turn
    def interrupt(self) -> None: ...  # cut Lightbulb's playback (or the reply about to play) now
    def usage_seconds(self) -> float: ...
    async def aclose(self) -> None: ...


class Room(Protocol):
    def publish(self, topic: str, payload: dict, to: list[str]) -> None: ...
    def set_attributes(self, attrs: dict[str, str]) -> None: ...  # the agent's participant attributes
    def shutdown(self) -> None: ...


def emit_metric(name: str, value: float, unit: str = "Seconds") -> None:
    """One CloudWatch EMF line on stdout (same shape as the backend's put_metric)."""
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{"Namespace": "LightbulbAI", "Dimensions": [[]], "Metrics": [{"Name": name, "Unit": unit}]}],
        },
        name: value,
    }
    print(json.dumps(line), file=sys.stdout, flush=True)


def _count(name: str) -> None:
    emit_metric(name, 1, "Count")


class _Sink:
    """What one exchange's Turns speaks through: its live session while it is open. After it ends,
    a turn that finishes is news for the room instead."""

    def __init__(self, huddle: "Huddle"):
        self._h = huddle
        self.x: _Exchange | None = None

    def _open(self) -> bool:
        return self.x is not None and self._h._x is self.x

    def thinking(self, text: str, delegation_id: str) -> None:
        if self._open():
            self._h._live_call(self.x, "append_thinking", text, delegation_id=delegation_id)

    def commentary(self, text: str, delegation_id: str) -> None:
        if self._open():
            self._h._live_call(self.x, "append_commentary", text, delegation_id=delegation_id)

    def ended(self, turn_id: str, agent_name: str, answer: str) -> None:
        if not self._open() and answer:
            self._h._queue_news(turn_id, f"{agent_name}: {answer}")


@dataclass
class _Exchange:
    call_id: str
    live: Any
    turns: Turns
    speaker: str
    deadline: float
    started: float
    last_activity: float
    last_thinking: float
    seq: int = 0
    posting: int = 0  # turn POSTs in flight
    agent_state: str = "listening"  # GPT-Live's AgentState: listening / thinking / speaking / ...
    lines: list[str] = field(default_factory=list)  # other people's lines not yet sent as thinking
    dropping: bool = False  # its live session failed; an end is on the way
    proactive: bool = False  # Lightbulb is speaking unprompted: anyone talking stops it
    opened_proactively: bool = False  # aborting the unprompted turn ends the whole exchange
    audio_started: bool = False
    news_ids: list[str] = field(default_factory=list)  # news handed to this session, dropped once it speaks


class Huddle:
    def __init__(
        self,
        *,
        room: Room,
        backend: Any,
        live_factory: Callable[[str, str, str], Awaitable[Live]],  # (persona, voice, speaker)
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._room, self._backend, self._live_factory, self._clock = room, backend, live_factory, clock
        self._sleep = sleep
        self._published: dict[str, str] | None = None
        self._transcript = Transcript()
        self._people: dict[str, str] = {}  # identity -> display name, humans only
        self._x: _Exchange | None = None
        self._lock = asyncio.Lock()  # one summon at a time
        self.dropped_frames = 0
        self._tasks: set[asyncio.Task] = set()
        self._started = clock()
        self._seen_human = False
        self._last_line: dict[str, str] = {}  # identity -> their latest final line
        self._released: set[str] = set()  # talk_end that landed while a summon was still starting
        self._last_heard = self._started  # the room's last speech (or arrival)
        self._idle_ended = self._left = False
        self._presence_v = 0
        # -- proactive speech (gate.py) --
        self._proactive = os.environ.get("LIGHTBULB_PROACTIVE", "on").strip().lower() != "off"
        self._news: dict[str, tuple[str, float]] = {}  # turn id -> (announcement, queued at)
        self._news_seen: set[str] = set()
        self._orphans: dict[str, tuple[Turns, float]] = {}  # call id -> (its Turns, keep until)
        self._speaking: set[str] = set()  # humans the SFU hears right now
        self._last_voice = self._started  # a human's last speech start, end or final line
        self._last_speaker: str | None = None
        self._said: tuple[str, float] | None = None  # Lightbulb's last spoken line, when it ended
        self._decision_v = 0  # every line supersedes a pending decision
        self._hand: dict | None = None  # {"until", "request"|"news"}: a raised hand's offer
        self._rate = gate.RateLimit()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _live_call(self, x: _Exchange, method: str, *args, **kwargs) -> bool:
        """Every call onto a GPT-Live session goes through here. A dead session raises
        ("AgentSession isn't running"): end that exchange cleanly instead of letting the error
        escape into a person's transcription loop. The next summon starts a fresh one."""
        try:
            getattr(x.live, method)(*args, **kwargs)
            return True
        except Exception:
            logger.exception("huddle live session %s failed; ending the exchange", method)
            self._drop(x)
            return False

    def _drop(self, x: _Exchange) -> None:
        if self._x is x and not x.dropping:
            x.dropping = True
            self._spawn(self.end_exchange(only=x))

    def on_live_closed(self, live: Any) -> None:
        """The GPT-Live session closed on its own (an unrecoverable realtime error)."""
        x = self._x
        if x and x.live is live:
            logger.warning("huddle live session closed under an open exchange call=%s", x.call_id)
            self._drop(x)

    def _name(self, identity: str) -> str:
        return self._people.get(identity) or "Someone"

    async def on_participants(self, people: dict[str, str]) -> None:
        if set(people) - set(self._people):
            self._last_heard = self._clock()  # someone just arrived: the room isn't idle
        self._people = dict(people)
        if people:
            self._seen_human = True
        # Taken before the first await: these run as concurrent tasks, and the backend drops a stale one.
        self._presence_v = version = max(self._presence_v + 1, time.time_ns())
        try:
            await self._backend.presence(sorted(people), version)
            x = self._x
            if x and x.speaker not in people:  # the speaker left: the exchange is over
                await self.end_exchange()
            if not people:
                await self.end_exchange()
        except Exception:
            logger.exception("huddle participants update failed")
        finally:
            # An empty room before anyone arrived is the dispatch race, not a huddle ending.
            if not people and self._seen_human:
                self._room.shutdown()

    async def on_final_transcript(self, identity: str, name: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._transcript.add(name, text)
        self._last_line[identity] = text
        self._last_heard = self._last_voice = self._clock()
        self._last_speaker = identity
        self._voice_from(identity)
        self._maybe_decide(identity, text)
        x = self._x
        if x is None:
            if summons_lightbulb(text) and not self._lock.locked():
                # a task: awaiting here would stall this person's STT loop for the whole summon
                self._spawn(self.summon(identity, request=text))
            return
        if identity == x.speaker:
            x.last_activity = self._clock()
        elif summons_lightbulb(text):  # someone else takes the floor: same call, their line is the request
            if self._repoint(x, identity):
                self._live_call(x, "reply_to", text)
        else:
            x.lines.append(f"{name}: {text}")
            self._flush_lines(x)

    def on_speaker_activity(self) -> None:
        """The live session heard the speaker (it hears no one else)."""
        if self._x:
            self._x.last_activity = self._clock()

    def on_agent_state(self, state: str) -> None:
        """GPT-Live's agent state. Speaking is activity: the idle clock restarts when it stops."""
        x = self._x
        if x:
            if state == "speaking":
                x.audio_started = True
                for turn_id in x.news_ids:  # the room is hearing it now
                    self._news.pop(turn_id, None)
                x.news_ids = []
            elif x.audio_started:
                x.proactive = False  # the unprompted reply is over; an ordinary exchange from here
            x.agent_state = state
            x.last_activity = self._clock()
            self._sync_state()

    async def talk_start(self, identity: str) -> None:
        if identity not in self._people:  # RPC caller_identity is server-set; still, only present humans
            return
        offer, self._hand = self._hand, None  # a raised hand: the tap asks for what Lightbulb had
        if offer and self._clock() < offer["until"]:
            self._sync_state()
            news = self._news_items() if offer.get("news") else None
            await self.summon(identity, request=offer.get("request"), news=news or None)
            return
        await self.summon(identity, request=None)

    async def talk_end(self, identity: str) -> None:
        x = self._x
        if x and identity == x.speaker:
            self._live_call(x, "nudge")
        elif self._lock.locked():  # released before the summon finished: nudge once it has
            self._released.add(identity)

    async def summon(
        self,
        identity: str,
        request: str | None,
        *,
        news: list[tuple[str, str]] | None = None,
        proactive_since: float | None = None,
    ) -> None:
        """`news`: (turn id, announcement) to announce instead of a request. `proactive_since`: Lightbulb
        decided to speak unprompted at that time; anyone's voice since then aborts it."""
        proactive = proactive_since is not None
        async with self._lock:
            self._released.discard(identity)  # only a release during THIS summon counts
            x = self._x
            if x is not None:  # already open: re-point, no second call
                if self._repoint(x, identity):
                    if proactive and self._spoke_since(proactive_since):
                        _count("live.proactive.aborted")
                    else:
                        self._deliver(x, request, news, proactive)
                return
            try:
                r = await self._backend.summon(identity)
            except BackendError as e:
                logger.info("huddle summon refused: %s", e.code)
                self._room.publish(ERROR_TOPIC, {"code": e.code, "reason": e.reason}, [identity])
                return
            now = self._clock()
            try:
                live = await self._live_factory(r["persona"], r["voice"], identity)  # pinned to the speaker
            except Exception:
                logger.exception("huddle live session failed to start")
                await self._end_call(r["callId"], 0)
                self._room.publish(ERROR_TOPIC, {"code": "huddles_upstream", "reason": None}, [identity])
                return
            names = {t["id"]: t["name"] for t in r.get("team") or []}
            sink = _Sink(self)
            x = self._x = sink.x = _Exchange(
                call_id=r["callId"],
                live=live,
                turns=Turns(sink, names, r.get("voiceName") or VOICE_NAME, self._clock),
                speaker=identity,
                deadline=float(r["deadline"]),
                started=now,
                last_activity=now,
                last_thinking=now,
            )
            context = "Transcript so far:\n" + self._transcript.text()
            ok = all(
                self._live_call(x, "append_thinking", context[i : i + SUMMON_APPEND_CHARS])
                for i in range(0, len(context), SUMMON_APPEND_CHARS)
            ) and self._live_call(x, "append_thinking", f"{self._name(identity)} is talking to you")
            x.opened_proactively = proactive
            if ok and proactive and self._spoke_since(proactive_since):  # the session start took a while
                _count("live.proactive.aborted")
                self._drop(x)
            elif ok and (request or news):
                self._deliver(x, request, news, proactive)
            elif ok and identity in self._released:
                self._live_call(x, "nudge")  # they already let go of Talk
            self._released.discard(identity)
            self._sync_state()

    def _deliver(self, x: _Exchange, request: str | None, news: list[tuple[str, str]] | None, proactive: bool) -> None:
        x.proactive, x.audio_started = proactive, False
        if news:
            x.news_ids = [turn_id for turn_id, _ in news]
            text = NEWS_PROMPT + "\n" + "\n".join(f"- {said}" for _, said in news)
            ok = all(
                self._live_call(x, "append_thinking", text[i : i + SUMMON_APPEND_CHARS])
                for i in range(0, len(text), SUMMON_APPEND_CHARS)
            )
            if ok:
                self._live_call(x, "nudge")
        elif request:
            self._live_call(x, "reply_to", request)

    def _repoint(self, x: _Exchange, identity: str) -> bool:
        """False when the live session died on the way (the exchange is ending)."""
        x.last_activity = self._clock()
        if identity == x.speaker:
            return True
        x.speaker = identity
        return self._live_call(x, "set_participant", identity) and self._live_call(
            x, "append_thinking", f"{self._name(identity)} is talking to you"
        )

    async def on_delegation(self, delegation_id: str, pending_transcript: str) -> None:
        x = self._x
        if x is None:
            return
        x.seq += 1
        seq = x.seq
        x.turns.delegated(delegation_id, seq)
        # A typed request (wake word) leaves GPT-Live's own audio transcript empty: fall back to the
        # speaker's own latest line, never whoever spoke last.
        utterance = pending_transcript.strip() or self._last_line.get(x.speaker, "").strip() or "(inaudible)"
        x.posting += 1
        started, attempt = self._clock(), 0
        try:
            while True:
                try:
                    # The same seq on every retry: the backend lets a delegation replace its own failed attempt.
                    r = await self._backend.turn(
                        x.call_id, x.speaker, seq, utterance[:MAX_UTTERANCE], self._transcript.text()[-MAX_TRANSCRIPT:]
                    )
                    break
                except BackendError as e:
                    delay = min(2 ** (attempt + 1), TURN_RETRY_MAX_DELAY)
                    waited = self._clock() - started
                    if _retryable(e) and self._x is x and waited + delay <= TURN_RETRY_BUDGET:
                        if attempt == 0:  # a cold workspace takes about a minute: say so once
                            self._live_call(x, "append_thinking", STILL_WAKING, delegation_id=delegation_id)
                        attempt += 1
                        await self._sleep(delay)
                        continue
                    if self._x is x:  # a 409 turn_conflict means a newer delegation won: drop it silently
                        x.turns.failed(delegation_id, speak=e.code != "turn_conflict")
                    return
        finally:
            x.posting -= 1
            x.last_activity = self._clock()
        if self._x is x:
            x.turns.resolved(delegation_id, r["turnId"], r["agentId"])

    def on_data(self, sender: object | None, topic: str | None, data: bytes) -> None:
        if topic != FRAME_TOPIC:
            return
        if sender is not None:  # only the backend's server-side SendData (no sender) is trusted
            self.dropped_frames += 1
            logger.warning("huddle: dropped a call.frame packet sent by a participant")
            return
        x = self._x
        if x is None and not self._orphans:
            return
        try:
            events = json.loads(data).get("events")
        except (ValueError, AttributeError):
            return
        for ev in events if isinstance(events, list) else []:
            call_id = isinstance(ev, dict) and (ev.get("payload") or {}).get("callId")
            if not call_id:
                continue
            if x and call_id == x.call_id:
                x.last_activity = self._clock()
                x.turns.on_event(ev)
            elif call_id in self._orphans:  # an ended exchange's turn still finishing: its result is news
                self._orphans[call_id][0].on_event(ev)

    async def tick(self) -> None:
        """Called about once a second by the glue."""
        if self._idle_ended:
            if not self._left:  # a tick after the notice, so its publish has gone out
                self._left = True
                self._room.shutdown()
            return
        if not self._seen_human and self._clock() - self._started >= FIRST_JOIN_GRACE:
            self._seen_human = True  # shut down once
            self._room.shutdown()
            return
        self._sync_state()
        now = self._clock()
        self._orphans = {c: o for c, o in self._orphans.items() if now < o[1] and o[0].running()}
        x = self._x
        if x is None:
            if self._people and self._clock() - self._last_heard >= ROOM_IDLE_SECONDS:
                await self._end_idle_room()
            return
        self._flush_lines(x)
        now = self._clock()
        busy = x.posting or x.turns.running() or x.agent_state == "speaking"
        if (now - x.last_activity >= IDLE_SECONDS and not busy) or now >= x.deadline:
            await self.end_exchange()

    def _state(self) -> dict[str, str]:
        """What the web's Lightbulb tile shows, as the worker's participant attributes."""
        x = self._x
        hand = "1" if self._hand and self._clock() < self._hand["until"] else ""
        if x is None:
            return {"lightbulb.status": "following", "lightbulb.speaker": "", "lightbulb.working": "", "lightbulb.hand": hand}
        working = x.turns.working()
        if x.agent_state == "speaking":
            status = "speaking"
        elif working:
            status = "working"
        elif x.agent_state == "thinking" or x.posting or x.turns.running():
            status = "thinking"
        else:
            status = "listening"
        return {
            "lightbulb.status": status,
            "lightbulb.speaker": self._name(x.speaker),
            "lightbulb.working": working or "",
            "lightbulb.hand": hand,
        }

    def _sync_state(self) -> None:
        state = self._state()
        if state != self._published:
            self._published = state
            self._room.set_attributes(state)

    def _flush_lines(self, x: _Exchange) -> None:
        now = self._clock()
        if x.lines and now - x.last_thinking >= THINKING_EVERY:
            self._live_call(x, "append_thinking", "\n".join(x.lines))
            x.lines.clear()
            x.last_thinking = now

    async def _end_idle_room(self) -> None:
        """Nobody has spoken for ROOM_IDLE_SECONDS: tell everyone why, clear the marker, leave."""
        logger.info("huddle idle for %ss: leaving", ROOM_IDLE_SECONDS)
        self._room.publish(ERROR_TOPIC, {"code": IDLE_ENDED, "reason": None}, sorted(self._people))
        self._idle_ended = True
        self._presence_v = version = max(self._presence_v + 1, time.time_ns())
        try:
            await self._backend.presence([], version)  # every client leaves on that notice
        except Exception:
            logger.exception("huddle idle presence clear failed")

    async def end_exchange(self, only: _Exchange | None = None) -> None:
        """`only`: end that exchange, not one a later summon opened."""
        async with self._lock:  # a summon in flight finishes first, then is ended here
            if only is not None and self._x is not only:
                return
            x, self._x = self._x, None
        if x is None:
            return
        if x.turns.running():  # a result that lands after this is news for the room
            self._orphans[x.call_id] = (x.turns, self._clock() + gate.NEWS_TTL)
            while len(self._orphans) > MAX_ORPHANS:
                self._orphans.pop(next(iter(self._orphans)))
        self._sync_state()
        usage = 0.0
        try:
            x.live.mute()
            await x.live.aclose()  # GPT-Live bills per session second
            usage = x.live.usage_seconds()  # after close: the final usage delta lands during it
        except Exception:
            logger.exception("huddle live session close failed")
        finally:
            wall = min(self._clock(), x.deadline) - x.started
            await self._end_call(x.call_id, usage or wall)

    async def _end_call(self, call_id: str, usage: float) -> None:
        try:
            await self._backend.end(call_id, max(0, round(usage)))
        except Exception:
            logger.exception("huddle end post failed call=%s", call_id)

    # -- proactive speech: news and follow-ups (gate.py) --

    def on_agent_said(self, text: str) -> None:
        """Lightbulb finished a spoken message (conversation_item_added, role assistant)."""
        text = text.strip()
        if text:
            self._transcript.add(VOICE_NAME, text)
            self._said = (text, self._clock())

    def on_active_speakers(self, identities: set[str]) -> None:
        """The SFU's active speakers (every participant's audio level): the room-wide floor."""
        humans = {i for i in identities if i in self._people}
        if humans or self._speaking:  # someone started, or the room just went quiet
            self._last_voice = self._clock()
        started = humans - self._speaking
        self._speaking = humans
        for identity in started:
            self._voice_from(identity)

    def _voice_from(self, identity: str) -> None:
        """A person spoke: an unprompted turn yields at once, before or during its audio."""
        x = self._x
        if not x or not x.proactive:
            return
        x.proactive = False
        if not x.audio_started:
            _count("live.proactive.aborted")
            self._live_call(x, "interrupt")
            if x.opened_proactively:
                self._drop(x)
        elif x.agent_state == "speaking":
            _count("live.proactive.cut")
            self._live_call(x, "interrupt")

    def _quiet(self, now: float) -> bool:
        x = self._x
        lightbulb_talking = x is not None and x.agent_state == "speaking"
        return not self._speaking and not lightbulb_talking and now - self._last_voice >= gate.PAUSE_S

    def _spoke_since(self, t: float) -> bool:
        return bool(self._speaking) or self._last_voice > t

    def _queue_news(self, turn_id: str, announcement: str) -> None:
        if turn_id in self._news_seen:
            return
        self._news_seen.add(turn_id)
        self._news[turn_id] = (announcement, self._clock())
        while len(self._news) > gate.MAX_NEWS:
            self._news.pop(next(iter(self._news)))
        self._maybe_decide(None, None)  # it may have landed in a lull

    def _news_items(self) -> list[tuple[str, str]]:
        now = self._clock()
        self._news = {k: v for k, v in self._news.items() if now - v[1] < gate.NEWS_TTL}
        return [(turn_id, said) for turn_id, (said, _) in self._news.items()]

    def _maybe_decide(self, identity: str | None, text: str | None) -> None:
        """A decision point: a final line, or news. Only one with a candidate kind costs a gate call."""
        self._decision_v += 1
        if not self._proactive or not self._people:
            return
        now = self._clock()
        in_window = self._said is not None and now - self._said[1] <= gate.FOLLOWUP_SECONDS
        if self._news_items() or (in_window and text):
            self._spawn(self._decide(self._decision_v, identity, text, now))

    async def _decide(self, v: int, identity: str | None, text: str | None, heard_at: float) -> None:
        await self._sleep(gate.PAUSE_S)
        now = self._clock()
        if v != self._decision_v or self._lock.locked() or not self._quiet(now):
            return  # a newer line took over, a summon is under way, or the floor isn't free
        x = self._x
        followup_ok = (
            text is not None
            and self._said is not None
            and heard_at - self._said[1] <= gate.FOLLOWUP_SECONDS
            # an open exchange already hears its own speaker; the others need the gate
            and (x is None or (identity != x.speaker and x.agent_state == "listening" and not x.posting and not x.turns.running()))
        )
        others = [n for i, n in self._people.items() if i != identity]
        kind = gate.candidate(
            text, others=others, exchange_open=x is not None, followup_ok=followup_ok, news=bool(self._news_items())
        )
        speaker = identity or self._last_speaker
        if speaker not in self._people:
            speaker = min(self._people, default=None)
        if kind is None or speaker is None or not self._rate.ok(now):
            return
        _count("live.gate.calls")
        try:
            r = await self._backend.gate(self._gate_body(now))
            p = float(r.get("pSpeak") or 0)
        except Exception:  # noqa: BLE001 -- no answer is silence
            logger.info("huddle gate call failed", exc_info=True)
            return
        if p < gate.HAND_P or v != self._decision_v:
            return
        if p < gate.SPEAK_P:
            _count("live.gate.hand")
            self._hand = {"until": self._clock() + gate.HAND_SECONDS}
            self._hand.update({"request": text} if kind == gate.FOLLOWUP else {"news": True})
            self._sync_state()
            return
        _count("live.gate.speak")
        if self._spoke_since(now) or self._lock.locked():  # the re-check: someone took the floor meanwhile
            _count("live.proactive.aborted")
            return
        self._rate.record(now)
        self._hand = None
        if kind == gate.FOLLOWUP:
            await self.summon(speaker, request=text, proactive_since=now)
        else:
            await self.summon(speaker, request=None, news=self._news_items(), proactive_since=now)

    def _gate_body(self, now: float) -> dict:
        lines = self._transcript.tail(GATE_CHARS)[-40:]
        said, said_at = self._said or (None, None)
        return {
            "transcript": [{"author": n[:128], "text": t[-1_000:]} for n, t in lines],
            "people": sorted(n[:128] for n in self._people.values())[:64],
            "lightbulbLastSaid": said[-1_000:] if said else None,
            "spokeSecondsAgo": int(now - said_at) if said_at is not None else None,
            "pendingNews": [said[-1_000:] for _, said in self._news_items()],
            "pauseMs": int((now - self._last_voice) * 1000),
        }
