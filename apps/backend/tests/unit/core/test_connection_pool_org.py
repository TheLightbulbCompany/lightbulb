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


@pytest.fixture
def make_conn():
    def _make(owner_id="org_1"):
        conn = GatewayConnection(
            frontend_connections=set(),
            conn_member_map={},
            user_id=owner_id,
            ip="10.0.0.1",
            token="tok",
            management_api=MagicMock(),
            owner_id=owner_id,
        )
        conn._management_api.send_message = MagicMock(return_value=True)
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
    await conn._forward_to_frontends({"type": "chunk", "content": "x"})

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
    await conn._forward_to_frontends({"type": "done"}, target_member_id="m1")

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

    await conn._forward_to_frontends({"type": "chunk", "content": "x"})

    conn._management_api.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_forward_caches_recipients_within_ttl(make_conn):
    """A second forward within the TTL reuses the cached recipient set and
    does not re-query DDB."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_chat", "m1", "chat")])

    await conn._forward_to_frontends({"type": "chunk"})
    await conn._forward_to_frontends({"type": "chunk"})

    assert conn._conn_svc.query_by_owner.call_count == 1


@pytest.mark.asyncio
async def test_forward_prunes_dead_conn_and_invalidates_cache(make_conn):
    """A send failure prunes the dead conn via delete_connection and busts the
    cache so the next forward re-queries."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._conn_svc.delete_connection = MagicMock()
    conn._management_api.send_message = MagicMock(side_effect=Exception("gone"))

    await conn._forward_to_frontends({"type": "chunk"})
    conn._conn_svc.delete_connection.assert_called_once_with("c_dead")

    # Cache invalidated → next forward re-queries instead of re-hitting the
    # dead conn.
    conn._management_api.send_message = MagicMock(return_value=True)
    await conn._forward_to_frontends({"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 2
