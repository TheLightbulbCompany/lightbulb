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


class Huddle:
    def __init__(
        self,
        *,
        room: Room,
        backend: Any,
        live_factory: Callable[[str, str, str], Awaitable[Live]],  # (persona, voice, speaker)
        clock: Callable[[], float] = time.time,
    ):
        self._room, self._backend, self._live_factory, self._clock = room, backend, live_factory, clock
        self._published: dict[str, str] | None = None
        self._transcript = Transcript()
        self._people: dict[str, str] = {}  # identity -> display name, humans only
        self._x: _Exchange | None = None
        self._lock = asyncio.Lock()  # one summon at a time
        self.dropped_frames = 0
        self._tasks: set[asyncio.Task] = set()
        self._started = clock()
        self._seen_human = False

    # -- the Sink Turns speaks through: always the current exchange's live session --
    def thinking(self, text: str, delegation_id: str) -> None:
        if self._x:
            self._x.live.append_thinking(text, delegation_id=delegation_id)

    def commentary(self, text: str, delegation_id: str) -> None:
        if self._x:
            self._x.live.append_commentary(text, delegation_id=delegation_id)

    def _name(self, identity: str) -> str:
        return self._people.get(identity) or "Someone"

    async def on_participants(self, people: dict[str, str]) -> None:
        self._people = dict(people)
        if people:
            self._seen_human = True
        try:
            await self._backend.presence(sorted(people))
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
        x = self._x
        if x is None:
            if summons_lightbulb(text) and not self._lock.locked():
                # a task: awaiting here would stall this person's STT loop for the whole summon
                t = asyncio.create_task(self.summon(identity, request=text))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            return
        if identity == x.speaker:
            x.last_activity = self._clock()
        elif summons_lightbulb(text):  # someone else takes the floor: same call, their line is the request
            self._repoint(x, identity)
            x.live.reply_to(text)
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
            self._sync_state()

    async def talk_start(self, identity: str) -> None:
        if identity not in self._people:  # RPC caller_identity is server-set; still, only present humans
            return
        await self.summon(identity, request=None)

    async def talk_end(self, identity: str) -> None:
        x = self._x
        if x and identity == x.speaker:
            x.live.nudge()

    async def summon(self, identity: str, request: str | None) -> None:
        async with self._lock:
            x = self._x
            if x is not None:  # already open: re-point, no second call
                self._repoint(x, identity)
                if request:
                    x.live.reply_to(request)
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
            x = self._x = _Exchange(
                call_id=r["callId"],
                live=live,
                turns=Turns(self, names, r.get("voiceName") or VOICE_NAME, self._clock),
                speaker=identity,
                deadline=float(r["deadline"]),
                started=now,
                last_activity=now,
                last_thinking=now,
            )
            live.append_thinking("Transcript so far:\n" + self._transcript.text())
            live.append_thinking(f"{self._name(identity)} is talking to you")
            if request:
                live.reply_to(request)
            self._sync_state()

    def _repoint(self, x: _Exchange, identity: str) -> None:
        x.last_activity = self._clock()
        if identity == x.speaker:
            return
        x.speaker = identity
        x.live.set_participant(identity)
        x.live.append_thinking(f"{self._name(identity)} is talking to you")

    async def on_delegation(self, delegation_id: str, pending_transcript: str) -> None:
        x = self._x
        if x is None:
            return
        x.seq += 1
        x.turns.delegated(delegation_id, x.seq)
        utterance = pending_transcript.strip() or (self._transcript.text().rsplit("\n", 1)[-1]) or "(inaudible)"
        x.posting += 1
        try:
            r = await self._backend.turn(
                x.call_id, x.speaker, x.seq, utterance[:MAX_UTTERANCE], self._transcript.text()[-MAX_TRANSCRIPT:]
            )
        except BackendError as e:
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
        if not self._seen_human and self._clock() - self._started >= FIRST_JOIN_GRACE:
            self._seen_human = True  # shut down once
            self._room.shutdown()
            return
        self._sync_state()
        x = self._x
        if x is None:
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
            x.live.append_thinking("\n".join(x.lines))
            x.lines.clear()
            x.last_thinking = now

    async def end_exchange(self) -> None:
        async with self._lock:  # a summon in flight finishes first, then is ended here
            x, self._x = self._x, None
        if x is None:
            return
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
