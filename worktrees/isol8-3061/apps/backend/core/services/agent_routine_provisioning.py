"""Create dormant Paperclip routines for an agent.

A Paperclip *schedule* is two objects:

  1. a **routine** (the work spec — title/description/assigneeAgentId), and
  2. a child **schedule trigger** (the cron + timezone that fires it).

The cron lives on the TRIGGER, not the routine, so provisioning a recurring
schedule is always a two-call sequence: ``create_routine`` → ``create_routine_trigger``.

COST-SAFE INVARIANT — every trigger the GENERAL path
(``create_agent_routines`` / ``build_dormant_trigger_body``) creates is
``enabled: False`` (dormant). It exists and proves the plumbing but fires zero
runs and costs nothing until a human flips it on later. Every routine is
created with ``concurrencyPolicy="coalesce_if_active"`` and
``catchUpPolicy="skip_missed"`` so that — even once a trigger IS flipped on — a
slow run never stacks overlapping executions and a missed tick is dropped
rather than back-filled.

There is exactly ONE narrowly-scoped exception —
``build_enabled_trigger_body`` / ``create_enabled_schedule_routine``, used only
for the paid nightly CEO routine armed by ``user_provision``. Every OTHER path
here stays dormant. ``create_agent_routines`` (marketplace deploy, bootstrap,
its unit tests) is unchanged and never emits an enabled trigger.

Idempotent: before creating, we list the company's existing routines and skip
any whose ``assigneeAgentId`` already matches the target agent AND whose title
matches the spec. Re-running never duplicates a routine (it may, however, leave
an already-present routine's trigger untouched — we do not reconcile triggers
on an existing routine, only the routine row itself).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Applied to every routine we create. Pinned here (not passed in by callers) so
# the cost-safe coalesce/skip posture can't be accidentally overridden per-call.
_ROUTINE_CONCURRENCY_POLICY = "coalesce_if_active"
_ROUTINE_CATCH_UP_POLICY = "skip_missed"

# Default timezone for schedule triggers. UTC keeps cron expressions
# unambiguous across the fleet (no DST surprises).
_DEFAULT_TIMEZONE = "UTC"


# ---------------------------------------------------------------------------
# Nightly CEO routine — the single paid, ENABLED exception.
# ---------------------------------------------------------------------------
#
# Identity of the one enabled routine we seed so Friday wakes nightly and makes
# unattended progress. Armed by ``user_provision`` gated on
# ``should_arm_nightly_ceo`` — paid owners AND live (unexpired) ``trial_local``
# owners, the latter as a retention lever; an EXPIRED trial is never armed.

# Title is the idempotency key — STABLE FOREVER. Changing it breaks the
# title-skip guard (``create_enabled_schedule_routine``) and duplicates the
# routine on the next reprovision. Frozen.
NIGHTLY_CEO_ROUTINE_TITLE = "Nightly CEO progress"

# Cron: once daily at 08:00 UTC — off-peak for the US-weighted base (~00:00–03:00
# US). No IANA-tz field exists on the billing/user rows, so we commit to UTC
# unconditionally (the per-owner-local branch would be dead code). See the plan.
NIGHTLY_CEO_ROUTINE_CRON = "0 8 * * *"

# Description is server-authored and self-authorizing: the dispatched execution
# issue's body carries a COMPLETE, bounded instruction, so even a Friday whose
# AGENTS.md predates this feature reads a full self-contained loop from the
# issue. The ``[NIGHTLY-CEO-LOOP]`` sentinel is the first line — Friday's
# AGENTS.md keys the self-agenda branch on it AND on the server-controlled
# ``originKind == "routine_execution"`` (which a teammate cannot forge).
# run_analytics keys ``is_nightly_ceo`` tagging on the same sentinel.
NIGHTLY_CEO_SENTINEL = "[NIGHTLY-CEO-LOOP]"

NIGHTLY_CEO_ROUTINE_DESCRIPTION = (
    f"{NIGHTLY_CEO_SENTINEL} Scheduled company-control wake. Do CEO work only — "
    "set or clarify priority, synthesize existing evidence, resolve or reroute a "
    "blocker, verify a claimed outcome, or delegate one bounded specialist issue. "
    "Never self-assign or execute specialist work, and never take external, "
    "irreversible, financial, legal, or reputation-bearing action. Advance ONE "
    "actionable constraint; if the board is empty, create ONE task derived from "
    "the company goal; if everything is blocked on the owner, identify what to "
    "unblock. File what the day made durable: wiki_search the company wiki's "
    "newest sources and wiki_apply any company-durable decision or fact with "
    "provenance. ALWAYS end by emailing the owner what you did and the single "
    "best next step — never exit silently."
)


def build_dormant_trigger_body(cron: str, *, timezone: str = _DEFAULT_TIMEZONE) -> dict[str, Any]:
    """The exact schedule-trigger body we emit — always dormant.

    Shape matches upstream ``createRoutineTriggerSchema``'s ``schedule``
    discriminant (paperclip/packages/shared/src/validators/routine.ts):
    ``{kind:"schedule", cronExpression, timezone, enabled:false}``.

    ``enabled`` is hard-coded ``False`` and is NOT a parameter — there is no
    way to ask this function for a live trigger.
    """
    return {
        "kind": "schedule",
        "cronExpression": cron,
        "timezone": timezone,
        "enabled": False,
    }


def _build_routine_body(
    *,
    title: str,
    description: str,
    agent_id: str,
    variables: list[dict] | None = None,
) -> dict[str, Any]:
    """The routine (work-spec) body. The cron is NOT here — it rides the
    trigger created separately.

    ``variables`` must ride along when the description references
    ``{{NAME}}`` placeholders: Paperclip syncs unreferenced-but-declared
    definitions away AND referenced-but-undeclared names into REQUIRED
    definitions with no default — and ``assertScheduleCompatibleVariables``
    then 422s schedule-trigger creation for any required variable without a
    default, which rolled back entire marketplace deploys.
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "assigneeAgentId": agent_id,
        "status": "active",
        "concurrencyPolicy": _ROUTINE_CONCURRENCY_POLICY,
        "catchUpPolicy": _ROUTINE_CATCH_UP_POLICY,
    }
    if variables:
        body["variables"] = variables
    return body


