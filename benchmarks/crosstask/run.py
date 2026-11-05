"""Run the cross-task recall eval and write a scored report.

    python run.py --backend fake --llm fake --out results/fake            # no model calls
    BEAM_LLM=codex BEAM_CODEX_HOME=... python run.py --llm codex --budget 1200 --out results/x [--no-links]

Resumable: questions already in ``<out>/results.jsonl`` are skipped, and Codex replies are cached in
``<out>/codex-cache.jsonl`` (llm_codex.set_cache). Codex subscription only; no API key is read.
The live backend (bench routes against dev AgentCore + the dev tasks table) lands with wave2-M Task 9.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import agent
import score
from fake_backend import FakeBackend

HERE = Path(__file__).parent


def load_llm(name: str):
    if name == "fake":
        return agent.HeuristicLLM()
    sys.path.insert(0, str(HERE.parent / "beam"))
    import llm_codex  # noqa: PLC0415 -- only on a real run

    return llm_codex


async def run(
    fixture: dict, backend, llm, out: Path, *, links: bool = True, limit: int | None = None, concurrency: int = 4
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "results.jsonl"
    done = {}
    if path.exists():
        done = {r["id"]: r for r in map(json.loads, path.read_text("utf-8").splitlines()) if r}
    todo = [q for q in fixture["questions"][:limit] if q["id"] not in done]
    slots = asyncio.Semaphore(concurrency)

    async def one(q: dict) -> None:
        async with slots:
            r = await agent.run_question(q, backend, llm, links=links)
        done[q["id"]] = r
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r) + "\n")

    await asyncio.gather(*(one(q) for q in todo))
    asked = fixture["questions"][:limit]
    summary = score.summarize(asked, [done[q["id"]] for q in asked if q["id"] in done], fixture["canaries"])
    summary["links"] = links
    summary["passes"] = score.passes(summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", "utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=str(HERE / "fixtures" / "workspace-v1.json"))
    ap.add_argument("--backend", choices=["fake"], default="fake")
    ap.add_argument("--llm", choices=["fake", "codex"], default="fake")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--budget", type=int, help="cap on Codex calls (checked before each call)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    fixture = json.loads(Path(args.fixture).read_text("utf-8"))
    llm = load_llm(args.llm)
    out = Path(args.out)
    if args.llm == "codex":
        out.mkdir(parents=True, exist_ok=True)
        llm.set_cache(out / "codex-cache.jsonl")
        llm.set_budget(args.budget)
    summary = asyncio.run(run(fixture, FakeBackend(fixture), llm, out, links=not args.no_links, limit=args.limit))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("byCategory", "leakedQuestions")}, indent=1))


if __name__ == "__main__":
    main()
