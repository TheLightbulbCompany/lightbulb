"""Ideas (spec 2026-10-14 §4.4): a pre-written prompt for a job the team can do.

Ideas come from the curated catalog (one YAML file per idea under ``apps/backend/ideas/``: YAML,
not Markdown, because the image's .dockerignore drops ``*.md``) and from ``suggest_idea``: an agent
adds one for this company when it notices a real reason during its work. Each has a size:
project (a Work project), task (one Work task assigned to the agent), quick (nothing in Work).
Let's do it records the start, makes the Work item, and posts the person's own message in their
DM with the agent: the idea quoted, "Let's do it!", and "Tracked in Work: [name](link)". Nothing
hidden. GET /ideas serves the cards (no prices); a started idea leaves the list for everyone while
its Work item is open (a quick one for a day).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

import httpx
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parents[2] / "ideas"
Role = Literal["ceo", "engineering", "finance", "growth", "analytics", "customer_operations"]
ROLES = get_args(Role)
Size = Literal["project", "task", "quick"]
AppKind = Literal[
    "landing_page", "content_site", "form", "internal_tool",
    "dashboard", "customer_portal", "product_with_payments", "automation",
]  # fmt: skip
#: A project in one of these is finished: its idea can be started again.
CLOSED_PROJECT_STATUSES = frozenset({"completed", "cancelled"})
#: A task in one of these is finished.
CLOSED_ISSUE_STATUSES = frozenset({"done", "cancelled"})
#: A quick idea (nothing in Work) is hidden this long after someone starts it.
QUICK_HIDDEN = timedelta(hours=24)
#: A start's project or task was deleted in Work: finished too.
GONE = "__gone__"
#: Open agent suggestions per workspace.
SUGGESTION_CAP = 10
SUGGESTION_PREFIX = "s-"
#: An agent's suggestion with no emoji of its own (and every one stored before emojis).
DEFAULT_EMOJI = "💡"
#: "Not now" is remembered: a dismissed suggestion's title is not suggested again for this long.
DISMISS_MEMORY = timedelta(days=14)
#: Ideas one request (a Worker run, e.g. the daily look around) may file.
RUN_IDEA_CAP = 3


#: Services an idea uses that are not connectors: the team mailbox, the shared browser, Meet.
PLATFORM_SERVICES = frozenset({"mailbox", "browser", "meet"})


class Need(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(pattern=r"^[a-z0-9_]+$", max_length=40)
    access: Literal["read", "write"] = "read"


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True)

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)
    owner_role: Role
    kind: Literal["agency", "everyday"]
    #: project: many steps over days (a Work project); task: one errand (one Work task); quick:
    #: answered in one go (nothing in Work). Spec 2026-10-14 §4.4.
    size: Literal["project", "task", "quick"]
    business_types: list[Literal["software", "store", "services", "any"]] = Field(min_length=1)
    #: The idea's picture in the list (owner, 2026-10-15: tell ideas apart at a glance).
    emoji: str = Field(min_length=1, max_length=8)
    title: str = Field(min_length=1, max_length=120)
    why: str = Field(min_length=1, max_length=200)
    deliverables: list[str] = Field(min_length=3, max_length=3)
    #: What the idea uses (spec 2026-10-15 §2.1): connector keys from providers.yaml or a
    #: platform service, each read or write; websites worked in the shared browser; steps only
    #: a person can do. Rendered into the Work item, never into the DM message.
    needs: list[Need] = Field(default_factory=list)
    browser: list[str] = Field(default_factory=list)
    person: list[str] = Field(default_factory=list)
    #: One line a check can read; the agent proves it before `done`.
    done_means: str = Field(min_length=1, max_length=200)
    agency_price_low: int | None = Field(default=None, gt=0)
    agency_price_high: int | None = Field(default=None, gt=0)
    price_unit: Literal["one_time", "month", "state"] | None = None  # state: per US state
    agency_turnaround: str | None = None
    our_turnaround: str = Field(min_length=1)
    your_part: str = Field(min_length=1)

    @field_validator("deliverables")
    @classmethod
    def _short_lines(cls, value: list[str]) -> list[str]:
        if any(not line.strip() or len(line) > 80 for line in value):
            raise ValueError("each deliverable is one short line")
        return value

    @model_validator(mode="after")
    def _price_is_whole(self) -> "Idea":
        priced = (self.agency_price_low, self.agency_price_high, self.price_unit, self.agency_turnaround)
        if any(v is None for v in priced) and any(v is not None for v in priced):
            raise ValueError("agency price low/high, unit and turnaround come together or not at all")
        if self.agency_price_low is not None and self.agency_price_low > self.agency_price_high:
            raise ValueError("agency_price_low is above agency_price_high")
        return self


@lru_cache(maxsize=1)
def catalog() -> dict[str, Idea]:
    """Every idea, by id. Raises on any invalid file: a broken catalog must fail loudly."""
    ideas: dict[str, Idea] = {}
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        idea = Idea.model_validate(yaml.safe_load(path.read_text()))
        if idea.id != path.stem:
            raise ValueError(f"{path.name}: id {idea.id!r} does not match the file name")
        ideas[idea.id] = idea
    return ideas


@dataclass(frozen=True)
class Job:
    """What Start needs, from a catalog idea or an agent's suggestion."""

    id: str
    title: str
    why: str
    deliverables: list[str]
    size: str
    agent: dict
    needs: list[Need] = field(default_factory=list)
    browser: list[str] = field(default_factory=list)
    person: list[str] = field(default_factory=list)
    done_means: str | None = None
    app_kind: str | None = None  # set for a suggested app: "Let's do it" tells the agent to build it


