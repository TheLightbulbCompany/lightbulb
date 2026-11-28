"""Compare two models on Spark's grey-text prompt, on public text only, at equal "how often" coverage. Paid: it calls
the model provider, so it is run by a person, never by CI, and it stops at the spend cap you give.

    cd apps/backend
    OPENAI_API_KEY=sk-... uv run python -m scripts.autocomplete_quality \\
        --models gpt-6-luna gpt-5.4-nano --samples 200 --spend-cap 10

The key comes from OPENAI_API_KEY in the environment of the person running it. Nothing is read from AWS.

Data (public, read through the Hugging Face datasets-server rows API, no download):
- email: `Yale-LILY/aeslc`, Enron email bodies (the public Enron corpus);
- chat: `allenai/soda`, chat-style dialogues (CC BY 4.0).

Each sample is cut at a random word boundary: the text before the cut is the draft, the real words after it (to the
end of the clause) are the target. Each model gets the route's own prompt (`core.services.autocomplete.messages`)
and is scored with the route's own score (mean token log-probability, or the model's estimate when there are none).

Report, per model:
- whether the provider returned token log-probabilities;
- at equal coverage (each model shows its most confident 20/40/60/80/100% of samples): ExactMatch@1..5 words and
  characters saved (the longest whole-word prefix of the suggestion that matches the real text), per sample;
- how often it would show at the Mac's "How often" end points (bar −2.0, −1.0, −0.5, −0.2);
- tokens in, cached, out, and the money spent.

The spend cap: before each call the worst case (its prompt at 3 chars a token, uncached, plus its maximum output)
is added to what was spent; the run stops when that would pass the cap. Prices are OpenAI's standard tier per 1M
tokens, read from developers.openai.com/api/docs/pricing on 2026-11-28; pass --price for any other model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import urllib.parse
import urllib.request

os.environ.setdefault("CLERK_ISSUER", "https://quality-check.invalid")  # settings needs one; nothing here signs in

from core.config import settings
from core.services import autocomplete as ac

PRICES = {  # USD per 1M tokens: input, cached input, output
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5-mini": (0.25, 0.025, 2.00),
}
ROWS = "https://datasets-server.huggingface.co/rows"
SOURCES = {"email": ("Yale-LILY/aeslc", 14_436), "chat": ("allenai/soda", 1_191_582)}  # train-split sizes
CLAUSE_END = re.compile(r"[,;:.!?…\n]")


def rows(dataset: str, offset: int, length: int = 100) -> list[dict]:
    q = urllib.parse.urlencode(
        {"dataset": dataset, "config": "default", "split": "train", "offset": offset, "length": length}
    )
    with urllib.request.urlopen(f"{ROWS}?{q}", timeout=60) as r:  # noqa: S310  a fixed public https host
        return [x["row"] for x in json.load(r)["rows"]]


def cut(text: str, rng: random.Random) -> tuple[str, str] | None:
    """(before, target): a random word boundary with 5+ words before it; the target runs to the end of its clause."""
    starts = [m.start() for m in re.finditer(r"(?<=\s)\S", text)]
    if len(starts) < 8:
        return None
    at = rng.choice(starts[5:-2]) - 1  # keep the space before the next word in the target, as the route inserts it
    rest = text[at:]
    end = CLAUSE_END.search(rest, 1)
    target = rest[: end.end()] if end else rest
    return (text[:at], target) if target.strip() else None


def samples(n: int, rng: random.Random) -> list[tuple[str, dict, str]]:
    """(source, request body, target), half email and half chat."""
    out: list[tuple[str, dict, str]] = []
    for name, (dataset, size) in SOURCES.items():
        want = n // 2 if name == "email" else n - n // 2
        got: list[tuple[str, dict, str]] = []
        while len(got) < want:
            for row in rows(dataset, rng.randrange(0, size - 100)):
                if len(got) >= want:
                    break
                if name == "email":
                    pair = cut(" ".join(row["email_body"].split()[:400]), rng)
                    if pair:
                        body = {"screen": "New message\n\n" + ac.MARK, "conversation": {"kind": "email"}}
                        got.append((name, body | {"before": pair[0]}, pair[1]))
                else:
                    turns, speakers = row["dialogue"], row["speakers"]
                    if len(turns) < 2:
                        continue
                    i = rng.randrange(1, len(turns))
                    pair = cut(turns[i], rng)
                    if pair:
                        thread = "\n".join(f"{s}: {t}" for s, t in zip(speakers[:i], turns[:i], strict=False))
                        body = {
                            "screen": f"{thread}\n\n{ac.MARK}",
                            "conversation": {"kind": "chat", "recipients": [speakers[i - 1]]},
                            "last_is_mine": False,
                        }
                        got.append((name, body | {"before": pair[0]}, pair[1]))
        out += got
    rng.shuffle(out)
    return out


def words(s: str) -> list[str]:
    return s.casefold().split()


def saved(suggestion: str, target: str) -> int:
    """Characters the person would not type: the longest whole-word prefix of the suggestion that matches."""
    n = 0
    for a, b in zip(words(suggestion), words(target), strict=False):
        if a != b:
            break
        n += len(a) + 1
    return n


def report(model: str, got: list[dict]) -> None:
    ranked = sorted(got, key=lambda g: g["score"], reverse=True)
    print(f"\n== {model}: {len(got)} samples, token logprobs returned: {any(g['logprobs'] for g in got)}")
    for cov in (0.2, 0.4, 0.6, 0.8, 1.0):
        shown = [g for g in ranked[: round(cov * len(ranked))] if g["text"] and math.isfinite(g["score"])]
        em = [
            sum(words(g["text"])[:k] == words(g["target"])[:k] for g in shown) / max(len(shown), 1) for k in range(1, 6)
        ]
        chars = sum(saved(g["text"], g["target"]) for g in shown) / max(len(got), 1)
        print(
            f"  coverage {cov:.0%}: shown {len(shown)}  ExactMatch@1..5 "
            + " ".join(f"{x:.2f}" for x in em)
            + f"  chars saved per sample {chars:.1f}"
        )
    for bar in (-2.0, -1.0, -0.5, -0.2):
        print(f"  bar {bar:+.1f}: would show {sum(g['score'] >= bar for g in got) / max(len(got), 1):.0%}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", nargs=2, required=True)
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--spend-cap", type=float, required=True, help="US dollars; the run stops before passing it")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--price", action="append", default=[], help="MODEL=IN,CACHED,OUT in USD per 1M tokens")
    args = ap.parse_args()
    if not os.environ.get("OPENAI_API_KEY", "").startswith("sk-"):
        print("Set OPENAI_API_KEY to your own key first.", file=sys.stderr)
        return 2
    prices = dict(PRICES)
    for p in args.price:
        name, nums = p.split("=", 1)
        prices[name] = tuple(float(x) for x in nums.split(","))  # type: ignore[assignment]
    for m in args.models:
        if m not in prices:
            print(f"No price for {m}: pass --price {m}=IN,CACHED,OUT", file=sys.stderr)
            return 2
    rng = random.Random(args.seed)
    data = samples(args.samples, rng)
    kind = ac.Kind("cont", 1, 80, 10.0)
    provider = ac.OpenAIProvider()
    spent, results = 0.0, {m: [] for m in args.models}
    tokens = {m: [0, 0, 0] for m in args.models}
    for i, (_source, body, target) in enumerate(data):
        req = ac.CompleteRequest.model_validate(body)
        developer, user = ac.messages(req, kind)
        for m in args.models:
            pin, _pc, pout = prices[m]
            worst = (len(developer) + len(user)) / 3 * pin / 1e6 + kind.max_tokens * pout / 1e6
            if spent + worst > args.spend_cap:
                print(f"Stopped at sample {i}: the next call could pass the ${args.spend_cap:.2f} cap.")
                break
            settings.SPARK_AUTOCOMPLETE_MODEL = m
            provider.logprobs = True
            try:
                comp = await provider.complete(developer, user, kind.max_tokens, f"quality-{args.seed}", kind.timeout_s)
            except ac.ProviderError as e:
                print(f"  {m}: {e}", file=sys.stderr)
                continue
            pin, pc, pout = prices[m]
            spent += (
                (comp.input_tokens - comp.cached_tokens) * pin + comp.cached_tokens * pc + comp.output_tokens * pout
            ) / 1e6
            t = tokens[m]
            t[0], t[1], t[2] = t[0] + comp.input_tokens, t[1] + comp.cached_tokens, t[2] + comp.output_tokens
            found = [v for v in ac.versions(comp.text) if v.p > 0 and v.text.strip()]
            v = found[0] if found else None
            results[m].append(
                {
                    "text": v.text.rstrip() if v else "",
                    "score": ac.score(comp.text, comp, v) if v else -math.inf,
                    "target": target,
                    "logprobs": comp.token_logprobs is not None,
                    "i": i,
                }
            )
        else:
            continue
        break
    both = set.intersection(*({g["i"] for g in r} for r in results.values()))  # the samples every model answered
    for m in args.models:
        report(m, [g for g in results[m] if g["i"] in both])
        print(f"  tokens in {tokens[m][0]}, cached {tokens[m][1]}, out {tokens[m][2]}")
    print(f"\nSpent about ${spent:.4f} of ${args.spend_cap:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
