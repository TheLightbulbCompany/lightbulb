"""Pins the load-bearing lines of the provisioning section of the bundled
``lightbulb`` skill.

The skill is what Friday actually reads, so its rules are as much a part of
this rail as the routes are. This pins only the claims a wrong edit would turn
into a leak or a dead end: the three container routes, the names-only rule, the
never-sign-up-manually rule, the two-rails distinction, the refusal codes on the
status that actually carries them, and the absence of any Stripe CLI command
(the CLI rail was removed — the container has no `stripe` binary).

Prose is matched against a WHITESPACE-FLATTENED copy, so re-wrapping a
paragraph does not fail a test; only changing the words does.
"""

from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[4] / "infra" / "hermes" / "skills-src" / "lightbulb"
SKILL = SKILL_DIR / "SKILL.md"
HEADING = "## Setting up accounts for the company"


def _section() -> str:
    """The provisioning section, wherever it currently lives.

    The skill was SPLIT into a thin SKILL.md + references/ read on demand
    (#2367), and this section moved to references/money.md. Searching both
    keeps the test pinned to the RULES rather than to a file layout — the
    split moved content, it did not delete it, and pinning the layout turned
    a reorganisation into twelve red tests on main.
    """
    for candidate in [SKILL, *sorted((SKILL_DIR / "references").glob("*.md"))]:
        text = candidate.read_text()
        if HEADING in text:
            return "\n## " + text.split(HEADING, 1)[1].split("\n## ", 1)[0]
    raise AssertionError(f"{HEADING} is gone from the skill and every reference file")


def _flat() -> str:
    return " ".join(_section().split())


def test_the_three_container_routes_are_documented():
    flat = _flat()
    # Path only: the skill now routes these through scripts/lightbulb-api.sh,
    # which prepends $ISOL8_API_URL itself — an inline `$ISOL8_API_URL/...`
    # curl is exactly the transcript-redaction trap the scripts exist to avoid.
    base = "/api/v1/company/provisioning/u/$ISOL8_OWNER_ID"
    assert f"{base}/catalog?q=" in flat
    assert f'{base}/requests"' in flat
    assert f'{base}/requests/<workflow_id>"' in flat


def test_credentials_are_names_only():
    flat = _flat()
    assert "env_keys" in flat
    assert "never the value" in flat


def test_the_never_sign_up_manually_rule_is_present():
    assert "Never tell the owner to sign up for something manually" in _flat()


def test_the_two_rails_are_distinguished():
    flat = _flat()
    assert "Two rails, and they are not the same thing." in flat
    assert "OAuth" in flat


def test_the_failure_table_matches_the_router():
    # `invalid_configuration` is a WorkflowRefused, so it arrives as a 409 —
    # putting it on the 422 row sends Friday to fix the wrong thing.
    rows = {line.split("|")[1].strip(): line for line in _section().splitlines() if line.startswith("| `4")}
    assert "invalid_configuration" in rows["`409`"]
    assert "service_unavailable" in rows["`409`"]
    assert "no_company`" in rows["`409`"]
    assert "invalid_configuration" not in rows["`422`"]
    assert "workflow_not_found" in rows["`404`"]
    # The router has no 429 door: a Stripe rate limit is transient, so
    # `_stripe_failure` answers 503. A 429 row sends Friday looking for a
    # status she will never see.
    assert "`429`" not in rows


def test_a_re_ask_after_an_uncertain_write_is_refused_in_both_places():
    """The one refusal that must never be worked around: the resource may
    already exist and be billing. It has to be in the failure table AND in the
    when-not-to-ask list, because Friday reads the facts file before the door."""
    section = _section()
    rows = {line.split("|")[1].strip(): line for line in section.splitlines() if line.startswith("| `4")}
    assert "needs_review_first" in rows["`409`"]
    not_to_ask = section.split("**When NOT to ask at all:**", 1)[1].split("**Step 1", 1)[0]
    assert "requires_review" in not_to_ask
    assert "needs_review_first" in not_to_ask


def test_the_catalog_envelope_and_its_gate_are_named():
    flat = _flat()
    assert "`{services, eligible, requirements}`" in flat
    assert "do not POST anything" in flat


def test_the_ask_state_has_an_exit():
    # The one dead end: nothing was filed, and `reused: true` keeps pointing at it.
    assert "| `ask` |" in _section()


def test_the_section_carries_no_stripe_cli_command():
    # Task 1 removed the CLI rail. A `stripe …` line here would send Friday
    # after a binary her container does not have.
    section = _section()
    assert "stripe login" not in section.lower()
    assert "stripe plugin" not in section.lower()
    for line in section.splitlines():
        assert not line.strip().startswith("stripe "), line


def test_the_skill_does_not_describe_agent_count_as_a_plan_entitlement():
    skill = " ".join(
        "\n".join(
            [SKILL.read_text(), *(path.read_text() for path in sorted((SKILL_DIR / "references").glob("*.md")))]
        ).split()
    )
    assert "required to run more than 10 agents" not in skill
    assert "More than 10 agents in total needs Ultra" not in skill
