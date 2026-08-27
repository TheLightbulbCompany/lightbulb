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

import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum

from core.containers.workspace import Workspace
from core.observability.product_analytics import capture_product_event

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
# instead of the user's own ask.
# ---------------------------------------------------------------------------

# The naming ask sits IN the kickoff body, BEFORE the board ask — not in the
# closing steps where it used to live. Two things freeze from the company's
# name: the board's issue prefix (Paperclip re-derives it on rename only while
# the board has minted nothing — fork v2026.8-isol8.4) and the email subdomain
# (minted on the first send, no delete path). Naming after the tasks landed is
# what left boards permanently keyed off the signup placeholder — three
# letters of the owner's email local part.
#
# The wording deliberately promises only "minted early and permanent", not the
# re-derivation mechanics: this text deploys with the backend, ahead of the
# Paperclip image pin that adds rename re-derivation, and must stay true on
# both sides of that flip.
_NAMING_STEP_SENTENCE = (
    "Before you put anything on the board, check the company's name — if it is "
    "still a placeholder rather than a real business name, rename it now: the "
    "name feeds identifiers that are minted early and can never be changed — "
    "the board's task-key prefix and the subdomain of my email address — so "
    "settle the name first and pick something you would put on a business card."
)

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
    "me, then dig into it and write up what you find. "
    f"{_NAMING_STEP_SENTENCE} "
    "Then put my three best next moves in the backlog. Don't start any of them yet "
    "— just tell me which to tackle first. I'm watching."
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


# Appended to BOTH kickoff paths, ahead of _CLOSING_STEPS. Research alone
# names the right next moves and then recommends none of the connections that
# would let the agent DO them: the first prod run on this kickoff (2026-08-08)
# produced three backlog items that each mapped onto an integration already in
# credentials/providers.yaml — waitlist email, social posting, payments — and
# offered zero of them.
#
# The lightbulb skill has a first-run rule for exactly this, but it covers only
# `requestable` (not-yet-available) integrations, and the agent has to open the
# skill to see it — which the 2026-07-26 run below proved she may not do. So the
# endpoint is handed over directly, same as the email script: it is public and
# unauthenticated, so it answers before anything is connected. The one-click
# connections she can now START herself (routers/connections.py
# authorize_from_container — hook-token sibling of the tab's Connect); that
# curl rides along for the same reason the catalog one does.
_INTEGRATIONS_STEP = (
    "\n\nAlong with those moves, tell me which integrations to connect. Read the "
    "live catalog first — `curl -s $LIGHTBULB_API_URL/api/v1/platform-info` — then "
    "name only the ones that serve the moves you picked, say what each one "
    "unlocks, and say what it costs me to set up (each entry's `requestable` and "
    "`byo_required` flags tell you that — don't promise me one click without "
    "checking them). For the one-click ones (both flags false) don't wait on me: "
    'mint the sign-in link yourself — `curl -s -H "Authorization: Bearer '
    '$OPENCLAW_HOOK_TOKEN" "$LIGHTBULB_API_URL/api/v1/connections/u/$LIGHTBULB_OWNER_ID/'
    '<provider>/authorize"` returns `authorize_url` — open it in your browser, and '
    "tell me to sign in in the Browser panel. Anything else, just tell me which "
    "are worth my time."
)


