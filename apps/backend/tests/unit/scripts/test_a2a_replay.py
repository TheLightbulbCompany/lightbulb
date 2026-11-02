import json
from pathlib import Path

from scripts.a2a_replay.__main__ import run


def test_replay_cases_all_match():
    cases = json.loads((Path(__file__).parents[3] / "scripts/a2a_replay/cases.json").read_text())["cases"]
    rows = run(cases)
    assert len(rows) >= 12
    assert all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]


def test_mutated_case_reports_not_ok():
    cases = json.loads((Path(__file__).parents[3] / "scripts/a2a_replay/cases.json").read_text())["cases"]
    assert run([{**cases[0], "expected": "allow"}])[0]["ok"] is False
