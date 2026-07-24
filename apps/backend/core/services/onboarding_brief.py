"""First-run brief staging — the onboarding aha's OpenClaw-primitive layer.

Writes ``BRIEF.md`` into Friday's agent workspace on EFS BEFORE her
container boots (same pre-boot window friday_identity.py uses; OpenClaw's
writeFileIfMissing honors pre-written files): the user's funnel input,
verbatim + identity context. Written unconditionally. The one-shot marker
for the whole flow is the generation-fenced ``funnel_state`` on the
paperclip-companies owner row (see
``paperclip_repo.claim_funnel_submission``), not BRIEF.md's existence.

There used to also be a first-run ``BOOTSTRAP.md`` variant staged here,
telling Friday to read BRIEF.md and go to work. It was deleted (spec PR-6,
BOOTSTRAP) once verified inert: OpenClaw's boot-time reconcile
(``reconcileWorkspaceBootstrapCompletionState``) never surfaces a seeded
BOOTSTRAP.md into Friday's context once ``setupCompletedAt`` is pre-marked
(see ``core/services/friday_identity.py``, and research §5 of
``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``'s
companion) — the file was written but never read. The kickoff is now a
server-dispatched ``chat.send`` (``core/services/bootstrap_dispatcher.py``),
an observed operation instead of a file with no code path to Friday's
context.

The spec is docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md
(§4.B). BRIEF.md's content contract is unchanged by PR-6.
"""

from __future__ import annotations

import logging

from core.containers.workspace import Workspace

logger = logging.getLogger(__name__)

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


def _cap_title(title: str) -> str:
    """Bound a provisional title to _MAX_GOAL_TITLE.

    Applies on EVERY path, not just ``new``: the ``grow`` path derives its
    title from a free-text field (``_domain_of`` falls back to the raw input
    when it isn't a parseable URL, and the field allows up to 2000 chars), so
    a user pasting a paragraph into the website box would otherwise produce a
    ~2000-char title. If Paperclip's goal schema rejects that, the client
    retries the identical oversized title forever — same input, same reject.
    """
    if len(title) <= _MAX_GOAL_TITLE:
        return title
    return title[: _MAX_GOAL_TITLE - 1].rstrip() + "…"


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
        return _cap_title(f"Grow {_domain_of(user_input)}")
    return _cap_title(user_input.strip())


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
    a half-seeded company. That rollback delete is itself best-effort: if
    ``delete_goal`` also raises, we swallow it rather than let it propagate
    -- otherwise a cleanup failure would REPLACE the original
    ``create_project`` error (e.g. a retryable Paperclip 503) with an
    unrelated one, misclassifying what the caller sees (the router maps
    ``PaperclipApiError``/``httpx.HTTPError``/``RuntimeError`` to a
    retryable 409; a masked exception could fall outside that set and
    surface as a raw 500 instead). The orphaned goal this can leave behind
    is a lesser problem than losing the original error, and is bounded --
    it is never linked to a project or the onboarding sentinel.
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
        try:
            await admin.delete_goal(session_cookie=session_cookie, goal_id=goal_id)
        except Exception as cleanup_err:
            logger.error(
                "seed_goal_and_project: failed to roll back goal %s after project create failure: %s",
                goal_id,
                cleanup_err,
            )
        raise
    return goal_id, project["id"]


def stage_first_run_files(
    *,
    workspace: Workspace,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
) -> None:
    """Write BRIEF.md, unconditionally.

    Used to be write-if-missing because its existence WAS the one-shot
    marker for the flow. The generation-fenced ``funnel_state`` claim on the
    paperclip-companies owner row is that marker now, and winning it already
    proves this is the first run — so a BRIEF.md left behind by an attempt
    that failed partway is stale state to overwrite, not a reason to stop.
    Stopping there would strand the goal and project the caller seeded
    moments earlier.
    """
    base = f"workspace-{friday_agent_id}"
    workspace.write_file(owner_id, f"{base}/BRIEF.md", brief_md)