# Appended to BOTH kickoff paths. This is what makes first contact happen at
# all: the command is handed over directly rather than left to be discovered.
#
# A live prod run (2026-07-26) showed why that matters. Asked to email its
# owner, the agent searched for gog, himalaya, and an OpenClaw "email channel",
# found none, and reported that email was unconfigured — it never opened the
# lightbulb skill, because nothing in that skill's description mentioned email.
# The description is fixed separately; this removes the dependency on discovery
# entirely for the one send that must not be missed.
#
# The naming ask used to lead these closing steps; it moved up into the
# kickoff body (_NAMING_STEP_SENTENCE) so the rename lands BEFORE the board's
# first task mints the issue prefix — the email subdomain was never the only
# thing frozen by the name. By the time this send runs, the company is named.
_CLOSING_STEPS = (
    "\n\nOne more thing before you stop: email me a short note in your own "
    "voice about what you found:\n"
    "printf '%s' \"<your note>\" | /app/skills/lightbulb/scripts/lightbulb-mail.sh send --owner "
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
        return FIRST_RUN_NO_BRIEF_MESSAGE + _INTEGRATIONS_STEP + _CLOSING_STEPS
    return (
        (
            f"I'm starting {brief}. Dig into the business and market and write it up. "
            f"{_NAMING_STEP_SENTENCE} "
            "Then put my three best next moves in the backlog — don't start any of them "
            "yet, just tell me which to tackle first. I'm watching."
        )
        + _INTEGRATIONS_STEP
        + _CLOSING_STEPS
    )


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


class DrainOutcome(str, Enum):
    """What one pass of ``_fulfil_pending_brief`` achieved."""

    DONE = "done"
    """Fulfilled, already fulfilled by a peer, or nothing pending. Stop."""

    NOT_READY = "not_ready"
    """The workspace can't receive a brief yet (no owner row, still
    provisioning, Friday not seeded). The NORMAL first-run state -- this is
    what the whole mechanism exists to wait through, so it must never burn a
    failure attempt."""

    FAILED = "failed"
    """Fulfilment was attempted and something went wrong. Counted, and the
    budget below is what stops it going round forever."""


# Failure budget, persisted on the pending item (not in memory -- the process
# that fails is often not the process that retries). Exhausting it flips the
# item to ``brief_state: "failed"``, which re-shows the funnel with the error
# instead of leaving the user in an empty chat with an agent that never
# starts. Sized like ``MAX_BOOTSTRAP_ATTEMPTS``: enough to ride out a
# Paperclip blip, few enough that a deterministic failure surfaces in ~a
# minute rather than never.
MAX_DRAIN_FAILURES = 5

# In-process retry cadence. This exists because the /container/status poll --
# the other thing that would retry -- STOPS once the funnel dismisses: opening
# the chat surface makes runtime_phase "ready", and `cadenceFor` drops the
# poll interval to 0. So the drain cannot rely on being called again; it has
# to carry its own retry. Bounded, and short enough to stay inside a request
# lifetime's worth of background work.
_RETRY_BACKOFF_S = (2, 5, 10, 20, 30, 60, 60)

# A funnel claim held this long with nothing seeded behind it is not a peer
# mid-flight (a real drain takes seconds) -- it is a claim whose owner died
# between the CAS and the seed, e.g. a task killed by a rolling deploy. The
# generation fence has no stale-steal of its own, so without this the row sits
# at "submitted" with no goal and no project forever, unclaimable.
STALE_CLAIM_S = 300

# How long after a claim its seeded ids are treated as final. A drain that is
# going to roll back does so in its own except handler, seconds after
# recording the ids -- so past this, ids on the row mean the brief really was
# fulfilled and any pending item still sitting there can be retired.
SETTLED_CLAIM_S = 60


def _claim_age_s(submitted_at) -> float | None:
    """Seconds since the funnel claim landed, or None if unknown/unparseable.
    Accepts the datetime the dataclass carries or the ISO string DDB returns."""
    if submitted_at is None:
        return None
    stamped = submitted_at if hasattr(submitted_at, "timestamp") else None
    if stamped is None:
        try:
            stamped = datetime.fromisoformat(str(submitted_at))
        except ValueError:
            return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamped).total_seconds()


# Owners with a drain already running in THIS process. The poll fires this
# every ~3s per tab and the endpoint fires it once more; without dedupe each
# call would independently do a Clerk lookup, a Paperclip sign-in (no cache --
# see paperclip_user_session) and a list_agents, and on a failing owner would
# create and delete a goal/project pair every time.
_draining: set[str] = set()


