"""Arm Paperclip's NATIVE budget fence from Isol8's billing state.

Paperclip already owns a pre-run budget fence: ``budgets.getInvocationBlock``
(deployed fork ``2fdafe831``, ``server/src/services/budgets.ts:718``) is
consulted by every Paperclip-initiated agent wake, and a block is raised
BEFORE any ``heartbeat_runs`` row is inserted (``heartbeat.ts:15376``, which
records a ``budget.blocked`` skipped-request and throws ``conflict``).
Routine dispatch rethrows that (``routines.ts:1865``), so a blocked wake
deletes its issue, fails the routine run and ticks the circuit breaker
(``routines.ts:1899``); three consecutive failures auto-pause the routine
(``instance-settings.ts:190`` — ``autoPauseDefaultThreshold: 3``). That is
"park, don't storm", and it is native.

We never armed it. ``paperclip_provisioning.provision_org`` creates companies
with no budget state, so Paperclip keeps dispatching wakes into a container
whose inference the Isol8 bedrock-gate is already refusing.

**Why company pause and not a budget policy.** ``getInvocationBlock``'s
policy branch compares ``SUM(cost_events.cost_cents)`` (``budgets.ts:143``)
against a monthly-or-lifetime amount (``BUDGET_WINDOW_KINDS`` is exactly
``["calendar_month_utc", "lifetime"]``). Isol8's authoritative numbers are
in neither ledger — the free-tier daily counter is written into DynamoDB by
the Bedrock invocation-log meter Lambda, paid credits live in Metronome —
and the free tier's cap is per-UTC-DAY, a window Paperclip does not have.
So an amount-based policy is structurally unable to express our predicate.

``companies.status = "paused"`` is checked FIRST and is independent of cost
events (``budgets.ts:745``). It is also the budget subsystem's own
representation of a hard-stopped scope: ``pauseAndCancelScopeForBudget``
(``budgets.ts:252``) writes precisely that when a policy trips. Writing it
from Isol8's billing predicate IS arming the native fence.

**This is UX / blast-radius control, not enforcement.** The authoritative
stop stays the bedrock-gate's pre-flight reservation plus the meter Lambda's
backstop kill. Nothing here may become the only thing between an owner and a
runaway spend — hence every failure path below fails OPEN.

Design doc: ``docs/superpowers/specs/2026-07-25-paperclip-budget-reconciliation-design.md``
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo

logger = logging.getLogger(__name__)

# Fence-reason codes. The trial pair is re-exported from ``trial_cap`` so the
# fence can never disagree with the chat gate about what "out of runway"
# means; the other two are fence-local.
CREDITS_EXHAUSTED = "credits_exhausted"
SUBSCRIPTION_INACTIVE = "subscription_inactive"

# The ONLY provider that draws Isol8-billed prepaid Bedrock credits. Same
# single fence ``bedrock_enforcement._CREDIT_DRAWING_PROVIDER`` uses: a
# Metronome balance of 0 is NORMAL for a ChatGPT-OAuth or BYO-key owner (they
# have no credit grant at all), and reading it as exhaustion is the exact bug
# that once revoked free-tier owners ~30s after signup.
_CREDIT_DRAWING_PROVIDER = "bedrock_claude"

FENCED = "fenced"
OPEN = "open"

_PAPERCLIP_TABLE = "paperclip-companies"


async def fence_reason(
    owner_id: str,
    account: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Why this owner's Paperclip company must not start new agent work, or
    None.

    Mirrors ``connection_pool.gate_chat``'s layering, and is PROVIDER-AWARE
    for the same reason: only some owners' inference is billed to Isol8.

      1. **Entitlement.** A billing row that exists and is not active
         (canceled / unpaid / past_due / …) fences every provider — the
         container itself is on us even when the model is not.
      2. **Free tier.** ``trial_local`` owners are judged by the ONE trial
         predicate, ``trial_cap.reason_for_account`` (window expired OR over
         today's $3). Gated on ``subscription_status``, never on
         ``provider_choice``.
      3. **Paid Bedrock.** ``provider_choice == "bedrock_claude"`` owners are
         gated on the Metronome fast-gate read — the same predicate the chat
         and provision gates use.

    **ChatGPT-OAuth and BYO-key owners are never fenced on Isol8 budget.**
    Their inference runs on their own account/key, so our credit state says
    nothing about whether they may run; fencing them would be the single
    worst failure this module can produce. There is no default-deny branch —
    anything the ladder does not positively identify as out-of-runway returns
    None.

    A MISSING billing row returns None (OPEN). This is a deliberate
    divergence from ``gate_chat``, which blocks it: that gate faces a user
    who gets a card and can act, whereas this one silently pauses a whole
    company in the background. Absence of data is not evidence of
    exhaustion.

    ``account`` may be passed by a caller that already read the billing row,
    so a hot path pays for no second GetItem.
    """
    from core.repositories import billing_repo
    from core.services import trial_cap
    from core.services.provision_gate import is_subscription_active

    if account is None:
        account = await billing_repo.get_by_owner_id(owner_id)
    if account is None:
        return None

    if not is_subscription_active(account):
        return SUBSCRIPTION_INACTIVE

    if account.get("subscription_status") == "trial_local":
        # Returns TRIAL_EXPIRED / DAILY_CAP / None. Shared with the provision
        # gate, the container-start chokepoint and the Teams run guards, so no
        # door can disagree about whether a trial owner has runway.
        return await trial_cap.reason_for_account(owner_id, account)

    if account.get("provider_choice") == _CREDIT_DRAWING_PROVIDER:
        from core.services.metronome_gate import is_owner_blocked

        if await is_owner_blocked(owner_id):
            return CREDITS_EXHAUSTED

    return None


