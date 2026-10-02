"""Report for the D-class signal experiment (time structure / open-request state), bench only.

uv run python -m scripts.speak_gate_bench.multiparty --addressing on --tag dx-e --signals time
uv run python -m scripts.speak_gate_bench.dclass   # prints the tables for results/2026-10-02-dclass-experiment.md
"""

from __future__ import annotations

import json
from pathlib import Path

from .multiparty import CATEGORIES, _f, report, score

RAW = Path(__file__).parent / "results" / "raw"
RUNS = {
    "d": ("(d) stage, rerun", "dx-d"),
    "e": ("(e) + time", "dx-e"),
    "f": ("(f) + open requests", "dx-f"),
    "f2": ("(f2) + open goals", "dx-f2"),
    "g1": ("(g) e + f", "dx-g1"),
    "g": ("(g2) e + f2", "dx-g"),
    "h": ("(h) f2 + pick up open", "dx-h"),
}
FOCUS = ["D01", "D03", "D12", "D02", "D07", "D08", "D10", "D13", "D14", "D16", "D18", "B11", "B12", "B15", "B18"]


def _load() -> dict:
    return {
        k: [json.loads(x) for x in (RAW / f"{f}-multiparty.jsonl").read_text().splitlines()]
        for k, (_, f) in RUNS.items()
        if (RAW / f"{f}-multiparty.jsonl").exists()
    }


def _got(r: dict) -> str:
    who = ", ".join(r["responders"]) or "none"
    p = ", ".join(f"{a} {v:.2f}" for a, v in sorted((r["whoP"] or {}).items(), key=lambda kv: -kv[1])[:2])
    return f"{who} ({p})" if p else who


def main() -> str:
    runs = _load()
    cols = list(runs)
    out = []
    for variant in ("chat", "voice"):
        out += [
            f"### {variant}: recall / false-speak per class",
            "",
            "| class | " + " | ".join(RUNS[k][0] for k in cols) + " |",
            "|---|" + "---|" * len(cols),
        ]
        slices = [(f"{c} {CATEGORIES[c]}", lambda r, c=c: r["category"] == c) for c in CATEGORIES]
        slices += [("D answered/closed rows", lambda r: r["category"] == "D" and r["expected"] == "SILENT")]
        slices += [("B group-acks", lambda r: r["category"] == "B" and r["subtype"] == "group-ack")]
        slices += [("**total**", lambda r: True)]
        for label, keep in slices:
            cells = []
            for k in cols:
                s = score([r for r in runs[k] if r["variant"] == variant and keep(r)])
                cells.append(f"{_f(s['recall'])} / {_f(s['false_speak'])}")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out.append("")
    out += [
        "### Focus rows (chat / voice): who spoke, top P(who)",
        "",
        "| row | expected | " + " | ".join(RUNS[k][0] for k in cols) + " |",
        "|---|---|" + "---|" * len(cols),
    ]
    for row in FOCUS:
        recs = {k: {r["variant"]: r for r in runs[k] if r["row"] == row} for k in cols}
        exp = next(iter(recs[cols[0]].values()))["expected_responder"] or "none"
        cells = [" <br> ".join(_got(recs[k][v]) for v in ("chat", "voice") if v in recs[k]) for k in cols]
        out.append(f"| {row} | {', '.join(exp) if isinstance(exp, list) else exp} | " + " | ".join(cells) + " |")
    out.append("")
    for k in cols:
        misses = [r for r in runs[k] if sorted(r["responders"]) != r["expected_responder"]]
        out += [f"### Misses, {RUNS[k][0]} ({len(misses)})", "", "| case | expected | got (P) |", "|---|---|---|"]
        out += [
            f"| {r['id']} | {', '.join(r['expected_responder']) or 'none'} | {_got(r)} |"
            for r in sorted(misses, key=lambda r: r["id"])
        ]
        out.append("")
    return "\n".join(out)


def full(key: str) -> str:
    return report(_load()[key])


if __name__ == "__main__":
    print(main())
