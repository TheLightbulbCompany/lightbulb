"""Create dormant Paperclip routines for an agent.

A Paperclip *schedule* is two objects:

  1. a **routine** (the work spec — title/description/assigneeAgentId), and
  2. a child **schedule trigger** (the cron + timezone that fires it).

The cron lives on the TRIGGER, not the routine, so provisioning a recurring
schedule is always a two-call sequence: ``create_routine`` → ``create_routine_trigger``.

COST-SAFE INVARIANT — every trigger this module creates is ``enabled: False``
(dormant). It exists and proves the plumbing but fires zero runs and costs
nothing until a human flips it on later. Every routine is created with
``concurrencyPolicy="coalesce_if_active"`` and ``catchUpPolicy="skip_missed"``
so that — even once a trigger IS flipped on — a slow run never stacks
overlapping executions and a missed tick is dropped rather than back-filled.

``paused=True`` moves the off switch from the trigger to the routine: the
routine is created ``status="paused"`` with an ENABLED trigger. Paperclip's
scheduler fires only ``routine.status == "active"`` AND ``trigger.enabled``, so
it is just as dormant — but the Teams Routines toggle (which PATCHes exactly
``{status: "active"}``) turns it on in one click. The starter routines use this.

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


# The retired nightly CEO routine's title. No longer seeded (the CEO wakes on a
# Paperclip timer heartbeat, armed by ``user_provision.arm_nightly_ceo``), but
# workspaces seeded before the switch still carry it, paused; the Teams routine
# routes key the owner's durable opt-out on this title.
NIGHTLY_CEO_ROUTINE_TITLE = "Nightly CEO progress"


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
    status: str = "active",
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
        "status": status,
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
        # satisfy "already exists".
        if r.get("status") == "archived":
            continue
        title = r.get("title")
        if isinstance(title, str):
            titles.add(title)
    return titles


async def create_agent_routines(
    *,
    admin: Any,
    session_cookie: str,
    company_id: str,
    agent_id: str,
    routines: list[dict],
    paused: bool = False,
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
                status="paused" if paused else "active",
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

        trigger = build_dormant_trigger_body(cron, timezone=spec.get("timezone") or _DEFAULT_TIMEZONE)
        if paused:
            trigger["enabled"] = True  # the paused routine is the off switch
        await admin.create_routine_trigger(
            session_cookie=session_cookie,
            routine_id=routine_id,
            body=trigger,
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
