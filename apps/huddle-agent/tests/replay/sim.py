"""A call replayed against a real `Huddle` on a virtual clock: no network, no audio, no real sleeping.

Everything around the controller is simulated: the people (their sound, the transcriber's speech detector and
its finals), the backend (`/route`, `/turns`, run frames), and the mouths (each unit "plays" for a time taken
from its text, asks permission like the real one, and stops on an interrupt). The controller is the real
one, unchanged. What comes out is the live rig's three evidence files (rig.txt, worker.log,
transcript.json), so the rig's own `check.py` judges a replay by the scenario's `expect` block.

Two sources of a call:
- synthetic: a scenario json alone. People speak on the scenario's script (reacting to agent audio as the
  rig does), every delay is drawn from `Model`, and `/route` answers from `routing.py` (a correct router,
  assumed).
- a recording (`Recording`): the people's events at their recorded times and the backend's recorded answers
  and latencies. Open loop: the people do not react to what the replayed controller says.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass, field

from huddle_agent import huddle as h_mod
from huddle_agent import record
from huddle_agent.agent import sound_limit
from huddle_agent.backend import BackendError
from huddle_agent.huddle import Huddle, units

from . import check
from .router import Router
from .routing import ANSWERS

EPOCH = 1_791_460_800.0  # one fixed noon UTC: every check is relative, so any single date will do
LEAD = 10.0  # the worker is in the room this long before scenario second 0
ROSTER = ("Friday", "Forge", "Tally", "Pulse", "Gauge", "Vera", "Atlas")  # the rig workspace's agents
PROFILES = ("current", "fast-eot")  # `Model`
RIG_QUIET_S = 0.6  # rig.py: agent audio below its threshold this long ends a burst
FIRST_SOUND_S = 1.0  # agent.py: a unit with no audible audio this long is stopped and reported silent
P_RUN_SLOW, P_RUN_NEVER = 0.12, 0.06  # `Model.run`
P_WINDOW_SLOW = 0.1  # `Model.window`


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps to the next timer when nothing is ready. Sleeps, timeouts and
    `call_later` all run on it, so a two-minute call takes milliseconds and runs the same way every time."""

    def __init__(self) -> None:
        super().__init__()
        self._now = 0.0
        self._selector.select = lambda timeout=None: []  # nothing here does real I/O, and nothing may wait for it

    def time(self) -> float:
        return self._now

    def _run_once(self) -> None:
        if not self._ready:
            # the next timer that will really fire: a cancelled one at the head (every timeout that did not
            # run out) is no reason to move the clock
            when = min((t.when() for t in self._scheduled if not t.cancelled()), default=None)
            if when is None:
                raise RuntimeError("replay deadlock: something waits on an event that never comes")
            self._now = max(self._now, when)
        super()._run_once()