def _existing_titles_for_agent(existing_routines: Any, agent_id: str) -> set[str]:
    """Titles of routines already assigned to ``agent_id``.

    ``existing_routines`` is whatever ``list_routines`` returned. Upstream's
    ``GET /companies/{companyId}/routines`` returns a bare JSON array
    (``res.json(result)``), but the client is typed ``-> dict`` and the BFF
    envelopes it under ``"routines"``, so accept both a bare list and an
    enveloped ``{"routines": [...]}`` shape defensively.
    """
    if isinstance(existing_routines, dict):
        items = existing_routines.get("routines") or existing_routines.get("items") or []
    elif isinstance(existing_routines, list):
        items = existing_routines
    else:
        items = []
    titles: set[str] = set()
    for r in items:
        if not isinstance(r, dict):
            continue
        if r.get("assigneeAgentId") != agent_id:
            continue
        # An archived routine is removed-from-service (delete_routine archives
        # rather than deletes — upstream has no DELETE route). It must not
        # satisfy "already exists", or the compensating cleanup in
        # create_enabled_schedule_routine would leave the owner durably armed
        # with a routine that can never fire.
        if r.get("status") == "archived":
            continue
        title = r.get("title")
        if isinstance(title, str):
            titles.add(title)
    return titles


def _find_routine_for_agent(existing_routines: Any, agent_id: str, title: str) -> dict | None:
    """The non-archived routine of ``title`` assigned to ``agent_id``, or None.

    Same shape-handling and archived-exclusion as ``_existing_titles_for_agent``
    (they must agree on what "already exists" means), but returns the routine row
    so a caller can inspect and reconcile its status."""
    if isinstance(existing_routines, dict):
        items = existing_routines.get("routines") or existing_routines.get("items") or []
    elif isinstance(existing_routines, list):
        items = existing_routines
    else:
        items = []
    for r in items:
        if not isinstance(r, dict) or r.get("assigneeAgentId") != agent_id or r.get("status") == "archived":
            continue
        if r.get("title") == title:
            return r
    return None


