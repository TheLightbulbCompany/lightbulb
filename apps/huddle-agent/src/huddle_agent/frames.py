"""Voice run frames for the huddle worker, routed by run id and agent (C2/C3).

Ordering is a port of packages/shared/src/voice/session.ts. Keep the two in step: same REORDER_WINDOW.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

# ponytail: the bridge publishes deltas in batches of 5 (FRAME_BATCH, hermes bridge turn.py) and AppSync can swap whole
# publishes, so deltas are held 10 back (2 batches). Reordering beyond that applies as it arrives; raise this with FRAME_BATCH.
REORDER_WINDOW = 10
# a run silent this long lost its run_ended (bridge crash): not running. A cold workspace start and four model
# steps were silent for 135 s and then answered (10-04 prod test), so the limit sits well above that.
STALE_TURN_SECONDS = 300
QUEUED_TURN_SECONDS = 1_800  # a run_queued run waits on the room lock behind a chat run (the chat run budget)

logger = logging.getLogger("huddle.frames")


def _order(state, frame: dict, handle: Callable[[dict], None]) -> None:
    """Bridge frames carry a per-run `seq`; publish order is not kept, so deltas are re-sorted here.
    `state` (a Run) holds pending, reset_seq and auto."""
    seq = frame.get("seq")
    if not isinstance(seq, (int, float)) or isinstance(seq, bool):
        state.auto += 1
        seq = state.auto
    kind = frame.get("type")
    if kind == "chunk" and frame.get("append"):
        if seq <= state.reset_seq:
            return  # narration from before a tool_start
        state.pending.append((seq, frame))
        state.pending.sort(key=lambda p: p[0])
        while len(state.pending) > REORDER_WINDOW:
            handle(state.pending.pop(0)[1])
        return
    # Any other frame: everything ordered before it is applied first.
    while state.pending and (kind != "tool_start" or state.pending[0][0] < seq):
        handle(state.pending.pop(0)[1])
    if kind in ("tool_start", "chunk"):
        state.reset_seq = max(state.reset_seq, seq)
    handle(frame)


# Every run delivers; nothing supersedes.

WORKING_CHARS = 60  # lightbulb.agent.<id>.working (C4)


class RunSink(Protocol):
    """Where a run's output goes. Nothing is said as it streams: its answer is said when it ends."""

    def progress(self, run: "Run", text: str) -> None: ...  # text = the tool label
    def ended(self, run: "Run", answer: str) -> None: ...
    def failed(self, run: "Run") -> None: ...


@dataclass
class Run:
    run_id: str
    agent_id: str
    speaker: str | None = None
    request: str = ""
    gen: int | None = None  # the voice that started it (`Huddle._same_session`); None: no voice did
    bound: bool = False  # started() was called; an unbound run comes from frames alone (a restarted worker)
    queued: bool = False
    ended: bool = False
    aborted: bool = False
    failed: bool = False
    buffered: list[dict] = field(default_factory=list)  # frames held until started() (or run_ended)
    held: bool = False  # its voice is still opening: frames wait for release()
    pending: list[tuple[int, dict]] = field(default_factory=list)
    reset_seq: float = -1
    auto: int = 0
    text: str = ""  # its last text box: narration before a tool is gone from it
    label: str | None = None
    seen: float = 0.0
    # When it began executing: /turns took it (`Huddle._post_turn`), or it left the room lock's queue after
    # that (`_apply`). 0 while a waking workspace is retried.
    accepted: float = 0.0
    gave_up: bool = False  # its asker was told it could not be done (`Runs.give_up`): it is waited for no longer


