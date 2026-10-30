"""Agent eval cases: one JSON object per line of ``cases.jsonl``, each from a real failure (G1).

A case is plain English so anyone can add one: what the person says (``say``), what a good reply
does (``good``) and what the real failure did (``bad``). ``seed`` sets the room up first (tasks on
the board, earlier messages); ``checks`` are the machine checks that fail a case whatever the judge
says. ``needs: "paid-workspace"`` marks a case that needs a background task run, which a trial
fixture workspace never dispatches (routers/teams/_guards.py): it is loaded but skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CASES = Path(__file__).parent / "cases.jsonl"
CHECK_KINDS = frozenset({"contains_any", "contains_none", "max_chars", "tasks_unchanged", "task_status"})
SEED_KEYS = frozenset({"tasks", "before"})


@dataclass(frozen=True)
class Case:
    id: str
    source: str
    say: str
    good: str
    bad: str
    seed: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    needs: str | None = None


def load_cases(path: Path = CASES) -> list[Case]:
    out: list[Case] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        for key in ("id", "say", "good", "bad"):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"line {n}: missing {key}")
        if row["id"] in seen:
            raise ValueError(f"line {n}: duplicate id {row['id']}")
        if bad := set(row.get("seed") or {}) - SEED_KEYS:
            raise ValueError(f"line {n}: unknown seed key {sorted(bad)}")
        if bad := [c.get("kind") for c in row.get("checks") or [] if c.get("kind") not in CHECK_KINDS]:
            raise ValueError(f"line {n}: unknown check kind {bad}")
        seen.add(row["id"])
        out.append(
            Case(
                id=row["id"],
                source=row.get("source", ""),
                say=row["say"],
                good=row["good"],
                bad=row["bad"],
                seed=row.get("seed") or {},
                checks=row.get("checks") or [],
                needs=row.get("needs"),
            )
        )
    return out
