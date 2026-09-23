"""Workspace readiness helpers.

``provision_new_workspace`` is the ONE door that starts a new workspace's first
provision: the Clerk ``organization.created`` webhook calls it, and it raises on a
failure so Svix redelivers the event. ``ensure_user_ready`` is the lazy, on-demand
Paperclip JOIN (plus the owner seed reconcile on a running workspace) for the Teams
self-heal and the Paperclip-proxy handshake; it never dispatches a provision.
``GET /container/status`` calls neither -- it is a cold read.

Every helper here is idempotent -- every call is safe to repeat.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

from fastapi import HTTPException

from core.auth import AuthContext, is_org_owner_id, resolve_owner_id
from core.observability.metrics import put_metric
from core.observability.product_analytics import (
    capture_once_per_owner_per_day,
    capture_product_event,
)
from core.repositories import billing_repo, container_repo
from core.services.agent_routine_provisioning import (
    NIGHTLY_CEO_ROUTINE_CRON,
    NIGHTLY_CEO_ROUTINE_DESCRIPTION,
    NIGHTLY_CEO_ROUTINE_TITLE,
    create_enabled_schedule_routine,
)
from core.services.provision_dispatch import dispatch_provision
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.provision_gate import evaluate_provision_gate, should_arm_nightly_ceo
from core.services.trial_cap import utc_today

logger = logging.getLogger(__name__)

# Per-org in-process lock serializing the create-vs-join decision so two
# concurrent first-pollers in an all-admin org don't each create a separate
# Paperclip company. Paperclip ignores the Idempotency-Key header
# (see paperclip_admin_client.py module docstring), so dedup must happen
# here. Single-backend-instance scope: if the service scales to N tasks the
# window reopens across tasks; acceptable for current fleet size — the loser
# of a cross-instance race resolves to the join path on its next poll.
_org_provision_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Namespace for the deterministic ``trial_started`` event uuid. A fixed,
# arbitrary uuid4 — its only job is to keep our uuid5 from colliding with any
# other uuid5 we might derive from an owner id later. Changing it would let
# every existing owner emit a second trial_started, so: don't.
_TRIAL_STARTED_NAMESPACE = uuid.UUID("6f6c9f2e-2d5b-4a1c-9a3f-3d5c8e1b7a40")


async def ensure_workspace_trial_default(auth: AuthContext, workspace_id: str) -> None:
    """Create the active workspace's unarmed trial once, before either provision gate.

    The conditional put selects one creator across members and backend instances.
    Existing trial analytics describe row creation; runtime readiness arms its clock.

    Workspace-only, and the id prefix is checked FIRST: this function only ever
    CREATES, and the product never provisions a new personal workspace, so a
    ``user_``-prefixed id is a 403 rather than the 409 ``resolve_owner_id``
    would raise for a caller with no org claim. That makes it impossible to
    mint a ``trial_local`` row for a personal owner from any door.
    """
    if not is_org_owner_id(workspace_id) or resolve_owner_id(auth) != workspace_id:
        raise HTTPException(status_code=403, detail="Workspace does not match the active organization")
    await _ensure_trial_row(workspace_id)


async def _ensure_trial_row(workspace_id: str) -> None:
    """The trial-row body of ``ensure_workspace_trial_default``, after its auth check."""
    row = await billing_repo.get_by_owner_id(workspace_id)
    if row:
        # Every trial-default door offers the row to the 30-day heal: a trial expired under the
        # old 3-day window reads as parked, so no ready-only door would reach it.
        if row.get("subscription_status") == "trial_local" and row.get("trial_end") is not None:
            await billing_repo.extend_short_trial(workspace_id, row)
        return

    capture_once_per_owner_per_day(owner_id=workspace_id, event="trial_start_requested", properties={})
    try:
        created = await billing_repo.create_trial_local_if_absent(
            owner_id=workspace_id, owner_type="org", trial_end=None
        )
    except Exception as exc:
        capture_once_per_owner_per_day(
            owner_id=workspace_id,
            event="trial_start_failed",
            properties={"error_code": type(exc).__name__},
        )
        raise

    if created:
        capture_product_event(
            distinct_id=workspace_id,
            event="trial_started",
            properties={"owner_type": "org"},
            event_uuid=str(uuid.uuid5(_TRIAL_STARTED_NAMESPACE, f"{workspace_id}:{utc_today()}")),
        )


async def provision_new_workspace(org_id: str) -> None:
    """Clerk ``organization.created``: start the new workspace's first provision.

    Creates the trial row, checks the provision gate as an org admin (the creator is one),
    and dispatches. A gate that is up (e.g. an expired trial) is a normal return -- the billing
    event that clears it provisions. Anything else RAISES: this webhook is the only thing
    that starts a first provision, so a failure must make Svix redeliver the event.
    """
    if not is_org_owner_id(org_id):
        return
    try:
        await _ensure_trial_row(org_id)
        gate = await evaluate_provision_gate(owner_id=org_id, is_admin=True)
        if gate is not None:
            logger.info("provision_new_workspace: gate up for %s (%s); its clearing event provisions", org_id, gate)
            return
        if not await dispatch_provision(org_id):
            raise RuntimeError(f"provision dispatch did not start for {org_id}")
    except Exception:
        put_metric("provision.org_created.error")
        raise


async def ensure_user_ready(auth: AuthContext) -> None:
    """The lazy Paperclip JOIN for a real on-demand request. Idempotent; never dispatches.

    Callers: the Teams self-heal (``routers/teams/agents._self_heal_reconcile``) and the
    Paperclip-proxy handshake. The ``organizationMembership.created`` webhook is the primary
    JOIN; this catches a member it missed (e.g. one invited before the company was active).

      1. Paperclip workspace JOIN (org context; role-blind). JOIN-ONLY: it never creates a
         company, so no provision gate may swallow it.
      2. For an org ADMIN with the gate clear and a RUNNING workspace, the owner company
         seed reconcile (``_reconcile_owner_seed``) -- the one state a provision execution's
         step 0 will not reach. A workspace that is not running is the executor's to seed.

    Workspace-only. A caller with no org claim returns here, before anything else.
    """
    # Personal workspaces no longer exist, in the code or in the data: the
    # September 2026 cohort was purged or migrated onto workspaces (#2624,
    # #2625, #2627), and every helper below is org-only. Refusing here is the
    # fence; resolve_owner_id 409s the same caller at every product API, and
    # provision_runner step 0 refuses a non-org owner the executor is handed.
    if not auth.is_org_context:
        logger.info(
            "ensure_user_ready: personal (no-org) caller user=%s refused — a workspace is required",
            auth.user_id,
        )
        put_metric("ensure_user_ready.personal_refused")
        return

    owner_id = resolve_owner_id(auth)

    # A seed/join failure never escapes: the caller answers 202 and the next request retries.
    try:
        await _ensure_paperclip_for_member(
            org_id=owner_id,
            member_user_id=auth.user_id,
            member_email=auth.email or "",
        )
    except Exception:
        put_metric("paperclip.seed.error")
        logger.exception(
            "ensure_user_ready: paperclip membership-add failed for owner=%s user=%s — retried by the next request",
            owner_id,
            auth.user_id,
        )

    if not auth.is_org_admin:
        return
    # A provision gate speaks to company-CREATE economics, so it guards the seed alone.
    if await evaluate_provision_gate(owner_id=owner_id, is_admin=True) is not None:
        return
    row = await container_repo.get_by_owner_id(owner_id)
    if (row or {}).get("status") != "running":
        return
    try:
        await _reconcile_owner_seed(auth, owner_id)
    except Exception:
        put_metric("paperclip.seed.error")
        logger.exception(
            "Paperclip seed reconcile failed for owner %s — container unaffected; Teams degraded",
            owner_id,
        )


async def _reconcile_owner_seed(auth: AuthContext, owner_id: str) -> None:
    """Run the OWNER company seed in-request, for the one case the executor's step 0
    cannot cover: the workspace is already running, so no provision will run.

    ``_ensure_paperclip_for_org``, but only when the org has NO active company.
    The JOIN half already ran (``_ensure_paperclip_for_member``, role-blind, above);
    what is left to reconcile here is CREATE only, and only an admin creates.
    """
    # Only reached from ensure_user_ready's admin branch.
    provisioning = await build_paperclip_provisioning()
    try:
        if await provisioning.find_org_owner(owner_id) is not None:
            return
    finally:
        await close_paperclip_provisioning(provisioning)
    await _ensure_paperclip_for_org(
        org_id=owner_id,
        user_id=auth.user_id,
        email=auth.email or "",
        is_admin=True,
    )


def _arm_status(status: str) -> str:
    put_metric("nightly_ceo.arm", dimensions={"status": status})
    return status


async def arm_nightly_ceo(owner_id: str, *, trust_cache: bool = True) -> str:
    """Idempotently arm the paid nightly CEO routine on Friday.

    THE deterministic caller is the provision executor's step 0
    (``scripts.provision_runner._seed_owner_paperclip``), right after the
    owner's Paperclip seed — every path to "paid + provider chosen" (org first
    provision, trial→paid conversion via reprovision_for_user, admin
    reprovision, image roll) runs there. ``owner_id`` is the workspace (Clerk
    org) id; its company row comes from the authoritative owner resolver.

    Armed for paid OR an unexpired trial (``should_arm_nightly_ceo``): paid buys
    always-on, and the nightly CEO is ALSO extended to live ``trial_local``
    owners as a retention lever (an EXPIRED trial is not armed, and self-disarms
    at the wake path). Returns the outcome (``armed`` covers created, reactivated
    OR title-skipped: the routine is present either way) and emits it as the
    ``nightly_ceo.arm`` metric's ``status``. Does NOT
    swallow: a failure emits ``status=error`` and re-raises, and the caller
    decides — the executor classifies it into its retry ABI exactly like a
    seed failure; ``reconcile_runtime`` logs and moves on.

    ``trust_cache=False`` (the executor) never short-circuits on the
    ``nightly_ceo_armed_company`` cache: the claim is written BEFORE the routine
    is created, so a runner that dies between the two leaves a row that says
    "armed" with no routine — trusted, that cache would return ``already_armed``
    forever. The retry re-issues the create (title-skip makes it idempotent)
    without re-claiming.

    Idempotency, three race classes closed:
      - Normal re-run (every ``reconcile_runtime``): the ``nightly_ceo_armed_company`` cache on
        the billing row short-circuits before any Paperclip call once it equals
        the current company.
      - Concurrency: ``try_claim_nightly_arm`` is a conditional write that
        elects exactly one winner to seed (plus ``create_enabled_schedule_routine``'s
        title-skip as a backstop).
      - Owner opt-out: ``nightly_ceo_opted_out`` (durable — survives a full
        reprovision because billing is preserved) blocks arming entirely (bailed
        above). Any paused routine reaching the create step is therefore an
        ENTITLEMENT pause (teardown / cancel), which ``reconcile_paused=True``
        re-enables on re-entitlement. Both re-subscribe paths reach the reconcile:
        a torn-down container re-provisions through the executor
        (``trust_cache=False``, which ignores the cache), and a container that
        SURVIVED teardown is re-armed by the reconcile the re-subscribe's Stripe
        ``entering_live`` edge dispatches — teardown's ``disarm_owner_nightly_automation``
        releases the ``nightly_ceo_armed_company`` cache when it pauses, so that
        ``trust_cache=True`` arm no longer short-circuits on it.
    """
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        if not should_arm_nightly_ceo(account):
            return _arm_status("not_entitled")
        if account.get("nightly_ceo_opted_out"):
            return _arm_status("opted_out")

        provisioning = await build_paperclip_provisioning()
        try:
            # Confirm a COMMITTED active company before seeding. This is why
            # there is no orphan/failed-company cost leak: we only ever arm a
            # company provision_org actually finished (status="active").
            row = await provisioning._repo.resolve_owner_row(owner_id)
            # ``row.org_id != owner_id``: never arm a company that belongs to a
            # different workspace under this workspace's billing account.
            if row is None or row.status != "active" or row.org_id != owner_id:
                return _arm_status("company_not_active")
            company_id = row.company_id
            cached = account.get("nightly_ceo_armed_company") == company_id
            if cached and trust_cache:
                return _arm_status("already_armed")  # cheap cache hit
            friday_id = (row.starter_agent_ids or {}).get("ceo") or (row.seeded_agent_ids or [None])[0]
            if not friday_id:
                return _arm_status("friday_not_seeded")
            # A cache already naming this company means the claim is ours —
            # skip re-claiming and go straight to the title-idempotent create.
            if not cached and not await billing_repo.try_claim_nightly_arm(owner_id, company_id):
                return _arm_status("claim_lost")  # a concurrent arm won
            try:
                admin_cookie = await get_admin_session_cookie(provisioning._admin._http)
                await create_enabled_schedule_routine(
                    admin=provisioning._admin,
                    session_cookie=admin_cookie,
                    company_id=company_id,
                    agent_id=friday_id,
                    title=NIGHTLY_CEO_ROUTINE_TITLE,
                    description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
                    cron=NIGHTLY_CEO_ROUTINE_CRON,
                    timezone="UTC",
                    # Re-enable a teardown/expiry/past_due-paused routine on
                    # re-entitlement. Owner opt-outs are already filtered above
                    # (nightly_ceo_opted_out), so a paused routine here is safe
                    # to reactivate — without this a churn→resubscribe owner
                    # keeps a paused routine (the title-skip won't reconcile it).
                    reconcile_paused=True,
                )
            except Exception:
                # Seed failed — release the claim (or the stale cache) so the
                # next run retries, then let the caller classify.
                await billing_repo.release_nightly_arm(owner_id, company_id)
                raise
            # Leave the claim as the armed cache.
            return _arm_status("armed")
        finally:
            await close_paperclip_provisioning(provisioning)
    except Exception:
        put_metric("nightly_ceo.arm", dimensions={"status": "error"})
        raise


async def _ensure_paperclip_for_org(
    *,
    org_id: str,
    user_id: str,
    email: str,
    is_admin: bool,
) -> None:
    """Ensure this user has a row in the org's shared Paperclip company.

    CALLERS: ``scripts.provision_runner._seed_owner_paperclip`` (step 0 of the
    provisioning executor's runner — the normal path, passing the Clerk org
    CREATOR as ``user_id`` with ``is_admin=True``), and
    ``_reconcile_owner_seed`` for the one state the executor never reaches (an
    org whose container is already running or parked, so no provision — and
    therefore no step 0 — will run). A FIRST provision always goes through the
    executor. Concurrent executions are possible (dispatch is racy by design),
    so the per-org asyncio lock and ``try_acquire_org_create_claim`` below are
    the fence against a duplicate company until Paperclip company create is
    idempotent in prod.

    Decided by company existence:
      - Company exists → JOIN it (provision_member), regardless of role.
        This is the role-blind fix: admins and members alike join the one
        shared company.
      - No active company yet → only an ADMIN creates it (provision_org),
        under a per-org lock so concurrent first-pollers can't
        double-create. A member returns gracefully and waits — an admin
        will bootstrap the company (and the container it rides) on their
        poll, and the member joins on a subsequent poll. Letting a member
        create here would seed a starter roster pointing at a container members
        can't provision, leaving a company-without-container window.

    The underlying helpers treat a concurrent-create 409 as idempotent
    success and a duplicate-email 422 (Better Auth) as a recorded failure —
    neither re-raises — so this is safe to repeat.
    """
    async with _org_provision_locks[org_id]:
        provisioning = await build_paperclip_provisioning()
        try:
            owner = await provisioning.find_org_owner(org_id)

            if owner is None:
                if not is_admin:
                    # Member polling a not-yet-bootstrapped org: wait for an
                    # admin to create the shared company. The pending state is
                    # implicit and resolves on the member's next poll.
                    logger.info(
                        "ensure_user_ready (org member): org=%s has no active "
                        "paperclip company yet — waiting for an admin to "
                        "bootstrap it; retried by the next attempt",
                        org_id,
                    )
                    return

                # Strongly-consistent cross-instance guard against
                # double-create. The per-org asyncio lock above only
                # serializes THIS process, and find_org_owner reads the
                # eventually-consistent by-org-id GSI — so two admins on
                # different backend instances (or within the GSI propagation
                # gap) could both see owner=None and both create a separate
                # company, silently splitting the org's workspace. The claim
                # is a strongly-consistent PK conditional write that exactly
                # one caller wins; losers fall through to the join path on
                # their next poll once the winner's company is GSI-visible.
                claim_token = await provisioning.try_acquire_org_create_claim(org_id, holder=user_id)
                if not claim_token:
                    logger.info(
                        "ensure_user_ready (org): org=%s create claim held by "
                        "another backend instance — waiting; will join on the "
                        "next attempt",
                        org_id,
                    )
                    return

                # We won the claim: become the org's company owner-of-record.
                try:
                    await _ensure_paperclip_for_owner(owner_id=org_id, owner_user_id=user_id, owner_email=email)
                except BaseException:
                    # Release the claim so the next poll (this instance or
                    # another) can re-acquire and retry. Fenced on the
                    # per-attempt token so a stale straggler never deletes a
                    # newer attempt's live claim.
                    await provisioning.release_org_create_claim(org_id, token=claim_token)
                    raise

                # Verify the create actually wrote an active row before
                # leaving the claim in place. provision_org's
                # 409-as-idempotent path can return WITHOUT writing one — e.g.
                # a prior holder crashed (SIGKILL) after Better Auth signup
                # but before the row was persisted: the random signup password
                # is lost and Paperclip exposes no user-delete/reset, so this
                # email's signup now 409s and _ensure_paperclip_for_owner
                # swallows it as "already provisioned." Leaving the claim then
                # would strand the org behind a held claim with no discoverable
                # company. Releasing it lets the next poll retry — ideally
                # another admin whose signup isn't burned — recover the org.
                # Strongly-consistent read so we never false-release on our own
                # just-written row. On genuine success the claim stays as a
                # permanent "bootstrapped" marker (find_org_owner then returns
                # the company and this branch is not re-entered).
                #
                # The row must also belong to THIS org. provision_org
                # short-circuits on repo.get(user_id) when the user already has
                # an active row — and since rows are keyed per-user, a user
                # whose row belongs to ANOTHER workspace would make this read
                # return that unrelated active row even though no org-indexed
                # company exists. Checking org_id keeps us from holding the
                # claim on another workspace's row while find_org_owner(org_id)
                # stays None.
                row = await provisioning.get_company_row(user_id)
                if row is None or row.status != "active" or row.org_id != org_id:
                    logger.warning(
                        "ensure_user_ready (org): create for org=%s user=%s "
                        "returned without an active paperclip row (likely a "
                        "prior partial/crashed attempt); releasing create claim "
                        "so a retry can recover",
                        org_id,
                        user_id,
                    )
                    await provisioning.release_org_create_claim(org_id, token=claim_token)
                return
        finally:
            await close_paperclip_provisioning(provisioning)

    # Company already exists → join it. Outside the create lock so member
    # adds for an established org don't serialize behind one mutex.
    await _ensure_paperclip_for_member(
        org_id=org_id,
        member_user_id=user_id,
        member_email=email,
    )


async def _ensure_paperclip_for_owner(
    *,
    owner_id: str,
    owner_user_id: str,
    owner_email: str,
) -> None:
    """Ensure the org ``owner_id`` has a Paperclip company, created with
    ``owner_user_id`` as its owner-of-record.

    Idempotent — ``provisioning.provision_org`` short-circuits on an
    existing active row (see ``core/services/paperclip_provisioning.py``
    ``provision_org`` docstring).

    CALLER: ``_ensure_paperclip_for_org``'s create branch only, after it has
    won the org create claim.

    Resolves ``owner_email`` via ``lookup_owner_email`` when not
    supplied by the caller (e.g. a runner invocation with no JWT
    email claim).
    """
    if not owner_email:
        owner_email = (
            await lookup_owner_email(
                org_id=owner_id,
                fallback_user_id=owner_user_id,
            )
            or ""
        )
    if not owner_email:
        logger.warning(
            "ensure_user_ready: no email for owner=%s — paperclip provision skipped",
            owner_id,
        )
        put_metric(
            "ensure_user_ready.no_email",
            dimensions={"path": "owner"},
        )
        return

    provisioning = await build_paperclip_provisioning()
    try:
        try:
            before = await provisioning.get_company_row(owner_user_id)
            company = await provisioning.provision_org(
                owner_id=owner_id, owner_user_id=owner_user_id, owner_email=owner_email
            )
            from core.services.messaging import _STARTER_KEYS, ensure_default_room

            before_starters = getattr(before, "starter_agent_ids", None) if before else None
            company_starters = getattr(company, "starter_agent_ids", None)
            fresh_or_partial = (
                before is None
                or getattr(before, "org_id", None) != owner_id
                or (isinstance(before_starters, dict) and any(not before_starters.get(key) for key in _STARTER_KEYS))
            )
            complete = (
                getattr(company, "status", None) == "active"
                and isinstance(company_starters, dict)
                and set(company_starters) == set(_STARTER_KEYS)
                and all(company_starters.get(key) for key in _STARTER_KEYS)
                and len(set(company_starters.values())) == len(_STARTER_KEYS)
            )
            if fresh_or_partial and complete:
                try:
                    await ensure_default_room(owner_id, owner_user_id)
                except Exception:  # noqa: BLE001 — partial retries reconcile again
                    logger.exception("ensure_user_ready: default room creation failed for owner=%s", owner_id)
        except PaperclipApiError as exc:
            # 409: a CONCURRENT provision_org (the previous ~3s
            # attempt) is mid-flight and hasn't written the
            # ``status="active"`` row yet. Benign idempotent no-op — the next
            # poll finds the active row. Mirrors the member-path pattern in
            # ``_ensure_paperclip_for_member``.
            if exc.status_code == 409:
                logger.info(
                    "ensure_user_ready (owner): %s paperclip provision already in flight — no-op (409)",
                    owner_id,
                )
            elif exc.status_code == 422:
                # Better Auth returns 422 (NOT 409) for a duplicate email: the
                # Paperclip account already exists but provision_org could not
                # (re)provision the company — e.g. an orphaned Better Auth user
                # whose password was wiped, so it can neither adopt (no stored
                # password) nor re-sign-up. This is NOT success: provision_org's
                # own _mark_failed writes a durable ``failed`` marker (via
                # put-if-absent when no row exists), which is what surfaces the
                # failure — /teams → 503 and teams_status="failed" on
                # /container/status — instead of an eternal provisioning spinner.
                # We swallow the exception here (a duplicate is an EXPECTED
                # terminal outcome, not a transient error worth re-raising on
                # every poll) but record it as a FAILURE — never a success no-op,
                # so nothing reports Teams as ready over a broken workspace. Full
                # orphan RECOVERY (adopt/reset the Better Auth user) needs a
                # Paperclip user-delete the platform doesn't expose — out of
                # scope for this fix.
                logger.warning(
                    "ensure_user_ready (owner): %s paperclip duplicate-email 422 — "
                    "recorded failure; Teams degraded (orphaned Better Auth account)",
                    owner_id,
                )
                put_metric("paperclip.seed.duplicate_email", dimensions={"path": "owner"})
            else:
                raise
    finally:
        await close_paperclip_provisioning(provisioning)


async def _ensure_paperclip_for_member(
    *,
    org_id: str,
    member_user_id: str,
    member_email: str,
) -> None:
    """Add the org member to the org's existing Paperclip company.

    JOIN-ONLY: this helper never creates a company, which is why
    ``ensure_user_ready``'s gate-up branch may call it directly for any
    org-context caller — a provision gate (credits, oauth, billing) must
    not orphan a joining member, but company CREATE stays gated.

    Returns gracefully if the owner's company row doesn't exist or
    isn't active yet. The pending state is implicit ("Clerk-membership
    exists, paperclip-membership doesn't") and resolves on the
    member's next /teams request once the owner finishes
    provisioning.

    member_email comes from auth.email on the member's JWT; if
    missing, lookup_owner_email falls back to user_repo + Clerk.

    Implementation note: ``paperclip_repo`` does not expose a single
    "fetch the org's company row" helper at the module level. Instead we
    reuse ``PaperclipProvisioning.find_org_owner``, which already filters
    to ``status="active"`` rows and is the same lookup
    ``provision_member`` itself uses internally. Returning ``None`` from
    that helper means the owner's company is either missing or not yet
    active — indistinguishable for our purposes (both → return gracefully).

    Two DISTINCT duplicate outcomes, same request body, opposite verdicts —
    the membership webhook and every /teams self-heal may fire this helper
    repeatedly, so both must be handled:

    * **409 = join/concurrency path (idempotent no-op).** A concurrent
      member-join is mid-flight, or the member was already added by a prior
      successful run. Paperclip returns 409; we swallow and return success.
    * **422 = sign-up duplicate email (recorded FAILURE).** Better Auth returns
      422 (NOT 409) when the member's email already exists as an account it
      can't (re)provision. This is NOT success: ``provision_member``'s own
      ``_mark_failed`` records a durable ``failed`` marker, which is what
      surfaces the failure (``/teams`` → 503, ``teams_status="failed"`` on
      ``/container/status``) — no longer merely a log line. We swallow (don't
      re-raise on every poll) but record it as a failure. Other
      PaperclipApiError statuses re-raise.
    """
    provisioning = await build_paperclip_provisioning()
    try:
        owner_company = await provisioning.find_org_owner(org_id)
        if owner_company is None:
            logger.info(
                "ensure_user_ready (member): owner's paperclip company "
                "for org=%s not active yet — skipping membership-add for "
                "user=%s; retried by the member's next /teams request",
                org_id,
                member_user_id,
            )
            put_metric("paperclip.member_add", dimensions={"outcome": "no_company"})
            return

        if not member_email:
            member_email = await lookup_owner_email(org_id=None, fallback_user_id=member_user_id) or ""
        if not member_email:
            logger.warning(
                "ensure_user_ready: no email for member=%s — provision_member skipped",
                member_user_id,
            )
            put_metric(
                "ensure_user_ready.no_email",
                dimensions={"path": "member"},
            )
            put_metric("paperclip.member_add", dimensions={"outcome": "no_email"})
            return

        # ``lookup_owner_email`` short-circuits to None when
        # ``fallback_user_id`` is falsy (see paperclip_owner_email.py
        # lines 46-47), so we MUST thread the owner's Clerk user_id
        # here. The owner's user_id sits on the PaperclipCompany row
        # we already fetched via ``find_org_owner`` — passing None
        # would make the lookup return None and call provision_member
        # with owner_email="" which fails Paperclip sign-in.
        owner_email = (
            await lookup_owner_email(
                org_id=org_id,
                fallback_user_id=owner_company.user_id,
            )
            or ""
        )

        # Membership-add observability (the orphan-member incident was a
        # SILENT skip): one INFO on dispatch and one INFO/WARNING on the
        # outcome, both carrying org + user ids, plus the bounded-outcome
        # ``paperclip.member_add`` metric — so "no member-add ever ran for
        # this user" is a log search away, and its absence is alarmable.
        logger.info(
            "ensure_user_ready (member): dispatching paperclip membership-add org=%s user=%s",
            org_id,
            member_user_id,
        )
        try:
            await provisioning.provision_member(
                owner_id=org_id,
                user_id=member_user_id,
                email=member_email,
                owner_email=owner_email,
            )
        except PaperclipApiError as exc:
            # 409: a concurrent member-join is mid-flight (or the member was
            # already added by a prior successful run). This helper is fired
            # repeatedly (webhook, /teams self-heal), so treat as an idempotent no-op success.
            if exc.status_code == 409:
                logger.info(
                    "ensure_user_ready (member): %s already in paperclip company for org=%s — no-op (409)",
                    member_user_id,
                    org_id,
                )
                put_metric("paperclip.member_add", dimensions={"outcome": "already_active"})
            elif exc.status_code == 422:
                # Better Auth returns 422 (NOT 409) for a duplicate email — same
                # recorded-failure semantics as the owner guard: the account
                # already exists but the member could not be (re)provisioned. NOT
                # success; provision_member's own _mark_failed writes a durable
                # ``failed`` marker (put-if-absent when no row exists), which
                # surfaces it — /teams → 503, teams_status="failed" — not a mere
                # log line. Swallow (don't re-raise on every poll) but record it
                # as a FAILURE, never a success no-op.
                logger.warning(
                    "ensure_user_ready (member): %s paperclip duplicate-email 422 for org=%s — "
                    "recorded failure; Teams degraded",
                    member_user_id,
                    org_id,
                )
                put_metric("paperclip.seed.duplicate_email", dimensions={"path": "member"})
                put_metric("paperclip.member_add", dimensions={"outcome": "duplicate_email"})
            else:
                logger.warning(
                    "ensure_user_ready (member): paperclip membership-add failed org=%s user=%s status=%s",
                    org_id,
                    member_user_id,
                    exc.status_code,
                )
                put_metric("paperclip.member_add", dimensions={"outcome": "error"})
                raise
        except Exception:
            logger.warning(
                "ensure_user_ready (member): paperclip membership-add failed org=%s user=%s",
                org_id,
                member_user_id,
            )
            put_metric("paperclip.member_add", dimensions={"outcome": "error"})
            raise
        else:
            logger.info(
                "ensure_user_ready (member): paperclip membership-add ok org=%s user=%s",
                org_id,
                member_user_id,
            )
            put_metric("paperclip.member_add", dimensions={"outcome": "ok"})
    finally:
        await close_paperclip_provisioning(provisioning)