async def reconcile_pending_brief(owner_id: str) -> None:
    """Fulfil a submitted funnel answer, if there is one and the workspace can
    now receive it. Retries itself until it succeeds or gives up.

    This is the FULFILMENT half of ``POST /onboarding/brief``. The endpoint
    itself does intake only (record what the user said, return); everything
    that needs Paperclip up and Friday seeded happens here, on the same
    server-owned, idempotent, claim-fenced pattern as
    ``bootstrap_dispatcher.maybe_dispatch_bootstrap``. Call sites:

      - ``routers/onboarding.py:submit_brief`` -- on every accepted POST,
        including a duplicate one, since a resubmit is often the user trying
        to unstick exactly this.
      - ``routers/container.py:container_status`` -- ahead of that endpoint's
        early returns, so a parked trial / gated / org-member poll still
        drains.

    **Why it retries itself rather than leaning on the poll.** The poll looks
    like a free retry engine and isn't: dismissing the funnel opens the chat
    surface, which makes ``runtime_phase`` "ready", which sets the client's
    poll interval to 0. The one moment a stranded brief needs another attempt
    is the moment the polling stops. So this owns its own bounded retry, and
    the poll is a secondary trigger for the case where the process holding
    that retry died.

    Never raises -- every caller fires it as a background task, where an
    escaping exception is an unretrieved-task error rather than a retry.
    """
    if owner_id in _draining:
        return
    _draining.add(owner_id)
    try:
        await _drain_until_settled(owner_id)
    except Exception:  # noqa: BLE001 — background task
        # CancelledError is NOT an Exception, so a shutdown still propagates
        # (the rollback inside has already run -- see its handler).
        logger.exception("reconcile_brief: drain loop failed for %s", owner_id)
    finally:
        _draining.discard(owner_id)


