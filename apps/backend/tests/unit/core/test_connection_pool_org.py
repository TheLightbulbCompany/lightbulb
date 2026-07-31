"""Tests for org-aware gateway connection pool."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


class TestOrgFanOut:
    """Multiple org members share one pool entry under the same owner_id."""

    def test_multiple_members_share_owner_key(self):
        pool = GatewayConnectionPool(management_api=MagicMock())
        pool.add_frontend_connection("org_456", "conn_alice")
        pool.add_frontend_connection("org_456", "conn_bob")

        assert pool._frontend_connections["org_456"] == {"conn_alice", "conn_bob"}

    def test_remove_one_member_keeps_other(self):
        pool = GatewayConnectionPool(management_api=MagicMock())
        pool.add_frontend_connection("org_456", "conn_alice")
        pool.add_frontend_connection("org_456", "conn_bob")

        pool.remove_frontend_connection("org_456", "conn_alice")
        assert pool._frontend_connections["org_456"] == {"conn_bob"}

    def test_personal_connections_unchanged(self):
        pool = GatewayConnectionPool(management_api=MagicMock())
        pool.add_frontend_connection("user_123", "conn_1")
        pool.add_frontend_connection("user_456", "conn_2")

        assert pool._frontend_connections["user_123"] == {"conn_1"}
        assert pool._frontend_connections["user_456"] == {"conn_2"}


# ------------------------------------------------------------------
# Owner-keyed DDB fan-out (Piece-1 Task 5)
#
# `_forward_to_frontends` sources recipients from the ws-connections
# by-owner-id GSI (ConnectionService.query_by_owner) rather than the
# in-process set, filters to connectionType=="chat", and demuxes per member.
# ------------------------------------------------------------------


async def _forward_and_flush(conn, message, target_member_id=None):
    """Forward then drain the FrameSender so send_message assertions see the
    delivered frames (delivery runs on per-connection drainer tasks now)."""
    await conn._forward_to_frontends(message, target_member_id)
    await conn._sender.flush()


@pytest.fixture
def make_conn():
    def _make(owner_id="org_1"):
        conn = GatewayConnection(
            frontend_connections=set(),
            user_id=owner_id,
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
            owner_id=owner_id,
        )
        conn._management_api.send_message = MagicMock(return_value=True)
        # Stub gone_confirmed EXPLICITLY. Without it, the bare MagicMock
        # returns a truthy Mock for any attribute call, so `not
        # gone_confirmed(...)` is False and every prune test falls into the
        # confirmed branch by accident — passing for a reason unrelated to
        # what it claims to check. Default True keeps the prune tests below
        # meaningful; the unconfirmed case overrides it to False.
        conn._management_api.gone_confirmed = MagicMock(return_value=True)
        conn._conn_svc = MagicMock()
        return conn

    return _make


@pytest.mark.asyncio
async def test_forward_filters_to_chat_connections(make_conn):
    """Broadcast (target None) reaches every chat connection and excludes
    node/operator transparent pipes."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(
        return_value=[
            ("c_chat1", "m1", "chat"),
            ("c_chat2", "m2", "chat"),
            ("c_node", "m1", "node"),
            ("c_op", "m1", "operator"),
        ]
    )
    await _forward_and_flush(conn, {"type": "chunk", "content": "x"})

    sent = {c.args[0] for c in conn._management_api.send_message.call_args_list}
    assert sent == {"c_chat1", "c_chat2"}
    conn._conn_svc.query_by_owner.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_forward_per_member_demux_case_insensitive(make_conn):
    """target_member_id reaches only that member's chat connection, matched
    case-insensitively."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(
        return_value=[
            ("c_m1", "M1", "chat"),
            ("c_m2", "m2", "chat"),
        ]
    )
    await _forward_and_flush(conn, {"type": "done"}, target_member_id="m1")

    sent = [c.args[0] for c in conn._management_api.send_message.call_args_list]
    assert sent == ["c_m1"]


@pytest.mark.asyncio
async def test_forward_query_error_drops_without_raising(make_conn):
    """A query_by_owner failure logs + drops the event — no fallback to the
    in-process set, no crash, nothing sent."""
    conn = make_conn(owner_id="user_p")
    conn._conn_svc.query_by_owner = AsyncMock(side_effect=RuntimeError("ddb down"))
    # In-process set is non-empty — it must NOT be used as a fallback.
    conn._frontend_connections.add("c_inprocess")

    await _forward_and_flush(conn, {"type": "chunk", "content": "x"})

    conn._management_api.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_forward_caches_recipients_within_ttl(make_conn):
    """A second forward within the TTL reuses the cached recipient set and
    does not re-query DDB."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_chat", "m1", "chat")])

    await _forward_and_flush(conn, {"type": "chunk"})
    await _forward_and_flush(conn, {"type": "chunk"})

    assert conn._conn_svc.query_by_owner.call_count == 1


