"""Orchestrator for provisioning Paperclip on Clerk webhook events.

Three entry points (all async, all idempotent on user_id / org_id):

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

Failure semantics: any failure mid-provisioning calls
``_mark_failed`` to leave a row with ``status="failed"`` and
``last_error`` set so the caller (or a retry path) can observe it,
then re-raises. Subsequent retries with the same args are safe — the
``status="active"`` short-circuit is the only path that skips work.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.encryption import decrypt, encrypt
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import service_token
from core.services.paperclip_adapter_config import OPENCLAW_GATEWAY_TYPE, synthesize_openclaw_adapter
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError

logger = logging.getLogger(__name__)


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
        org_id: str,
        owner_user_id: str,
        owner_email: str,
    ) -> PaperclipCompany:
        """Provision the Paperclip company + Better Auth user for a new owner.

        Paperclip's ``POST /api/companies`` requires
        ``req.actor.isInstanceAdmin`` in ``authenticated`` deployment mode
        (per ``paperclip/server/src/routes/companies.ts:269``). The
        old flow signed up the new user and immediately tried to create
        the company AS that user — which 403'd because the new user
        isn't an admin. Fixed flow: the bootstrap admin (admin@isol8.co,
        the only ``instance_admin`` on the deployment) creates the
        company and invites the new user as a co-owner via the standard
        invite/accept/approve chain. New user ends up as an active
        owner; admin stays as a co-owner of every company (harmless
        side effect — admin doesn't act without explicit code paths).

        Service-token contract: the seeded Main Agent's
        ``adapterConfig.authToken`` is a JWT whose ``sub`` claim MUST
        equal the container's ``owner_id``, because the WS Lambda
        authorizer uses ``sub`` to route the connection to that owner's
        OpenClaw container. ``owner_id`` is the org_id when an org owns
        the container; for personal users ``owner_id == user_id`` and
        ``paperclip_autoprovision.ensure_paperclip_workspace`` passes
        ``org_id == clerk_user_id``, so minting with ``org_id`` is also
        correct for the personal path (it collapses to ``user_id``).

        Idempotent: if a row already exists for ``owner_user_id`` with
        ``status="active"``, return it unchanged. Other statuses redo
        the chain — Paperclip's Better Auth sign-up is NOT idempotent,
        so retrying after a partial failure will hit "user already
        exists" at sign_up_user. The proxy's failed-row handler
        redirects back to /chat for explicit re-handshake instead of
        looping autoprovisions.
        """
        existing = await self._repo.get(owner_user_id)
        if existing is not None and existing.status == "active":
            logger.info(
                "paperclip_provisioning.provision_org: owner %s already active for org %s",
                owner_user_id,
                org_id,
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

        password = secrets.token_urlsafe(32)
        try:
            # 1. Better Auth public signup — creates the new Paperclip
            #    user. Doesn't require admin privileges (unless
            #    ``disableSignUp`` is set, which it isn't on this
            #    deployment). Returns a session token we'll use later
            #    for the invite-accept and Main Agent steps.
            signup = await self._admin.sign_up_user(
                email=owner_email,
                password=password,
                name=owner_email,
            )
            paperclip_user_id = signup["user"]["id"]
            new_user_session_cookie = signup["_session_cookie"]

            # 2. ADMIN creates the company. Admin becomes the initial
            #    owner via Paperclip's auto-ensureMembership in
            #    ``routes/companies.ts:273``.
            try:
                company = await self._admin.create_company(
                    name=owner_email,
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
                        name=owner_email,
                        description="Isol8 Teams workspace",
                        session_cookie=admin_cookie,
                        idempotency_key=owner_user_id,
                    )
                else:
                    raise
            company_id = company["id"]

            # 3. ADMIN creates a one-shot invite for the new user.
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
            invite = await self._admin.create_invite(
                session_cookie=admin_cookie,
                company_id=company_id,
                email=owner_email,
                human_role="owner",
            )
            invite_token = invite["token"]

            # 4. New user accepts the invite using their own session.
            #    Produces a join-request bound to their Better Auth user.
            accept = await self._admin.accept_invite(
                session_cookie=new_user_session_cookie,
                invite_token=invite_token,
            )
            try:
                request_id = accept["id"]
            except (KeyError, TypeError) as e:
                raise PaperclipApiError(
                    "accept_invite response missing 'id' field",
                    status_code=200,
                    body=accept,
                ) from e

            # 5. ADMIN approves the join request → user is now an
            #    active owner of the company.
            await self._admin.approve_join_request(
                session_cookie=admin_cookie,
                company_id=company_id,
                request_id=request_id,
            )

            # 6. Mint a long-lived OpenClaw service-token JWT. Baked
            #    into the seeded Main Agent's adapter config so the
            #    agent can reach the user's container via the gateway.
            #
            #    The JWT's ``sub`` claim MUST equal the container's
            #    ``owner_id`` — the WS Lambda authorizer extracts ``sub``
            #    and routes the connection to that owner's container.
            #    For org context, ``owner_id == org_id``; for the
            #    personal autoprovision path the caller passes
            #    ``org_id == clerk_user_id``, so this also collapses to
            #    the right value (``user_id``). Previously this minted
            #    with ``owner_user_id`` (the human creator's user_id),
            #    which broke org wakes: the authorizer then tried to
            #    route to a user-personal container that doesn't exist
            #    for org members.
            svc_token = service_token.mint(org_id)

            # 7. Best-effort seed a "Main Agent" pointing at the
            #    user's OpenClaw container. Failure here is not fatal
            #    — the company is still usable, the user can create
            #    agents from the UI manually. Acts AS the user (their
            #    session) so the agent ends up owned by them.
            try:
                await self._admin.create_agent(
                    company_id=company_id,
                    name="Friday",
                    role="ceo",
                    adapter_type=OPENCLAW_GATEWAY_TYPE,
                    adapter_config=synthesize_openclaw_adapter(
                        gateway_url=_ws_gateway_url(self._env_name),
                        service_token=svc_token,
                        user_id=owner_user_id,
                    ),
                    session_cookie=new_user_session_cookie,
                    idempotency_key=f"{owner_user_id}:main-agent",
                )
            except PaperclipApiError as e:
                logger.warning(
                    "paperclip_provisioning.provision_org: seed agent failed for %s: %s",
                    owner_user_id,
                    e,
                )

            # 8. Persist
            now = datetime.now(timezone.utc)
            row = PaperclipCompany(
                user_id=owner_user_id,
                org_id=org_id,
                company_id=company_id,
                paperclip_user_id=paperclip_user_id,
                paperclip_password_encrypted=encrypt(password),
                service_token_encrypted=encrypt(svc_token),
                status="active",
                created_at=now,
                updated_at=now,
            )
            await self._repo.put(row)
            logger.info(
                "paperclip_provisioning.provision_org: org %s active (company=%s, owner=%s)",
                org_id,
                company_id,
                owner_user_id,
            )
            return row

        except Exception as e:
            await self._mark_failed(
                user_id=owner_user_id,
                org_id=org_id,
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
        org_id: str,
        user_id: str,
        email: str,
        owner_email: str,
    ) -> PaperclipCompany:
        """Add a new Isol8 member to the org's existing Paperclip company.

        ``owner_email`` is supplied by the webhook caller (Clerk's
        ``organizationMembership.created`` payload exposes it). We
        could look it up via ``user_repo`` instead, but keeping it as
        a kwarg avoids the cross-service coupling and makes the
        orchestrator easier to test in isolation.

        Service-token contract: org members share the org's container
        (keyed by ``org_id``), so the member's stored
        ``service_token_encrypted`` must be a JWT whose ``sub`` claim
        equals ``org_id`` — not the member's own user_id. The WS Lambda
        authorizer extracts ``sub`` and routes the resulting connection
        to that owner's container; minting with the member's user_id
        would route to a user-personal container that doesn't exist for
        org members.

        Idempotent on ``user_id``: an already-active row short-circuits
        to a no-op return.
        """
        existing = await self._repo.get(user_id)
        if existing is not None and existing.status == "active":
            logger.info(
                "paperclip_provisioning.provision_member: member %s already active",
                user_id,
            )
            return existing

        company_id = await self._repo.get_org_company_id(org_id)
        if not company_id:
            raise OrgNotProvisionedError(
                f"Org {org_id} has no Paperclip company yet. "
                "organization.created webhook may not have run yet — caller should retry."
            )

        owner_row = await self._find_org_owner(org_id)
        if owner_row is None:
            # Defensive — get_org_company_id returned a company_id but
            # _find_org_owner couldn't locate the row that produced it.
            # Should be impossible if the GSI is consistent.
            raise OrgNotProvisionedError(f"Org {org_id} has company {company_id} but no owner row found")
        owner_password = decrypt(owner_row.paperclip_password_encrypted)

        member_password = secrets.token_urlsafe(32)
        try:
            # 1. Sign up the new member (creates their Better Auth account).
            member_signup = await self._admin.sign_up_user(
                email=email,
                password=member_password,
                name=email,
            )
            member_paperclip_user_id = member_signup["user"]["id"]
            member_session_cookie = member_signup["_session_cookie"]

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

            # 4. Member accepts the invite, producing a pending join
            #    request bound to the member's Better Auth user.
            accept = await self._admin.accept_invite(
                session_cookie=member_session_cookie,
                invite_token=invite_token,
            )
            # Paperclip's accept_invite response shape verified against
            # ``server/src/routes/access.ts:3604-3630``:
            # ``toJoinRequestResponse(created)`` spreads the joinRequests
            # row into a flat object with a top-level ``id`` field. No
            # wrapping, no alternate casing — strict access here so any
            # future shape change fails loud instead of silently dropping
            # the approve_join_request step.
            try:
                request_id = accept["id"]
            except (KeyError, TypeError) as e:
                raise PaperclipApiError(
                    "accept_invite response missing 'id' field",
                    status_code=200,
                    body=accept,
                ) from e

            # 5. Owner approves the pending join request — flips the
            #    membership from pending_approval to active.
            await self._admin.approve_join_request(
                session_cookie=owner_session_cookie,
                company_id=company_id,
                request_id=request_id,
            )

            # 6. Mint the member's OpenClaw service-token JWT.
            #
            #    Org members share the org's container (keyed by
            #    ``org_id``), so the JWT's ``sub`` claim MUST equal
            #    ``org_id`` — that's the value the WS Lambda authorizer
            #    pulls out and routes against. Previously this minted
            #    with the joining member's own ``user_id``, which broke
            #    Paperclip wakes for org members: the authorizer would
            #    try to route to a user-personal container that doesn't
            #    exist for org members.
            svc_token = service_token.mint(org_id)

            # 7. Persist the member row.
            now = datetime.now(timezone.utc)
            row = PaperclipCompany(
                user_id=user_id,
                org_id=org_id,
                company_id=company_id,
                paperclip_user_id=member_paperclip_user_id,
                paperclip_password_encrypted=encrypt(member_password),
                service_token_encrypted=encrypt(svc_token),
                status="active",
                created_at=now,
                updated_at=now,
            )
            await self._repo.put(row)
            logger.info(
                "paperclip_provisioning.provision_member: member %s added to org %s (company=%s)",
                user_id,
                org_id,
                company_id,
            )
            return row

        except Exception as e:  # noqa: F841 — kept for clarity in future logging
            await self._mark_failed(
                user_id=user_id,
                org_id=org_id,
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

    async def _find_org_owner(self, org_id: str) -> Optional[PaperclipCompany]:
        """Find the org-owner row (oldest ACTIVE ``created_at`` for this org).

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

        Returns None if the org has no active rows.
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
                "KeyConditionExpression": Key("org_id").eq(org_id),
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

    async def _mark_failed(
        self,
        *,
        user_id: str,
        org_id: str,
        reason: str,
        company_id: str = "",
    ) -> None:
        """Persist ``status="failed"`` + ``last_error``.

        If a prior row exists we update in place (preserving
        immutables like paperclip_user_id); otherwise we write a fresh
        placeholder row so subsequent admin tooling can see the
        failure. Truncates the reason to a sane length to keep the
        DynamoDB item small.
        """
        truncated = reason[:1000]
        existing = await self._repo.get(user_id)
        if existing is not None:
            await self._repo.update_status(
                user_id,
                status="failed",
                last_error=truncated,
            )
            return

        now = datetime.now(timezone.utc)
        await self._repo.put(
            PaperclipCompany(
                user_id=user_id,
                org_id=org_id,
                company_id=company_id,
                paperclip_user_id="",
                paperclip_password_encrypted="",
                service_token_encrypted="",
                status="failed",
                created_at=now,
                updated_at=now,
                last_error=truncated,
            )
        )
