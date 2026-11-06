"""First-message onboarding intake and fulfilment.

The first human message in a fresh workspace's canonical room remains a
normal room dispatch. When that dispatch selected an agent, a post-send capture
persists its text and target, then this module creates the initial goal/project
and stages ``BRIEF.md`` in that agent's workspace exactly once. The
generation-fenced ``funnel_state`` is the durable claim; there is no synthetic
bootstrap message or bootstrap dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from enum import Enum

from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.repositories import messaging_repo
from core.services.person_label import is_relay_email

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

## What this first run must deliver

The person should see work done and make one decision. Nothing more.

1. **First reply: three sentences at most, the point first.** Restate the
   business in one line and say what you are making and when, e.g. "Got it:
   {{one line}}. I'm writing a one-page plan for the next two weeks. It'll be
   here in a few minutes."
2. **Then one result message** in this room, with exactly these parts:
   - **One document:** the plan, written as a Paperclip issue document, linked
     (see the lightbulb skill's `references/chat-vs-document.md`).
{decision_block}
   - **One connect card:** the one tool you need to do the first piece for
     real, as the link `<app_url>/chat?panel=connections&provider=<slug>`
     (`app_url` is in `lightbulb-facts.json`). Skip it if that tool is
     already connected.
   - **Three offers, written as outcomes:** each names a finished thing and
     the person's one part ("I'll draft the three invoices due this week; you
     send them in a tap."). Offer only what you can do with what is connected
     or one connect away. Never offer work you will not do.
3. **Only you speak in the room** until the approval is answered. Hand work to
   teammates through Paperclip tasks, not room @mentions. When the approval is
   answered, say in one line which teammate has it and what they will post.
"""

# A link or hostname token. It only means "the owner's site" under the framing in
# first_message_intake (a website prefix, or the whole message being the link).
# Labels take any letter or digit (Unicode, so bücher.de counts); the last label is letters
# or a punycode xn-- label (example.xn--p1ai), never digits, so "3.5" is not a site.
_URL_RE = re.compile(
    r"(https?://\S+|(?<![@\w.-])(?:[^\W_](?:(?:[^\W_]|-){0,61}[^\W_])?\.)+(?:xn--[a-z0-9-]{2,59}|[^\W\d_]{2,24})\b(?:/\S*)?)",
    re.IGNORECASE,
)
#: The third pre-typed reply (apps/frontend/src/components/messaging/FirstReplyChips.tsx)
#: starts with these words. Matching the prefix, not the whole sentence, survives copy tweaks.
_YOU_PICK_PREFIX = "not sure yet"
_WEBSITE_PREFIX_RE = re.compile(
    r"(here's our website|here is our website|our website is|our site is|website:)", re.IGNORECASE
)


def first_message_intake(text: str) -> tuple[str, str | None]:
    """(path, user_input) for the brief from the owner's first message.

    A website-framed message or a bare link -> ("grow", the link); "Not sure yet..." -> ("new", None), which the
    brief template renders as the owner asking the CEO to pick; anything else -> ("new", text).
    """
    stripped = text.strip()
    if stripped.lower().startswith(_YOU_PICK_PREFIX):
        return "new", None
    if re.match(r"(i'm|i am|im) starting", stripped, re.IGNORECASE):
        return "new", stripped
    prefix = _WEBSITE_PREFIX_RE.match(stripped)
    if prefix:
        match = _URL_RE.search(stripped[prefix.end() :])
    else:
        match = _URL_RE.fullmatch(stripped.strip("<>\"' "))
    if match:
        return "grow", _trim_link(match.group(0))
    return "new", stripped


_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}


def _trim_link(token: str) -> str:
    """Drop prose punctuation from the end of a link, keeping delimiters the link itself opened:
    ".../widget_(blue)" stays whole, "(https://acme.com)." and "<https://acme.com>" lose the wrapper."""
    token = token.rstrip(".,;:")
    while token and (token[-1] in _CLOSERS or token[-1] in "\"'"):
        last = token[-1]
        if last in _CLOSERS and token.count(_CLOSERS[last]) >= token.count(last):
            break
        token = token[:-1].rstrip(".,;:")
    return token


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


