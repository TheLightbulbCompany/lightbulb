"""Blocked or unsure goes to ask_person; a count of tasks comes from the board itself (no wrapper tool).

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


def test_the_skill_carries_the_ask_and_count_rules(tmp_path):
    text = (_built(tmp_path) / "lightbulb/SKILL.md").read_text()
    assert "`ask_person`" in text
    assert "`lightbulb_board`" not in text
    assert "Any count or status of tasks" in text


def test_the_paperclip_skill_is_not_baked(tmp_path):
    """Paperclip exit (task 8b.2): no profile has a Paperclip key, so its skill is gone from the image."""
    assert not (_built(tmp_path) / "paperclip").exists()


def test_no_wrapper_board_tool_is_registered():
    import sys
    sys.path.insert(0, str(HERMES / "plugins"))
    import lightbulb

    assert not hasattr(lightbulb, "_board")
    assert "lightbulb_board" not in (HERMES / "plugins/lightbulb/plugin.yaml").read_text()
