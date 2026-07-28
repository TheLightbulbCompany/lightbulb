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


# ---------------------------------------------------------------------------
# First-run kickoff message — the user's OWN words as their visible first
# message in Friday's chat.
#
# Two functions, one source of truth for the wording:
#
#   * ``build_onboarding_brief_text`` — runs at ``/onboarding/brief`` submit
#     time (where the funnel ``path`` is known) and produces the natural-
#     language brief phrase persisted on the paperclip-companies owner row
#     (``onboarding_brief_text``, written in ``claim_funnel_submission``'s CAS).
#   * ``build_first_run_message`` — runs in ``bootstrap_dispatcher`` and wraps
#     that persisted phrase into the chat.send the server dispatches as
#     Friday's first-run kickoff. The dispatcher only sees the row (no funnel
#     ``path``), so the phrase must already read correctly after "I'm starting
#     " on BOTH funnel paths — which is why the phrase, not the wrapper, is
#     where the new-vs-grow verb lives.
#
# This replaces the old generic ``FIRST_RUN_PROMPT`` ("Friday — I just set up
# my company. Read BRIEF.md…"), which showed a canned instruction (or nothing)
# instead of the user's own ask. See
# ``docs/superpowers/specs/2026-07-25-onboarding-first-message-design.md``
# (Option A).
# ---------------------------------------------------------------------------

# No idea given ("I don't know yet — you pick"): there is no brief to lead
# with, so the visible first message still reads in the user's voice and still
# carries the same research → write-up → three-next-moves-on-the-board ask the
# with-brief message does. BRIEF.md (staged pre-boot) carries the email lead +
# "job zero" instructions Friday needs to actually choose one.
# NB: the kickoff must leave the three tasks in the BACKLOG and NOT start them.
# Starting a task wakes the assignee to work it, and that auto-work (on tasks
# the user never prioritized) is what burned the trial's daily cap and produced
# the round-1 retry storm (Isol8AI/isol8#1426). The user prioritizes; Friday
# only does the research/write-up the user is watching, then hands the board
# back. "in the backlog … don't start any yet … tell me which to tackle first"
# makes the (nondeterministic) good round-2 behavior deterministic.
FIRST_RUN_NO_BRIEF_MESSAGE = (
    "I'm not sure what to build yet — take a look and pick something promising for "
    "me, then dig into it, write up what you find, and put my three best next moves "
    "in the backlog. Don't start any of them yet — just tell me which to tackle "
    "first. I'm watching."
)


def build_onboarding_brief_text(*, path: str, user_input: str | None, detail: str | None) -> str | None:
    """The natural-language brief phrase persisted on the owner row and later
    woven into Friday's first-run kickoff message (``build_first_run_message``).

    Returns ``None`` on the "you pick" path (``user_input`` absent/blank) — the
    row simply carries no brief text and the dispatcher falls back to
    ``FIRST_RUN_NO_BRIEF_MESSAGE``.

    The phrase is written to slot after "I'm starting " and read correctly on
    both funnel paths, since the dispatcher has only the persisted text (not the
    ``path``) to work from:

      * ``new`` — the user's verbatim idea, with the optional "who's it for"
        audience (``detail``) woven in naturally when present. Their words are
        the seed, so they are kept verbatim.
      * ``grow`` — the funnel input is a website, not an idea, so "I'm starting
        {website}" would misread; phrase it as "work on {domain}" so the
        kickoff reads "I'm starting work on {domain}" for an existing business.
        (``detail`` is not collected on the grow path.)
    """
    idea = (user_input or "").strip()
    if not idea:
        return None
    if path == "grow":
        return f"work on {_domain_of(idea)}"
    audience = (detail or "").strip()
    if audience:
        return f"{idea} — it's for {audience}"
    return idea


# Appended to BOTH kickoff paths. This is what makes first contact happen at
# all: the command is handed over directly rather than left to be discovered.
#
# A live prod run (2026-07-26) showed why that matters. Asked to email its
# owner, the agent searched for gog, himalaya, and an OpenClaw "email channel",
# found none, and reported that email was unconfigured — it never opened the
# isol8 skill, because nothing in that skill's description mentioned email.
# The description is fixed separately; this removes the dependency on discovery
# entirely for the one send that must not be missed.
#
# The naming step comes FIRST and is not decoration: the address is minted from
# the company's name on the FIRST send, and `company_slug_repo` has no delete
# path, so whatever it is called at that moment is permanent. "If it is still a
# placeholder" is deliberate — by the time this runs the owner may already have
# named it (a real org's Clerk name carries through), and renaming a good name
# to a worse one is the failure mode on the other side.
_CLOSING_STEPS = (
    "\n\nTwo more things before you stop. First, check the company's name and, "
    "if it is still a placeholder rather than a real business name, give it one "
    "— that name becomes the subdomain of my email address and cannot be "
    "changed afterwards, so pick something you would put on a business card. "
    "Then email me a short note in your own voice about what you found:\n"
    "printf '%s' \"<your note>\" | /app/skills/isol8/scripts/isol8-owner-email.sh "
    '--subject "<subject>"\n'
    "That script is how you send email here — there is no Gmail, himalaya, or "
    "email channel to configure."
)


