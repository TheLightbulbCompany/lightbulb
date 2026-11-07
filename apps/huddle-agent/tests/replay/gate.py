"""The 17 scenarios against the one-voice meeting mode's gate (meet.Gate), offline.

    uv run python -m tests.replay.gate

The Gemini session cannot run here, so this judges only what the worker decides: when a reply may be heard.
It assumes the worst case for false answers: the model wants to reply after EVERY line (proactive audio
declining is a bonus on top). Every agent name in a scenario is the one assistant ("Nova"). A reply plays for
REPLY_S unless a person speaks into it (Gemini's barge-in). The order is the live one (7 Nov): the reply's
audio is ready MODEL_S after a line ends, but the line's transcript reaches the gate only TRANSCRIPT_LAG_S
after that, and the reply is held until then (meet.MeetAgent). Judged: `ack_within` (a reply starts within its
seconds of the line, with MODEL_S of model latency on top of the gate) and `silent_between` (no reply in the
window, except one owed to an ask made before it when the scenario allows one). Runs, words and answer text
need the model and are not judged.
"""

import os
import re
import sys

from huddle_agent import meet

from . import SCENARIOS, load
from .sim import ROSTER

NAME = "Nova"
WPS = 2.7  # spoken words a second
REPLY_S = 4.0
MODEL_S = 0.8  # ponytail: assumed Gemini first-audio latency after a line ends
TRANSCRIPT_LAG_S = float(os.environ.get("MEET_TRANSCRIPT_LAG_S", "1.9"))  # measured live 7 Nov: replies held 1.4-1.9 s
STEP = 0.1
_ROSTER = re.compile(r"\b(" + "|".join(ROSTER) + r")\b")


def timeline(scenario: dict) -> tuple[list[dict], list[dict]]:
    """[(line: start, end, who, text)], [(burst: start, end, line it answers)]."""
    gate = meet.Gate(
        name=NAME, people=[{"name": p["name"]} for p in scenario["people"].values()]
    )
    lines, bursts = [], []
    playing = None  # the burst being said
    heard: list[tuple[float, str, str, int]] = []  # transcripts in flight: (arrives, who, text, line index)
    asked_line = -1  # the line whose transcript opened the gate: what a reply answers
    t, i, quiet_since = 0.0, 0, 0.0
    todo = list(scenario["lines"])
    end_at = max((ln.get("at") or 0) for ln in todo) + scenario.get("tail", 20)
    while t < end_at:
        for h in [h for h in heard if t >= h[0]]:
            heard.remove(h)
            if gate.heard(h[1], h[2], t, new_turn=True) in meet.ADDRESSED:
                asked_line = h[3]
        if playing and t >= playing["end"]:
            gate.answered(playing["end"])
            playing = None
        line = todo[i] if i < len(todo) else None
        due = line is not None and (
            (line.get("at") is not None and t >= line["at"])
            or (
                "over_agent" in line
                and bursts
                and t >= bursts[-1]["start"] + line["over_agent"]
            )
        )
        if due:
            text = _ROSTER.sub(NAME, line["say"])
            start, end = t, t + len(text.split()) / WPS
            lines.append({"start": start, "end": end, "who": line["who"], "text": text})
            if playing:  # barge-in: a person talking over the reply cuts it
                playing["end"] = start
                gate.answered(start)
                playing = None
            heard.append((end + MODEL_S + TRANSCRIPT_LAG_S, line["who"], text, len(lines) - 1))
            quiet_since, t, i = end, end, i + 1
            continue
        if playing is None and (not lines or t >= lines[-1]["end"]):
            if gate.verdict(t, t - quiet_since) is True:
                begin = max(t, lines[asked_line]["end"] + MODEL_S)  # the held audio plays at once
                playing = {
                    "start": begin,
                    "end": begin + REPLY_S,
                    "line": asked_line,
                }
                bursts.append(playing)
                gate.ask_at = None  # this reply is the answer; no second one for it
        t = round(t + STEP, 3)
    return lines, bursts


def judge(name: str) -> list[tuple[str, str, str]]:
    scenario = load(name)
    lines, bursts = timeline(scenario)

    def at(k, edge, default):
        return default if k is None else lines[k][edge] if k < len(lines) else None

    out = []
    for e in scenario["expect"]:
        kind, s = next(iter(e.items()))
        if kind == "ack_within":
            end = at(s["line"], "end", None)
            b = next(
                (b for b in bursts if end is not None and b["start"] >= end - 0.5), None
            )
            ok = b is not None and b["start"] - end <= s["seconds"]
            out.append(
                (
                    "PASS" if ok else "FAIL",
                    kind,
                    f"line {s['line']}: "
                    + (f"+{b['start'] - end:.1f}s" if b else "no reply"),
                )
            )
        elif kind == "silent_between":
            ws = at(s.get("from"), s.get("from_edge", "start"), -1e9)
            we = (at(s.get("to"), s.get("to_edge", "start"), 1e9) or 1e9) + s.get(
                "to_plus", 0
            )
            frm = s.get("from") or 0
            bad = [
                b
                for b in bursts
                if ws <= b["start"] < we and not (s.get("allow") and b["line"] < frm)
            ]
            out.append(("FAIL" if bad else "PASS", kind, f"{len(bad)} reply(ies) in lines {s.get('from', 0)}..{s.get('to', 'end')}"
                        + (f", answering line(s) {[b['line'] for b in bad]}" if bad else "")))  # fmt: skip
    return out


def main() -> int:
    total = {"PASS": 0, "FAIL": 0}
    for name in SCENARIOS:
        for status, kind, ev in judge(name):
            total[status] += 1
            print(f"{status}  {name:22} {kind:15} {ev}")
    print(
        f"\n{total['PASS']} passed, {total['FAIL']} failed (gate only; model behaviour unverified offline)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
