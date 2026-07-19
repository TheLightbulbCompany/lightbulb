"""Parse agentcompanies/v1 packages (the companies.sh format).

PR1 scope: metadata only — counts, frontmatter, env requirements — for
marketplace ingestion. The company-deploy engine (PR2) extends this module
with full DeployPlan parsing. Format reference: the Agent Companies
Specification (schema ``agentcompanies/v1``), vendored fixture under
``tests/fixtures/companies/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

COMPANY_STANDARD_PLAN_MAX_AGENTS = 10


@dataclass
class CompanyMeta:
    slug: str
    name: str
    description: str
    version: str | None
    license: str | None
    authors: list[str]
    agent_count: int
    team_count: int
    skill_count: int
    agent_slugs: list[str]
    team_slugs: list[str]
    skill_slugs: list[str]
    env_requirements: list[dict]

    @property
    def min_plan(self) -> str:
        if self.agent_count <= COMPANY_STANDARD_PLAN_MAX_AGENTS:
            return "standard"
        return "ultra_pro"

    def to_manifest_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "license": self.license,
            "authors": self.authors,
            "agent_count": self.agent_count,
            "team_count": self.team_count,
            "skill_count": self.skill_count,
            "agent_slugs": self.agent_slugs,
            "team_slugs": self.team_slugs,
            "skill_slugs": self.skill_slugs,
            "env_requirements": self.env_requirements,
            "min_plan": self.min_plan,
        }


def _frontmatter(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data = yaml.safe_load(text[3:end])
    return data if isinstance(data, dict) else {}


def _child_slugs(pkg_dir: Path, subdir: str, marker: str) -> list[str]:
    return sorted(p.parent.name for p in pkg_dir.glob(f"{subdir}/*/{marker}"))


def _env_requirements(pkg_dir: Path) -> list[dict]:
    pc_path = pkg_dir / ".paperclip.yaml"
    if not pc_path.is_file():
        return []
    data = yaml.safe_load(pc_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for agent_slug, acfg in (data.get("agents") or {}).items():
        env = ((acfg or {}).get("inputs") or {}).get("env") or {}
        for env_name, decl in env.items():
            decl = decl or {}
            out.append(
                {
                    "name": env_name,
                    "kind": decl.get("kind", "plain"),
                    "requirement": decl.get("requirement", "optional"),
                    "agent": agent_slug,
                }
            )
    return out


def parse_company_meta(pkg_dir: Path) -> CompanyMeta:
    company_md = pkg_dir / "COMPANY.md"
    if not company_md.is_file():
        raise ValueError(f"not a company package (no COMPANY.md): {pkg_dir}")
    fm = _frontmatter(company_md)
    agent_slugs = _child_slugs(pkg_dir, "agents", "AGENTS.md")
    team_slugs = _child_slugs(pkg_dir, "teams", "TEAM.md")
    skill_slugs = _child_slugs(pkg_dir, "skills", "SKILL.md")
    authors = [a.get("name", "") if isinstance(a, dict) else str(a) for a in (fm.get("authors") or [])]
    return CompanyMeta(
        slug=str(fm.get("slug") or pkg_dir.name),
        name=str(fm.get("name") or pkg_dir.name),
        description=str(fm.get("description") or ""),
        version=fm.get("version"),
        license=fm.get("license"),
        authors=[a for a in authors if a],
        agent_count=len(agent_slugs),
        team_count=len(team_slugs),
        skill_count=len(skill_slugs),
        agent_slugs=agent_slugs,
        team_slugs=team_slugs,
        skill_slugs=skill_slugs,
        env_requirements=_env_requirements(pkg_dir),
    )
