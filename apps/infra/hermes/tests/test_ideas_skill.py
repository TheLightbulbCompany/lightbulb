"""The lightbulb skill routes a started idea to references/ideas.md, and the built tree ships it."""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]
BACKEND = HERMES.parents[1] / "backend"


def test_the_skill_routes_a_started_idea_to_the_ideas_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ideas.md`" in line)
    assert row.startswith("|") and "started an idea" in row
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    # The contract the backend's Start relies on: the guide path, the plan approval, the proof.
    assert "$HERMES_HOME/ideas/<idea>.md" in ideas
    assert 'request_approval(title="Plan: <idea title>"' in ideas and "tasks=[" in ideas
    assert "request_confirmation" in ideas and "--feed --why" in ideas
    assert "Nothing is sent, published, bought or merged before the plan is approved" in ideas
    assert "Promise the action, never the result" in ideas
    assert "ideas.md" in (HERMES / "skills-src/lightbulb/MAINTENANCE.md").read_text()
    # The backend's wake line points at the same two files.
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "$HERMES_HOME/ideas/{idea.id}.md" in service and "references/ideas.md" in service
