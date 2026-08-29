"""Scheduling for the provisioning loop. No business rules live here.

There are no Provisioning webhooks, so each open workflow gets an asyncio task
that calls ``advance`` until the row goes terminal, and a startup sweep
re-enters every open row after a restart or deploy — the same shape as
``main._resume_wake_resize_transitions``.

Owner-blocked states (``awaiting_approval``, ``awaiting_user_action``,
``stale``) are DRIVEN, not abandoned: the guide says to keep polling the
Account request and the Payment profile from the server, and a card approved on
a phone must resume without waiting for a deploy. They just poll on the slow
clock. Only ``TERMINAL_STATES`` stops the loop; how long an owner may take is
the state machine's decision, not this module's.

A rolling deploy runs two backend instances (``maxHealthyPercent: 200``), so
every step is taken under a per-row LEASE (``repo.claim``): one driver per
workflow, and the other instance skips the row rather than filing the same card
or issuing the same Stripe read twice. The conditional ``transition`` is not
enough on its own — a step is a read plus a write, and both instances would
still poll.

This module never POSTs anything to Stripe. It calls ``advance``,
``on_approved``, ``on_rejected`` and one ``transition``; every decision about
what to write lives in ``provisioning_workflow``. Its log lines carry a
workflow id, a state and an error code — never a credential.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from core.repositories import provisioning_workflow_repo as repo
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.provisioning_workflow import advance, on_approved, on_rejected, poll_delay

logger = logging.getLogger(__name__)

# How long a workflow waiting on the owner sits before re-reading its approval.
IDLE_RECONCILE_S = 60
# Runaway guard: at POLL_MAX_S this is hours, well past the workflow's own
# 15-minute deadline, so it only ever catches a logic loop.
MAX_STEPS = 400
# Twice the longest sleep the loop can take, so a live driver always renews
# before its own lease lapses, and a dead one's row is claimable a minute later.
LEASE_S = 2 * IDLE_RECONCILE_S

# Which instance holds a lease. The pid matters: a rolling deploy can put two
# tasks on one host, and `HOSTNAME` is the Fargate task id in ECS.
INSTANCE_ID = f"{os.getenv('HOSTNAME') or 'local'}:{os.getpid()}"

# Waiting on a human, not on Stripe: the slow clock, not the poll backoff.
_OWNER_BLOCKED = frozenset({"awaiting_approval", "awaiting_user_action", "stale"})

# Paperclip's approved/rejected spellings. Several are accepted because the
# repo does not state them anywhere — see the VERIFY in the plan.
_APPROVED = frozenset({"approved", "accepted"})
# Rejection is a real outcome, not silence: without it a declined card polls
# for seven days and then goes stale, and the owner is never told it stopped.
_REJECTED = frozenset({"rejected", "declined", "denied"})

_TASKS: dict[str, asyncio.Task] = {}
# One log line per distinct wire spelling, so an unhandled one is visible in the
# logs instead of quietly stalling a workflow.
_SEEN_STATUSES: set[str] = set()


def _approval_outcome(approval: dict | None) -> str | None:
    raw = (approval or {}).get("status") or (approval or {}).get("state")
    if not isinstance(raw, str):
        return None
    if raw not in _SEEN_STATUSES:
        _SEEN_STATUSES.add(raw)
        logger.info("provisioning: paperclip approval status=%r", raw)
    lowered = raw.lower()
    if lowered in _APPROVED:
        return "approved"
    if lowered in _REJECTED:
        return "rejected"
    return None


async def _reconcile_approval(row: dict) -> dict | None:
    """Resume a workflow whose card was decided somewhere we never saw.

    Returns the moved row, or ``None`` when the card is still open. A Paperclip
    outage is transient by construction: the row is left exactly as it was and
    the next tick reads it again.
    """
    approval_id = row.get("approval_id")
    if not approval_id:
        return None
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        approval = await admin.get_approval(session_cookie=cookie, approval_id=approval_id)
    except (PaperclipApiError, httpx.HTTPError) as e:
        logger.warning("provisioning: approval read failed for %s: %s", row["workflow_id"], e)
        return None
    outcome = _approval_outcome(approval)
    if outcome is None:
        return None
    if outcome == "rejected":
        return await on_rejected(row["workflow_id"])
    # What the owner actually approved: the card's amount if they edited it,
    # otherwise the one it was filed with.
    usage_limit = ((approval or {}).get("payload") or {}).get("usage_limit") or row.get("usage_limit")
    return await on_approved(row["workflow_id"], usage_limit=usage_limit, approved_by="paperclip")


async def _drive(workflow_id: str) -> None:
    """Step one workflow until it is terminal or out of steps."""
    row = await repo.get(workflow_id)
    held = False
    for _ in range(MAX_STEPS):
        if row is None or row.get("state") in repo.TERMINAL_STATES:
            return
        if await repo.claim(workflow_id, owner=INSTANCE_ID, ttl_s=LEASE_S) is None:
            # Another instance is driving this row — a rolling deploy's
            # transient N=2. Not an error, and not a reason to give up either:
            # wait out its lease so a driver that dies mid-deploy is taken over
            # here rather than stalling until the next restart.
            held = False
            await asyncio.sleep(IDLE_RECONCILE_S)
            row = await repo.get(workflow_id)
            continue
        held = True
        before = row.get("state")
        try:
            if before == "awaiting_approval":
                # Poll Paperclip, not Stripe: nothing moves until the owner
                # approves or rejects the card.
                moved = await _reconcile_approval(row)
                row = moved if moved is not None else row
            else:
                row = await advance(row)
        except Exception:
            logger.exception("provisioning: step failed for %s", workflow_id)
            row = await repo.get(workflow_id)
            continue
        if row is None or row.get("state") != before:
            continue
        # Nothing moved. Owner-blocked states wait on a human, so they get the
        # slow clock; an in-flight Stripe operation gets the backoff.
        delay = IDLE_RECONCILE_S if before in _OWNER_BLOCKED else poll_delay(int(row.get("attempts") or 0))
        await asyncio.sleep(delay)
    if held and row and row.get("state") not in repo.TERMINAL_STATES:
        # Still moving after MAX_STEPS is a logic loop, not patience. Park it
        # with its ids intact rather than polling Stripe forever. Only when we
        # were the driver: a task that spent its steps waiting for someone
        # else's lease has no business parking their row.
        logger.warning("provisioning: workflow %s exceeded %d driver steps", workflow_id, MAX_STEPS)
        await repo.transition(
            workflow_id,
            from_state=row["state"],
            to_state="requires_review",
            fields={"error_code": "driver_steps_exceeded"},
        )


async def _run(workflow_id: str) -> None:
    """One driver task. Never raises, and never keeps a lease it is done with."""
    try:
        await _drive(workflow_id)
    except Exception:
        logger.exception("provisioning: driver stopped for %s", workflow_id)
    finally:
        try:
            await repo.release(workflow_id, owner=INSTANCE_ID)
        except Exception:
            logger.warning("provisioning: lease release failed for %s", workflow_id, exc_info=True)


async def kick(workflow_id: str) -> None:
    """Ensure a driver task exists for this workflow. Idempotent."""
    existing = _TASKS.get(workflow_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run(workflow_id), name=f"provisioning:{workflow_id}")
    _TASKS[workflow_id] = task
    # Pop by identity: a task that finished after a later kick replaced it must
    # not evict the live one.
    task.add_done_callback(lambda t, wid=workflow_id: _TASKS.pop(wid, None) if _TASKS.get(wid) is t else None)


async def resume_open_workflows() -> None:
    """Startup sweep. Best-effort: never raises into the lifespan."""
    try:
        rows = await repo.list_open()
    except Exception:
        logger.exception("provisioning: could not list open workflows; skipping the resume sweep")
        return
    resumed = 0
    for row in rows:
        workflow_id = row["workflow_id"]
        try:
            if await repo.claim(workflow_id, owner=INSTANCE_ID, ttl_s=LEASE_S) is None:
                continue  # the other instance still has it
            await kick(workflow_id)
            resumed += 1
        except Exception:
            logger.exception("provisioning: could not resume workflow %s", workflow_id)
    if resumed:
        logger.info("provisioning: resumed %d open workflow(s)", resumed)
