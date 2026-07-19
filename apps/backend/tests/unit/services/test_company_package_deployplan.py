"""DeployPlan parsing for the company deploy engine."""

from pathlib import Path

from core.services.company_package import (
    DeployPlan,
    PlannedAgent,
    parse_deploy_plan,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"


def test_parse_gstack_deploy_plan():
    plan = parse_deploy_plan(FIXTURES / "gstack")
    assert isinstance(plan, DeployPlan)
    assert all(isinstance(a, PlannedAgent) for a in plan.agents)
    assert plan.slug == "gstack"
    assert plan.ceo_slug == "ceo"
    slugs = [a.slug for a in plan.agents]
    assert set(slugs) == {"ceo", "cto", "qa-engineer", "release-engineer", "staff-engineer"}
    # CEO (no reportsTo) is first — topological order for reports_to wiring.
    assert slugs[0] == "ceo"
    ceo = next(a for a in plan.agents if a.slug == "ceo")
    assert ceo.role == "ceo" and ceo.reports_to_slug is None
    assert ceo.body_md.strip()  # AGENTS.md body is the identity content
    # A non-CEO reports to someone inside the package.
    non_ceo = [a for a in plan.agents if a.slug != "ceo"]
    assert all(a.reports_to_slug is not None for a in non_ceo)
    # reportsTo targets resolve to real package slugs.
    known = set(slugs)
    assert all(a.reports_to_slug in known for a in non_ceo)


def test_children_never_precede_their_manager():
    plan = parse_deploy_plan(FIXTURES / "gstack")
    seen: set[str] = set()
    for a in plan.agents:
        if a.reports_to_slug is not None:
            assert a.reports_to_slug in seen, f"{a.slug} before its manager"
        seen.add(a.slug)


def test_frontmatter_body_split_survives_dashes_in_body(tmp_path):
    # A body line starting with --- must not truncate the frontmatter parse or
    # leak into it.
    (tmp_path / "COMPANY.md").write_text(
        "---\nname: Dash\nslug: dash\ndescription: d\n---\nintro\n\n---\n\nmore body\n"
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "solo").mkdir()
    (tmp_path / "agents" / "solo" / "AGENTS.md").write_text(
        "---\nname: Solo\nrole: ceo\n---\nSolo body\n---\nsecond section\n"
    )
    plan = parse_deploy_plan(tmp_path)
    solo = next(a for a in plan.agents if a.slug == "solo")
    assert solo.role == "ceo"
    assert "second section" in solo.body_md  # body kept whole, dashes and all
