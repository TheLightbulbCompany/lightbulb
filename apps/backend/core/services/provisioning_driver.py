"""Scheduling for the provisioning loop. No business rules live here.

There are no Provisioning webhooks, so each open workflow gets an asyncio task
that calls ``advance`` until the row goes terminal, and a startup sweep
re-enters every open row after a restart or deploy — the same shape as
``main._resume_wake_resize_transitions``.

Owner-blocked states (``awaiting_approval``, ``awaiting_user_action``,
``stale``) are DRIVEN, not abandoned: the guide says to keep polling the
Account request and the Payment profile from the server, and a card approved on
a phone must resume without waiting for a deploy. They just poll on the slow
clock, and those ticks spend no step budget — how long an owner may take is the
state machine's decision, not this module's. The loop stops on
``TERMINAL_STATES``, on ``MAX_STEPS`` polling steps, on ``MAX_WALL_S``, or on a
``WorkflowRefused`` — a door that is shut for a reason no retry clears.

A rolling deploy runs two backend instances (``maxHealthyPercent: 200``), so
every step is taken under a per-row LEASE (``repo.claim``): one driver per
workflow, and the other instance waits rather than filing the same card or
issuing the same Stripe read twice. The conditional ``transition`` is not
enough on its own — a step is a read plus a write, and both instances would
still poll.

The election lives in the LOOP, not in the sweep: the new instance is started
and health-checked while the old one is still renewing, so a sweep that skipped
held rows would skip everything and never run again. It kicks every open row;
each task then waits its turn. Shutdown cancels the tasks so their leases are
released rather than left to expire.

This module never POSTs anything to Stripe. It calls ``advance``,
``on_approved``, ``on_rejected`` and one ``transition``; every decision about
what to write lives in ``provisioning_workflow``. Its log lines carry a
workflow id, a state and an error code — never a credential.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from core.repositories import provisioning_workflow_repo as repo
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.provisioning_workflow import (
    WorkflowRefused,
    _write_facts,
    advance,
    on_approved,
    on_rejected,
    poll_delay,
)

logger = logging.getLogger(__name__)

# How long a workflow waiting on the owner sits before re-reading its approval.
IDLE_RECONCILE_S = 60
# Runaway guard on the FAST clock only: at POLL_MAX_S, 400 polling steps is
# hours, well past the workflow's own 15-minute deadline, so it only ever
# catches a logic loop. Slow-clock ticks are not steps — a workflow may wait on
# its owner for a week without spending one.
MAX_STEPS = 400
# The bound that does cover waiting: just past the state machine's own 7-day
# `stale` deadline, so a row nobody ever came back to is parked rather than
# polled forever. Measured from the ROW's `created_at`, never from the task's
# start — a task-anchored bound restarts on every deploy, and an abandoned row
# outlives deploys by definition.
MAX_WALL_S = 8 * 24 * 3600
# Ceiling on ONE sleep between polls, so a Stripe `Retry-After` cannot park a
# workflow for longer than the slow clock would have taken to look again anyway.
MAX_POLL_SLEEP_S = 300
# Renewed at the top of every iteration, so this only has to outlast ONE
# iteration: the 60s idle sleep plus a worst-case step of two chained Stripe
# calls at the client's 45s timeout, doubled for headroom. Shorter would let a
# live driver's lease lapse mid-step; longer would leave a crashed driver's row
# stuck for no reason.
LEASE_S = 300

# Which instance holds a lease. The pid matters: a rolling deploy can put two
# tasks on one host, and `HOSTNAME` is the Fargate task id in ECS.
INSTANCE_ID = f"{os.getenv('HOSTNAME') or 'local'}:{os.getpid()}"

# Nothing here is waiting on Stripe, so these tick on the slow clock and spend
# no steps. `ask` is included because a row that never left it is not polling
# anything either — `start` should have moved it, and the wall clock is what
# eventually surfaces one that did not.
_SLOW_STATES = frozenset({"ask", "awaiting_approval", "awaiting_user_action", "stale"})

# Paperclip's own two outcomes (fork `services/approvals.ts`). Rejection is a
# real outcome, not silence: without it a declined card polls for seven days and
# then goes stale, and the owner is never told it stopped.
_OUTCOMES = ("approved", "rejected")

_TASKS: dict[str, asyncio.Task] = {}


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
    status = (approval or {}).get("status")
    if status not in _OUTCOMES:
        return None
    if status == "rejected":
        return await on_rejected(row["workflow_id"])
    # An approver CANNOT edit a card's payload in Paperclip, so the only limit
    # this path can honour is the one the card was filed with. An owner who
    # changed the amount comes through the approve route, which passes theirs.
    return await on_approved(row["workflow_id"], usage_limit=row.get("usage_limit"), approved_by="paperclip")


def _too_old(row: dict) -> bool:
    """Has this WORKFLOW run out of wall-clock time? Read off the row, not off
    the task: the task is replaced on every deploy, and the whole point of the
    bound is to catch a row that outlived one."""
    created_at = row.get("created_at") or ""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=MAX_WALL_S)).isoformat()
    return bool(created_at) and created_at < cutoff


def sleep_for(row: dict, slow: bool) -> float:
    """How long before this row is looked at again.

    Waiting on a human gets the slow clock. A polling row gets its own backoff,
    floored by any ``next_poll_after`` Stripe asked for (`Retry-After` on a 429)
    and capped so a bad header cannot strand it. Public so the test can assert
    the arithmetic without driving a whole loop.
    """
    if slow:
        return float(IDLE_RECONCILE_S)
    delay = poll_delay(int(row.get("attempts") or 0))
    return min(max(delay, _seconds_until(row.get("next_poll_after"))), MAX_POLL_SLEEP_S)


def _seconds_until(when: str | None) -> float:
    """Seconds from now to an ISO instant, never negative. An unparseable value
    is treated as "no floor" — the backoff alone still bounds the poll."""
    if not when:
        return 0.0
    try:
        return max(0.0, (datetime.fromisoformat(when) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


async def _park(row: dict, code: str) -> None:
    logger.warning("provisioning: workflow %s parked by the driver (%s)", row["workflow_id"], code)
    await repo.transition(
        row["workflow_id"], from_state=row["state"], to_state="requires_review", fields={"error_code": code}
    )
    # Friday's facts file must stop saying "in flight" the moment nothing drives it.
    await _write_facts(row["owner_id"])


async def _drive(workflow_id: str) -> None:
    """Step one workflow until it is terminal, out of steps, or out of time."""
    row = await repo.get(workflow_id)
    steps = 0
    while True:
        if row is None or row.get("state") in repo.TERMINAL_STATES:
            return
        claimed = await repo.claim(workflow_id, owner=INSTANCE_ID, ttl_s=LEASE_S)
        if claimed is None:
            # Another instance is driving this row — a rolling deploy's
            # transient N=2. Not an error, and not a reason to give up either:
            # wait out its lease so the row is taken over here the moment that
            # instance stops renewing.
            await asyncio.sleep(IDLE_RECONCILE_S)
            row = await repo.get(workflow_id)
            continue
        # The claim's own ALL_NEW projection, so a move the owner route made
        # while we were sleeping is seen on this tick rather than overwritten —
        # including one that finished the workflow.
        if claimed.get("state") in repo.TERMINAL_STATES:
            return
        row = claimed
        before = row.get("state")
        slow = before in _SLOW_STATES
        if _too_old(row):
            return await _park(row, "driver_wall_exceeded")
        if not slow:
            if steps >= MAX_STEPS:
                return await _park(row, "driver_steps_exceeded")
            steps += 1
        try:
            if before == "awaiting_approval":
                # Poll Paperclip, not Stripe: nothing moves until the owner
                # approves or rejects the card.
                moved = await _reconcile_approval(row)
                row = moved if moved is not None else row
            else:
                row = await advance(row)
        except WorkflowRefused as e:
            # A door the state machine will keep finding shut — the owner
            # disconnected the company Stripe account, say. Retrying it for
            # eight days is noise; park it for the human who has to fix it.
            return await _park(row, e.code)
        except Exception:
            logger.exception("provisioning: step failed for %s", workflow_id)
            # Back off before retrying: a failing step that looped at full
            # speed would burn the whole step budget in milliseconds and park a
            # workflow whose only problem was a blip.
            await asyncio.sleep(sleep_for(row, slow))
            row = await repo.get(workflow_id)
            continue
        if row is None or row.get("state") != before:
            continue
        # Nothing moved. Waiting on a human gets the slow clock; an in-flight
        # Stripe operation gets the backoff.
        await asyncio.sleep(sleep_for(row, slow))


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
    """Startup sweep. Best-effort: never raises into the lifespan.

    Kicks EVERY open row, including ones another instance still holds. It runs
    once per process, while the outgoing instance is still alive and renewing,
    so skipping held rows would skip all of them and nothing would ever look
    again. The lease election is the loop's job, not the sweep's.
    """
    try:
        rows = await repo.list_open()
    except Exception:
        logger.exception("provisioning: could not list open workflows; skipping the resume sweep")
        return
    resumed = 0
    for row in rows:
        workflow_id = row["workflow_id"]
        try:
            await kick(workflow_id)
            resumed += 1
        except Exception:
            logger.exception("provisioning: could not resume workflow %s", workflow_id)
    if resumed:
        logger.info("provisioning: resumed %d open workflow(s)", resumed)


async def cancel_all() -> None:
    """Shutdown: stop every driver task so each one's ``finally`` hands its
    lease back. Otherwise the leases of the instance being replaced sit there
    until they expire, and the new instance cannot touch those rows for as long
    as LEASE_S."""
    tasks = list(_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.clear()