def build_first_run_message(brief_text: str | None) -> str:
    """Friday's first-run kickoff chat.send message — the VISIBLE first user
    message in her chat.

    ``brief_text`` is the persisted ``onboarding_brief_text``
    (``build_onboarding_brief_text``'s output). When present, the message leads
    with the user's own ask and carries the research → write-up → three-next-
    moves instruction in that same message (deterministic, not a hoped-for
    system-prompt behavior). When absent/blank (the "you pick" path, or a legacy
    row with no persisted brief), it falls back to ``FIRST_RUN_NO_BRIEF_MESSAGE``
    — so a missing field degrades to a sensible default, never an empty send.
    """
    brief = (brief_text or "").strip()
    if not brief:
        return FIRST_RUN_NO_BRIEF_MESSAGE + _CLOSING_STEPS
    return (
        f"I'm starting {brief}. Dig into the business and market, write it up, and put "
        "my three best next moves in the backlog — don't start any of them yet, just "
        "tell me which to tackle first. I'm watching."
    ) + _CLOSING_STEPS


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


def first_name_from_email(email: str | None) -> str:
    """ "priya.sharma@acme.co" -> "Priya Sharma". Names the seeded project."""
    local = (email or "").split("@")[0].strip()
    return local.replace(".", " ").title() or "there"


async def reconcile_pending_brief(owner_id: str) -> None:
    """Fulfil a submitted funnel answer, if there is one and the workspace can
    now receive it.

    This is the FULFILMENT half of ``POST /onboarding/brief``. The endpoint
    itself does intake only (record what the user said, return); everything
    that needs Paperclip up and Friday seeded happens here, on the same
    server-owned, idempotent, claim-fenced pattern as
    ``bootstrap_dispatcher.maybe_dispatch_bootstrap`` -- and off the same two
    call sites:

      - ``routers/onboarding.py:submit_brief`` -- immediately after intake, for
        the case where the workspace is ALREADY up (a re-signup, or a user who
        took their time on the funnel). Makes the split invisible.
      - ``routers/container.py:container_status`` -- every ~3s poll while
        provisioning. This is the one that matters: the funnel renders DURING
        provisioning, so the first-time path always drains here.

    Never raises -- both callers fire it as a background task, where an escaping
    exception is an unretrieved-task error rather than a retry. Anything that
    isn't ready yet (no owner row, still provisioning, Friday not seeded,
    Paperclip 5xx) simply returns and leaves the pending item in place for the
    next poll to retry. That retry loop replaces the one the BROWSER used to
    run, which lost the brief entirely if the tab closed or the user gave up.
    """
    try:
        await _fulfil_pending_brief(owner_id)
    except Exception:  # noqa: BLE001 — background task; the next poll retries
        # CancelledError is NOT an Exception, so a shutdown still propagates
        # (the rollback inside has already run -- see its handler).
        logger.exception("reconcile_brief: fulfilment failed for %s; will retry", owner_id)