def schedule_reconcile(owner_id: str, *, account: Optional[Mapping[str, Any]] = None) -> None:
    """Fire-and-forget ``reconcile``.

    Every trigger site (the ``/container/status`` provision gate, the
    Metronome fresh-balance hook, provisioning) hangs this off a path that
    must not gain latency or a new failure mode. ``reconcile`` already
    swallows everything; this additionally keeps it off the caller's await
    chain.

    A no-op when Paperclip isn't configured (local dev / unit tests have no
    ``PAPERCLIP_INTERNAL_URL``, so there is no company to fence and no reason
    to spend a DynamoDB read finding that out), or when there is no running
    loop (synchronous contexts).

    **One reconcile per owner at a time.** Two reasons, one of them load-bearing:

      * ``/container/status`` polls every ~3s, so without this a slow reconcile
        would stack tasks that all compute the same answer; and
      * ``metronome_gate.apply_fresh_balance`` schedules a reconcile, and the
        reconcile's own predicate calls ``is_owner_blocked``, which on a stale
        fast-gate row refreshes and calls ``apply_fresh_balance`` again. Without
        an in-flight guard that is an unbounded Metronome-call loop whenever the
        fast-gate row write fails. The guard caps it at depth 1.
    """
    from core.config import settings

    if not (settings.PAPERCLIP_INTERNAL_URL or "").strip():
        return
    if owner_id in _INFLIGHT_OWNERS:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _INFLIGHT_OWNERS.add(owner_id)
    task = asyncio.create_task(reconcile(owner_id, account=account))

    def _done(_task: asyncio.Task) -> None:
        _INFLIGHT_OWNERS.discard(owner_id)
        _INFLIGHT_TASKS.discard(_task)

    # Retain a reference so the task isn't GC'd mid-flight. asyncio only holds
    # a weak reference to running tasks.
    _INFLIGHT_TASKS.add(task)
    task.add_done_callback(_done)


_INFLIGHT_TASKS: set[asyncio.Task] = set()
_INFLIGHT_OWNERS: set[str] = set()


