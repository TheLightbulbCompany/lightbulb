"""Unified provisioning entry point.

Single canonical helper called from exactly one site:
  - GET /container/status (routers/container.py)

The frontend polls /container/status every ~3s while the user is
in pre-ready state (ProvisioningStepper.tsx:250-252), so within
~3s of the readiness gate clearing, ensure_user_ready dispatches.
No webhook, REST endpoint, or service-layer call dispatches it
from anywhere else — see plan §"One trigger only" for rationale.

Clerk webhooks (org.created, membership.created) are pure
record-keeping; they do NOT call this helper.

ensure_user_ready is idempotent — every call is safe to repeat.
It never raises on "not ready yet" — that's a normal return.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from core.auth import AuthContext
from core.billing.metronome_provisioning import ensure_metronome_customer
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.repositories import billing_repo
from core.services import clerk_admin
from core.services.container_provision import background_provision
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.provision_gate import evaluate_provision_gate

logger = logging.getLogger(__name__)

# Per-org in-process lock serializing the create-vs-join decision so two
# concurrent first-pollers in an all-admin org don't each create a separate
# Paperclip company. Paperclip ignores the Idempotency-Key header
# (see paperclip_admin_client.py module docstring), so dedup must happen
# here. Single-backend-instance scope: if the service scales to N tasks the
# window reopens across tasks; acceptable for current fleet size — the loser
# of a cross-instance race resolves to the join path on its next poll.
_org_provision_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def ensure_personal_trial_default(auth: AuthContext, owner_id: str) -> None:
    """Auto-default a brand-new PERSONAL owner into a card-less ``trial_local``
    billing row (idempotent conditional put — no-op if any row already exists).

    The free tier is PERSONAL-ONLY: orgs get no card-less trial (the master
    plan refuses org owners; Teams billing is per-seat / no-trial), so
    ``not auth.is_org_context`` excludes every org context, admin included.

    Called from BOTH ``ensure_user_ready`` (before its gate) AND
    ``GET /container/status`` (before ITS gate). Each evaluates the provision
    gate independently, and the row must exist before whichever gate runs
    first — otherwise the gate sees no billing row, returns ``signup_required``,
    and the caller short-circuits (``/container/status`` 404s to the
    ProviderPicker) before the trial is ever created. Keeping the personal-only
    predicate in one place stops the two gate sites from drifting.

    Enforces the SAME tenancy invariant (Gate B) as ``/billing/trial-checkout``:
    a user with a pending org invitation may not take a personal plan — they
    must accept the invite and become a seat. Without it, this auto-default
    would be a way around that gate: the invited user need only let the
    ``/container/status`` poll run to land a personal ``trial_local`` row and a
    personal container. The frontend normally routes them to the invitation
    screen, but a direct poll or a routing race must not be able to violate a
    backend invariant.
    """
    if auth.is_org_context:
        return

    # Short-circuit on an existing row BEFORE the Clerk call. This helper runs on
    # every ~3s /container/status poll; the invitation lookup is a Clerk API call
    # and must not run on each one. Once the row exists there is nothing to
    # create, so the only callers that reach Clerk are those at the actual signup
    # moment (no billing row yet) — and an invited user gets routed to the
    # invitation screen rather than left polling.
    if await billing_repo.get_by_owner_id(owner_id):
        return

    pending = await clerk_admin.list_pending_invitations_for_user(auth.user_id)
    if pending:
        logger.info(
            "user_provision: personal trial default refused for user=%s — pending org invitation",
            auth.user_id,
        )
        return

    created = await billing_repo.create_trial_local_if_absent(
        owner_id=owner_id,
        owner_type="personal",
    )
    if created:
        # Only the call that actually wrote the row fires this — every
        # later idempotent no-op (each ~3s poll) must not re-fire it.
        capture_product_event(distinct_id=owner_id, event="trial_started")


async def ensure_user_ready(auth: AuthContext, *, provision_container: bool = True) -> None:
    """Single canonical readiness entry point. Idempotent.

    Flow:
      1. Resolve readiness gate via evaluate_provision_gate. If a gate
         is up, return — pre-conditions not satisfied. (No exception:
         this is a normal "not ready yet" outcome.)
      2. Paperclip workspace (role-blind for org context):
           - Org context: create the org's ONE shared company if it
             doesn't exist yet, otherwise join it. The decision is
             company existence, never Clerk role.
           - Personal: always sole owner of their own company.
      3. Container (ECS) provisioning is a SEPARATE concern. Dispatched
         only when the caller is a container-owner (personal user OR org
         admin) AND provision_container is True. The /container/status
         running-container poll path passes provision_container=False
         because provision_user_container force-restarts an already-running
         service — calling it on every ~3s poll would restart the box.
    """
    owner_id = auth.org_id if auth.is_org_context else auth.user_id

    # Auto-default a brand-new personal owner into a card-less trial_local
    # BEFORE the gate is evaluated, so the gate (which treats trial_local as
    # active + provision-eligible) returns None below and the normal
    # Paperclip-seed + background_provision flow runs. Personal-only + the
    # rationale for the shared helper live in its docstring; /container/status
    # calls the SAME helper before its own gate.
    await ensure_personal_trial_default(auth, owner_id)

    # The gate check uses the same predicate as /container/status so
    # the helper and the endpoint can never disagree.
    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        is_admin=auth.is_org_admin if auth.is_org_context else True,
    )
    if gate is not None:
        logger.debug(
            "ensure_user_ready: gate up for owner=%s code=%s — no-op",
            owner_id,
            gate.code,
        )
        return

    # Metronome customer lifecycle rides the owner path: once the gate
    # clears, the billing row (Stripe customer + active subscription) exists,
    # which is all ensure_metronome_customer needs. Best-effort — a Metronome
    # outage must never block provisioning. The short-circuit keys on the
    # persisted metronome_customer_id, so the next /container/status poll
    # simply retries until it lands. Org members never run this: the org's
    # Metronome customer belongs to the org owner_id and an admin poll
    # creates it.
    if not auth.is_org_context or auth.is_org_admin:
        try:
            await ensure_metronome_customer(owner_id=owner_id)
        except Exception:
            put_metric("metronome.provision.error")
            logger.exception(
                "Metronome customer provisioning failed for owner %s — will retry on next status poll",
                owner_id,
            )

    if auth.is_org_context:
        # JOIN is role-blind — a co-admin (or member) of an
        # already-provisioned org joins the org's ONE shared company, never
        # creates a second one. CREATE is gated to admins: bootstrapping the
        # org's shared company mirrors who bootstraps the shared container
        # (admins; members ride). A member polling a not-yet-bootstrapped
        # org waits for an admin — avoids a company seeded with Friday/Atlas
        # whose adapters point at a container no member can provision yet.
        await _ensure_paperclip_for_org(
            org_id=auth.org_id,
            user_id=auth.user_id,
            email=auth.email or "",
            is_admin=auth.is_org_admin,
        )
    else:
        # Personal owner: always the sole owner of their own company.
        await _ensure_paperclip_for_owner(
            owner_id=owner_id,
            owner_user_id=auth.user_id,
            owner_email=auth.email or "",
            org_id=None,
        )

    # Container (ECS) provisioning is a SEPARATE concern from the Paperclip
    # workspace above. Dispatch it only when:
    #   (a) the caller is a container-owner — personal user or org ADMIN.
    #       Org members ride the shared org container; they never spin one
    #       up. (This role check is about WHO SPINS UP THE BOX, which is
    #       unrelated to Teams/Paperclip access — that is role-blind above.)
    #   (b) provision_container is True. The /container/status running-
    #       container branch passes False, because provision_user_container
    #       force-restarts an already-running service (ecs_manager docstring
    #       scenario 3) — calling it every ~3s poll would restart the box.
    # background_provision is idempotent on the absent/0-desired cases, so
    # repeated owner polls on a not-yet-running container are safe.
    if provision_container and (not auth.is_org_context or auth.is_org_admin):
        asyncio.create_task(background_provision(owner_id, auth.user_id))


async def _ensure_paperclip_for_org(
    *,
    org_id: str,
    user_id: str,
    email: str,
    is_admin: bool,
) -> None:
    """Ensure this user has a row in the org's shared Paperclip company.

    Decided by company existence:
      - Company exists → JOIN it (provision_member), regardless of role.
        This is the role-blind fix: admins and members alike join the one
        shared company.
      - No active company yet → only an ADMIN creates it (provision_org),
        under a per-org lock so concurrent first-pollers can't
        double-create. A member returns gracefully and waits — an admin
        will bootstrap the company (and the container it rides) on their
        poll, and the member joins on a subsequent poll. Letting a member
        create here would seed Friday/Atlas pointing at a container members
        can't provision, leaving a company-without-container window.

    The underlying helpers treat a duplicate-409 as idempotent success, so
    this is safe to call on every /container/status poll.
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
                        "bootstrap it; will retry on next /container/status poll",
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
                if not await provisioning.try_acquire_org_create_claim(org_id, holder=user_id):
                    logger.info(
                        "ensure_user_ready (org): org=%s create claim held by "
                        "another backend instance — waiting; will join on the "
                        "next /container/status poll",
                        org_id,
                    )
                    return

                # We won the claim: become the org's company owner-of-record.
                try:
                    await _ensure_paperclip_for_owner(
                        owner_id=org_id,
                        owner_user_id=user_id,
                        owner_email=email,
                        org_id=org_id,
                    )
                except BaseException:
                    # Release the claim so the next poll (this instance or
                    # another) can re-acquire and retry.
                    await provisioning.release_org_create_claim(org_id, holder=user_id)
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
                # an active row — and since rows are keyed per-user, a user who
                # was provisioned in a PERSONAL context (row.org_id == their
                # user_id) would make this read return that unrelated active row
                # even though no org-indexed company exists. Checking org_id
                # keeps us from holding the claim on a personal row while
                # find_org_owner(org_id) stays None.
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
                    await provisioning.release_org_create_claim(org_id, holder=user_id)
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
    org_id: str | None,
) -> None:
    """Ensure a Paperclip company exists for this owner.

    Idempotent — ``provisioning.provision_org`` short-circuits on an
    existing active row (see ``core/services/paperclip_provisioning.py``
    ``provision_org`` docstring).

    Resolves ``owner_email`` via ``lookup_owner_email`` when not
    supplied on the AuthContext (e.g. retry callers without a JWT
    email claim). The caller (``ensure_user_ready``) is the source
    of truth for org-vs-personal context via ``auth.is_org_context``
    and forwards ``org_id`` here explicitly — None for personal
    owners, the Clerk org_id for org admins.
    """
    if not owner_email:
        owner_email = (
            await lookup_owner_email(
                org_id=org_id,
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
            await provisioning.provision_org(
                owner_id=owner_id,
                owner_user_id=owner_user_id,
                owner_email=owner_email,
            )
        except PaperclipApiError as exc:
            # Duplicate-owner 409: race condition — the previous
            # /container/status poll already kicked off a provision_org
            # for this owner that hasn't yet written the
            # ``status="active"`` DDB row. /container/status is the
            # canonical poll trigger; provision_org may fire on every
            # ~3s poll until ECS creates the container row, so duplicate
            # calls must no-op. Mirrors the member-path pattern in
            # ``_ensure_paperclip_for_member``.
            if exc.status_code == 409:
                logger.info(
                    "ensure_user_ready (owner): %s already in paperclip — no-op (sign_up_user 409)",
                    owner_id,
                )
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

    Returns gracefully if the owner's company row doesn't exist or
    isn't active yet. The pending state is implicit ("Clerk-membership
    exists, paperclip-membership doesn't") and resolves on the
    member's next /container/status poll once the owner finishes
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

    Idempotent on duplicate-add: ``provision_member`` calls
    ``sign_up_user`` for the joining member; on a duplicate
    (member already in Paperclip from a previous successful add)
    Paperclip's Better Auth returns 409 and ``sign_up_user`` raises
    ``PaperclipApiError(status_code=409)``. /container/status is the
    canonical poll trigger and may fire this helper on every member
    poll, so duplicate calls must no-op. We catch the 409 and return —
    other PaperclipApiError statuses re-raise.
    """
    provisioning = await build_paperclip_provisioning()
    try:
        owner_company = await provisioning.find_org_owner(org_id)
        if owner_company is None:
            logger.info(
                "ensure_user_ready (member): owner's paperclip company "
                "for org=%s not active yet — skipping; will retry on "
                "member's next /container/status poll",
                org_id,
            )
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

        try:
            await provisioning.provision_member(
                owner_id=org_id,
                user_id=member_user_id,
                email=member_email,
                owner_email=owner_email,
            )
        except PaperclipApiError as exc:
            # Duplicate-member 409: the member was already added in a
            # prior successful run. /container/status is the canonical
            # poll trigger and may fire this on every member poll, so
            # treat as idempotent success. Any other status re-raises.
            if exc.status_code == 409:
                logger.debug(
                    "ensure_user_ready (member): %s already in paperclip company for org=%s — no-op (sign_up_user 409)",
                    member_user_id,
                    org_id,
                )
            else:
                raise
    finally:
        await close_paperclip_provisioning(provisioning)
