"""Task 26: relay_hooks' two OpenClaw-shaped hooks (approve_pending_node_surface's RPC
chain, and the tools-deny-list config patch) must no-op for an AgentCore workspace --
neither exists there. `publish_node_invalidation` is transport-agnostic and must still run.

The brief's own reference tests monkeypatch the guard (`_is_agentcore`) with a plain sync
lambda, but the real guard has to read the owner's container row (there is no way to know
"is this owner on AgentCore" from `owner_id` alone without a lookup), so it is async and
must be awaited at the call site. These tests fake it accordingly (an async function/
AsyncMock, never a bare `lambda: bool`) rather than the brief's sync-lambda snippet.

M2-R4: the AgentCore-owner test proves the guard returns BEFORE any `ConnectionService`
call by making `count_relay_nodes` raise -- if the guard did not short-circuit, this test
would fail with that exception instead of asserting on `called`/`published`.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import relay_hooks
from core.services.connection_service import ConnectionService


@pytest.mark.asyncio
async def test_an_agentcore_owner_skips_both_openclaw_shaped_hooks():
    called: list[str] = []
    published: list[str] = []
    with (
        patch(
            "core.services.relay_hooks.approve_pending_node_surface",
            new_callable=AsyncMock,
            side_effect=lambda **kw: called.append("approve"),
        ),
        patch(
            "core.services.relay_hooks.patch_openclaw_config",
            new_callable=AsyncMock,
            side_effect=lambda *a, **kw: called.append("patch"),
        ),
        patch("core.services.relay_hooks._is_agentcore", new_callable=AsyncMock, return_value=True),
        patch(
            "core.services.relay_hooks.publish_node_invalidation",
            new_callable=AsyncMock,
            side_effect=lambda owner, user: published.append(owner),
        ),
        # M2-R4: proves the guard returns before any ConnectionService call -- a live
        # DynamoDB call here would fail the test instead of just going unused.
        patch.object(ConnectionService, "count_relay_nodes", AsyncMock(side_effect=RuntimeError("must not be called"))),
    ):
        await relay_hooks.on_node_ready("org_1", "user_1", "dev_1")
        await relay_hooks.on_node_closed("org_1", "user_1")
    assert called == []  # no container RPC, no EFS config patch
    assert published == ["org_1", "org_1"]  # the UI still learns the node came and went


@pytest.mark.asyncio
async def test_an_ecs_owner_is_unchanged():
    called: list[str] = []
    with (
        patch(
            "core.services.relay_hooks.approve_pending_node_surface",
            new_callable=AsyncMock,
            side_effect=lambda **kw: called.append("approve"),
        ),
        patch("core.services.relay_hooks._is_agentcore", new_callable=AsyncMock, return_value=False),
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
        patch.object(ConnectionService, "count_relay_nodes", AsyncMock(return_value=0)),
    ):
        await relay_hooks.on_node_ready("org_1", "user_1", "dev_1")
    assert called == ["approve"]


@pytest.mark.asyncio
async def test_is_agentcore_defers_to_runtime_transport():
    """The guard itself is a thin, awaited wrapper over the shared owner-id lookup added in
    runtime_transport.py (M2-R9) -- not a reimplementation."""
    with patch(
        "core.services.relay_hooks.runtime_transport.is_agentcore_owner", new_callable=AsyncMock, return_value=True
    ) as lookup:
        assert await relay_hooks._is_agentcore("org_1") is True
    lookup.assert_awaited_once_with("org_1")
