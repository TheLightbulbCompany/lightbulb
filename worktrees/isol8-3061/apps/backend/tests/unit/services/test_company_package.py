"""Tests for the agentcompanies/v1 metadata parser."""

from pathlib import Path

import pytest

from core.services.company_package import CompanyMeta, parse_company_meta

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"


def test_parse_gstack_real_package():
    meta = parse_company_meta(FIXTURES / "gstack")
    assert meta.slug == "gstack"
    assert meta.name == "GStack"
    assert meta.license == "MIT"
    assert meta.agent_count == 5
    assert sorted(meta.agent_slugs) == [
        "ceo",
        "cto",
        "qa-engineer",
        "release-engineer",
        "staff-engineer",
    ]
    assert meta.team_count == 1
    assert meta.skill_count == 27
    assert "Dotta" in meta.authors and "Garry Tan" in meta.authors
    # .paperclip.yaml declares an optional GH_TOKEN secret for release-engineer
    gh = [r for r in meta.env_requirements if r["name"] == "GH_TOKEN"]
    assert gh and gh[0]["requirement"] == "optional" and gh[0]["kind"] == "secret"
    assert meta.min_plan == "standard"


def test_large_company_is_standard_compatible():
    meta = CompanyMeta(
        slug="big",
        name="Big",
        description="",
        version=None,
        license="MIT",
        authors=[],
        agent_count=37,
        team_count=0,
        skill_count=0,
        agent_slugs=[],
        team_slugs=[],
        skill_slugs=[],
        env_requirements=[],
    )
    assert meta.min_plan == "standard"


def test_rejects_non_company_dir(tmp_path):
    with pytest.raises(ValueError, match="COMPANY.md"):
        parse_company_meta(tmp_path)


def test_missing_optional_files_are_fine(tmp_path):
    (tmp_path / "COMPANY.md").write_text("---\nname: Tiny\nslug: tiny\ndescription: d\n---\nbody\n")
    meta = parse_company_meta(tmp_path)
    assert meta.slug == "tiny"
    assert meta.agent_count == 0
    assert meta.env_requirements == []
    assert meta.license is None


def test_numeric_frontmatter_scalars_coerce_to_str(tmp_path):
    """YAML parses `version: 1.1` as a float. The parser must coerce it to a
    string — a float here violates the annotation and, worse, fails the DDB
    Map write after the S3 upload and draft row have already happened."""
    (tmp_path / "COMPANY.md").write_text(
        "---\nname: Tiny\nslug: tiny\ndescription: d\nversion: 1.1\nlicense: 2026\n---\nbody\n"
    )
    meta = parse_company_meta(tmp_path)
    assert meta.version == "1.1"
    assert meta.license == "2026"
