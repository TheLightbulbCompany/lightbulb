"""Scoring for the voice eval bar: pass^k per scenario plus an aggregate floor. Synthetic data, no network."""

import pytest

from passk import BAR, exit_code, score, card


def runs(**kw):
    return {name: list(r) for name, r in kw.items()}


def test_pass3_fails_scenario_with_one_bad_run():
    v = score(runs(book=[True, True, True], cancel=[True, False, True]))
    assert v["scenarios"] == {"book": True, "cancel": False}
    assert v["rate"] == pytest.approx(5 / 6)
    assert not v["ok"]


def test_pass3_needs_k_runs():
    assert not score(runs(book=[True, True]))["scenarios"]["book"]  # 2 runs is not pass^3


def test_check_exits_nonzero_below_bar():
    # every scenario passes pass^3 only if all runs pass, so the aggregate can only be below the bar with a failure,
    # but the aggregate rule still holds on its own at a stricter bar
    assert exit_code(score(runs(a=[True] * 3, b=[True, False, True]))) == 1
    assert exit_code(score(runs(a=[True] * 3, b=[True] * 3))) == 0
    assert exit_code(score(runs(a=[True] * 3, b=[True] * 3), bar=1.1)) == 1
    assert BAR == 0.9


def test_card_has_table_and_unchecked_dialin_line():
    md = card("phone", score(runs(a=[True] * 3, b=[True, False, True])), "20261107-1200")
    assert "| a | 3/3 | pass |" in md and "| b | 2/3 | FAIL |" in md
    assert "- [ ] 20+ real dial-ins" in md