class Suggestion(BaseModel):
    """An idea an agent added for this company (``suggest_idea``): the same fields as a catalog
    idea, its size set by the agent, owned by the agent that suggested it."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    agent_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=3, max_length=120)
    why: str = Field(min_length=3, max_length=400)
    deliverables: list[str] = Field(min_length=1, max_length=3)
    size: Size
    emoji: str = Field(default=DEFAULT_EMOJI, min_length=1, max_length=8)
    kind: Literal["app"] | None = None  # an app idea; None is an ordinary idea
    app_kind: AppKind | None = None
    origin: str | None = Field(default=None, max_length=120)  # one line: what keeps coming back
    task_id: str | None = Field(default=None, max_length=64)  # the request it was filed from (a Worker run)

    @model_validator(mode="after")
    def _app_needs_kind(self):
        if (self.kind == "app") != (self.app_kind is not None):
            raise ValueError("an app idea needs app_kind, and app_kind needs kind='app'")
        return self

    @field_validator("title", "why", "origin")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        line = " ".join(value.split())
        if len(line) < 3:  # "   " passes min_length, then normalises to ""
            raise ValueError("needs at least three visible characters")
        return line

    @field_validator("deliverables")
    @classmethod
    def _short_lines(cls, value: list[str]) -> list[str]:
        lines = [" ".join(line.split()) for line in value]
        if any(not line or len(line) > 120 for line in lines):
            raise ValueError("each deliverable is one short line")
        return lines


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


async def _workspace(owner_id: str) -> tuple[object | None, dict[str, dict], dict[str, str]]:
    """The workspace row, role -> {id, name} for the starter roster's live agents only (a removed
    one has no roster row), and agent id -> name for the whole roster: one roster read."""
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    names = await _roster(owner_id)
    starters = (row.starter_agent_ids if row else None) or {}
    agents = {role: {"id": aid, "name": names[aid]} for role, aid in starters.items() if aid in names}
    # A workspace older than the starter roster (or one missing a starter) gives the idea to its
    # top agent, who decides whether a teammate should do the work (owner, 2026-10-15).
    top = getattr(names, "top", None)
    if top:
        for role in get_args(Role):
            agents.setdefault(role, {"id": top, "name": names[top]})
    return row, agents, names


#: Never served: no prices in the app (owner, 2026-10-15). They stay in the YAML for marketing.
_NOT_SERVED = {
    "agency_price_low",
    "agency_price_high",
    "price_unit",
    "agency_turnaround",
    "needs",
    "browser",
    "person",
    "done_means",
}


def _card(idea: Idea, agent: dict) -> dict:
    return {**idea.model_dump(by_alias=True, exclude=_NOT_SERVED), "agentId": agent["id"], "agentName": agent["name"]}


def _suggestion_card(row: dict, names: dict[str, str]) -> dict:
    app = {"appKind": row["appKind"], "origin": row.get("origin")} if row.get("appKind") else {}
    return {
        "id": row["ideaId"],
        "title": row["title"],
        "why": row["why"],
        "emoji": row.get("emoji") or DEFAULT_EMOJI,
        "deliverables": row["deliverables"],
        "size": row["size"],
        "kind": "app" if app else "everyday",
        **app,
        "businessTypes": ["any"],
        "suggested": True,  # "For you": this agent added it for this company
        "agentId": row["agentId"],
        "agentName": names[row["agentId"]],
    }


class _Roster(dict):
    """Agent id -> name, plus ``top``: the agent at the head of the org chart."""

    top: str | None = None


def _top_agent(rows: list[dict]) -> str | None:
    """The CEO if there is one, else an agent that reports to nobody, else the first agent."""
    for pick in (
        lambda a: a.get("role") == "ceo",
        lambda a: not a.get("reportsTo"),
        lambda a: True,
    ):
        found = [a["id"] for a in rows if pick(a)]
        if found:
            return found[0]
    return None


async def _roster(owner_id: str) -> dict[str, str]:
    from core.services import messaging

    rows = await messaging.load_agent_roster(owner_id)
    names = _Roster((a["id"], a["name"]) for a in rows)
    names.top = _top_agent(rows)
    return names


async def _suggestions(owner_id: str, names: dict[str, str]) -> list[dict]:
    """This workspace's suggestions from agents still on the roster, newest first."""
    rows = [r for r in await messaging_repo.suggested_ideas(owner_id) if r.get("agentId") in names]
    return sorted(rows, key=lambda r: r.get("createdAt") or "", reverse=True)


