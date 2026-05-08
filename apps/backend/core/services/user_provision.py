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

from core.auth import AuthContext
from core.observability.metrics import put_metric
from core.services.container_provision import background_provision
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.provision_gate import evaluate_provision_gate

logger = logging.getLogger(__name__)


async def ensure_user_ready(auth: AuthContext) -> None:
    """Single canonical readiness entry point. Idempotent.

    Flow:
      1. Resolve readiness gate via evaluate_provision_gate. If a gate
         is up, return — pre-conditions not satisfied. (No exception:
         this is a normal "not ready yet" outcome.)
      2. For owner roles (personal user OR org admin):
           a. Synchronously ensure Paperclip company (~2-3s).
           b. Dispatch container provisioning as a background task.
      3. For org members:
           If owner's Paperclip company exists & active → add member.
           Else log info and return. Member's pending state is
           implicit; resolves on the next /container/status poll
           once owner is ready.
    """
    owner_id = auth.org_id if auth.is_org_context else auth.user_id

    # The gate check uses the same predicate as /container/status so
    # the helper and the endpoint can never disagree.
    gate = await evaluate_provision_gate(
        owner_id=owner_id,
        clerk_user_id=auth.user_id,
        is_admin=auth.is_org_admin if auth.is_org_context else True,
    )
    if gate is not None:
        logger.debug(
            "ensure_user_ready: gate up for owner=%s code=%s — no-op",
            owner_id,
            gate.code,
        )
        return

    if auth.is_org_context and not auth.is_org_admin:
        # Org member fast path. Implementation in Task 4.
        await _ensure_paperclip_for_member(
            org_id=auth.org_id,
            member_user_id=auth.user_id,
            member_email=auth.email or "",
        )
        return

    # Owner path (personal user OR org admin).
    # Implementation in Tasks 3 + 5.
    await _ensure_paperclip_for_owner(
        owner_id=owner_id,
        owner_user_id=auth.user_id,
        owner_email=auth.email or "",
        org_id=auth.org_id if auth.is_org_context else None,
    )

    # Container provisioning is dispatched, not awaited. /container/status
    # polling (~3s cadence) drives the user from "provisioning" to "ready".
    asyncio.create_task(background_provision(owner_id, auth.user_id))


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
        await provisioning.provision_org(
            owner_id=owner_id,
            owner_user_id=owner_user_id,
            owner_email=owner_email,
        )
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
    "fetch the org's company row" helper at the module level — see
    Task 4 verification (Step 4.1). Instead we reuse
    ``PaperclipProvisioning._find_org_owner``, which already filters to
    ``status="active"`` rows and is the same lookup ``provision_member``
    itself uses internally. Returning ``None`` from that helper means
    the owner's company is either missing or not yet active —
    indistinguishable for our purposes (both → return gracefully).
    """
    provisioning = await build_paperclip_provisioning()
    try:
        owner_row = await provisioning._find_org_owner(org_id)
        if owner_row is None:
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

        owner_email = await lookup_owner_email(org_id=org_id, fallback_user_id=None) or ""

        await provisioning.provision_member(
            owner_id=org_id,
            user_id=member_user_id,
            email=member_email,
            owner_email=owner_email,
        )
    finally:
        await close_paperclip_provisioning(provisioning)
