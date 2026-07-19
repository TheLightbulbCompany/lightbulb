"""First-run brief staging — the onboarding aha's OpenClaw-primitive layer.

Writes two files into Friday's agent workspace on EFS BEFORE her container
boots (same pre-boot window friday_identity.py uses; OpenClaw's
writeFileIfMissing honors pre-written files):

  - ``BRIEF.md``      — the user's funnel input, verbatim + identity context.
                        Written unconditionally. The one-shot marker for the
                        whole flow is the company-scoped onboarding claim in
                        paperclip_repo.py, not BRIEF.md's existence.
  - ``BOOTSTRAP.md``  — the first-run variant, unconditionally overwritten.
                        Ordering note: friday_identity seeds the standard
                        BOOTSTRAP with write_file_if_missing, so whichever
                        of (seed, brief-submit) runs first, the first-run
                        variant wins — no race.

The spec is docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md
(§4.B, §4.D). The visible auto-prompt lives in the frontend
(apps/frontend/src/lib/firstRun.ts) — keep the two in sync.
"""

from __future__ import annotations

from core.containers.workspace import Workspace

_BRIEF_TEMPLATE = """\
# BRIEF.md — Onboarding brief

The human just signed up and told us about the company they want run.

- **Path:** {path}  (new = starting something new, grow = existing business)
{idea_block}\
{detail_line}\
- **Name:** {display_name}
- **Email:** {email}

Treat their words as the seed, not the ceiling — on the `grow` path the
input is usually their website; fetch it first. Research the person as
well as the business (their public presence tells you who you work for).
"""

_IDEA_LINE = "- **Their words, verbatim:** {user_input}\n"

# No idea given: they clicked "I don't know yet — you pick". The absence is
# the instruction. Their email is the only lead we have, so say so plainly
# rather than leaving her to infer it.
_NO_IDEA_BLOCK = """\
- **Their words, verbatim:** *(nothing — they told us they don't have an
  idea yet and asked you to pick one for them.)*

**So choosing the business is job zero.** Work out who this person is before
you decide: their email address is below, and you have `web_search`. If the
domain is their own company's, that site tells you most of what you need. If
it's a personal address, search the address itself and their name for a
public footprint — what they've built, written, or worked on. Then commit to
ONE business you can argue for, given who they are, and tell them what you
chose and why. If the trail was cold, say so honestly and back your own
judgment anyway — do not stall waiting for them to answer.
"""

_FIRST_RUN_BOOTSTRAP_TEMPLATE = """\
# BOOTSTRAP.md — Your first session is a working session

{first_name} just set up their company and is WATCHING you work: your
chat stream is on the left of their screen, the task board on the right.
A kickoff message (sent on their behalf) opens this conversation. Unlike
a normal first chat, you SHOULD use tools this session — that is the
point. Narrate briefly as you go so they can follow along.

This session is about UNDERSTANDING their business and leaving them with a
clear set of next moves. You do the research and the docs live; you do NOT
execute the follow-up work here. Each next-move task you propose is run
later as its own separate session, when {first_name} chooses.

Load your Paperclip identity first (see AGENTS.md — the export block for
`paperclip-claimed-api-key.json`), then follow this order strictly:

1. Greet {first_name} by name in ONE line, then start working. Narrate
   briefly as you go — no walls of text between actions.
2. Read BRIEF.md (this workspace) to learn what business you're running. If
   it says they have no idea yet, picking one is your first job — look them
   up (their email is in the brief), commit to a single business you can
   argue for, and say what you chose and why. Then carry on below as though
   it had been their idea all along.
3. **Name the company.** Their company and project are both currently
   named after them as a placeholder — pick a real name for the business
   from what you just read, then set it in ONE go before anything else.
   This is the first thing {first_name} sees change, so do it now, not at
   the end.

   ```
   PATCH /api/companies/$PAPERCLIP_COMPANY_ID   {{"name": "<the name>"}}
   PATCH /api/projects/{project_id}             {{"name": "<the name>"}}
   ```

   If the company PATCH 403s, keep going — the project rename is the one
   that matters and the rest of the session is unaffected.
4. Understand the business: research it and its market with web_search —
   on the grow path, fetch their website first. This is the heart of the
   session; actually understand who they serve and what they're up against.
5. Write what you learned as durable docs — this is the visible output of
   the research: `research/market-research.md` (market, competitors,
   positioning) and `MISSION.md` (what this company is, who it serves,
   where it's headed). Fold the mission's essence into USER.md's Context
   section and append durable facts to MEMORY.md — your permanent context.
6. **Write the mission onto the goal.** The company goal currently holds
   their raw words. Replace it with the mission you just wrote:

   ```
   PATCH /api/goals/{goal_id}
   {{"title": "<one-line mission>", "description": "<2-3 sentences>"}}
   ```

   Do NOT change its status or delete it — it is the company's north star
   and everything else hangs off it.
7. ONLY NOW, as the session's closing OUTPUT, propose their three best next
   moves as tasks on the board. Create each with `"projectId": "{project_id}"`
   so it lands in their project and inherits the company goal. You do not
   need to set `goalId` — the board resolves it from the project. Create
   them; do NOT start them — each is a task {first_name} will run later, in
   its own session. Branch on the Path you read in BRIEF.md:
     - NEW business (starting from scratch): the FIRST task is ALWAYS a
       website — create a "Build [company] website" task for a landing page,
       noting that running it produces the site in this workspace and going
       live means connecting their own GitHub + Vercel. Choose the other two
       from launch moves your research points to — customer outreach, a first
       lead list, a content/social starter, or pricing.
     - GROW (they already have a business, usually a live site): do NOT
       propose building a website from scratch — they have one. Propose the
       three highest-leverage GROWTH moves your research surfaced — e.g.
       sharpening their existing site/conversion, customer outreach, a
       content engine, lead research, or a pricing review — whichever you
       found weakest or with the most upside for THIS business.
   Give each a short specific title and a one-line description.
8. Close in chat: recap what you learned about their business, name the
   three tasks you queued, and invite them to pick one and hit run —
   "I'll do it while you watch." If they want things running while they're
   away, that's a plan upgrade — say so plainly, don't sell.

Keep the whole session under ~10 minutes of work. If the browser tab
closed mid-run (no one is replying), finish the docs and the three
proposed tasks anyway — they are durable and {first_name} will see them
on return.

## After this session

This file becomes descriptive, not prescriptive. BRIEF.md stays as the
founding record. Your real working context is IDENTITY.md, SOUL.md,
USER.md, MISSION.md, and MEMORY.md.
"""


