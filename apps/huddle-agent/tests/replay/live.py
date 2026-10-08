"""Recordings of real calls: one made by the worker's recorder (`load`), or rebuilt from a live rig run's
evidence (`recording`), which is how the harness is checked against reality: the run is replayed and its
verdicts compared with the run's own report.txt.

    uv run python -m tests.replay gap --recording <file.jsonl> [--rig <rig.txt>]
    uv run python -m tests.replay --live <run dir> [<run dir> ...]

A run directory is what the rig's run_checked.sh leaves: scenario.json, rig.txt, worker.log, transcript.json
and report.txt. Those runs were made before the recorder existed, so the recording is rebuilt from the
evidence, and only as far as the evidence goes:

  from the run    when each person's speech was detected and heard (worker.log `speech` lines), when each
                  final transcript came and what it said (`turn` lines, transcript.json), /route latencies,
                  how long each run took, and for each sentence a mouth began, in order: how long its first
                  audio took, whether it made a sound, how long it lasted (`permit` and `receipt` lines)
  not in the run  what /route answered (the routing table's verdict is used: a correct router, assumed),
                  what each run answered (the table's answer), how long each voice took to open and the
                  rig to hear it (the timing model, so each run is replayed over several seeds)

A recording made by `huddle_agent.record` has all of it and needs none of this.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re

from huddle_agent import huddle as h_mod

from .check import RIG_LINE, load_transcripts
from .routing import ROUTING
from .sim import Recording, Sim, judge

SEEDS = 10


def _plain(s: str) -> str:
    return s.replace("’", "'")


def _line_seed(spoken: list[str]) -> int | None:
    """Where the call's fixed lines began (`Huddle._line_seed`): each set of them (acknowledgements, "still
    working", ...) is said in turn from that place, so the first line the call said of each set gives the
    seed up to the set's length, and all of them together give it. A replay that starts there says the
    same fixed lines in the same order. None: the call said none of them."""
    said = [_plain(t) for t in spoken]
    sets = [[h_mod.units(x)[0] for x in lines] for lines in (h_mod.ACKS, h_mod.STILL_WORKING, h_mod.QUEUED_LINES, h_mod.HERE, h_mod.COULD_NOT, h_mod.STEER_ACKS)]
    first = [(firsts, next(firsts.index(t) for t in said if t in firsts)) for firsts in sets if any(t in firsts for t in said)]
    fits = (seed for seed in range(math.lcm(*map(len, sets))) if all(seed % len(firsts) == i for firsts, i in first))
    return next(fits, None) if first else None


def _says(rig: str, scenario: dict) -> tuple[float | None, list[tuple[str, str, float, float]]]:
    """rig.txt's scripted lines: (when the scenario clock started, [(name, text, start, end)]), in seconds
    of the UTC day."""
    says, open_, zero = [], {}, None
    for ln in open(rig, errors="replace"):
        if not (g := RIG_LINE.match(ln)):
            continue
        t, kind, detail = int(g[2]) * 3600 + int(g[3]) * 60 + int(g[4]) + int(g[5]) / 1000, g[6], g[7].strip()
        if kind == "WORKER_READY":
            zero = t
        elif kind == "SAY":
            name, _, text = detail.partition(": ")
            # run_checked.sh cuts stdout at 170 columns: the scenario has the whole line
            whole = next((x["say"] for x in scenario["lines"] if x["say"].startswith(text.rstrip())), text)
            open_[name] = (name, whole, t)
        elif kind == "SAID" and detail in open_:
            says.append((*open_.pop(detail), t))
    return zero, says


def load(path: str, scenario: dict, rig: str | None = None) -> Recording:
    """A recording made by `huddle_agent.record` (HUDDLE_RECORD_DIR), with the mouth timings it holds.
    `rig`: the rig.txt of the same call, for when each scripted line was said: without it check.py has to
    estimate line times from the transcript and skips what needs audio timing."""
    with open(path) as f:
        events = [json.loads(ln) for ln in f if ln.strip()]
    start = next(e for e in events if e["k"] == "start")
    fixed: dict[str, list] = {k: [] for k in ("open", "first_audio", "lead", "trailing", "silent", "silent_took", "write")}
    asked: dict[tuple, list[float]] = {}
    for e in events:
        if e["k"] == "live" and e.get("ok"):
            fixed["open"].append(e["ms"] / 1000)
        elif e["k"] == "write":
            fixed["write"].append(e["ms"] / 1000)
        elif e["k"] == "may_say":
            asked.setdefault((e["agent_id"], e["order"], e["index"]), []).append(e["t"])
        elif e["k"] == "on_unit":
            at, proof = asked.get((e["agent_id"], e["order"], e["index"]), []), e.get("evidence") or {}
            fixed["first_audio"].append(at[1] - at[0] if len(at) > 1 else proof.get("tookMs", 400) / 1000)
            fixed["silent"].append(e["outcome"] == "silent")
            if e["outcome"] == "silent":
                fixed["silent_took"].append(proof.get("tookMs", 1000) / 1000)
            if e["outcome"] == "said":
                fixed.setdefault("unit:" + e["text"], []).append(proof.get("loudMs", 0) / 1000)
            if "leadMs" in proof and e["outcome"] in ("said", "cut") and proof.get("audioMs"):
                fixed["lead"].append(proof["leadMs"] / 1000)  # every sentence whose audio began, said or cut
                if e["outcome"] == "said":
                    fixed["trailing"].append(max(proof["audioMs"] - proof["loudMs"] - proof["leadMs"], 0) / 1000)
    zero, says = None, []
    if rig:
        # ponytail: one UTC day. A call recorded across midnight needs the date carried on rig.txt's lines.
        to_clock = lambda sod: start["t"] + sod - start["wall"] % 86400  # noqa: E731
        ready, said = _says(rig, scenario)
        zero, says = ready and to_clock(ready), [(n, text, to_clock(a), to_clock(b)) for n, text, a, b in said]
    spoken = [e["text"] for e in events if e["k"] == "on_unit"]
    return Recording(events, zero if zero is not None else start["t"], says, fixed, line_seed=_line_seed(spoken))


def recording(d: str) -> tuple[dict, Recording]:
    """(the run's scenario, its people and timings as a recording). Times are seconds of the UTC day, the
    one clock rig.txt and worker.log share."""
    with open(os.path.join(d, "scenario.json")) as f:
        scenario = json.load(f)
    by_name = {p["name"]: f"meet:replay:rig{pid}" for pid, p in scenario["people"].items()}
    notes: list[str] = []
    zero, says = _says(os.path.join(d, "rig.txt"), scenario)
    events, level, finals, receipts, created, ready, jev = [], set(), [], [], [], {}, []
    permits: dict[tuple, list[float]] = {}  # (order, unit) -> when the mouth asked: at the order, at its first audio
    mouth: dict[str, list] = {"first_audio": [], "silent": [], "silent_took": []}
    for ln in open(os.path.join(d, "worker.log"), errors="replace"):
        if not ln.startswith("{"):
            continue
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if "_aws" in j:
            if "live.request.created" in j:
                created.append(j["_aws"]["Timestamp"] / 1000 % 86400)
            if "live.latency.jev" in j:
                jev.append(j["live.latency.jev"] / 1000)
            continue
        g = re.match(r"huddle (speech|turn|gate|permit|receipt) (\{.*\})$", j.get("message", ""))
        if not g:
            continue
        at = dt.datetime.fromisoformat(j["timestamp"])
        t, f = at.hour * 3600 + at.minute * 60 + at.second + at.microsecond / 1e6, json.loads(g[2])
        who = "meet:replay:" + f["who"].rsplit(":", 1)[-1] if isinstance(f.get("who"), str) else None
        if g[1] == "speech" and f["src"] == "voiced":
            events.append({"t": t, "k": "on_speech", "identity": who, "started": f["on"]})
        elif g[1] == "speech" and f["src"] == "level":
            (level.add if f["on"] else level.discard)(who)
            events.append({"t": t, "k": "on_active_speakers", "identities": sorted(level)})
        elif g[1] == "turn" and f["event"] == "final":
            finals.append((t, at.timestamp(), who))
        elif g[1] == "permit" and f["ok"]:
            permits.setdefault((f["order"], f["unit"]), []).append(t)
        elif g[1] == "receipt":
            # One receipt for each sentence a mouth began, in order: how long its first audio took (when it
            # got that far), and whether it made a sound at all.
            asked = permits.get((f["order"], f["unit"]), [])
            mouth["first_audio"].append(asked[1] - asked[0] if len(asked) > 1 else f["tookMs"] / 1000)
            mouth["silent"].append(f["outcome"] == "silent")
            if f["outcome"] == "silent":
                mouth["silent_took"].append(f["tookMs"] / 1000)
            if f["outcome"] == "said":
                receipts.append(f["loudMs"] / 1000)
        elif g[1] == "gate" and f["kind"] == "answer":
            ready.setdefault(f["utterance"], t)  # the first the scheduler saw of an answer: its run had ended
    rows = (load_transcripts(os.path.join(d, "transcript.json")) or [[]])[-1]
    said = [r for r in rows if r["human"]]
    for t, wall, who in finals:
        # a final and its transcript line: the same person, the same second. A final with no line had no words.
        line = next((r for r in said if by_name.get(r["who"]) == who and abs(r["mark"] - int(wall)) <= 1), None)
        if line:
            said.remove(line)
        name = next(n for n, i in by_name.items() if i == who)
        events.append({"t": t, "k": "on_final_transcript", "identity": who, "name": name, "text": line["text"] if line else ""})
    if said:
        notes.append(f"{len(said)} transcript line(s) matched no final in worker.log and were not replayed")
    spoken = [r for r in rows if not r["human"]]
    # request made -> its answer ready, in order. A request with no answer by the end of the call never got one.
    runs = [b - a for a, b in zip(sorted(created), sorted(ready.values()))] + [1e9] * (len(created) - len(ready))
    fixed: dict[str, list] = {"route": jev, "run": runs, **mouth}
    if len(receipts) == len(spoken):  # each sentence said to its end is one receipt and one transcript line
        for r, seconds in zip(spoken, receipts):
            fixed.setdefault("unit:" + _plain(r["text"]), []).append(seconds)
    else:
        notes.append(f"{len(receipts)} receipts for {len(spoken)} agent lines: sentence lengths are modelled")
    events.sort(key=lambda e: e["t"])
    zero = zero if zero is not None else (says[0][2] if says else 0.0)
    return scenario, Recording(events, zero, says, fixed, notes, _line_seed([r["text"] for r in spoken]))


def verdicts(report: str) -> list[tuple[str, str]]:
    """[(PASS|FAIL|SKIP, the check)] of a report.txt, in the order of the scenario's `expect` block."""
    out, on = [], False
    for ln in open(report, errors="replace"):
        on = on or ln.startswith("== assertions")
        if on and (g := re.match(r"(PASS|FAIL|SKIP)  (.*)$", ln)):
            out.append((g[1], g[2].split("  #")[0]))
    return out


def compare(dirs: list[str], seeds: int = SEEDS) -> int:
    agree = total = 0
    for d in dirs:
        base = os.path.basename(d.rstrip("/"))
        name = base.split("-", 1)[-1]
        if not all(os.path.exists(os.path.join(d, f)) for f in ("report.txt", "rig.txt", "worker.log", "transcript.json")):
            print(f"== {base}: not a finished run, skipped")
            continue
        scenario, rec = recording(d)
        live, runs = verdicts(os.path.join(d, "report.txt")), []
        for seed in range(seeds):
            fresh = Recording(rec.events, rec.zero, rec.says, {k: list(v) for k, v in rec.fixed.items()}, list(rec.notes), rec.line_seed)
            runs.append(judge(scenario, Sim(scenario, ROUTING.get(name, {}), f"{name}:{seed}", recording=fresh).run()))
        print(f"== {base}")
        for i, (status, what) in enumerate(live):
            got = [r[i][0] for r in runs]
            same = sum(g == status for g in got)
            total += 1
            agree += same * 2 > len(got)
            flag = "  " if same == len(got) else "~ " if same * 2 > len(got) else "!!"
            tally = " ".join(f"{k} {got.count(k)}" for k in ("PASS", "FAIL", "SKIP") if got.count(k))
            print(f"  {flag} live {status}  replay {tally}  {what[:90]}")
            if same * 2 <= len(got):
                print(f"       replay: {next(r[i] for r in runs if r[i][0] != status)[2][:260]}")
        for n in rec.notes:
            print("  NOTE", n)
    print(f"{agree}/{total} checks: the replay (most of {seeds} seeds) and the live report agree")
    return 0
