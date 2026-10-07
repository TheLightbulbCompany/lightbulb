"""Best-effort private AppSync invalidation for the composed status contract."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

STATUS_CHANGED_EVENT = "runtime.resource.invalidated"


async def publish_status_changed_now(owner_id: str) -> None:
    """Publish the nudge and wait for it. Never raises: a live nudge never breaks its caller."""
    try:
        from core.services.live_events import publish_owner_users
        from core.services.messaging import make_live_event

        event = make_live_event(STATUS_CHANGED_EVENT, {"resource": "onboarding"})
        await publish_owner_users(owner_id, event)
    except Exception:  # noqa: BLE001 — a live nudge never breaks its durable write
        logger.info("status event publish failed for owner %s", owner_id, exc_info=True)


def publish_status_changed(owner_id: str | None) -> None:
    """Tell every active owner member to refetch onboarding/container status.

    The event carries no computed state; ``compute_status`` remains authoritative.
    Safe from repository write sites and never allowed to break their commit.
    """
    if not owner_id:
        return
    try:
        asyncio.get_running_loop().create_task(publish_status_changed_now(owner_id))
    except RuntimeError:
        # Synchronous scripts/tests have no live subscribers to notify.
        return