async def _job(owner_id: str, idea_id: str, agents: dict[str, dict], names: dict[str, str]) -> Job:
    """The catalog idea or this workspace's suggestion; 404 for anything else."""
    idea = catalog().get(idea_id)
    if idea is not None:
        agent = agents.get(idea.owner_role)
        if agent is None:
            raise HTTPException(status_code=409, detail="No teammate owns this idea in this workspace.")
        return Job(
            idea.id,
            idea.title,
            idea.why,
            list(idea.deliverables),
            idea.size,
            agent,
            list(idea.needs),
            list(idea.browser),
            list(idea.person),
            idea.done_means,
        )
    if idea_id.startswith(SUGGESTION_PREFIX):
        row = next((r for r in await _suggestions(owner_id, names) if r["ideaId"] == idea_id), None)
        if row is not None:
            agent = {"id": row["agentId"], "name": names[row["agentId"]]}
            return Job(
                row["ideaId"], row["title"], row["why"], list(row["deliverables"]), row["size"], agent,
                app_kind=row.get("appKind"),
            )  # fmt: skip
    raise HTTPException(status_code=404, detail="Idea not found")


async def list_ideas(auth: AuthContext) -> dict:
    """Agents' suggestions for this company (newest first), then the catalog ideas whose role has
    an agent (agency jobs first, then by title), minus dismissed ones and ideas someone in the
    workspace has started (owner, 2026-10-15: once you say Let's do it, the idea goes away; you are
    doing the work). No prices (owner, 2026-10-15)."""
    owner_id = resolve_owner_id(auth)
    _, agents, names = await _workspace(owner_id)
    dismissed = await messaging_repo.dismissed_ideas(owner_id)
    rows = await messaging_repo.open_idea_starts(owner_id)
    hidden = dismissed | await _started(auth, rows)
    shown = [i for i in catalog().values() if i.id not in hidden and i.owner_role in agents]
    shown.sort(key=lambda i: (i.kind != "agency", i.title))
    suggested = [r for r in await _suggestions(owner_id, names) if r["ideaId"] not in hidden]
    return {
        "ideas": [_suggestion_card(r, names) for r in suggested] + [_card(i, agents[i.owner_role]) for i in shown],
        # The CEO welcome's proposal shows only until the workspace has started (a start stays
        # recorded until it is started again) or hidden an idea.
        "anyStarted": bool(rows),
        "anyDismissed": bool(dismissed),
    }


