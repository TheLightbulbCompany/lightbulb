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


def test_every_planned_role_is_one_paperclip_accepts():
    """Paperclip validates role with z.enum(AGENT_ROLES) and 400s anything else.

    A 400 on agent-create rolls back the whole company deploy, so an
    unmappable role in package frontmatter is not cosmetic — it kills the
    import. Package AGENTS.md files routinely omit ``role:`` entirely (the
    gstack fixture sets it on none of its five agents), so the fallback has
    to land inside the enum.
    """
    from core.services.agent_meta import AGENT_ROLES

    plan = parse_deploy_plan(FIXTURES / "gstack")
    assert len(plan.agents) > 1, "need a non-CEO agent to exercise the fallback"
    bad = [(a.slug, a.role) for a in plan.agents if a.role not in AGENT_ROLES]
    assert not bad, f"roles Paperclip would reject: {bad}"


def test_free_text_role_is_coerced(tmp_path):
    """Author-written frontmatter carries prose like 'Head of Growth'."""
    from core.services.agent_meta import AGENT_ROLES

    (tmp_path / "COMPANY.md").write_text("---\nslug: acme\n---\nAcme\n")
    for slug, role in (("boss", "ceo"), ("growth", "Head of Growth"), ("eng", "CTO")):
        d = tmp_path / "agents" / slug
        d.mkdir(parents=True)
        reports = "null" if slug == "boss" else "boss"
        (d / "AGENTS.md").write_text(f"---\nname: {slug}\nrole: {role}\nreportsTo: {reports}\n---\nbody\n")

    plan = parse_deploy_plan(tmp_path)
    roles = {a.slug: a.role for a in plan.agents}
    assert all(r in AGENT_ROLES for r in roles.values()), roles
    assert roles["boss"] == "ceo"
    assert roles["eng"] == "cto"  # case-normalized, not dropped
    assert roles["growth"] == "general"  # unmappable prose falls back
