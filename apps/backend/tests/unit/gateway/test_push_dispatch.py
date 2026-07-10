"""Unit tests for the run-completion push dispatch.

NOTE ON DIVERGENCE FROM THE TASK BRIEF: the brief assumed a two-arg
``send_rpc(method, params)`` that returns the RPC result directly. The real
``GatewayConnection.send_rpc(req_id, method, params)`` only *sends* (returns
``None``); the response is awaited separately via ``wait_for_response(req_id)``
(see ``_fetch_and_record_usage``, the sibling pattern this dispatch mirrors).
The agents.list result shape is ``{"agents": [{"id", "name", "identity": {...}}]}``
(verified against ``agents-types.ts`` / ``admin_service.list_user_agents``), so
the mocks below stub ``wait_for_response`` and the name key is ``name``.
``has_active_runs`` already exists as a ``@property`` on the class, so its test
uses property access to match the live contract.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from core.gateway.connection_pool import GatewayConnection


def _make_conn(owner_id="user_owner"):
    # Construct without running __init__'s socket setup: build a bare instance
    # and set only the attributes the dispatch reads.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = owner_id
    conn.user_id = owner_id
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    # send_rpc only sends (returns None); wait_for_response carries the result.
    conn.send_rpc = AsyncMock(return_value=None)
    conn.wait_for_response = AsyncMock(return_value={"agents": [{"id": "a1", "name": "Friday"}]})
    return conn


@pytest.mark.asyncio
async def test_dispatch_pushes_to_member_with_preview():
    conn = _make_conn()
    payload = {
        "sessionKey": "agent:a1:user_member:s1",
        "message": {"content": [{"type": "text", "text": "here is the summary"}]},
    }
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member="user_member", agent_id="a1")
        # drain the fire-and-forget task
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_awaited_once()
    args = sent.await_args.args
    assert args[0] == "user_member"  # audience = the member
    assert args[1] == "Friday"  # title = agent name
    assert args[2] == "here is the summary"  # body = preview
    assert args[3]["agentId"] == "a1"
    assert args[3]["sessionKey"] == "agent:a1:user_member:s1"


@pytest.mark.asyncio
async def test_personal_session_targets_owner():
    conn = _make_conn(owner_id="user_owner")
    payload = {"sessionKey": "agent:a1:main", "message": {"content": [{"type": "text", "text": "done"}]}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    assert sent.await_args.args[0] == "user_owner"


@pytest.mark.asyncio
async def test_org_owner_without_member_does_not_push():
    conn = _make_conn(owner_id="org_123")
    payload = {"sessionKey": "agent:a1:cron:c1", "message": {"content": []}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_no_preview_does_not_push():
    conn = _make_conn(owner_id="user_owner")
    payload = {"sessionKey": "agent:a1:main", "message": {"content": []}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_body_truncated_to_120_chars():
    conn = _make_conn(owner_id="user_owner")
    long_text = "x" * 200
    payload = {"sessionKey": "agent:a1:main", "message": {"content": [{"type": "text", "text": long_text}]}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    body = sent.await_args.args[2]
    assert len(body) == 120
    assert body.endswith("…")


@pytest.mark.asyncio
async def test_generic_title_when_agent_name_unresolved():
    conn = _make_conn(owner_id="user_owner")
    conn.wait_for_response = AsyncMock(return_value={"agents": []})
    payload = {"sessionKey": "agent:a1:main", "message": {"content": [{"type": "text", "text": "hi"}]}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    assert sent.await_args.args[1] == "New reply"


def test_has_active_runs():
    # has_active_runs is a pre-existing @property (not a method) on the class.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    assert conn.has_active_runs is False
    conn._active_runs = {"agent:a1:main": {}}
    assert conn.has_active_runs is True
