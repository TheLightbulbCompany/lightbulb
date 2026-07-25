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
from core.repositories import billing_repo, container_repo
from core.services import clerk_admin, wake_policy
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


async def ensure_user_ready(auth: AuthContext) -> None:
    """Single canonical readiness entry point. Idempotent.

    Flow:
      1. Resolve readiness gate via evaluate_provision_gate. If a gate
         is up, return — pre-conditions not satisfied. (No exception:
         this is a normal "not ready yet" outcome.)
      2. Paperclip workspace JOIN (org context; role-blind). The org's
         ONE shared company is normally CREATED by the provisioning
         executor's runner (scripts.provision_runner step 0), not here.
      3. Container (ECS) provisioning + the owner company-CREATE, for
         container-owners only (personal user OR org admin), via
         ``_provision_or_reconcile`` — which reads the container row and
         decides between "dispatch the executor" and "seed here",
         because those are the same job routed by live state. See its
         docstring; there is no caller-supplied flag any more (the old
         ``provision_container`` kwarg made every caller re-derive that
         decision, and got it wrong for the seed half — a self-healing
         poll on a running container seeded nothing).
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
        # A provision gate speaks to CONTAINER-provisioning (and company-
        # CREATE) economics; it must NOT swallow an org-context caller's
        # idempotent membership-add to an already-active shared company.
        # Root cause of the silent orphan-member incident: an org whose
        # Bedrock credits hit zero fires ``credits_required`` on every
        # poll, and this early return used to no-op the membership-add at
        # DEBUG level forever — the joining member could reach the running
        # shared container (which made them LOOK joined) while every Teams
        # surface 401'd on the missing Paperclip membership, with zero
        # log lines carrying their user id. Join-only by construction:
        # ``_ensure_paperclip_for_member`` never creates a company (it
        # returns gracefully when no ACTIVE company exists), so company
        # CREATE stays behind the gate — an admin bootstrapping a brand-new
        # org still needs the gate clear.
        if auth.is_org_context:
            try:
                await _ensure_paperclip_for_member(
                    org_id=auth.org_id,
                    member_user_id=auth.user_id,
                    member_email=auth.email or "",
                )
            except Exception:
                put_metric("paperclip.seed.error")
                logger.exception(
                    "ensure_user_ready: paperclip membership-add failed for "
                    "owner=%s user=%s (gate=%s) — will retry on next poll",
                    owner_id,
                    auth.user_id,
                    gate.code,
                )
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
    # dispatch below. A seed/join failure must NOT abort provisioning: the box
    # still boots and serves chat because ecs_manager falls back to the legacy
    # `main` agent when no Paperclip company exists. The failure is recorded on
    # the paperclip-companies row (by _mark_failed) and surfaced via
    # /container/status; here we only guarantee it never wedges the container.
    # Best-effort — mirrors the ensure_metronome_customer guard above; the next
    # /container/status poll retries. Do NOT narrow the except: it can fail with
    # PaperclipApiError (non-409) OR any transient error, and all of them must
    # fall through to the dispatch rather than escape the poll.
    #
    # The org member JOIN (_ensure_paperclip_for_member) lives here: a seat-add
    # is NOT a company-creation race, so it never needed the executor's
    # single-flight — co-admins/members join the shared company the runner
    # creates, and the creator's own re-poll no-ops on their already-active
    # owner row. The owner company-CREATE is _provision_or_reconcile's job
    # below. Personal owners have no join, so nothing runs in this block for
    # them.
    try:
        if auth.is_org_context:
            await _ensure_paperclip_for_member(
                org_id=auth.org_id,
                member_user_id=auth.user_id,
                member_email=auth.email or "",
            )
    except Exception:
        put_metric("paperclip.seed.error")
        logger.exception(
            "Paperclip seed failed for owner %s — provisioning continues; Teams degraded",
            owner_id,
        )

    # Container + owner-seed, for container-owners only — personal user or org
    # ADMIN. Org members ride the shared org container; they never spin one up.
    # (This role check is about WHO SPINS UP THE BOX and is unrelated to
    # Teams/Paperclip access, which is role-blind above.)
    if not auth.is_org_context or auth.is_org_admin:
        await _provision_or_reconcile(auth, owner_id)

    # Nightly CEO loop (issue #1273): arm the paid, ENABLED nightly routine on
    # Friday for the container OWNER (personal owner or org admin) — never an
    # org member, never a trial owner (gated on has_always_on_entitlement
    # inside). Runs on EVERY owner poll, including the running-container one,
    # so pre-existing paid owners and free→paid upgrades get armed on their
    # next /container/status poll — not only brand-new provisions. Idempotent +
    # best-effort (never raises).
    if not auth.is_org_context or auth.is_org_admin:
        await _maybe_arm_nightly_ceo(auth, owner_id)


async def _provision_or_reconcile(auth: AuthContext, owner_id: str) -> None:
    """Container-owner half of ``ensure_user_ready``: dispatch the provisioning
    executor, or — when no provision can run — reconcile the Paperclip seed
    here instead.

    ONE decision, made from live container state rather than from a
    caller-supplied flag, because the two halves are not independent:

    * **Row absent / error / stopped / stale-provisioning → DISPATCH.** The
      executor's runner does the seed itself (step 0) under
      ``try_claim_provision``'s one-execution-per-owner election, which is what
      makes the duplicate-company race (#1442 — a load-balanced poll fan-out
      double-running ``provision_org``) structurally impossible for a first
      provision. Seeding here as well would put that race straight back.
    * **Row already ``running`` → NO dispatch, seed HERE.**
      ``try_claim_provision`` refuses a ``running`` row by design (see its
      docstring: "NOT claimable: running"), so a dispatch would only lose the
      claim and stand down — no execution, and therefore no step 0. The seed
      still has work to do in exactly this state (a ``failed`` Teams row on a
      healthy box; a legacy container that never had a company), so it runs on
      this thread. No new race: an owner with a RUNNING container has already
      been through a provision, so this path never competes with the executor
      for a first CREATE.
    * **Deliberately parked trial box → NO dispatch, seed HERE.** Both passive
      -read doors (``/container/status``'s auto-retry and the ``/teams``
      reconcile) must consult ``wake_policy`` and never wake a parked trial box
      — see that module's docstring. ``ensure_user_ready`` is only ever called
      from those passive doors (explicit intent goes through
      ``container_wake.request_wake`` / ``request_wake_silent``), so the
      suppression belongs here, once, instead of being re-derived per caller.
      Paperclip seeding needs no container, so the seed still runs.

    The extra containers-row read is the price of making the decision from
    state; ``ensure_user_ready`` always runs off the request path (a background
    task at every call site), so it costs no response latency.
    """
    row = await container_repo.get_by_owner_id(owner_id)
    if await wake_policy.is_parked_trial(owner_id, row):
        wake_policy.note_wake_suppressed(owner_id, row, door="ensure_user_ready", reason="gated")
    elif (row or {}).get("status") != "running":
        # background_provision is idempotent on the absent/0-desired cases, so
        # repeated owner polls on a not-yet-running container are safe: the
        # atomic claim collapses them to one execution.
        asyncio.create_task(background_provision(owner_id, auth.user_id))
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
    """Run the OWNER company seed on the poll thread, for the one case the
    executor's step 0 cannot cover: no provision is going to run at all.

    Personal owner → ``_ensure_paperclip_for_owner``: idempotent
    (``provision_org`` short-circuits on an active row) and genuinely
    reconciling on a ``failed`` one, which is the state this path exists for.

    Org admin → ``_ensure_paperclip_for_org``, but only when the org has NO
    active company. The JOIN half already ran on this thread
    (``_ensure_paperclip_for_member``, role-blind, above); calling the
    join-or-create helper unconditionally would re-issue that member-add — a
    Paperclip round trip + a Clerk email lookup — on every ~3s poll of a
    healthy org. What is left to reconcile here is CREATE only.
    """
    if auth.is_org_context:
        if not auth.is_org_admin:
            return  # members never create; they joined above
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
            is_admin=auth.is_org_admin,
        )
        return

    await _ensure_paperclip_for_owner(
        owner_id=owner_id,
        owner_user_id=auth.user_id,
        owner_email=auth.email or "",
        org_id=None,
    )


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

    CALLERS: ``scripts.provision_runner._seed_owner_paperclip`` (step 0 of the
    provisioning executor's runner — the normal path, passing the Clerk org
    CREATOR as ``user_id`` with ``is_admin=True``), and
    ``_reconcile_owner_seed`` for the one state the executor never reaches (an
    org whose container is already running or parked, so no provision — and
    therefore no step 0 — will run). A FIRST provision always goes through the
    executor, where try_claim_provision elects one execution per owner, so the
    duplicate-company race (#1442) stays closed on the path that had it. The
    per-org asyncio lock and ``try_acquire_org_create_claim`` below are KEPT as
    defense-in-depth for the reconcile path (and for a bare/local runner
    invocation outside the state machine).

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

    CALLERS: ``scripts.provision_runner._seed_owner_paperclip`` (step 0,
    personal owners — the normal path, where the company CREATE inherits the
    executor's one-runner-per-owner election), ``_ensure_paperclip_for_org``'s
    create branch, and ``_reconcile_owner_seed`` when no provision will run at
    all (container already running, or a deliberately parked trial box).

    Resolves ``owner_email`` via ``lookup_owner_email`` when not
    supplied by the caller (e.g. a runner invocation with no JWT
    email claim). The caller is the source of truth for
    org-vs-personal context and forwards ``org_id`` here explicitly —
    None for personal owners, the Clerk org_id for orgs.
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

    Two DISTINCT duplicate outcomes, same request body, opposite verdicts —
    ``/container/status`` is the canonical poll trigger and may fire this helper
    on every member poll, so both must be handled:

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
                "user=%s; will retry on member's next /container/status poll",
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
            # already added by a prior successful run). /container/status is the
            # canonical poll trigger and may fire this on every member poll, so
            # treat as an idempotent no-op success.
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