# The first run's one decision. Only the root CEO files ``approve_ceo_strategy``;
# the first message can be dispatched to a specialist (whichever target the room
# picked), and a specialist's decision is a plain board approval.
_CEO_DECISION = """   - **One decision:** one `approve_ceo_strategy` approval for the plan:
     call the `request_approval` tool with `companyPlan: true` and the plan's
     task in `tasks`, and give the `link` it returns so it shows as an
     Approve card. Any later sign-off is a `request_board_approval` (the same
     tool, without `companyPlan`), never a task assigned to the person."""
_SPECIALIST_DECISION = """   - **One decision:** one `request_board_approval` for the plan's first
     concrete action: call the `request_approval` tool with the plan's task in
     `tasks`, and give the `link` it returns so it shows as an Approve card.
     Never a task assigned to the person."""


def is_root_ceo(agent: dict | None) -> bool:
    """The root CEO by role, never by display name: ``role: ceo`` and no manager."""
    return bool(agent) and agent.get("role") == "ceo" and not agent.get("reportsTo")


def render_brief_md(
    *,
    path: str,
    user_input: str | None,
    detail: str | None,
    display_name: str,
    email: str,
    target_is_ceo: bool,
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
        decision_block=_CEO_DECISION if target_is_ceo else _SPECIALIST_DECISION,
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


def first_name_from_email(email: str | None) -> str:
    """ "priya.sharma@acme.co" -> "Priya Sharma". Names the seeded project."""
    local = "" if is_relay_email(email) else (email or "").split("@")[0].strip()
    return local.replace(".", " ").title() or "there"


class DrainOutcome(str, Enum):
    """What one pass of ``_fulfil_pending_brief`` achieved."""

    DONE = "done"
    """Fulfilled, already fulfilled by a peer, or nothing pending. Stop."""

    NOT_READY = "not_ready"
    """The workspace can't receive a brief yet (no owner row, still
    provisioning, selected agent not seeded). The NORMAL first-run state -- this is
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

# Ceiling on an honoured ``Retry-After``. Paperclip sends 60 on its rate-limited
# routes, so this never bites in practice -- it is here so a wrong or hostile
# header value cannot park a background task for hours.
_MAX_RATE_LIMIT_WAIT_S = max(_RETRY_BACKOFF_S)

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


#: How young a non-general room must be for its message to be read as the first-run DM's answer.
_FIRST_RUN_DM_SEQS = 20


async def capture_first_workspace_message(owner_id: str, message: dict) -> bool:
    """Turn the owner's first answer in the first-run room into onboarding intake: the first human message in the
    canonical #general, or (super-agent) the first human message after the welcome in the owner's Lightbulb DM.
    That answer also closes the DM's first run (``messaging.close_first_run``), captured or not."""
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.messaging import FIRST_RUN_KIND, TEAM_WELCOME_KIND, close_first_run, default_room_id

    author = message.get("author") or {}
    text = str(message.get("text") or "").strip()
    message_seq = int(message.get("messageSeq") or 0)
    in_general = message.get("roomId") == default_room_id(owner_id)
    # Elsewhere only a young room can be the first-run DM (its welcome is checked below).
    if message_seq < 1 or author.get("kind") != "human" or not (in_general or message_seq <= _FIRST_RUN_DM_SEQS):
        return False
    target_agent_id = next(
        (
            target.get("agentId")
            for target in message.get("dispatchTargets") or []
            if isinstance(target, dict) and isinstance(target.get("agentId"), str)
        ),
        None,
    )
    eligible = len(text) >= 3 and target_agent_id is not None
    if in_general and not eligible:
        return False  # #general can be long: no history read for a message that could not be captured anyway

    earlier = await messaging_repo.list_messages_between(str(message["roomId"]), 1, message_seq - 1)
    humans = [i for i, row in enumerate(earlier) if (row.get("author") or {}).get("kind") == "human"]
    welcome = next((i for i, row in enumerate(earlier) if row.get("kind") == FIRST_RUN_KIND), None)
    if welcome is not None:
        # Super-agent: the welcome in the owner's Lightbulb DM. Messages the owner sent before it (before provisioning
        # finished) do not count; the first one after it is the answer.
        if any(i > welcome for i in humans):
            return False
        await close_first_run(str(message["roomId"]))
    elif not in_general or humans:
        return False
    elif earlier and int(earlier[0].get("messageSeq") or 0) == 1 and earlier[0].get("kind") == TEAM_WELCOME_KIND:
        return False  # super-agent #general holds only the team line
    if not eligible:
        return False
    if len(text) > 2000:
        put_metric("OnboardingBriefTooLong")
        return False

    repo = PaperclipRepo(table_name="paperclip-companies")
    owner_row = await repo.resolve_owner_row(owner_id, consistent_read=True)
    if getattr(owner_row, "status", None) != "active" or getattr(owner_row, "funnel_state", None) != "not_started":
        return False

    path, user_input = first_message_intake(text)
    landed = await repo.put_pending_brief(
        owner_id,
        path=path,
        user_input=user_input,
        detail=None if user_input == text.strip() else text,
        brief_text=text,
        submitted_by=str(author["id"]),
        source_message_id=str(message["messageId"]),
        target_agent_id=target_agent_id,
    )
    if not landed:
        return False
    await reconcile_pending_brief(owner_id)
    return True


FRONT_DESK_BUILD_PROMPT = """\
Set up the front desk for {url}.

1. Read the whole site: every page, menu, service list, booking or ordering flow, hours, locations, delivery area, policies, and the listed phone number.
2. Save the facts you will need to answer customers to memory.
3. For each thing a customer can do on the site (order, book, get a quote, check availability), build an OpenCLI adapter: `opencli adapter init <site>/<command>`, write it, then verify it with `opencli adapter check <site>/<command>` and `opencli adapter test` against the real site. Prefer a public API, then captured network calls, then page actions.
4. Run /app/skills/lightbulb/scripts/lightbulb-text.sh set-business-phone <number> with the phone number the site lists.
5. Reply here with one line: "Front desk ready for {url}: <what you can do for customers>." Post nothing in rooms.
"""


async def start_front_desk_build_for_line(owner_id: str) -> bool:
    """DM Customer Operations the build prompt for the website the workspace's captured first message named.
    False when that message named no website, or the roster has no such agent.

    The website comes from the pending brief while it waits; once fulfilled it is deleted, so the owner row's
    stored brief text and submitter are read instead (the same text the pending brief was built from). One
    message per (workspace, url): the messaging IDEMP receipt makes a repeat a no-op."""
    import uuid

    from core.auth import AuthContext
    from core.repositories.paperclip_repo import PaperclipRepo, customer_ops_agent_id
    from core.services import messaging

    repo = PaperclipRepo(table_name="paperclip-companies")
    pending = await repo.get_pending_brief(owner_id)
    if pending:
        path, url, by = pending.get("path"), pending.get("input"), pending.get("submitted_by")
    else:
        row = await repo.resolve_owner_row(owner_id)
        text = getattr(row, "onboarding_brief_text", None)
        if not text:
            return False
        path, url = first_message_intake(text)
        by = getattr(row, "onboarding_submitted_by", None)
    if path != "grow" or not url or not by:
        return False
    agent_id = await customer_ops_agent_id(owner_id)
    if not agent_id:
        return False
    user_id = str(by)
    room_id = await messaging._agent_dm_room_id(owner_id, user_id, agent_id)
    await messaging.create_human_message(
        AuthContext(user_id=user_id, org_id=owner_id),
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-front-desk|{owner_id}|{url.strip().lower()}"),
            text=FRONT_DESK_BUILD_PROMPT.format(url=url),
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="front_desk",
    )
    return True


async def reconcile_pending_brief(owner_id: str) -> None:
    """Fulfil a captured first-room message when its workspace is ready.

    ``capture_first_workspace_message`` is intake; it persists the message
    and invokes this function after normal room dispatch. Everything that
    needs an active Paperclip workspace happens here under one idempotent,
    claim-fenced path. The provision executor (``scripts.provision_runner``)
    re-drains pending intake the moment step 0 makes the company active.

    It owns its own bounded retry because the capture's intake usually lands
    before the workspace is ready, and nothing else calls it again after the
    provision that follows.

    Never raises -- the capture fires it as a background task (where an
    escaping exception is an unretrieved-task error rather than a retry), and
    the executor bounds it with a timeout.
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
    made them. A Paperclip 429 is in the first category, not the second: it
    waits (honouring ``Retry-After``) and spends a pass of the ladder, but
    never a slot of the budget.
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    repo = PaperclipRepo(table_name="paperclip-companies")

    from core.services.paperclip_admin_client import PaperclipApiError

    for delay in _RETRY_BACKOFF_S:
        reason = ""
        try:
            outcome = await _fulfil_pending_brief(owner_id)
        except Exception as e:  # noqa: BLE001 — a raise IS a failed attempt
            # ...with ONE exception: a 429 is not an attempt that failed, it is
            # Paperclip asking us to come back later, and it always clears. It
            # used to cost a budget slot like any other raise, so five
            # rate-limit hits -- entirely possible when several brand-new
            # owners seed goals and projects at once -- permanently failed a
            # perfectly good brief. Deliberately NARROWER than
            # ``PaperclipApiError.retryable``, which also covers 5xx: a 5xx may
            # be an upstream that never recovers, and the budget is exactly
            # what stops that looping forever (see below). The pass is still
            # spent, so the ladder stays bounded either way.
            if isinstance(e, PaperclipApiError) and e.status_code == 429:
                wait = min(e.retry_after or delay, _MAX_RATE_LIMIT_WAIT_S)
                logger.info(
                    "reconcile_brief: rate limited for %s, retrying in %ss (not counted as a failure)",
                    owner_id,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            # Otherwise deliberately broad. Some of what lands here is transient
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
                await repo.fail_pending_brief(owner_id, reason=reason)
                return
        await asyncio.sleep(delay)

    # Out of in-process retries without a terminal verdict -- the workspace
    # never came up. The item stays PENDING (not failed): this is a
    # provisioning problem, not a bad brief, and the next provision execution
    # re-enters here. Deliberately not marked failed, or a slow provision
    # would throw away a perfectly good answer.
    logger.info("reconcile_brief: %s not fulfilled within the in-process retry window; leaving pending", owner_id)


async def _fulfil_pending_brief(owner_id: str) -> DrainOutcome:
    """One fulfilment pass. May raise; ``_drain_until_settled`` counts that as
    a FAILED attempt exactly like a returned FAILED."""
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

    submitted_by = pending.get("submitted_by") or owner_row.provisioner_user_id
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
        # Minted for a CURRENT member of the company — the provisioner while
        # they still are one, else any other joined member (the session is not
        # company-scoped, so it must be someone Paperclip still admits). Not
        # the submitter: they may not have joined yet. Every member reaches the
        # same company, so the goal/project land identically.
        members = owner_row.member_user_ids
        session_user = (
            owner_row.provisioner_user_id
            if owner_row.provisioner_user_id in members
            else next(iter(sorted(members)), owner_row.provisioner_user_id)
        )
        session_cookie = await get_user_session_cookie(
            user_id=session_user,
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
    if "target_agent_id" in pending:
        target_agent_id = pending.get("target_agent_id")
        target_agent = next((agent for agent in agents if agent.get("id") == target_agent_id), None)
    else:
        # Briefs persisted by the retired onboarding endpoint predate room
        # dispatch targets. Fulfil only those legacy rows through the existing
        # root CEO; current chat capture always persists an explicit target.
        target_agent = next((agent for agent in agents if is_root_ceo(agent)), None)
    if target_agent is None:
        # The selected dispatch target (or a legacy row's existing root CEO)
        # has not landed yet.
        return DrainOutcome.NOT_READY
    assigned_agent_id = target_agent["id"]

    claim_result = await repo.claim_funnel_submission(
        owner_id,
        onboarding_generation=generation,
        submitted_by=submitted_by,
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
        # Seed before staging BRIEF.md, so the dispatched agent has a project
        # to work against when it next reads its workspace.
        goal_id, project_id = await seed_goal_and_project(
            admin=admin,
            session_cookie=session_cookie,
            company_id=owner_row.company_id,
            friday_agent_id=assigned_agent_id,
            goal_title=derive_goal_title(path=path, user_input=user_input),
            project_name=project_name_for(first_name),
        )
        # Recorded BEFORE staging the workspace file, not after: this is the
        # generation-fenced write that decides who wins. Staging is a visible
        # side effect on the agent's next boot -- if this write raced a
        # reprovision (generation moved on) and staging ran first anyway, the
        # agent could boot against dead goal/project ids.
        await repo.record_onboarding_ids(
            owner_id,
            onboarding_generation=generation,
            goal_id=goal_id,
            project_id=project_id,
        )
        await stage_first_run_files(
            owner_id=owner_id,
            friday_agent_id=assigned_agent_id,
            brief_md=render_brief_md(
                path=path,
                user_input=user_input,
                detail=detail,
                display_name=first_name,
                email=email or "",
                target_is_ceo=is_root_ceo(target_agent),
            ),
        )
        completed = await repo.complete_funnel_submission(
            owner_id,
            onboarding_generation=generation,
        )
        if not completed:
            raise RuntimeError("onboarding generation changed before completion")
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
            await repo.revert_funnel_submission(owner_id, onboarding_generation=generation)
        except Exception as cleanup_err:
            logger.error("reconcile_brief: failed to revert funnel claim for %s: %s", owner_id, cleanup_err)
        raise

    await repo.delete_pending_brief(owner_id)

    # The funnel-conversion event belongs HERE, not at intake: it is the
    # metric that answers "did onboarding work", and an intake-time emission
    # would have shown 100% success through a fleet-wide fulfilment outage --
    # the exact failure this whole mechanism exists to make visible.
    capture_product_event(
        distinct_id=submitted_by,
        event="first_run_brief_submitted",
        properties={"path": path, "no_idea": user_input is None},
    )

    return DrainOutcome.DONE


async def _settle_lost_claim(repo, owner_id: str, owner_row, generation: str) -> DrainOutcome:
    """We lost the funnel claim. Decide what that means for the pending item.

    Three cases, and the distinction is load-bearing:

    * **Fulfilment completed** (``funnel_state == "complete"`` after the
      initial goal/project and BRIEF.md stage). The brief is done; retire it.
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
      while onboarding stays incomplete. Revert the claim so the next pass can
      win it.
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
    await repo.revert_funnel_submission(owner_id, onboarding_generation=generation)
    # Reverted, not fulfilled -- come back and claim it properly.
    return DrainOutcome.NOT_READY


async def stage_first_run_files(
    *,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
) -> None:
    """Write BRIEF.md, unconditionally, into the CEO agent's Hermes profile.

    Used to be write-if-missing because its existence WAS the one-shot
    marker for the flow. The generation-fenced ``funnel_state`` claim on the
    paperclip-companies owner row is that marker now, and winning it already
    proves this is the first run — so a BRIEF.md left behind by an attempt
    that failed partway is stale state to overwrite, not a reason to stop.
    Stopping there would strand the goal and project the caller seeded
    moments earlier.

    The profile prefix is that workspace's home — the bridge pulls it down beside ``SOUL.md``/
    ``AGENTS.md`` — so the file lands where the agent reads.
    ``write_profile_file`` raises rather than no-op when ``OWNER_STATE_BUCKET`` is unset, which
    is what keeps this the LAST fallible step the caller's rollback depends on.
    """
    from core.services.hermes_profiles import write_profile_file

    await write_profile_file(owner_id, friday_agent_id, "BRIEF.md", brief_md)