async def create_agent_routines(
    *,
    admin: Any,
    session_cookie: str,
    company_id: str,
    agent_id: str,
    routines: list[dict],
) -> list[str]:
    """Create dormant routines for ``agent_id`` in ``company_id``.

    For each spec ``{title, description, cron}`` (plus optional ``timezone``,
    which a marketplace-captured routine carries — without it a seller's
    non-UTC schedule would be recreated as UTC and fire at the wrong local
    time once the buyer enables it):
      1. skip it if a routine with that title is already assigned to the agent
         (idempotent — re-runs never duplicate);
      2. else ``create_routine`` (→ routine id), then ``create_routine_trigger``
         with the dormant ``enabled:false`` schedule body.

    Args:
        admin: a ``PaperclipAdminClient`` (or anything with ``list_routines``,
            ``create_routine``, ``create_routine_trigger`` — injected so tests
            don't need a live Paperclip).
        session_cookie: the board-actor session cookie (must carry
            ``tasks:assign`` authority — these routes require it).
        company_id: the Paperclip company that owns the agent.
        agent_id: the routine assignee (``assigneeAgentId``).
        routines: list of ``{"title", "description", "cron"}`` dicts, each
            optionally carrying ``"timezone"`` (defaults to UTC).

    Returns:
        The routine ids we created this call (NOT including skipped ones).
    """
    existing = await admin.list_routines(
        session_cookie=session_cookie,
        company_id=company_id,
    )
    existing_titles = _existing_titles_for_agent(existing, agent_id)

    created_ids: list[str] = []
    for spec in routines:
        title = spec["title"]
        description = spec.get("description", "")
        cron = spec["cron"]

        if title in existing_titles:
            logger.info(
                "agent_routine_provisioning: skip existing routine title=%r agent=%s company=%s",
                title,
                agent_id,
                company_id,
            )
            continue

        routine = await admin.create_routine(
            session_cookie=session_cookie,
            company_id=company_id,
            body=_build_routine_body(
                title=title,
                description=description,
                agent_id=agent_id,
                variables=spec.get("variables"),
            ),
        )
        routine_id = routine.get("id") if isinstance(routine, dict) else None
        if not routine_id:
            # Defensive: upstream always returns the created routine with an
            # id, but if a future shape change drops it we can't attach a
            # trigger. Log + move on rather than crash the whole batch.
            logger.warning(
                "agent_routine_provisioning: create_routine returned no id title=%r agent=%s — skipping trigger",
                title,
                agent_id,
            )
            continue

        await admin.create_routine_trigger(
            session_cookie=session_cookie,
            routine_id=routine_id,
            body=build_dormant_trigger_body(cron, timezone=spec.get("timezone") or _DEFAULT_TIMEZONE),
        )
        logger.info(
            "agent_routine_provisioning: created DORMANT routine id=%s title=%r cron=%r agent=%s company=%s",
            routine_id,
            title,
            cron,
            agent_id,
            company_id,
        )
        created_ids.append(routine_id)
        # Track within this batch so a duplicate spec in the same call is a no-op too.
        existing_titles.add(title)

    return created_ids


# ---------------------------------------------------------------------------
# The single ENABLED path — paid nightly CEO routine ONLY.
# ---------------------------------------------------------------------------


def build_enabled_trigger_body(cron: str, *, timezone: str = _DEFAULT_TIMEZONE) -> dict[str, Any]:
    """Enabled schedule-trigger body. Same shape as ``build_dormant_trigger_body``
    but ``enabled: True``.

    Reserved for the single paid nightly CEO routine — the ONE deliberate
    exception to this module's dormant-only invariant. Do NOT call this from
    ``create_agent_routines`` or any marketplace/bootstrap path.
    """
    return {
        "kind": "schedule",
        "cronExpression": cron,
        "timezone": timezone,
        "enabled": True,
    }


