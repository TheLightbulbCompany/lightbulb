from pathlib import Path
import subprocess


HERMES = Path(__file__).parents[1]


def test_lightbulb_skill_routes_google_checks_to_managed_gog_cli(tmp_path):
    out = tmp_path / "skills"
    subprocess.run(
        [str(HERMES / "build-skills.sh"), str(out)],
        check=True,
        capture_output=True,
    )

    skill = (out / "lightbulb/SKILL.md").read_text()
    integrations = (out / "lightbulb/references/integrations.md").read_text()

    assert "Google Workspace on Lightbulb uses the managed `gog` CLI" in skill
    assert "do not run the generic Google Workspace skill's `setup.py --check`" in integrations
    assert "`gog auth status`" in integrations