async def _started(auth: AuthContext, rows: list[dict]) -> set[str]:
    """Ideas hidden because someone started them: while the start's project or task is open in
    Work (it comes back once that is finished or deleted), or for QUICK_HIDDEN after a quick one
    (nothing in Work: it was answered in one go). A Work item that cannot be read counts as open."""
    issues = await _issue_statuses(auth, [r["issueId"] for r in rows if r.get("issueId")])
    projects = await _project_statuses(auth, [r["projectId"] for r in rows if r.get("projectId")])
    now = datetime.now(timezone.utc)
    started = set()
    owner_id = resolve_owner_id(auth)
    for r in rows:
        if r.get("taskId"):  # a super-agent start: a Router request, open until it ends
            from core.services import proactive

            open_ = await proactive.request_open(owner_id, r["taskId"])
        elif r.get("projectId"):
            open_ = projects.get(r["projectId"]) not in CLOSED_PROJECT_STATUSES | {GONE}
        elif r.get("issueId"):
            open_ = issues.get(r["issueId"]) not in CLOSED_ISSUE_STATUSES | {GONE}
        else:
            at = _parse_time(r.get("createdAt"))
            open_ = at is not None and now - at < QUICK_HIDDEN
        if open_:
            started.add(r["ideaId"])
    return started


