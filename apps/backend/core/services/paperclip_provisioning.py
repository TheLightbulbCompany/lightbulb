"""Orchestrator for provisioning Paperclip: one workspace = one company.

The paperclip-companies table holds ONE workspace row per Clerk org (keyed by
the org id: company facts + ``member_user_ids``) and one identity row per human
(keyed by their Clerk user id: their Better Auth account only). See
``core/repositories/paperclip_repo.py``.

* ``provision_org`` — creates the workspace's Paperclip company (the only
  caller of ``create_company``) with the provisioner as its first member, and
  seeds the starter roster onto the workspace row.
* ``provision_member`` — joins a human to the workspace's existing company
  (sign up or adopt their identity; the bootstrap admin invites; they accept)
  and records the join on the workspace row.
* ``archive_member`` — removes one human from ONE workspace's company
  (``organizationMembership.deleted``, the account wipe).
* ``disable_workspace`` / ``purge`` — a deleted Clerk org's workspace row goes
  ``disabled`` with a purge timer; the cleanup cron deletes it afterwards.

Failure semantics: ``_mark_failed`` downgrades an unconfirmed sign-up claim so
it can be reclaimed, and (for ``provision_org``) leaves the workspace row
``failed`` — in place, or via a put-if-absent marker — so ``/teams`` answers an
honest 503 instead of an eternal provisioning spinner. It never downgrades a
confirmed identity or an ``active`` workspace. Then it re-raises.
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
from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipRepo, PaperclipWorkspace
from core.services.agent_routine_provisioning import create_agent_routines
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

#: The specialists' Paperclip timer heartbeat: one wake a day, skipped when the agent
#: has no assigned todo/in_progress work (fork ``heartbeat.ts`` timer tick), so an idle
#: specialist costs nothing. Assignment/on-demand wakes stay on. The CEO's heartbeat is
#: armed by ``user_provision.arm_nightly_ceo``, not seeded here.
SPECIALIST_HEARTBEAT = {
    "enabled": True,
    "intervalSec": 86400,
    "wakeOnDemand": True,
    "skipTimerWhenNoActionableWork": True,
}


def _specialist_heartbeat() -> dict:
    """``SPECIALIST_HEARTBEAT``, or no timer while ``NIGHTLY_CEO_HEARTBEATS_ENABLED`` is off (dev)."""
    if settings.NIGHTLY_CEO_HEARTBEATS_ENABLED:
        return SPECIALIST_HEARTBEAT
    return {"enabled": False, "wakeOnDemand": True}


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
        "Owns the company's goals and the plan. Answers the owner and does quick work itself; delegates "
        "specialist work to the right teammate with what done means, and follows it to done. Keeps "
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
        "revenue/pricing questions to Finance, instrumentation to Analytics. For a project built from "
        "scratch, follows the lightbulb skill's references/software-projects.md."
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


#: The routines a fresh workspace's specialists ship with, all created PAUSED
#: (``agent_routine_provisioning`` ``paused=True``): nothing fires until someone switches
#: one on in Teams -> Routines. The CEO gets none -- its timer heartbeat already writes the
#: daily brief. Every description ends with the ``Post results to:`` line the paperclip
#: skill's ``references/routines.md`` documents; ``none`` keeps the result on the task.
#: Times are UTC (13:00 = morning in the Americas, afternoon in Europe).
_ROUTINE_TAIL = (
    " If the data source for this is not connected, say which one on the task and stop."
    "\n\nPut the result on this task.\nPost results to: none"
)
STARTER_ROUTINES = {
    "engineering": [
        {
            "title": "Weekly engineering summary",
            "cron": "0 13 * * 1",
            "description": "List what shipped in the last 7 days (merged PRs, deploys) and what is open or "
            "blocked, with links." + _ROUTINE_TAIL,
        },
    ],
    "finance": [
        {
            "title": "Weekly revenue summary",
            "cron": "0 13 * * 1",
            "description": "Summarise last week's revenue, new and churned customers, refunds and failed "
            "payments against the week before, with the source of each figure." + _ROUTINE_TAIL,
        },
        {
            "title": "Monthly cost review",
            "cron": "0 13 1 * *",
            "description": "List last month's largest costs and any cost that is new or up more than 20% on "
            "the month before, with sources." + _ROUTINE_TAIL,
        },
    ],
    "growth": [
        {
            "title": "Weekly funnel report",
            "cron": "0 13 * * 1",
            "description": "Report last week's traffic, signups and conversions by channel against the week "
            "before, and name one experiment to run next." + _ROUTINE_TAIL,
        },
    ],
    "analytics": [
        {
            "title": "Weekly metrics digest",
            "cron": "0 13 * * 1",
            "description": "Report the key metrics for the last 7 days against the week before; flag any that "
            "moved more than 20% and say why if the data shows it." + _ROUTINE_TAIL,
        },
    ],
    "customer_operations": [
        {
            "title": "Weekly support themes",
            "cron": "0 13 * * 5",
            "description": "Group the last 7 days of customer emails and tickets into the top themes, with a "
            "count and one example each; flag anything urgent or repeated." + _ROUTINE_TAIL,
        },
    ],
}

# Every onboarding attribute on ``PaperclipWorkspace``. A reconcile's
# whole-item ``put_workspace`` carries all of them forward: drift between
# "what the dataclass carries" and "what a put preserves" is exactly the
# bug class this guard exists to close.
_ONBOARDING_FIELD_NAMES = (
    "onboarding_generation",
    "funnel_state",
    "funnel_submitted_at",
    "funnel_completed_at",
    "onboarding_goal_id",
    "onboarding_project_id",
    "onboarding_brief_text",
    "onboarding_submitted_by",
    "first_contact_state",
    "first_contact_attempts",
    "first_contact_reason",
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


def _is_credential_less_failed(identity: Optional[PaperclipIdentity]) -> bool:
    """A ``failed`` sign-up claim with no confirmed ``paperclip_user_id``.
    Signup may already have succeeded; the staged password is retained for
    recovery. Its retry must go through the sign-up fence like a fresh one."""
    return identity is not None and identity.status == "failed" and not identity.paperclip_user_id


def _carry_forward_onboarding_fields(ws: Optional[PaperclipWorkspace]) -> dict:
    """Read-merge helper for a reconcile's whole-item ``put_workspace``: every
    onboarding field the row already carries, so the put cannot wipe it."""
    if ws is None:
        return {}
    return {name: getattr(ws, name) for name in _ONBOARDING_FIELD_NAMES}


def _auth_email(identity: Optional[PaperclipIdentity], live_email: str) -> str:
    """The address to sign in to Better Auth with.

    Better Auth's ``user.email`` is set at sign-up and never updated, so the
    live Clerk address stops being the sign-in truth the moment a human moves
    mailbox domains. Prefer the address recorded on the identity row; fall
    back to the live one only when it is empty. NEVER the other way round —
    that is what mints duplicate Better Auth accounts.
    """
    return (identity.paperclip_email if identity is not None else "") or live_email


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
    ) -> PaperclipWorkspace:
        """Provision the workspace's Paperclip company, with ``owner_user_id``
        as the provisioner.

        ``owner_id`` is the workspace (Clerk org) id — the key of the ONE
        workspace row this writes. ``owner_user_id``'s Better Auth identity
        lives on their own identity row.

        Paperclip's ``POST /api/companies`` requires
        ``req.actor.isInstanceAdmin``, so the bootstrap admin (email loaded
        from Secrets Manager, never hardcoded) creates the company and invites
        the new user as a co-owner via invite/accept/approve.

        Idempotent — AND fenced across backend instances. An ``active``
        workspace with a complete starter roster short-circuits. A fresh
        sign-up first claims the provisioner's IDENTITY row via
        ``repo.try_acquire_seed_claim`` BEFORE any Paperclip side effect; only
        the winner signs up, and a loser waits briefly for the winner's active
        workspace and adopts it, or raises the 409-shaped in-flight error the
        caller already tolerates (``_await_concurrent_seed``). Company creation
        is idempotent at the source: ``create_company`` sends the per-org
        ``Idempotency-Key`` ``lightbulb-company-create:{org}``, which the fork
        scopes to the creating principal (always the one bootstrap admin), so
        concurrent or retried creates for one org replay the same company.

        A non-active workspace row that already carries a ``company_id`` (a
        partial roster) is RECONCILED — we adopt that company and retry the
        seed against it, so a persistent seed outage never mints a new orphan
        company on each poll. A workspace row with NO company_id (a pre-create
        failure) redoes the full chain; a confirmed identity is adopted via
        sign-in (Better Auth sign-up is NOT idempotent).
        """
        ws = await self._repo.get_workspace(owner_id)
        identity = await self._repo.get_identity(owner_user_id)
        starter_incomplete = (
            ws is not None
            and ws.starter_agent_ids is not None
            and any(not ws.starter_agent_ids.get(starter_key) for starter_key, *_ in STARTER_ROSTER)
        )
        if ws is not None and ws.status == "active" and not starter_incomplete:
            logger.info("paperclip_provisioning.provision_org: workspace %s already active", owner_id)
            return ws

        # Lazy import to keep the cycle clean (admin_session imports
        # PaperclipAdminClient).
        from core.services.paperclip_admin_session import (
            get_admin_session_cookie,
            invalidate_admin_session,
        )

        # Acquire admin session up front so we fail loudly if the
        # bootstrap secret is missing (rather than failing 4 steps in).
        admin_cookie = await get_admin_session_cookie(self._admin._http)

        # A non-active workspace row carrying a real company_id is a prior
        # attempt that CREATED the company but whose roster didn't land: adopt
        # it (the create below would replay it anyway under the per-org key).
        reconciling_company = ws is not None and bool(ws.company_id)
        # Never a second company for an org that already has one on record (a
        # pre-migration per-member row, during the deploy → migration-script
        # gap). 409 = the caller's "in flight — no-op"; raised before the
        # try, so no failed marker lands on a workspace awaiting migration.
        if not reconciling_company and await self._repo.org_company_ids(owner_id):
            raise PaperclipApiError(
                f"org {owner_id} already has a company on record but no workspace row — refusing to create another",
                status_code=409,
                body=None,
            )

        # ---- Cross-instance sign-up fence ----------------------------------
        # Fenced: no identity yet, a leftover bare ``seeding`` claim (a live
        # peer's → we lose; a hard-crashed holder's, stale ~5 min → we steal),
        # or a credential-less ``failed`` claim (its retry re-runs sign-up).
        # A confirmed identity is adopted via sign-in and needs no fence.
        seed_claim_id: str | None = None
        if identity is None or identity.status == "seeding" or _is_credential_less_failed(identity):
            seed_claim_id = uuid4().hex
            claimed = await self._repo.try_acquire_seed_claim(owner_user_id, claim_id=seed_claim_id)
            if not claimed:
                put_metric("paperclip.provision.seed_claim_lost")
                return await self._await_concurrent_seed(owner_id=owner_id, owner_user_id=owner_user_id)

        adopting_existing = identity is not None and bool(identity.paperclip_user_id)
        password = decrypt(identity.paperclip_password_encrypted) if adopting_existing else secrets.token_urlsafe(32)
        try:
            # 1. The provisioner's Better Auth account + session — sign-up for
            #    a brand-new user, sign-in to adopt an existing account.
            if adopting_existing:
                owner_auth_email = _auth_email(identity, owner_email)
                signin = await self._admin.sign_in_user(email=owner_auth_email, password=password)
                paperclip_user_id = identity.paperclip_user_id
                new_user_session_cookie = signin["_session_cookie"]
            else:
                signup, owner_auth_email, password = await self._sign_up_with_staged_credentials(
                    user_id=owner_user_id, email=owner_email, password=password
                )
                paperclip_user_id = signup["user"]["id"]
                new_user_session_cookie = signup["_session_cookie"]
                # Settle the identity BEFORE the rest of the chain, so a later
                # failure's retry ADOPTS via sign-in instead of re-signing-up
                # (which 422s against the now-existing Better Auth user).
                await self._repo.put_identity(
                    PaperclipIdentity(
                        user_id=owner_user_id,
                        paperclip_user_id=paperclip_user_id,
                        paperclip_password_encrypted=encrypt(password),
                        paperclip_email=owner_auth_email,
                    )
                )

            # 2. Reconcile-or-create the company.
            if reconciling_company:
                company_id = ws.company_id
                logger.info(
                    "paperclip_provisioning.provision_org: adopting existing company %s for "
                    "owner_id %s (prior non-active row) — retrying seed",
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

                # One key per org: the fork scopes it to the creating principal
                # (the bootstrap admin), so every caller for this org replays
                # the same company instead of minting a second.
                company_create_key = f"lightbulb-company-create:{owner_id}"
                try:
                    company = await self._admin.create_company(
                        name=company_name,
                        description="Lightbulb Teams workspace",
                        session_cookie=admin_cookie,
                        idempotency_key=company_create_key,
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
                            idempotency_key=company_create_key,
                        )
                    else:
                        raise
                company_id = company["id"]

            # NOTE: the agent-email slug is deliberately NOT allocated here —
            # see core/services/agent_mail_owner_send.py (minted on first send
            # so it derives from the name the agent actually picked).

            # 3+4+5. ADMIN invites the provisioner, who accepts; the join is
            #    ensured approved. ``human_role="owner"`` is critical: Paperclip
            #    defaults a missing role to ``"operator"``, which lacks
            #    ``agents:create``. Skipped on reconcile when the provisioner
            #    already joined; a DIFFERENT admin reconciling a failed company
            #    joins it here (409 "already belongs" is the join already done).
            if ws is None or owner_user_id not in ws.member_user_ids:
                invite = await self._admin.create_invite(
                    session_cookie=admin_cookie,
                    company_id=company_id,
                    email=owner_email,
                    human_role="owner",
                )
                try:
                    await self._accept_invite_and_ensure_approved(
                        member_session_cookie=new_user_session_cookie,
                        approver_session_cookie=admin_cookie,
                        company_id=company_id,
                        invite_token=invite["token"],
                    )
                except PaperclipApiError as e:
                    if e.status_code != 409:
                        raise

            # 6. Seed the starter roster. The deployed native Hermes adapter
            #    reaches the backend facade with the owner's hook token.
            owner_container = await container_repo.get_by_owner_id(owner_id)
            seed_hook_token = (owner_container or {}).get("hook_token") or ""

            starter_agent_ids = dict(ws.starter_agent_ids or {}) if reconciling_company else {}
            for starter_key, name, role, title, parent_key in STARTER_ROSTER:
                if starter_agent_ids.get(starter_key):
                    continue
                reports_to = starter_agent_ids.get(parent_key) if parent_key else None
                if parent_key and not reports_to:
                    continue
                try:
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
                        # The CEO keeps the hire authority Paperclip 813 gave role=ceo by
                        # default; every other agent gets the funnel's False.
                        can_create_agents=starter_key == "ceo",
                        runtime_config=None if starter_key == "ceo" else {"heartbeat": _specialist_heartbeat()},
                    )
                    if isinstance(agent, dict) and agent.get("id"):
                        starter_agent_ids[starter_key] = agent["id"]
                except PaperclipApiError as e:
                    logger.warning(
                        "paperclip_provisioning.provision_org: starter %s seed failed for %s: %s",
                        starter_key,
                        owner_id,
                        e,
                    )
                except httpx.RequestError:
                    logger.warning(
                        "paperclip_provisioning.provision_org: starter %s seed transport failed for %s",
                        starter_key,
                        owner_id,
                    )
            missing_starter_keys = [
                starter_key for starter_key, *_ in STARTER_ROSTER if not starter_agent_ids.get(starter_key)
            ]
            seed_ok = not missing_starter_keys

            # Paperclip's built-ins are purged only after the selected seed
            # contract is complete. A later normal reconciliation retries any
            # missing starter keys against the same company.
            if seed_ok:
                await self._purge_builtin_agents(
                    company_id=company_id,
                    session_cookie=new_user_session_cookie,
                    keep_agent_ids=list(starter_agent_ids.values()),
                )
                await self._seed_starter_routines(
                    company_id=company_id,
                    session_cookie=new_user_session_cookie,
                    starter_agent_ids=starter_agent_ids,
                    owner_id=owner_id,
                )

            # 7. Persist the workspace row.
            #
            # Onboarding fields (G2 mint / writer-safety — INV-3, INV-4): on
            # RECONCILE this is a retry of the SAME onboarding attempt — carry
            # every onboarding field forward untouched. On a fresh create a
            # genuinely-new company was just confirmed: mint a generation
            # (adopting a concurrent peer's already-persisted one) and reset
            # the funnel.
            now = datetime.now(timezone.utc)
            if reconciling_company:
                onboarding_fields = _carry_forward_onboarding_fields(ws)
            else:
                onboarding_fields = {
                    "onboarding_generation": (
                        ws.onboarding_generation if ws is not None and ws.onboarding_generation else str(ULID())
                    ),
                    "funnel_state": "not_started",
                }

            row = PaperclipWorkspace(
                org_id=owner_id,
                company_id=company_id,
                status="active" if seed_ok else "failed",
                created_at=ws.created_at if ws is not None else now,
                updated_at=now,
                provisioner_user_id=(ws.provisioner_user_id if ws is not None else "") or owner_user_id,
                # ponytail: whole-item put — a member who joined between the
                # read above and this write (only possible on an active,
                # starter-incomplete legacy workspace) drops out of the set;
                # their next request re-joins (409 swallowed) and re-adds them.
                member_user_ids=(set(ws.member_user_ids) if ws is not None else set()) | {owner_user_id},
                seeded_agent_ids=(ws.seeded_agent_ids if reconciling_company else None),
                starter_agent_ids=starter_agent_ids,
                last_error=(
                    None
                    if seed_ok
                    else "provision_org: missing required starter agents: " + ", ".join(missing_starter_keys)
                ),
                **onboarding_fields,
            )
            if reconciling_company:
                await self._repo.put_workspace(row)
            else:
                # The read that decided onboarding_fields and this put are not
                # atomic: condition it so a peer who landed first WINS, then
                # adopt the stored generation and put once more.
                try:
                    await self._repo.put_workspace(
                        row,
                        condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
                        condition_values={":g": row.onboarding_generation},
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                    winner = await self._repo.get_workspace(owner_id, consistent_read=True)
                    row.onboarding_generation = winner.onboarding_generation if winner else row.onboarding_generation
                    await self._repo.put_workspace(row)
            if seed_ok:
                logger.info(
                    "paperclip_provisioning.provision_org: owner_id %s active (company=%s, provisioner=%s)",
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
            await self._mark_failed(
                user_id=owner_user_id,
                org_id=owner_id,
                reason=f"provision_org failed: {e}",
                seed_claim_id=seed_claim_id,
            )
            # ``retryable`` is set on the exception itself (PaperclipApiError:
            # 5xx/429 → True; anything else is non-retryable to the caller).
            raise

    async def provision_member(
        self,
        *,
        owner_id: str,
        user_id: str,
        email: str,
    ) -> PaperclipWorkspace:
        """Join ``user_id`` to the workspace ``owner_id``'s existing company.

        Signs the member up (or adopts their existing Better Auth identity —
        one identity per human, whatever workspaces they are in), then the
        bootstrap admin (a co-owner of every company) invites them and the
        member accepts. Records the join on the workspace row's
        ``member_user_ids``; a member already recorded short-circuits.
        """
        ws = await self._repo.resolve_owner_row(owner_id)
        if ws is None or not ws.company_id:
            raise OrgNotProvisionedError(f"Owner {owner_id} has no active Paperclip company yet — caller should retry.")
        if user_id in ws.member_user_ids:
            logger.info("paperclip_provisioning.provision_member: member %s already joined %s", user_id, owner_id)
            return ws
        company_id = ws.company_id
        identity = await self._repo.get_identity(user_id)

        # Sign-up fence (same claim ``provision_org`` takes): N concurrent
        # dispatches for a joining member would otherwise each sign up the
        # SAME email with its OWN password — Better Auth's ``user.email`` is
        # not unique, so every sign-up mints a duplicate account and the last
        # writer stores a password that matches none of them.
        member_claim_id: str | None = None
        if identity is None or identity.status == "seeding" or _is_credential_less_failed(identity):
            member_claim_id = uuid4().hex
            claimed = await self._repo.try_acquire_seed_claim(user_id, claim_id=member_claim_id)
            if not claimed:
                put_metric("paperclip.provision.member_claim_lost")
                return await self._await_concurrent_seed(
                    owner_id=owner_id,
                    owner_user_id=user_id,
                    what="provision_member",
                )

        adopting_existing = identity is not None and bool(identity.paperclip_user_id)
        member_password = (
            decrypt(identity.paperclip_password_encrypted) if adopting_existing else secrets.token_urlsafe(32)
        )
        from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session

        try:
            # 1. The member's Better Auth account + session.
            if adopting_existing:
                member_auth_email = _auth_email(identity, email)
                member_signin = await self._admin.sign_in_user(email=member_auth_email, password=member_password)
                member_session_cookie = member_signin["_session_cookie"]
            else:
                member_signup, member_auth_email, member_password = await self._sign_up_with_staged_credentials(
                    user_id=user_id, email=email, password=member_password
                )
                member_session_cookie = member_signup["_session_cookie"]
                await self._repo.put_identity(
                    PaperclipIdentity(
                        user_id=user_id,
                        paperclip_user_id=member_signup["user"]["id"],
                        paperclip_password_encrypted=encrypt(member_password),
                        paperclip_email=member_auth_email,
                    )
                )

            # 2+3. The admin invites the member as an owner (without
            #    ``human_role="owner"`` Paperclip defaults to ``operator``,
            #    which cannot create agents); the member accepts.
            #    409 = "You already belong to this company": the join is
            #    already done — record it rather than failing forever.
            admin_cookie = await get_admin_session_cookie(self._admin._http)
            try:
                invite = await self._admin.create_invite(
                    session_cookie=admin_cookie, company_id=company_id, email=email, human_role="owner"
                )
            except PaperclipApiError as e:
                if e.status_code != 401:
                    raise
                # Admin session expired; retry once with a fresh one.
                invalidate_admin_session()
                admin_cookie = await get_admin_session_cookie(self._admin._http)
                invite = await self._admin.create_invite(
                    session_cookie=admin_cookie, company_id=company_id, email=email, human_role="owner"
                )
            try:
                await self._accept_invite_and_ensure_approved(
                    member_session_cookie=member_session_cookie,
                    approver_session_cookie=admin_cookie,
                    company_id=company_id,
                    invite_token=invite["token"],
                )
            except PaperclipApiError as e:
                if e.status_code != 409:
                    raise
                logger.info(
                    "paperclip_provisioning.provision_member: %s already belongs to company %s",
                    user_id,
                    company_id,
                )

            # 4. Record the join on the workspace row.
            await self._repo.add_member(owner_id, user_id)
            ws.member_user_ids.add(user_id)
            logger.info(
                "paperclip_provisioning.provision_member: member %s added to owner_id %s (company=%s)",
                user_id,
                owner_id,
                company_id,
            )
            return ws

        except Exception as e:
            await self._mark_failed(
                user_id=user_id,
                reason=f"provision_member failed: {e}",
                seed_claim_id=member_claim_id,
            )
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

    async def archive_member(self, *, user_id: str, org_id: str) -> None:
        """Remove ``user_id`` from workspace ``org_id``'s Paperclip company —
        and ONLY that one: a human in two workspaces keeps the other.

        Used by ``organizationMembership.deleted`` and the account wipe. The
        DDB membership is dropped FIRST (it is the Teams authorization fence),
        then Paperclip's membership row is archived (Paperclip reassigns the
        member's issues). A 5xx from the archive re-raises so the webhook can
        enqueue a retry; the retry needs only the workspace + identity rows,
        which this never touches. A membership Paperclip no longer lists is
        logged, not raised: the user is already gone from Clerk.
        """
        ws = await self._repo.get_workspace(org_id)
        if ws is None:
            return
        await self._repo.remove_member(org_id, user_id)
        identity = await self._repo.get_identity(user_id)
        if not ws.company_id or identity is None or not identity.paperclip_user_id:
            return

        from core.services.paperclip_admin_session import (
            get_admin_session_cookie,
            invalidate_admin_session,
        )

        admin_cookie = await get_admin_session_cookie(self._admin._http)
        try:
            members_resp = await self._admin.list_members(session_cookie=admin_cookie, company_id=ws.company_id)
        except PaperclipApiError as e:
            if e.status_code != 401:
                raise
            invalidate_admin_session()
            admin_cookie = await get_admin_session_cookie(self._admin._http)
            members_resp = await self._admin.list_members(session_cookie=admin_cookie, company_id=ws.company_id)

        member_id = next(
            (m.get("id") for m in _member_rows(members_resp) if m.get("principalId") == identity.paperclip_user_id),
            None,
        )
        if member_id is None:
            logger.warning(
                "paperclip_provisioning.archive_member: no member row for user=%s paperclip_user=%s in company=%s",
                user_id,
                identity.paperclip_user_id,
                ws.company_id,
            )
            return

        try:
            await self._admin.archive_member(session_cookie=admin_cookie, company_id=ws.company_id, member_id=member_id)
        except PaperclipApiError as e:
            if e.status_code != 401:
                raise
            invalidate_admin_session()
            admin_cookie = await get_admin_session_cookie(self._admin._http)
            await self._admin.archive_member(session_cookie=admin_cookie, company_id=ws.company_id, member_id=member_id)
        logger.info(
            "paperclip_provisioning.archive_member: user %s archived from org %s (company=%s, member=%s)",
            user_id,
            org_id,
            ws.company_id,
            member_id,
        )

    async def disable_workspace(self, *, org_id: str, grace_days: int = 30) -> None:
        """Mark a workspace row ``disabled`` with a purge timer (the Clerk org
        was deleted). ``purge`` deletes the row after the grace window."""
        if await self._repo.get_workspace(org_id) is None:
            return
        purge_at = datetime.now(timezone.utc) + timedelta(days=grace_days)
        await self._repo.update_status(org_id, status="disabled", scheduled_purge_at=purge_at)
        logger.info(
            "paperclip_provisioning.disable_workspace: %s scheduled for purge at %s", org_id, purge_at.isoformat()
        )

    async def purge(self, *, org_id: str) -> None:
        """Delete a disabled workspace row whose grace window has elapsed.

        The Paperclip company itself is left alone: orphan companies are cheap
        rows in a shared Aurora cluster, and an out-of-band sweep can archive
        them. Identity rows are untouched — a human's identity outlives any
        one workspace.
        """
        await self._repo.delete(org_id)
        logger.info("paperclip_provisioning.purge: workspace %s purged", org_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_workspace(self, org_id: str) -> Optional[PaperclipWorkspace]:
        """Strongly-consistent read of the workspace row — never
        false-negatives on a just-written row."""
        return await self._repo.get_workspace(org_id, consistent_read=True)

    async def find_org_owner(self, owner_id: str) -> Optional[PaperclipWorkspace]:
        """The workspace's row iff it is ``active`` (a company exists to join)."""
        return await self._repo.resolve_owner_row(owner_id)

    async def _await_concurrent_seed(
        self,
        *,
        owner_id: str,
        owner_user_id: str,
        what: str = "provision_org",
    ) -> PaperclipWorkspace:
        """Loser side of the ``try_acquire_seed_claim`` fence: a peer on
        another backend instance is signing this human up RIGHT NOW. Wait
        briefly for its result — the ACTIVE workspace (``provision_org``) or
        this human's recorded join (``provision_member``) — and adopt it.

        Two non-adopt outcomes, both raising the 409-shaped
        ``PaperclipApiError`` the caller already treats as "in flight —
        no-op": the peer FAILED (a ``failed`` identity, or for
        ``provision_org`` a ``failed`` workspace — the next poll's ordinary
        retry path owns recovery), or the wait timed out.
        """
        for _ in range(_SEED_WAIT_ATTEMPTS):
            await asyncio.sleep(_SEED_WAIT_INTERVAL_S)
            ws = await self._repo.get_workspace(owner_id, consistent_read=True)
            if (
                ws is not None
                and ws.status == "active"
                and (what == "provision_org" or owner_user_id in ws.member_user_ids)
            ):
                logger.info(
                    "paperclip_provisioning.%s: adopted concurrent seed for owner_id %s (user=%s)",
                    what,
                    owner_id,
                    owner_user_id,
                )
                return ws
            identity = await self._repo.get_identity(owner_user_id, consistent_read=True)
            if (identity is not None and identity.status == "failed") or (
                what == "provision_org" and ws is not None and ws.status == "failed"
            ):
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

    async def _seed_starter_routines(
        self, *, company_id: str, session_cookie: str, starter_agent_ids: dict, owner_id: str
    ) -> None:
        """Best effort: a failed routine seed never fails the provision. Idempotent by
        (agent, title), so a reconcile re-run creates only what is missing."""
        for starter_key, routines in STARTER_ROUTINES.items():
            try:
                await create_agent_routines(
                    admin=self._admin,
                    session_cookie=session_cookie,
                    company_id=company_id,
                    agent_id=starter_agent_ids[starter_key],
                    routines=routines,
                    paused=True,
                )
            except Exception as e:  # a missing routine is never worth failing the workspace
                logger.warning(
                    "paperclip_provisioning.provision_org: starter routines for %s failed for %s: %s",
                    starter_key,
                    owner_id,
                    e,
                )

    async def _mark_failed(
        self,
        *,
        user_id: str,
        reason: str,
        seed_claim_id: str | None,
        org_id: str | None = None,
    ) -> None:
        """Leave the failure durably visible, never clobbering a success.

        * The human's sign-up claim (when this attempt took one) goes
          ``failed`` so the next attempt can reclaim it — a no-op once their
          identity is confirmed, or when a newer holder stole the claim.
        * For ``provision_org`` (``org_id`` given) the WORKSPACE row goes
          ``failed`` — in place when it exists (skipped when a concurrent
          attempt already made it ``active``), else via a put-if-absent marker
          so ``/teams`` answers an honest 503 instead of an eternal 202.
        """
        truncated = reason[:1000]
        if seed_claim_id is not None:
            await self._repo.fail_signup_claim(user_id, reason=truncated, claim_id=seed_claim_id)
        if org_id is None:
            return
        # Strongly consistent: this can run milliseconds after the row's
        # first write, and a replica-lagged miss would fall through to the
        # put-if-absent marker, which then fails its condition.
        if await self._repo.get_workspace(org_id, consistent_read=True) is not None:
            landed = await self._repo.update_status(
                org_id, status="failed", last_error=truncated, not_if_status="active"
            )
        else:
            landed = await self._repo.put_failed_marker_if_absent(org_id=org_id, reason=truncated)
        if landed:
            logger.warning("paperclip_provisioning._mark_failed: workspace %s marked failed: %s", org_id, truncated)
        else:
            logger.info(
                "paperclip_provisioning._mark_failed: workspace %s not marked failed (active, or a real row "
                "appeared concurrently)",
                org_id,
            )


def _member_rows(members_resp) -> list[dict]:
    """``list_members`` answers a bare list or a ``{members: [...]}`` envelope."""
    if isinstance(members_resp, list):
        rows = members_resp
    elif isinstance(members_resp, dict):
        rows = members_resp.get("members") or members_resp.get("items") or []
    else:
        rows = []
    return [m for m in rows if isinstance(m, dict)]
