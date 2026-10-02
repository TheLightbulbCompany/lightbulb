"""Routing gate over repeated runs: mean (min-max) per class, baseline vs candidate, chat and voice.

    uv run python -m scripts.speak_gate_bench._agg BASE_RAW_DIR CAND_RAW_DIR [VOICE_PREFIX]

Reads ``base-{1,2,3}-multiparty.jsonl`` and ``cand-{1,2,3}-multiparty.jsonl`` (``multiparty --tag base-1`` ...);
``VOICE_PREFIX`` (default ``cand``) reads the candidate's voice rows from ``<prefix>-{1,2,3}`` instead.
Jev flips margin rows between runs (+-0.07 per class), so one run cannot decide. Acceptance: cued recall >= 0.9 and
precision >= 0.95 on the candidate's means, and no class mean worse than the baseline's by more than 0.03.
"""

import json
import statistics
import sys
from scripts.speak_gate_bench.multiparty import CATEGORIES, score

base_dir, cand_dir = sys.argv[1], sys.argv[2]


def load(d: str, prefix: str) -> list[list[dict]]:
    return [[json.loads(line) for line in open(f"{d}/{prefix}-{i}-multiparty.jsonl")] for i in (1, 2, 3)]


B, C = load(base_dir, "base"), load(cand_dir, "cand")
if len(sys.argv) > 3:
    voice_runs = load(cand_dir, sys.argv[3])
    C = [
        [r for r in run if r["variant"] == "chat"] + [r for r in v if r["variant"] == "voice"]
        for run, v in zip(C, voice_runs)
    ]
SL = [(c, f"{c} {CATEGORIES[c]}", lambda r, c=c: r["category"] == c) for c in CATEGORIES]
SL += [
    ("cued", "cued", lambda r: r["cue"] == "cued"),
    ("implicit", "implicit", lambda r: r["cue"] == "implicit"),
    ("total", "**total**", lambda r: True),
]
M = ("recall", "false_speak", "precision")


def stats(runs, variant, pred, m):
    xs = [score([r for r in run if r["variant"] == variant and pred(r)])[m] for run in runs]
    xs = [x for x in xs if x is not None]
    return (statistics.mean(xs), min(xs), max(xs)) if xs else None


def f(s):
    return "–" if s is None else f"{s[0]:.2f} ({s[1]:.2f}–{s[2]:.2f})"


out, fails = [], []
for variant in ("chat", "voice"):
    out += [
        f"### {variant}",
        "",
        "| slice | base recall | cand recall | base false-speak | cand false-speak | base precision | cand precision |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, label, pred in SL:
        row = [label]
        for m in M:
            b, c = stats(B, variant, pred, m), stats(C, variant, pred, m)
            row += [f(b), f(c)]
            if b and c:
                worse = (c[0] - b[0]) if m == "false_speak" else (b[0] - c[0])
                if worse > 0.03:
                    fails.append(f"{variant} {key} {m}: base {b[0]:.2f} -> cand {c[0]:.2f}")
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    for name, m, pred, ok in (
        ("cued recall", "recall", SL[-3][2], lambda x: x >= 0.9),
        ("precision", "precision", SL[-1][2], lambda x: x >= 0.95),
    ):
        c = stats(C, variant, pred, m)
        if not ok(c[0]):
            fails.append(f"{variant} §12.5 {name} {c[0]:.2f}")
print("\n".join(out))
print("Gate misses:" if fails else "Gate: met", *fails, sep="\n- ")