def _parse_time(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _issue_statuses(auth: AuthContext, issue_ids: list[str]) -> dict[str, str]:
    """{issue: status} from Paperclip; a deleted one (404) is GONE, one that could not be read is
    left out (never failing the read)."""
    if not issue_ids:
        return {}
    from core.services import task_board
    from core.services.paperclip_admin_client import shared_admin_client

    try:
        ctx = await task_board._context(auth)
        admin = shared_admin_client()
        found = await asyncio.gather(
            *(admin.get_issue(session_cookie=ctx.session_cookie, issue_id=i) for i in issue_ids), return_exceptions=True
        )
    except Exception:  # noqa: BLE001 -- progress is optional on this read
        logger.info("ideas: Work task status unavailable", exc_info=True)
        return {}
    from core.services.paperclip_admin_client import PaperclipApiError

    statuses = {}
    for issue_id, found_issue in zip(issue_ids, found):
        if isinstance(found_issue, PaperclipApiError) and found_issue.status_code == 404:
            statuses[issue_id] = GONE
        elif isinstance(found_issue, dict) and found_issue.get("status"):
            statuses[issue_id] = found_issue["status"]
    return statuses


async def _project_statuses(auth: AuthContext, project_ids: list[str]) -> dict[str, str]:
    """{project: status} from Paperclip; {} when Work does not answer (never failing the read).
    A project the list leaves out (deleted, or one this caller cannot list) is read on its own: a
    404 is GONE; anything else leaves it unknown, which counts as open."""
    if not project_ids:
        return {}
    from core.services import task_board
    from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client

    try:
        ctx = await task_board._context(auth)
        admin = shared_admin_client()
        projects = await admin.list_projects(session_cookie=ctx.session_cookie, company_id=ctx.company_id)
        rows = projects if isinstance(projects, list) else (projects or {}).get("projects") or []
        statuses = {p["id"]: p.get("status") for p in rows if isinstance(p, dict) and "id" in p}
        unlisted = [pid for pid in project_ids if pid not in statuses]
        read = await asyncio.gather(
            *(admin.get_project(session_cookie=ctx.session_cookie, project_id=pid) for pid in unlisted),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001 -- an unknown project counts as open
        logger.info("ideas: Work projects unavailable", exc_info=True)
        return {}
    for pid, found_project in zip(unlisted, read):
        if isinstance(found_project, PaperclipApiError) and found_project.status_code == 404:
            statuses[pid] = GONE
        elif isinstance(found_project, dict):
            archived = bool(found_project.get("archivedAt"))
            statuses[pid] = "completed" if archived else found_project.get("status")
    return statuses


async def dismiss(auth: AuthContext, idea_id: str) -> None:
    owner_id = resolve_owner_id(auth)
    if idea_id not in catalog():
        names = await _roster(owner_id)
        if not any(r["ideaId"] == idea_id for r in await _suggestions(owner_id, names)):
            raise HTTPException(status_code=404, detail="Idea not found")
    await messaging_repo.dismiss_idea(owner_id, idea_id)


async def suggest(owner_id: str, suggestion: Suggestion) -> dict:
    """An agent's ``suggest_idea``: stored in this workspace's list only (never a chat card).
    ``{"status": "added", "id"}``; ``duplicate`` when an idea with the same title is already there
    (a catalog one, or any suggestion); ``cap`` at SUGGESTION_CAP open suggestions."""
    names = await _roster(owner_id)
    if suggestion.agent_id not in names:
        raise HTTPException(status_code=403, detail="Not an agent on this workspace's team.")
    title = _norm(suggestion.title)
    # ponytail: dedupe and the cap are read-then-write, so two suggest_idea calls at the same
    # moment can store a duplicate title or an 11th suggestion; bounded by the agents' turns
    # (a handful per day). A per-title conditional row would close it if it ever matters.
    stored = await _suggestions(owner_id, names)
    dismissed = await messaging_repo.dismissed_idea_times(owner_id)
    remembered = (datetime.now(timezone.utc) - DISMISS_MEMORY).isoformat()
    # A stored one blocks its title while it is listed (or started), and for DISMISS_MEMORY after "Not now".
    blocking = [r for r in stored if r["ideaId"] not in dismissed or dismissed[r["ideaId"]] >= remembered]
    if any(_norm(i.title) == title for i in catalog().values()) or any(_norm(r["title"]) == title for r in blocking):
        return {"status": "duplicate"}
    if await _super_agent(owner_id):
        from core.services import proactive

        if any(_norm(t.get("title") or "") == title for t in await proactive.open_requests(owner_id)):
            return {"status": "duplicate"}
    if suggestion.task_id and sum(r.get("taskId") == suggestion.task_id for r in stored) >= RUN_IDEA_CAP:
        return {"status": "run_cap"}
    if sum(r["ideaId"] not in dismissed for r in stored) >= SUGGESTION_CAP:
        return {"status": "cap"}
    idea_id = f"{SUGGESTION_PREFIX}{uuid.uuid4().hex[:12]}"
    await messaging_repo.put_suggested_idea(
        owner_id,
        {
            "ideaId": idea_id,
            "agentId": suggestion.agent_id,
            "title": suggestion.title,
            "why": suggestion.why,
            "deliverables": suggestion.deliverables,
            "size": suggestion.size,
            "emoji": suggestion.emoji,
            "createdAt": utc_now_iso(),
            **({"appKind": suggestion.app_kind, "origin": suggestion.origin} if suggestion.app_kind else {}),
            **({"taskId": suggestion.task_id} if suggestion.task_id else {}),
        },
    )
    return {"status": "added", "id": idea_id}


async def _closed(admin, session_cookie: str, start: dict) -> bool:
    """True when the start's Work item is finished or gone, or it had none (quick: done in one
    go). A Paperclip error reads as "still open": the caller answers with the stored ids."""
    from core.services.paperclip_admin_client import PaperclipApiError

    try:
        if start.get("projectId"):
            project = await admin.get_project(session_cookie=session_cookie, project_id=start["projectId"])
            return project.get("status") in CLOSED_PROJECT_STATUSES or bool(project.get("archivedAt"))
        if start.get("issueId"):
            issue = await admin.get_issue(session_cookie=session_cookie, issue_id=start["issueId"])
            return issue.get("status") in CLOSED_ISSUE_STATUSES
    except PaperclipApiError as exc:
        return exc.status_code == 404
    except httpx.HTTPError:
        return False
    return True


def work_link(kind: str, work_id: str) -> str:
    """The Work item's page, in the app."""
    panel = "teams-project-detail" if kind == "project" else "teams-issue-detail"
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel={panel}&detailId={work_id}"


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]()<>#!|~])")


def _md_text(value: str) -> str:
    """Plain text inside markdown: an agent-written title or description cannot add a link,
    emphasis or anything else to the person's own message, or break the Work link."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(value.split()))


def work_description(job: Job, *, origin: dict | None = None) -> str:
    """The Work item's text: the why, what's included, and (catalog ideas) what it needs and what
    done means. The agent reads these here; the DM message stays the plain prompt. ``origin``
    (``name``, ``roomId``, ``paperclipUserId``) adds a **From** section: the idea's run is a Work
    run, not bound to the DM, so this is how it reaches the person (``lightbulb-post.sh --room``)
    and assigns them a step."""
    text = job.why + "\n\nWhat's included:\n" + "\n".join(f"- {d}" for d in job.deliverables)
    # Connector keys can be linked; built-in services (mailbox, browser, meet) never are.
    needs = [f"- {n.service} ({n.access})" for n in job.needs if n.service not in PLATFORM_SERVICES]
    needs += [f"- {n.service} (built in)" for n in job.needs if n.service in PLATFORM_SERVICES]
    needs += [f"- browser: {site}" for site in job.browser]
    needs += [f"- you: {step}" for step in job.person]
    if needs:
        text += "\n\n## Needs\n" + "\n".join(needs)
    if job.done_means:
        text += "\n\n## Done means\n" + job.done_means
    if origin:
        text += (
            f"\n\n## From\nStarted from Ideas by {' '.join(origin['name'].split())}. "
            f"Their DM with you: room `{origin['roomId']}`. "
            f"Their Paperclip user id: `{origin['paperclipUserId']}`."
        )
    return text


def start_text(job: Job, tracked: tuple[str, str] | None) -> str:
    """The person's own message when they press Let's do it: the idea quoted, the ask, and, for a
    project or a task, the Work item tracking it. The link is how the agent learns where this
    start's work goes. Nothing hidden."""
    title = _md_text(job.title)
    text = f"> **{title}**\n> {_md_text(job.why)}\n\nLet's do it!"
    if tracked:
        text += f"\nTracked in Work: [{title}]({work_link(*tracked)})"
    if job.app_kind:  # no agent-only channel on this path: one plain sentence; the skill has the commands
        text += f"\nBuild this as an app ({job.app_kind.replace('_', ' ')}), origin suggested:{job.id}"
        text += f", kind {job.app_kind}."
    return text


def work_idempotency_key(owner_id: str, idea_id: str, user_id: str, start_token: str) -> str:
    """Stable per claim: a retry after a crash replays the same Paperclip project or task (the
    fork's ``idempotencyKey`` on create) instead of making a second one."""
    digest = hashlib.sha256(f"{owner_id}|{idea_id}|{user_id}|{start_token}".encode()).hexdigest()
    return f"lightbulb-idea-project:v1:{digest}"


async def start(auth: AuthContext, idea_id: str) -> dict:
    """Start an idea for the caller; idempotent per (workspace, idea, starter) while its Work item
    is open. Returns ``{roomId, projectId?, issueId?}``. A Paperclip failure is a 502 to retry."""
    from core.services.paperclip_admin_client import PaperclipApiError

    try:
        return await _start(auth, idea_id)
    except (PaperclipApiError, httpx.HTTPError) as exc:
        logger.warning("ideas: start %s failed on Paperclip: %s", idea_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Work did not answer. Try again in a moment.") from exc


def _ids(start: dict) -> dict:
    return {"roomId": start["roomId"], **{k: start[k] for k in ("projectId", "issueId", "taskId") if start.get(k)}}


async def _super_agent(owner_id: str) -> bool:
    from core.services import super_agent_flags

    return await super_agent_flags.flags_for(owner_id)


async def _start_request(auth: AuthContext, owner_id: str, idea_id: str, job: Job) -> dict:
    """Super-agent Let's do it: a Router request in the person's DM with Lightbulb, done by the idea's
    specialist (Lightbulb itself for the top agent's ideas). No Paperclip. Idempotent per (idea, person) while
    the request is open; once it ends the idea can be started again."""
    from core.services import proactive, task_conversation

    user_id = auth.user_id
    for _ in range(2):
        won, claim = await messaging_repo.claim_idea_start(owner_id, idea_id, user_id)
        if won:
            break
        if claim.get("state") != "open":
            raise HTTPException(status_code=409, detail="This idea is already starting.")
        if claim.get("taskId") and await proactive.request_open(owner_id, claim["taskId"]):
            return _ids(claim)
        await messaging_repo.close_idea_start(owner_id, idea_id, user_id, claim["startToken"])
    else:
        raise HTTPException(status_code=409, detail="This idea is already starting.")
    try:
        front = await task_conversation.front_agent_id(owner_id)
        room_id = await _usable_dm(owner_id, user_id, front)
        brief = work_description(job)
        if job.app_kind:
            brief += f"\n\nBuild this as an app ({job.app_kind.replace('_', ' ')}), origin suggested:{job.id}."
        key = hashlib.sha256(f"{idea_id}|{user_id}|{claim['startToken']}".encode()).hexdigest()[:32]
        task_id = await proactive.start_request(
            owner_id,
            key=key,
            title=job.title,
            brief=brief,
            actor_user_id=user_id,
            room_id=room_id,
            specialist=None if job.agent["id"] == front else job.agent["id"],
        )
        fields = {"state": "open", "roomId": room_id, "taskId": task_id, "createdAt": utc_now_iso(),
                  "agentId": job.agent["id"], "title": job.title, "size": job.size}  # fmt: skip
        await messaging_repo.update_idea_start(owner_id, idea_id, user_id, fields)
    except BaseException:
        try:
            await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"claimedAt": 0})
        except Exception:
            logger.warning("ideas: could not release the start claim %s/%s", owner_id, idea_id, exc_info=True)
        raise
    return {"roomId": room_id, "taskId": task_id}


