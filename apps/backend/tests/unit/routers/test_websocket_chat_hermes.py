"""H3.4 — the Hermes chat bridge inside the websocket_chat seams.

Asserts the four load-bearing behaviors of the spike:
  1. A Hermes owner's TURN routes through ``runtime.send_chat`` (NOT the pool's
     chat.send), and every emitted frontend frame carries the routing envelope
     (``runId`` == the frontend's clientRunId, ``agent_id``, ``session_id``).
  2. A Hermes owner's CONTROL-PLANE RPCs answer minimally from the backend
     (``agents.list`` roster + empty ``sessions.list`` + graceful empty-OK) so
     the ``/chat`` page mounts — the pool is never touched.
  3. An OpenClaw owner still uses the pool path, unchanged.

The Hermes logic is strictly behind ``get_runtime_for_owner(owner_id).kind ==
"hermes"``, so each test patches that resolver rather than priming the real
process-local cache (which would leak across tests).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.runtime.base import ChatResult, ContainerNotRunningError
from routers.websocket_chat import (
    _process_agent_chat_background,
    _process_rpc_background,
)

_VALID_SESSION_ID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def mock_ecs_manager():
    with patch("routers.websocket_chat.get_ecs_manager") as mock_getter:
        manager = AsyncMock()
        # Hermes owner: a container row exists (runtime_kind=hermes). The Hermes
        # branch ignores it — send_chat re-resolves internally — but resolve is
        # still called first (it primes the runtime-kind cache in prod).
        manager.resolve_running_container = AsyncMock(
            return_value=({"owner_id": "org_h", "runtime_kind": "hermes"}, "10.0.9.9")
        )
        mock_getter.return_value = manager
        yield manager


@pytest.fixture
def mock_pool():
    with patch("routers.websocket_chat.get_gateway_pool") as mock_getter:
        pool = AsyncMock()
        pool.send_rpc = AsyncMock(return_value={})
        pool.gate_chat = AsyncMock(return_value={"blocked": False})
        pool.mark_run_active = MagicMock()
        pool.clear_active_run = MagicMock()
        pool.mark_side_run = MagicMock()
        mock_getter.return_value = pool
        yield pool


@pytest.fixture
def mock_mgmt():
    with patch("routers.websocket_chat.get_management_api_client") as mock_getter:
        client = MagicMock()
        client.send_message = MagicMock(return_value=True)
        mock_getter.return_value = client
        yield client


def _hermes_runtime(send_chat=None):
    rt = MagicMock()
    rt.kind = "hermes"
    rt.send_chat = AsyncMock(side_effect=send_chat) if send_chat else AsyncMock()
    return rt


def _frames(mock_mgmt):
    return [call[0][1] for call in mock_mgmt.send_message.call_args_list]


# ---------------------------------------------------------------------------
# (1) Turn routing + envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_turn_routes_through_send_chat_not_pool(mock_ecs_manager, mock_pool, mock_mgmt):
    async def fake_send_chat(owner_id, agent_id, message, session_key, on_event, context=None):
        await on_event({"type": "chunk", "content": "hi"})
        await on_event({"type": "done"})
        return ChatResult(run_id="hermes-internal-run", status="completed")

    rt = _hermes_runtime(fake_send_chat)
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            agent_id="friday",
            message="hi",
            client_run_id="crid-1",
            session_id=None,
        )

    # The OpenClaw pool chat.send was NEVER used.
    mock_pool.send_rpc.assert_not_called()
    # The turn drove through the runtime, carrying the built session key + the
    # context seam (None for a plain webchat turn).
    rt.send_chat.assert_awaited_once()
    args, kwargs = rt.send_chat.call_args
    assert args[0] == "org_h"  # owner_id
    assert args[1] == "friday"  # agent_id
    assert args[3] == "agent:friday:user-1"  # session_key
    assert kwargs.get("context") is None

    # Every frontend frame carries the routing envelope. runId is the FRONTEND's
    # clientRunId (its busy-gate correlation key) — NOT Hermes' internal run id.
    frames = _frames(mock_mgmt)
    assert {"type": "chunk", "content": "hi", "agent_id": "friday", "runId": "crid-1"} in frames
    assert {"type": "done", "agent_id": "friday", "runId": "crid-1"} in frames
    assert all(f["runId"] == "crid-1" and f["agent_id"] == "friday" for f in frames)


@pytest.mark.asyncio
async def test_hermes_turn_stamps_session_id_for_named_session(mock_ecs_manager, mock_pool, mock_mgmt):
    async def fake_send_chat(owner_id, agent_id, message, session_key, on_event, context=None):
        await on_event({"type": "chunk", "content": "yo"})

    rt = _hermes_runtime(fake_send_chat)
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            agent_id="friday",
            message="yo",
            client_run_id="crid-2",
            session_id=_VALID_SESSION_ID,
        )
    # session_key threads the named session; the envelope stamps the raw id.
    assert rt.send_chat.call_args[0][3] == f"agent:friday:user-1:{_VALID_SESSION_ID}"
    frame = _frames(mock_mgmt)[0]
    assert frame["session_id"] == _VALID_SESSION_ID


@pytest.mark.asyncio
async def test_hermes_turn_blocked_by_budget_gate_does_not_call_send_chat(mock_ecs_manager, mock_pool, mock_mgmt):
    mock_pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "out_of_credits", "message": "Top up."})
    rt = _hermes_runtime()
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            agent_id="friday",
            message="hi",
            client_run_id="crid-1",
        )
    rt.send_chat.assert_not_awaited()
    frame = _frames(mock_mgmt)[0]
    assert frame["type"] == "error"
    assert frame["code"] == "out_of_credits"


@pytest.mark.asyncio
async def test_hermes_turn_cold_container_emits_runid_tagged_error(mock_ecs_manager, mock_pool, mock_mgmt):
    async def cold(owner_id, agent_id, message, session_key, on_event, context=None):
        raise ContainerNotRunningError("cold")

    rt = _hermes_runtime(cold)
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_agent_chat_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            agent_id="friday",
            message="hi",
            client_run_id="crid-1",
        )
    frame = _frames(mock_mgmt)[0]
    assert frame["type"] == "error"
    assert frame["runId"] == "crid-1"  # tagged so the frontend releases its gate
    assert "starting up" in frame["message"]


# ---------------------------------------------------------------------------
# (2) Control plane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_rpc_agents_list_returns_roster(mock_ecs_manager, mock_pool, mock_mgmt):
    roster = [
        {"id": "friday", "role": "ceo", "name": "Friday"},
        {"id": "atlas", "role": "general", "name": "Atlas"},
    ]
    rt = _hermes_runtime()
    with (
        patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt),
        patch("routers.websocket_chat.chat_bridge.fetch_hermes_roster", AsyncMock(return_value=roster)),
    ):
        await _process_rpc_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            req_id="r1",
            method="agents.list",
            params={},
        )
    mock_pool.send_rpc.assert_not_called()
    _, frame = mock_mgmt.send_message.call_args[0]
    assert frame["ok"] is True
    assert frame["id"] == "r1"
    assert frame["payload"]["defaultId"] == "friday"
    assert [a["id"] for a in frame["payload"]["agents"]] == ["friday", "atlas"]


@pytest.mark.asyncio
async def test_hermes_rpc_sessions_list_is_empty(mock_ecs_manager, mock_pool, mock_mgmt):
    rt = _hermes_runtime()
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_rpc_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            req_id="r2",
            method="sessions.list",
            params={},
        )
    mock_pool.send_rpc.assert_not_called()
    _, frame = mock_mgmt.send_message.call_args[0]
    assert frame["ok"] is True
    assert frame["payload"] == {"sessions": []}


@pytest.mark.asyncio
async def test_hermes_rpc_unknown_method_is_graceful_empty_ok(mock_ecs_manager, mock_pool, mock_mgmt):
    # health / channels.status fire every ~3s from the page; they must never 500.
    rt = _hermes_runtime()
    with patch("routers.websocket_chat.get_runtime_for_owner", return_value=rt):
        await _process_rpc_background(
            connection_id="conn-1",
            user_id="user-1",
            owner_id="org_h",
            req_id="r3",
            method="health",
            params={},
        )
    _, frame = mock_mgmt.send_message.call_args[0]
    assert frame["ok"] is True
    assert frame["payload"] == {}


# ---------------------------------------------------------------------------
# (3) OpenClaw owner is unchanged — still the pool path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openclaw_owner_turn_still_uses_pool_chat_send(mock_pool, mock_mgmt):
    with patch("routers.websocket_chat.get_ecs_manager") as ecs_getter:
        manager = AsyncMock()
        manager.resolve_running_container = AsyncMock(
            return_value=({"gateway_token": "gw", "owner_id": "user-1", "service_name": "openclaw-x"}, "10.0.1.5")
        )
        ecs_getter.return_value = manager
        oc = MagicMock()
        oc.kind = "openclaw"
        with patch("routers.websocket_chat.get_runtime_for_owner", return_value=oc):
            await _process_agent_chat_background(
                connection_id="conn-1",
                user_id="user-1",
                owner_id="user-1",
                agent_id="luna",
                message="Hello!",
            )
    # The OpenClaw path forwards chat.send through the pool, untouched.
    mock_pool.send_rpc.assert_called_once()
    assert mock_pool.send_rpc.call_args[1]["method"] == "chat.send"


@pytest.mark.asyncio
async def test_openclaw_owner_rpc_still_uses_pool(mock_pool, mock_mgmt):
    with patch("routers.websocket_chat.get_ecs_manager") as ecs_getter:
        manager = AsyncMock()
        manager.resolve_running_container = AsyncMock(
            return_value=({"gateway_token": "gw", "owner_id": "user-1"}, "10.0.1.5")
        )
        ecs_getter.return_value = manager
        mock_pool.send_rpc = AsyncMock(return_value={"sessions": []})
        oc = MagicMock()
        oc.kind = "openclaw"
        with patch("routers.websocket_chat.get_runtime_for_owner", return_value=oc):
            await _process_rpc_background(
                connection_id="conn-1",
                user_id="user-1",
                owner_id="user-1",
                req_id="r1",
                method="sessions.list",
                params={},
            )
    mock_pool.send_rpc.assert_awaited_once()
    assert mock_pool.send_rpc.call_args[1]["method"] == "sessions.list"
