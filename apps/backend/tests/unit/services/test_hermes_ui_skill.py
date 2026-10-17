"""The lightbulb skill routes card-shaped replies to references/ui.md, and the built tree ships it
with every component name the shared library defines (packages/shared/src/ui/library.ts)."""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
BUILD = REPO / "apps/infra/hermes/build-skills.sh"
LIBRARY = REPO / "packages/shared/src/ui/library.ts"


def _names() -> list[str]:
    return re.findall(r'name: "([A-Za-z]+)"', LIBRARY.read_text())


def test_the_skill_routes_cards_to_the_ui_reference_and_ships_every_signature(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ui.md`" in line)
    assert row.startswith("|") and "openui-lang" in row
    assert "Ask with a card" in skill and "request_approval" in skill
    assert "## How you reply in chat" in skill and "Lead with the answer" in skill
    ui = (out / "lightbulb/references/ui.md").read_text()
    names = _names()
    assert len(names) == 20
    for name in names:
        assert f"{name}(" in ui, name
    assert "<!-- openui:signatures:start -->" in ui and "request_approval" in ui
    assert "ui.md" in (REPO / "apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md").read_text()
