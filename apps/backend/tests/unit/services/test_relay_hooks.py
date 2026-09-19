"""relay_hooks is one thing now: the live-event invalidation a node's arrival or departure
publishes, so the UI's health tile learns the desktop node came and went.

Everything else this module carried was OpenClaw-shaped and is gone with that runtime: the
operator wake gate (the operator is on the HTTP facade, where `chat_gate` runs in the request
path), the expired-trial self-disarm that hung off an operator connect, the pending-surface
approval RPC into a container gateway, and the first-node/last-node `tools.deny` patch on
`openclaw.json` -- the broker's `commands` allowlist, built by the relay at connect time, is
what replaces that flip on this transport (spec §4.4).

Two collaborators are booby-trapped rather than merely left unused: `chat_gate` and
`ConnectionService.count_relay_nodes` are what the deleted branches called, so a reintroduced
branch fails loudly here instead of passing unnoticed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import relay_hooks


@pytest.mark.asyncio
async def test_both_node_hooks_publish_the_invalidation():
    """BOTH edges, not just the arrival: a node that goes away without a publish leaves the
    health tile claiming a desktop is connected until the next full refetch."""
    published: list[tuple[str, str]] = []
    with patch(
        "core.services.relay_hooks.publish_node_invalidation",
        new_callable=AsyncMock,
        side_effect=lambda owner, user: published.append((owner, user)),
    ):
        await relay_hooks.on_node_ready("org_1", "user_1")
        await relay_hooks.on_node_closed("org_1", "user_1")

    assert published == [("org_1", "user_1"), ("org_1", "user_1")]


@pytest.mark.asyncio
async def test_the_publish_is_a_single_user_channel_event():
    publisher = AsyncMock()
    with patch("core.services.live_events.get_live_event_publisher", return_value=publisher):
        await relay_hooks.publish_node_invalidation("org_1", "user_1")

    publisher.publish_user.assert_awaited_once()
    owner, user, event = publisher.publish_user.await_args.args
    assert (owner, user) == ("org_1", "user_1")
    assert event["type"] == "runtime.resource.invalidated"
    assert event["payload"] == {"resource": "health", "resourceId": "desktop-node"}


@pytest.mark.asyncio
async def test_a_publish_failure_never_reaches_the_socket_handler():
    """The caller owns a live WebSocket. An AppSync fault must not take the connection down --
    a stale health tile is the cost, and the next refetch heals it."""
    publisher = AsyncMock()
    publisher.publish_user.side_effect = RuntimeError("appsync down")
    with patch("core.services.live_events.get_live_event_publisher", return_value=publisher):
        await relay_hooks.on_node_ready("org_1", "user_1")  # must not raise


def test_the_openclaw_shaped_edges_are_gone_not_merely_unreached():
    """Each of these called into the ECS runtime -- the container's gateway RPC, or a deep-merge
    into the owner's `openclaw.json`. A reintroduced branch would raise or silently no-op on every
    node connect in the fleet, so their ABSENCE is the assertion."""
    from core.services.connection_service import ConnectionService

    assert not hasattr(relay_hooks, "admit_operator_wake")
    assert not hasattr(relay_hooks, "WAKE_GATED_METHODS")
    assert not hasattr(relay_hooks, "chat_gate")
    assert not hasattr(relay_hooks, "approve_pending_node_surface")
    # The collaborator the deleted deny-list branch counted nodes with.
    assert not hasattr(ConnectionService, "count_relay_nodes")
