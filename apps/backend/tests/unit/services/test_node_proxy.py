"""Tests for per-user node connection tracking."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.services.node_proxy import (
    handle_node_connect,
    handle_node_disconnect,
    get_user_node,
    force_close_device,
    _user_nodes,
    _node_count,
    _node_upstreams,
)


@pytest.fixture(autouse=True)
def clear_module_state():
    """Reset module-level dicts between tests."""
    _user_nodes.clear()
    _node_count.clear()
    _node_upstreams.clear()
    yield
    _user_nodes.clear()
    _node_count.clear()
    _node_upstreams.clear()


@pytest.fixture
def mock_ecs():
    with patch("core.services.node_proxy.get_ecs_manager") as m:
        ecs = AsyncMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok123"}, "10.0.1.1"))
        m.return_value = ecs
        yield ecs


@pytest.fixture
def mock_pool():
    with patch("core.services.node_proxy.get_gateway_pool") as m:
        pool = MagicMock()
        pool.broadcast_to_member = AsyncMock()
        m.return_value = pool
        yield pool


@pytest.fixture
def mock_upstream():
    with patch("core.services.node_proxy.NodeUpstreamConnection") as cls:
        upstream = AsyncMock()
        upstream.connect = AsyncMock(return_value={"ok": True, "payload": {"protocol": 4}})
        upstream.start_reader = AsyncMock()
        upstream.close = AsyncMock()
        upstream.device_id = "abc123def456"
        cls.return_value = upstream
        yield upstream


@pytest.fixture
def mock_config_patcher():
    with patch("core.services.node_proxy.patch_openclaw_config", new_callable=AsyncMock) as m:
        yield m


@pytest.mark.asyncio
async def test_connect_stores_user_node(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """handle_node_connect stores the user_id -> nodeId mapping (under cli bucket for node-host)."""
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_1",
        connect_params={"role": "node", "client": {"id": "node-host"}},
        management_api=mgmt,
    )

    assert "user_alice" in _user_nodes
    assert _user_nodes["user_alice"]["cli"]["connection_id"] == "conn_1"


@pytest.mark.asyncio
async def test_connect_increments_node_count(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """First node connection for an owner patches config to enable node tools."""
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_1",
        connect_params={},
        management_api=mgmt,
    )
    assert _node_count.get("org_123") == 1
    mock_config_patcher.assert_called_once()  # config patched on 0->1

    mock_config_patcher.reset_mock()
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_bob",
        connection_id="conn_2",
        connect_params={},
        management_api=mgmt,
    )
    assert _node_count.get("org_123") == 2
    mock_config_patcher.assert_not_called()  # no re-patch on 1->2


@pytest.mark.asyncio
async def test_disconnect_decrements_and_patches_on_zero(
    mock_ecs,
    mock_pool,
    mock_upstream,
    mock_config_patcher,
):
    """Config is re-disabled only when the last node disconnects."""
    mgmt = MagicMock()

    # Connect two users
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_1",
        connect_params={},
        management_api=mgmt,
    )
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_bob",
        connection_id="conn_2",
        connect_params={},
        management_api=mgmt,
    )
    mock_config_patcher.reset_mock()

    # Disconnect Alice — count goes 2->1, no config patch
    await handle_node_disconnect("conn_1", "org_123", "user_alice")
    assert _node_count.get("org_123") == 1
    mock_config_patcher.assert_not_called()

    # Disconnect Bob — count goes 1->0, config patched
    await handle_node_disconnect("conn_2", "org_123", "user_bob")
    assert _node_count.get("org_123", 0) == 0
    mock_config_patcher.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_broadcasts_to_member(
    mock_ecs,
    mock_pool,
    mock_upstream,
    mock_config_patcher,
):
    """Disconnect broadcasts node_status to the specific user, not the whole org."""
    mgmt = MagicMock()
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_1",
        connect_params={},
        management_api=mgmt,
    )
    mock_pool.broadcast_to_member.reset_mock()

    await handle_node_disconnect("conn_1", "org_123", "user_alice")

    mock_pool.broadcast_to_member.assert_called_once_with(
        "org_123",
        "user_alice",
        {"type": "node_status", "status": "disconnected"},
    )


@pytest.mark.asyncio
async def test_get_user_node_returns_none_when_disconnected(
    mock_ecs,
    mock_pool,
    mock_upstream,
    mock_config_patcher,
):
    """get_user_node returns None for users without a connected node."""
    assert get_user_node("user_nobody") is None


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_clobber_fresh_reconnect(
    mock_ecs,
    mock_pool,
    mock_upstream,
    mock_config_patcher,
):
    """A late close on the OLD socket must not clear the newer connection's state
    or emit a false 'disconnected' to the UI."""
    mgmt = MagicMock()

    # Alice connects on conn_old, then quickly reconnects on conn_new.
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_old",
        connect_params={},
        management_api=mgmt,
    )
    await handle_node_connect(
        owner_id="org_123",
        user_id="user_alice",
        connection_id="conn_new",
        connect_params={},
        management_api=mgmt,
    )
    mock_pool.broadcast_to_member.reset_mock()

    # The old socket's close event fires after the reconnect landed.
    await handle_node_disconnect("conn_old", "org_123", "user_alice")

    # The live mapping must still point at the new connection.
    stored = get_user_node("user_alice")
    assert stored is not None
    assert stored["connection_id"] == "conn_new"

    # And no phantom "disconnected" must have been emitted.
    mock_pool.broadcast_to_member.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent_on_double_call(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """Both the upstream-died callback AND the desktop ws_disconnect path
    can trigger handle_node_disconnect for the same connection. Calling
    twice must not double-decrement _node_count (ref counter) — the
    second call must be a no-op."""
    from core.services.node_proxy import _node_count

    mgmt = MagicMock()
    await handle_node_connect(
        owner_id="org_dbl",
        user_id="user_a",
        connection_id="conn_x",
        connect_params={},
        management_api=mgmt,
    )
    assert _node_count.get("org_dbl") == 1

    # First disconnect: real cleanup.
    await handle_node_disconnect("conn_x", "org_dbl", "user_a")
    # Ref count goes to 0, key popped per the existing cleanup path.
    assert _node_count.get("org_dbl", 0) == 0

    # Second disconnect (simulates the upstream-died callback racing the
    # desktop ws_disconnect, or vice versa). MUST be a no-op.
    mock_config_patcher.reset_mock()
    mock_pool.broadcast_to_member.reset_mock()
    await handle_node_disconnect("conn_x", "org_dbl", "user_a")

    # Ref count unchanged (no double-decrement into negative territory).
    assert _node_count.get("org_dbl", 0) == 0
    # No extra config patch, no extra broadcast.
    mock_config_patcher.assert_not_called()
    mock_pool.broadcast_to_member.assert_not_called()


@pytest.mark.asyncio
async def test_upstream_closed_callback_cleans_up_user_state(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """When the upstream WS dies (container restart, net drop), the
    callback installed in handle_node_connect must tear down _user_nodes
    and emit node_status=disconnected — otherwise the agent keeps
    binding sessions to a dead nodeId while the desktop is still alive."""
    from core.services.node_proxy import _user_nodes

    mgmt = MagicMock()
    await handle_node_connect(
        owner_id="org_cb",
        user_id="user_alice",
        connection_id="conn_cb",
        connect_params={},
        management_api=mgmt,
    )
    assert "user_alice" in _user_nodes
    # handle_node_connect should have called set_on_upstream_closed with
    # a callable that, when invoked, runs handle_node_disconnect.
    cb = mock_upstream.set_on_upstream_closed.call_args[0][0]
    assert callable(cb)

    mock_pool.broadcast_to_member.reset_mock()

    # Simulate the upstream reader exiting.
    await cb()

    # Per-user state cleared, disconnected broadcast emitted.
    assert "user_alice" not in _user_nodes
    mock_pool.broadcast_to_member.assert_called_once_with(
        "org_cb",
        "user_alice",
        {"type": "node_status", "status": "disconnected"},
    )


@pytest.mark.asyncio
async def test_force_close_device_closes_matching_upstream():
    """force_close_device closes any upstreams whose device_id matches and removes them."""
    fake_upstream = MagicMock()
    fake_upstream.device_id = "dev_target"
    fake_upstream.close = AsyncMock()
    _node_upstreams["conn_1"] = fake_upstream

    closed = await force_close_device("dev_target")
    assert closed is True
    fake_upstream.close.assert_awaited_once()
    # Connection should be removed from the registry after close
    assert "conn_1" not in _node_upstreams


@pytest.mark.asyncio
async def test_force_close_device_no_match_returns_false():
    """If no upstream has the given device_id, returns False and touches nothing."""
    closed = await force_close_device("dev_missing")
    assert closed is False


@pytest.mark.asyncio
async def test_force_close_device_handles_close_exception():
    """If upstream.close() raises, log + continue + still mark removed."""
    fake_upstream = MagicMock()
    fake_upstream.device_id = "dev_target"
    fake_upstream.close = AsyncMock(side_effect=RuntimeError("broken pipe"))
    _node_upstreams["conn_1"] = fake_upstream

    # Should NOT propagate the exception; revocation is best-effort
    closed = await force_close_device("dev_target")
    assert closed is True
    assert "conn_1" not in _node_upstreams


# ---------------------------------------------------------------------------
# Task 11: per-user {cli, mac} accessors
# ---------------------------------------------------------------------------


def test_get_user_cli_node_returns_cli_entry():
    from core.services.node_proxy import get_user_cli_node, get_user_mac_node

    _user_nodes.clear()
    _user_nodes["user_alice"] = {
        "cli": {"deviceId": "dev_cli", "clientId": "node-host", "connection_id": "c1", "owner_id": "o"},
        "mac": {"deviceId": "dev_mac", "clientId": "openclaw-macos", "connection_id": "c2", "owner_id": "o"},
    }
    cli = get_user_cli_node("user_alice")
    assert cli is not None
    assert cli["deviceId"] == "dev_cli"
    assert cli["clientId"] == "node-host"
    mac = get_user_mac_node("user_alice")
    assert mac is not None
    assert mac["deviceId"] == "dev_mac"
    assert mac["clientId"] == "openclaw-macos"


def test_get_user_cli_node_none_when_missing():
    from core.services.node_proxy import get_user_cli_node, get_user_mac_node

    _user_nodes.clear()
    assert get_user_cli_node("user_unknown") is None
    _user_nodes["user_alice"] = {"mac": {"deviceId": "dev_mac"}}  # only mac
    assert get_user_cli_node("user_alice") is None
    assert get_user_mac_node("user_alice")["deviceId"] == "dev_mac"


def test_get_user_node_backward_compat_prefers_cli():
    """Legacy get_user_node should still return something — prefer CLI when both exist."""
    from core.services.node_proxy import get_user_node

    _user_nodes.clear()
    _user_nodes["u"] = {
        "cli": {"deviceId": "dev_cli", "clientId": "node-host"},
        "mac": {"deviceId": "dev_mac", "clientId": "openclaw-macos"},
    }
    legacy = get_user_node("u")
    assert legacy["deviceId"] == "dev_cli"  # CLI preferred for backward compat


# ---------------------------------------------------------------------------
# Task 12: handle_node_connect slots by clientId + handle_node_disconnect
#          owning-close check traverses both buckets
# ---------------------------------------------------------------------------


async def test_handle_node_connect_slots_cli_into_cli_bucket(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """node-host client -> _user_nodes[user_id]["cli"]"""
    mock_upstream.device_id = "dev_cli_hash"
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="owner_42",
        user_id="user_alice",
        connection_id="conn_cli",
        connect_params={
            "role": "node",
            "client": {"id": "node-host", "displayName": "Alice | CLI"},
        },
        management_api=mgmt,
    )
    entry = _user_nodes["user_alice"]
    assert "cli" in entry
    assert entry["cli"]["deviceId"] == "dev_cli_hash"
    assert entry["cli"]["clientId"] == "node-host"
    assert entry["cli"]["connection_id"] == "conn_cli"
    assert entry["cli"]["owner_id"] == "owner_42"
    assert "mac" not in entry


async def test_handle_node_connect_slots_mac_into_mac_bucket(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """openclaw-macos client -> _user_nodes[user_id]["mac"]"""
    mock_upstream.device_id = "dev_mac_hash"
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="owner_42",
        user_id="user_alice",
        connection_id="conn_mac",
        connect_params={
            "role": "node",
            "client": {"id": "openclaw-macos", "displayName": "Alice | Mac"},
        },
        management_api=mgmt,
    )
    entry = _user_nodes["user_alice"]
    assert "mac" in entry
    assert entry["mac"]["deviceId"] == "dev_mac_hash"
    assert entry["mac"]["clientId"] == "openclaw-macos"
    assert "cli" not in entry


async def test_handle_node_connect_unknown_client_id_defaults_to_cli(
    mock_ecs, mock_pool, mock_upstream, mock_config_patcher
):
    """Unknown/missing clientId defaults to cli bucket (safe fallback)."""
    mock_upstream.device_id = "dev_unknown"
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="o",
        user_id="u",
        connection_id="c",
        connect_params={"role": "node", "client": {}},  # no id
        management_api=mgmt,
    )
    entry = _user_nodes["u"]
    assert "cli" in entry
    assert entry["cli"]["deviceId"] == "dev_unknown"


async def test_handle_node_disconnect_owning_close_finds_in_cli_bucket(
    mock_ecs, mock_pool, mock_upstream, mock_config_patcher
):
    """Disconnect with connection_id that matches the CLI bucket triggers cleanup."""
    # Pre-populate with both buckets (manual state — bypasses handle_node_connect)
    _user_nodes["u"] = {
        "cli": {"deviceId": "dev_cli", "clientId": "node-host", "connection_id": "c_cli", "owner_id": "o"},
        "mac": {"deviceId": "dev_mac", "clientId": "openclaw-macos", "connection_id": "c_mac", "owner_id": "o"},
    }
    fake_upstream = MagicMock()
    fake_upstream.close = AsyncMock()
    _node_upstreams["c_cli"] = fake_upstream
    _node_count["o"] = 1

    await handle_node_disconnect(connection_id="c_cli", owner_id="o", user_id="u")

    # CLI bucket cleared, mac stays
    assert "cli" not in _user_nodes["u"]
    assert "mac" in _user_nodes["u"]


async def test_handle_node_disconnect_removes_user_entry_when_last_bucket_closes(
    mock_ecs, mock_pool, mock_upstream, mock_config_patcher
):
    """When the last bucket closes, the user_id entry itself is removed."""
    _user_nodes["u"] = {
        "cli": {"deviceId": "dev_cli", "clientId": "node-host", "connection_id": "c_cli", "owner_id": "o"},
    }
    fake_upstream = MagicMock()
    fake_upstream.close = AsyncMock()
    _node_upstreams["c_cli"] = fake_upstream
    _node_count["o"] = 1

    await handle_node_disconnect(connection_id="c_cli", owner_id="o", user_id="u")

    assert "u" not in _user_nodes


# ---------------------------------------------------------------------------
# Task 13: bucket entries drop the transient `nodeId` alias;
#          patched-session clearing scoped to CLI-bucket disconnects only
# ---------------------------------------------------------------------------


async def test_handle_node_connect_bucket_entry_has_no_nodeid_alias(
    mock_ecs, mock_pool, mock_upstream, mock_config_patcher
):
    """Task 13 removes the transient `nodeId` legacy alias the bucket
    used to carry for websocket_chat session-binding. Callers must read
    `deviceId` directly now."""
    mock_upstream.device_id = "dev_only"
    mgmt = MagicMock()

    await handle_node_connect(
        owner_id="o",
        user_id="u",
        connection_id="c",
        connect_params={"role": "node", "client": {"id": "node-host"}},
        management_api=mgmt,
    )
    entry = _user_nodes["u"]["cli"]
    assert entry["deviceId"] == "dev_only"
    assert "nodeId" not in entry, (
        "Task 13 must drop the transient `nodeId` alias from bucket entries; callers should read `deviceId`."
    )


async def test_mac_only_disconnect_does_not_clear_patched_sessions(
    mock_ecs, mock_pool, mock_upstream, mock_config_patcher
):
    """Patched sessions reference the CLI device for exec routing. A
    Mac-only disconnect must NOT clobber the user's exec session pin —
    otherwise the next chat send would re-issue the bind RPC needlessly
    AND a user who has only the Mac connected (no CLI yet) would have
    their CLI-pinned sessions wiped when the Mac drops."""
    from core.services.node_proxy import _patched_sessions, set_patched_session

    # Pin a session against a (future) CLI device.
    set_patched_session("agent:a1:user_alice", "dev_cli")

    # Both buckets connected; CLI bucket holds c_cli, Mac bucket holds c_mac.
    _user_nodes["user_alice"] = {
        "cli": {
            "deviceId": "dev_cli",
            "clientId": "node-host",
            "connection_id": "c_cli",
            "owner_id": "org_42",
        },
        "mac": {
            "deviceId": "dev_mac",
            "clientId": "openclaw-macos",
            "connection_id": "c_mac",
            "owner_id": "org_42",
        },
    }
    fake = MagicMock()
    fake.close = AsyncMock()
    _node_upstreams["c_mac"] = fake
    _node_count["org_42"] = 2

    await handle_node_disconnect(connection_id="c_mac", owner_id="org_42", user_id="user_alice")

    # CLI bucket survives; patched-session pin survives.
    assert "cli" in _user_nodes["user_alice"]
    assert _patched_sessions.get("agent:a1:user_alice") == "dev_cli"


async def test_cli_disconnect_clears_patched_sessions(mock_ecs, mock_pool, mock_upstream, mock_config_patcher):
    """The CLI-bucket close path must still clear patched sessions —
    exec is bound to CLI, so when the CLI socket drops the bound
    execNode is dead and we need to re-patch on next chat."""
    from core.services.node_proxy import _patched_sessions, set_patched_session

    set_patched_session("agent:a1:user_alice", "dev_cli")

    _user_nodes["user_alice"] = {
        "cli": {
            "deviceId": "dev_cli",
            "clientId": "node-host",
            "connection_id": "c_cli",
            "owner_id": "org_42",
        },
    }
    fake = MagicMock()
    fake.close = AsyncMock()
    _node_upstreams["c_cli"] = fake
    _node_count["org_42"] = 1

    # gateway-pool.send_rpc is invoked while clearing — mock_pool already
    # has it stubbed via AsyncMock attribute lookup; ensure it doesn't blow up.
    mock_pool.send_rpc = AsyncMock()

    await handle_node_disconnect(connection_id="c_cli", owner_id="org_42", user_id="user_alice")

    # CLI gone, patched-session pin gone.
    assert _patched_sessions.get("agent:a1:user_alice") is None
