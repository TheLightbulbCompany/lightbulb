"""Per-connection side effects of the node relay; safe to call from the handler that owns
the socket.

All that is left is the live-event invalidation a node's arrival/departure publishes. The
OpenClaw-shaped edges this module carried are gone with that runtime: the operator wake gate
(the operator is on the HTTP facade now, where `chat_gate` runs in the request path), the
expired-trial self-disarm that hung off an operator connect, the pending-surface approval RPC
into a container gateway, and the first-node/last-node `tools.deny` patch on `openclaw.json` —
the broker's `commands` allowlist (`node_broker.NodeSession`, built by the relay at connect
time) is what replaces that flip on this transport (spec §4.4).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def publish_node_invalidation(owner_id: str, user_id: str) -> None:
    # Lazy imports deliberately kept (not hoisted to module top): avoids an
    # import-cycle risk between relay_hooks and live_events/messaging.
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    try:
        await get_live_event_publisher().publish_user(
            owner_id,
            user_id,
            make_live_event(
                "runtime.resource.invalidated",
                {"resource": "health", "resourceId": "desktop-node"},
            ),
        )
    except Exception:
        logger.warning("desktop-node invalidation publish failed", exc_info=True)


async def on_node_ready(owner_id: str, user_id: str) -> None:
    """A node came up."""
    await publish_node_invalidation(owner_id, user_id)


async def on_node_closed(owner_id: str, user_id: str) -> None:
    """A node went away."""
    await publish_node_invalidation(owner_id, user_id)
