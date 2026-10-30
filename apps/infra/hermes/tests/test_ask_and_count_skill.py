"""Blocked or unsure goes to ask_person; a count of tasks comes from lightbulb_board.

G1 (30 Oct 2026, 42 real failures): the paperclip skill told a blocked agent that "the comment is
what gets a human's attention", and nobody read the comments (ISOA-867 sat 8 days, ISOA-752 had
Aden chasing twice). An agent stated "only 3 open cards" with 55 open. Both tools exist in the
lightbulb plugin; the skills must send the model to them.
"""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def _built(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    return out


def test_a_blocked_agent_asks_with_ask_person_not_a_comment(tmp_path):
    heartbeat = (_built(tmp_path) / "paperclip/references/heartbeat.md").read_text()
    assert "the comment is what gets a human's attention" not in heartbeat
    assert "ask_person(" in heartbeat and "blocked=true" in heartbeat


def test_interactions_reference_says_when_to_ask_instead_of_approve(tmp_path):
    text = (_built(tmp_path) / "paperclip/references/interactions.md").read_text()
    assert "## When you need an answer, not a sign-off: ask_person" in text


def test_both_skills_carry_the_ask_and_count_rules(tmp_path):
    out = _built(tmp_path)
    for rel in ("paperclip/SKILL.md", "lightbulb/SKILL.md"):
        text = (out / rel).read_text()
        assert "`ask_person`" in text, rel
        assert "`lightbulb_board`" in text, rel


def test_the_waiting_posture_rule_agrees_with_ask_person(tmp_path):
    """heartbeat.md's posture line said in_review for every ask_user_questions wait while the
    blocked rule says ask_person(blocked=true) parks it blocked (final review, 30 Oct)."""
    heartbeat = (_built(tmp_path) / "paperclip/references/heartbeat.md").read_text()
    assert "`ask_person` with `blocked=true` already parks it `blocked`" in heartbeat


def test_the_skill_says_one_open_question_per_task(tmp_path):
    """The fork expires an agent's earlier pending ask_user_questions when it files another on the
    same issue, and replays a key only within one run (final review, 30 Oct)."""
    text = (_built(tmp_path) / "paperclip/references/interactions.md").read_text()
    assert "does not make a second card" not in text
    assert "One open question per task" in text
