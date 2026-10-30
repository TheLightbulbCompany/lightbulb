"""agent_evals checks: the machine checks that fail a case whatever the judge says."""

from scripts.agent_evals.checks import run_checks

BOARD = [{"title": "Ship the ChatGPT plugin", "status": "todo"}, {"title": "Ship the Claude plugin", "status": "todo"}]


def _one(check, reply="", before=BOARD, after=BOARD):
    return run_checks([check], reply, before, after)


def test_contains_any_is_case_insensitive_and_fails_when_absent():
    assert _one({"kind": "contains_any", "values": ["Hacker News", "HN"]}, "we go with hacker news") == []
    assert _one({"kind": "contains_any", "values": ["12"]}, "there are 3 open") != []


def test_contains_none_fails_on_any_listed_phrase():
    assert _one({"kind": "contains_none", "values": ["email sent"]}, "I can't send mail yet") == []
    assert _one({"kind": "contains_none", "values": ["email sent"]}, "Done, Email sent!") != []


def test_max_chars():
    assert _one({"kind": "max_chars", "n": 5}, "short") == []
    assert _one({"kind": "max_chars", "n": 5}, "too long") != []


def test_tasks_unchanged_fails_on_a_new_task_or_a_status_change():
    assert _one({"kind": "tasks_unchanged"}) == []
    assert _one({"kind": "tasks_unchanged"}, after=BOARD + [{"title": "Invite bug", "status": "todo"}]) != []
    moved = [{**BOARD[0], "status": "cancelled"}, BOARD[1]]
    assert _one({"kind": "tasks_unchanged"}, after=moved) != []


def test_task_status_fails_when_missing_or_different():
    check = {"kind": "task_status", "title": "Ship the ChatGPT plugin", "status": "cancelled"}
    assert _one(check, after=[{**BOARD[0], "status": "cancelled"}, BOARD[1]]) == []
    assert _one(check) != []
    assert _one(check, after=[BOARD[1]]) != []


def test_several_failures_are_all_reported():
    checks = [{"kind": "max_chars", "n": 1}, {"kind": "contains_any", "values": ["x"]}]
    assert len(run_checks(checks, "abc", BOARD, BOARD)) == 2