def render_brief_md(
    *,
    path: str,
    user_input: str | None,
    detail: str | None,
    display_name: str,
    email: str,
) -> str:
    """Render BRIEF.md. ``user_input`` is None on the "you pick" button."""
    idea_block = _IDEA_LINE.format(user_input=user_input) if user_input else _NO_IDEA_BLOCK
    detail_line = f"- **Extra detail:** {detail}\n" if detail else ""
    return _BRIEF_TEMPLATE.format(
        path=path,
        idea_block=idea_block,
        detail_line=detail_line,
        display_name=display_name,
        email=email,
    )


def render_first_run_bootstrap(*, first_name: str, goal_id: str, project_id: str) -> str:
    return _FIRST_RUN_BOOTSTRAP_TEMPLATE.format(
        first_name=first_name,
        goal_id=goal_id,
        project_id=project_id,
    )


_MAX_GOAL_TITLE = 120


def _domain_of(raw: str) -> str:
    """Best-effort registrable domain from whatever went in the URL field.

    The grow-path input is free text — users type ``ferrosocial.com``,
    ``https://ferrosocial.com/pricing``, or ``www.ferrosocial.com``. Falls
    back to the raw string rather than raising: this feeds a provisional
    title Friday overwrites within one turn, so a scruffy value is a
    cosmetic problem, not a failure.
    """
    value = raw.strip()
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].strip().lower()
    if value.startswith("www."):
        value = value[4:]
    return value or raw.strip()


def derive_goal_title(*, path: str, user_input: str | None) -> str:
    """Provisional goal title — a real mission statement on every path.

    Friday rewrites this after research. It must never read as filler in
    the meantime, because the user is watching the board while she works.
    """
    if not user_input:
        # "I don't know yet — you pick". This is an accurate description of
        # their situation and stops being true the moment Friday chooses.
        return "Find the right business to build"
    if path == "grow":
        return f"Grow {_domain_of(user_input)}"
    text = user_input.strip()
    if len(text) <= _MAX_GOAL_TITLE:
        return text
    return text[: _MAX_GOAL_TITLE - 1].rstrip() + "…"


def project_name_for(first_name: str) -> str:
    """The funnel never collects a company name, and the description makes a
    bad one ("A social media agency for small restaurants" is not a name).
    The owner's first name is always available and always reads as real.
    Friday renames it as her first visible action.
    """
    return f"{first_name}'s Company"


async def seed_goal_and_project(
    *,
    admin,
    session_cookie: str,
    company_id: str,
    friday_agent_id: str,
    goal_title: str,
    project_name: str,
) -> tuple[str, str]:
    """Create the company goal, then the project linked to it.

    Order is load-bearing. The project passes ``goalIds: [goal_id]``, and
    Paperclip mirrors ``goalIds[0]`` into the legacy ``projects.goalId``
    column that ``getProjectDefaultGoalId`` reads. A project with a null
    ``goalId`` is worse than no project at all: ``resolveIssueGoalId``
    returns ``projectGoalId ?? null`` for any issue that HAS a project and
    does not fall through to the company default, so every task filed into
    it would be silently unattributed.

    ``level="company"``, ``status="active"``, no ``parentId`` — anything
    else and ``getDefaultCompanyGoal`` never selects it.

    Rolls the goal back if the project create fails, so a retry never finds
    a half-seeded company.
    """
    goal = await admin.create_goal(
        session_cookie=session_cookie,
        company_id=company_id,
        body={
            "title": goal_title,
            "level": "company",
            "status": "active",
            "ownerAgentId": friday_agent_id,
        },
    )
    goal_id = goal["id"]
    try:
        project = await admin.create_project(
            session_cookie=session_cookie,
            company_id=company_id,
            body={
                "name": project_name,
                "status": "in_progress",
                "goalIds": [goal_id],
                "leadAgentId": friday_agent_id,
            },
        )
    except Exception:
        await admin.delete_goal(session_cookie=session_cookie, goal_id=goal_id)
        raise
    return goal_id, project["id"]


def stage_first_run_files(
    *,
    workspace: Workspace,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
    bootstrap_md: str,
) -> None:
    """Write BRIEF.md + the first-run BOOTSTRAP.md, both unconditionally.

    BRIEF.md used to be written write-if-missing because its existence WAS
    the one-shot marker for the flow. The company-scoped onboarding claim is
    that marker now, and holding it already proves this is the first run —
    so a BRIEF.md left behind by an attempt that failed partway is stale
    state to overwrite, not a reason to stop. Stopping there would strand the
    goal and project the caller seeded moments earlier.
    """
    base = f"workspaces/{friday_agent_id}"
    workspace.write_file(owner_id, f"{base}/BRIEF.md", brief_md)
    workspace.write_file(owner_id, f"{base}/BOOTSTRAP.md", bootstrap_md)