class Model:
    """Every delay the simulation draws, seeded. Ranges are what the 2026-10-08 live rig runs showed (8 calls,
    35 lines, 55 spoken units; worker.log `speech`/`turn`/`permit`/`receipt` lines against rig.txt), each
    with its rarer tail. `fixed`: values read from a recording, used first and in order."""

    def __init__(
        self, seed: int | str, fixed: dict[str, list] | None = None, silent: float = 0.04, profile: str = "current",
        noise: float = 0.0,
    ) -> None:  # fmt: skip
        """`profile`: the transcriber's timing. `current`: today's (onset 0.4-0.6 s, final 1.2-1.9 s after the
        line). `fast-eot`: a what-if, not a measurement: one that hears the onset in 0.2-0.3 s, sends interims
        early, and gives the words with an end-of-turn event about 0.3 s after speech stops.
        `noise`: sound that is not speech (a cough, a key, a chair) on a silent person's track, in bursts a
        second per person: the SFU's level for them goes up for 0.15-0.8 s and the detector hears nothing.
        An assumption, not a measurement, and off by default: the rig's people are `say` and make none."""
        if profile not in PROFILES:
            raise ValueError(f"no timing profile {profile!r}; there are: {', '.join(PROFILES)}")
        self.fast = profile == "fast-eot"
        self.rng = random.Random(seed)
        # the writer's delays are dealt from their own deck: a controller that writes more often (a reply
        # begun beside /route and thrown away) must not re-deal every other delay of the replay
        self.writes = random.Random(f"{seed}:write")
        self.noise, self.noises = noise, random.Random(f"{seed}:noise")  # (its own deck too)
        self.runs = random.Random(f"{seed}:run")  # (and how long each run takes,
        self.windows = random.Random(f"{seed}:window")  # and the call's window)
        self.fixed = fixed or {}
        self.p_silent = silent  # 4 of 55 units played no audible audio

    def u(self, key: str, lo: float, hi: float, tail: tuple[float, float, float] | None = None) -> float:
        if self.fixed.get(key):
            return self.fixed[key].pop(0)
        if tail and self.rng.random() < tail[0]:
            return self.rng.uniform(tail[1], tail[2])
        return self.rng.uniform(lo, hi)

    # -- a person --
    def line_s(self, text: str) -> float:
        """How long macOS `say` takes over a line: "Friday?" 0.64 s, "Friday, go ahead." 1.45 s, 54 chars and a
        comma 3.6 s, 138 chars and four commas 8.1 s. Within 0.5 s of 12 lines of the runs."""
        return 0.25 + 0.052 * len(text) + 0.35 * len(re.findall(r"[,.;:!?]\s", text))

    def voiced_on(self) -> float:  # speech start -> the transcriber's detector
        if self.fast:
            return self.u("voiced_on", 0.2, 0.3)
        return self.u("voiced_on", 0.37, 0.58)

    def voiced_off(self) -> float:  # speech end -> its detector's stop
        if self.fast:
            return self.u("voiced_off", 0.22, 0.32)
        return self.u("voiced_off", 0.75, 1.1, (0.05, 0.2, 0.75))

    def interim(self) -> float:  # the detector's start -> the first interim words
        return 0.2 if self.fast else 0.5

    def final_gap(self) -> float:  # the least time from the detector's stop to the final
        return 0.02 if self.fast else 0.15

    def level_on(self) -> float:  # speech start -> the SFU's active-speaker set
        return self.u("level_on", 0.3, 0.8, (0.05, 0.8, 2.5))

    def level_off(self) -> float:
        return self.u("level_off", 0.5, 1.15)

    def level_gaps(self, s: float, e: float) -> list[tuple[float, float]]:
        """The SFU drops a speaker for a moment mid-sentence (a third of lines; 0.3-0.5 s), and once in the
        runs for 5.2 s under a long line, which is what the detector-loss rule is for."""
        gaps, t = [], s + 1.0 + self.rng.uniform(0.4, 3.6)
        if self.rng.random() < 0.35:
            while t < e - 0.6:
                back = t + self.rng.uniform(0.3, 0.5)
                gaps.append((t, back))
                t = back + self.rng.uniform(0.4, 3.6)
        elif e - s >= 6 and self.rng.random() < 0.15:
            a = s + self.rng.uniform(1.5, 2.5)
            gaps.append((a, min(a + self.rng.uniform(4.5, 5.5), e - 0.5)))
        return gaps

    def final(self) -> float:  # speech end -> its final transcript: 1.17-1.93 s, 4 of 56 later, one at 3.9
        if self.fast:  # the words come with the end-of-turn event
            return self.u("final", 0.26, 0.4)
        if self.rng.random() < 0.02:
            return self.u("final", 2.4, 3.9)
        return self.u("final", 1.2, 1.9, (0.07, 1.9, 2.4))

    def pieces(self, text: str) -> list[tuple[str, float]]:
        """The transcriber sometimes ends a line at a sentence break and hands it over as two (2 of 35):
        [(piece, where it ends as a share of the line)]."""
        out, start = [], 0
        for m in re.finditer(r"[.!?]\s+", text):
            if self.rng.random() < 0.08:
                out.append((text[start : m.end()].strip(), m.end() / len(text)))
                start = m.end()
        return out + [(text[start:], 1.0)]

    # -- the backend --
    def route(self) -> float:  # live.latency.jev: 0.42-0.86 s, 6 of 32 to 1.15, one at 1.76
        if self.rng.random() < 0.03:
            return self.u("route", 1.2, 2.2)
        return self.u("route", 0.45, 0.75, (0.2, 0.75, 1.15))

    def quick(self) -> float:  # /session, /turns: one round trip
        return self.u("quick", 0.1, 0.25)

    def window(self) -> float:
        """/summon, which opens the call's window. A round trip, but in the 2026-10-08 live run the first
        line of one call waited 4.7 s on the window and session calls (`live.latency.route` 5252 ms with the
        router's answer in 548 ms): P_WINDOW_SLOW of them here take 2-5 s. One observation, not a rate."""
        return self.windows.uniform(2.0, 5.0) if self.windows.random() < P_WINDOW_SLOW else self.quick()

    def run(self) -> float | None:
        """Request made -> its answer ready. Most take 5.4-12.3 s. In the 2026-10-08 live run of the 15
        scenarios about 3 of 17 took 45-110 s or never ended: P_RUN_SLOW of them here take that long, and
        P_RUN_NEVER send nothing at all (None). Sized from that one run, not measured."""
        if self.fixed.get("run"):
            return self.fixed["run"].pop(0)
        x = self.runs.random()
        if x < P_RUN_NEVER:
            return None
        return self.runs.uniform(45.0, 110.0) if x < P_RUN_NEVER + P_RUN_SLOW else self.runs.uniform(5.0, 12.0)

    def write(self) -> float:  # a line of small talk
        return self.fixed["write"].pop(0) if self.fixed.get("write") else self.writes.uniform(0.5, 1.5)

    # -- a mouth --
    def open(self) -> float:  # a voice session opening: session_started 0.57-0.91 s, a third to 1.76, less a round trip
        return self.u("open", 0.35, 0.6, (0.33, 0.7, 1.4))

    def first_audio(self) -> float:  # an order -> its first audio (the second permit): 0.27-1.12 s
        return self.u("first_audio", 0.27, 0.5, (0.1, 0.5, 1.1))

    def unit(self, text: str) -> float:  # audible seconds of one sentence: "Got it." 1.5 s, 93 chars 6.9 s
        if self.fixed.get("unit:" + text):
            return self.fixed["unit:" + text].pop(0)
        return max(1.2, 0.3 + 0.062 * len(text)) * self.rng.uniform(0.85, 1.15)

    # An order reached the rig's ears 0.37 s (median) after its first audio frame left: the model's quiet
    # lead-in, then the SFU and the rig's jitter buffer. A stop reached it in about 0.15 s: the second part alone.
    def lead(self) -> float:
        return self.u("lead", 0.05, 0.35, (0.15, 0.35, 1.2))

    def hear(self) -> float:
        return self.u("hear", 0.1, 0.25)

    def trailing(self) -> float:  # silence the model sends after the last word, before its receipt
        return self.u("trailing", 0.0, 0.5, (0.2, 0.5, 1.5))

    def stop(self) -> float:  # an interrupt -> the audio has stopped
        return self.u("stop", 0.0, 0.05, (0.3, 0.05, 0.58))

    def silent(self) -> bool:
        if self.fixed.get("silent"):
            return self.fixed["silent"].pop(0)
        return self.rng.random() < self.p_silent


