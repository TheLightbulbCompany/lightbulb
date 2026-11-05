"""The router_no_false_approval dataset (40 cases) through the guard itself: 40/40 before the codex eval exists."""

import json
from pathlib import Path

import pytest

from core.services import questions

DATASET = Path(__file__).resolve().parents[4] / "evals" / "router_no_false_approval" / "dataset.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_dataset_shape():
    assert len(CASES) == 46 and {c["expect"] for c in CASES} == {"blocked", "posted"}
    assert sum(c["expect"] == "posted" for c in CASES) == 23


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case):
    claimed = bool(questions.FALSE_APPROVAL_RE.search(case["text"]))
    assert claimed == (case["expect"] == "blocked"), case["text"]
