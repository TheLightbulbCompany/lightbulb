"""The lightbulb skill routes an idea someone sent to references/ideas.md, matching the backend's message."""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]
BACKEND = HERMES.parents[1] / "backend"


def test_the_skill_routes_an_idea_to_its_one_paragraph_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ideas.md`" in line)
    assert row.startswith("|") and "Tracked in Work" in row
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    assert "do it like any request" in ideas and '"Tracked in Work"' in ideas
    # The backend's message carries the same phrases the reference keys on.
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "Let's do it!" in service and "Tracked in Work: [" in service