@dataclass
class Recording:
    """A call's inputs. `events`: {t, k, ...} lines as `huddle_agent.record` writes them, `t` in seconds on
    the recording's own clock. `zero`: that clock at scenario second 0. `says`: the rig's (name, text, start,
    end) for each scripted line, on the same clock (check.py needs to know when each line was said)."""

    events: list[dict]
    zero: float
    says: list[tuple[str, str, float, float]] = field(default_factory=list)
    fixed: dict[str, list] = field(default_factory=dict)  # Model overrides
    notes: list[str] = field(default_factory=list)
    line_seed: int | None = None  # which of the controller's fixed lines the call began with, when known

    def calls(self, method: str) -> list[dict]:
        return [e for e in self.events if e["k"] == "call" and e.get("m") == method]


@dataclass
class Evidence:
    """What a replay leaves behind, in the live rig's three formats."""

    t0: float  # scenario second 0
    rig: list[tuple[float, str, str]] = field(default_factory=list)
    emf: list[tuple[float, str, float, dict]] = field(default_factory=list)
    lines: list = field(default_factory=list)  # the controller's transcript
    errors: list[str] = field(default_factory=list)  # the controller raised
    notes: list[str] = field(default_factory=list)  # what the simulation could not match
    trace: list[tuple[float, str]] = field(default_factory=list)  # the controller's own log, on request
    # What the simulated backend did that a check may fail on (`Sim.route`, `Backend.turn`), counted:
    # ask_missed, steer, double_steer, people_taken, other_taken, run_slow, run_never, window_slow; and the worker's
    # own word on a slow run: told_status, told_gave_up.
    facts: dict[str, int] = field(default_factory=dict)

    def fact(self, name: str) -> None:
        self.facts[name] = self.facts.get(name, 0) + 1

    def write(self, d: str) -> tuple[str, str, str]:
        rig, worker, tr = (os.path.join(d, n) for n in ("rig.txt", "worker.log", "transcript.json"))
        with open(rig, "w") as f:
            for t, kind, detail in sorted(self.rig, key=lambda r: r[0]):
                wall = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                f.write(f"{t - self.t0:7.2f}  {wall}  {kind:<12} {detail}\n")
        os.utime(rig, (self.t0, self.t0))  # check.py dates a rig file with no worker log by its mtime
        with open(worker, "w") as f:
            for t, name, value, dims in self.emf:
                f.write(json.dumps({"_aws": {"Timestamp": int(t * 1000)}, **dims, name: value}) + "\n")
        with open(tr, "w") as f:  # routers/huddles.py `transcript`: one log message, one line per line
            text = "\n".join(f"[{x.mark} @{int(x.at)}] {x.name}{' → ' + x.to if x.to else ''}: {x.text}" for x in self.lines)
            json.dump(["live transcript room=replay\n" + text], f)
        return rig, worker, tr


def judge(scenario: dict, ev: Evidence, keep: str | None = None) -> list[tuple[str, str, str]]:
    """The scenario's `expect` block, checked by the rig's own check.py: [(PASS|FAIL|SKIP, check, evidence)]."""
    with tempfile.TemporaryDirectory() as tmp:
        if keep:
            os.makedirs(keep, exist_ok=True)
        results = check.Run(scenario, *ev.write(keep or tmp)).evaluate(scenario.get("expect", []))
    if ev.errors:  # a replay that the controller crashed in proves nothing by its silence
        results.append(("FAIL", "controller raised", " | ".join(ev.errors[:3])))
    return results


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9$']+", s.lower()))


