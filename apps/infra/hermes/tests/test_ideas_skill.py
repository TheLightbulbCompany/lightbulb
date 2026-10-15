"""The lightbulb skill's short Ideas reference, matching the backend's message and the plugin tool."""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]
BACKEND = HERMES.parents[1] / "backend"


def test_the_skill_routes_ideas_to_a_short_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ideas.md`" in line)
    assert row.startswith("|") and "suggest_idea" in row
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    assert len(ideas.splitlines()) < 30  # short, by owner ruling
    assert "Do it like any request" in ideas
    # A project: break it into assigned tasks first, so it continues after the reply.
    assert "break the work into tasks in that\n  project, each assigned to yourself or a teammate" in ideas
    assert "first move the linked task to in_progress (assigned to yourself),\n  then work it and close it with proof" in ideas
    assert "sign-in (live browser only)" in ideas and "`request_approval`" in ideas
    assert "to the feed" in ideas
    # suggest_idea: concrete and company-specific only; never in chat, never on a schedule.
    assert "`suggest_idea` only when you notice a concrete, company-specific job" in ideas
    assert "not to chat" in ideas and "never on a schedule" in ideas
    # No plan step, no hidden notes, no guide files.
    assert "plan" not in ideas.lower() and "$HERMES_HOME/ideas" not in ideas
    # The backend's message carries the phrases the reference keys on.
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "Let's do it!" in service and "Tracked in Work: [" in service
    plugin = (HERMES / "plugins/lightbulb/plugin.yaml").read_text()
    assert "- suggest_idea" in plugin
