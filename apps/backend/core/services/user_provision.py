"""Workspace readiness helpers.

``provision_new_workspace`` is the ONE door that starts a new person's first
provision: the Clerk ``user.created`` path calls it, and it raises on a
failure so Svix redelivers the event (redelivery is also the retry for a provision that failed
later). ``GET /container/status`` does not call it -- it is a cold read.

Every helper here is idempotent -- every call is safe to repeat.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id
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
    """Create the caller's tenant's unarmed trial once: an org's, or a signed-in person's own.

    The conditional put selects one creator across members and backend instances.
    Existing trial analytics describe row creation; runtime readiness arms its clock.
    A ``workspace_id`` other than the caller's tenant is a 403.
    """
    if resolve_owner_id(auth) != workspace_id:
        raise HTTPException(status_code=403, detail="Workspace does not match the caller's tenant")
    await _ensure_trial_row(workspace_id, actor_user_id=auth.user_id)


async def _ensure_trial_row(workspace_id: str, actor_user_id: str | None = None) -> None:
    """The trial-row body of ``ensure_workspace_trial_default``, after its auth check.

    ``actor_user_id`` is the human behind this door (the person on the ``user.created``
    path, the caller on a status poll): the trial events are captured on THEIR
    PostHog person, with ``workspace_id`` as a property.
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
        created = await billing_repo.create_trial_local_if_absent(owner_id=workspace_id, trial_end=None)
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
            properties={**source, "workspace_id": workspace_id},
            event_uuid=str(uuid.uuid5(_TRIAL_STARTED_NAMESPACE, f"{workspace_id}:{utc_today()}")),
        )


async def provision_new_workspace(owner_id: str, created_by: str | None = None) -> None:
    """Start a new tenant's first provision (the person's own id; the Clerk ``user.created`` path).

    Creates the trial row and dispatches. No billing gate: a workspace on the shared pool costs
    nothing until it runs, and every door that spends (chat, orb, phone) has its own gate. A
    workspace already ``running`` is left alone, so a redelivered event is a no-op on a healthy
    workspace and a retry on a failed one. Anything else RAISES: this webhook is the only thing
    that starts a first provision, so a failure must make Svix redeliver the event.
    """
    try:
        await _ensure_trial_row(owner_id, actor_user_id=created_by)
        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        if row.get("status") == "running":
            return
        if not await dispatch_provision(owner_id):
            raise RuntimeError(f"provision dispatch did not start for {owner_id}")
    except Exception:
        put_metric("provision.org_created.error")
        raise


def ceo_agent_id(row) -> str | None:
    """The workspace's CEO: the starter CEO, else a legacy roster's first seeded agent."""
    return (row.starter_agent_ids or {}).get("ceo") or (row.seeded_agent_ids or [None])[0]


async def _ensure_owner_workspace(*, owner_id: str, owner_user_id: str) -> None:
    """Provision the workspace with ``owner_user_id`` as provisioner, then, the first time it
    goes active, create ``#general``."""
    before = await workspace_provisioning._repo().get_workspace(owner_id, consistent_read=True)
    await workspace_provisioning.provision_org(owner_id, provisioner=owner_user_id)
    if before is not None and before.status == "active":
        return
    from core.services.messaging import ensure_default_room

    try:
        await ensure_default_room(owner_id, owner_user_id)
    except Exception:  # noqa: BLE001 — partial retries reconcile again
        logger.exception("ensure_user_ready: default room creation failed for owner=%s", owner_id)
