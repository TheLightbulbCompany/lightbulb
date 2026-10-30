"""agent_evals cases: the shipped file loads, and a malformed line is refused by its line number."""

import json

import pytest

from scripts.agent_evals import cases as mod


def _write(tmp_path, rows):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


ROW = {"id": "a", "source": "G1#1", "say": "hi", "good": "answers", "bad": "does not"}


def test_shipped_cases_load_and_every_id_is_unique():
    loaded = mod.load_cases(mod.CASES)
    assert len({c.id for c in loaded}) == len(loaded)
    assert len([c for c in loaded if c.needs is None]) >= 14


def test_unknown_check_kind_names_the_line(tmp_path):
    path = _write(tmp_path, [ROW, {**ROW, "id": "b", "checks": [{"kind": "vibes"}]}])
    with pytest.raises(ValueError, match="line 2.*vibes"):
        mod.load_cases(path)


def test_missing_good_is_refused(tmp_path):
    path = _write(tmp_path, [{k: v for k, v in ROW.items() if k != "good"}])
    with pytest.raises(ValueError, match="line 1.*good"):
        mod.load_cases(path)


def test_duplicate_id_is_refused(tmp_path):
    with pytest.raises(ValueError, match="line 2.*duplicate"):
        mod.load_cases(_write(tmp_path, [ROW, ROW]))


def test_unknown_seed_key_is_refused(tmp_path):
    with pytest.raises(ValueError, match="line 1.*seed"):
        mod.load_cases(_write(tmp_path, [{**ROW, "seed": {"files": []}}]))


def test_paid_workspace_cases_are_loaded_but_marked(tmp_path):
    [case] = mod.load_cases(_write(tmp_path, [{**ROW, "needs": "paid-workspace"}]))
    assert case.needs == "paid-workspace"
    assert case.checks == [] and case.seed == {}