async def _start(auth: AuthContext, idea_id: str) -> dict:
    from core.services import messaging, task_board
    from core.services.paperclip_admin_client import shared_admin_client

    row, agents, names = await _workspace(resolve_owner_id(auth))
    job = await _job(resolve_owner_id(auth), idea_id, agents, names)  # an unknown idea is a 404 first
    if await _super_agent(resolve_owner_id(auth)):
        return await _start_request(auth, resolve_owner_id(auth), idea_id, job)
    ctx = await task_board._context(auth)  # a joined member of this workspace, with a Paperclip session
    owner_id, user_id = ctx.owner_id, auth.user_id
    agent = job.agent
    admin = shared_admin_client()

    for _ in range(2):  # a second pass only after a finished start was closed
        won, claim = await messaging_repo.claim_idea_start(owner_id, idea_id, user_id)
        if won:
            break
        if claim.get("state") != "open":
            raise HTTPException(status_code=409, detail="This idea is already starting.")
        if not await _closed(admin, ctx.session_cookie, claim):
            return _ids(claim)
        await messaging_repo.close_idea_start(owner_id, idea_id, user_id, claim["startToken"])
    else:
        raise HTTPException(status_code=409, detail="This idea is already starting.")

    try:
        room_id = await _usable_dm(owner_id, user_id, agent["id"])
        key = work_idempotency_key(owner_id, idea_id, user_id, claim["startToken"])
        tracked: tuple[str, str] | None = None
        try:
            name = await messaging.human_display_name(user_id)
        except Exception:  # noqa: BLE001 - a missing name never blocks the start
            name = ""
        origin = {
            "name": name if name and name != user_id else "the person",
            "roomId": room_id,
            "paperclipUserId": ctx.paperclip_user_id,
        }
        if job.size == "project":
            project_id = claim.get("projectId")
            if not project_id:
                # Only a project: an idea never makes a company-level goal, so it can never become
                # the company's default goal. It joins the onboarding goal when the workspace has one.
                goal_id = getattr(row, "onboarding_goal_id", None)
                body = {
                    "name": job.title,
                    "description": work_description(job, origin=origin),
                    "status": "in_progress",
                    "leadAgentId": agent["id"],
                    "idempotencyKey": key,
                    **({"goalIds": [goal_id]} if goal_id else {}),
                }
                project = await admin.create_project(
                    session_cookie=ctx.session_cookie, company_id=ctx.company_id, body=body
                )
                project_id = project["id"]
                await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"projectId": project_id})
            if not claim.get("firstIssueId"):  # the project's one task, carrying the idea
                first = await admin.create_issue(
                    session_cookie=ctx.session_cookie,
                    company_id=ctx.company_id,
                    body={
                        "title": job.title,
                        "description": work_description(job, origin=origin),
                        "status": "todo",
                        "assigneeAgentId": agent["id"],
                        "projectId": project_id,
                        "idempotencyKey": f"{key}:task",
                        "allowDuplicate": True,
                    },
                )
                await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"firstIssueId": first["id"]})
            tracked = ("project", project_id)
        elif job.size == "task":
            issue_id = claim.get("issueId")
            if not issue_id:
                body = {
                    "title": job.title,
                    "description": work_description(job, origin=origin),
                    # todo: Paperclip starts the run at once, like any assigned task (owner,
                    # 2026-10-16). The DM message below is the person's record and does not wake.
                    "status": "todo",
                    "assigneeAgentId": agent["id"],
                    "idempotencyKey": key,
                    # The same title may be started again after a finished task; the key still
                    # replays a retry's issue instead of making a second one.
                    "allowDuplicate": True,
                }
                issue = await admin.create_issue(
                    session_cookie=ctx.session_cookie, company_id=ctx.company_id, body=body
                )
                issue_id = issue["id"]
                await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"issueId": issue_id})
            tracked = ("task", issue_id)
        # The person's own message (via Ideas), shown live. Work tracked in Paperclip already woke
        # the agent, so it is a record; a quick idea has no task, so this message is the wake.
        # One per start: a retry replays it.
        client_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"lightbulb-idea-start|{owner_id}|{idea_id}|{user_id}|{claim['startToken']}"
        )
        await messaging.create_human_message(
            AuthContext(user_id=user_id, org_id=owner_id),
            room_id,
            messaging.CreateMessageRequest(
                client_message_id=client_id,
                text=start_text(job, tracked),
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="ideas",
            wake=tracked is None,
        )
        fields = {"state": "open", "roomId": room_id, "createdAt": utc_now_iso(), "agentId": agent["id"],
                  "title": job.title, "size": job.size}  # fmt: skip
        await messaging_repo.update_idea_start(owner_id, idea_id, user_id, fields)
    except BaseException:
        try:  # free the claim at once; the next Start reuses whatever already landed
            await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"claimedAt": 0})
        except Exception:
            logger.warning("ideas: could not release the start claim %s/%s", owner_id, idea_id, exc_info=True)
        raise
    return _ids({"roomId": room_id, "projectId": tracked[1] if tracked and tracked[0] == "project" else None,
                 "issueId": tracked[1] if tracked and tracked[0] == "task" else None})  # fmt: skip