async def _fulfil_pending_brief(owner_id: str) -> None:
    """The body of ``reconcile_pending_brief``; may raise, and its caller is
    the single place that guarantees it doesn't escape."""
    from core.containers import get_workspace
    from core.repositories.paperclip_repo import FunnelClaimResult, PaperclipRepo
    from core.services.clerk_admin import get_primary_email
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_user_session import get_user_session_cookie

    repo = PaperclipRepo(table_name="paperclip-companies")
    pending = await repo.get_pending_brief(owner_id)
    if pending is None:
        return

    # Strong read: we are about to conditionally write this same row.
    owner_row = await repo.resolve_owner_row(owner_id, consistent_read=True)
    if owner_row is None or not owner_row.onboarding_generation or owner_row.status != "active":
        # Not provisioned far enough to receive a brief. The pending item is
        # the whole point: nothing is lost by waiting for the next poll.
        return
    generation = owner_row.onboarding_generation

    submitted_by = pending.get("submitted_by") or owner_row.user_id
    path = pending.get("path") or "new"
    user_input = pending.get("input")
    detail = pending.get("detail")
    brief_text = pending.get("brief_text")

    # Everything below the claim has side effects; everything above it is a
    # read. So do ALL the reads first -- a Paperclip blip then costs nothing,
    # where the pre-split code claimed first and had to unwind the claim on
    # the way back out.
    try:
        email = await get_primary_email(submitted_by)
        # Minted for the OWNER row's user (the org creator / provisioner),
        # not the submitter: an org member who submitted the funnel may not
        # have a Paperclip row of their own yet, and the creator is
        # guaranteed to -- it is the row we just resolved. Both are members
        # of the same company, so the goal/project land identically.
        session_cookie = await get_user_session_cookie(
            user_id=owner_row.user_id,
            repo=repo,
            admin_client=shared_admin_client(),
            clerk_email_resolver=get_primary_email,
        )
        admin = shared_admin_client()
        agents = await admin.list_agents(session_cookie=session_cookie, company_id=owner_row.company_id)
    except Exception as e:  # noqa: BLE001 — every one of these is retryable
        logger.info("reconcile_brief: workspace not ready for %s (%s); retrying next poll", owner_id, e)
        return
    friday = next((a for a in agents if a.get("role") == "ceo"), None)
    if friday is None:
        # Paperclip seed hasn't landed Friday yet.
        return
    friday_id = friday["id"]

    claim_result = await repo.claim_funnel_submission(
        owner_row.user_id,
        onboarding_generation=generation,
        submitted_by=submitted_by,
        owner_id=owner_id,
        brief_text=brief_text,
    )
    if claim_result is FunnelClaimResult.STALE_GENERATION:
        # Reprovisioned underneath us. Leave the pending item: the next poll
        # re-reads the new generation and drains against that instead.
        return
    if claim_result is FunnelClaimResult.ALREADY_SUBMITTED:
        # Someone else won -- a peer drain on another backend instance, or an
        # earlier drain of ours that got as far as the claim. Retire the
        # pending item ONLY on evidence that fulfilment actually completed
        # (the row carries the seeded ids). A drain that is still in flight
        # can still fail and revert its claim, and deleting here would lose
        # the brief for good -- the one outcome this whole mechanism exists
        # to prevent. (For an org this re-read goes through the
        # eventually-consistent by-org-id GSI, so a just-written id may not
        # be visible yet; the cost of that is one more poll, in the safe
        # direction.)
        fulfilled = await repo.resolve_owner_row(owner_id, consistent_read=True)
        if fulfilled is not None and fulfilled.onboarding_goal_id:
            await repo.delete_pending_brief(owner_id)
        return

    goal_id: str | None = None
    project_id: str | None = None
    try:
        first_name = first_name_from_email(email)
        # Seeded BEFORE the kickoff dispatches, or Friday wakes into a company
        # with no project to work against.
        goal_id, project_id = await seed_goal_and_project(
            admin=admin,
            session_cookie=session_cookie,
            company_id=owner_row.company_id,
            friday_agent_id=friday_id,
            goal_title=derive_goal_title(path=path, user_input=user_input),
            project_name=project_name_for(first_name),
        )
        # Recorded BEFORE staging the workspace file, not after: this is the
        # generation-fenced write that decides who wins. Staging is a visible
        # side effect on Friday's NEXT boot -- if this write raced a
        # reprovision (generation moved on) and staging ran first anyway,
        # Friday could boot against dead goal/project ids.
        await repo.record_onboarding_ids(
            owner_row.user_id,
            onboarding_generation=generation,
            goal_id=goal_id,
            project_id=project_id,
            owner_id=owner_id,
        )
        stage_first_run_files(
            workspace=get_workspace(),
            owner_id=owner_id,
            friday_agent_id=friday_id,
            brief_md=render_brief_md(
                path=path,
                user_input=user_input,
                detail=detail,
                display_name=first_name,
                email=email or "",
            ),
        )
    except BaseException:
        # BaseException, not Exception: asyncio.CancelledError (backend
        # shutdown mid-drain) inherits from BaseException. Skipping the revert
        # would strand the claim in ``submitted`` forever -- the generation
        # fence has no stale-steal safety net, so a stuck claim permanently
        # blocks this generation from ever being re-submitted.
        #
        # Roll back whatever landed, then release the claim. Each step runs
        # independently of the others' success, and none may raise past this
        # block or a cleanup failure would replace the original error.
        #
        # The PENDING ITEM IS DELIBERATELY LEFT IN PLACE: it is what makes the
        # next poll retry the whole thing. It is only deleted on the success
        # path below.
        if goal_id is not None and project_id is not None:
            try:
                await admin.delete_project(session_cookie=session_cookie, project_id=project_id)
            except Exception as cleanup_err:
                logger.error(
                    "reconcile_brief: failed to roll back project %s for %s: %s", project_id, owner_id, cleanup_err
                )
            try:
                await admin.delete_goal(session_cookie=session_cookie, goal_id=goal_id)
            except Exception as cleanup_err:
                logger.error("reconcile_brief: failed to roll back goal %s for %s: %s", goal_id, owner_id, cleanup_err)
        try:
            await repo.revert_funnel_submission(owner_row.user_id, onboarding_generation=generation, owner_id=owner_id)
        except Exception as cleanup_err:
            logger.error("reconcile_brief: failed to revert funnel claim for %s: %s", owner_id, cleanup_err)
        raise

    await repo.delete_pending_brief(owner_id)

    # Same fire-and-forget hand-off the endpoint used to make: if the runtime
    # is already up, Friday's kickoff goes now; if it is still cold, the next
    # /container/status poll dispatches it (bootstrap_dispatcher's own guards
    # make a redundant call a cheap no-op).
    from core.services.bootstrap_dispatcher import maybe_dispatch_bootstrap

    await maybe_dispatch_bootstrap(owner_id)


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
