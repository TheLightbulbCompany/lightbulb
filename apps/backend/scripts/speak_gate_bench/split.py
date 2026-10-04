"""Split bench: one spoken Live line that names several agents -> each agent's own task.

Runs the REAL ``messaging.address(live=True)`` (live Jev on the dev gateway) and derives each agent's
task the way ``voice_calls.route_live_line`` does. ``split.jsonl`` rows: ``{id, cat, line, expect}``
where ``expect`` maps an agent id to ``{"has": [...], "not": [...]}``: phrases the agent's task must
and must not contain. A row passes when exactly the expected agents are addressed and every phrase
check holds.

    export AI_GATEWAY_API_KEY=$(AWS_PROFILE=isol8-admin aws secretsmanager get-secret-value \\
        --secret-id lightbulb/dev/vercel-ai-gateway --query SecretString --output text)
    uv run python -m scripts.speak_gate_bench.split --runs 3 --tag clause
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://bench.clerk.accounts.dev")  # Settings() refuses to load without it

from core.services import jev, messaging  # noqa: E402

HERE = Path(__file__).parent


def _agents(names) -> list[dict]:
    return [
        {
            "kind": "agent",
            "id": n.lower().replace(" ", "-"),
            "displayName": n,
            "mentionHandle": n.lower().replace(" ", "-"),
        }
        for n in names
    ]


AGENTS = _agents(("Forge", "Friday", "Pulse", "Gauge"))
PEOPLE = [
    {"kind": "human", "id": "u-pras", "displayName": "Prasiddha Parthsarthy"},
    {"kind": "human", "id": "u-leo", "displayName": "Leo Park"},
]
MEMBERS = AGENTS + PEOPLE


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' -]", " ", text.lower().replace("’", "'"))).strip()


async def one(case: dict) -> dict:
    latest = {"author": PEOPLE[0], "text": case["line"], "mentions": []}
    started = time.perf_counter()
    # ``agents``: a row's own roster (the starter roster's role names, a renamed agent), else the default four.
    members = _agents(case["agents"]) + PEOPLE if case.get("agents") else MEMBERS
    out = await messaging.address(latest, members, history=[], live=True)
    ms = (time.perf_counter() - started) * 1000
    order, tasks = out["agents"], out.get("tasks")
    # route_live_line: with 2+ agents each gets its task, else the whole line.
    got = {a: (tasks or {}).get(a) or case["line"] for a in order}
    why = []
    if set(got) != set(case["expect"]):
        why.append(f"agents {sorted(got)} != {sorted(case['expect'])}")
    for agent, want in case["expect"].items():
        task = _norm(got.get(agent, ""))
        why += [f"{agent} lacks '{p}'" for p in want.get("has", []) if _norm(p) not in task]
        why += [f"{agent} has '{p}'" for p in want.get("not", []) if _norm(p) in task]
    return {"id": case["id"], "cat": case["cat"], "ok": not why, "why": why, "got": got, "path": out["path"], "ms": ms}


async def run(cases: list[dict], runs: int, concurrency: int) -> list[list[dict]]:
    jev._key, jev._key_checked_at, jev._KEY_TTL_S = os.environ["AI_GATEWAY_API_KEY"], time.monotonic(), 10**9
    sem = asyncio.Semaphore(concurrency)

    async def guarded(case):
        async with sem:
            return await one(case)

    return [await asyncio.gather(*(guarded(c) for c in cases)) for _ in range(runs)]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--timeout", type=float, default=0, help="override ADDRESSING_TIMEOUT_S (to see real latency)")
    args = ap.parse_args(argv)
    if args.timeout:
        messaging.ADDRESSING_TIMEOUT_S = jev._TIMEOUT_S = args.timeout
    cases = [json.loads(line) for line in (HERE / "split.jsonl").read_text().splitlines() if line.strip()]
    cases = [c for c in cases if re.search(args.only, c["id"])]
    with_metric = messaging.put_metric
    messaging.put_metric = jev.put_metric = lambda *a, **k: None  # no EMF noise in a bench
    try:
        all_runs = asyncio.run(run(cases, args.runs, args.concurrency))
    finally:
        messaging.put_metric = jev.put_metric = with_metric
    out_dir = HERE / "results" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"split-{args.tag}.json").write_text(json.dumps(all_runs, indent=1))
    by_cat: dict[str, list[bool]] = defaultdict(list)
    flaky: dict[str, list[dict]] = defaultdict(list)
    for rows in all_runs:
        for r in rows:
            by_cat[r["cat"]].append(r["ok"])
            flaky[r["id"]].append(r)
    total = [ok for oks in by_cat.values() for ok in oks]
    ms = [r["ms"] for rows in all_runs for r in rows if r["path"] == "jev"]
    paths = defaultdict(int)
    for rows in all_runs:
        for r in rows:
            paths[r["path"]] += 1
    print(f"\n== {args.tag}: {sum(total)}/{len(total)} = {sum(total) / len(total):.2f} over {args.runs} run(s)")
    for cat in sorted(by_cat):
        oks = by_cat[cat]
        print(f"  {cat:16} {sum(oks):3}/{len(oks):<3} {sum(oks) / len(oks):.2f}")
    if ms:
        qs = statistics.quantiles(ms, n=20) if len(ms) >= 20 else [max(ms)] * 19
        print(
            f"  jev latency ms: p50 {statistics.median(ms):.0f}  p95 {qs[18]:.0f}  max {max(ms):.0f}   paths {dict(paths)}"
        )
    print("\n-- failures (id: passes/runs)")
    for cid, rows in flaky.items():
        bad = [r for r in rows if not r["ok"]]
        if bad:
            print(f"  {cid}: {len(rows) - len(bad)}/{len(rows)}  {bad[0]['why']}")
            for agent, task in bad[0]["got"].items():
                print(f"      {agent}: {task[:110]}")


if __name__ == "__main__":
    main()
