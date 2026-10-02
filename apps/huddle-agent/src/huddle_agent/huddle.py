"""The Lightbulb huddle state machine: follow along, get summoned, run one exchange, go quiet again.

Everything LiveKit-shaped is injected (`room`, `live_factory`), so this runs against fakes in tests;
agent.py is the thin glue onto the real room and GPT-Live session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

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
    def usage_seconds(self) -> float: ...
    async def aclose(self) -> None: ...


class Room(Protocol):
    def publish(self, topic: str, payload: dict, to: list[str]) -> None: ...
    def set_attributes(self, attrs: dict[str, str]) -> None: ...  # the agent's participant attributes
    def shutdown(self) -> None: ...


def emit_metric(name: str, value: float, unit: str = "Seconds", dims: dict[str, str] | None = None) -> None:
    """One CloudWatch EMF line on stdout (same shape as the backend's put_metric)."""
    dims = dims or {}
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": "LightbulbAI", "Dimensions": [list(dims)], "Metrics": [{"Name": name, "Unit": unit}]}
            ],
        },
        **dims,
        name: value,
    }
    print(json.dumps(line), file=sys.stdout, flush=True)


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
    heard_at: float | None = None  # the request's final transcript, until first audio is measured


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
        self._gptlive_closed = 0.0  # usage of this job's closed GPT-Live sessions
        self._closing: _Exchange | None = None  # still counted while its session closes

    def gptlive_seconds(self) -> float:
        """GPT-Live session seconds this job has used so far, open session included."""
        total = self._gptlive_closed
        for x in (self._x, self._closing):
            try:
                total += x.live.usage_seconds() if x else 0.0
            except Exception:
                pass
        return total

    def _latency(self, name: str, heard_at: float) -> None:
        emit_metric(f"live.latency.{name}", round((self._clock() - heard_at) * 1000), "Milliseconds", {"path": "today"})

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

    # -- the Sink Turns speaks through: always the current exchange's live session --
    def thinking(self, text: str, delegation_id: str) -> None:
        if self._x:
            self._live_call(self._x, "append_thinking", text, delegation_id=delegation_id)

    def commentary(self, text: str, delegation_id: str) -> None:
        if self._x:
            self._live_call(self._x, "append_commentary", text, delegation_id=delegation_id)

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
        self._last_heard = self._clock()
        x = self._x
        if x is None:
            if summons_lightbulb(text) and not self._lock.locked():
                # a task: awaiting here would stall this person's STT loop for the whole summon
                self._spawn(self.summon(identity, request=text, heard_at=self._last_heard))
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
            x.agent_state = state
            x.last_activity = self._clock()
            if state == "speaking" and x.heard_at is not None:
                self._latency("first_audio", x.heard_at)
                x.heard_at = None
            self._sync_state()

    async def talk_start(self, identity: str) -> None:
        if identity not in self._people:  # RPC caller_identity is server-set; still, only present humans
            return
        await self.summon(identity, request=None)

    async def talk_end(self, identity: str) -> None:
        x = self._x
        if x and identity == x.speaker:
            self._live_call(x, "nudge")
        elif self._lock.locked():  # released before the summon finished: nudge once it has
            self._released.add(identity)

    async def summon(self, identity: str, request: str | None, heard_at: float | None = None) -> None:
        if heard_at is None:
            heard_at = self._clock()
        async with self._lock:
            self._released.discard(identity)  # only a release during THIS summon counts
            x = self._x
            if x is not None:  # already open: re-point, no second call
                if self._repoint(x, identity) and request:
                    self._live_call(x, "reply_to", request)
                return
            try:
                r = await self._backend.summon(identity)
            except BackendError as e:
                logger.info("huddle summon refused: %s", e.code)
                self._room.publish(ERROR_TOPIC, {"code": e.code, "reason": e.reason}, [identity])
                return
            self._latency("route", heard_at)
            now = self._clock()
            try:
                live = await self._live_factory(r["persona"], r["voice"], identity)  # pinned to the speaker
            except Exception:
                logger.exception("huddle live session failed to start")
                await self._end_call(r["callId"], 0)
                self._room.publish(ERROR_TOPIC, {"code": "huddles_upstream", "reason": None}, [identity])
                return
            self._latency("session_started", heard_at)
            names = {t["id"]: t["name"] for t in r.get("team") or []}
            x = self._x = _Exchange(
                call_id=r["callId"],
                live=live,
                turns=Turns(self, names, r.get("voiceName") or VOICE_NAME, self._clock),
                speaker=identity,
                deadline=float(r["deadline"]),
                started=now,
                last_activity=now,
                last_thinking=now,
                heard_at=heard_at,
            )
            context = "Transcript so far:\n" + self._transcript.text()
            ok = all(
                self._live_call(x, "append_thinking", context[i : i + SUMMON_APPEND_CHARS])
                for i in range(0, len(context), SUMMON_APPEND_CHARS)
            ) and self._live_call(x, "append_thinking", f"{self._name(identity)} is talking to you")
            if ok and request:
                self._live_call(x, "reply_to", request)
            elif ok and identity in self._released:
                self._live_call(x, "nudge")  # they already let go of Talk
            self._released.discard(identity)
            self._sync_state()

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
        if x is None:
            return
        try:
            events = json.loads(data).get("events")
        except (ValueError, AttributeError):
            return
        for ev in events if isinstance(events, list) else []:
            if isinstance(ev, dict) and (ev.get("payload") or {}).get("callId") == x.call_id:
                x.last_activity = self._clock()
                x.turns.on_event(ev)

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
        if x is None:
            return {"lightbulb.status": "following", "lightbulb.speaker": "", "lightbulb.working": ""}
        working = x.turns.working()
        if x.agent_state == "speaking":
            status = "speaking"
        elif working:
            status = "working"
        elif x.agent_state == "thinking" or x.posting or x.turns.running():
            status = "thinking"
        else:
            status = "listening"
        return {"lightbulb.status": status, "lightbulb.speaker": self._name(x.speaker), "lightbulb.working": working or ""}

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
            if x is not None:
                self._closing = x
        if x is None:
            return
        self._sync_state()
        usage = 0.0
        try:
            x.live.mute()
            await x.live.aclose()  # GPT-Live bills per session second
        except Exception:
            logger.exception("huddle live session close failed")
        finally:
            try:
                usage = x.live.usage_seconds()  # after close: the final usage delta lands during it
            except Exception:
                logger.exception("huddle live usage read failed")
            self._gptlive_closed += usage
            if self._closing is x:
                self._closing = None
            wall = min(self._clock(), x.deadline) - x.started
            await self._end_call(x.call_id, usage or wall)

    async def _end_call(self, call_id: str, usage: float) -> None:
        try:
            await self._backend.end(call_id, max(0, round(usage)))
        except Exception:
            logger.exception("huddle end post failed call=%s", call_id)