async def _usable_dm(owner_id: str, user_id: str, agent_id: str) -> str:
    """The starter's DM with the agent, unarchived through the room PATCH path when needed, so
    the person's Let's do it message can land and they can answer there."""
    from core.services import messaging

    room_id = await messaging._agent_dm_room_id(owner_id, user_id, agent_id)
    meta = await messaging_repo.get_room(room_id)
    if meta and meta.get("archivedAt"):
        await messaging.patch_room(
            AuthContext(user_id=user_id, org_id=owner_id), room_id, messaging.PatchRoomRequest(archived=False)
        )
    return room_id


async def resume_after_connect(owner_id: str, provider_key: str) -> int:
    """A service was connected: one comment on each open idea start that needs it, which wakes
    the assignee through Paperclip's own comment wake (spec 2026-10-15 §2.3). No approval card,
    no extra step. Idempotent per (start, provider). Returns the number of comments posted."""
    from core.services.credentials import registry
    from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client

    waiting = []
    for row in await messaging_repo.open_idea_starts(owner_id):
        idea = catalog().get(row.get("ideaId", ""))
        if idea is None or provider_key in (row.get("resumed") or []):
            continue
        if any(n.service == provider_key for n in idea.needs):
            waiting.append(row)
    if not waiting:
        return 0
    try:
        label = registry.get_provider(provider_key).display_name
    except Exception:  # noqa: BLE001
        label = provider_key.replace("_", " ").title()
    text = f"{label} is connected. Carry on with this; ask again only if something else is missing."
    admin = shared_admin_client()
    posted = 0
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        cookie = await get_admin_session_cookie(http)
        ws = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
        for row in waiting:
            try:
                # A project's own task when we stored it and it is still open; else its first open
                # task with an assignee.
                target = row.get("firstIssueId") or row.get("issueId")
                if target:  # firstIssueId or the task start's issueId: skip it when already closed
                    try:
                        first = await admin.get_issue(session_cookie=cookie, issue_id=target)
                        if (first or {}).get("status") in CLOSED_ISSUE_STATUSES:
                            target = None
                    except PaperclipApiError as exc:
                        if exc.status_code != 404:
                            raise
                        target = None
                if not target and row.get("projectId") and ws is not None:
                    listed = await admin.list_issues(
                        session_cookie=cookie,
                        company_id=ws.company_id,
                        params={"projectId": row["projectId"], "status": "todo,in_progress,in_review,blocked"},
                    )
                    rows = listed if isinstance(listed, list) else (listed.get("issues") or listed.get("items") or [])
                    target = next(
                        (
                            i["id"]
                            for i in rows
                            if i.get("assigneeAgentId") and i.get("status") not in CLOSED_ISSUE_STATUSES
                        ),
                        None,
                    )
                if not target:
                    continue  # nothing open to wake: leave it unmarked so a later connect can nudge
                # Reserve before posting: a racing connect finds it taken and posts nothing.
                if not await messaging_repo.claim_idea_resume(owner_id, row["ideaId"], row["userId"], provider_key):
                    continue
                try:
                    await admin.add_issue_comment(session_cookie=cookie, issue_id=target, body={"body": text})
                except Exception:
                    await messaging_repo.release_idea_resume(owner_id, row["ideaId"], row["userId"], provider_key)
                    raise
                posted += 1
            except Exception:  # noqa: BLE001 - one bad start never blocks the rest
                logger.warning("ideas: resume failed owner=%s idea=%s", owner_id, row.get("ideaId"), exc_info=True)
    return posted