async def create_enabled_schedule_routine(
    *,
    admin: Any,
    session_cookie: str,
    company_id: str,
    agent_id: str,
    title: str,
    description: str,
    cron: str,
    timezone: str = _DEFAULT_TIMEZONE,
    reconcile_paused: bool = False,
) -> str | None:
    """Create ONE enabled schedule routine for ``agent_id``, idempotently.

    ``reconcile_paused`` OPTS IN to re-enabling an existing PAUSED routine
    (``patch_routine`` status='active') instead of skipping it. Callers that set
    it MUST have filtered deliberate owner opt-outs first (``arm_nightly_ceo``
    bails on ``nightly_ceo_opted_out``), so a paused routine here is an
    entitlement pause (teardown / past_due / trial expiry), safe to reactivate.
    The disarm sets only status=paused (the schedule trigger stays enabled), so
    status='active' is all it takes to re-arm.

    Title-idempotent exactly like ``create_agent_routines``: list existing
    routines; if one with ``title`` is already assigned to ``agent_id``, return
    ``None`` WITHOUT creating and WITHOUT touching its trigger. Skipping purely
    on title-match is what makes a paused routine STAY paused — we never
    reconcile an existing routine's trigger, so an owner who disabled this
    routine is never silently re-enabled. Only when the title is ABSENT do we
    create (routine + enabled trigger).

    Reuses ``_build_routine_body`` so the ``coalesce_if_active`` /
    ``skip_missed`` posture is identical to every other routine — those bounds
    cap a single wake's blast radius and must stay intact.

    Returns the new routine id on create, or ``None`` on a title-skip (the
    routine is present either way).
    """
    existing = await admin.list_routines(session_cookie=session_cookie, company_id=company_id)
    existing_routine = _find_routine_for_agent(existing, agent_id, title)
    if existing_routine is not None:
        if reconcile_paused and existing_routine.get("status") == "paused":
            await admin.patch_routine(
                routine_id=existing_routine["id"],
                session_cookie=session_cookie,
                body={"status": "active"},
            )
            logger.info(
                "agent_routine_provisioning: reactivated paused routine title=%r agent=%s company=%s",
                title,
                agent_id,
                company_id,
            )
            return existing_routine["id"]
        logger.info(
            "agent_routine_provisioning: skip existing ENABLED routine title=%r agent=%s company=%s",
            title,
            agent_id,
            company_id,
        )
        return None

    routine = await admin.create_routine(
        session_cookie=session_cookie,
        company_id=company_id,
        body=_build_routine_body(title=title, description=description, agent_id=agent_id),
    )
    routine_id = routine.get("id") if isinstance(routine, dict) else None
    if not routine_id:
        logger.warning(
            "agent_routine_provisioning: create_routine returned no id for ENABLED routine title=%r agent=%s",
            title,
            agent_id,
        )
        return None

    try:
        await admin.create_routine_trigger(
            session_cookie=session_cookie,
            routine_id=routine_id,
            body=build_enabled_trigger_body(cron, timezone=timezone),
        )
    except Exception:
        # The routine now exists but has NO trigger. If we left it, the next
        # arm retry would title-skip onto this trigger-less routine (title-skip
        # never reconciles triggers) and treat the skip as success — leaving the
        # owner durably "armed" with a routine that can never fire. Compensate
        # by deleting the trigger-less routine so a retry starts from a clean
        # slate and recreates routine + trigger together. Best-effort cleanup;
        # re-raise the original failure so the caller releases the arm claim.
        try:
            await admin.delete_routine(routine_id=routine_id, session_cookie=session_cookie)
        except Exception:
            logger.warning(
                "agent_routine_provisioning: failed to clean up trigger-less routine id=%s title=%r agent=%s",
                routine_id,
                title,
                agent_id,
                exc_info=True,
            )
        raise
    logger.info(
        "agent_routine_provisioning: created ENABLED routine id=%s title=%r cron=%r agent=%s company=%s",
        routine_id,
        title,
        cron,
        agent_id,
        company_id,
    )
    return routine_id