class Mouth:
    """A driven voice as the controller sees it (`huddle.Live`), after agent.py `_DrivenLive`: orders run one
    after the other, each is said a sentence at a time with the controller's permission asked before the
    sentence and again as its first audio is about to go out, each sentence ends in a receipt, each order in
    one. An interrupt stops the audio and drops what had not started."""

    def __init__(self, sim: Sim, agent_id: str, n: int) -> None:
        self.agent_id, self.track_name = agent_id, f"agent-{agent_id}"
        self.sim, self.track = sim, f"agent-{agent_id}#{n:04d}"  # a new track (sid) for each session
        self._tail: asyncio.Task | None = None
        self._epoch, self._cut = 0, asyncio.Event()
        self.lag = sim.model.hear()  # one connection, one delay: its audio reaches the rig in the order sent
        self._opened, self._closed = sim.now(), None
        self._long: str | None = None  # agent.py `_DrivenLive._long`: the unit stopped as long, free on its next try

    def _nothing(self, *a, **kw) -> None:  # it has no ears, and only an order makes it speak
        pass

    set_participant = append_thinking = append_commentary = append_instructions = mute = nudge = reply_to = _nothing

    def say(self, text: str, order: int | None = None, first: int = 0) -> None:
        prior, epoch, h = self._tail, self._epoch, self.sim.h

        async def play() -> None:
            if prior is not None:
                await asyncio.wait([prior])
            if self._closed is not None:
                return
            failed = False
            for index, unit in enumerate(units(text), first):
                if epoch != self._epoch or not h.may_say(self.agent_id, order, index):
                    break
                outcome = await self._speak(order, index, unit)
                if outcome != "said":
                    failed = outcome in ("silent", "long")
                    break
            h.on_playback_ended(self.agent_id, order, failed)

        self._tail = self.sim.spawn(play())

    async def _wait(self, seconds: float) -> bool:
        """True: interrupted before `seconds` passed."""
        try:
            await asyncio.wait_for(self._cut.wait(), max(seconds, 0.0))
            return True
        except asyncio.TimeoutError:
            return False

    async def _speak(self, order: int | None, index: int, unit: str) -> str:
        sim, h, m = self.sim, self.sim.h, self.sim.model
        began, loud, sound, lead = sim.now(), 0.0, 0.0, 0.0
        self._cut = asyncio.Event()
        if await self._wait(m.first_audio()):
            outcome = "cut"  # interrupted while the model was still generating: nothing went out
        elif not h.may_say(self.agent_id, order, index):
            await asyncio.sleep(0.1 + m.stop())
            outcome = "revoked"
        elif m.silent():  # quiet frames and no sound (0.8-1.8 s), or nothing at all until agent.py stops it
            took = min(FIRST_SOUND_S, m.u("silent_took", 0.8, 1.8, (0.25, FIRST_SOUND_S, FIRST_SOUND_S)))
            outcome = "cut" if await self._wait(took - (sim.now() - began)) else "silent"
        else:
            h.on_agent_state(self.agent_id, "speaking")
            at = sim.now()
            cut = await self._wait(m.lead())
            lead = sim.now() - at
            # agent.py asks again before the first frame with sound in it: a person may have begun in the lead
            revoked = not cut and not h.may_say(self.agent_id, order, index)
            long = False
            if revoked:
                await asyncio.sleep(m.stop())
            elif not cut:
                h.on_unit_audible(self.agent_id, order, index)  # the first frame with sound in it
                sim.audio(self, True)
                heard = sim.now()
                # agent.py: sound past `sound_limit` stops the unit as long; its next try has no limit
                need, limit = m.unit(unit), float("inf") if unit == self._long else sound_limit(unit)
                cut = await self._wait(min(need, limit))
                long = not cut and need > limit
                if cut:
                    await asyncio.sleep(m.stop())
                loud = sim.now() - heard
                sim.audio(self, False)
                # the model's trailing silence is still the unit: an interrupt inside it is a cut unit,
                # though every word of it was heard
                cut = cut or (not long and await self._wait(m.trailing()))
            sound = sim.now() - at
            h.on_agent_state(self.agent_id, "listening")
            outcome = "revoked" if revoked else "cut" if cut else "long" if long else "said"
        # agent.py `_DrivenLive._speak`: the exemption holds only for that unit's own next try
        if outcome == "long":
            self._long = unit
        elif outcome == "said" or unit != self._long:
            self._long = None
        evidence = {
            "audioMs": round(sound * 1000), "loudMs": round(loud * 1000), "heardMs": round(loud * 1000),
            "leadMs": round(lead * 1000),
            "tookMs": round((sim.now() - began) * 1000),
        }  # fmt: skip
        h.on_unit(self.agent_id, order, index, unit, outcome, evidence)
        return outcome

    def interrupt(self) -> None:
        self._epoch += 1
        self._cut.set()

    def usage_seconds(self) -> float:
        return (self._closed if self._closed is not None else self.sim.now()) - self._opened

    async def aclose(self) -> None:
        if self._closed is None:
            self._closed = self.sim.now()
        self.interrupt()
        await asyncio.sleep(0.1)
        self.sim.h.on_live_closed(self)


class Room:
    def __init__(self) -> None:
        self.closed = False

    def publish(self, topic: str, payload: dict, to: list[str]) -> None:
        pass

    def set_attributes(self, attrs: dict[str, str]) -> None:
        pass

    def shutdown(self) -> None:
        self.closed = True


