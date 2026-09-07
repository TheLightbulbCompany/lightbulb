"""The pool's connect must declare every capability the gateway gates on.

OpenClaw's `canDeliverApprovals` (fork src/gateway/server-request-context.ts)
requires an approval scope AND proof that the client renders the approval kind:
`internal.approvalRuntime`, a client id in one of its hardcoded sets
(control-ui / macos / ios / android / tui), or an explicit capability. We connect
as `gateway-client`, which is in none of those sets, so without the `approvals`
cap we match nothing.

The failure is worse than a missing event: with no approval route, the record is
`manager.expire(id, "no-approval-route")` and the agent receives `decision: null`
immediately. That covers plugin and system-agent approvals, which our frontend
subscribes to (hooks/useExecApprovals.tsx). Exec approvals are moot by design —
`tools.exec.mode` is "full", so none is ever created.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnection


@pytest.mark.asyncio
async def test_handshake_declares_the_approvals_capability():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._ws = AsyncMock()
    conn.user_id = "u_1"
    conn.token = "t_1"

    with patch("core.gateway.connection_pool.perform_signed_connect", new_callable=AsyncMock) as signed:
        await conn._handshake()

    caps = signed.await_args.kwargs["caps"]
    assert "approvals" in caps, (
        "without the approvals cap, canDeliverApprovals matches nothing for "
        "client id 'gateway-client' and every plugin/system-agent approval is "
        "expired with decision: null before the owner ever sees it"
    )


@pytest.mark.asyncio
async def test_handshake_still_declares_tool_events():
    """tool-events is load-bearing — the gateway silently drops stream:"tool"
    events without it (agent-turn/principal.ts, chat-send-agent-dispatch.ts)."""
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._ws = AsyncMock()
    conn.user_id = "u_1"
    conn.token = "t_1"

    with patch("core.gateway.connection_pool.perform_signed_connect", new_callable=AsyncMock) as signed:
        await conn._handshake()

    assert "tool-events" in signed.await_args.kwargs["caps"]
