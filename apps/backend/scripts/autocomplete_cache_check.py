"""Measure prompt caching on Spark's grey-text prompt: the same contexts, 5 consecutive pauses each, before and after a
layout change. Paid (it calls the model), run by a person with a key, never by CI; it stops at the spend cap.

    # after: this checkout
    OPENAI_API_KEY=sk-... uv run python -m scripts.autocomplete_cache_check run --out /tmp/after.jsonl --spend-cap 2
    # before: the same script against main's prompt code
    git worktree add /tmp/ac-before origin/main && cp scripts/autocomplete_cache_check.py /tmp/ac-before/apps/backend/scripts/
    (cd /tmp/ac-before/apps/backend && OPENAI_API_KEY=sk-... uv run python -m scripts.autocomplete_cache_check \\
        run --out /tmp/before.jsonl --spend-cap 2)
    uv run python -m scripts.autocomplete_cache_check compare /tmp/before.jsonl /tmp/after.jsonl

Public text only (Enron email bodies, `Yale-LILY/aeslc`). Each context is one email being written, with other emails
standing in for the person's writing samples, their messages in this app, memory items and two windows visited
before, so the prompt is past the 1,024-token caching minimum. A pause is the draft one word longer than the last,
4 s later: every relative age ticks, as on a real Mac. Calls run one after another, so each can read what the last
one wrote. The model is SPARK_AUTOCOMPLETE_MODEL; prices are `autocomplete_quality.PRICES` (a cache write is billed
at 1.25× input, a read at the cached price; developers.openai.com/api/docs/guides/prompt-caching).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time

os.environ.setdefault("CLERK_ISSUER", "https://quality-check.invalid")  # settings needs one; nothing here signs in

from core.config import settings
from core.services import autocomplete as ac
from scripts.autocomplete_quality import PRICES, SOURCES, rows, words

WRITE_RATE = 1.25
GAP_S = 4


def contexts(n: int, pauses: int, rng: random.Random) -> list[list[tuple[dict, str]]]:
    """n contexts, each a list of `pauses` (request body, target) pairs in the same place."""
    dataset, size = SOURCES["email"]
    pool = [" ".join(r["email_body"].split()[:350]) for r in rows(dataset, rng.randrange(0, size - 100))]
    pool = [p for p in pool if len(p.split()) >= 40]
    out = []
    for c in range(n):
        target, others = pool[c % len(pool)], [p for p in pool if p is not pool[c % len(pool)]]
        rng.shuffle(others)
        ws = list(re.finditer(r"\S+", target))
        start = rng.randrange(6, len(ws) - pauses - 6)
        steps = []
        for i in range(pauses):
            cut = ws[start + i].end()
            rest = target[cut:]
            end = re.search(r"[,;:.!?\n]", rest[1:])
            body = {
                "before": target[:cut],
                "screen": f"Inbox · Re: the plan\n\n{others[0][:1500]}\n\n{ac.MARK}\n\nTo: Sam\nSubject: Re: the plan",
                "conversation": {"kind": "email", "header": "Sam", "recipients": []},
                "voice": [
                    {"text": o[:400], "app": "Mail", "ago_s": 3600 + 900 * k + GAP_S * i}
                    for k, o in enumerate(others[1:4])
                ],
                "style": [" ".join(o.split()[:30]) for o in others[4:12]],
                "mind": [{"text": others[12][:300], "day": "today"}],
                "recent_flow": [
                    {
                        "app_id": "com.apple.Safari",
                        "title": "Docs",
                        "text": others[13][:2000],
                        "ago_s": 40 + GAP_S * i,
                        "stay_s": 95,
                    },
                    {
                        "app_id": "com.apple.Notes",
                        "title": "Notes",
                        "text": others[14][:2000],
                        "ago_s": 300 + GAP_S * i,
                        "stay_s": 30,
                    },
                ],
                "memory": [{"text": o[:1800], "title": "Earlier email", "app_id": "Mail"} for o in others[15:19]],
                "last_is_mine": False,
                "utc_offset_min": -300,
                "bar": -2.0,
            }
            steps.append((body, rest[: end.end() + 1] if end else rest))
        out.append(steps)
    return out


async def run(args: argparse.Namespace) -> int:
    if not os.environ.get("OPENAI_API_KEY", "").startswith("sk-"):
        print("Set OPENAI_API_KEY to your own key first.", file=sys.stderr)
        return 2
    model = settings.SPARK_AUTOCOMPLETE_MODEL
    pin, pc, pout = PRICES[model]
    kind = ac.Kind("cont", 1, 80, 10.0)
    provider = ac.OpenAIProvider()
    spent = 0.0
    with open(args.out, "w") as f:
        for c, steps in enumerate(contexts(args.contexts, args.pauses, random.Random(args.seed))):
            key = f"cache-check-{args.seed}-{c}"  # one person, one place
            for i, (body, target) in enumerate(steps):
                req = ac.CompleteRequest.model_validate(body)
                developer, user = ac.messages(req, kind)
                chars = len(developer) + (len(user) if isinstance(user, str) else sum(map(len, user)))
                if spent + chars / 3 * pin * WRITE_RATE / 1e6 + kind.max_tokens * pout / 1e6 > args.spend_cap:
                    print(f"Stopped before context {c} pause {i}: the next call could pass ${args.spend_cap:.2f}.")
                    return 0
                t0 = time.monotonic()
                try:
                    comp = await provider.complete(developer, user, kind.max_tokens, key, kind.timeout_s)
                except ac.ProviderError as e:
                    print(f"context {c} pause {i}: {e}", file=sys.stderr)
                    continue
                ms = (time.monotonic() - t0) * 1000
                plain = comp.input_tokens - comp.cached_tokens - comp.cache_write_tokens
                cost = (
                    plain * pin
                    + comp.cached_tokens * pc
                    + comp.cache_write_tokens * pin * WRITE_RATE
                    + comp.output_tokens * pout
                ) / 1e6
                spent += cost
                found = [v for v in ac.versions(comp.text) if v.p > 0 and v.text.strip()]
                f.write(
                    json.dumps(
                        {
                            "ctx": c,
                            "pause": i,
                            "prompt_chars": chars,
                            "in": comp.input_tokens,
                            "cached": comp.cached_tokens,
                            "write": comp.cache_write_tokens,
                            "out": comp.output_tokens,
                            "ms": round(ms),
                            "cost": cost,
                            "insert": found[0].text.rstrip() if found else "",
                            "target": target,
                        }
                    )
                    + "\n"
                )
    print(f"Spent about ${spent:.4f}; wrote {args.out}.")
    return 0


def _load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _hit(r: dict, k: int = 2) -> bool:
    def first(s: str) -> list[str]:
        return [w.strip(".,;:!?\"'") for w in words(s)[:k]]

    return bool(r["insert"]) and first(r["insert"]) == first(r["target"])


def compare(args: argparse.Namespace) -> int:
    runs = {"before": _load(args.before), "after": _load(args.after)}
    print("| | before | after |\n|---|---|---|")
    rows_ = {
        "calls": lambda r: str(len(r)),
        "in_tokens, mean": lambda r: f"{statistics.mean(x['in'] for x in r):.0f}",
        **{
            f"cached / written, pause {p + 1}": (
                lambda r, p=p: (
                    f"{statistics.mean(x['cached'] for x in r if x['pause'] == p):.0f} / "
                    f"{statistics.mean(x['write'] for x in r if x['pause'] == p):.0f}"
                )
            )
            for p in range(max(x["pause"] for x in runs["after"]) + 1)
        },
        "model_ms p50": lambda r: f"{statistics.median(x['ms'] for x in r):.0f}",
        "model_ms p90": lambda r: f"{statistics.quantiles([x['ms'] for x in r], n=10)[-1]:.0f}",
        "cost per 1,000 pauses": lambda r: f"${1000 * statistics.mean(x['cost'] for x in r):.3f}",
        "first 2 words right": lambda r: f"{sum(map(_hit, r))} / {len(r)}",
    }
    for name, fn in rows_.items():
        print(f"| {name} | {fn(runs['before'])} | {fn(runs['after'])} |")
    after = {(x["ctx"], x["pause"]): x for x in runs["after"]}
    worse = []
    print("\nSuggestions side by side (ctx.pause: before | after | real next words):")
    for b in runs["before"]:
        a = after.get((b["ctx"], b["pause"]))
        if a is None:
            continue
        mark = "  " if a["insert"] == b["insert"] else "≠ "
        print(f"{mark}{b['ctx']}.{b['pause']}: {b['insert']!r} | {a['insert']!r} | {b['target']!r}")
        if _hit(b) and not _hit(a):
            worse.append(f"{b['ctx']}.{b['pause']}")
    print(f"\nRight before, wrong after: {', '.join(worse) or 'none'}")
    return 1 if worse else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--contexts", type=int, default=8)
    r.add_argument("--pauses", type=int, default=5)
    r.add_argument("--spend-cap", type=float, required=True, help="US dollars; the run stops before passing it")
    r.add_argument("--seed", type=int, default=11)
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = ap.parse_args()
    return asyncio.run(run(args)) if args.cmd == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