class Backend:
    """The backend's /internal/huddles routes, answered by the simulation after a modelled round trip."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self._runs: dict[str, dict] = {}  # run id -> {agent, text, done, handles}

    async def team(self) -> dict:
        await asyncio.sleep(self.sim.model.quick())
        return json.loads(json.dumps(self.sim.team))

    async def route(self, speaker, utterance, transcript, humans=None, **body) -> dict:
        latency, reply = await self.sim.route(speaker, utterance, transcript, humans, body)
        await asyncio.sleep(latency)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def summon(self, speaker, speaker_name=None) -> dict:
        took = self.sim.model.window()
        if took >= 2:
            self.sim.ev.fact("window_slow")
        await asyncio.sleep(took)
        return {"callId": "call-replay", "deadline": self.sim.now() + 3600, "budgetSeconds": 3600, "joined": False}

    async def session(self, agent_id, silent=False) -> dict:
        await asyncio.sleep(self.sim.model.quick())
        name = next(a["name"] for a in self.sim.team["agents"] if a["id"] == agent_id)
        return {"agentId": agent_id, "name": name, "voice": "marin", "persona": f"You are {name}.", "askName": False}

    async def turn(self, call_id, speaker, agent_id, run_id, utterance, transcript, steer_run_id=None, steer_only=False, **kw) -> dict:
        """routers/huddles.py `turn`: a steer goes into the run when the bridge still has it (turn.py
        `steer_run`: a running turn takes it and answers it with the rest; one that has ended refuses it).
        A steer that was not taken is dispatched as a run of its own, unless the worker said steer-only."""
        sim = self.sim
        await asyncio.sleep(sim.model.quick())
        loop = asyncio.get_running_loop()
        if steer_run_id:
            run = self._runs.get(steer_run_id)
            if run is not None and not run["done"] and "text" in run:
                run["text"] += " " + sim._answer(utterance)
                if run["handles"]:  # it has more to do before it ends
                    more = run["handles"][0].when() - loop.time() + sim.model.runs.uniform(2.0, 6.0)
                    run["handles"][0].cancel()
                    run["handles"] = [loop.call_later(more, self._end, steer_run_id)]
                return {"runId": steer_run_id, "agentId": agent_id, "steered": True}
        if steer_only:
            return {"runId": steer_run_id, "agentId": agent_id, "steered": False}
        send = lambda events: sim.h.on_data(None, h_mod.FRAME_TOPIC, json.dumps({"events": events}).encode())  # noqa: E731
        if sim.rec and sim._turns:
            self._runs[run_id] = {"done": False, "handles": [loop.call_later(d, send, ev) for d, ev in sim.frames(agent_id, run_id, utterance)]}
            return {"runId": run_id, "agentId": agent_id, "steered": False}
        took = sim.model.run()  # None: it never ends, and sends nothing
        if took is None or took >= 45:
            sim.ev.fact("run_never" if took is None else "run_slow")
        run = self._runs[run_id] = {"agent": agent_id, "text": sim._answer(utterance), "done": False, "handles": []}
        if took is not None:
            run["handles"] = [loop.call_later(took, self._end, run_id)]
        return {"runId": run_id, "agentId": agent_id, "steered": False}

    def _end(self, run_id: str) -> None:
        run = self._runs[run_id]
        run["done"] = True
        frame = lambda f: {"type": "voice.run.frame", "payload": {"runId": run_id, "agentId": run["agent"], "frame": {**f, "runId": run_id, "agentId": run["agent"]}}}  # noqa: E731
        events = [frame({"type": "chunk", "content": run["text"], "seq": 1}), frame({"type": "run_ended", "seq": 2})]
        self.sim.h.on_data(None, h_mod.FRAME_TOPIC, json.dumps({"events": events}).encode())

    async def abort(self, run_id) -> dict:
        run = self._runs.pop(run_id, None) or {"handles": []}
        for handle in run["handles"]:  # an aborted run sends no answer
            handle.cancel()
        return {"aborted": True}

    async def _ok(self, *a, **kw) -> dict:
        return {}

    # (a late answer is written to the room: nobody hears it, so no check sees it)
    no_post = late_answer = end = presence = transcript = leave_meeting = aclose = _ok


class Sim:
    def __init__(
        self, scenario: dict, routing: dict[int, dict] | None = None, seed: int | str = 0,
        recording: Recording | None = None, trace: bool = False, tape: str | None = None, profile: str = "current",
        noise: float = 0.0,
    ) -> None:  # fmt: skip
        """`tape`: write this call's inputs there with the worker's own recorder (`huddle_agent.record`)."""
        self.s, self.routing, self.rec, self.trace, self.tape = scenario, routing or {}, recording, trace, tape
        self.model = Model(seed, recording.fixed if recording else None, profile=profile, noise=noise)
        people = scenario["people"]
        self.ident = {pid: f"meet:replay:rig{pid}" for pid in people}
        self.people = {self.ident[pid]: p["name"] for pid, p in people.items()}
        self.team = {
            "agents": [{"id": n.lower(), "name": n, "listenMode": "active", "defaultName": False} for n in ROSTER],
            "people": [{"userId": "u1", "name": "Prasiddha"}],
            "ceoId": "friday",
        }
        self.ev = Evidence(EPOCH + LEAD)
        self.level: set[str] = set()  # the SFU's active speakers
        self.talking: set[str] = set()  # names whose scripted line is playing (the rig's `human_speaking`)
        self.bursts: dict[str, dict] = {}  # track -> its open audio burst, as the rig hears it
        self.agent_starts, self.ever_loud = 0, False
        self.router = Router(self, self.routing, seed)
        self._steered: dict[int | None, int] = {}  # scenario line -> how often /route steered a run with it
        self._tasks: set[asyncio.Task] = set()
        self._mouths = 0
        self._kept: dict[str, Mouth] = {}  # agent.py `start_live`: an agent's mouth is opened once in a call
        if recording:
            self._routes = {}
            for c in recording.calls("route"):
                self._routes.setdefault(c["args"].get("utterance", ""), []).append(c)
            self._written = iter([e for e in recording.events if e["k"] == "write"])
            self._turns = {}
            for c in recording.calls("turn"):
                self._turns.setdefault(c["args"].get("agent_id"), []).append(c)

    # -- the clock and the evidence --

    def now(self) -> float:
        return EPOCH + self.loop.time()

    def rel(self) -> float:
        return self.now() - self.ev.t0

    def rig(self, kind: str, detail: str, at: float | None = None) -> None:
        self.ev.rig.append((self.now() if at is None else at, kind, detail))

    def spawn(self, coro) -> asyncio.Task:
        t = self.loop.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._done)
        return t

    def _done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            self.ev.errors.append(f"simulation task: {t.exception()!r}")

    def at(self, t: float, fn, *args) -> None:
        self.loop.call_at(max(t - EPOCH, self.loop.time()), fn, *args)

    def audio(self, mouth: Mouth, on: bool) -> None:
        """A mouth's audible audio starts or stops. The rig hears it a moment later (the SFU and its own
        jitter buffer), and everything the rig knows of the agents, and reacts to, is on that delay."""
        self.loop.call_later(mouth.lag, self._heard, mouth.track, on)

    def _heard(self, track: str, on: bool) -> None:
        """The rig hears an agent track start or stop; bursts are cut as rig.py cuts them."""
        b = self.bursts.get(track)
        if on:
            self.ever_loud = True
            if b is not None:
                b["end"].cancel()  # sound again inside RIG_QUIET_S: the same burst
                return
            self.bursts[track] = {"start": self.now()}
            self.agent_starts += 1
            self.rig("AGENT_START", f"{track} (people talking: {', '.join(sorted(self.talking)) or '-'})")
            return
        b["last"] = self.now()
        b["end"] = self.loop.call_later(RIG_QUIET_S, self._burst_over, track)

    def _burst_over(self, track: str) -> None:
        b = self.bursts.pop(track)
        self.rig("AGENT_END", f"{track} spoke {b['last'] - b['start']:.1f}s")

    # -- the people --

    def _level(self, who: str, on: bool) -> None:
        (self.level.add if on else self.level.discard)(who)
        self.h.on_active_speakers(set(self.level))

    def _final(self, who: str, text: str) -> None:
        self.spawn(self.h.on_final_transcript(who, self.people[who], text))

    def _stretch(self, who: str, text: str, s: float, e: float) -> None:
        """One unbroken stretch of a person's speech, `s` to `e`: what the SFU, the transcriber's speech
        detector and its transcripts make of it, each on its own delay."""
        m = self.model
        self.at(s + m.level_on(), self._level, who, True)
        for a, b in m.level_gaps(s, e):
            self.at(a, self._level, who, False)
            self.at(b, self._level, who, True)
        self.at(e + m.level_off(), self._level, who, False)
        begin, heard = s, s
        for piece, share in m.pieces(text):
            end = s + (e - s) * share
            # a detector that stopped at a pause hears the rest a moment after, never before its own stop
            on = max(begin + m.voiced_on(), heard + 0.05)
            heard = end + (m.voiced_off() if share == 1.0 or m.fast else m.u("split_off", 0.45, 0.8))
            self.at(on, self.h.on_speech, who, True)
            self.at(on + m.interim(), self.h.on_interim_transcript, who, self.people[who], " ".join(piece.split()[:2]))
            self.at(heard, self.h.on_speech, who, False)
            self.at(max(heard + m.final_gap(), end + m.final()), self._final, who, piece)
            begin = end

    async def _say(self, i: int, line: dict) -> None:
        who, name = self.ident[line["who"]], self.s["people"][line["who"]]["name"]
        self.talking.add(name)
        self.rig("SAY", f"{name}: {line['say']}")
        parts = re.split(r"\s*\[\[slnc (\d+)\]\]\s*", line["say"])  # `say`'s silence command, in ms
        t = self.now()
        for text, gap in zip(parts[::2], [*parts[1::2], "0"]):
            if text.strip():
                self._stretch(who, text, t, t + self.model.line_s(text))
                t += self.model.line_s(text)
            t += int(gap) / 1000
        await asyncio.sleep(t - self.now())
        self.talking.discard(name)
        self.rig("SAID", name)

    async def _line(self, i: int, line: dict, prev: asyncio.Task | None) -> None:
        """rig.py `run_line`: on the clock, or a set time after the agents go quiet, or into their next speech."""
        if prev is not None:
            await prev
        if "at" in line:
            await asyncio.sleep(max(0.0, line["at"] - self.rel()))
        elif "after_agent" in line:
            while not self.ever_loud or self.bursts:
                await asyncio.sleep(0.05)
            await asyncio.sleep(line["after_agent"])
        elif "over_agent" in line:
            seen = self.agent_starts
            while self.agent_starts == seen:
                await asyncio.sleep(0.05)
            await asyncio.sleep(line["over_agent"])
        async with self._mics[line["who"]]:  # one microphone each: a person's lines play one after the other
            await self._say(i, line)

    async def _noise(self, who: str, name: str) -> None:
        """Sound that is not speech on a person's track while they say nothing (`Model.noise`)."""
        rng = self.model.noises
        while True:
            await asyncio.sleep(rng.expovariate(self.model.noise))
            if name in self.talking or who in self.level:
                continue
            self._level(who, True)
            await asyncio.sleep(rng.uniform(0.15, 0.8))
            if name not in self.talking:  # (they began to speak inside it: their own line ends the level)
                self._level(who, False)

    async def _script(self) -> None:
        """The scenario's lines, as rig.py plays them."""
        self._mics = {pid: asyncio.Lock() for pid in self.s["people"]}
        if self.model.noise:
            for who, name in self.people.items():
                self.spawn(self._noise(who, name))
        prev, pending = None, []
        for i, line in enumerate(self.s["lines"]):
            prev = self.spawn(self._line(i, line, None if "at" in line else prev))
            pending.append(prev)
        try:
            await asyncio.wait_for(asyncio.gather(*pending), self.s.get("timeout", 180))
        except asyncio.TimeoutError:
            self.rig("TIMEOUT", "a scripted line never ran (the agent never spoke?)")
        await asyncio.sleep(self.s.get("tail", 25))

    async def _feed(self) -> None:
        """A recording's people, at their recorded times."""
        rec, h, last = self.rec, self.h, 0.0
        virtual = lambda t: max(self.ev.t0 + t - rec.zero, EPOCH)  # noqa: E731
        for name, text, s, e in rec.says:
            self.rig("SAY", f"{name}: {text}", virtual(s))
            self.rig("SAID", name, virtual(e))
            self.at(virtual(s), self.talking.add, name)
            self.at(virtual(e), self.talking.discard, name)
            last = max(last, virtual(e))
        for e in rec.events:
            k, t = e["k"], virtual(e["t"])
            if k == "on_active_speakers":
                self.at(t, h.on_active_speakers, {i for i in e["identities"]})
            elif k == "on_speech":
                self.at(t, h.on_speech, e["identity"], e["started"])
            elif k == "on_interim_transcript":
                self.at(t, h.on_interim_transcript, e["identity"], e["name"], e["text"])
            elif k == "on_final_transcript":
                self.at(t, lambda e=e: self.spawn(h.on_final_transcript(e["identity"], e["name"], e["text"])))
            elif k == "stt_stopped":
                self.at(t, h.stt_stopped, e["identity"])
            elif k == "on_participants":
                self.at(t, lambda e=e: self.spawn(h.on_participants(dict(e["people"]))))
            elif k == "on_data" and e.get("topic") == h_mod.SPEAKER_TOPIC and e.get("sender") is None:
                self.at(t, h.on_data, None, e["topic"], json.dumps(e["data"]).encode())
            else:
                continue
            last = max(last, t)
        if rec.says:  # the rig leaves `tail` after its last line: what the worker logged after that is the leaving
            last = max(virtual(e) for *_, e in rec.says)
        await asyncio.sleep(last + self.s.get("tail", 25) - self.now())

    # -- the backend's answers --

    def _line_of(self, speaker: str, utterance: str) -> int | None:
        """The scenario line an utterance is the transcript of. Lines joined by the controller are their last
        line (the one that completed the sentence); a piece of a split line is that line."""
        said, best = _words(utterance), None
        for i, line in enumerate(self.s["lines"]):
            if self.s["people"][line["who"]]["name"] != speaker:
                continue
            w = _words(re.sub(r"\[\[.*?\]\]", " ", line["say"]))
            # the most of its words explained, then the closest fit ("Friday?" is not "Friday, go ahead.")
            score = (len(w & said), len(w & said) / len(w | said), i)
            if score[0] * 2 >= min(len(w), len(said)) and (best is None or score > best):
                best = score
        return best and best[2]

    async def route(self, speaker: str, utterance: str, transcript: str, humans, body: dict) -> tuple[float, dict | Exception]:
        if self.rec and self._routes:
            # The router's real answers, each used once: the one for these words, else for the nearest words
            # (a changed controller may join or strip a line differently).
            said = _words(utterance)
            near = lambda t: len(_words(t) & said) / max(len(_words(t) | said), 1)  # noqa: E731
            left = [t for t, calls in self._routes.items() if calls]
            text = utterance if utterance in left else max(left, key=near, default=None)
            if text is not None and (text == utterance or near(text) >= 0.5):
                c = self._routes[text].pop(0)
                err = c.get("error")
                return c["ms"] / 1000, BackendError(err["status"], err["code"], None) if err else c["reply"]
            self.ev.notes.append(f"no recorded /route answer for {utterance[:80]!r}: answered as asking nothing")
        # the backend's own decision on this body, as the room is now; its answer comes a round trip later
        latency = self.model.route()
        reply = await self.router.route(speaker, utterance, transcript, humans, body)
        i, v = self.router.jev.verdict(speaker, utterance)
        ops = reply["ops"]
        if "ask" in v and not (ops["requests"] or ops["steer"] or reply.get("hold") or body.get("unclaimed")):
            self.ev.fact("ask_missed")  # (an unclaimed question's own verdict is `by: room`, decided when nobody took it)
        if ops["steer"]:
            self.ev.fact("steer")
            self._steered[i] = self._steered.get(i, 0) + 1
            if self._steered[i] == 2:
                self.ev.fact("double_steer")
        if "ask" not in v and ops["requests"]:
            self.ev.fact("people_taken" if v.get("people") else "other_taken")
        if self.trace:
            self.ev.trace.append((self.rel(), f"/route {utterance[:60]!r} -> {json.dumps({'agentIds': reply['agentIds'], **{k: v for k, v in reply['ops'].items() if v}})}"))
        return latency, reply

    def _answer(self, task: str) -> str:
        """What a run answers to `task` (the router's own words for the request, a stretch of the line): the
        answer of the scenario line it was said in, the latest when two fit (a correction joined to its request)."""
        said, best = _words(task), None
        for i, line in enumerate(self.s["lines"]):
            for v in (self.routing.get(i, {}), self.routing.get(i, {}).get("else", {})):
                w = _words(re.sub(r"\[\[.*?\]\]", " ", line["say"]))
                if "answer" in v and len(w & said) * 2 >= len(said):
                    best = v["answer"]
        return ANSWERS[best] if best else "Done."

    def frames(self, agent_id: str, run_id: str, utterance: str) -> list[tuple[float, list[dict]]]:
        """A run's frames, as [(seconds after /turns answered, the packet's events)]."""
        if self.rec and self._turns:  # the recorded run's own frames, under this replay's run id
            if not (calls := self._turns.get(agent_id)):
                self.ev.notes.append(f"no recorded run for {agent_id}: {utterance[:60]!r} (it never answers)")
                return []
            c = calls.pop(0)
            was = c["args"]["run_id"]
            return [
                (max(e["t"] - c["t"], 0.0), json.loads(json.dumps(e["data"]["events"]).replace(was, run_id)))
                for e in self.rec.events
                if e["k"] == "on_data" and e.get("topic") == h_mod.FRAME_TOPIC and was in json.dumps(e["data"])
            ]
        raise AssertionError("a synthetic run is the Backend's (`turn`, `_end`)")

    async def _write(self, persona: str, event: str) -> str:
        """A mouth's one sentence of small talk: the scenario line's `reply`, after a writer's delay."""
        await asyncio.sleep(self.model.write())
        if self.rec and (w := next(self._written, None)):
            return w.get("reply") or ""
        said = _words(event)
        for i, line in enumerate(self.s["lines"]):
            if "reply" in self.routing.get(i, {}) and _words(line["say"]) <= said:
                return self.routing[i]["reply"]
        return ""

    async def _live(self, *, agent_id, persona, voice, speaker, seed) -> Mouth:
        opening = self.model.open()  # dealt also when it is not waited for: the replay's other delays stay as dealt
        if (m := self._kept.get(agent_id)) is not None and m._closed is not None:
            m._opened, m._closed = self.now(), None
            return m
        await asyncio.sleep(opening)
        self._mouths += 1
        m = self._kept[agent_id] = Mouth(self, agent_id, self._mouths)
        return m

    # -- the call --

    async def _call(self) -> None:
        tape = record.Recorder(self.tape, clock=self.loop.time) if self.tape else record._Off()
        self.h = h = Huddle(
            room=Room(), backend=tape.backend(Backend(self)), live_factory=tape.factory(self._live),
            clock=self.now, setting="meeting", writer=tape.writer(self._write),
        )  # fmt: skip
        tape.inputs(h)  # as agent.py wires it
        # which fixed line comes first: a real call draws it from its id
        h._line_seed = self.model.rng.randrange(1000)
        if self.rec and self.rec.line_seed is not None:
            h._line_seed = self.rec.line_seed

        async def ticks() -> None:
            while True:  # agent.py's loop
                await asyncio.sleep(1)
                await h.tick()

        ticking = self.spawn(ticks())
        if self.rec is None or not any(e["k"] == "on_participants" for e in self.rec.events):
            await h.on_participants(dict(self.people))
        if self.rec is None:
            await asyncio.sleep(LEAD - self.loop.time())
        self.rig("WORKER_READY", "worker listening; scenario clock starts", self.ev.t0)
        await (self._feed() if self.rec else self._script())
        ticking.cancel()
        for track, b in list(self.bursts.items()):
            if "last" in b:  # its audio ended inside the last RIG_QUIET_S. One still sounding has no end:
                b["end"].cancel()  # check.py closes it when the rig left, as for a live run.
                self._burst_over(track)
        self.ev.lines = h._transcript.after(-1)

    def run(self) -> Evidence:
        self.loop = loop = VirtualLoop()
        # a callback that raises is a broken replay, not a quiet one
        loop.set_exception_handler(lambda _loop, ctx: self.ev.errors.append(f"{ctx.get('message')}: {ctx.get('exception')!r}"))
        emit, level = h_mod.emit_metric, h_mod.logger.level
        tap = _Tap(self)
        def metric(name, value, unit="Seconds", dims=None) -> None:
            self.ev.emf.append((self.now(), name, value, dims or {}))
            if name == "live.request.overdue":  # the worker's own word on a slow run: a status line, or giving up
                self.ev.fact("told_" + dims["what"])

        h_mod.emit_metric = metric
        h_mod.logger.addHandler(tap)
        h_mod.logger.setLevel(logging.INFO if self.trace else logging.ERROR)
        try:
            loop.run_until_complete(self._call())
        finally:
            h_mod.emit_metric = emit
            h_mod.logger.removeHandler(tap)
            h_mod.logger.setLevel(level)
            left = asyncio.all_tasks(loop)
            for t in left:
                t.cancel()
            loop.run_until_complete(asyncio.wait(left)) if left else None
            loop.close()
        if self.rec:
            self.ev.notes += self.rec.notes
        return self.ev


class _Tap(logging.Handler):
    """The controller's log during one replay: its errors always, its decisions when a trace is wanted."""

    def __init__(self, sim: Sim) -> None:
        super().__init__(logging.INFO)
        self.sim = sim

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.levelno >= logging.ERROR:
            cause = f": {record.exc_info[1]!r}" if record.exc_info and record.exc_info[1] else ""
            self.sim.ev.errors.append(text + cause)
        if self.sim.trace:
            # the controller stamps its lines with the real monotonic clock, which is nothing here
            self.sim.ev.trace.append((self.sim.rel(), re.sub(r'"t": \d+, ', "", text)))
