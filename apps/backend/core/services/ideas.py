"""Ideas (spec 2026-10-14 §4.4 / §8): jobs a company usually pays an agency for, done by the team.

The catalog is one YAML file per idea under ``apps/backend/ideas/`` (YAML, not Markdown: the
image's .dockerignore drops ``*.md``). Each holds the card and the agent's guide. Start seeds a
goal + project in Work, writes the guide into the owner agent's profile, and wakes that agent
with one system line in the starter's DM; the agent then files its plan for approval
(skills-src/lightbulb/references/ideas.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

import httpx
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from core.auth import AuthContext, resolve_owner_id
from core.constants import SYSTEM_ACTOR_ID
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parents[2] / "ideas"
Role = Literal["ceo", "engineering", "finance", "growth", "analytics", "customer_operations"]
ROLES = get_args(Role)
#: A project in one of these is finished: its idea can be started again.
CLOSED_PROJECT_STATUSES = frozenset({"completed", "cancelled"})


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True)

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)
    owner_role: Role
    kind: Literal["agency", "everyday"]
    business_types: list[Literal["software", "store", "services", "any"]] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    why: str = Field(min_length=1, max_length=200)
    deliverables: list[str] = Field(min_length=3, max_length=3)
    agency_price_low: int | None = Field(default=None, gt=0)
    agency_price_high: int | None = Field(default=None, gt=0)
    price_unit: Literal["one_time", "month", "state"] | None = None  # state: per US state
    agency_turnaround: str | None = None
    our_turnaround: str = Field(min_length=1)
    your_part: str = Field(min_length=1)
    guide: str = Field(min_length=200)

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
        for heading in ("## Steps", "## Decisions", "## Done", "## Honesty"):
            if heading not in self.guide:
                raise ValueError(f"guide is missing {heading!r}")
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


def _idea(idea_id: str) -> Idea:
    idea = catalog().get(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


async def _workspace(owner_id: str) -> tuple[object | None, dict[str, dict]]:
    """The workspace row and role -> {id, name} for the starter roster's live agents only (a
    removed one has no roster row)."""
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import messaging

    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    names = {a["id"]: a["name"] for a in await messaging.load_agent_roster(owner_id)}
    starters = (row.starter_agent_ids if row else None) or {}
    return row, {role: {"id": aid, "name": names[aid]} for role, aid in starters.items() if aid in names}


def _card(idea: Idea, agent: dict) -> dict:
    return {**idea.model_dump(by_alias=True, exclude={"guide"}), "agentId": agent["id"], "agentName": agent["name"]}


async def list_ideas(auth: AuthContext) -> dict:
    """All ideas (no matching yet) whose role has an agent, minus dismissed ones, highest agency
    price first, unpriced last. ``totalLow`` sums the low prices of the one-off ones shown.
    ``running`` is the workspace's open starts, with their project's issue counts when Work
    answers (never failing the read when it does not)."""
    owner_id = resolve_owner_id(auth)
    _, agents = await _workspace(owner_id)
    dismissed = await messaging_repo.dismissed_ideas(owner_id)
    shown = [i for i in catalog().values() if i.id not in dismissed and i.owner_role in agents]
    shown.sort(key=lambda i: -(i.agency_price_low or 0))
    return {
        "ideas": [_card(i, agents[i.owner_role]) for i in shown],
        "totalLow": sum(i.agency_price_low for i in shown if i.price_unit == "one_time"),
        "running": await _running(auth, owner_id, agents),
    }


async def _running(auth: AuthContext, owner_id: str, agents: dict[str, dict]) -> list[dict]:
    from core.services import messaging

    names = {a["id"]: a["name"] for a in agents.values()}
    rows = [
        r for r in await messaging_repo.open_idea_starts(owner_id) if r["ideaId"] in catalog() and r.get("projectId")
    ]
    if not rows:
        return []
    statuses, progress = await _work_progress(auth, [r["projectId"] for r in rows])
    running = []
    for r in rows:
        if statuses.get(r["projectId"]) in CLOSED_PROJECT_STATUSES:
            continue
        item = {
            "ideaId": r["ideaId"],
            "title": catalog()[r["ideaId"]].title,
            "agentId": r.get("agentId"),
            "agentName": names.get(r.get("agentId")),
            "roomId": r.get("roomId"),
            "projectId": r["projectId"],
            "starterId": r["userId"],
            "starterName": await messaging.human_display_name(r["userId"]),
            "createdAt": r.get("createdAt"),
        }
        if r["projectId"] in progress:
            item["progress"] = progress[r["projectId"]]
        running.append(item)
    return running


async def _work_progress(auth: AuthContext, project_ids: list[str]) -> tuple[dict[str, str], dict[str, dict]]:
    """({project: status}, {project: {issue status: count}}) from Paperclip; empty on any failure,
    so the Ideas read never fails because Work is slow or the caller has not joined it yet."""
    from collections import Counter

    from core.services import task_board
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_agent_lifecycle import list_all_issues

    try:
        ctx = await task_board._context(auth)
        admin = shared_admin_client()
        projects = await admin.list_projects(session_cookie=ctx.session_cookie, company_id=ctx.company_id)
        rows = projects if isinstance(projects, list) else (projects or {}).get("projects") or []
        statuses = {p["id"]: p.get("status") for p in rows if isinstance(p, dict) and "id" in p}
        issues = await asyncio.gather(
            *(
                list_all_issues(admin, cookie=ctx.session_cookie, company_id=ctx.company_id, params={"projectId": pid})
                for pid in project_ids
            )
        )
    except Exception:  # noqa: BLE001 -- progress is optional on this read
        logger.info("ideas: Work progress unavailable", exc_info=True)
        return {}, {}
    return statuses, {pid: dict(Counter(i.get("status") for i in found)) for pid, found in zip(project_ids, issues)}


async def dismiss(auth: AuthContext, idea_id: str) -> None:
    _idea(idea_id)
    await messaging_repo.dismiss_idea(resolve_owner_id(auth), idea_id)


async def _project_closed(admin, session_cookie: str, project_id: str) -> bool:
    """True when the started project is finished or gone. A Paperclip error reads as "still
    open": the caller then answers with the stored ids instead of failing."""
    from core.services.paperclip_admin_client import PaperclipApiError

    try:
        project = await admin.get_project(session_cookie=session_cookie, project_id=project_id)
    except PaperclipApiError as exc:
        return exc.status_code == 404
    except httpx.HTTPError:
        return False
    return project.get("status") in CLOSED_PROJECT_STATUSES or bool(project.get("archivedAt"))


