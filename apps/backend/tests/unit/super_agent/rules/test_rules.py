"""Proactive rules, pure functions (wave2-S Task 2, spec 12 §3.1-3.4)."""

import pytest

from core.services import rules


def _rule(sid, *, room="r1", mode="observe", status="active", created="2026-11-01", sources=("room",), **extra):
    return {
        "scheduleId": sid,
        "name": "reproduce the bug",
        "policyText": f"When someone reports a bug ({sid})",
        "mode": mode,
        "status": status,
        "createdAt": created,
        "match": {"sources": list(sources), "roomIds": [room]},
        **extra,
    }


def test_jev_question_shape():
    many = [_rule(f"s{i}", created=f"2026-11-0{i}") for i in range(1, 7)]
    many[0]["policyText"] = "x" * 900
    q = rules.jev_question(many)
    assert q["type"] == "choice"
    assert q["instructions"] == rules.RULE_INSTRUCTIONS
    assert set(q["criteria"]) == {"s1", "s2", "s3", "s4", "s5", "none"}
    assert len(q["criteria"]["s1"]) <= 600
    assert q["criteria"]["none"] == rules.RULE_NONE


@pytest.mark.parametrize(
    ("p_rule", "p_none", "expected"),
    [(0.69, 0.1, None), (0.70, 0.2, "s1"), (0.5, 0.5, None), (0.8, 0.85, None)],
)
def test_match_needs_confident_p(p_rule, p_none, expected):
    answers = {"rule": {"choice": "s1", "probabilities": {"s1": p_rule, "none": p_none}}}
    sid, p = rules.parse_match(answers, {"s1", "s2"})
    assert sid == expected
    assert p == (p_rule if expected else None)


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"rule": {"choice": "s9"}},
        {"rule": {"choice": "s1", "probabilities": {"s1": "x"}}},
        {"rule": {"choice": "s1"}},
        {"rule": "s1"},
        {"rule": {"choice": "none", "probabilities": {"none": 0.9}}},
        {"rule": {"choice": "s1", "probabilities": {"s1": 0.9, "none": "x"}}},
    ],
)
def test_parse_match_malformed(answers):
    assert rules.parse_match(answers, {"s1"}) == (None, None)


def test_code_gates_order():
    gate = rules.code_gates
    assert gate(author_kind="agent", addressed=True, thread_task_id="t", thread_stopped_by_human=True) == "not_human"
    assert gate(author_kind="human", addressed=True, thread_task_id="t", thread_stopped_by_human=True) == "addressed"
    assert gate(author_kind="human", addressed=False, thread_task_id="t", thread_stopped_by_human=True) == "thread_busy"
    assert (
        gate(author_kind="human", addressed=False, thread_task_id=None, thread_stopped_by_human=True) == "human_stopped"
    )
    assert gate(author_kind="human", addressed=False, thread_task_id=None, thread_stopped_by_human=False) is None


def test_rules_for_room_scope():
    rows = [
        _rule("on", created="2026-11-02"),
        _rule("auto", mode="auto", created="2026-11-01"),
        _rule("off", mode="off"),
        _rule("paused", status="paused"),
        _rule("draft", status="draft"),
        _rule("other", room="r2"),
        {**_rule("cal", sources=("calendar",)), "match": {"sources": ["calendar"], "account": "a@x.com"}},
        {"scheduleId": "plain", "status": "active", "mode": None},
    ]
    assert [r["scheduleId"] for r in rules.rules_for_room(rows, "r1")] == ["auto", "on"]
    cap = [_rule(f"s{i}", created=f"2026-11-0{i}") for i in range(1, 8)]
    assert len(rules.rules_for_room(cap, "r1")) == rules.RULES_PER_ROOM


def test_would_do_templates():
    room = rules.would_do(_rule("s1"), {"text": "Login  page\n500s " + "y" * 200}, "UTC")
    assert room.startswith("Would reproduce the bug for: “Login page 500s ")
    assert room.endswith("”")
    assert len(room.split("“", 1)[1]) == 121  # 120 chars + closing quote
    cal_rule = {"name": "Invite prep", "match": {"sources": ["calendar"], "account": "a@x.com"}}
    line = rules.would_do(cal_rule, {"title": "Q4 review", "start": "2026-11-05T19:00:00Z"}, "America/New_York")
    assert line == "Would prep a reply to “Q4 review” (Thu 5 Nov 14:00)"


def test_validate_scope():
    v = rules.validate_scope
    ok = {"member_room_ids": {"r1"}, "active_rules_by_room": {"r1": 4}, "builtin": None}
    assert v({"sources": ["room"], "roomIds": ["r1"]}, **ok) is None
    assert v(None, **ok) == "scope_missing"
    assert v({"sources": []}, **ok) == "scope_missing"
    assert v({"sources": ["room"], "roomIds": []}, **ok) == "scope_missing"
    assert v({"sources": ["calendar"]}, **ok) == "scope_missing"
    assert v({"sources": ["calendar"], "account": "a@x.com"}, **ok) is None
    assert v({"sources": ["room", "calendar"], "roomIds": ["r1"]}, **ok) == "one_source_only"
    assert v({"sources": ["room"], "roomIds": ["r9"]}, **ok) == "room_not_member"
    assert v({"sources": ["room"], "roomIds": ["r1"]}, **{**ok, "active_rules_by_room": {"r1": 5}}) == "room_rule_cap"
    assert v({"sources": ["room"], "roomIds": ["r1"]}, **{**ok, "builtin": "repro_fix"}) == "repo_required"
    assert v({"sources": ["room"], "roomIds": ["r1"], "repo": "acme/web"}, **{**ok, "builtin": "repro_fix"}) is None


def test_matches_on_version():
    row = {"version": 9, "ruleVersion": 2, "stats": {"matches": [{"version": 1}, {"version": 2}, {"version": 2}]}}
    assert rules.matches_on_version(row) == 2
    assert rules.matches_on_version({"version": 1}) == 0
    assert rules.matches_on_version({"version": 7, "stats": {"matches": [{"version": 1}]}}) == 1  # default 1