async def reconcile(
    owner_id: str,
    *,
    account: Optional[Mapping[str, Any]] = None,
    repo: Optional[PaperclipRepo] = None,
) -> None:
    """Bring the owner's Paperclip company in line with Isol8's billing state.

    Idempotent and cheap when nothing changed: the last state we APPLIED is
    stamped on the paperclip-companies owner row, so a matching desired state
    costs one DynamoDB read and no Paperclip round-trip at all.

    The apply rule is deliberately asymmetric::

        desired = "fenced" if fence_reason(...) else "open"
        applied = row.budget_fence_state or "open"
        if desired == applied:  return
        if desired == "fenced": PATCH status=paused ; stamp fenced
        else:                   PATCH status=active ; stamp open

    We therefore only ever write ``status: "active"`` to undo a pause WE
    stamped. Paperclip's own budget subsystem pauses companies too, and
    ``GET /api/companies/{id}`` does not project ``pauseReason``
    (``services/companies.ts:247``), so the stamp is the only way to tell the
    two apart — without it a reconcile could silently lift someone else's
    hard stop.

    The converse is closed by construction: ``updateCompanySchema`` has no
    ``pauseReason`` field, so our pause lands with ``pauseReason = NULL``, and
    ``resumeScopeFromBudget``'s ``WHERE pauseReason = 'budget'`` filter
    (``budgets.ts:261``) cannot lift it.

    Never raises. A fence that throws into the provision gate or the
    Metronome webhook would be worse than a fence that is briefly stale.
    """
    try:
        await _reconcile_inner(owner_id, account=account, repo=repo)
    except Exception:
        put_metric("paperclip.budget_fence.error")
        logger.warning(
            "paperclip_budget_fence: reconcile failed for owner %s (next trigger retries)",
            owner_id,
            exc_info=True,
        )


async def _reconcile_inner(
    owner_id: str,
    *,
    account: Optional[Mapping[str, Any]] = None,
    repo: Optional[PaperclipRepo] = None,
) -> None:
    if repo is None:
        repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)

    row = await repo.resolve_owner_row(owner_id)
    if row is None or row.status != "active" or not row.company_id:
        # Nothing provisioned (or mid-provision / failed / disabled) — no
        # company to fence, and a half-built one must not be poked.
        return

    reason = await fence_reason(owner_id, account)
    desired = FENCED if reason else OPEN
    applied = row.budget_fence_state or OPEN
    if desired == applied:
        return

    await _apply(
        company_id=row.company_id,
        status="paused" if desired == FENCED else "active",
    )
    await repo.set_budget_fence_state(row.user_id, state=desired, reason=reason)
    put_metric("paperclip.budget_fence.applied", dimensions={"state": desired})
    logger.warning(
        "paperclip_budget_fence: company %s (owner %s) -> %s (reason=%s)",
        row.company_id,
        owner_id,
        desired,
        reason or "-",
    )


async def _apply(*, company_id: str, status: str) -> None:
    """``PATCH /api/companies/{id}`` with the instance-admin session.

    The admin is an ``owner``-role member of every Isol8 company (Paperclip's
    ``POST /api/companies`` grants membership to the creating actor, and
    ``provision_org`` always creates with the admin cookie), which is what
    ``assertSameCompanyCeoAgentOrBoard`` (``routes/companies.ts:415`` → ``:93``)
    requires. So no per-owner Better Auth sign-in is needed here.

    The 401-retry-after-invalidate is the same pattern provisioning uses.
    """
    from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
    from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session

    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)  # noqa: SLF001 — same pattern as provisioning
    try:
        await admin.patch_company(session_cookie=cookie, company_id=company_id, body={"status": status})
    except PaperclipApiError as e:
        if e.status_code != 401:
            raise
        invalidate_admin_session()
        cookie = await get_admin_session_cookie(admin._http)  # noqa: SLF001
        await admin.patch_company(session_cookie=cookie, company_id=company_id, body={"status": status})
