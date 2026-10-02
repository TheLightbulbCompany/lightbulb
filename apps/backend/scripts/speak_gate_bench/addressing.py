"""Experiment: an explicit ADDRESSING stage in front of chat routing (bench only, no prod path).

Runs on a human's latest message in a channel, before ``select_dispatch_targets`` / Jev:

1. Candidates (deterministic, recall-first): every agent name and every person's name or first name
   found anywhere in the text (word boundary, case-insensitive, @handles included); voice adds
   ``huddle_agent.transcript``'s fuzzy/soundex match.
2. One Jev call (``jev.decide``: same gateway, model and zero-data-retention options) labelling each
   candidate ``addressed`` / ``relayed`` / ``mentioned`` / ``not_a_name``, plus two booleans:
   ``complete`` (a finished thought) and ``anyone`` (a request put to the room beyond the named).
   Jev generates no text, so the brief's ``requests: [{text, to}]`` split is reported as
   ``to`` only: one entry per addressed/relayed candidate, plus ``anyone``.
   FAST PATH, no call: today's opening-name grammar (``messaging._NAME_GREETING`` + name +
   ``_NAME_FOLLOW``) names exactly one agent, no other candidate is in the text, >= 4 words,
   terminal punctuation (voice: every bench line is a final transcript, so no punctuation test),
   and the name is neither a weekday nor shared with a person.
3. Decision: P(complete) < 0.5 -> hold (nobody). Else addressed/relayed agents respond (a name an
   agent shares with a person goes to the person unless the text opens "hey/ok/okay <name>").
   Else fall through to the unchanged Jev path, with "Author -> Target" on the lines Jev sees.
4. Fallback (Jev error / 2 s timeout / forced): every candidate agent name is addressed except
   weekday names; a name shared with a person goes to the person; a plain person name is "mentioned"
   (marking it addressed would trip the 0.7 to-a-person rule and silence ambient Jev picks).
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import time
from pathlib import Path

import os

os.environ.setdefault("CLERK_ISSUER", "https://bench.clerk.accounts.dev")

from core.services import jev, messaging  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "huddle_transcript", Path(__file__).parents[3] / "huddle-agent/src/huddle_agent/transcript.py"
)
_transcript = sys.modules["huddle_transcript"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_transcript)

TIMEOUT_S = 2.0
HOLD_BELOW = 0.5
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
_STRONG_LEAD = r"\s*(?:hey|ok|okay)[\s,]+@?"

INSTRUCTIONS = (
    "People and AI teammates share this chat. For the one name given, decide how the LATEST message "
    "uses it. addressed: the message speaks TO them, at the start, middle or end: calls, greets, asks "
    "or tells them something ('Friday, ...', 'Hey guys, hey Friday', 'What do you think, Finance?'). "
    "relayed: someone else is asked to get them to act, or they are proposed for the job ('@leo can "
    "you get Finance to tell us the runway?': Finance relayed, Leo addressed; 'Leo wants to know from "
    "Pulse ...': Pulse relayed; 'let's have Pulse draft it': Pulse relayed). mentioned: talked about, "
    "thanked, or told NOT to act, with nothing asked of them ('Friday said it was fine', 'thanks "
    "Friday', 'Not you Friday, Atlas, can you check?': Friday mentioned, Atlas addressed). "
    "not_a_name: the word is not this teammate or person here, but a weekday, a date or a common "
    "word ('Friday, we ship the beta' is the day; 'by Friday'; 'Pulse check'; 'Forge ahead')."
)
LABELS = {
    "addressed": "Spoken to: called, greeted, asked or told something",
    "relayed": "Someone else is asked to get them to do it, or they are proposed to do it",
    "mentioned": "Talked about, thanked, or told not to act; nothing asked of them",
    "not_a_name": "Not this teammate or person: a weekday, date or common word",
}
COMPLETE = (
    "Is the latest message a finished thought? No only when the speaker stopped mid-sentence and is "
    "still typing or talking ('Friday, we're trying to', 'we were hoping someone could', 'so the "
    "thing is'). Short messages ('hey', 'thanks Friday', 'Friday, are you there?') are finished."
)
ANYONE = "Besides anyone it names, does the latest message ask the room or anyone at all for something?"


def _names(member: dict) -> set[str]:
    names = {member["displayName"], member.get("mentionHandle") or ""}
    if member["kind"] == "human":
        names.add(member["displayName"].split()[0])
    return {n for n in names if n}


def candidates(text: str, members: list[dict], voice: bool) -> list[dict]:
    """``[{name, agents, humans, pos}]`` in text order; one entry per distinct name said."""
    found: dict[str, dict] = {}
    tokens = _transcript._tokens(text) if voice else []
    for m in members:
        for name in _names(m):
            hit = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I)
            pos = hit.start() if hit else None
            if pos is None and voice:
                words = tuple(_transcript._tokens(name))
                pos = next(
                    (
                        i * 1000
                        for i in range(len(tokens))
                        if _transcript._fuzzy(tuple(tokens[i : i + len(words)]), words)
                    ),
                    None,
                )  # ponytail: token index as position; fine for ordering, voice only
            if pos is None:
                continue
            key = m["displayName"].split()[0].lower() if m["kind"] == "human" else m["displayName"].lower()
            row = found.setdefault(key, {"name": key, "agents": [], "humans": [], "pos": pos})
            row["pos"] = min(row["pos"], pos)
            if m["id"] not in row["agents"] + row["humans"]:
                row["agents" if m["kind"] == "agent" else "humans"].append(m["id"])
            break
    # A person's first name that is also an agent's name is one candidate, both kinds.
    return sorted(found.values(), key=lambda r: r["pos"])


def _opens_with(text: str, name: str, *, strong: bool = False) -> bool:
    lead = _STRONG_LEAD if strong else rf"\s*{messaging._NAME_GREETING}@?"
    follow = r"(?![\w-])" if strong else rf"(?![\w-]){messaging._NAME_FOLLOW}"
    return re.match(rf"{lead}{re.escape(name)}{follow}", text, re.I) is not None


def fast_path(text: str, cands: list[dict], voice: bool) -> str | None:
    if len(cands) != 1 or len(cands[0]["agents"]) != 1 or cands[0]["humans"] or cands[0]["name"] in WEEKDAYS:
        return None
    if len(text.split()) < 4 or (not voice and not re.search(r"[.?!]\s*$", text)):
        return None
    return cands[0]["agents"][0] if _opens_with(text, cands[0]["name"]) else None


def _state(state: dict, voice: bool) -> dict:
    people = [m["displayName"] for m in state["members"] if m["kind"] == "human"]
    agents = [f"{a['displayName']} ({a['role']})" for a in state["agents"]]
    recent = [jev._line(m) for m in state["history"][-6:]]
    return {
        "room": {"kind": "call" if voice else "chat", "people": people, "aiTeammates": agents},
        "recent": recent,
        "latest": jev._line(state["latest"]),
    }


async def classify(state: dict, cands: list[dict], voice: bool) -> dict | None:
    """The one Jev call: ``{labels: {name: (label, p)}, complete: p, anyone: p}`` or None."""
    questions = {
        "complete": {"type": "boolean", "instructions": COMPLETE},
        "anyone": {"type": "boolean", "instructions": ANYONE},
    }
    for i, c in enumerate(cands):
        kind = (
            "both a person and an AI teammate here"
            if c["agents"] and c["humans"]
            else "an AI teammate"
            if c["agents"]
            else "a person in the room"
        )
        questions[f"n{i}"] = {
            "type": "choice",
            "instructions": f"{INSTRUCTIONS} The name: '{c['name']}' ({kind}).",
            "criteria": LABELS,
        }
    answers = await jev.decide(_state(state, voice), questions)
    if not isinstance(answers, dict):
        return None
    try:
        labels = {
            c["name"]: (
                answers[f"n{i}"]["choice"],
                round(float(answers[f"n{i}"]["probabilities"][answers[f"n{i}"]["choice"]]), 2),
            )
            for i, c in enumerate(cands)
        }
        return {
            "labels": labels,
            "complete": float(answers["complete"]["probability"]),
            "anyone": float(answers["anyone"]["probability"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def stage(case: dict, state: dict, mode: str) -> dict:
    """``{path, agents (ordered), humans, hold, labels, ms}``. ``mode``: "on" | "fallback"."""
    text, voice = state["latest"]["text"], case["variant"] == "voice"
    cands = candidates(text, state["members"], voice)
    out = {"path": "none", "agents": [], "humans": [], "hold": False, "labels": {}, "ms": None, "requests": []}
    if not cands:
        return out
    if (agent := fast_path(text, cands, voice)) is not None:
        return out | {"path": "fast", "agents": [agent], "requests": [{"to": cands[0]["name"]}]}
    result = None
    if mode == "on":
        started = time.perf_counter()
        for attempt in range(3):
            try:
                result = await asyncio.wait_for(classify(state, cands, voice), TIMEOUT_S)
            except asyncio.TimeoutError:
                out["path"] = "timeout"
                break
            if result is not None:
                break
            await asyncio.sleep(2**attempt)  # a 429/5xx: the bench measures the model, not the quota
        out["ms"] = round((time.perf_counter() - started) * 1000)
    if result is None:
        out["path"] = "fallback" if mode == "fallback" else out["path"] if out["path"] == "timeout" else "error"
        result = {
            "labels": {
                c["name"]: (
                    "not_a_name" if c["name"] in WEEKDAYS else "addressed" if c["agents"] else "mentioned",
                    None,
                )
                for c in cands
            },
            "complete": 1.0,
            "anyone": None,
        }
    else:
        out["path"] = "llm"
    out["labels"], out["complete"], out["anyone"] = result["labels"], result["complete"], result["anyone"]
    out["hold"] = result["complete"] < HOLD_BELOW
    for c in cands:
        label = result["labels"][c["name"]][0]
        if label not in ("addressed", "relayed"):
            continue
        out["requests"].append({"to": c["name"], "label": label})
        # A name a person shares goes to the person unless "hey <name>" (the owner's rule).
        if c["agents"] and (not c["humans"] or (label == "addressed" and _opens_with(text, c["name"], strong=True))):
            out["agents"] += [a for a in c["agents"] if a not in out["agents"]]
        elif label == "addressed":
            out["humans"] += c["humans"]
    if result["anyone"] is not None and result["anyone"] >= 0.5:
        out["requests"].append({"to": "anyone"})
    return out


def annotate(state: dict, stage_out: dict | None) -> None:
    """ "Author -> Target" on what Jev sees: history by today's opening grammar + @handles,
    the latest by the stage's labels."""
    members = state["members"]
    by_id = {m["id"]: m["displayName"] for m in members}
    for msg in state["history"]:
        text = msg["text"]
        to = [m["displayName"] for m in members if any(_opens_with(text, n) for n in _names(m))]
        to += [
            m["displayName"]
            for m in members
            if m.get("mentionHandle") and re.search(rf"@{re.escape(m['mentionHandle'])}(?![\w-])", text, re.I)
        ]
        msg["_to"] = list(dict.fromkeys(to))
    if stage_out:
        state["latest"]["_to"] = [by_id[i] for i in stage_out["agents"] + stage_out["humans"]]


