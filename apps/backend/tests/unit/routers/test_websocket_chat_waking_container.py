"""The RPC proxy's "container has no IP yet" answer is a RETRY CONTRACT.

An ECS task that hasn't published an IP is waking, not broken — a routine
state for a trial box that scaled to zero. Clients (mobile roster, web
sidebar) branch on the `retryable` flag to render that as loading and re-probe
themselves; without it they show a red "Couldn't load agents" failure card to a
user whose app is working. Keep the flag when editing this branch.
"""

import os

# routers.websocket_chat eagerly reads env at import.
os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://stub-management-api.local")

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mgmt_mock(monkeypatch):
    mgmt = MagicMock()
    mgmt.send_message = MagicMock(return_value=True)
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)
    monkeypatch.setattr(
        "routers.websocket_chat.get_management_api_client",
        AsyncMock(return_value=mgmt),
    )
    return mgmt


def _ecs_mock(monkeypatch, resolved):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=resolved)
    monkeypatch.setattr("routers.websocket_chat.get_ecs_manager", MagicMock(return_value=ecs))


async def _run_rpc():
    from routers.websocket_chat import _process_rpc_background

    await _process_rpc_background(
        connection_id="conn-1",
        user_id="user_a",
        owner_id="user_a",
        req_id="req-1",
        method="agents.list",
        params={},
    )


@pytest.mark.asyncio
async def test_waking_container_answers_retryable(monkeypatch):
    """A container row with no IP yet → retryable, with a wait hint."""
    _ecs_mock(monkeypatch, ({"gateway_token": "gw"}, None))
    mgmt = _mgmt_mock(monkeypatch)

    await _run_rpc()

    _, frame = mgmt.send_message.call_args.args
    assert frame["ok"] is False
    err = frame["error"]
    assert err["retryable"] is True
    assert err["retryAfterMs"] > 0
    # The message stays a plain human sentence — clients render it verbatim.
    assert "starting up" in err["message"]
    # NO `code`: already-shipped mobile builds string-join an error's `code`
    # into the text they display, so adding one would regress the installed
    # base to "...try again in a moment. — [UNAVAILABLE]" — the exact message
    # class this work exists to remove. Older clients ignore the other two.
    assert "code" not in err


@pytest.mark.asyncio
async def test_missing_container_is_not_retryable(monkeypatch):
    """No container at all is NOT a wait — provisioning owns that state, and
    flagging it retryable would spin a 'waking' spinner forever."""
    _ecs_mock(monkeypatch, (None, None))
    mgmt = _mgmt_mock(monkeypatch)

    await _run_rpc()

    _, frame = mgmt.send_message.call_args.args
    assert frame["ok"] is False
    assert frame["error"].get("retryable") is not True


@pytest.mark.asyncio
async def test_gateway_errors_are_not_dressed_up_as_waking(monkeypatch):
    """A gateway failure over a container that HAS an IP is forwarded as-is.

    An earlier revision substring-matched this error's text for "still coming
    up" markers. It was wrong three ways (see the comment at the call site);
    the load-bearing one: two of the markers were raised immediately after the
    pool emits GATEWAY_DOWN_DEAD, so a cap-killed container was advertised to
    every client as merely waking. Whether a container is coming up is
    answered by /container/status, never by matching error prose.
    """
    _ecs_mock(monkeypatch, ({"gateway_token": "gw"}, "10.0.0.5"))
    mgmt = _mgmt_mock(monkeypatch)

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("Gateway connection lost"))
    monkeypatch.setattr("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool))

    await _run_rpc()

    _, frame = mgmt.send_message.call_args.args
    assert frame["ok"] is False
    assert frame["error"].get("retryable") is not True
