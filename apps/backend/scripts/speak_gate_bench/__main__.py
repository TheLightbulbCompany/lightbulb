"""Offline "speak or stay silent" bench for Lightbulb Live: can Jev decide when to talk at a pause?

Runs prod Jev (``core.services.jev.decide`` -- same gateway, model, ZDR options and 3 s timeout)
with the §2.5 prompt from the Speak-or-Stay-Silent report on:

* the AMI test split (CC BY 4.0) of ``ishiki-labs/multi-party-dialogue`` -- 1,410 rows, the
  target speaker renamed "Lightbulb". Friends and SPGI are NOT used (not commercially clean).
* ``golden.jsonl`` -- hand-written Lightbulb scenarios, the ship gate.

    export AI_GATEWAY_API_KEY=$(AWS_PROFILE=isol8-admin aws secretsmanager get-secret-value \\
        --secret-id lightbulb/dev/vercel-ai-gateway --query SecretString --output text)
    uv run --extra bench python -m scripts.speak_gate_bench            # run both sets + report
    uv run --extra bench python -m scripts.speak_gate_bench --sets golden --limit 20
    uv run python -m scripts.speak_gate_bench --report-only            # re-render from raw/

Use the DEV key: a 1.6k-call burst on the prod key would throttle prod routing. Raw per-row
outputs land in ``results/raw/`` (gitignored); the markdown summary in ``results/<date>.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://bench.clerk.accounts.dev")  # Settings() refuses to load without it

from core.services import jev  # noqa: E402

HERE = Path(__file__).parent
RAW = HERE / "results" / "raw"
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)

# ------------------------------------------------------------------ the §2.5 prompt, verbatim
SPEAK_INSTRUCTIONS = (
    "Lightbulb is an AI teammate listening to a live voice call among colleagues. A pause just "
    "happened after the latest line. Decide whether Lightbulb should START TALKING now or STAY SILENT. "
    "First decide Lightbulb's role in the current exchange: ACTIVE (Lightbulb spoke in the last few "
    "lines, was asked something, or the people are reacting to what Lightbulb just said) or BYSTANDER "
    "(people are talking to each other). Speak when the latest line is a follow-up to Lightbulb's last "
    "answer, when it is a question put to the whole room that nobody has answered and Lightbulb can "
    "answer it, or when Lightbulb has news the room is waiting for and the conversation has reached a "
    "natural break. Stay silent when the line is addressed to a person by name, when Lightbulb is only "
    "mentioned or talked about, when the speaker is mid-thought, for thanks, backchannels and "
    "chit-chat, and whenever Lightbulb is a bystander and unsure. Talking over people is worse than "
    "staying quiet: when in doubt, stay silent."
)
CRITERIA = {
    "speak": "SPEAK: Lightbulb should talk now (a follow-up to Lightbulb, an unanswered question to the "
    "room it can answer, or awaited news at a natural break)",
    "silent": "SILENT: people are talking to each other, Lightbulb is only mentioned, the speaker is "
    "mid-thought, or it is chatter, thanks or a backchannel",
}
#: The report's optional diagnostic question. Jev returns no free-text reasoning, so this
#: classification (plus Jev's confidence) is the per-row "why" we record.
KIND_CRITERIA = {
    "I1": "Lightbulb is addressed by name and asked something",
    "I2a": "A follow-up to what Lightbulb just said, without its name",
    "I2b": "An open question to the whole room that nobody has answered",
    "N1": "Lightbulb has awaited news and the conversation is at a natural break",
    "S1": "People talking to each other; Lightbulb is a bystander",
    "S2": "Lightbulb is mentioned or talked about, not addressed",
    "S3": "The speaker is mid-thought; the line is incomplete",
    "S4": "The line is addressed to a person by name",
    "S5": "A backchannel, acknowledgement or thanks",
}
#: ``--prompt news``: the §2.5 prompt reads "news the room is waiting for", and Jev took that
#: literally -- it held back unsolicited results at a natural break. This variant makes any
#: pending result worth sharing once the floor is free; everything else is unchanged.
NEWS_INSTRUCTIONS = SPEAK_INSTRUCTIONS.replace(
    "or when Lightbulb has news the room is waiting for and the conversation has reached a natural break.",
    "or when Lightbulb has news to share (every item in lightbulb.news is a finished result the room "
    "wants to hear, asked for or not) and the conversation has reached a natural break: a topic just "
    "wrapped up, an acknowledgement, a lull, or a question the news answers.",
)
NEWS_CRITERIA = {**CRITERIA, "speak": CRITERIA["speak"].replace("awaited news", "pending news")}
PROMPTS = {"report": (SPEAK_INSTRUCTIONS, CRITERIA), "news": (NEWS_INSTRUCTIONS, NEWS_CRITERIA)}


def questions(prompt: str) -> dict:
    instructions, criteria = PROMPTS[prompt]
    return {
        "turn": {"type": "choice", "instructions": instructions, "criteria": criteria},
        "kind": {
            "type": "choice",
            "instructions": "Which situation best describes the latest line, from Lightbulb's point of view?",
            "criteria": KIND_CRITERIA,
        },
    }


# ------------------------------------------------------------------ datasets
def _state(people, lines, lightbulb=None) -> dict:
    turns = [{"author": s, "text": t} for s, t in lines]
    state = {
        "room": {"kind": "call", "people": people, "ai": "Lightbulb"},
        "recent": turns[:-1][-12:],
        "latest": {**turns[-1], "pauseMs": 900},
    }
    if lightbulb:
        state["lightbulb"] = lightbulb
    return state


#: The paper's AMI categories into ours (report §2.4). Their I2 splits on whether the target
#: already spoke in the window (ACTIVE -> I2a follow-up) or not (I2b). Their S2 is "referenced,
#: or was active, but someone else spoke next" -- NOT our S2, so it is kept as S2p.
_AMI_CATS = {"SPEAK_explicit": "I1", "SPEAK_implicit": "I2", "SILENT_no_ref": "S1", "SILENT_ref": "S2p"}
_AMI_NAMES = ["Alex", "Blair", "Casey", "Drew", "Eden"]


def load_ami() -> list[dict]:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download("ishiki-labs/multi-party-dialogue", "ami/test/test_samples.jsonl", repo_type="dataset")
    rows = []
    for raw in Path(path).read_text().splitlines():
        r = json.loads(raw)
        target = r["target_speaker"]
        others = [s for s in r["all_speakers"] if s != target]
        name = {s: _AMI_NAMES[i] for i, s in enumerate(others)} | {target: "Lightbulb"}
        turns = r["context_turns"] + [r["current_turn"]]
        lines = [(name.get(t["speaker"], t["speaker"]), t["text"]) for t in turns]
        cat = _AMI_CATS[r["category"]]
        if cat == "I2":
            cat = "I2a" if any(t["speaker"] == target for t in r["context_turns"]) else "I2b"
        rows.append(
            {
                "id": r["decision_point_id"],
                "category": cat,
                "expected": r["decision"],
                "state": _state([name[s] for s in others], lines),
            }
        )
    return rows


def load_golden() -> list[dict]:
    rows = []
    for raw in (HERE / "golden.jsonl").read_text().splitlines():
        if not raw.strip():
            continue
        g = json.loads(raw)
        lb = {
            k: v
            for k, v in (
                ("lastSaid", g["lightbulb_last_said"]),
                ("spokeSecondsAgo", g.get("lightbulb_spoke_seconds_ago")),
                ("news", g["pending_news"]),
            )
            if v
        }
        lines = [(t["speaker"], t["text"]) for t in g["transcript"]]
        rows.append(
            {
                "id": g["id"],
                "category": g["category"],
                "expected": g["expected"],
                "state": _state(g["people"], lines, lb),
            }
        )
    return rows


# ------------------------------------------------------------------ runner
_call = contextvars.ContextVar("call")


def _capture(name, value=1.0, unit="Count", dimensions=None):  # stands in for put_metric
    sink = _call.get(None)
    if sink is not None:
        sink[name] = dimensions.get("outcome") if dimensions else value


async def run(rows: list[dict], out: Path, concurrency: int, budget: dict, qs: dict) -> None:
    jev.put_metric = _capture
    jev._key, jev._key_checked_at, jev._KEY_TTL_S = os.environ["AI_GATEWAY_API_KEY"], time.monotonic(), 10**9
    done = {json.loads(line)["id"] for line in out.read_text().splitlines()} if out.exists() else set()
    sem = asyncio.Semaphore(concurrency)

    async def one(row):
        async with sem:
            if budget["spent"] >= budget["cap"]:
                return None
            for attempt in range(1, 5):
                sink = {}
                _call.set(sink)
                started = time.perf_counter()
                answers = await jev.decide(row["state"], qs)
                latency = (time.perf_counter() - started) * 1000
                budget["spent"] += float(sink.get("messaging.jev.cost") or 0)
                if isinstance(answers, dict):
                    break
                await asyncio.sleep(2**attempt)
            ok = isinstance(answers, dict)
            turn, kind = (answers.get("turn") or {}, answers.get("kind") or {}) if ok else ({}, {})
            return {
                "id": row["id"],
                "category": row["category"],
                "expected": row["expected"],
                "p_speak": (turn.get("probabilities") or {}).get("speak"),
                "confidence": turn.get("confidence"),
                "kind": kind.get("choice"),
                "kind_p": kind.get("probabilities"),
                "outcome": sink.get("messaging.jev.decision"),
                "attempts": attempt,
                "latency_ms": round(latency),
                "cost": float(sink.get("messaging.jev.cost") or 0),
                "input_tokens": sink.get("messaging.jev.input_tokens"),
            }

    todo = [r for r in rows if r["id"] not in done]
    with out.open("a") as fh:
        for i, coro in enumerate(asyncio.as_completed([one(r) for r in todo]), 1):
            rec = await coro
            if rec:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
            if i % 100 == 0:
                print(f"  {out.stem}: {i}/{len(todo)}  spent ${budget['spent']:.4f}", file=sys.stderr)


# ------------------------------------------------------------------ metrics
def _speaks(r, t):
    # A failed call is silence -- what prod does when Jev is unavailable.
    return r["p_speak"] is not None and r["p_speak"] >= t


def prf(recs, t):
    tp = sum(1 for r in recs if _speaks(r, t) and r["expected"] == "SPEAK")
    fp = sum(1 for r in recs if _speaks(r, t) and r["expected"] == "SILENT")
    pos = sum(1 for r in recs if r["expected"] == "SPEAK")
    return (tp / (tp + fp) if tp + fp else None), (tp / pos if pos else None)


def cat_acc(recs, t):
    by = defaultdict(list)
    for r in recs:
        by[r["category"]].append(_speaks(r, t) == (r["expected"] == "SPEAK"))
    return {c: sum(v) / len(v) for c, v in sorted(by.items())}


def bal_acc(recs, t):
    _, rec_s = prf(recs, t)
    sil = [r for r in recs if r["expected"] == "SILENT"]
    spec = sum(1 for r in sil if not _speaks(r, t)) / len(sil) if sil else 0
    return ((rec_s or 0) + spec) / 2


#: Report §5.3 ship thresholds (golden set).
SHIP = {"precision": 0.95, "I2a": 0.6, "N1": 0.8, "S2": 0.98, "S4": 0.98, "S5": 0.98}


def ship_check(recs, t) -> dict[str, bool]:
    p, _ = prf(recs, t)
    acc = cat_acc(recs, t)
    return {k: ((p or 0) if k == "precision" else acc.get(k, 0)) >= v for k, v in SHIP.items()}


def _f(x):
    return "–" if x is None else f"{x:.2f}"


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


def report(sets: dict[str, list[dict]], prompt: str) -> str:
    out = [f"# Speak-gate bench — Jev zero-shot, `{prompt}` prompt", ""]
    out += [
        f"Model `{jev.MODEL}` via `{jev._URL}` (prod `jev.decide`, 3 s timeout, ZDR), the report's §2.5 prompt (`news`: its news clause widened, see `NEWS_INSTRUCTIONS`). "
        "SPEAK iff P(speak) ≥ threshold; a failed call counts as SILENT (prod behaviour). "
        "No candidate filter is applied: every row is judged.",
        "",
    ]
    allrecs = [r for recs in sets.values() for r in recs]
    lat = [r["latency_ms"] for r in allrecs if r["p_speak"] is not None]
    fails = sum(1 for r in allrecs if r["p_speak"] is None)
    out += [
        "## Cost and latency",
        "",
        f"- Calls: {len(allrecs)} rows ({fails} failed after retries), total cost **${sum(r['cost'] for r in allrecs):.4f}**",
        f"- Latency (successful call, wall clock): p50 **{_pct(lat, 0.5)} ms**, p90 **{_pct(lat, 0.9)} ms**, "
        f"p99 {_pct(lat, 0.99)} ms",
        f"- Mean input tokens: {statistics.mean([r['input_tokens'] for r in allrecs if r['input_tokens']] or [0]):.0f}",
        "",
    ]
    for name, recs in sets.items():
        if not recs:
            continue
        cats = sorted({r["category"] for r in recs})
        n = Counter(r["category"] for r in recs)
        out += [f"## {name} ({len(recs)} rows)", ""]
        out += ["Accuracy per category (SPEAK categories = recall, SILENT categories = silence rate):", ""]
        out += [
            "| threshold | "
            + " | ".join(f"{c} (n={n[c]})" for c in cats)
            + " | SPEAK precision | SPEAK recall | BalAcc |"
        ]
        out += ["|---" * (len(cats) + 4) + "|"]
        for t in THRESHOLDS:
            acc = cat_acc(recs, t)
            p, rc = prf(recs, t)
            out += [
                f"| {t} | "
                + " | ".join(_f(acc.get(c)) for c in cats)
                + f" | {_f(p)} | {_f(rc)} | {_f(bal_acc(recs, t))} |"
            ]
        says = [r["p_speak"] for r in recs if r["p_speak"] is not None]
        out += [
            "",
            f"P(speak) distribution: p10 {_f(_pct(says, 0.1))}, p50 {_f(_pct(says, 0.5))}, p90 {_f(_pct(says, 0.9))}.",
            "",
        ]
        out += ["Jev's diagnostic `kind` answer per expected category (top 3):", ""]
        for c in cats:
            top = Counter(r["kind"] for r in recs if r["category"] == c).most_common(3)
            out += [f"- {c}: " + ", ".join(f"{k} {v}" for k, v in top)]
        out += [""]
        if name == "golden":
            out += ["### Ship thresholds (report §5.3)", ""]
            out += [
                "| threshold | precision ≥ 0.95 | I2a recall ≥ 0.6 | N1 recall ≥ 0.8 | S2 ≥ 0.98 | S4 ≥ 0.98 | S5 ≥ 0.98 | all |"
            ]
            out += ["|---" * 8 + "|"]
            passing = []
            for t in THRESHOLDS:
                chk = ship_check(recs, t)
                ok = all(chk.values())
                passing += [t] if ok else []
                out += [
                    f"| {t} | " + " | ".join("✅" if chk[k] else "❌" for k in SHIP) + f" | {'✅' if ok else '❌'} |"
                ]
            out += [""]
            verdict = (
                f"**PASS** at thresholds {passing[0]}–{passing[-1]}: Jev zero-shot meets every ship bar on the golden set."
                if passing
                else "**FAIL**: no threshold meets every ship bar. Jev zero-shot is not good enough as the "
                "proactive-speech gate; see the reading below."
            )
            out += [f"**Verdict:** {verdict}", ""]
    return "\n".join(out)


def _selfcheck():
    recs = [
        {"category": "I2a", "expected": "SPEAK", "p_speak": 0.9},
        {"category": "S4", "expected": "SILENT", "p_speak": 0.6},
        {"category": "S4", "expected": "SILENT", "p_speak": None},
    ]
    assert prf(recs, 0.5) == (0.5, 1.0) and prf(recs, 0.7) == (1.0, 1.0)
    assert cat_acc(recs, 0.7) == {"I2a": 1.0, "S4": 1.0} and cat_acc(recs, 0.5)["S4"] == 0.5
    assert bal_acc(recs, 0.5) == 0.75


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", default=["ami", "golden"], choices=["ami", "golden"])
    ap.add_argument("--limit", type=int, default=0, help="first N rows per set (smoke runs)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--cost-cap", type=float, default=5.0, help="USD; stop issuing calls past it")
    ap.add_argument("--prompt", default="report", choices=sorted(PROMPTS))
    ap.add_argument("--tag", default="", help="raw/<tag>-<set>.jsonl; reruns resume (default: date[-prompt])")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    args.tag = args.tag or date.today().isoformat() + ("" if args.prompt == "report" else f"-{args.prompt}")
    _selfcheck()
    RAW.mkdir(parents=True, exist_ok=True)
    loaders = {"ami": load_ami, "golden": load_golden}
    if not args.report_only:
        budget = {"spent": 0.0, "cap": args.cost_cap}

        async def all_sets():
            for s in args.sets:
                rows = loaders[s]()
                rows = rows[: args.limit] if args.limit else rows
                await run(rows, RAW / f"{args.tag}-{s}.jsonl", args.concurrency, budget, questions(args.prompt))

        asyncio.run(all_sets())
    sets = {}
    for s in ("ami", "golden"):
        f = RAW / f"{args.tag}-{s}.jsonl"
        sets[s] = [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
    md = report(sets, args.prompt)
    (HERE / "results" / f"{args.tag}.md").write_text(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