def start_line(who: str, idea: Idea, agent_name: str) -> str:
    return (
        f"{who} started **{idea.title}**. {agent_name}: read `$HERMES_HOME/ideas/{idea.id}.md` and "
        "follow the lightbulb skill's `references/ideas.md`: post the plan for approval here first."
    )


def project_idempotency_key(owner_id: str, idea_id: str, user_id: str, start_token: str) -> str:
    """Stable per claim: a retry after a crash replays the same Paperclip project (the fork's
    ``idempotencyKey`` on project create) instead of making a second one."""
    digest = hashlib.sha256(f"{owner_id}|{idea_id}|{user_id}|{start_token}".encode()).hexdigest()
    return f"lightbulb-idea-project:v1:{digest}"


async def start(auth: AuthContext, idea_id: str) -> dict:
    """Start an idea for the caller; idempotent per (workspace, idea, starter) while the started
    project is open. Returns ``{roomId, projectId}``. A Paperclip failure is a 502 to retry."""
    from core.services.paperclip_admin_client import PaperclipApiError

    try:
        return await _start(auth, idea_id)
    except (PaperclipApiError, httpx.HTTPError) as exc:
        logger.warning("ideas: start %s failed on Paperclip: %s", idea_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Work did not answer. Try Start again in a moment.") from exc


async def _start(auth: AuthContext, idea_id: str) -> dict:
    from core.services import hermes_profiles, messaging, task_board
    from core.services.paperclip_admin_client import shared_admin_client

    idea = _idea(idea_id)
    ctx = await task_board._context(auth)  # a joined member of this workspace, with a Paperclip session
    owner_id, user_id = ctx.owner_id, auth.user_id
    row, agents = await _workspace(owner_id)
    agent = agents.get(idea.owner_role)
    if agent is None:
        raise HTTPException(status_code=409, detail="No teammate owns this idea in this workspace.")
    admin = shared_admin_client()

    for _ in range(2):  # a second pass only after a finished start was closed
        won, claim = await messaging_repo.claim_idea_start(owner_id, idea_id, user_id)
        if won:
            break
        if claim.get("state") != "open":
            raise HTTPException(status_code=409, detail="This idea is already starting.")
        if not await _project_closed(admin, ctx.session_cookie, claim["projectId"]):
            return {"roomId": claim["roomId"], "projectId": claim["projectId"]}
        await messaging_repo.close_idea_start(owner_id, idea_id, user_id, claim["projectId"])
    else:
        raise HTTPException(status_code=409, detail="This idea is already starting.")

    try:
        project_id = claim.get("projectId")
        if not project_id:
            # Only a project: an idea never makes a company-level goal, so it can never become the
            # company's default goal. It joins the onboarding goal when the workspace has one.
            goal_id = getattr(row, "onboarding_goal_id", None)
            body = {
                "name": idea.title,
                "status": "in_progress",
                "leadAgentId": agent["id"],
                "idempotencyKey": project_idempotency_key(owner_id, idea_id, user_id, claim["startToken"]),
                **({"goalIds": [goal_id]} if goal_id else {}),
            }
            project = await admin.create_project(
                session_cookie=ctx.session_cookie, company_id=ctx.company_id, body=body
            )
            project_id = project["id"]
            await messaging_repo.update_idea_start(
                owner_id, idea_id, user_id, {"projectId": project_id, "agentId": agent["id"]}
            )
        await hermes_profiles.write_profile_file(owner_id, agent["id"], f"ideas/{idea_id}.md", idea.guide)
        room_id = await _usable_dm(owner_id, user_id, agent["id"])
        text = start_line(await messaging.human_display_name(user_id), idea, agent["name"])
        key = f"idea-start|{idea_id}|{user_id}|{project_id}"  # one line per started project
        try:
            await messaging_repo.create_message(
                room_id=room_id,
                owner_id=owner_id,
                idemp_pk=messaging_repo.room_pk(room_id),
                idemp_sk=messaging_repo.human_idemp_sk(SYSTEM_ACTOR_ID, key),
                req_hash=messaging_repo.request_hash(key),
                author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
                text=text,
                mentions=[],
                reply_to_message_id=None,
                thread_root_message_id=None,
                run_id=None,
                dispatch_targets=[{"agentId": agent["id"], "eventKind": "user_request"}],
                source_agent_id=None,
                client_message_id=None,
            )
        except messaging_repo.IdempotencyConflict:
            pass  # a retried attempt already posted it
        await messaging_repo.update_idea_start(
            owner_id, idea_id, user_id, {"state": "open", "roomId": room_id, "createdAt": utc_now_iso()}
        )
    except BaseException:
        try:  # free the claim at once; the next Start reuses whatever already landed
            await messaging_repo.update_idea_start(owner_id, idea_id, user_id, {"claimedAt": 0})
        except Exception:
            logger.warning("ideas: could not release the start claim %s/%s", owner_id, idea_id, exc_info=True)
        raise
    return {"roomId": room_id, "projectId": project_id}


async def _usable_dm(owner_id: str, user_id: str, agent_id: str) -> str:
    """The starter's DM with the agent, unarchived through the room PATCH path when needed, so
    the start line can land and the person can answer there."""
    from core.services import messaging

    room_id = await messaging._agent_dm_room_id(owner_id, user_id, agent_id)
    meta = await messaging_repo.get_room(room_id)
    if meta and meta.get("archivedAt"):
        await messaging.patch_room(
            AuthContext(user_id=user_id, org_id=owner_id), room_id, messaging.PatchRoomRequest(archived=False)
        )
    return room_id
