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

import logging
import uuid

from fastapi import HTTPException

from core.auth import AuthContext, is_org_owner_id, resolve_owner_id
from core.observability.metrics import put_metric
from core.observability.product_analytics import (
    capture_once_per_owner_per_day,
    capture_product_event,
)
from core.repositories import billing_repo, container_repo
from core.services.provision_dispatch import dispatch_provision
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.paperclip_agent_lifecycle import (
    pause_owner_automation,
)
from core.services.provision_gate import evaluate_provision_gate
from core.services.trial_cap import utc_today

logger = logging.getLogger(__name__)

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
    await _ensure_trial_row(workspace_id, actor_user_id=auth.user_id)


async def _ensure_trial_row(workspace_id: str, actor_user_id: str | None = None) -> None:
    """The trial-row body of ``ensure_workspace_trial_default``, after its auth check.

    ``actor_user_id`` is the human behind this door (the org's creator on the
    webhook, the caller on a status poll): the trial events are captured on
    THEIR PostHog person, with ``workspace_id`` as a property, so a user's
    timeline shows their trial instead of an ``org_…`` ghost owning it.
    """
    row = await billing_repo.get_by_owner_id(workspace_id)
    if row:
        # Every trial-default door offers the row to the 30-day heal: a trial expired under the
        # old 3-day window reads as parked, so no ready-only door would reach it.
        if row.get("subscription_status") == "trial_local" and row.get("trial_end") is not None:
            await billing_repo.extend_short_trial(workspace_id, row)
        return

    capture_once_per_owner_per_day(
        owner_id=workspace_id, event="trial_start_requested", properties={}, distinct_id=actor_user_id
    )
    try:
        created = await billing_repo.create_trial_local_if_absent(
            owner_id=workspace_id, owner_type="org", trial_end=None
        )
    except Exception as exc:
        capture_once_per_owner_per_day(
            owner_id=workspace_id,
            event="trial_start_failed",
            properties={"error_code": type(exc).__name__},
            distinct_id=actor_user_id,
        )
        raise

    if created:
        from core.services.workspace_source import adopt_for_workspace

        source = await adopt_for_workspace(workspace_id, actor_user_id or "")
        capture_product_event(
            distinct_id=actor_user_id or workspace_id,
            event="trial_started",
            properties={**source, "owner_type": "org", "workspace_id": workspace_id},
            event_uuid=str(uuid.uuid5(_TRIAL_STARTED_NAMESPACE, f"{workspace_id}:{utc_today()}")),
        )


async def provision_new_workspace(org_id: str, created_by: str | None = None) -> None:
    """Clerk ``organization.created``: start the new workspace's first provision.

    Creates the trial row, checks the provision gate as an org admin (the creator is one),
    and dispatches. A gate that is up (e.g. an expired trial) is a normal return -- the billing
    event that clears it provisions. Anything else RAISES: this webhook is the only thing
    that starts a first provision, so a failure must make Svix redeliver the event.
    """
    if not is_org_owner_id(org_id):
        return
    try:
        await _ensure_trial_row(org_id, actor_user_id=created_by)
        gate = await evaluate_provision_gate(owner_id=org_id, is_admin=True, actor_user_id=created_by)
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
    if await evaluate_provision_gate(owner_id=owner_id, is_admin=True, actor_user_id=auth.user_id) is not None:
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


def ceo_agent_id(row) -> str | None:
    """The workspace's CEO: the starter CEO, else a legacy roster's first seeded agent."""
    return (row.starter_agent_ids or {}).get("ceo") or (row.seeded_agent_ids or [None])[0]


#: The feed's schedule (spec 2026-10-14 §4.2): three CEO routines a day, in the owner's local time.
FEED_ROUTINE_HOURS = (8, 13, 18)
FEED_ROUTINE_DESCRIPTION = (
    "Scheduled feed post. Look at what happened since the last scheduled feed post: rooms, Work, "
    "runs, sessions and connected tools. Read the feed's recent posts first and never repeat one; "
    "the feed's topic is the team's brief. If one thing matters, post ONE feed post about it:\n\n"
    "/app/skills/lightbulb/scripts/lightbulb-post.sh --feed --why \"<why it matters>\" <<'EOF'\n<post>\nEOF\n\n"
    "You may also research the web for news relevant to the company and the brief. "
    "If nothing matters, post nothing. How to write a post: the lightbulb skill's references/feed.md."
)


def feed_routine_title(hour: int) -> str:
    return f"Feed post {hour:02d}:00"


