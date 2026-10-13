"""An opt-in tap on the call controller: every input it gets, one JSON line each, with a monotonic time.

A recording is what `tests/replay` needs to run the same call again against a changed controller, offline.
Set HUDDLE_RECORD_DIR to a directory to turn it on; unset (everywhere but a test rig) it does nothing.

Inputs only: what people did (speech, sound, transcripts, joins), what the backend answered and how long it
took (`call`), run frames (`on_data`), what each voice reported (state, receipts) and how long a voice took to
open (`live`) or a line to be written (`write`). What the controller decided is not written: a replay makes
its own decisions. No secrets: the room token and the service secret never reach these seams, and anything
that looks like a credential or a URL is dropped on the way out (`_clean`)."""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import time
from typing import Any

ENV = "HUDDLE_RECORD_DIR"
# Huddle's entry points, as the worker calls them (agent.py). A method the controller calls on itself is not
# an input and is not written (`_tap`'s depth).
INPUTS = (
    "on_participants", "on_active_speakers", "on_speech", "on_interim_transcript", "on_final_transcript",
    "stt_stopped", "on_data", "on_agent_state", "on_agent_said", "on_unit", "on_unit_audible",
    "on_playback_ended", "may_say", "on_live_closed", "talk_start", "tick",
)  # fmt: skip
CALLS = ("team", "route", "summon", "session", "turn", "abort", "no_post", "late_answer", "end", "leave_meeting")
# The transcript is the controller's own record, sent back to the backend: an output, and most of the bytes.
DROPPED_ARGS = ("transcript",)
_SECRET = re.compile(r"token|secret|password|authorization|api_?key|url|uri", re.I)
_URL = re.compile(r"\b(?:https?|wss?)://\S+")


def _clean(x: Any) -> Any:
    """JSON-safe, with nothing that could be a credential or an address."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items() if not _SECRET.search(str(k))}
    if isinstance(x, (set, frozenset)):
        return sorted(_clean(v) for v in x)
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, bytes):
        try:
            return _clean(json.loads(x))
        except ValueError:
            return None
    if isinstance(x, str):
        return _URL.sub("[url]", x)
    if x is None or isinstance(x, (bool, int, float)):
        return x
    for attr in ("agent_id", "identity"):  # a voice, a participant: who, not what
        if isinstance(v := getattr(x, attr, None), str):
            return v
    return type(x).__name__


class Recorder:
    def __init__(self, path: str, clock=time.monotonic) -> None:
        self._f = open(path, "a", buffering=1)  # line buffered: a killed worker loses nothing written
        self._depth, self.clock = 0, clock
        self.write("start", wall=time.time())  # the one wall-clock time: where the rig's own clock lines up

    def write(self, k: str, **fields) -> None:
        self._f.write(json.dumps({"t": round(self.clock(), 4), "k": k, **_clean(fields)}) + "\n")

    def _tap(self, name: str, fn):
        sig = inspect.signature(fn)

        def note(a, kw) -> None:
            if self._depth == 0:
                self.write(name, **sig.bind(*a, **kw).arguments)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def tapped(*a, **kw):
                note(a, kw)
                return await fn(*a, **kw)

            return tapped

        @functools.wraps(fn)
        def tapped(*a, **kw):
            note(a, kw)
            self._depth += 1
            try:
                return fn(*a, **kw)
            finally:
                self._depth -= 1

        return tapped

    def inputs(self, huddle: Any) -> None:
        for name in INPUTS:
            setattr(huddle, name, self._tap(name, getattr(huddle, name)))

    def backend(self, inner: Any) -> Any:
        return _Backend(inner, self)

    def timed(self, k: str, fn, keep=lambda kw, r: {}):
        """`fn` with how long it took written as one `k` line; `keep`: what else of it a replay needs."""

        async def call(*a, **kw):
            began = self.clock()
            r = None
            try:
                r = await fn(*a, **kw)
                return r
            finally:
                self.write(k, ms=round((self.clock() - began) * 1000), ok=r is not None, **keep(kw, r))

        return call

    def factory(self, fn):
        return self.timed("live", fn, lambda kw, r: {"agent": kw.get("agent_id")})

    def writer(self, fn):
        async def write(persona: str, event: str) -> str:
            return await self.timed("write", fn, lambda kw, r: {"event": event, "reply": r})(persona, event)

        return write


class _Backend:
    """The backend client, with each answer and its latency written as it lands."""

    def __init__(self, inner: Any, rec: Recorder) -> None:
        self._inner, self._rec = inner, rec

    def __getattr__(self, name: str):
        fn = getattr(self._inner, name)
        if name not in CALLS:
            return fn
        sig = inspect.signature(fn)

        async def call(*a, **kw):
            began = self._rec.clock()
            args = {k: v for k, v in sig.bind(*a, **kw).arguments.items() if k not in DROPPED_ARGS}
            out: dict = {}
            try:
                out["reply"] = r = await fn(*a, **kw)
                return r
            except Exception as e:
                out["error"] = {"status": getattr(e, "status", 0), "code": getattr(e, "code", type(e).__name__)}
                raise
            finally:
                self._rec.write("call", m=name, args=args, ms=round((self._rec.clock() - began) * 1000), **out)

        return call


class _Off:
    """No recording: every seam is handed back as it came."""

    def inputs(self, huddle: Any) -> None:
        pass

    def backend(self, inner: Any) -> Any:
        return inner

    factory = writer = staticmethod(lambda fn: fn)


def start(room_id: str) -> Recorder | _Off:
    if not (out := os.environ.get(ENV)):
        return _Off()
    os.makedirs(out, exist_ok=True)
    return Recorder(os.path.join(out, f"{room_id}-{int(time.time())}.jsonl"))