class Runs:
    def __init__(self, sink: RunSink, clock: Callable[[], float] = time.time):
        self._sink, self._clock = sink, clock
        self._runs: dict[str, Run] = {}

    def started(self, run_id: str, agent_id: str, *, speaker: str | None, request: str, gen: int | None) -> Run:
        """Call before the /turns POST (the worker mints runId), so no frame can beat it."""
        run = self._runs.get(run_id) or Run(run_id=run_id, agent_id=agent_id)
        run.agent_id, run.speaker, run.request = agent_id, speaker, request
        run.gen, run.bound, run.seen = gen, True, self._clock()
        self._runs[run_id] = run
        if not run.held:
            self._replay(run)
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def hold(self, run_id: str, agent_id: str) -> None:
        """Its voice is still opening: every frame waits until it is open (10-04 prod Meet: a question got a
        greeting and no answer). Called before the run is started, so the entry is made here: a voice that fails
        at once still finds it to release."""
        self._runs.setdefault(run_id, Run(run_id=run_id, agent_id=agent_id)).held = True

    def release(self, run_id: str | None) -> None:
        """The voice is live, or will never be: the frames that waited go where the run's output goes now."""
        if (run := self._runs.get(run_id)) and run.held:
            run.held = False
            self._replay(run)

    def abort(self, run_id: str) -> None:
        """An aborted run is silent from here on; the entry stays so late frames don't recreate it."""
        run = self._runs.get(run_id)
        if run:
            run.aborted = run.ended = True
            run.buffered.clear()
            run.pending.clear()

    def give_up(self, run_id: str) -> None:
        """Its answer is waited for no longer. Unlike `abort` its frames are still read: an answer that comes
        after all is its sink's to place."""
        if run := self._runs.get(run_id):
            run.gave_up = True

    def on_event(self, event: dict) -> None:
        if event.get("type") != "voice.run.frame":
            return
        payload = event.get("payload") or {}
        run_id, frame = payload.get("runId"), payload.get("frame")
        if not isinstance(run_id, str) or not isinstance(frame, dict):
            return
        run = self._runs.get(run_id)
        if run is None:
            agent_id = payload.get("agentId")
            if not isinstance(agent_id, str):
                return  # unroutable
            run = self._runs[run_id] = Run(run_id=run_id, agent_id=agent_id)
        if run.aborted or run.failed:
            return
        run.seen = self._clock()
        if run.held:
            run.buffered.append(frame)
            return
        if not run.bound:
            # ponytail: an unbound run (worker restarted, or frames beat started()) holds every frame until
            # started() or its run_ended; a long run's frames sit in memory meanwhile.
            run.buffered.append(frame)
            if frame.get("type") == "run_ended":
                self._replay(run)
            return
        self._apply(run, frame)

    def running(self, agent_id: str | None = None) -> list[Run]:
        """Runs whose answer is still coming. One with no frame for STALE_TURN_SECONDS lost its run_ended;
        a queued one (run_queued, behind a chat run) waits up to QUEUED_TURN_SECONDS."""
        now = self._clock()
        return [
            r for r in self._runs.values()
            if not r.ended
            and not r.gave_up
            and now - r.seen < (QUEUED_TURN_SECONDS if r.queued else STALE_TURN_SECONDS)
            and agent_id in (None, r.agent_id)
        ]

    def working(self, agent_id: str) -> str:
        """The newest running labelled run's tool label for this agent, ≤60 chars; "" when none."""
        labelled = [r for r in self.running(agent_id) if r.label]
        return labelled[-1].label[:WORKING_CHARS] if labelled else ""

    def _replay(self, run: Run) -> None:
        buffered, run.buffered = run.buffered, []
        for frame in buffered:
            self._apply(run, frame)

    def _apply(self, run: Run, frame: dict) -> None:
        if frame.get("type") == "run_queued":
            run.queued = True
            return
        if run.queued:
            run.accepted = self._clock()  # it starts executing now: the wait on the room lock was not its time
        run.queued = False
        _order(run, frame, lambda f: self._handle(run, f))

    def _handle(self, run: Run, frame: dict) -> None:
        if run.aborted or run.failed:
            return
        kind = frame.get("type")
        if kind == "tool_start" and isinstance(frame.get("tool"), str):
            run.text = ""  # narration before a tool is not the answer: the next text box is
            if frame["tool"] != run.label:
                run.label = frame["tool"]
                self._sink.progress(run, run.label)
        elif kind == "chunk" and isinstance(frame.get("content"), str):
            before = run.text.strip()
            run.text = run.text + frame["content"] if frame.get("append") else frame["content"]
            if run.ended and run.text.strip() != before:
                # the closing chunk landed after run_ended (publishes can swap): the answer is this, not what ended
                self._sink.ended(run, run.text.strip())
        elif kind == "error":
            run.failed = run.ended = True
            # the bridge's own reason, for the ops log (10-07: every turn of a workspace failed in 0.4 s and the
            # worker had nothing to say about why)
            logger.warning(
                "run %s of agent %s failed: %s", run.run_id, run.agent_id,
                str({k: v for k, v in frame.items() if k in ("message", "error", "code", "reason", "detail")})[:300],
            )
            self._sink.failed(run)
        elif kind == "run_ended":
            run.ended = True
            if frame.get("outcome") not in (None, "done"):  # the wire's success is "done" (bridge _WIRE_OUTCOME)
                logger.warning("run %s of agent %s ended %s: %s", run.run_id, run.agent_id, frame.get("outcome"),
                               str({k: v for k, v in frame.items() if k in ("error", "reason", "code", "message", "detail")})[:300])
            self._sink.ended(run, run.text.strip())
