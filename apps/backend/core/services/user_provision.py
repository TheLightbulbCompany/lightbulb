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
from core.services.agent_routine_provisioning import (
    NIGHTLY_CEO_ROUTINE_CRON,
    NIGHTLY_CEO_ROUTINE_DESCRIPTION,
    NIGHTLY_CEO_ROUTINE_TITLE,
    create_enabled_schedule_routine,
)
from core.services.container_provision import background_provision
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.provision_gate import evaluate_provision_gate, has_always_on_entitlement

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

    # Paperclip (Teams) seed is a SEPARATE concern from the container (ECS)
    # dispatch below. A seed failure must NOT abort provisioning: the box
    # still boots and serves chat because ecs_manager falls back to the legacy
    # `main` agent when no Paperclip company exists. The failure is recorded on
    # the paperclip-companies row (by _mark_failed) and surfaced via
    # /container/status; here we only guarantee it never wedges the container.
    # Best-effort — mirrors the ensure_metronome_customer guard above; the next
    # /container/status poll retries the seed. Do NOT narrow the except: a seed
    # can fail with PaperclipApiError (non-409) OR any transient error, and all
    # of them must fall through to the dispatch rather than escape the poll.
    try:
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
    except Exception:
        put_metric("paperclip.seed.error")
        logger.exception(
            "Paperclip seed failed for owner %s — provisioning continues; Teams degraded",
            owner_id,
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

    # Nightly CEO loop (issue #1273): arm the paid, ENABLED nightly routine on
    # Friday for the container OWNER (personal owner or org admin) — never an
    # org member, never a trial owner (gated on has_always_on_entitlement
    # inside). Deliberately NOT gated on provision_container: it must also run
    # on the running-container poll (org path, provision_container=False) so
    # pre-existing paid owners and free→paid upgrades get armed on their next
    # /container/status poll — not only brand-new provisions. Idempotent +
    # best-effort (never raises).
    if not auth.is_org_context or auth.is_org_admin:
        await _maybe_arm_nightly_ceo(auth, owner_id)


async def _maybe_arm_nightly_ceo(auth: AuthContext, owner_id: str) -> None:
    """Idempotently arm the paid nightly CEO routine on Friday (issue #1273).

    Paid-only: ``has_always_on_entitlement`` returns True only for Stripe
    ``active``/``trialing`` (or the legacy None+stripe_sub row) and fail-closes
    on no account — a ``trial_local`` owner is NEVER armed. Best-effort: any
    failure is logged and swallowed so ``ensure_user_ready`` keeps answering the
    poll; the next poll retries.

    Idempotency, three race classes closed:
      - Normal re-poll: the ``nightly_ceo_armed_company`` cache on the billing
        row short-circuits before any Paperclip call once it equals the current
        company.
      - Concurrency: ``try_claim_nightly_arm`` is a conditional write that
        elects exactly one winner to seed (plus ``create_enabled_schedule_routine``'s
        title-skip as a backstop).
      - Owner opt-out: ``nightly_ceo_opted_out`` (durable — survives a full
        reprovision because billing is preserved) blocks re-seeding, and the
        title-skip never reconciles an existing paused trigger.
    """
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        if not has_always_on_entitlement(account):
            return
        if account.get("nightly_ceo_opted_out"):
            return

        provisioning = await build_paperclip_provisioning()
        try:
            # Confirm a COMMITTED active company before seeding. This is why
            # there is no orphan/failed-company cost leak: we only ever arm a
            # company provision_org actually finished (status="active").
            row = (
                await provisioning.find_org_owner(owner_id)
                if auth.is_org_context
                else await provisioning.get_company_row(auth.user_id)
            )
            if row is None or row.status != "active":
                return
            company_id = row.company_id
            if account.get("nightly_ceo_armed_company") == company_id:
                return  # cheap cache hit — already armed for this company
            # Friday is seeded FIRST (seed order == identity), so it's element 0.
            friday_id = (row.seeded_agent_ids or [None])[0]
            if not friday_id:
                return  # Friday not seeded yet — retry on a later poll
            if not await billing_repo.try_claim_nightly_arm(owner_id, company_id):
                return  # a concurrent poll won the claim
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
                )
                # Success (created OR title-skip: the routine is present either
                # way) — leave the claim as the armed cache.
            except Exception:
                # Seed failed after we won the claim — release it so the next
                # poll retries, then let the outer handler log.
                await billing_repo.release_nightly_arm(owner_id, company_id)
                raise
        finally:
            await close_paperclip_provisioning(provisioning)
    except Exception:
        logger.exception("nightly CEO arm failed (non-fatal) owner=%s", owner_id)


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

    The underlying helpers treat a concurrent-create 409 as idempotent
    success and a duplicate-email 422 (Better Auth) as a recorded failure —
    neither re-raises — so this is safe to call on every /container/status poll.
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
            # 409: a CONCURRENT provision_org (the previous ~3s
            # /container/status poll) is mid-flight and hasn't written the
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
                # own _mark_failed records the failed row, surfaced via
                # /container/status. We swallow the exception here (a duplicate is
                # an EXPECTED terminal outcome, not a transient error worth
                # re-raising on every poll) but record it as a FAILURE — never a
                # success no-op, so nothing reports Teams as ready over a broken
                # workspace. Full orphan RECOVERY (adopt/reset the Better Auth
                # user) needs a Paperclip user-delete the platform doesn't
                # expose — out of scope for this fix.
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
            # 409: a concurrent member-join is mid-flight (or the member was
            # already added by a prior successful run). /container/status is the
            # canonical poll trigger and may fire this on every member poll, so
            # treat as an idempotent no-op success.
            if exc.status_code == 409:
                logger.debug(
                    "ensure_user_ready (member): %s already in paperclip company for org=%s — no-op (409)",
                    member_user_id,
                    org_id,
                )
            elif exc.status_code == 422:
                # Better Auth returns 422 (NOT 409) for a duplicate email — same
                # recorded-failure semantics as the owner guard: the account
                # already exists but the member could not be (re)provisioned. NOT
                # success; provision_member's own _mark_failed records the row,
                # surfaced via /container/status. Swallow (don't re-raise on every
                # poll) but record it as a FAILURE, never a success no-op.
                logger.warning(
                    "ensure_user_ready (member): %s paperclip duplicate-email 422 for org=%s — "
                    "recorded failure; Teams degraded",
                    member_user_id,
                    org_id,
                )
                put_metric("paperclip.seed.duplicate_email", dimensions={"path": "member"})
            else:
                raise
    finally:
        await close_paperclip_provisioning(provisioning)
