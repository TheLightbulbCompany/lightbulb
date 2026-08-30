"""Pins the load-bearing lines of the provisioning section of the bundled
``lightbulb`` skill.

The skill is what Friday actually reads, so its rules are as much a part of
this rail as the routes are. This test pins only the claims a wrong edit would
turn into a leak or a dead end: the three container routes, the names-only
rule, the never-sign-up-manually rule, the two-rails distinction, and the
absence of any Stripe CLI command (the CLI rail was removed — the container
has no `stripe` binary and no project of its own).
"""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[4] / "infra" / "openclaw" / "skills" / "lightbulb" / "SKILL.md"
HEADING = "## Setting up accounts for the company"


def _section() -> str:
    text = SKILL.read_text()
    assert HEADING in text, f"{HEADING} is gone from the skill"
    return "\n## " + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def test_the_three_container_routes_are_documented():
    section = _section()
    base = "$ISOL8_API_URL/api/v1/company/provisioning/u/$ISOL8_OWNER_ID"
    assert f"{base}/catalog?q=" in section
    assert f'{base}/requests"' in section
    assert f'{base}/requests/<workflow_id>"' in section


def test_credentials_are_names_only():
    section = _section()
    assert "env_keys" in section
    assert "never the value" in section


def test_the_never_sign_up_manually_rule_is_present():
    assert "Never tell the owner to sign up for something manually" in _section()


def test_the_two_rails_are_distinguished():
    section = _section()
    assert "Two rails, and they are not the same thing." in section
    assert "OAuth" in section


def test_the_section_carries_no_stripe_cli_command():
    # Task 1 removed the CLI rail. A `stripe …` line here would send Friday
    # after a binary her container does not have.
    section = _section()
    assert "stripe login" not in section.lower()
    assert "stripe plugin" not in section.lower()
    for line in section.splitlines():
        assert not line.strip().startswith("stripe "), line
