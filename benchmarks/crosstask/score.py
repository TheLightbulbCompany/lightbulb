"""Scoring (spec 07 §8.6). Deterministic: the fixture's gold facts are template tokens, so fact
accuracy is a token match, and abstention is the agent's own ``abstain`` flag. No judge calls."""

import math


def leaks(question: dict, result: dict, canaries: list[dict]) -> list[str]:
    """Every canary or forbidden task id the question's audience saw or answered with. Hard gate: none."""
    seen = "\n".join([*result["read"], result["answer"]])
    out = [c["token"] for c in canaries if c["token"] in seen and question["audience"] not in c["allowed"]]
    reached = {t for hop in result["reached"] for t in hop} | set(result["cited"])
    out += [t for t in question["forbiddenTaskIds"] if t in reached or t in seen]
    return out


def task_hit(question: dict, result: dict, n: int) -> bool:
    within = {t for hop in result["reached"][:n] for t in hop}
    return bool(set(question["goldTaskIds"]) & within)


def fact_correct(question: dict, result: dict) -> bool:
    """Every gold fact named, and no superseded value (an answer hedging old and new is wrong)."""
    answer = result["answer"].lower()
    return all(f.lower() in answer for f in question["goldFacts"]) and not any(
        f.lower() in answer for f in question.get("staleFacts", [])
    )


def _rate(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, math.ceil(0.95 * len(xs)) - 1)], 4)


def summarize(questions: list[dict], results: list[dict], canaries: list[dict]) -> dict:
    by_id = {r["id"]: r for r in results}
    rows = [(q, by_id[q["id"]]) for q in questions if q["id"] in by_id]
    gold = [(q, r) for q, r in rows if q["goldTaskIds"]]
    leaked = {q["id"]: lk for q, r in rows if (lk := leaks(q, r, canaries))}
    return {
        "questions": len(rows),
        "leaks": len(leaked),
        "leakedQuestions": leaked,
        **{f"taskHit@{n}": _rate([task_hit(q, r, n) for q, r in gold]) for n in (1, 2, 3)},
        "taskHit@2Oblique": _rate([task_hit(q, r, 2) for q, r in gold if q["category"] == "oblique"]),
        "factAccuracy": _rate([fact_correct(q, r) for q, r in gold]),
        "revokedUnreachable": _rate([not leaks(q, r, []) for q, r in rows if q["category"] == "revoked"]),
        "abstention": _rate([r["abstain"] for q, r in rows if q["expectAbstain"]]),
        "byCategory": {
            cat: {
                "n": len(sub),
                "taskHit@2": _rate([task_hit(q, r, 2) for q, r in sub if q["goldTaskIds"]]),
                "factAccuracy": _rate([fact_correct(q, r) for q, r in sub if q["goldTaskIds"]]),
            }
            for cat in dict.fromkeys(q["category"] for q, _ in rows)
            for sub in [[(q, r) for q, r in rows if q["category"] == cat]]
        },
        "latencyP95S": _p95([t for _, r in rows for t in r["latencies"]]),
    }


def lift(with_links: dict, without: dict) -> dict[str, float | None]:
    """Paired differences in points; a taskHit@2 lift below +10 is flagged for review. Without links
    nothing is "reached" (reach = links or read_task), so the fact-accuracy lift is the informative one."""

    def pts(key: str) -> float | None:
        a, b = with_links.get(key), without.get(key)
        return None if a is None or b is None else round(100 * (a - b), 1)

    return {"taskHit@2": pts("taskHit@2"), "factAccuracy": pts("factAccuracy")}


def passes(summary: dict) -> dict[str, bool]:
    """The spec's bars (founder-adjustable, spec 07 Q7)."""
    return {
        "leak=0": summary["leaks"] == 0,
        "revoked=100%": summary["revokedUnreachable"] in (None, 1.0),
        "taskHit@2>=0.85": (summary["taskHit@2"] or 0) >= 0.85,
        "taskHit@2Oblique>=0.75": (summary["taskHit@2Oblique"] or 0) >= 0.75,
        "factAccuracy>=0.80": (summary["factAccuracy"] or 0) >= 0.80,
        "abstention>=0.90": (summary["abstention"] or 0) >= 0.90,
        "latencyP95<=6s": (summary["latencyP95S"] or 0) <= 6.0,
    }
