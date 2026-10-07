"""The lightbulb skill tells agents a session's chat is for people, and the built tree ships it."""
import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def test_the_skill_keeps_session_chat_answers_in_the_chat(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    surfaces = (out / "lightbulb/references/surfaces.md").read_text()
    assert "**Session chat**" in surfaces
    assert "`Session chat · <title>`" in surfaces
    assert "Never send a chat message into the session" in surfaces
    assert "Session chat" in (HERMES / "skills-src/lightbulb/MAINTENANCE.md").read_text()
