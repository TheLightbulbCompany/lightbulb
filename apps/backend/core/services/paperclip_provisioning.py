"""Orchestrator for provisioning Paperclip on Clerk webhook events.

Three entry points (all async, all idempotent on user_id / owner_id):

* ``provision_org`` — for ``organization.created``. Creates the Paperclip
  company AND the org owner's Better Auth account in a single
  end-to-end chain (sign_up_user -> create_company -> mint service token
  -> seed Main Agent -> persist row). This is the only path that calls
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

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.encryption import decrypt, encrypt
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import service_token
from core.services.agent_email import mint_agent_email_address
from core.services.company_identity import allocate_company_slug, generate_company_name
from core.services.paperclip_adapter_config import OPENCLAW_GATEWAY_TYPE, synthesize_openclaw_adapter
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError

logger = logging.getLogger(__name__)

# Roster metadata for the seed team. The roster (GET /companies/{id}/agents)
# is Friday's source of truth for delegation — these strings are what stops
# her from assigning Atlas domain work. Also used by
# scripts/backfill_seed_team_roster.py for pre-existing companies.
FRIDAY_TITLE = "CEO"
FRIDAY_CAPABILITIES = (
    "Orchestrator and single point of contact for the owner. Decomposes "
    "goals, delegates to specialist agents, triages what comes back, and "
    "reports up. Does not execute specialist work itself; when no "
    "specialist fits, it hires one via Atlas or recommends the marketplace."
)
ATLAS_TITLE = "Agent Recruiter"
ATLAS_CAPABILITIES = (
    "Builds new specialist agents on request: researches the role, "
    "architects the agent, generates its workspace, and registers it into "
    "the company roster. Assign Atlas a role spec (what the new hire must "
    "do) — not domain work. Atlas does not do research tasks, prototypes, "
    "or general work itself, and never publishes to the marketplace (the "
    "owner publishes via the seller wizard)."
)


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
    # prefix once (``isol8-{env}-``). Passing the fully-qualified name
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


def _ws_gateway_url(env_name: str) -> str:
    """Pick the right WS gateway URL for the seeded Main Agent's
    openclaw-gateway adapter.

    Mirrors the env-name convention used by ``CLAUDE.md`` and the
    frontend's ``NEXT_PUBLIC_API_URL`` (``api-{env}.isol8.co``).
    Production drops the ``-prod`` suffix.
    """
    if env_name in ("prod", "production"):
        return "wss://ws.isol8.co"
    if env_name:
        return f"wss://ws-{env_name}.isol8.co"
    # Empty env (local dev) — fall back to local WS endpoint.
    return "ws://localhost:8000"


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
        seeded Main Agent's gateway URL (``dev``/``staging``/``prod``).
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
        (per ``paperclip/server/src/routes/companies.ts:269``). The
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

        Service-token contract: the seeded Main Agent's
        ``adapterConfig.authToken`` is a JWT whose ``sub`` claim MUST
        equal the container's ``owner_id``, because the WS Lambda
        authorizer uses ``sub`` to route the connection to that owner's
        OpenClaw container. We mint with ``owner_id`` directly so the
        token's ``sub`` matches the routing key for both org and
        personal owners.

        Idempotent: if a row already exists for ``owner_user_id`` with
        ``status="active"``, return it unchanged. A non-active row that
        already carries a ``company_id`` (the Friday-partial case: the
        company WAS created, only the required CEO seed failed) is
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
        if existing is not None and existing.status == "active" and existing.org_id == owner_id:
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

        # Sign-up-or-ADOPT (same rationale as provision_member): a user who
        # already has a Paperclip account — a personal row that the org-scoped
        # idempotency check let fall through, or a prior ``failed`` row
        # (``_mark_failed`` preserves paperclip_user_id + password) — is
        # adopted by signing in with their stored password rather than
        # re-signing-up (which would 422 and corrupt the row). We hold the
        # password, and Paperclip has no user-delete/reset. The final
        # ``repo.put`` re-points the row at this org.
        adopting_existing = existing is not None and bool(existing.paperclip_user_id)
        # Reconcile a Friday-partial company instead of reminting one. When a
        # prior attempt CREATED the company but the REQUIRED Friday (CEO) seed
        # didn't land, step 8 persists a NON-active ``failed`` row carrying the
        # real ``company_id`` (a CEO-less company must never be marked active).
        # The ``status=="active"`` short-circuit above never fires for it, so
        # without adopting that company here the next /container/status poll
        # re-runs the full create + invite/accept/approve chain — and because
        # Paperclip ignores our ``Idempotency-Key``, MINTS A NEW ORPHAN COMPANY
        # on every ~3s poll for as long as the seed outage lasts. When a
        # non-active row already carries a company_id we ADOPT it: skip the
        # create + invite/accept/approve chain and resume at the seed step,
        # retrying Friday/Atlas against the SAME company. A net-new failure with
        # NO company_id (the orphan-422 / pre-create marker) is deliberately
        # excluded — its ``company_id`` is "" and it re-runs the full chain.
        reconciling_company = existing is not None and existing.status != "active" and bool(existing.company_id)
        password = decrypt(existing.paperclip_password_encrypted) if adopting_existing else secrets.token_urlsafe(32)
        try:
            # 1. Obtain the owner's Better Auth account + session — signup for
            #    a brand-new user, sign-in to adopt an existing account. The
            #    session is used later for the invite-accept and seed-agent
            #    steps.
            if adopting_existing:
                signin = await self._admin.sign_in_user(email=owner_email, password=password)
                paperclip_user_id = existing.paperclip_user_id
                new_user_session_cookie = signin["_session_cookie"]
            else:
                signup = await self._admin.sign_up_user(
                    email=owner_email,
                    password=password,
                    name=owner_email,
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
                now = datetime.now(timezone.utc)
                await self._repo.put(
                    PaperclipCompany(
                        user_id=owner_user_id,
                        org_id=owner_id,
                        company_id="",
                        paperclip_user_id=paperclip_user_id,
                        paperclip_password_encrypted=encrypt(password),
                        service_token_encrypted="",
                        status="provisioning",
                        created_at=now,
                        updated_at=now,
                    )
                )

            # 2. Reconcile-or-create the company. On the RECONCILE path (a prior
            #    non-active row already carries a real company_id — see the
            #    ``reconciling_company`` note above) we ADOPT that company and
            #    skip both the create and the invite/accept/approve chain,
            #    dropping straight to the seed retry below. The user is already
            #    an approved owner of it from the first attempt. Otherwise the
            #    ADMIN creates the company (admin becomes the initial owner via
            #    Paperclip's auto-ensureMembership in ``routes/companies.ts:273``).
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
                        description="Isol8 Teams workspace",
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
                            description="Isol8 Teams workspace",
                            session_cookie=admin_cookie,
                            idempotency_key=owner_user_id,
                        )
                    else:
                        raise
                company_id = company["id"]

            # Reserve the platform-unique slug the agent's email address will
            # later be built from. Best-effort: an owner without a slug simply
            # has no address minted yet. Idempotent per-owner (allocate returns
            # any existing reservation), so it also runs on the reconcile path —
            # a retry that finally lands Friday can then mint the address the
            # failed first attempt couldn't.
            company_slug: str | None = None
            try:
                company_slug = await allocate_company_slug(owner_id=owner_id, name=company_name)
            except Exception as e:  # noqa: BLE001 — allocation must not fail provisioning
                logger.warning(
                    "paperclip_provisioning.provision_org: slug allocation failed for owner_id %s: %s",
                    owner_id,
                    e,
                )

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

            # 6. Mint a long-lived OpenClaw service-token JWT. Baked
            #    into the seeded Main Agent's adapter config so the
            #    agent can reach the user's container via the gateway.
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

            # IDs of the agents we seed below. Persisted on the company
            # row (step 8) as the STABLE marker of "Isol8-seeded, must not
            # be deleted" — see PaperclipCompany.seeded_agent_ids and
            # paperclip_openclaw_pairing.is_seeded_agent. Each seed is
            # best-effort, so a failed seed simply contributes no id.
            #
            # On the reconcile path an earlier attempt may already have seeded
            # Atlas (a failed row can only carry Atlas — Friday's presence is
            # exactly what marks the row active). Carry those ids forward so we
            # don't drop them, and skip the Atlas re-create below (Paperclip
            # ignores Idempotency-Key, so re-creating would spawn a SECOND Atlas
            # and orphan the first). The ternary short-circuits before touching
            # ``existing`` unless reconciling_company is True (which implies
            # existing is not None).
            seeded_agent_ids: list[str] = list(existing.seeded_agent_ids or []) if reconciling_company else []

            # 7. Best-effort seed a "Main Agent" pointing at the
            #    user's OpenClaw container. Failure here is not fatal
            #    — the company is still usable, the user can create
            #    agents from the UI manually. Acts AS the user (their
            #    session) so the agent ends up owned by them.
            # Captured for Atlas's org placement below; stays None when the
            # Friday mint fails so Atlas simply seeds unparented.
            friday_agent_id: str | None = None
            try:
                friday = await self._admin.create_agent(
                    company_id=company_id,
                    name="Friday",
                    role="ceo",
                    title=FRIDAY_TITLE,
                    capabilities=FRIDAY_CAPABILITIES,
                    adapter_type=OPENCLAW_GATEWAY_TYPE,
                    adapter_config=synthesize_openclaw_adapter(
                        gateway_url=_ws_gateway_url(self._env_name),
                        service_token=svc_token,
                        user_id=owner_user_id,
                    ),
                    session_cookie=new_user_session_cookie,
                    idempotency_key=f"{owner_user_id}:main-agent",
                )
                if isinstance(friday, dict) and friday.get("id"):
                    friday_agent_id = friday["id"]
                    seeded_agent_ids.append(friday["id"])
            except PaperclipApiError as e:
                logger.warning(
                    "paperclip_provisioning.provision_org: seed agent failed for %s: %s",
                    owner_user_id,
                    e,
                )

            # 7b. Best-effort seed an "Atlas" agent alongside Friday. Atlas
            #     is the in-house "agent recruiter" — Friday-as-orchestrator
            #     delegates "hire a new agent" requests to Atlas, so it must
            #     exist on day one. Wrapped in its own try/except so an Atlas
            #     failure does NOT undo a successful Friday seed
            #     (defense-in-depth — both seeds are best-effort).
            #
            #     Skip the create entirely when RECONCILING a failed row that
            #     already carries Atlas. A failed row's seeded_agent_ids can
            #     only ever be Atlas (Friday's presence is what flips the row
            #     active), and Paperclip ignores our Idempotency-Key — so
            #     re-creating here would spawn a SECOND Atlas and orphan the
            #     first (it drops out of seeded_agent_ids, losing its
            #     delete-protection). Accepted residual: the carried-forward
            #     Atlas keeps reports_to=None (it was seeded before Friday
            #     existed) — one correctly-counted unparented Atlas beats two
            #     with one orphaned; re-parenting would need an extra
            #     update-agent call (out of scope).
            atlas_already_seeded = reconciling_company and bool(existing.seeded_agent_ids)
            if not atlas_already_seeded:
                try:
                    atlas = await self._admin.create_agent(
                        company_id=company_id,
                        name="Atlas",
                        # Atlas was originally typed as ``role="recruiter"``
                        # but Paperclip's AGENT_ROLES enum doesn't include
                        # "recruiter" (see paperclip/packages/shared/src/
                        # constants.ts:44). The constraint here is just
                        # "must not be 'ceo'" because config.py:463 selects
                        # the ceo-role agent as the default. "general" is the
                        # most semantically neutral fit — which is exactly why
                        # title + capabilities below must carry the real job:
                        # the roster otherwise describes Atlas as a generalist
                        # and Friday delegates domain work to it.
                        role="general",
                        title=ATLAS_TITLE,
                        capabilities=ATLAS_CAPABILITIES,
                        reports_to=friday_agent_id,
                        adapter_type=OPENCLAW_GATEWAY_TYPE,
                        adapter_config=synthesize_openclaw_adapter(
                            gateway_url=_ws_gateway_url(self._env_name),
                            service_token=svc_token,
                            user_id=owner_user_id,
                        ),
                        session_cookie=new_user_session_cookie,
                        idempotency_key=f"{owner_user_id}:atlas-agent",
                    )
                    if isinstance(atlas, dict) and atlas.get("id"):
                        seeded_agent_ids.append(atlas["id"])
                except PaperclipApiError as e:
                    logger.warning(
                        "paperclip_provisioning.provision_org: seed Atlas agent failed for %s: %s",
                        owner_user_id,
                        e,
                    )

            # Mint the agent's email address from the slug we just allocated.
            # Best-effort and guarded on both Friday and the slug existing —
            # the address is a convenience, and a mail-side failure must never
            # undo a successful provision (same posture as the Atlas seed).
            if friday_agent_id and company_slug:
                try:
                    await mint_agent_email_address(
                        owner_id=owner_id,
                        agent_id=friday_agent_id,
                        agent_name="Friday",
                        owner_email=owner_email,
                        slug=company_slug,
                    )
                except Exception as e:  # noqa: BLE001 — never fail the provision
                    logger.warning(
                        "paperclip_provisioning.provision_org: agent-email mint failed for owner_id %s: %s",
                        owner_id,
                        e,
                    )

            # 8. Persist. The DDB column is still ``org_id`` — that's a
            #    Paperclip-side stored concept; we just feed it from the
            #    new ``owner_id`` kwarg.
            #
            #    Only mark ``active`` if the REQUIRED CEO seed (Friday) actually
            #    landed. Friday's seed is best-effort above, but a company with
            #    no CEO is both unusable AND poisonous: the ``status=="active"``
            #    idempotency short-circuit (top of this method) would freeze it —
            #    every later poll returns the CEO-less row unchanged and never
            #    re-seeds. When Friday is missing we persist a non-active
            #    ``failed`` row instead, carrying the full adoptable credential
            #    (paperclip_user_id + password) so a later poll signs IN and
            #    reconciles rather than re-signing-up. Atlas stays optional — its
            #    absence alone does NOT block ``active``.
            seed_ok = friday_agent_id is not None
            now = datetime.now(timezone.utc)
            row = PaperclipCompany(
                user_id=owner_user_id,
                org_id=owner_id,
                company_id=company_id,
                paperclip_user_id=paperclip_user_id,
                paperclip_password_encrypted=encrypt(password),
                service_token_encrypted=encrypt(svc_token),
                status="active" if seed_ok else "failed",
                created_at=now,
                updated_at=now,
                seeded_agent_ids=seeded_agent_ids or None,
                last_error=(
                    None
                    if seed_ok
                    else "provision_org: required CEO seed (Friday) did not land — "
                    "company left non-active for a later poll to reconcile"
                ),
            )
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
                    "paperclip_provisioning.provision_org: owner_id %s company=%s created but CEO "
                    "seed (Friday) missing — persisted status=failed for a later poll to reconcile",
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
            if adopting_existing:
                member_signin = await self._admin.sign_in_user(email=email, password=member_password)
                member_paperclip_user_id = existing.paperclip_user_id
                member_session_cookie = member_signin["_session_cookie"]
            else:
                member_signup = await self._admin.sign_up_user(
                    email=email,
                    password=member_password,
                    name=email,
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
                now = datetime.now(timezone.utc)
                await self._repo.put(
                    PaperclipCompany(
                        user_id=user_id,
                        org_id=owner_id,
                        company_id=company_id,
                        paperclip_user_id=member_paperclip_user_id,
                        paperclip_password_encrypted=encrypt(member_password),
                        service_token_encrypted="",
                        status="provisioning",
                        created_at=now,
                        updated_at=now,
                    )
                )

            # 2. Sign in as the org owner so we can act on the company
            #    (create invite + approve join request).
            owner_signin = await self._admin.sign_in_user(
                email=owner_email,
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
            await self._accept_invite_and_ensure_approved(
                member_session_cookie=member_session_cookie,
                approver_session_cookie=owner_session_cookie,
                company_id=company_id,
                invite_token=invite_token,
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
            now = datetime.now(timezone.utc)
            row = PaperclipCompany(
                user_id=user_id,
                org_id=owner_id,
                company_id=company_id,
                paperclip_user_id=member_paperclip_user_id,
                paperclip_password_encrypted=encrypt(member_password),
                service_token_encrypted=encrypt(svc_token),
                status="active",
                created_at=now,
                updated_at=now,
                # Replicate the seeded-agent marker onto every member row so
                # the company's Friday/Atlas stay protected even if the
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
                )
            # ``retryable`` is set on the exception itself (see provision_org
            # for the full rationale).
            raise

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

    async def try_acquire_org_create_claim(self, owner_id: str, *, holder: str) -> bool:
        """Delegate to the repo's strongly-consistent per-org create claim.

        Used by ``ensure_user_ready`` to serialize company creation across
        backend instances (the per-org asyncio lock only covers one process,
        and ``find_org_owner`` reads an eventually-consistent GSI). Returns
        True iff this caller may proceed to create the company.
        """
        return await self._repo.try_acquire_org_create_claim(owner_id, holder=holder)

    async def release_org_create_claim(self, owner_id: str, *, holder: str) -> None:
        """Delegate to the repo: release the per-org create claim on failure."""
        await self._repo.release_org_create_claim(owner_id, holder=holder)

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

    async def _mark_failed(
        self,
        *,
        user_id: str,
        owner_id: str,
        reason: str,
        company_id: str = "",
    ) -> None:
        """Persist ONE durable ``status="failed"`` marker for this seed failure.

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
        existing = await self._repo.get(user_id)
        if existing is not None:
            # Never downgrade a row that a concurrent provision attempt has
            # already completed to ``active``. Two overlapping attempts for one
            # member can race: the early adoptable row this PR persists lets a
            # second poll sign in and write the final ``active`` row while the
            # first poll is still running; if that first poll then hits a late
            # transient error, this failure path must NOT clobber the active
            # row. The conditional write skips (no-ops) in that case.
            landed = await self._repo.update_status(
                user_id,
                status="failed",
                last_error=truncated,
                not_if_status="active",
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
