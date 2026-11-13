"""Workspace readiness helpers.

``provision_new_workspace`` is the ONE door that starts a new workspace's first
provision: the Clerk ``organization.created`` webhook calls it, and it raises on a
failure so Svix redelivers the event (redelivery is also the retry for a provision that failed
later). ``GET /container/status`` does not call it -- it is a cold read.

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
from core.services import workspace_provisioning
from core.services.provision_dispatch import dispatch_provision
from core.services.trial_cap import utc_today

logger = logging.getLogger(__name__)

# Namespace for the deterministic ``trial_started`` event uuid. A fixed,
# arbitrary uuid4 — its only job is to keep our uuid5 from colliding with any
# other uuid5 we might derive from an owner id later. Changing it would let
# every existing owner emit a second trial_started, so: don't.
_TRIAL_STARTED_NAMESPACE = uuid.UUID("6f6c9f2e-2d5b-4a1c-9a3f-3d5c8e1b7a40")


async def ensure_workspace_trial_default(auth: AuthContext, workspace_id: str) -> None:
    """Create the active workspace's unarmed trial once.

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

    Creates the trial row and dispatches. No billing gate: a workspace on the shared pool costs
    nothing until it runs, and every door that spends (chat, orb, phone) has its own gate. A
    workspace already ``running`` is left alone, so a redelivered event is a no-op on a healthy
    workspace and a retry on a failed one. Anything else RAISES: this webhook is the only thing
    that starts a first provision, so a failure must make Svix redeliver the event.
    """
    if not is_org_owner_id(org_id):
        return
    try:
        await _ensure_trial_row(org_id, actor_user_id=created_by)
        row = await container_repo.get_by_owner_id(org_id, consistent=True) or {}
        if row.get("status") == "running":
            return
        if not await dispatch_provision(org_id):
            raise RuntimeError(f"provision dispatch did not start for {org_id}")
    except Exception:
        put_metric("provision.org_created.error")
        raise


def ceo_agent_id(row) -> str | None:
    """The workspace's CEO: the starter CEO, else a legacy roster's first seeded agent."""
    return (row.starter_agent_ids or {}).get("ceo") or (row.seeded_agent_ids or [None])[0]


async def _ensure_workspace_for_org(*, org_id: str, user_id: str, is_admin: bool) -> None:
    """Join the org's active workspace, or (an admin only) provision it. Idempotent.

    CALLERS: ``scripts.provision_runner._seed_owner_workspace`` (step 0 of the provisioning
    executor, with the Clerk org CREATOR and ``is_admin=True``) and ``_reconcile_owner_seed``
    for a workspace whose container is already running. A member never provisions: an admin
    does, and the member joins on a later request.
    """
    if await workspace_provisioning._repo().resolve_owner_row(org_id) is not None:
        await _ensure_workspace_member(org_id=org_id, member_user_id=user_id)
    elif is_admin:
        await _ensure_owner_workspace(owner_id=org_id, owner_user_id=user_id)
    else:
        logger.info("ensure_user_ready (org member): org=%s not active yet — waiting for an admin", org_id)


async def _ensure_owner_workspace(*, owner_id: str, owner_user_id: str) -> None:
    """Provision the workspace with ``owner_user_id`` as provisioner, then, the first time it
    goes active, create ``#general`` and the feed room and send the owner's welcome."""
    before = await workspace_provisioning._repo().get_workspace(owner_id, consistent_read=True)
    await workspace_provisioning.provision_org(owner_id, provisioner=owner_user_id)
    if before is not None and before.status == "active":
        return
    from core.services.messaging import ensure_default_room, ensure_feed_room

    try:
        room_id = await ensure_default_room(owner_id, owner_user_id)
    except Exception:  # noqa: BLE001 — partial retries reconcile again
        room_id = None
        logger.exception("ensure_user_ready: default room creation failed for owner=%s", owner_id)
    try:
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


async def _ensure_workspace_member(*, org_id: str, member_user_id: str) -> None:
    """Record the org member on the org's active workspace. JOIN-ONLY (never creates one), so
    no provision gate may swallow it; a workspace not active yet is retried by the next request."""
    if await workspace_provisioning.provision_member(org_id, member_user_id) is None:
        logger.info(
            "ensure_user_ready (member): org=%s not active yet — membership-add for user=%s retried later",
            org_id,
            member_user_id,
        )
        put_metric("workspace.member_add", dimensions={"outcome": "no_company"})
        return
    put_metric("workspace.member_add", dimensions={"outcome": "ok"})
