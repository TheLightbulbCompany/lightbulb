"""Turn frame handling for the CEO huddle worker.

A port of packages/shared/src/voice/session.ts (delegate/onLive/applyFrame/handle/speakReady)
and sentences.ts. Keep the two in step: same speech strings, same REORDER_WINDOW.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

APPEND_CHARS = 1400  # OpenAI caps each append at 500 tokens
# ponytail: the bridge publishes deltas in batches of 5 (FRAME_BATCH, hermes bridge turn.py) and AppSync can swap whole
# publishes, so deltas are held 10 back (2 batches). Reordering beyond that applies as it arrives; raise this with FRAME_BATCH.
REORDER_WINDOW = 10
STALE_TURN_SECONDS = 120  # a resolved turn silent this long lost its run_ended (bridge crash): not running

_SENTENCES = re.compile(r".*[.!?](?=\s)", re.DOTALL)


def take_sentences(text: str) -> tuple[str, str]:
    """Split off every completed sentence (terminator followed by whitespace) -> (ready, rest)."""
    m = _SENTENCES.match(text)
    if not m:
        return "", text
    return m.group(0).strip(), text[m.end():]


class Sink(Protocol):
    def thinking(self, text: str, delegation_id: str) -> None: ...
    def commentary(self, text: str, delegation_id: str) -> None: ...


@dataclass
class _Turn:
    order: int
    delegation_id: str
    turn_id: str | None = None
    agent_id: str | None = None
    buffered: list[tuple[str, dict]] = field(default_factory=list)
    pending: list[tuple[int, dict]] = field(default_factory=list)
    ended: bool = False
    reset_seq: float = -1
    auto: int = 0
    text: str = ""
    spoken: str = ""
    label: str | None = None
    seen: float = 0.0  # last resolve or frame


class Turns:
    def __init__(self, sink: Sink, names: dict[str, str], voice_name: str, clock: Callable[[], float] = time.time):
        self._sink, self._clock = sink, clock
        self._names = names
        self._voice_name = voice_name
        self._turns: dict[str, _Turn] = {}  # by delegation id
        self._latest_by_agent: dict[str, str] = {}  # agentId -> delegation id

    def delegated(self, delegation_id: str, order: int) -> None:
        """Record a delegation before its POST, so frames that beat the response are buffered."""
        self._turns[delegation_id] = _Turn(order=order, delegation_id=delegation_id)

    def resolved(self, delegation_id: str, turn_id: str, agent_id: str) -> bool:
        """The POST returned. False = this turn was superseded (or is unknown) and will never speak."""
        turn = self._turns.get(delegation_id)
        if turn is None:
            return False
        turn.turn_id, turn.agent_id = turn_id, agent_id
        turn.seen = self._clock()
        # Supersession follows delegation order, not the order POSTs resolve.
        prev = self._latest_by_agent.get(agent_id)
        prev_turn = self._turns.get(prev) if prev else None
        if prev_turn and prev != delegation_id and prev_turn.order > turn.order:
            del self._turns[delegation_id]
            return False
        if prev and prev != delegation_id:
            self._turns.pop(prev, None)  # the same agent's older turn: its frames are now ignored
        self._latest_by_agent[agent_id] = delegation_id
        # Name the teammate now, so the voice can attribute a result that arrives with no tool frame.
        self._sink.thinking(f"{self._name_of(turn)} is on it. Not done yet.", delegation_id)
        buffered, turn.buffered = turn.buffered, []
        for tid, frame in buffered:
            if tid == turn_id:
                self._apply(turn, frame)
        return True

    def failed(self, delegation_id: str, speak: bool = True) -> None:
        """The POST failed. speak=False (a 409 turn_conflict: a newer delegation won) drops the turn silently."""
        if speak:
            self._sink.commentary("Sorry, I couldn't reach the team for that.", delegation_id)
        self._turns.pop(delegation_id, None)

    def on_event(self, event: dict) -> None:
        if event.get("type") != "call.turn.frame":
            return
        payload = event.get("payload") or {}
        turn_id, frame = payload.get("turnId"), payload.get("frame")
        if not isinstance(frame, dict):
            return
        for turn in self._turns.values():
            # No turnId never matches: an unresolved turn's turn_id is None too (TS: undefined !== null).
            if turn_id is not None and turn.turn_id == turn_id:
                turn.seen = self._clock()
                return self._apply(turn, frame)
        # Not resolved yet: hold it on every unresolved turn; each replays only frames with its own turnId.
        for turn in self._turns.values():
            if turn.turn_id is None:
                turn.buffered.append((turn_id, frame))

    def running(self) -> bool:
        """A resolved turn that has not ended, labelled or not: its answer is still coming.
        One with no frame for STALE_TURN_SECONDS lost its run_ended and stops holding the exchange open."""
        now = self._clock()
        return any(
            t.turn_id is not None and not t.ended and now - t.seen < STALE_TURN_SECONDS for t in self._turns.values()
        )

    def working(self) -> str | None:
        """"Gauge — checking Stripe" for the newest labelled running turn, else None."""
        busy = [t for t in self._turns.values() if t.label and not t.ended and t.agent_id]
        if not busy:
            return None
        t = max(busy, key=lambda t: t.order)
        return f"{self._name_of(t)} — {t.label}"

    def _name_of(self, turn: _Turn) -> str:
        return (turn.agent_id and self._names.get(turn.agent_id)) or self._voice_name or "the team"

    def _apply(self, turn: _Turn, frame: dict) -> None:
        """Bridge frames carry a per-run `seq`; publish order is not kept, so deltas are re-sorted here."""
        seq = frame.get("seq")
        if not isinstance(seq, (int, float)) or isinstance(seq, bool):
            turn.auto += 1
            seq = turn.auto
        kind = frame.get("type")
        if kind == "chunk" and frame.get("append"):
            if seq <= turn.reset_seq:
                return  # narration from before a tool_start
            turn.pending.append((seq, frame))
            turn.pending.sort(key=lambda p: p[0])
            while len(turn.pending) > REORDER_WINDOW:
                self._handle(turn, turn.pending.pop(0)[1])
            return
        # Any other frame: everything ordered before it is applied first.
        while turn.pending and (kind != "tool_start" or turn.pending[0][0] < seq):
            self._handle(turn, turn.pending.pop(0)[1])
        if kind in ("tool_start", "chunk"):
            turn.reset_seq = max(turn.reset_seq, seq)
        self._handle(turn, frame)

    def _handle(self, turn: _Turn, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "tool_start" and isinstance(frame.get("tool"), str):
            turn.text = ""  # narration so far was already spoken; the next text box is a new answer
            turn.spoken = ""
            if frame["tool"] == turn.label:
                return
            turn.label = frame["tool"]
            self._sink.thinking(f"{self._name_of(turn)} is working: {turn.label}. Not done yet.", turn.delegation_id)
        elif kind == "chunk" and isinstance(frame.get("content"), str):
            content = frame["content"]
            if frame.get("append"):
                turn.text += content
            elif content.startswith(turn.spoken):
                turn.text = content
            else:  # deltas were lost: speak the whole answer only if nothing was spoken since the reset
                nothing_spoken = not turn.spoken
                turn.text = content
                turn.spoken = "" if nothing_spoken else content
            self._speak_ready(turn, turn.ended)  # a closing chunk that lands after run_ended is final
        elif kind == "error" and isinstance(frame.get("message"), str):
            turn.text = turn.spoken = "(apology)"  # counts as spoken so run_ended stays quiet
            self._sink.commentary(f"Sorry, I couldn't reach {self._name_of(turn)} for that.", turn.delegation_id)
        elif kind == "run_ended":
            turn.ended = True
            self._speak_ready(turn, True)
            if not turn.spoken:
                self._sink.thinking(
                    f"{self._name_of(turn)} finished that request without a spoken result.", turn.delegation_id
                )

    def _speak_ready(self, turn: _Turn, final: bool) -> None:
        pending = turn.text[len(turn.spoken):]
        ready, rest = (pending.strip(), "") if final else take_sentences(pending)
        if not ready:
            return
        turn.spoken = turn.text[: len(turn.text) - len(rest)]
        for i in range(0, len(ready), APPEND_CHARS):
            self._sink.commentary(ready[i : i + APPEND_CHARS], turn.delegation_id)

