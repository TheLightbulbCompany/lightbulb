"""Parse agentcompanies/v1 packages (the companies.sh format).

Two parse layers live here. ``parse_company_meta`` is metadata only — counts,
frontmatter, env requirements — for marketplace ingestion. ``parse_deploy_plan``
is the full deploy engine view: agents with their AGENTS.md bodies and
``reportsTo`` edges (topologically ordered, managers before reports) plus
routines. Format reference: the Agent Companies Specification (schema
``agentcompanies/v1``), vendored fixture under ``tests/fixtures/companies/``.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from core.services.agent_meta import AGENT_ROLES


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
        return "standard"

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


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading ``---``-fenced YAML frontmatter from the markdown body.

    Returns ``(frontmatter_dict, body)``. The closing fence must be a line that
    is exactly ``---`` (on its own line) — a body line that merely *starts* with
    ``---`` does not close the block, so bodies keep their dashes intact. A
    naive ``text.find("\\n---")`` truncates on such a line; this walks lines
    instead. If there is no opening/closing fence pair the whole text is body.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            data = yaml.safe_load("".join(lines[1:i]))
            body = "".join(lines[i + 1 :])
            return (data if isinstance(data, dict) else {}), body
    return {}, text


def _frontmatter(md_path: Path) -> dict:
    fm, _ = _split_frontmatter(md_path.read_text(encoding="utf-8"))
    return fm


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
        # YAML scalars like `version: 1.1` parse as float — coerce to str so
        # downstream consumers (and the DDB Map write) always see strings.
        version=str(fm["version"]) if fm.get("version") is not None else None,
        license=str(fm["license"]) if fm.get("license") is not None else None,
        authors=[a for a in authors if a],
        agent_count=len(agent_slugs),
        team_count=len(team_slugs),
        skill_count=len(skill_slugs),
        agent_slugs=agent_slugs,
        team_slugs=team_slugs,
        skill_slugs=skill_slugs,
        env_requirements=_env_requirements(pkg_dir),
    )


@dataclass
class PlannedAgent:
    slug: str
    name: str
    role: str
    title: str | None
    body_md: str
    reports_to_slug: str | None
    skills: list[str]
    capabilities: str | None


@dataclass
class PlannedRoutine:
    agent_slug: str
    title: str
    description: str
    cron: str | None


@dataclass
class DeployPlan:
    slug: str
    agents: list[PlannedAgent]
    routines: list[PlannedRoutine]
    ceo_slug: str


def _agent_slug_from_ref(ref) -> str:
    """Resolve an agent reference to its slug.

    A reference is either a bare slug (``cto``) or a relative path to the
    agent's markdown file (``../../agents/cto/AGENTS.md``). The slug is the
    name of the directory that holds the ``AGENTS.md``, so basename the path
    to the parent dir when it points at an ``AGENTS.md`` and otherwise take the
    final path segment.
    """
    cleaned = str(ref).strip().strip("/")
    if not cleaned:
        return ""
    p = PurePosixPath(cleaned)
    if p.name.lower() == "agents.md":
        return p.parent.name
    return p.name


def _agent_role(raw: str | None, *, is_ceo: bool) -> str:
    """Coerce an AGENTS.md ``role:`` into ``AGENT_ROLES``: package frontmatter carries free text like
    "Head of Growth", so anything else becomes ``ceo`` for the CEO and ``general`` otherwise."""
    role = (raw or "").strip().lower()
    if role in AGENT_ROLES:
        return role
    return "ceo" if is_ceo else "general"


def _team_managers(pkg_dir: Path) -> dict[str, str]:
    """Map each team member's slug to its team ``manager`` slug.

    A member only inherits the manager as its reports-to when its own AGENTS.md
    omits ``reportsTo``; the manager never reports to itself.
    """
    out: dict[str, str] = {}
    for team_md in sorted(pkg_dir.glob("teams/*/TEAM.md")):
        fm, _ = _split_frontmatter(team_md.read_text(encoding="utf-8"))
        mgr = fm.get("manager")
        if not isinstance(mgr, str) or not mgr.strip():
            continue
        mgr_slug = _agent_slug_from_ref(mgr)
        for member in fm.get("includes") or []:
            if not isinstance(member, str):
                continue
            member_slug = _agent_slug_from_ref(member)
            if member_slug and member_slug != mgr_slug:
                out.setdefault(member_slug, mgr_slug)
    return out


def _topo_order(agents: dict[str, PlannedAgent], ceo_slug: str) -> list[PlannedAgent]:
    """Order agents so a manager always precedes its reports (Kahn's algorithm).

    Ties break by slug for determinism; the CEO is popped first. Any agents
    caught in a cycle are appended by slug so nodes are never dropped.
    """
    children: dict[str, list[str]] = {s: [] for s in agents}
    indegree: dict[str, int] = {s: 0 for s in agents}
    for slug, agent in agents.items():
        mgr = agent.reports_to_slug
        if mgr in agents and mgr != slug:
            children[mgr].append(slug)
            indegree[slug] += 1

    def key(s: str) -> tuple[int, str]:
        return (0 if s == ceo_slug else 1, s)

    heap = [key(s) for s, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order: list[PlannedAgent] = []
    while heap:
        _, slug = heapq.heappop(heap)
        order.append(agents[slug])
        for child in sorted(children[slug]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, key(child))

    if len(order) < len(agents):
        placed = {a.slug for a in order}
        for slug in sorted(agents):
            if slug not in placed:
                order.append(agents[slug])
    return order


def _parse_routines(pkg_dir: Path, known_agents: set[str]) -> list[PlannedRoutine]:
    """Collect recurring routines from ``tasks/*/TASK.md`` and the
    ``.paperclip.yaml`` ``routines`` block, each keyed to a known agent slug via
    its ``assignee``. Routines with no resolvable in-package assignee are
    dropped."""
    out: list[PlannedRoutine] = []

    def _cron(d: dict):
        for k in ("cron", "schedule"):
            if d.get(k) is not None:
                return str(d[k])
        return None

    for task_md in sorted(pkg_dir.glob("tasks/*/TASK.md")):
        fm, body = _split_frontmatter(task_md.read_text(encoding="utf-8"))
        if not fm.get("recurring"):
            continue
        assignee = fm.get("assignee")
        slug = _agent_slug_from_ref(assignee) if isinstance(assignee, str) else ""
        if slug not in known_agents:
            continue
        out.append(
            PlannedRoutine(
                agent_slug=slug,
                title=str(fm.get("title") or task_md.parent.name),
                description=str(fm.get("description") or body.strip()),
                cron=_cron(fm),
            )
        )

    pc_path = pkg_dir / ".paperclip.yaml"
    if pc_path.is_file():
        data = yaml.safe_load(pc_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for routine in data.get("routines") or []:
                if not isinstance(routine, dict):
                    continue
                assignee = routine.get("assignee") or routine.get("agent")
                slug = _agent_slug_from_ref(assignee) if isinstance(assignee, str) else ""
                if slug not in known_agents:
                    continue
                out.append(
                    PlannedRoutine(
                        agent_slug=slug,
                        title=str(routine.get("title") or routine.get("name") or ""),
                        description=str(routine.get("description") or ""),
                        cron=_cron(routine),
                    )
                )
    return out


def parse_deploy_plan(pkg_dir: Path) -> DeployPlan:
    """Parse a company package into the deploy engine's execution view.

    Agents come back topologically ordered (a manager before its reports) so a
    consumer can create each agent and wire ``reports_to`` in a single forward
    pass. ``ceo_slug`` is the agent with ``role == "ceo"``, falling back to the
    sole agent with no reports-to.
    """
    pkg_dir = Path(pkg_dir)
    company_md = pkg_dir / "COMPANY.md"
    if not company_md.is_file():
        raise ValueError(f"not a company package (no COMPANY.md): {pkg_dir}")
    company_fm, _ = _split_frontmatter(company_md.read_text(encoding="utf-8"))
    slug = str(company_fm.get("slug") or pkg_dir.name)

    team_managers = _team_managers(pkg_dir)

    agents: dict[str, PlannedAgent] = {}
    raw_roles: dict[str, str | None] = {}
    for agents_md in sorted(pkg_dir.glob("agents/*/AGENTS.md")):
        agent_slug = agents_md.parent.name
        fm, body = _split_frontmatter(agents_md.read_text(encoding="utf-8"))

        reports_to = fm.get("reportsTo")
        if isinstance(reports_to, str) and reports_to.strip():
            reports_to_slug: str | None = _agent_slug_from_ref(reports_to)
        else:
            # null / missing reportsTo → inherit the owning team's manager.
            reports_to_slug = team_managers.get(agent_slug)

        raw_role = fm.get("role")
        raw_roles[agent_slug] = str(raw_role) if raw_role is not None else None
        agents[agent_slug] = PlannedAgent(
            slug=agent_slug,
            name=str(fm.get("name") or agent_slug),
            role="",  # finalized below, once the CEO is known
            title=str(fm["title"]) if fm.get("title") is not None else None,
            body_md=body,
            reports_to_slug=reports_to_slug,
            skills=[str(s) for s in (fm.get("skills") or []) if s is not None],
            capabilities=str(fm["capabilities"]) if fm.get("capabilities") is not None else None,
        )

    role_ceos = sorted(s for s, r in raw_roles.items() if (r or "").lower() == "ceo")
    if role_ceos:
        ceo_slug = role_ceos[0]
    else:
        no_manager = sorted(s for s, a in agents.items() if a.reports_to_slug is None)
        ceo_slug = no_manager[0] if no_manager else (sorted(agents)[0] if agents else "")

    for s, agent in agents.items():
        agent.role = _agent_role(raw_roles[s], is_ceo=s == ceo_slug)

    ordered = _topo_order(agents, ceo_slug)
    routines = _parse_routines(pkg_dir, set(agents))
    return DeployPlan(slug=slug, agents=ordered, routines=routines, ceo_slug=ceo_slug)