@pytest.mark.asyncio
async def test_forward_caches_recipients_and_busts_on_register(make_conn):
    """The recipient set (incl. an empty one) is cached for the TTL; a new tab
    is picked up because $connect/$disconnect bust the cache via
    invalidate_recipients_cache — not via any freshness cross-check against the
    in-process set. (GSI lag is moot: fan-out always postdates the $connect that
    wrote the row by a chat.send round-trip + LLM turn.)"""
    conn = make_conn(owner_id="org_1")
    # First forward: GSI returns empty → cached for the TTL (no per-chunk re-query).
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    await _forward_and_flush(conn, {"type": "chunk"})
    await _forward_and_flush(conn, {"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 1

    # A connect/disconnect busts the cache; the next event re-queries and
    # delivers to the now-visible connection.
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_chat", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=True)
    conn.invalidate_recipients_cache()
    await _forward_and_flush(conn, {"type": "chunk", "content": "hi"})
    conn._conn_svc.query_by_owner.assert_awaited_once_with("org_1")
    conn._management_api.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_forward_does_not_prune_on_transient_exception(make_conn):
    """An EXCEPTION from send_message is transient (throttle/payload/API-GW
    blip), NOT a gone connection — it must NOT prune, or a temporary blip
    deletes a live socket's row."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.delete_connection = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_live", "m1", "chat")])
    conn._management_api.send_message = MagicMock(side_effect=Exception("throttled"))

    await _forward_and_flush(conn, {"type": "chunk"})
    conn._conn_svc.delete_connection.assert_not_called()


@pytest.mark.asyncio
async def test_forward_does_not_prune_on_unconfirmed_gone(make_conn):
    """A lone Gone must NOT prune — this is the founders'-chat bug itself.

    `_forward_to_frontends` → `_prune_dead_conn` is the exact path that
    zombified a live connection: one spurious 410 deleted the ws-connections
    row, after which the tab's pings 401'd forever and it stopped receiving
    chat fan-out, while the run completed invisibly behind a frozen
    'Thinking…'. Until gone_confirmed() says the Gone persisted, the row has
    to survive.
    """
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.delete_connection = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_live", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=False)
    conn._management_api.gone_confirmed = MagicMock(return_value=False)

    await _forward_and_flush(conn, {"type": "chunk"})

    conn._conn_svc.delete_connection.assert_not_called()
    assert "c_live" not in getattr(conn, "_dead_frontends", set())


@pytest.mark.asyncio
async def test_forward_prunes_on_gone_false(make_conn):
    """A CONFIRMED Gone prunes. send_message returning False is necessary but
    no longer sufficient — the fixture stubs gone_confirmed=True to stand in
    for a Gone that has persisted past the confirm window."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.delete_connection = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=False)

    await _forward_and_flush(conn, {"type": "chunk"})
    conn._conn_svc.delete_connection.assert_called_once_with("c_dead")


@pytest.mark.asyncio
async def test_prune_discards_from_in_process_set(make_conn):
    """Pruning a gone conn must also remove it from the in-process
    _frontend_connections set (which now backs only grace/idle teardown),
    not just delete the DDB row."""
    conn = make_conn(owner_id="org_1")
    conn._frontend_connections.add("c_dead")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._conn_svc.delete_connection = MagicMock()
    conn._management_api.send_message = MagicMock(return_value=False)

    await _forward_and_flush(conn, {"type": "chunk"})
    assert "c_dead" not in conn._frontend_connections


@pytest.mark.asyncio
async def test_forward_prunes_dead_conn_and_invalidates_cache(make_conn):
    """A gone connection (send_message → False) is pruned via delete_connection
    and busts the cache so the next forward re-queries."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._conn_svc.delete_connection = MagicMock()
    conn._management_api.send_message = MagicMock(return_value=False)

    await _forward_and_flush(conn, {"type": "chunk"})
    conn._conn_svc.delete_connection.assert_called_once_with("c_dead")

    # Cache invalidated → next forward re-queries instead of re-hitting the
    # dead conn.
    conn._management_api.send_message = MagicMock(return_value=True)
    await _forward_and_flush(conn, {"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 2


# ---------------------------------------------------------------------------
# broadcast_to_member goes through the POOL-level owner-keyed fan-out
# (query_by_owner on the by-owner-id GSI), reachable WITHOUT a GatewayConnection
# (Codex P2) — the node-status path creates a NodeUpstreamConnection, not a
# chat GatewayConnection, so requiring one dropped status pings.
# ---------------------------------------------------------------------------


def _capture_sends(pool):
    sent = []
    pool._management_api.send_message = MagicMock(side_effect=lambda cid, msg: sent.append(cid) or True)
    return sent


@pytest.mark.asyncio
async def test_broadcast_to_member_org_demuxes_via_owner_keyed_query():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._conn_svc = MagicMock()
    pool._conn_svc.query_by_owner = AsyncMock(
        return_value=[
            ("c_target", "m_target", "chat"),
            ("c_other", "m_other", "chat"),  # different member — excluded
            ("c_node", "m_target", "node"),  # transparent pipe — excluded
        ]
    )
    sent = _capture_sends(pool)

    await pool.broadcast_to_member("org_1", "m_target", {"type": "event", "event": "node_status"})
    await pool._sender.flush()

    pool._conn_svc.query_by_owner.assert_awaited_once_with("org_1")
    assert sent == ["c_target"]  # only the target member's chat tab


@pytest.mark.asyncio
async def test_broadcast_to_member_works_without_gateway_connection():
    """The fan-out must reach the owner's tabs purely via the GSI — no chat
    GatewayConnection exists on the task handling a node-status broadcast."""
    pool = GatewayConnectionPool(management_api=MagicMock())
    assert "org_1" not in pool._connections  # no chat gateway connection
    pool._conn_svc = MagicMock()
    pool._conn_svc.query_by_owner = AsyncMock(return_value=[("c_target", "m_target", "chat")])
    sent = _capture_sends(pool)

    await pool.broadcast_to_member("org_1", "m_target", {"type": "event"})
    await pool._sender.flush()
    assert sent == ["c_target"]


@pytest.mark.asyncio
async def test_broadcast_to_member_personal_broadcasts_all_chat():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._conn_svc = MagicMock()
    pool._conn_svc.query_by_owner = AsyncMock(
        return_value=[
            ("c_1", "user_1", "chat"),
            ("c_2", "user_1", "chat"),
            ("c_node", "user_1", "node"),  # excluded
        ]
    )
    sent = _capture_sends(pool)

    # Personal (owner == member) → no demux, all chat conns; node pipe excluded.
    await pool.broadcast_to_member("user_1", "user_1", {"type": "event"})
    await pool._sender.flush()
    assert set(sent) == {"c_1", "c_2"}


@pytest.mark.asyncio
async def test_broadcast_to_member_query_failure_drops_silently():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._conn_svc = MagicMock()
    pool._conn_svc.query_by_owner = AsyncMock(side_effect=RuntimeError("ddb down"))
    pool._management_api.send_message = MagicMock(return_value=True)

    await pool.broadcast_to_member("org_1", "m1", {"type": "event"})  # must not raise
    await pool._sender.flush()
    pool._management_api.send_message.assert_not_called()
