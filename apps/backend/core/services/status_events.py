"""``publish_status_changed`` -- realtime invalidation for the StatusContract.

See ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
§3's end-state diagram (``PUBLISH -.->|onboarding_status_changed| LOOP``) and
the ``REALTIME`` session in
``docs/superpowers/plans/2026-07-24-onboarding-implementation-prompts.md``
(spec PR-7).

Every durable write that can move a fact ``compute_status`` composes --
the ``containers`` row, the ``billing-accounts`` row, the
``paperclip-companies`` owner row (including its co-located onboarding
fields), and gateway liveness -- calls ``publish_status_changed(owner_id)``
right after the write commits. It pushes a content-free
``onboarding_status_changed`` event to every open frontend WebSocket tab for
that owner; the client's only reaction is to revalidate the existing
``/container/status`` SWR key (see ``useGateway.tsx``'s single subscription).

INV-1: the payload carries NO computed state -- ``compute_status(owner_id)``
stays the sole composer. This is a pure "go re-fetch" nudge, never a second
source of truth. Do not start attaching a phase/lifecycle string to the
payload; that would let a client render a stale push instead of a fresh
composed read.

Best-effort, fire-and-forget: a push failure (gateway pool not booted, no
open tab, a transient Management API error) must never break the write it
rides along with -- every call site treats this the same way it already
treats a metric emission, not a return-value-checked operation.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STATUS_CHANGED_EVENT = "onboarding_status_changed"


def publish_status_changed(owner_id: str | None) -> None:
    """Fire-and-forget push telling ``owner_id``'s open tabs to revalidate
    ``/container/status``.

    Safe to call from any repository or service -- the import of
    ``core.containers.get_gateway_pool`` is local to this function (not
    module-level) so this module can be imported from ``core/repositories/*``
    without joining the ``core.containers`` -> ``core.containers.ecs_manager``
    -> ``core.repositories.container_repo`` import cycle.
    """
    if not owner_id:
        return
    try:
        from core.containers import get_gateway_pool

        get_gateway_pool().broadcast_to_owner(owner_id, STATUS_CHANGED_EVENT, {})
    except Exception:  # noqa: BLE001 -- a push must never break the write it rides along with
        logger.info(
            "status_events: publish_status_changed failed for owner %s",
            owner_id,
            exc_info=True,
        )
