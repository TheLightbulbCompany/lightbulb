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
from core.services.container_provision import background_provision
from core.services.paperclip_owner_email import lookup_owner_email
from core.services.provision_gate import evaluate_provision_gate

logger = logging.getLogger(__name__)


async def _get_paperclip_provisioning():
    """Lazy import to avoid a router→service→router cycle.

    routers/webhooks.py owns the provisioning factory (httpx client
    lifetime is attached to the returned instance via ``_http_client``
    for cleanup by ``_close_paperclip_http``). We reuse its accessor so
    there's exactly one place that constructs the chain.
    """
    from routers.webhooks import _get_paperclip_provisioning as _factory

    return await _factory()


async def _close_paperclip_http(provisioning) -> None:
    """Close the httpx client attached by ``_get_paperclip_provisioning``."""
    from routers.webhooks import _close_paperclip_http as _close

    await _close(provisioning)


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
    )

    # Container provisioning is dispatched, not awaited. /container/status
    # polling (~3s cadence) drives the user from "provisioning" to "ready".
    asyncio.create_task(background_provision(owner_id, auth.user_id))


async def _ensure_paperclip_for_owner(*, owner_id: str, owner_user_id: str, owner_email: str) -> None:
    """Ensure a Paperclip company exists for this owner.

    Idempotent — ``provisioning.provision_org`` short-circuits on an
    existing active row (see ``core/services/paperclip_provisioning.py``
    ``provision_org`` docstring).

    Resolves ``owner_email`` via ``lookup_owner_email`` when not
    supplied on the AuthContext (e.g. retry callers without a JWT
    email claim). Personal context: ``owner_id == owner_user_id`` so
    we pass ``org_id=None`` to the resolver and let it fall back to
    the user_id row. Org admin context: ``owner_id`` is the org_id
    and is passed for log context.
    """
    if not owner_email:
        owner_email = (
            await lookup_owner_email(
                org_id=owner_id if owner_id != owner_user_id else None,
                fallback_user_id=owner_user_id,
            )
            or ""
        )
    if not owner_email:
        logger.warning(
            "ensure_user_ready: no email for owner=%s — paperclip provision skipped",
            owner_id,
        )
        return

    provisioning = await _get_paperclip_provisioning()
    try:
        await provisioning.provision_org(
            owner_id=owner_id,
            owner_user_id=owner_user_id,
            owner_email=owner_email,
        )
    finally:
        await _close_paperclip_http(provisioning)


async def _ensure_paperclip_for_member(*, org_id: str, member_user_id: str, member_email: str) -> None:
    """Org-member fast path. Implemented in Task 4."""
    raise NotImplementedError("Task 4")