_orig_line = jev._line


def _line_with_target(message: dict) -> dict:
    line = _orig_line(message)
    if message.get("_to"):
        line["author"] += " -> " + ", ".join(message["_to"])
    return line


def install_annotations() -> None:
    jev._line = _line_with_target


# ------------------------------------------------------------------ the experiment's report
RUNS = {
    "a0": ("baseline (recorded 2026-10-02)", "2026-10-02"),
    "a": ("(a) baseline rerun", "exp-a"),
    "b": ("(b) addressing stage", "exp-b"),
    "c": ("(c) stage, Jev call forced to fallback", "exp-c"),
    "d": ("(d) = (b) + re-asked question is a new request", "exp-d"),
}


def _slices(recs: list[dict], variant: str, first: bool = False) -> dict:
    from .multiparty import score

    rs = [r for r in recs if r["variant"] == variant]
    if first:
        rs = [{**r, "responders": r.get("responders_first", r["responders"])} for r in rs]
    no_cand = [r for r in rs if r["cue"] == "implicit" and r["id"] in NO_NAME_IDS]
    return {
        "cued recall": score([r for r in rs if r["cue"] == "cued"])["recall"],
        "cued false-speak": score([r for r in rs if r["cue"] == "cued"])["false_speak"],
        "C agent-named recall": score([r for r in rs if r["category"] == "C" and r["subtype"] == "agent-named"])[
            "recall"
        ],
        "B false-speak": score([r for r in rs if r["category"] == "B"])["false_speak"],
        "R recall": score([r for r in rs if r["category"] == "R"])["recall"],
        "R false-speak": score([r for r in rs if r["category"] == "R"])["false_speak"],
        "implicit recall": score([r for r in rs if r["cue"] == "implicit"])["recall"],
        "implicit, no name in text: recall": score(no_cand)["recall"],
        "overall recall": score(rs)["recall"],
        "overall false-speak": score(rs)["false_speak"],
        "precision": score(rs)["precision"],
    }


