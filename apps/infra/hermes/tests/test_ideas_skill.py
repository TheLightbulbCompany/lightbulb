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
    # The contract the backend's Start relies on: the tips path, the per-start project, no plan.
    assert "$HERMES_HOME/ideas/<idea>.md" in ideas
    assert "Start working right away" in ideas and "tips, not required steps" in ideas
    assert "There is no plan to file" in ideas
    assert "a note only you see" in ideas and "the project the note names" in ideas
    # Approvals only before outside actions in the person's name, linked to this start's task.
    assert "only before an action in the outside world" in ideas and "tasks=[" in ideas
    assert "only in the live browser" in ideas
    assert "--feed --why" in ideas and "Done only with proof" in ideas
    assert "Promise the action, never the result" in ideas
    assert "ideas.md" in (HERMES / "skills-src/lightbulb/MAINTENANCE.md").read_text()
    # The backend's agent note points at the same two files.
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "$HERMES_HOME/ideas/{idea.id}.md" in service and "references/ideas.md" in service
