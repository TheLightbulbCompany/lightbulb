"""The lightbulb skill routes Live voice requests to references/voice.md, and the built tree ships it."""
import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]
TOOL = "lightbulb_voice_profile"  # plugins/lightbulb/__init__.py registers it under this name


def test_the_skill_routes_voice_requests_to_the_voice_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/voice.md`" in line)
    assert row.startswith("|") and "Live voice requests" in row
    voice = (out / "lightbulb/references/voice.md").read_text()
    assert TOOL in voice
    assert TOOL in (HERMES / "plugins/lightbulb/__init__.py").read_text()
    assert "voice.md" in (HERMES / "skills-src/lightbulb/MAINTENANCE.md").read_text()