NO_NAME_IDS: set[str] = set()


def report_experiment(raw: Path) -> str:
    import json
    import statistics

    from .multiparty import _f as fmt
    from .multiparty import build, load, report

    # Rows whose latest line names nobody (text only), fixed across runs so before/after compare.
    NO_NAME_IDS.clear()
    NO_NAME_IDS.update(
        c["id"]
        for c in load()
        if not candidates(build(c)["latest"]["text"], build(c)["members"], c["variant"] == "voice")
    )
    loaded = {
        k: [json.loads(x) for x in (raw / f"{f}-multiparty.jsonl").read_text().splitlines()]
        for k, (_, f) in RUNS.items()
        if (raw / f"{f}-multiparty.jsonl").exists()
    }
    out = ["# Addressing stage experiment (bench only)", ""]
    cols = [k for k in RUNS if k in loaded]
    for variant in ("chat", "voice"):
        out += [
            f"## {variant}: before / after",
            "",
            "| metric | " + " | ".join(RUNS[k][0] for k in cols) + " |",
            "|---|" + "---|" * len(cols),
        ]
        tables = {k: _slices(loaded[k], variant) for k in cols}
        out += [f"| {m} | " + " | ".join(fmt(tables[k][m]) for k in cols) + " |" for m in tables[cols[0]]]
        out.append("")
    staged = [k for k in cols if k in ("b", "c", "d")]
    if staged:
        out += [
            "## chat: one-responder policy (first addressed only) vs all addressed",
            "",
            "| metric | " + " | ".join(f"{RUNS[k][0]}: all / first" for k in staged) + " |",
            "|---|" + "---|" * len(staged),
        ]
        al = {k: _slices(loaded[k], "chat") for k in staged}
        fi = {k: _slices(loaded[k], "chat", first=True) for k in staged}
        out += [
            f"| {m} | " + " | ".join(f"{fmt(al[k][m])} / {fmt(fi[k][m])}" for k in staged) + " |" for m in al[staged[0]]
        ]
        out.append("")
        out += [
            "## Stage cost",
            "",
            "| run | stage ran (human, channel) | no candidate | fast path | Jev call | timeout | error | p50 ms | p95 ms | max ms |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for k in staged:
            st = [r["stage"] for r in loaded[k] if r.get("stage")]
            paths = [x["path"] for x in st]
            ms = sorted(x["ms"] for x in st if x.get("ms") is not None)
            p95 = ms[min(len(ms) - 1, int(0.95 * len(ms)))] if ms else None
            out.append(
                f"| {RUNS[k][0]} | {len(st)} | {paths.count('none')} | {paths.count('fast')} | {paths.count('llm')} | {paths.count('timeout')} | "
                f"{paths.count('error')} | {statistics.median(ms) if ms else '–'} | {p95 or '–'} | {ms[-1] if ms else '–'} |"
            )
        out.append("")
    for k in staged:
        misses = [
            r
            for r in loaded[k]
            if r.get("responders") is not None and sorted(r["responders"]) != r["expected_responder"]
        ]
        out += [
            f"## Remaining misses, {RUNS[k][0]} ({len(misses)})",
            "",
            "| case | expected | got | how | stage path | labels (P) | P(complete) | Jev P(who) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in sorted(misses, key=lambda r: r["id"]):
            st = r.get("stage") or {}
            labels = ", ".join(
                f"{n}: {lab}" + (f" {p:.2f}" if p is not None else "")
                for n, (lab, p) in (st.get("labels") or {}).items()
            )
            pw = ", ".join(f"{a} {v:.2f}" for a, v in sorted((r["whoP"] or {}).items(), key=lambda kv: -kv[1])[:3])
            comp = st.get("complete")
            out.append(
                f"| {r['id']} | {r['expected_responder'] or 'none'} | {r['responders'] or 'none'} | {r['how']} | {st.get('path', '–')} | {labels or '–'} | {fmt(comp)} | {pw} |"
            )
        out.append("")
    if "b" in loaded:
        out += ["## Full per-class tables, (b) addressing stage", ""] + report(loaded["b"]).split("\n")[3:]
    return "\n".join(out)


def demo() -> None:
    ms = [
        {"kind": "human", "id": "u-max", "displayName": "Max", "mentionHandle": "max"},
        {"kind": "human", "id": "u-leo", "displayName": "Leo Park", "mentionHandle": "leo-park"},
        {"kind": "agent", "id": "max", "displayName": "Max", "mentionHandle": "max"},
        {"kind": "agent", "id": "fin", "displayName": "Finance", "mentionHandle": "finance"},
        {"kind": "agent", "id": "ops", "displayName": "Customer Operations", "mentionHandle": "customer-operations"},
    ]
    c = candidates("@leo-park can you get Finance and @customer-operations to look?", ms, False)
    assert [x["name"] for x in c] == ["leo", "finance", "customer operations"], c
    assert candidates("Max, send the deck", ms, False)[0] | {} == {
        "name": "max",
        "agents": ["max"],
        "humans": ["u-max"],
        "pos": 0,
    }
    assert (
        fast_path(
            "Finance, can you resend the invoice?", candidates("Finance, can you resend the invoice?", ms, False), False
        )
        == "fin"
    )
    assert fast_path("Finance, we're trying to", candidates("Finance, we're trying to", ms, False), False) is None
    assert candidates("fynance can you check", ms, True)[0]["agents"] == ["fin"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--report"]:
        here = Path(__file__).parent / "results"
        md = report_experiment(here / "raw")
        (here / "2026-10-02-addressing-experiment.md").write_text(md + "\n")
        print(md)
    else:
        demo()
        print("ok")
