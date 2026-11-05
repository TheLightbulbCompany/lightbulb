"""E-rules: does Jev's rule question match the right messages? (wave2-S Task 8, spec 12 §3.3)

    cd apps/backend && ENVIRONMENT=dev uv run python ../../benchmarks/rules/run_eval.py --key-from dev [--limit N] [--out /tmp/e-rules.jsonl]

Every row is judged twice through the production path (`jev._decide_for_message`): once with the rules in scope
for its room, once without (latency baseline). 180 rows → 360 gateway calls (founder-approved budget, contracts
§18). `--key-from dev` reads `lightbulb/dev/vercel-ai-gateway` the way the backend does (AWS creds needed);
`--key-from env` reads `AI_GATEWAY_API_KEY`. Either way ENVIRONMENT must be dev (never prod). Bar: precision ≥ 0.90, recall ≥ 0.60 at 0.7, p95 latency +300 ms.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "apps" / "backend"))
os.environ.setdefault("CLERK_ISSUER", "https://eval.invalid")  # backend Settings requires it; unused here

from core.services import jev, rules  # noqa: E402

RULES = [
    {
        "scheduleId": "bug",
        "name": "reproduce and fix",
        "policyText": "When someone reports a bug in our product (something broken, an error, a crash), try to "
        "reproduce it and fix it quietly in acme/web.",
        "room": "dev",
    },
    {
        "scheduleId": "refund",
        "name": "draft the refund",
        "policyText": "When a customer asks for a refund over $100, draft the refund and the reply for a person "
        "to approve.",
        "room": "support",
    },
    {
        "scheduleId": "deck",
        "name": "share the deck",
        "policyText": "When someone asks for the sales deck or pitch deck, find the latest version and share the link.",
        "room": "sales",
    },
]
AGENT = {"id": "a1", "kind": "agent", "displayName": "Lightbulb"}
CARD = "Lightbulb. The workspace assistant: answers questions, does tasks people ask for. Tools: web, gh."


def load(path: Path = HERE / "dataset.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rule_rows() -> list[dict]:
    return [
        {**r, "mode": "observe", "status": "active", "createdAt": str(i), "match": {"sources": ["room"], "roomIds": [r["room"]]}}
        for i, r in enumerate(RULES)
    ]  # fmt: skip


def _msg(i: int, author: str, text: str, *, latest: bool = False) -> dict:
    m = {
        "messageId": f"m{i}",
        "author": {"kind": "human", "id": author.lower(), "displayName": author},
        "text": text,
        "createdAt": f"2026-11-04T10:{i:02d}:00Z",
    }
    if latest:
        m["dispatchTargets"] = [{"eventKind": "room_event", "agentId": AGENT["id"]}]
    return m


def conversation(row: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    history = [_msg(i, a, t) for i, (a, t) in enumerate(row["history"])]
    latest = _msg(len(history), "Priya", row["latest"], latest=True)
    people = {m["author"]["id"]: m["author"]["displayName"] for m in [*history, latest]}
    members = [{"id": k, "kind": "human", "displayName": v} for k, v in people.items()] + [AGENT]
    meta = {"ownerId": "org_eval", "roomId": row["room"], "kind": "channel", "name": row["room"]}
    return meta, latest, members, history


def score(results: list[dict]) -> dict:
    tp = sum(1 for r in results if r["label"] == "match" and r["got"] == r["rule_id"])
    fp = sum(1 for r in results if r["got"] and (r["label"] != "match" or r["got"] != r["rule_id"]))
    pos = sum(1 for r in results if r["label"] == "match")

    def p95(xs: list[float]) -> float:
        xs = sorted(xs)
        return xs[max(0, int(round(0.95 * len(xs))) - 1)] if xs else 0.0

    with_ms = [r["ms_rules"] for r in results if r["ms_rules"] is not None]
    without_ms = [r["ms_plain"] for r in results if r["ms_plain"] is not None]
    return {
        "n": len(results),
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / pos, 3) if pos else None,
        "false_positives_by_kind": {
            k: sum(1 for r in results if r["got"] and r["kind"] == k and r["label"] != "match")
            for k in ("near_miss", "chatter", "rule_question")
        },
        "jev_unavailable": sum(1 for r in results if r["unavailable"]),
        "p95_ms_with_rules": round(p95(with_ms)),
        "p95_ms_without": round(p95(without_ms)),
        "p95_delta_ms": round(p95(with_ms) - p95(without_ms)),
    }


def check_env(env: str | None = None) -> None:
    """The eval spends the dev gateway key only, whichever way the key arrives: never from a prod process."""
    env = (env if env is not None else os.environ.get("ENVIRONMENT")) or "dev"
    if env != "dev":
        sys.exit(f"run_eval runs with ENVIRONMENT=dev only (got {env!r})")


async def judge(row: dict, all_rules: list[dict], *, baseline: bool) -> dict:
    meta, latest, members, history = conversation(row)
    in_scope = rules.rules_for_room(all_rules, row["room"])
    timings: list[float] = []
    real = jev.decide

    async def timed(state, questions):
        started = time.perf_counter()
        try:
            return await real(state, questions)
        finally:
            timings.append((time.perf_counter() - started) * 1000)

    jev.decide = timed
    try:
        out = await jev._decide_for_message(meta, latest, latest, members, history, rules=in_scope)
        ms_rules = timings[-1]
        ms_plain = None
        if baseline:
            await jev._decide_for_message(meta, latest, latest, members, history)
            ms_plain = timings[-1]
    finally:
        jev.decide = real
    unavailable = bool(out.get("rateLimited") or (out.get("fallback") and out.get("reason") != "invalid_choice"))
    return {
        **row,
        "got": out.get("rule"),
        "p": out.get("ruleP"),
        "unavailable": unavailable,
        "ms_rules": ms_rules,
        "ms_plain": ms_plain,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-from", choices=["dev", "env"], required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-baseline", action="store_true", help="skip the without-rules call (halves the calls)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    check_env()
    if args.key_from == "env":
        key = os.environ["AI_GATEWAY_API_KEY"]

        async def env_key() -> str:
            return key

        jev._gateway_key = env_key
    costs: list[float] = []
    jev.put_metric = lambda name, value=None, **kw: costs.append(value) if name == "messaging.jev.cost" else None

    async def card(*_a, **_k) -> str:
        return CARD

    jev.agent_card = card
    rows = load()[: args.limit or None]
    all_rules = rule_rows()
    results = []
    for i, row in enumerate(rows, 1):
        results.append(await judge(row, all_rules, baseline=not args.no_baseline))
        print(f"\r{i}/{len(rows)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    summary = {**score(results), "calls": len(rows) * (1 if args.no_baseline else 2), "cost_usd": round(sum(costs), 4)}
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    misses = [r for r in results if (r["got"] or None) != (r["rule_id"] if r["label"] == "match" else None)]
    for r in misses:
        print(f"MISS {r['rule_id']:6} {r['kind']:13} got={r['got']} p={r['p']} :: {r['latest'][:90]}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
