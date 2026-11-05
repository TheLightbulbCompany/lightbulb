"""E-rules dataset shape and the scorer (no model calls). Run from apps/backend:
uv run pytest ../../benchmarks/rules -q
"""

from collections import Counter

import pytest

import run_eval


@pytest.mark.parametrize("env", ["prod", "staging", "local"])
def test_refuses_outside_dev(env):
    with pytest.raises(SystemExit):
        run_eval.check_env(env)
    run_eval.check_env("dev")
    run_eval.check_env("")  # unset defaults to dev


def test_dataset_shape():
    rows = run_eval.load()
    assert len(rows) == 180
    for r in rows:
        assert set(r) == {"rule_id", "room", "kind", "label", "latest", "history"}
        assert r["label"] in {"match", "none"}
        assert (r["label"] == "match") == (r["kind"] == "positive")
    per_rule = Counter(r["rule_id"] for r in rows)
    assert per_rule == {"bug": 60, "refund": 60, "deck": 60}
    near = Counter(r["rule_id"] for r in rows if r["kind"] == "near_miss")
    assert all(near[k] >= 20 for k in per_rule)
    rooms = {r["scheduleId"]: r["room"] for r in run_eval.RULES}
    assert all(r["room"] == rooms[r["rule_id"]] for r in rows)
    assert len({r["latest"] for r in rows}) > 170  # no copy-paste padding


def test_each_room_sends_only_its_rule():
    rows = run_eval.rule_rows()
    assert [r["scheduleId"] for r in run_eval.rules.rules_for_room(rows, "dev")] == ["bug"]


def test_score():
    base = {"ms_rules": 100.0, "ms_plain": 90.0, "unavailable": False}
    results = [
        {**base, "rule_id": "bug", "kind": "positive", "label": "match", "got": "bug"},
        {**base, "rule_id": "bug", "kind": "positive", "label": "match", "got": None},
        {**base, "rule_id": "bug", "kind": "near_miss", "label": "none", "got": "bug"},
        {**base, "rule_id": "bug", "kind": "chatter", "label": "none", "got": None},
    ]
    s = run_eval.score(results)
    assert (s["precision"], s["recall"]) == (0.5, 0.5)
    assert s["false_positives_by_kind"]["near_miss"] == 1
    assert s["p95_delta_ms"] == 10
