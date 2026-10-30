"""Blocked on a person goes to request_approval or a DM, never only a comment; a count of tasks
comes from the board, through the Paperclip skill's own list route.

G1 (30 Oct 2026, 42 real failures): the paperclip skill told a blocked agent that "the comment is
what gets a human's attention", and nobody read the comments (ISOA-867 sat 8 days, ISOA-752 had
Aden chasing twice). An agent stated "only 3 open cards" with 55 open. Two wrapper tools for these
(ask_person, lightbulb_board, #4495) were removed: the existing paths already do both.
"""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def _built(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    return out


def test_a_blocked_agent_reaches_a_person_not_only_a_comment(tmp_path):
    heartbeat = (_built(tmp_path) / "paperclip/references/heartbeat.md").read_text()
    assert "the comment is what gets a human's attention" not in heartbeat
    assert "A comment alone reaches nobody" in heartbeat
    block = heartbeat.split("If the block needs a human decision", 1)[1].split("If you cannot name an action", 1)[0]
    assert "`request_approval`" in block and "DM" in block


def test_both_skills_carry_the_blocked_and_count_rules(tmp_path):
    out = _built(tmp_path)
    for rel in ("paperclip/SKILL.md", "lightbulb/SKILL.md"):
        text = (out / rel).read_text()
        assert "Blocked on a person" in text, rel
        assert "Any count or status of tasks" in text, rel


def test_the_count_rule_points_at_the_skills_own_list_route(tmp_path):
    text = (_built(tmp_path) / "paperclip/SKILL.md").read_text()
    assert "references/hot-routes.md" in text.split("Any count or status of tasks", 1)[1].split("\n", 1)[0]


def test_no_wrapper_tool_or_skill_text_remains(tmp_path):
    import sys

    sys.path.insert(0, str(HERMES / "plugins"))
    import lightbulb

    assert not hasattr(lightbulb, "_board") and not hasattr(lightbulb, "_ask_person")
    manifest = (HERMES / "plugins/lightbulb/plugin.yaml").read_text()
    assert "lightbulb_board" not in manifest and "ask_person" not in manifest
    out = _built(tmp_path)
    for rel in ("paperclip/SKILL.md", "lightbulb/SKILL.md", "paperclip/references/heartbeat.md",
                "paperclip/references/interactions.md"):
        assert "ask_person" not in (out / rel).read_text(), rel
