"""`sessions.patch {permissionMode}` must never reach the container.

This is a privilege-escalation vector that the session-key ownership guard
structurally cannot catch, because the caller patches THEIR OWN session — every
ownership check passes.

OpenClaw maps the field straight onto core-tool policy
(fork src/agents/session-permission-exec-mode.ts):

    full -> execMode "full", workspaceOnly false,
            applyPatchWorkspaceOnly false, bypassHostApprovalFloors true

`sessions.patch` requires `operator.admin` to write it, and our proxy supplies
exactly that on every member's behalf. So on a shared per-owner container, any
org member can hand-craft one frame and get exec outside their workspace with
approval floors bypassed — i.e. read access to every other member's
`workspace-*` on the shared EFS mount, and to the container environment, which
is where a BYO LLM key lives.

Nothing legitimately sends this: `grep -rn permissionMode` across the frontend,
shared, mobile and backend returns only an unrelated ACP block in
core/containers/config.py. A blanket reject costs us nothing.
"""

from unittest.mock import AsyncMock, patch

import pytest

from routers import websocket_chat


def _sent(mgmt: AsyncMock) -> list[dict]:
    return [c.args[1] for c in mgmt.send_message.call_args_list]


async def _guard(method: str, params: dict | None):
    mgmt = AsyncMock()
    with patch.object(websocket_chat, "management_api", mgmt):
        consumed = await websocket_chat._guard_session_key_ownership(
            user_id="user_1",
            method=method,
            params=params,
            connection_id="conn_1",
            req_id="req_1",
        )
    return consumed, mgmt


_OWN_KEY = "agent:agent_a:user_1:11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_permission_mode_rejected_on_the_callers_own_session():
    """The ownership check passes — that is precisely the problem."""
    assert websocket_chat._user_owns_session_key(_OWN_KEY, "user_1") is True
    consumed, mgmt = await _guard("sessions.patch", {"key": _OWN_KEY, "permissionMode": "full"})
    assert consumed is True, "permissionMode must never be forwarded"
    res = _sent(mgmt)[0]
    assert res["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "workspace", "guarded", "read-only", "anything"])
async def test_every_permission_mode_value_is_rejected(mode):
    """Reject the FIELD, not a blocklist of values. `full` is today's dangerous
    value; the enum is upstream's to change, and a value-based guard would go
    quietly stale on the next base bump."""
    consumed, _ = await _guard("sessions.patch", {"key": _OWN_KEY, "permissionMode": mode})
    assert consumed is True, f"permissionMode={mode!r} was forwarded"


@pytest.mark.asyncio
async def test_ordinary_sessions_patch_still_works():
    """Renaming your own session is the common path and must not regress."""
    consumed, _ = await _guard("sessions.patch", {"key": _OWN_KEY, "label": "Q3 pricing"})
    assert consumed is False


@pytest.mark.asyncio
async def test_a_foreign_key_is_still_rejected_by_the_ownership_check():
    foreign = "agent:agent_a:user_2:22222222-2222-4222-8222-222222222222"
    consumed, _ = await _guard("sessions.patch", {"key": foreign, "label": "nice try"})
    assert consumed is True


@pytest.mark.asyncio
async def test_permission_mode_rejected_even_with_a_foreign_key():
    """Both guards fire; the request must not slip through either way."""
    foreign = "agent:agent_a:user_2:22222222-2222-4222-8222-222222222222"
    consumed, _ = await _guard("sessions.patch", {"key": foreign, "permissionMode": "full"})
    assert consumed is True
