"""Orchestrator for provisioning Paperclip on Clerk webhook events.

Three entry points (all async, all idempotent on user_id / owner_id):

* ``provision_org`` — for ``organization.created``. Creates the Paperclip
  company AND the org owner's Better Auth account in a single
  end-to-end chain (sign_up_user -> create_company -> mint service token
  -> seed the required agent roster -> persist row). This is the only path that calls
  ``create_company``.

* ``provision_member`` — for ``organizationMembership.created``. Adds a
  new Isol8 member to the org's existing Paperclip company via the
  full Better Auth + invite-flow chain:

      member_signUp ->
      owner_signIn (using stored owner password) ->
      owner.create_invite ->
      member.accept_invite ->
      owner.approve_join_request ->
      mint member service-token ->
      persist member row.

  Requires the caller (T12 webhook handler) to pass ``owner_email`` —
  Clerk's ``organizationMembership.created`` payload includes the org
  owner's email anyway, so we keep the orchestrator self-contained
  rather than depending on user_repo.

* ``disable`` — for cancellation/deletion. Marks the row
  ``status="disabled"`` with ``scheduled_purge_at = now + grace_days``;
  the company itself is left alive (other org members still have
  access). The ``purge`` cron entrypoint hard-deletes after the grace
  window.

Failure semantics: a failure mid-provisioning calls ``_mark_failed``, which
leaves exactly ONE durable ``status="failed"`` marker. When a row already
exists it downgrades that row in place (preserving paperclip_user_id +
password so a retry can ADOPT), guarded so it never clobbers a row a
concurrent attempt completed to ``active``. When NO row exists yet it writes
a durable marker via a CONDITIONAL put-if-absent (carrying NO fabricated
paperclip_user_id — a zeroed id poisoned the row — and never overwriting a
real row): this keeps the net-new / orphan-422 failure honestly visible
(``/teams`` → 503, ``teams_status="failed"``) instead of an eternal
provisioning spinner. Then it re-raises. Subsequent retries with the same
args are safe — the ``status="active"`` short-circuit is the only path that
skips work.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import httpx
from botocore.exceptions import ClientError
from ulid import ULID

from core.auth import is_org_owner_id
from core.config import settings
from core.encryption import decrypt, encrypt
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import service_token
from core.services.company_identity import generate_company_name
from core.services.paperclip_adapter_config import (
    HERMES_GATEWAY_TYPE,
    build_hermes_gateway_adapter_config,
)
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError, _paperclip_error_code

logger = logging.getLogger(__name__)

STARTER_ROSTER = (
    ("ceo", "CEO", "ceo", "CEO", None),
    ("engineering", "Engineering", "engineer", "Engineering", "ceo"),
    ("finance", "Finance", "cfo", "Finance", "ceo"),
    ("growth", "Growth", "cmo", "Growth", "ceo"),
    ("analytics", "Analytics", "researcher", "Analytics", "ceo"),
    ("customer_operations", "Customer Operations", "general", "Customer Operations", "ceo"),
)

#: The starter roles, in seed order. ``messaging``/``user_provision`` check a company's
#: ``starter_agent_ids`` against exactly this set.
STARTER_ROLE_KEYS = tuple(row[0] for row in STARTER_ROSTER)

#: Each starter's persona, sent as Paperclip ``capabilities`` on create. ``hermes_profiles.build_soul_md``
#: renders it into the agent's SOUL.md under "## What you do" -- without it that file is one line.
#: Keyed by role key (not a sixth tuple element) so every ``STARTER_ROSTER`` unpacking stays as is.
STARTER_CAPABILITIES = {
    "ceo": (
        "Sets direction, splits work across the team, reports back."
        "\n\n"
        "Owns the company's goals and the plan. When the owner asks for something, first restate the "
        "outcome in one line, then decide who does it: do it yourself only if it's a decision or a "
        "summary, otherwise create a task for the right specialist and tell the owner who has it. Keeps "
        "BRIEF.md and the goals current, checks on stalled tasks, and posts a short progress note when "
        "work lands. Tools when connected: Google Workspace or Microsoft 365 (calendar, mail, docs), "
        "Notion, Linear."
    ),
    "engineering": (
        "Builds and fixes product and site; owns code, deploys and technical debt."
        "\n\n"
        "Owns anything that ships as code: the product, the website, integrations, scripts. First step "
        "on any request: find the repo or site, read what exists, and propose the smallest change before"
        " building. Works in branches and PRs, never pushes untested changes, and reports what shipped "
        "and how to verify it. Tools when connected: GitHub, Vercel, Sentry, Cloudflare, AWS. Hands "
        "revenue/pricing questions to Finance, instrumentation to Analytics."
    ),
    "finance": (
        "Tracks money in and out; owns revenue, costs, pricing and runway."
        "\n\n"
        "Owns the numbers that cost or make money. First step: pull the current picture (revenue, MRR, "
        "refunds, largest costs) before giving an opinion. Answers with figures and their source, flags "
        "anything unusual (failed payments, churn spikes, surprise spend), and drafts pricing or budget "
        "changes for the owner to approve, never executing a payment itself. Tools when connected: "
        "Stripe, QuickBooks. Hands growth spend questions to Growth, metric definitions to Analytics."
    ),
    "growth": (
        "Finds and wins customers; owns marketing, content, outreach and campaigns."
        "\n\n"
        "Owns getting attention and turning it into customers. First step: ask who the customer is and "
        "where they already are, then propose one experiment with a success number before producing "
        "anything. Writes posts, landing copy, emails and outreach lists; runs campaigns only with the "
        "owner's approval. Tools when connected: X, Instagram, Meta Ads, Apollo, Attio, Google "
        "Workspace. Hands results to Analytics to measure, site changes to Engineering."
    ),
    "analytics": (
        "Measures what's happening; owns metrics, dashboards, research and competitor analysis."
        "\n\n"
        'Owns answering "is it working?" with evidence. First step: state the question as a metric and '
        "name the data source, then report the number, the trend, and one sentence on what it means. "
        "Also does market and competitor research with sources cited. Tools when connected: PostHog, "
        "Stripe (read), Parallel web research, Google Sheets. Hands missing tracking to Engineering, "
        "findings that change the plan to the CEO."
    ),
    "customer_operations": (
        "Looks after customers; owns support, onboarding, feedback and retention."
        "\n\n"
        "Owns every customer conversation after the sale. First step on a ticket: read the customer's "
        "history, answer or fix what can be done now, and escalate what can't with the context attached."
        " Turns repeated questions into help docs, collects feedback into themes for the CEO, and flags "
        "churn risks early. Tools when connected: Intercom, Google Workspace or Microsoft 365 mail, "
        "Attio, Linear (bug reports). Hands bugs to Engineering, refunds to Finance."
    ),
}


# The onboarding attribute names carried on ``PaperclipCompany`` -- the exact
# set of ``PaperclipCompany`` fields listed under "Onboarding" in
# ``core/repositories/paperclip_repo.py``. Kept as a tuple (not re-derived
# via dataclasses.fields()) so it stays an explicit, reviewable list: every
# whole-item ``repo.put`` writer-safety fix in this module reads this same
# set, and drift between "what the dataclass carries" and "what a put
# preserves" is exactly the bug class this guard exists to close.
_ONBOARDING_FIELD_NAMES = (
    "onboarding_generation",
    "funnel_state",
    "funnel_submitted_at",
    "funnel_completed_at",
    "onboarding_goal_id",
    "onboarding_project_id",
)


# Loser-side wait for a concurrent seed (the ``try_acquire_seed_claim``
# fence in ``provision_org``). ~6s of 1s polls covers the typical 2-3s
# winner chain; on timeout the loser raises the same 409-shaped in-flight
# error ``_ensure_paperclip_for_owner`` already treats as a benign no-op,
# and the next /container/status poll adopts the winner's active row.
# Module-level (not kwargs) so tests can shrink the interval without
# threading knobs through provision_org's signature.
_SEED_WAIT_ATTEMPTS = 6
_SEED_WAIT_INTERVAL_S = 1.0


def _is_credential_less_failed(existing: Optional[PaperclipCompany]) -> bool:
    """A ``failed`` row with no confirmed ``paperclip_user_id``. Signup may
    already have succeeded; any staged password is retained for recovery.
    Its retry must go through the seed-claim fence like a fresh row;
    a failed row WITH a confirmed identity is adopted and needs no fence
    (``try_acquire_seed_claim`` reclaims exactly this shape)."""
    return existing is not None and existing.status == "failed" and not existing.paperclip_user_id


def _carry_forward_onboarding_fields(existing: Optional[PaperclipCompany]) -> dict:
    """Read-merge helper for every whole-item ``repo.put`` in this module.

    ``PaperclipRepo.put`` is a raw ``PutItem`` — any onboarding field not
    set on the ``PaperclipCompany`` passed to it is simply ABSENT from the
    new item, silently wiping a previously-written value. Every put site in
    this module that is NOT the deliberate G2 mint (provision_org's
    fresh-create branch) must carry the row's existing onboarding fields
    forward through this helper, or reconcile/adopt/re-point writes would
    reintroduce the exact clobber the co-located design's "load-bearing
    safety requirement" exists to prevent.

    Returns ``{}`` (no onboarding kwargs at all) when ``existing`` is None —
    there is nothing to preserve, and ``PaperclipCompany``'s onboarding
    fields already default to None.
    """
    if existing is None:
        return {}
    return {name: getattr(existing, name) for name in _ONBOARDING_FIELD_NAMES}


def _auth_email(existing: Optional[PaperclipCompany], live_email: str) -> str:
    """The address to sign in to Better Auth with.

    Better Auth's ``user.email`` is set at sign-up and never updated, so the
    live Clerk address stops being the sign-in truth the moment a human moves
    mailbox domains. Prefer the address recorded on the row; fall back to the
    live one only for rows written before ``paperclip_email`` existed (or not
    yet backfilled). NEVER the other way round — overwriting the stored value
    with a live Clerk address is what mints duplicate Better Auth accounts.
    """
    return (existing.paperclip_email if existing is not None else "") or live_email


async def build_paperclip_provisioning() -> "PaperclipProvisioning":
    """Build a PaperclipProvisioning for this request.

    Returns a ``PaperclipProvisioning`` instance. The underlying
    ``httpx.AsyncClient`` is attached as ``provisioning._http_client``
    so handlers can close it via ``aclose()`` after use — keeping the
    socket from leaking on production webhook traffic. T14/T15 will
    replace this per-request construction with a long-lived shared
    client tied to the FastAPI app lifespan.

    Public service-layer entry point — callers (webhook router,
    billing router, autoprovision, ensure_user_ready) construct a
    fresh provisioning chain per webhook invocation. The httpx client
    lifecycle is bounded by the request, so the connection pool
    doesn't need to be shared.
    """
    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    admin = PaperclipAdminClient(http_client=http)
    # Short name only — ``core.dynamodb.get_table`` prepends the env
    # prefix once (``lightbulb-{env}-``). Passing the fully-qualified name
    # would double-prefix at production deploy time and 404 on every lookup.
    repo = PaperclipRepo(table_name="paperclip-companies")
    provisioning = PaperclipProvisioning(admin, repo, env_name=settings.ENVIRONMENT)
    # Expose the underlying client for cleanup. Tests that patch
    # build_paperclip_provisioning return AsyncMock-shaped objects that
    # don't carry this attribute, so consumers must guard with getattr.
    provisioning._http_client = http  # type: ignore[attr-defined]
    return provisioning


async def close_paperclip_provisioning(provisioning) -> None:
    """Close the underlying httpx client if one was attached.

    AsyncMock-shaped objects (used in tests) won't have the attribute,
    so we no-op gracefully — keeps the production cleanup hot path
    working without breaking the test patches.
    """
    http = getattr(provisioning, "_http_client", None)
    if http is None:
        return
    try:
        await http.aclose()
    except Exception:
        logger.exception("paperclip httpx client close failed")


class OrgNotProvisionedError(RuntimeError):
    """Raised when ``provision_member`` is called but the org has no
    Paperclip company yet.

    We deliberately do NOT auto-fall-through to ``provision_org`` here
    because that would race with whichever webhook handler is also
    trying to provision the org. The caller (webhook router) is
    expected to dispatch ``organization.created`` before any
    ``organizationMembership.created`` event for the same org; if the
    order arrives reversed, the caller should retry.

    The ``retryable`` class attribute is a stable signal for T12's
    webhook handler: it can dispatch a retry without string-matching
    on the exception's type or message. This is always retryable —
    the org-create webhook is in flight and will land momentarily.
    """

    retryable: bool = True


class PaperclipProvisioning:
    """End-to-end provisioning chain for Paperclip companies + members.

    Consumes:

      * ``admin_client``: the typed httpx client wrapping Paperclip's
        REST API. Caller is responsible for its lifecycle (it shares
        the connection pool with the proxy router).
      * ``repo``: the async DynamoDB repo for paperclip-companies.
      * ``env_name``: the deploy-environment name used to compute the
        seeded agents' gateway URL (``dev``/``staging``/``prod``).
    """

    def __init__(
        self,
        admin_client: PaperclipAdminClient,
        repo: PaperclipRepo,
        env_name: str,
    ):
        self._admin = admin_client
        self._repo = repo
        self._env_name = env_name

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def provision_org(
        self,
        *,
        owner_id: str,
        owner_user_id: str,
        owner_email: str,
    ) -> PaperclipCompany:
        """Provision the Paperclip company + Better Auth user for a new owner.

        ``owner_id`` is the container's owner — Clerk's org_id for orgs
        and Clerk's user_id for personal owners. It's the same value
        every other backend layer keys on (``container_repo.get_by_owner_id``,
        ``_resolve_owner_id`` helpers, ``fetch_paperclip_agents_for_owner``).
        The DDB column on the persisted row is still called ``org_id`` —
        that's a Paperclip-side concept and we don't churn the storage
        layer for the rename.

        Paperclip's ``POST /api/companies`` requires
        ``req.actor.isInstanceAdmin`` in ``authenticated`` deployment mode
        (per ``paperclip/server/src/routes/companies.ts``). The
        old flow signed up the new user and immediately tried to create
        the company AS that user — which 403'd because the new user
        isn't an admin. Fixed flow: the bootstrap admin
        (``admin+paperclip@isol8.co`` in new envs, ``admin@isol8.co`` in
        legacy envs — email loaded dynamically from Secrets Manager,
        never hardcoded), the only ``instance_admin`` on the deployment,
        creates the company and invites the new user as a co-owner via
        the standard
        invite/accept/approve chain. New user ends up as an active
        owner; admin stays as a co-owner of every company (harmless
        side effect — admin doesn't act without explicit code paths).

        Service-token contract: every seeded agent's
        ``adapterConfig.authToken`` is a JWT whose ``sub`` claim MUST
        equal the container's ``owner_id``, because the WS Lambda
        authorizer uses ``sub`` to route the connection to that owner's
        OpenClaw container. We mint with ``owner_id`` directly so the
        token's ``sub`` matches the routing key for both org and
        personal owners.

        Idempotent — AND fenced across backend instances. The read-then-act
        idempotency below is not enough on its own: two concurrent calls on
        different backend tasks could both read "no row" and both run the
        full seed chain (duplicate same-email Better Auth users + duplicate
        companies, observed live 2026-07-24). So the fresh-create path first
        claims the row itself via ``repo.try_acquire_seed_claim`` (a
        strongly-consistent conditional put of a bare ``status="seeding"``
        row) BEFORE any Paperclip side effect; only the winner proceeds, and
        its later writes (the post-sign-up credential put, then the final
        active/failed put) replace that claim row in place. A loser waits
        briefly for the winner's active row and adopts it, or raises the
        same 409-shaped in-flight error the caller already tolerates (see
        ``_await_concurrent_seed``). A winner that fails mid-chain releases
        the claim via the ordinary ``_mark_failed`` in-place downgrade
        (``seeding`` -> ``failed``), and a winner that crashed hard before
        any durable progress leaves a bare ``seeding`` row that goes stale
        and is re-claimable after ~5 minutes.

        Idempotent: if a row already exists for ``owner_user_id`` with
        ``status="active"``, return it unchanged. A non-active row that
        already carries a ``company_id`` (a partial roster, including the
        legacy Friday-partial case) is
        RECONCILED — we adopt that company (sign in with the stored
        credential, skip the create + invite/accept/approve chain) and
        retry the seed against it, so a persistent seed outage never mints
        a new orphan company on each poll. A non-active row with NO
        company_id (a pre-create failure) redoes the full chain, adopting
        the user via sign-in — Paperclip's Better Auth sign-up is NOT
        idempotent, so ``_mark_failed`` preserves the credential to make
        that retry a sign-in rather than a re-422'ing sign-up.
        """
        existing = await self._repo.get(owner_user_id)
        # Idempotency must be org-scoped: rows are keyed per-user, so a user
        # provisioned in a different context (e.g. a personal company,
        # org_id == their user_id) would otherwise short-circuit here and
        # never get an org-indexed company. Only an active row for THIS
        # owner_id counts as "already provisioned."
        starter_incomplete = (
            existing is not None
            and existing.starter_agent_ids is not None
            and any(not existing.starter_agent_ids.get(starter_key) for starter_key, *_ in STARTER_ROSTER)
        )
        if (
            existing is not None
            and existing.status == "active"
            and existing.org_id == owner_id
            and not starter_incomplete
        ):
            logger.info(
                "paperclip_provisioning.provision_org: owner %s already active for owner_id %s",
                owner_user_id,
                owner_id,
            )
            return existing

        # Lazy import to keep the cycle clean (admin_session imports
        # PaperclipAdminClient, which would otherwise create an import
        # cycle if loaded at module level).
        from core.services.paperclip_admin_session import (
            get_admin_session_cookie,
            invalidate_admin_session,
        )

        # Acquire admin session up front so we fail loudly if the
        # bootstrap secret is missing (rather than failing 4 steps in).
        admin_cookie = await get_admin_session_cookie(self._admin._http)

        # ---- Cross-instance seed fence (fresh-create path) ----------------
        # Claim the row BEFORE the first Paperclip side effect (the sign-up
        # below). Fenced cases:
        #   * ``existing is None`` — the genuine fresh create. The claim's
        #     put-if-absent elects exactly one winner across backend
        #     instances; the loser adopts-or-defers in _await_concurrent_seed.
        #   * ``existing.status == "seeding"`` — a leftover bare claim. If it
        #     is a live peer's (non-stale), we are the loser; if its holder
        #     crashed hard before any durable progress (stale, ~5 min), we
        #     steal it and run the full chain ourselves.
        # A credential-less ``failed`` marker goes through the fence too: its
        # retry re-runs the sign-up, so concurrent retries would re-mint
        # duplicate accounts. Every OTHER existing state deliberately
        # bypasses the fence and keeps today's semantics: an active row
        # already short-circuited above, and the adopt (credential-bearing) /
        # reconcile (company_id-bearing) paths sign in rather than sign up,
        # so a concurrent retry there cannot duplicate an account.
        #
        # Placed AFTER the admin-cookie fetch on purpose: everything from
        # here on runs inside the try below, whose failure path downgrades
        # the claim row via _mark_failed (seeding -> failed) so a later poll
        # retries promptly instead of waiting out the stale window.
        # Set only on the fresh-create/seeding path below; threaded into
        # _mark_failed so its downgrade is fenced on OUR claim (a stale holder
        # must not flip a newer holder's stolen seeding row to failed).
        seed_claim_id: str | None = None
        if existing is None or existing.status == "seeding" or _is_credential_less_failed(existing):
            seed_claim_id = uuid4().hex
            claimed = await self._repo.try_acquire_seed_claim(
                owner_user_id,
                org_id=owner_id,
                claim_id=seed_claim_id,
            )
            if not claimed:
                put_metric("paperclip.provision.seed_claim_lost")
                return await self._await_concurrent_seed(
                    owner_id=owner_id,
                    owner_user_id=owner_user_id,
                )

        # Sign-up-or-ADOPT (same rationale as provision_member): a user who
        # already has a Paperclip account — a personal row that the org-scoped
        # idempotency check let fall through, or a prior ``failed`` row
        # (``_mark_failed`` preserves paperclip_user_id + password) — is
        # adopted by signing in with their stored password rather than
        # re-signing-up (which would 422 and corrupt the row). We hold the
        # password, and Paperclip has no user-delete/reset. The final
        # ``repo.put`` re-points the row at this org.
        adopting_existing = existing is not None and bool(existing.paperclip_user_id)
        # Reconcile a partially seeded company instead of reminting one. When a
        # prior attempt CREATED the company but the required roster didn't land,
        # step 8 persists a NON-active ``failed`` row carrying the
        # real ``company_id`` (a CEO-less company must never be marked active).
        # The ``status=="active"`` short-circuit above never fires for it, so
        # without adopting that company here the next /container/status poll
        # re-runs the full create + invite/accept/approve chain — and because
        # Paperclip ignores our ``Idempotency-Key``, MINTS A NEW ORPHAN COMPANY
        # on every ~3s poll for as long as the seed outage lasts. When a
        # non-active row already carries a company_id we ADOPT it: skip the
        # create + invite/accept/approve chain and resume at the seed step,
        # retrying the stored seed contract against the SAME company. A net-new failure with
        # NO company_id (the orphan-422 / pre-create marker) is deliberately
        # excluded — its ``company_id`` is "" and it re-runs the full chain.
        #
        # The ``org_id == owner_id`` guard is own-side defense-in-depth (L3):
        # rows are keyed per-user, so a seat-only ex-member re-entering
        # PERSONAL context (owner_id == their own user_id) can still find
        # their OWN row still carrying the ORG's company_id from a prior
        # membership. Without this guard that row would be adopted here,
        # re-pointing a personal provision at someone else's company.
        # Cross-tenant access is separately fenced by Paperclip authz (the
        # re-signed-in session isn't a member of that company), so the real
        # damage is a silently dropped 30-day purge timer on the row, not a
        # data leak — this closes it at the source instead of relying on the
        # downstream fence.
        reconciling_company = (
            existing is not None
            and (existing.status != "active" or starter_incomplete)
            and bool(existing.company_id)
            and existing.org_id == owner_id
        )
        password = decrypt(existing.paperclip_password_encrypted) if adopting_existing else secrets.token_urlsafe(32)
        try:
            # 1. Obtain the owner's Better Auth account + session — signup for
            #    a brand-new user, sign-in to adopt an existing account. The
            #    session is used later for the invite-accept and seed-agent
            #    steps.
            if adopting_existing:
                owner_auth_email = _auth_email(existing, owner_email)
                signin = await self._admin.sign_in_user(email=owner_auth_email, password=password)
                paperclip_user_id = existing.paperclip_user_id
                new_user_session_cookie = signin["_session_cookie"]
            else:
                owner_auth_email = owner_email
                signup, owner_auth_email, password = await self._sign_up_with_staged_credentials(
                    user_id=owner_user_id, email=owner_email, password=password
                )
                paperclip_user_id = signup["user"]["id"]
                new_user_session_cookie = signup["_session_cookie"]

                # Persist the freshly created credential BEFORE the rest of
                # the chain (mirrors provision_member). If a later step
                # fails, _mark_failed updates this row in place — preserving
                # paperclip_user_id + password — so the next attempt ADOPTS
                # via sign-in instead of re-signing-up, which would 422
                # against the now-existing Better Auth user and strand it as
                # an unadoptable orphan (observed live 2026-06-11: a failure
                # after sign-up left a row with an empty paperclip_user_id,
                # wedging every retry). company_id isn't known yet at this
                # point in the org flow; adoption only needs the credential
                # pair. Net-new only: an adopted row already carries its
                # credential and must not be overwritten to "provisioning".
                #
                # WRITER SAFETY (whole-item put #1 of the audited 4 — see
                # architecture spec §2's "load-bearing safety requirement"):
                # this branch only runs when ``adopting_existing`` is False,
                # which implies ``reconciling_company`` is also False (a
                # company_id can only exist on a row that also carries
                # paperclip_user_id — the credential pair is written together
                # below, and ``_mark_failed``'s put-if-absent marker never
                # carries a company_id either). So there is normally nothing
                # to preserve here. Read-merge ``existing`` anyway, purely
                # defensively: a residual row from an unrelated prior
                # incarnation (the personal->org L6 edge case, out of scope
                # per the 2026-07-24 decisions) could in principle carry
                # onboarding fields this write would otherwise silently wipe.
                now = datetime.now(timezone.utc)
                await self._repo.put(
                    PaperclipCompany(
                        user_id=owner_user_id,
                        org_id=owner_id,
                        company_id="",
                        paperclip_user_id=paperclip_user_id,
                        paperclip_password_encrypted=encrypt(password),
                        paperclip_email=owner_auth_email,
                        service_token_encrypted="",
                        status="provisioning",
                        created_at=now,
                        updated_at=now,
                        **_carry_forward_onboarding_fields(existing),
                    )
                )

            # 2. Reconcile-or-create the company. On the RECONCILE path (a prior
            #    non-active row already carries a real company_id — see the
            #    ``reconciling_company`` note above) we ADOPT that company and
            #    skip both the create and the invite/accept/approve chain,
            #    dropping straight to the seed retry below. The user is already
            #    an approved owner of it from the first attempt. Otherwise the
            #    ADMIN creates the company (admin becomes the initial owner via
            #    Paperclip's auto-ensureMembership in ``routes/companies.ts``).
            if reconciling_company:
                company_id = existing.company_id
                # Only used as the slug-derivation fallback below; the adopted
                # company already has its real, user-visible name from the first
                # attempt, so we don't re-generate (or re-create) it.
                company_name = owner_email
                logger.info(
                    "paperclip_provisioning.provision_org: adopting existing company %s for "
                    "owner_id %s (prior non-active row) — skipping create/invite, retrying seed",
                    company_id,
                    owner_id,
                )
            else:
                # The company name is user-visible and editable afterwards, so
                # generate a real one. Falling back to the owner's email keeps
                # the previous behaviour if naming fails — never abort here.
                try:
                    company_name = await generate_company_name(
                        org_id=owner_id if is_org_owner_id(owner_id) else None,
                        owner_email=owner_email,
                    )
                except Exception as e:  # noqa: BLE001 — naming must not fail provisioning
                    logger.warning(
                        "paperclip_provisioning.provision_org: name generation failed for owner_id %s: %s",
                        owner_id,
                        e,
                    )
                    company_name = owner_email

                try:
                    company = await self._admin.create_company(
                        name=company_name,
                        description="Lightbulb Teams workspace",
                        session_cookie=admin_cookie,
                        idempotency_key=owner_user_id,
                    )
                except PaperclipApiError as e:
                    if e.status_code == 401:
                        # Admin session expired; retry once with fresh token.
                        invalidate_admin_session()
                        admin_cookie = await get_admin_session_cookie(self._admin._http)
                        company = await self._admin.create_company(
                            name=company_name,
                            description="Lightbulb Teams workspace",
                            session_cookie=admin_cookie,
                            idempotency_key=owner_user_id,
                        )
                    else:
                        raise
                company_id = company["id"]

            # NOTE: the agent-email slug is deliberately NOT allocated here.
            # It is allocated (and the address minted) on the agent's first
            # owner-directed send — see core/services/agent_mail_owner_send.py.
            #
            # The slug becomes the subdomain of an address other people are
            # given, and `company_slug_repo` has no delete path, so whatever is
            # reserved is permanent. Reserving it HERE would freeze
            # `company_name` above — an auto-generated placeholder nobody
            # chose, which for a personal owner is just their email local part.
            # Minting late lets it derive from the name the agent actually
            # picked during its first run.

            # 3+4+5. ADMIN creates a one-shot invite for the new user, who
            #    accepts it, and the join request is ensured approved — leaving
            #    the user an active owner of the company. Skipped entirely on the
            #    reconcile path (the adopted company already has this membership).
            #    ``human_role="owner"`` is critical: Paperclip's
            #    ``resolveHumanInviteRole`` defaults a missing role to
            #    ``"operator"``, and ``grantsForHumanRole("operator")``
            #    grants only ``tasks:assign`` — no ``agents:create``.
            #    Without this, the user joins their own company as a
            #    low-privilege member and POST /api/companies/{co}/agents
            #    403s with "Missing permission: agents:create". v1
            #    assumes single-tenant orgs, so granting the new user
            #    ``"owner"`` matches the Clerk-side org-owner concept;
            #    multi-tenant role distinctions can be revisited later.
            #    (Paperclip auto-approves human accepts, so the standalone
            #    approve inside _accept_invite_and_ensure_approved only fires as
            #    a fallback.)
            if not reconciling_company:
                invite = await self._admin.create_invite(
                    session_cookie=admin_cookie,
                    company_id=company_id,
                    email=owner_email,
                    human_role="owner",
                )
                invite_token = invite["token"]

                await self._accept_invite_and_ensure_approved(
                    member_session_cookie=new_user_session_cookie,
                    approver_session_cookie=admin_cookie,
                    company_id=company_id,
                    invite_token=invite_token,
                )

            # 6. Mint a long-lived OpenClaw service-token JWT. Baked into each
            #    seeded agent's adapter config so it can reach the container.
            #
            #    The JWT's ``sub`` claim MUST equal the container's
            #    ``owner_id`` — the WS Lambda authorizer extracts ``sub``
            #    and routes the connection to that owner's container.
            #    For orgs ``owner_id`` is the Clerk org_id; for personal
            #    owners it's the Clerk user_id. Previously this minted
            #    with ``owner_user_id`` (the human creator's user_id),
            #    which broke org wakes: the authorizer then tried to
            #    route to a user-personal container that doesn't exist
            #    for org members.
            svc_token = service_token.mint(owner_id)

            # Which adapter carries these seeded runs: the deployed native Hermes adapter
            # through the hook-token-authenticated backend facade, whose ``apiKey`` is that
            # token -- so the seed needs the owner's container row.
            owner_container = await container_repo.get_by_owner_id(owner_id)
            seed_hook_token = (owner_container or {}).get("hook_token") or ""

            starter_agent_ids = dict(existing.starter_agent_ids or {}) if reconciling_company else {}
            for starter_key, name, role, title, parent_key in STARTER_ROSTER:
                if starter_agent_ids.get(starter_key):
                    continue
                reports_to = starter_agent_ids.get(parent_key) if parent_key else None
                if parent_key and not reports_to:
                    continue
                try:
                    # Paperclip gets the authenticated backend facade, not a private runtime
                    # address. Its native session header supplies the final agent id after
                    # Paperclip assigns it.
                    adapter_config = build_hermes_gateway_adapter_config(owner_id, hook_token=seed_hook_token)
                    agent = await self._admin.create_agent(
                        company_id=company_id,
                        name=name,
                        role=role,
                        title=title,
                        capabilities=STARTER_CAPABILITIES[starter_key],
                        reports_to=reports_to,
                        adapter_type=HERMES_GATEWAY_TYPE,
                        adapter_config=adapter_config,
                        session_cookie=new_user_session_cookie,
                        idempotency_key=f"seed:starter:{starter_key}:v1",
                    )
                    if isinstance(agent, dict) and agent.get("id"):
                        starter_agent_ids[starter_key] = agent["id"]
                except PaperclipApiError as e:
                    logger.warning(
                        "paperclip_provisioning.provision_org: starter %s seed failed for %s: %s",
                        starter_key,
                        owner_user_id,
                        e,
                    )
                except httpx.RequestError:
                    logger.warning(
                        "paperclip_provisioning.provision_org: starter %s seed transport failed for %s",
                        starter_key,
                        owner_user_id,
                    )
            missing_starter_keys = [
                starter_key for starter_key, *_ in STARTER_ROSTER if not starter_agent_ids.get(starter_key)
            ]
            seed_ok = not missing_starter_keys
            keep_agent_ids = list(starter_agent_ids.values())

            # Paperclip's built-ins are purged only after the selected seed
            # contract is complete. A later normal reconciliation retries any
            # missing starter keys against the same company.
            if seed_ok:
                await self._purge_builtin_agents(
                    company_id=company_id,
                    session_cookie=new_user_session_cookie,
                    keep_agent_ids=keep_agent_ids,
                )

            # 8. Persist. The DDB column is still ``org_id`` — that's a
            #    Paperclip-side stored concept; we just feed it from the
            #    new ``owner_id`` kwarg.
            #
            now = datetime.now(timezone.utc)

            # ---- Onboarding fields (G2 mint / writer-safety — INV-3, INV-4) ----
            # WRITER SAFETY (whole-item put #2 of the audited 4): this is THE
            # row-creating write for the company, so it is also the ONLY place
            # onboarding_generation is minted (G2). Two cases:
            #
            # * ``reconciling_company`` (RECONCILE-ADOPT of an existing,
            #   non-active row's real company_id — see the docstring above):
            #   this is a RETRY of the SAME onboarding attempt, not a new one.
            #   The generation (and whatever funnel progress exists)
            #   was already established on ``existing`` by the ORIGINAL
            #   create — carry it forward untouched. This is the exact case
            #   the architecture spec's "load-bearing safety requirement"
            #   warns about: skipping this carry-forward would silently reset
            #   a company's funnel state on every reconcile retry.
            # * fresh create (``not reconciling_company``): a genuinely-new
            #   Paperclip company was just confirmed (``create_company`` ran
            #   above, unconditionally in this branch). Mint a fresh
            #   onboarding_generation and reset the funnel to its
            #   initial states, atomically on this same write — never on
            #   ``provision_member`` (org seat), per INV-3.
            if reconciling_company:
                onboarding_fields = _carry_forward_onboarding_fields(existing)
            else:
                # if_not_exists, mapped onto this whole-item put: a company-less
                # retry (create_company itself failed after signup succeeded) is
                # UNCLAIMED (reconciling_company is False with no company_id, so
                # the seed-claim fence never engages -- see #15/#20). A
                # concurrent peer's retry may have already minted and persisted
                # a generation before this read; adopt it instead of stomping it
                # with a fresh random ULID. Only a genuinely generation-less
                # existing row (or none at all) mints fresh.
                onboarding_fields = {
                    "onboarding_generation": (
                        existing.onboarding_generation
                        if existing is not None and existing.onboarding_generation
                        else str(ULID())
                    ),
                    "funnel_state": "not_started",
                }

            row = PaperclipCompany(
                user_id=owner_user_id,
                org_id=owner_id,
                company_id=company_id,
                paperclip_user_id=paperclip_user_id,
                paperclip_password_encrypted=encrypt(password),
                paperclip_email=owner_auth_email,
                service_token_encrypted=encrypt(svc_token),
                status="active" if seed_ok else "failed",
                created_at=now,
                updated_at=now,
                # Never minted any more (the Friday/Atlas seed is retired), but
                # this is a whole-item put: carry a legacy row's ids forward so a
                # reconcile does not silently wipe them before the owner migrates.
                seeded_agent_ids=(existing.seeded_agent_ids if reconciling_company else None),
                starter_agent_ids=starter_agent_ids,
                last_error=(
                    None
                    if seed_ok
                    else "provision_org: missing required starter agents: " + ", ".join(missing_starter_keys)
                ),
                **onboarding_fields,
            )
            if reconciling_company:
                await self._repo.put(row)
            else:
                # Fix round 1 (Important #2): the read that decided onboarding_fields above and
                # this put are not atomic -- a peer's own mint can land in between. Condition the
                # put so a peer who landed first WINS instead of being silently overwritten; on
                # loss, adopt the stored generation with one consistent re-read and put once
                # more (a single retry, not a new lease).
                try:
                    await self._repo.put(
                        row,
                        condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
                        condition_values={":g": row.onboarding_generation},
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                    winner = await self._repo.get(owner_user_id, consistent_read=True)
                    row.onboarding_generation = winner.onboarding_generation if winner else row.onboarding_generation
                    await self._repo.put(row)
            if seed_ok:
                logger.info(
                    "paperclip_provisioning.provision_org: owner_id %s active (company=%s, owner=%s)",
                    owner_id,
                    company_id,
                    owner_user_id,
                )
            else:
                logger.warning(
                    "paperclip_provisioning.provision_org: owner_id %s company=%s has an incomplete "
                    "agent seed — persisted status=failed for a later poll to reconcile",
                    owner_id,
                    company_id,
                )
            return row

        except Exception as e:
            # Don't corrupt an ADOPTED row (see provision_member): re-pointing
            # a user's existing active row at this org only lands on the
            # successful repo.put above, so a transient failure must preserve
            # that row. Only mark-failed for net-new owners.
            if not adopting_existing:
                await self._mark_failed(
                    user_id=owner_user_id,
                    owner_id=owner_id,
                    reason=f"provision_org failed: {e}",
                    expected_seeding_claim_id=seed_claim_id,
                )
            # ``retryable`` is now set automatically:
            #   - PaperclipApiError sets it in __init__ (5xx/429 → True).
            #   - OrgNotProvisionedError carries the class attribute.
            #   - Other exceptions are treated as non-retryable by the
            #     T12 caller (``getattr(exc, "retryable", False)``).
            raise

    async def provision_member(
        self,
        *,
        owner_id: str,
        user_id: str,
        email: str,
        owner_email: str,
    ) -> PaperclipCompany:
        """Add a new Isol8 member to the org's existing Paperclip company.

        ``owner_id`` is the container's owner — Clerk's org_id for orgs
        and Clerk's user_id for personal owners. Since this entry point
        only runs from ``organizationMembership.created`` it'll always
        be a Clerk org_id in practice, but the kwarg is named ``owner_id``
        to align with the rest of the backend. The DDB column is still
        ``org_id`` (Paperclip-side stored concept).

        ``owner_email`` is supplied by the webhook caller (Clerk's
        ``organizationMembership.created`` payload exposes it). We
        could look it up via ``user_repo`` instead, but keeping it as
        a kwarg avoids the cross-service coupling and makes the
        orchestrator easier to test in isolation.

        Service-token contract: org members share the org's container
        (keyed by ``owner_id``), so the member's stored
        ``service_token_encrypted`` must be a JWT whose ``sub`` claim
        equals ``owner_id`` — not the member's own user_id. The WS Lambda
        authorizer extracts ``sub`` and routes the resulting connection
        to that owner's container; minting with the member's user_id
        would route to a user-personal container that doesn't exist for
        org members.

        Idempotent on ``user_id``: an already-active row short-circuits
        to a no-op return.
        """
        existing = await self._repo.get(user_id)
        # Idempotency must be org-scoped: rows are keyed per-user, so a user
        # with an active row for a DIFFERENT org_id (e.g. a personal company)
        # must be treated as not-yet-joined to THIS org, not idempotent
        # success — otherwise they'd never be invited and /teams would keep
        # resolving them to their other company.
        if existing is not None and existing.status == "active" and existing.org_id == owner_id:
            logger.info(
                "paperclip_provisioning.provision_member: member %s already active",
                user_id,
            )
            return existing

        company_id = await self._repo.get_org_company_id(owner_id)
        if not company_id:
            raise OrgNotProvisionedError(
                f"Owner {owner_id} has no Paperclip company yet. "
                "organization.created webhook may not have run yet — caller should retry."
            )

        owner_row = await self.find_org_owner(owner_id)
        if owner_row is None:
            # Defensive — get_org_company_id returned a company_id but
            # find_org_owner couldn't locate the row that produced it.
            # Should be impossible if the GSI is consistent.
            raise OrgNotProvisionedError(f"Owner {owner_id} has company {company_id} but no owner row found")
        owner_password = decrypt(owner_row.paperclip_password_encrypted)

        # Fresh-join fence: the same PK put-if-absent claim ``provision_org``
        # takes, on the member's own row. Without it, N concurrent dispatches
        # for a joining member (the /container/status poll and the /teams
        # reconcile fire together on the first page load after an invite
        # accept) each read "no row", each sign up the SAME email with its
        # OWN random password — Better Auth's ``user.email`` has no unique
        # constraint, so every sign-up returns 200 and mints a duplicate
        # account — and the last DDB writer stores a password that does not
        # belong to the account sign-in resolves by email. Every later mint
        # is then INVALID_EMAIL_OR_PASSWORD, and the retries trip Better
        # Auth's 3-per-10s sign-in limit (429). Losers wait for the winner's
        # active row and adopt it; on timeout they raise the 409 the caller
        # already treats as "join in flight, no-op". Placed after the owner
        # lookups on purpose: everything from here on runs inside the try
        # below, whose failure path downgrades the claim row (seeding ->
        # failed) so the next poll retries promptly.
        member_claim_id: str | None = None
        if existing is None or existing.status == "seeding" or _is_credential_less_failed(existing):
            member_claim_id = uuid4().hex
            claimed = await self._repo.try_acquire_seed_claim(
                user_id,
                org_id=owner_id,
                claim_id=member_claim_id,
                # The org's company is already known here; a membership
                # removal that lands mid-claim needs it to archive cleanly.
                company_id=company_id,
            )
            if not claimed:
                put_metric("paperclip.provision.member_claim_lost")
                return await self._await_concurrent_seed(
                    owner_id=owner_id,
                    owner_user_id=user_id,
                    what="provision_member",
                )

        # Sign-up-or-ADOPT. A brand-new user signs up. A user who ALREADY has
        # a Paperclip account — e.g. a personal workspace row, which the
        # org-scoped idempotency check above intentionally let fall through,
        # or a prior attempt that left a ``failed`` row (``_mark_failed``
        # preserves paperclip_user_id + password) — is ADOPTED by signing in
        # with their stored password. Re-signing-up would 422 (email already
        # in Better Auth) and corrupt their existing row via ``_mark_failed``;
        # Paperclip exposes no user-delete/reset, but we hold the account's
        # password, so sign-in is the clean path and also recovers a
        # previously-corrupted row. The final ``repo.put`` re-points the row
        # at this org.
        adopting_existing = existing is not None and bool(existing.paperclip_user_id)
        member_password = (
            decrypt(existing.paperclip_password_encrypted) if adopting_existing else secrets.token_urlsafe(32)
        )
        try:
            # 1. Obtain the member's Better Auth account + session.
            member_auth_email = _auth_email(existing, email) if adopting_existing else email
            if adopting_existing:
                member_signin = await self._admin.sign_in_user(email=member_auth_email, password=member_password)
                member_paperclip_user_id = existing.paperclip_user_id
                member_session_cookie = member_signin["_session_cookie"]
            else:
                member_signup, member_auth_email, member_password = await self._sign_up_with_staged_credentials(
                    user_id=user_id, email=email, password=member_password
                )
                member_paperclip_user_id = member_signup["user"]["id"]
                member_session_cookie = member_signup["_session_cookie"]

                # Persist the freshly created credential BEFORE the rest of
                # the join chain. If a later step fails, this row lets the
                # next attempt ADOPT (sign-in) instead of re-signing-up —
                # re-signup would 422 against the now-existing Better Auth
                # user and strand it as an orphan (the failure path's
                # _mark_failed updates this row in place, preserving the
                # paperclip_user_id + password). Only for net-new members:
                # an adopted row already carries its credential and must not
                # be overwritten to "provisioning".
                #
                # WRITER SAFETY (whole-item put #3 of the audited 4):
                # deliberately carries NO onboarding fields, and per INV-3
                # never mints onboarding_generation here (a seat-join is not
                # a generation bump). Onboarding truth lives ONLY on the
                # OWNER row (see PaperclipRepo.resolve_owner_row) — a member
                # row is never read for it, so this is safe by construction,
                # not by omission. The one accepted residual: if THIS row
                # previously belonged to the member's own personal company
                # (the sign-up-or-ADOPT re-point noted above), that row's
                # personal onboarding fields are wiped here rather than
                # carried forward — orphaning them is the explicitly
                # out-of-scope personal->org L6 cleanup (2026-07-24 decision:
                # "no live path in the main app today"), not a new gap this
                # PR introduces.
                now = datetime.now(timezone.utc)
                await self._repo.put(
                    PaperclipCompany(
                        user_id=user_id,
                        org_id=owner_id,
                        company_id=company_id,
                        paperclip_user_id=member_paperclip_user_id,
                        paperclip_password_encrypted=encrypt(member_password),
                        paperclip_email=member_auth_email,
                        service_token_encrypted="",
                        status="provisioning",
                        created_at=now,
                        updated_at=now,
                    )
                )

            # 2. Sign in as the org owner so we can act on the company
            #    (create invite + approve join request).
            owner_signin = await self._admin.sign_in_user(
                email=_auth_email(owner_row, owner_email),
                password=owner_password,
            )
            owner_session_cookie = owner_signin["_session_cookie"]

            # 3. Owner mints a one-shot invite token for this member.
            #    ``human_role="owner"`` mirrors provision_org — without
            #    it, Paperclip defaults humanRole to ``"operator"`` and
            #    the new member can't create agents in their own
            #    company (agents:create not granted). v1 single-tenant
            #    assumption: every Isol8 org member needs full access.
            invite = await self._admin.create_invite(
                session_cookie=owner_session_cookie,
                company_id=company_id,
                email=email,
                human_role="owner",
            )
            invite_token = invite["token"]

            # 4+5. Member accepts the invite; the join request is ensured
            #    approved (Paperclip auto-approves human accepts, so the
            #    standalone approve only fires as a fallback — see
            #    _accept_invite_and_ensure_approved).
            #
            #    409 = "You already belong to this company"
            #    (paperclip server/src/routes/access.ts). That is the JOIN
            #    already being done, not a failure — and it is the ONLY thing
            #    standing between a stale row and a working Teams surface.
            #    Prod 2026-09-13: a member whose row still pointed at their
            #    personal company 403'd on every Teams route
            #    (routers/teams/deps.py compares row.org_id to the active
            #    owner) because this raise aborted the chain before step 7's
            #    repo.put — the only write that re-points the row — and the
            #    caller swallowed the 409 as an idempotent no-op. Forever.
            #    Swallowing it here converges the row on the next request.
            try:
                await self._accept_invite_and_ensure_approved(
                    member_session_cookie=member_session_cookie,
                    approver_session_cookie=owner_session_cookie,
                    company_id=company_id,
                    invite_token=invite_token,
                )
            except PaperclipApiError as e:
                if e.status_code != 409:
                    raise
                logger.info(
                    "paperclip_provisioning.provision_member: %s already belongs to company %s "
                    "— persisting row at owner_id %s",
                    user_id,
                    company_id,
                    owner_id,
                )

            # 6. Mint the member's OpenClaw service-token JWT.
            #
            #    Org members share the org's container (keyed by
            #    ``owner_id``), so the JWT's ``sub`` claim MUST equal
            #    ``owner_id`` — that's the value the WS Lambda authorizer
            #    pulls out and routes against. Previously this minted
            #    with the joining member's own ``user_id``, which broke
            #    Paperclip wakes for org members: the authorizer would
            #    try to route to a user-personal container that doesn't
            #    exist for org members.
            svc_token = service_token.mint(owner_id)

            # 7. Persist the member row. DDB column stays ``org_id`` —
            #    that's a Paperclip-side stored concept; we just feed it
            #    from the new ``owner_id`` kwarg.
            #
            # WRITER SAFETY (whole-item put #4 of the audited 4): same
            # reasoning as put #3 above — a member row deliberately carries
            # NO onboarding fields and never mints onboarding_generation
            # (INV-3). Unlike ``seeded_agent_ids`` (replicated below so the
            # delete-protection survives even if the owner row is purged),
            # onboarding truth is NOT replicated onto member rows: it has
            # exactly one home (the owner row, via
            # ``PaperclipRepo.resolve_owner_row``), and a member's own row is
            # structurally never consulted for it.
            now = datetime.now(timezone.utc)
            row = PaperclipCompany(
                user_id=user_id,
                org_id=owner_id,
                company_id=company_id,
                paperclip_user_id=member_paperclip_user_id,
                paperclip_password_encrypted=encrypt(member_password),
                paperclip_email=member_auth_email,
                service_token_encrypted=encrypt(svc_token),
                status="active",
                created_at=now,
                updated_at=now,
                # Replicate the seeded-agent marker onto every member row so
                # the company's legacy Friday/Atlas stay protected even if the
                # original provisioner's row is later purged (the company
                # survives as long as ANY active member remains, and
                # get_seeded_agent_ids resolves from any row carrying it).
                seeded_agent_ids=owner_row.seeded_agent_ids,
            )
            await self._repo.put(row)
            logger.info(
                "paperclip_provisioning.provision_member: member %s added to owner_id %s (company=%s)",
                user_id,
                owner_id,
                company_id,
            )
            return row

        except Exception as e:  # noqa: F841 — kept for clarity in future logging
            # Don't corrupt an ADOPTED row: when we're re-pointing a user's
            # existing (e.g. personal) active row at this org, a transient
            # join failure must NOT mark that unrelated workspace failed — the
            # re-point only lands on the successful repo.put above, so on
            # failure the existing row is still valid and the next poll
            # retries. Only mark-failed for net-new users (their own
            # placeholder).
            if not adopting_existing:
                await self._mark_failed(
                    user_id=user_id,
                    owner_id=owner_id,
                    reason=f"provision_member failed: {e}",
                    company_id=company_id,
                    expected_seeding_claim_id=member_claim_id,
                )
            # ``retryable`` is set on the exception itself (see provision_org
            # for the full rationale).
            raise

    async def _sign_up_with_staged_credentials(
        self, *, user_id: str, email: str, password: str
    ) -> tuple[dict, str, str]:
        # A lost response can mean signup succeeded. Save the secret before
        # making that external side effect (if_not_exists + read-back converges).
        email, encrypted = await self._repo.stage_signup_credentials(
            user_id, email=email, password_encrypted=encrypt(password)
        )
        password = decrypt(encrypted)
        try:
            account = await self._admin.sign_up_user(email=email, password=password, name=email)
        except PaperclipApiError as duplicate:
            if (
                duplicate.status_code != 422
                or _paperclip_error_code(duplicate.body) != "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL"
            ):
                raise
            try:
                account = await self._admin.sign_in_user(email=email, password=password)
            except PaperclipApiError as signin_error:
                if signin_error.status_code == 401:
                    # An unrelated existing account is still a signup failure.
                    # Never infer identity or membership from email alone.
                    raise duplicate from signin_error
                raise
        return account, email, password

    async def archive_member(self, *, user_id: str, grace_days: int = 30) -> None:
        """Archive a member's Paperclip membership AND disable their DDB row.

        Used by ``organizationMembership.deleted`` (spec §3 case C). The
        member is removed from the Clerk org but their Paperclip
        membership lingers unless we proactively archive it. We
        archive on the Paperclip side first (so the Teams UI stops
        listing them immediately) then mark the DDB row disabled with
        a purge timer (same 30-day grace as ``disable``).

        Lookup chain:

          1. ``repo.get(user_id)`` — find their company + paperclip_user_id.
             If absent, no-op (member never had Paperclip provisioned).
          2. ``list_members(company_id)`` — find the membership row
             whose ``principalId`` matches our paperclip_user_id. We
             need the membership ``id`` to call archive (NOT the
             user_id).
          3. ``archive_member(company_id, member_id)`` — POST to the
             archive endpoint with admin session cookie.
          4. ``update_status(disabled, purge_at)`` — same DDB shape as
             ``disable``.

        If the member row can't be located on Paperclip's side (already
        archived externally, or list_members shape drift) we still mark
        the DDB row disabled — but record ``last_error`` so admin
        tooling can surface the inconsistency. We deliberately do NOT
        raise: the user is already gone from Clerk, and a stuck
        Paperclip row is recoverable out-of-band.

        If Paperclip's archive call itself fails (5xx), we re-raise so
        the webhook caller can enqueue a retry — DDB stays untouched
        in that case so a successful retry can complete the full chain.
        """
        existing = await self._repo.get(user_id)
        if existing is None:
            return

        # Lazy import to avoid the same import-cycle workaround
        # ``provision_org`` uses.
        from core.services.paperclip_admin_session import (
            get_admin_session_cookie,
            invalidate_admin_session,
        )

        admin_cookie = await get_admin_session_cookie(self._admin._http)

        member_id: Optional[str] = None
        try:
            members_resp = await self._admin.list_members(
                session_cookie=admin_cookie,
                company_id=existing.company_id,
            )
        except PaperclipApiError as e:
            if e.status_code == 401:
                invalidate_admin_session()
                admin_cookie = await get_admin_session_cookie(self._admin._http)
                members_resp = await self._admin.list_members(
                    session_cookie=admin_cookie,
                    company_id=existing.company_id,
                )
            else:
                raise

        # Paperclip's list_members returns either a flat list or a
        # ``{members: [...]}`` envelope depending on caller; defensive
        # against both since the BFF doesn't unwrap it.
        members: list[dict]
        if isinstance(members_resp, list):
            members = members_resp
        elif isinstance(members_resp, dict):
            members = members_resp.get("members") or members_resp.get("items") or []
        else:
            members = []

        for m in members:
            if m.get("principalId") == existing.paperclip_user_id:
                member_id = m.get("id")
                break

        purge_at = datetime.now(timezone.utc) + timedelta(days=grace_days)
        if member_id is None:
            logger.warning(
                "paperclip_provisioning.archive_member: no member row found "
                "for user=%s paperclip_user=%s in company=%s; marking DDB "
                "disabled without Paperclip archive",
                user_id,
                existing.paperclip_user_id,
                existing.company_id,
            )
            await self._repo.update_status(
                user_id,
                status="disabled",
                scheduled_purge_at=purge_at,
                last_error="archive_member: paperclip member row not found",
            )
            return

        try:
            await self._admin.archive_member(
                session_cookie=admin_cookie,
                company_id=existing.company_id,
                member_id=member_id,
            )
        except PaperclipApiError as e:
            if e.status_code == 401:
                invalidate_admin_session()
                admin_cookie = await get_admin_session_cookie(self._admin._http)
                await self._admin.archive_member(
                    session_cookie=admin_cookie,
                    company_id=existing.company_id,
                    member_id=member_id,
                )
            else:
                # Re-raise so the webhook caller can enqueue retry.
                # DDB row stays untouched so the retry path can
                # complete the full chain.
                raise

        await self._repo.update_status(
            user_id,
            status="disabled",
            scheduled_purge_at=purge_at,
            owner_id=existing.org_id,
        )
        logger.info(
            "paperclip_provisioning.archive_member: user %s archived (company=%s, member=%s, purge_at=%s)",
            user_id,
            existing.company_id,
            member_id,
            purge_at.isoformat(),
        )

    async def disable(self, *, user_id: str, grace_days: int = 30) -> None:
        """Mark a user's Paperclip row disabled with a purge timer.

        Idempotent: returns early if no row exists. We deliberately do
        NOT call ``disable_company`` here even when this is the only
        member — that's a destructive action the cancellation flow may
        want to delay until after the grace window closes (mirrors the
        existing "30-day account recovery" UX). ``purge`` is where
        Paperclip-side cleanup happens.
        """
        existing = await self._repo.get(user_id)
        if existing is None:
            return
        purge_at = datetime.now(timezone.utc) + timedelta(days=grace_days)
        await self._repo.update_status(
            user_id,
            status="disabled",
            scheduled_purge_at=purge_at,
            owner_id=existing.org_id,
        )
        logger.info(
            "paperclip_provisioning.disable: user %s scheduled for purge at %s",
            user_id,
            purge_at.isoformat(),
        )

    async def purge(self, *, user_id: str) -> None:
        """Hard-delete a row whose grace window has elapsed.

        Designed to be called from the cleanup cron (T13) for each row
        returned by ``repo.scan_purge_due``. v1: we delete the local
        mapping row only; we do NOT archive the underlying Paperclip
        company even when this user was the last member of their org.
        Reasoning:

        - Calling ``disable_company`` requires a Better Auth session
          token (post per-org architecture pivot in T11), which means
          signing in as the just-purged user. That works (the Better
          Auth account survives our local-row deletion), but adds
          complexity for a non-essential cleanup.
        - Orphan Paperclip companies are cheap (rows in a shared
          Aurora cluster) and harmless. An out-of-band admin sweep
          script can find companies in Paperclip with no matching
          ``paperclip-companies`` row and archive them periodically.

        Treat purge as "remove the user's mapping; let Paperclip-side
        cleanup ride a separate ops cadence."
        """
        existing = await self._repo.get(user_id)
        if existing is None:
            return

        await self._repo.delete(user_id)
        remaining_members = await self._repo.count_org_members(existing.org_id)
        logger.info(
            "paperclip_provisioning.purge: user %s purged (org_remaining=%d)",
            user_id,
            remaining_members,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def try_acquire_org_create_claim(self, owner_id: str, *, holder: str) -> Optional[str]:
        """Delegate to the repo's strongly-consistent per-org create claim.

        Used by ``ensure_user_ready`` to serialize company creation across
        backend instances (the per-org asyncio lock only covers one process,
        and ``find_org_owner`` reads an eventually-consistent GSI). Returns a
        per-attempt fencing token iff this caller may proceed to create the
        company (None otherwise); pass it back to ``release_org_create_claim``.
        """
        return await self._repo.try_acquire_org_create_claim(owner_id, holder=holder)

    async def release_org_create_claim(self, owner_id: str, *, token: str) -> None:
        """Delegate to the repo: release the per-org create claim on failure,
        fenced on the per-attempt token so a stale straggler can't delete a
        newer attempt's claim (ABA)."""
        await self._repo.release_org_create_claim(owner_id, token=token)

    async def get_company_row(self, user_id: str) -> Optional[PaperclipCompany]:
        """Strongly-consistent fetch of a user's row.

        Used by ``ensure_user_ready`` to verify that a create actually wrote
        an active row before leaving the per-org claim in place — a
        strongly-consistent read so it never false-negatives on our own
        just-written row.
        """
        return await self._repo.get(user_id, consistent_read=True)

    async def find_org_owner(self, owner_id: str) -> Optional[PaperclipCompany]:
        """Find the owner row (oldest ACTIVE ``created_at`` for this owner_id).

        Filters out rows that aren't ``status="active"`` before picking
        the oldest. ``disable()`` flips ``status="disabled"`` but leaves
        the row in DDB until the purge worker runs (up to 30 days). If
        the original/oldest member was disabled, returning their stale
        Better Auth account would break ``provision_member`` (the
        owner-side sign-in / create-invite / approve-join would all
        fail) even though other active admins exist. Likewise we skip
        ``provisioning`` and ``failed`` rows — neither is sign-in-able.

        v1 implementation: query the ``by-org-id`` GSI for all rows in
        the org, filter to ``active``, then pick the smallest
        ``created_at``. For typical org sizes (1-10 users) this is
        fine; if Teams ever supports large orgs we'd want to either:

          * sort the GSI by ``created_at`` (range key on the GSI), or
          * track the owner's user_id explicitly on every row.

        Both are deferred — the GSI was declared with KEYS_ONLY-style
        projection minimums and the table is small.

        The DDB column queried by the GSI is still named ``org_id`` —
        that's a Paperclip-side stored concept; the parameter name
        ``owner_id`` reflects the platform-wide naming.

        Returns None if the owner has no active rows.
        """
        # Re-query the GSI without a Limit so we can pick the oldest
        # row. This is intentionally a separate code path from
        # ``get_org_company_id`` (which only needs one row).
        from boto3.dynamodb.conditions import Key  # local import to keep module load light

        items: list[dict] = []
        last_evaluated_key: Optional[dict] = None
        # We access the repo's internal table to avoid duplicating the
        # boto3 setup. This is a deliberate provisioning-side helper —
        # if we ever need this for non-provisioning code we'll move it
        # into paperclip_repo.
        table = self._repo._table()  # noqa: SLF001 — same package, justified above
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(owner_id),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            from core.dynamodb import run_in_thread

            resp = await run_in_thread(table.query, **kwargs)
            items.extend(resp.get("Items", []))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        # Drop disabled / provisioning / failed rows BEFORE the oldest
        # pick — see docstring. ``status`` should always be present
        # since ``put()`` writes it, but treat missing as "not active"
        # to be safe.
        active_items = [it for it in items if it.get("status") == "active"]
        if not active_items:
            return None

        # Pick the oldest by created_at; if any item is missing the
        # field we fall back to the first row (defensive — shouldn't
        # happen since put() always writes it).
        def _sort_key(it: dict) -> str:
            return it.get("created_at", "")

        oldest = min(active_items, key=_sort_key)
        return await self._repo.get(oldest["user_id"])

    async def _await_concurrent_seed(
        self,
        *,
        owner_id: str,
        owner_user_id: str,
        what: str = "provision_org",
    ) -> PaperclipCompany:
        """Loser side of the ``try_acquire_seed_claim`` fence in
        ``provision_org`` (and, with ``what="provision_member"``, the member
        join): a peer on another backend instance is running the
        seed chain RIGHT NOW. Wait briefly for its active row and ADOPT it —
        the exact same "already provisioned" semantics as the ``status ==
        "active"`` short-circuit — instead of running a second full seed.

        Strongly-consistent polls (the row was just written cross-instance;
        an eventually-consistent read could miss the winner's final put for
        the entire window). Two non-adopt outcomes, both deliberate:

        * The winner FAILED (``status="failed"`` observed): stop waiting and
          raise the in-flight 409 below — the NEXT poll's ordinary retry
          path (adopt/reconcile/full-rerun, depending on what the failed row
          carries) owns recovery; this loser must not start a competing
          chain against a row a peer just downgraded.
        * Timeout (winner still mid-chain): raise a 409-shaped
          ``PaperclipApiError`` — the exact error class + status the caller
          (``_ensure_paperclip_for_owner``) already treats as "provision
          already in flight — no-op", so the poll loop simply comes back
          and finds the active row.
        """
        for _ in range(_SEED_WAIT_ATTEMPTS):
            await asyncio.sleep(_SEED_WAIT_INTERVAL_S)
            row = await self._repo.get(owner_user_id, consistent_read=True)
            if row is not None and row.status == "active" and row.org_id == owner_id:
                logger.info(
                    "paperclip_provisioning.%s: adopted concurrent seed for "
                    "owner_id %s (user=%s) — a peer instance won the seed claim",
                    what,
                    owner_id,
                    owner_user_id,
                )
                return row
            if row is not None and row.status == "failed":
                # The winner failed; recovery belongs to the next poll's
                # ordinary retry path against the downgraded row.
                break
        raise PaperclipApiError(
            f"concurrent {what} seed in flight for owner {owner_id} "
            f"(user_id={owner_user_id}) — lost the seed claim; retry on next poll",
            status_code=409,
            body=None,
        )

    async def _accept_invite_and_ensure_approved(
        self,
        *,
        member_session_cookie: str,
        approver_session_cookie: str,
        company_id: str,
        invite_token: str,
    ) -> str:
        """Accept an invite as the joining user and ensure the resulting
        join request is approved. Returns the join-request id.

        ONE implementation for both provision_org and provision_member —
        the accept/approve chain was previously duplicated in each, and
        the skip-redundant-approve guard landed only in the member copy,
        so every fresh org/personal signup kept failing on the approve
        409 (observed live 2026-06-11). A shared helper makes that drift
        impossible.

        Paperclip's human-invite accept handler auto-approves in the same
        request (ensures membership + grants, flips the join request to
        ``approved`` — server/src/routes/access.ts, the
        ``human_invite_accept`` branch), so the response comes back
        ``status="approved"`` and calling the standalone approve endpoint
        again returns 409 ("Join request is not pending"). We always mint
        a fresh invite, so the accept response reliably reports status:
        only a still-pending request is approved — which also self-heals
        if Paperclip ever stops auto-approving.
        """
        accept = await self._admin.accept_invite(
            session_cookie=member_session_cookie,
            invite_token=invite_token,
        )
        # The accept response is the join-request row flattened by
        # ``toJoinRequestResponse`` — a top-level ``id`` plus ``status``.
        # Strict access on ``id`` so a shape change fails loud instead of
        # silently dropping the approve step.
        try:
            request_id = accept["id"]
        except (KeyError, TypeError) as e:
            raise PaperclipApiError(
                "accept_invite response missing 'id' field",
                status_code=200,
                body=accept,
            ) from e

        if accept.get("status") != "approved":
            await self._admin.approve_join_request(
                session_cookie=approver_session_cookie,
                company_id=company_id,
                request_id=request_id,
            )
        return request_id

    async def _purge_builtin_agents(
        self,
        *,
        company_id: str,
        session_cookie: str,
        keep_agent_ids: list[str],
    ) -> None:
        """Best-effort removal of agents outside the selected seed contract.

        WHY this exists: Paperclip's company-bootstrap creates its OWN starter
        agents ("Reflection Coach", "Summarizer") server-side the instant a
        company is created — verified via DB timestamps, they land 1-5s BEFORE
        our seeds, produced by upstream company-create and NOT by
        any Lightbulb code. We can't
        patch Paperclip on our timeline (fork release cycle), so provision_org
        deletes the leftovers here.

        Keyed on persisted agent IDs, not names, so renames remain safe.

        Best-effort and NEVER fatal — a leftover starter is a cosmetic roster
        wart, not a reason to fail provisioning. A list failure or any per-agent
        delete failure logs a warning + emits
        ``paperclip.provision.builtin_purge_error`` and is swallowed. Idempotent
        on the reconcile path: an already-purged company contains only kept IDs.

        HARD LIMIT — Paperclip's built-in agents are UNDELETABLE. The starters
        (Reflection Coach, Summarizer) carry a built-in marker in their metadata,
        and the deployed BFF's ``DELETE /api/agents/{id}`` answers 409
        ``built_in_agent_undeletable`` ("Built-in agents cannot be deleted; pause
        them instead") BEFORE touching the DB — there is no force/override, so no
        backend call can remove them from the roster against this Paperclip
        version. They are created ``status="paused"`` with disabled triggers, so
        they never run; the residue is cosmetic. This purge therefore attempts
        each non-seeded delete (which cleanly removes any genuine stray — the BFF
        deletes a deletable agent's own assigned routines inside the same delete
        transaction, so there is no ``routines.assigneeAgentId`` FK for us to
        pre-clear) and treats the built-in-undeletable 409 as an EXPECTED no-op it
        logs and steps past. Every OTHER delete failure stays a logged + metered
        warning, and the whole purge stays best-effort (never fatal).

        (A prior revision pre-listed and pre-deleted each victim's routines on the
        theory the 409 was a ``routines.assigneeAgentId`` FK. It was not — the FK
        is handled BFF-side and the 409 is the built-in guard — so that inert step
        was removed; verified against the pinned Paperclip commit's agents-service
        ``remove`` + routines-list route.)
        """
        keep = set(keep_agent_ids)
        try:
            listed = await self._admin.list_agents(
                session_cookie=session_cookie,
                company_id=company_id,
            )
        except Exception as e:  # noqa: BLE001 — purge must never fail provisioning
            logger.warning(
                "paperclip_provisioning: builtin-agent purge could not list agents for company %s: %s",
                company_id,
                e,
            )
            put_metric("paperclip.provision.builtin_purge_error")
            return

        # ``list_agents`` is typed ``dict`` upstream but returns a bare list in
        # practice; accept the list and the enveloped shapes defensively (same
        # normalization as company_deploy_service._find_friday_id).
        if isinstance(listed, dict):
            agents = listed.get("agents") or listed.get("items") or []
        elif isinstance(listed, list):
            agents = listed
        else:
            agents = []

        # Attempt to delete every non-seeded agent. A genuine stray is removed
        # cleanly (the BFF clears a deletable agent's own assigned routines in the
        # same delete transaction — there is no routines.assigneeAgentId FK for us
        # to pre-clear). Paperclip's built-in managed agents answer 409
        # ``built_in_agent_undeletable`` and cannot be removed at all; that is the
        # EXPECTED outcome, logged and stepped past — NOT a purge error. Any other
        # failure is a logged + metered warning. The purge is never fatal.
        purged: list[str] = []
        undeletable_builtins: list[str] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id")
            if not agent_id or agent_id in keep:
                continue
            agent_name = agent.get("name")
            try:
                await self._admin.delete_agent(
                    session_cookie=session_cookie,
                    agent_id=agent_id,
                )
                purged.append(f"{agent_name!r}({agent_id})")
            except PaperclipApiError as e:
                if e.status_code == 409 and "built_in_agent_undeletable" in str(e.body or ""):
                    # Undeletable Paperclip built-in (already paused) — expected.
                    undeletable_builtins.append(f"{agent_name!r}({agent_id})")
                    continue
                logger.warning(
                    "paperclip_provisioning: builtin-agent purge failed to delete agent %r (%s) in company %s: %s",
                    agent_name,
                    agent_id,
                    company_id,
                    e,
                )
                put_metric("paperclip.provision.builtin_purge_error")
            except Exception as e:  # noqa: BLE001 — one delete failing must not fail provisioning
                logger.warning(
                    "paperclip_provisioning: builtin-agent purge failed to delete agent %r (%s) in company %s: %s",
                    agent_name,
                    agent_id,
                    company_id,
                    e,
                )
                put_metric("paperclip.provision.builtin_purge_error")

        if purged:
            logger.info(
                "paperclip_provisioning: purged %d non-seeded Paperclip agent(s) from company %s: %s",
                len(purged),
                company_id,
                ", ".join(purged),
            )
        if undeletable_builtins:
            logger.info(
                "paperclip_provisioning: %d Paperclip built-in agent(s) are undeletable in this "
                "BFF version and were left in place (created already-paused) for company %s: %s",
                len(undeletable_builtins),
                company_id,
                ", ".join(undeletable_builtins),
            )

    async def _mark_failed(
        self,
        *,
        user_id: str,
        owner_id: str,
        reason: str,
        company_id: str = "",
        expected_seeding_claim_id: str | None = None,
    ) -> None:
        """Persist ONE durable ``status="failed"`` marker for this seed failure.

        ``expected_seeding_claim_id`` (the fresh-create seed path passes the
        token it acquired) fences the in-place downgrade on the bare
        ``seeding`` row's ``seeding_claim_id`` so a stale holder can't flip a
        NEWER holder's live claim (a stolen one) to ``failed``. It is a no-op
        once the row advanced past the bare claim (that write dropped the id).

        Two paths, both leaving exactly one honest failed record:

        * A prior row exists → update it in place (preserving immutables like
          paperclip_user_id + password so the next attempt can ADOPT), guarded
          so it never downgrades a row a concurrent attempt already completed to
          ``active``.
        * NO prior row → write a durable marker via a CONDITIONAL put-if-absent
          (``put_failed_marker_if_absent``). This restores the honest 503 on
          ``/teams`` (and ``teams_status="failed"`` on ``/container/status``) for
          the net-new / orphan-422 case: without it the row is MISSING, and the
          consumer maps a missing row → 202 "provisioning" (an eternal spinner),
          only a ``failed`` row → 503. The marker carries NO fabricated
          paperclip_user_id — that is the OLD poison (an empty id blocked
          adoption and, on a stale read, clobbered a real id) — and, being
          conditional, can never overwrite a real row a concurrent provision
          wrote in the gap. The marker is durable-visibility only, not adoptable.

        Truncates the reason to keep the DynamoDB item small. The DDB column
        ``org_id`` is fed from the ``owner_id`` kwarg — the rename is at the call
        boundary, not the storage layer.
        """
        truncated = reason[:1000]
        # Strongly consistent: this can run milliseconds after the seed
        # claim's conditional put (a sign-up that fails immediately). A
        # replica-lagged miss here would fall through to the put-if-absent
        # marker, which then fails ITS condition against the claim row — and
        # the row stays a bare ``seeding`` claim for the whole stale window.
        existing = await self._repo.get(user_id, consistent_read=True)
        if existing is not None:
            # Never downgrade a row that a concurrent provision attempt has
            # already completed to ``active``. Two overlapping attempts for one
            # member can race: the early adoptable row persisted here lets a
            # second poll sign in and write the final ``active`` row while the
            # first poll is still running; if that first poll then hits a late
            # transient error, this failure path must NOT clobber the active
            # row. The conditional write skips (no-ops) in that case.
            landed = await self._repo.update_status(
                user_id,
                status="failed",
                last_error=truncated,
                not_if_status="active",
                require_seeding_claim_id=expected_seeding_claim_id,
                owner_id=existing.org_id,
                # A bare member seed-claim row has no company_id; keep the
                # failed marker carrying the known one (see update_status).
                company_id=company_id or None,
            )
            if not landed:
                logger.info(
                    "paperclip_provisioning._mark_failed: skipped downgrade of "
                    "user %s — row already active (concurrent provision won)",
                    user_id,
                )
            return

        # No prior row: write a durable ``failed`` marker via a CONDITIONAL
        # put-if-absent — NOT the poisoned empty-id placeholder, and NOT an
        # unconditional put. This makes the failure durably visible (honest 503
        # on /teams, teams_status="failed" on /container/status) instead of the
        # eternal 202 spinner a missing row produces. The conditional no-ops if a
        # real row appeared concurrently (never clobbers it); the marker carries
        # no fabricated paperclip_user_id (durable visibility only, not adoptable).
        landed = await self._repo.put_failed_marker_if_absent(
            user_id=user_id,
            org_id=owner_id,
            reason=truncated,
            company_id=company_id,
        )
        if landed:
            logger.warning(
                "paperclip_provisioning._mark_failed: no existing row for user %s "
                "(owner_id %s, company_id %r) — wrote durable failed marker: %s",
                user_id,
                owner_id,
                company_id,
                truncated,
            )
        else:
            logger.info(
                "paperclip_provisioning._mark_failed: no marker written for user %s "
                "(owner_id %s) — a real row appeared concurrently (put-if-absent no-op)",
                user_id,
                owner_id,
            )