#: The CEO's planning sweep runs as a routine, not its timer heartbeat: Paperclip's timer
#: counts from the agent's last run of any kind, so a CEO busy with feed posts never gets one.
PLANNING_ROUTINE_HOUR = 9
PLANNING_ROUTINE_TITLE = "Plan the company"
PLANNING_ROUTINE_DESCRIPTION = (
    "Planning heartbeat. Follow your HEARTBEAT.md checklist; if it is empty, the paperclip skill's "
    "references/ceo-heartbeat.md. Review the company goals and the board, turn goals into work assigned "
    "to the right teammate with what done means, unblock what you can, and send the founders one short "
    "message: what moved and what needs them. If nothing changed, post nothing."
)
#: (kind, title, description, local hour) of every CEO routine; ``kind`` names its keys.
CEO_ROUTINES = [("feed", feed_routine_title(h), FEED_ROUTINE_DESCRIPTION, h) for h in FEED_ROUTINE_HOURS] + [
    ("planning", PLANNING_ROUTINE_TITLE, PLANNING_ROUTINE_DESCRIPTION, PLANNING_ROUTINE_HOUR)
]


async def arm_nightly_ceo(owner_id: str) -> str:
    """Workspaces never run Paperclip routines or heartbeats (Paperclip is being removed): pause whatever an
    earlier arm left on (idempotent), arm nothing, and ensure the proactive layer instead (first ideas; look
    around + feed posts while paying). Every billing change comes through here (the provision executor's
    step 0 and ``reconcile_runtime``), so the paying-only automations follow the plan. Never fails on the
    proactive ensure."""
    await pause_owner_automation(owner_id)
    from core.services import proactive

    try:
        await proactive.ensure(owner_id)
    except Exception:  # noqa: BLE001 -- the daily schedule reconcile ensures it again
        logger.exception("proactive ensure failed for owner=%s", owner_id)
    return _arm_status("super_agent")


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
    executor. Concurrent executions are possible (dispatch is racy by design);
    they converge at the source: Paperclip company create replays under the
    per-org ``Idempotency-Key`` (see ``provision_org``), and the provisioner's
    sign-up is fenced by the seed claim on their identity row.

    Decided by company existence:
      - Company exists → JOIN it (provision_member), regardless of role.
        This is the role-blind fix: admins and members alike join the one
        shared company.
      - No active company yet → only an ADMIN creates it (provision_org).
        A member returns gracefully and waits — an admin will bootstrap the
        company (and the container it rides) on their poll, and the member
        joins on a subsequent poll. Letting a member create here would seed a
        starter roster pointing at a container members can't provision,
        leaving a company-without-container window.

    The underlying helpers treat a concurrent-create 409 as idempotent
    success and a duplicate-email 422 (Better Auth) as a recorded failure —
    neither re-raises — so this is safe to repeat.
    """
    provisioning = await build_paperclip_provisioning()
    try:
        owner = await provisioning.find_org_owner(org_id)
    finally:
        await close_paperclip_provisioning(provisioning)

    if owner is None:
        if not is_admin:
            # Member polling a not-yet-bootstrapped org: wait for an admin to
            # create the shared company. The pending state is implicit and
            # resolves on the member's next poll.
            logger.info(
                "ensure_user_ready (org member): org=%s has no active "
                "paperclip company yet — waiting for an admin to "
                "bootstrap it; retried by the next attempt",
                org_id,
            )
            return
        await _ensure_paperclip_for_owner(owner_id=org_id, owner_user_id=user_id, owner_email=email)
        return

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

    CALLER: ``_ensure_paperclip_for_org``'s create branch only.

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
            before = await provisioning.get_workspace(owner_id)
            company = await provisioning.provision_org(
                owner_id=owner_id, owner_user_id=owner_user_id, owner_email=owner_email
            )
            from core.services.messaging import _STARTER_KEYS, ensure_default_room

            before_starters = getattr(before, "starter_agent_ids", None) if before else None
            company_starters = getattr(company, "starter_agent_ids", None)
            # A row that never became active is a first provision still in progress: a seed that failed
            # before any starter was made leaves status "failed" and NO starter map, which read as
            # neither fresh nor partial, so its retry skipped #general (GC Pilot, 2026-10-19).
            fresh_or_partial = (
                before is None
                or getattr(before, "status", None) != "active"
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
                    room_id = await ensure_default_room(owner_id, owner_user_id)
                except Exception:  # noqa: BLE001 — partial retries reconcile again
                    room_id = None
                    logger.exception("ensure_user_ready: default room creation failed for owner=%s", owner_id)
                try:
                    from core.services.messaging import ensure_feed_room

                    await ensure_feed_room(owner_id, owner_user_id)
                except Exception:  # noqa: BLE001 -- the Activity page's first read creates it
                    logger.exception("ensure_user_ready: feed room creation failed for owner=%s", owner_id)
                if room_id:
                    # The owner's welcome, from the CEO, now that #general exists. Once-only via
                    # send_to_owner's first-contact claim, so a re-run here sends nothing.
                    from core.services.welcome_email import send_owner_welcome

                    try:
                        await send_owner_welcome(owner_id, owner_user_id)
                    except Exception:  # noqa: BLE001 — a welcome must never fail provisioning
                        logger.exception("ensure_user_ready: owner welcome failed for owner=%s", owner_id)
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

    ``PaperclipProvisioning.find_org_owner`` returns the workspace row only
    when it is ``active``; None means the company is missing or not yet
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
