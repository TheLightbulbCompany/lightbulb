"""relay_hooks' two node hooks are pure invalidation publishes.

They used to run two OpenClaw-shaped edges first -- `approve_pending_node_surface`'s
RPC chain into a container gateway, and the tools-deny-list patch on `openclaw.json`
-- behind an `_is_agentcore` guard. AgentCore is the only runtime now, so both edges
and the guard are gone; what must NOT regress is that the hooks still reach nothing
but `publish_node_invalidation`, and that they publish for BOTH edges (the UI learns
the node came and went).

`ConnectionService.count_relay_nodes` is booby-trapped here rather than merely left
unused: it is the collaborator the deleted deny-list branch called, so a reintroduced
branch fails this test with that exception instead of passing unnoticed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import relay_hooks
from core.services.connection_service import ConnectionService


@pytest.mark.asyncio
async def test_both_node_hooks_only_publish_the_invalidation():
    published: list[str] = []
    with (
        patch(
            "core.services.relay_hooks.publish_node_invalidation",
            new_callable=AsyncMock,
            side_effect=lambda owner, user: published.append(owner),
        ),
        patch.object(ConnectionService, "count_relay_nodes", AsyncMock(side_effect=RuntimeError("must not be called"))),
    ):
        await relay_hooks.on_node_ready("org_1", "user_1", "dev_1")
        await relay_hooks.on_node_closed("org_1", "user_1")
        await relay_hooks.on_node_closed("org_1", "user_1", stale_row_undeleted=True)
    assert published == ["org_1", "org_1", "org_1"]
