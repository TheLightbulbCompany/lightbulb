"""The eval bar, shared by the phone and Meet harnesses: a scenario passes only if ALL k runs pass (pass^k), and the
aggregate run pass rate must also reach BAR. Nothing here touches the network."""

from pathlib import Path

BAR = 0.9
EVIDENCE = Path.home() / "Lightbulb/super-agent-design/evidence/voice"


def score(results: dict[str, list[bool]], k: int = 3, bar: float = BAR) -> dict:
    per = {n: len(r) >= k and all(r) for n, r in results.items()}
    flat = [x for r in results.values() for x in r]
    rate = sum(flat) / len(flat) if flat else 0.0
    return {"k": k, "bar": bar, "runs": results, "scenarios": per, "rate": rate, "ok": bool(per) and all(per.values()) and rate >= bar}


def exit_code(v: dict) -> int:
    return 0 if v["ok"] else 1


def card(kind: str, v: dict, stamp: str) -> str:
    md = [f"# {kind} pass^{v['k']} eval {stamp} — {'PASS' if v['ok'] else 'FAIL'}\n",
          f"Aggregate run pass rate {v['rate']:.0%} (bar {v['bar']:.0%}).\n",
          f"| scenario | runs passed | pass^{v['k']} |", "|---|---|---|"]  # fmt: skip
    md += [f"| {n} | {sum(r)}/{len(r)} | {'pass' if v['scenarios'][n] else 'FAIL'} |" for n, r in v["runs"].items()]
    md += ["\n## Sellable bar\n",
           f"- {'[x]' if v['ok'] else '[ ]'} Every scenario passes pass^{v['k']} and the aggregate is at least {v['bar']:.0%} (this run).",
           "- [ ] 20+ real dial-ins on the real number by people who are not the builder, each scored by the same judge."]  # fmt: skip
    return "\n".join(md) + "\n"


def write_card(kind: str, v: dict, stamp: str, d: Path = EVIDENCE) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"eval-{kind}-pass{v['k']}-{stamp}.md"
    p.write_text(card(kind, v, stamp))
    return p