async def _drain_until_settled(owner_id: str) -> None:
    """Retry ``_fulfil_pending_brief`` until it settles, or the budget is out.

    NOT_READY costs nothing but a wait -- provisioning taking its time is the
    expected path, not a failure. Only a real FAILED attempt is counted, and
    only against the persisted budget, so attempts survive the process that
    made them.
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    repo = PaperclipRepo(table_name="paperclip-companies")

    for delay in _RETRY_BACKOFF_S:
        reason = ""
        try:
            outcome = await _fulfil_pending_brief(owner_id)
        except Exception as e:  # noqa: BLE001 — a raise IS a failed attempt
            # Deliberately broad. Some of what lands here is transient
            # (Paperclip warming, a 5xx) and some never resolves (Clerk has no
            # email for a deleted submitter; a TypeError from a real bug). The
            # budget below is what tells them apart in the end -- classifying
            # up front is guesswork, and getting it wrong in the "permanent"
            # direction is what turned a broken drain into a silent
            # forever-loop the user could not escape.
            logger.warning("reconcile_brief: attempt failed for %s: %s", owner_id, e, exc_info=True)
            outcome, reason = DrainOutcome.FAILED, f"{type(e).__name__}: {e}"

        if outcome is DrainOutcome.DONE:
            return
        if outcome is DrainOutcome.FAILED:
            failures = await repo.record_pending_brief_failure(owner_id, reason=reason)
            if failures >= MAX_DRAIN_FAILURES:
                # Give up VISIBLY: the funnel comes back carrying this error,
                # so the user can answer again instead of sitting in an empty
                # chat forever.
                logger.error(
                    "reconcile_brief: giving up on %s after %s failures (%s)",
                    owner_id,
                    failures,
                    reason,
                )
                await repo.fail_pending_brief(owner_id, reason=reason, owner_broadcast_id=owner_id)
                return
        await asyncio.sleep(delay)

    # Out of in-process retries without a terminal verdict -- the workspace
    # never came up. The item stays PENDING (not failed): this is a
    # provisioning problem, not a bad brief, and the next /container/status
    # poll re-enters here. Deliberately not marked failed, or a slow provision
    # would throw away a perfectly good answer.
    logger.info("reconcile_brief: %s not fulfilled within the in-process retry window; leaving pending", owner_id)


async def _fulfil_pending_brief(owner_id: str) -> DrainOutcome:
    """One fulfilment pass. May raise; ``_drain_until_settled`` counts that as
    a FAILED attempt exactly like a returned FAILED."""
    from core.containers import get_workspace
    from core.repositories.paperclip_repo import FunnelClaimResult, PaperclipRepo
    from core.services.clerk_admin import get_primary_email
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_user_session import get_user_session_cookie

    repo = PaperclipRepo(table_name="paperclip-companies")
    pending = await repo.get_pending_brief(owner_id)
    if pending is None or pending.get("brief_state") == "failed":
        # Nothing pending, or we already gave up and handed it back to the
        # user via the funnel. Either way this loop is over.
        return DrainOutcome.DONE

    # Strong read: we are about to conditionally write this same row.
    owner_row = await repo.resolve_owner_row(owner_id, consistent_read=True)
    if owner_row is None or not owner_row.onboarding_generation or owner_row.status != "active":
        # Not provisioned far enough to receive a brief. The pending item is
        # the whole point: nothing is lost by waiting.
        return DrainOutcome.NOT_READY
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
    except Exception as e:  # noqa: BLE001 — classified by the caller's budget
        # Counted as a FAILURE, not as "not ready". Most of what lands here IS
        # transient (Paperclip warming, a 5xx), but some of it never resolves:
        # Clerk raising for a submitter whose account was deleted, or an
        # AttributeError from a genuine bug in these three calls. Treating the
        # whole class as "retry next poll" is what turned a permanent error
        # into a silent forever-loop the user could never escape -- the budget
        # in _drain_until_settled is what ends it now, and exc_info is what
        # tells the next person which kind it was.
        logger.warning("reconcile_brief: fulfilment read phase failed for %s: %s", owner_id, e, exc_info=True)
        raise
    friday = next((a for a in agents if a.get("role") == "ceo"), None)
    if friday is None:
        # Paperclip seed hasn't landed Friday yet -- still provisioning.
        return DrainOutcome.NOT_READY
    friday_id = friday["id"]

    claim_result = await repo.claim_funnel_submission(
        owner_row.user_id,
        onboarding_generation=generation,
        submitted_by=submitted_by,
        owner_id=owner_id,
        brief_text=brief_text,
    )
    if claim_result is FunnelClaimResult.STALE_GENERATION:
        # Reprovisioned underneath us. Leave the pending item: the next pass
        # re-reads the new generation and drains against that instead.
        return DrainOutcome.NOT_READY
    if claim_result is FunnelClaimResult.ALREADY_SUBMITTED:
        return await _settle_lost_claim(repo, owner_id, owner_row, generation)

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

    # The pending item is deliberately NOT deleted here, even though we won the
    # claim and finished the work. Deleting it now hands the funnel's dismissal
    # back to a single signal -- the row's own funnel_state -- which for an org
    # is read through the eventually-consistent by-org-id GSI. A poll landing
    # in that replication window would see the item gone AND the row still
    # "not_started", and re-render the committed front door at a user who just
    # submitted (empty, since the client clears its draft on success).
    #
    # Retirement instead happens at one of two later, lag-free points:
    #   * the completion sweep in bootstrap_dispatcher, when funnel_state goes
    #     "complete" -- which is also the moment get_onboarding_state stops
    #     reading this item at all; or
    #   * _settle_lost_claim, once the seeded ids have outlived the rollback
    #     window (SETTLED_CLAIM_S), for the case where the kickoff never
    #     delivers.

    # The funnel-conversion event belongs HERE, not at intake: it is the
    # metric that answers "did onboarding work", and an intake-time emission
    # would have shown 100% success through a fleet-wide fulfilment outage --
    # the exact failure this whole mechanism exists to make visible.
    capture_product_event(
        distinct_id=submitted_by,
        event="first_run_brief_submitted",
        properties={"path": path, "no_idea": user_input is None},
    )

    # Same fire-and-forget hand-off the endpoint used to make: if the runtime
    # is already up, Friday's kickoff goes now; if it is still cold, the next
    # /container/status poll dispatches it (bootstrap_dispatcher's own guards
    # make a redundant call a cheap no-op).
    #
    # The onboarding dict is passed INLINE rather than letting the dispatcher
    # re-read the row. For an org that re-read goes through the
    # eventually-consistent by-org-id GSI, and the claim landed milliseconds
    # ago -- it would read funnel_state "not_started" and silently skip the
    # kickoff, which is precisely the miss the old router avoided by passing
    # this same dict. Every value here is one we just wrote or just read.
    #
    # Deliberately carries no bootstrap_dispatched_at/_dispatch_id: this fast
    # path cannot expire a stale claim (the dispatcher's staleness rule refuses
    # to act on a claim it cannot date -- fail closed), and it does not need to.
    # The /container/status poll passes the full row and is the recovery path.
    from core.services.bootstrap_dispatcher import maybe_dispatch_bootstrap

    await maybe_dispatch_bootstrap(
        owner_id,
        onboarding={
            "onboarding_generation": generation,
            "funnel_state": "submitted",
            "bootstrap_state": owner_row.bootstrap_state or "not_started",
            "bootstrap_attempts": owner_row.bootstrap_attempts or 0,
            "user_id": owner_row.user_id,
            "onboarding_submitted_by": submitted_by,
            "onboarding_brief_text": brief_text,
        },
    )
    return DrainOutcome.DONE


async def _settle_lost_claim(repo, owner_id: str, owner_row, generation: str) -> DrainOutcome:
    """We lost the funnel claim. Decide what that means for the pending item.

    Three cases, and the distinction is load-bearing:

    * **Fulfilment completed** (``funnel_state == "complete"`` -- set only by
      ``mark_bootstrap_delivered``, i.e. Friday's kickoff was OBSERVED to
      deliver). The brief is done; retire the item.
    * **A peer is mid-flight.** Leave the item alone and stop. It is NOT
      enough that the row carries ``onboarding_goal_id``: those ids are
      written by ``record_onboarding_ids`` BEFORE the last fallible step
      (``stage_first_run_files``), whose failure rolls the goal, the project
      AND the ids back. Deleting on that evidence loses the brief in exactly
      the case this mechanism exists to survive -- a winner that reverts.
    * **The claim is stale.** A drain that won the CAS and then died before
      seeding (a rolling deploy, a cancelled task) leaves the row at
      "submitted" with nothing behind it, and the generation fence has no
      stale-steal of its own -- so the row would sit unclaimable forever
      while the funnel stays dismissed. Revert the claim so the next pass can
      win it, the same recovery ``rearm_bootstrap_dispatch`` gives the
      bootstrap claim.
    """
    fresh = await repo.resolve_owner_row(owner_id, consistent_read=True)
    if fresh is None:
        return DrainOutcome.NOT_READY
    if (getattr(fresh, "funnel_state", None) or "") == "complete":
        await repo.delete_pending_brief(owner_id)
        return DrainOutcome.DONE
    age_s = _claim_age_s(getattr(fresh, "funnel_submitted_at", None))

    if fresh.onboarding_goal_id:
        if age_s is not None and age_s >= SETTLED_CLAIM_S:
            # Ids present and the winner is long past its own rollback window,
            # so they are stable: this brief WAS fulfilled, even though the
            # kickoff has not been observed to deliver yet. Retire the item.
            # Without this the winner's own delete is the only retirement, and
            # anything that wrote a second item (a funnel that briefly
            # re-rendered on a stale read, a racing peer) would leak one
            # forever -- and a leaked item's put-if-absent key blocks every
            # future brief for that owner.
            await repo.delete_pending_brief(owner_id)
        # Otherwise: seeded moments ago and still inside the window where the
        # winner could fail and roll the ids back. A peer owns this; leave the
        # item and let their success path retire it.
        return DrainOutcome.DONE

    if age_s is not None and age_s < STALE_CLAIM_S:
        # Young claim with no ids yet: a peer really is mid-seed. Wait.
        return DrainOutcome.DONE

    logger.warning(
        "reconcile_brief: reverting a stale funnel claim for %s (age=%ss, no seeded ids)",
        owner_id,
        int(age_s) if age_s is not None else "unknown",
    )
    await repo.revert_funnel_submission(fresh.user_id, onboarding_generation=generation, owner_id=owner_id)
    # Reverted, not fulfilled -- come back and claim it properly.
    return DrainOutcome.NOT_READY


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
