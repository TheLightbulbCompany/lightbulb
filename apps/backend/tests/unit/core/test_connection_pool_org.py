"""Tests for org-aware gateway connection pool."""

import os
import time

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
async def test_forward_does_not_cache_empty_recipients(make_conn):
    """An empty query result (e.g. a tab that sent chat before the by-owner-id
    GSI propagated its $connect row) must NOT be cached — otherwise the new
    tab's first chunks + done are dropped for the whole TTL. The next event
    must re-query so it self-heals once the GSI catches up."""
    conn = make_conn(owner_id="org_1")
    # First forward: GSI hasn't indexed the new tab yet → empty.
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    await conn._forward_to_frontends({"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 1

    # GSI now propagated; the very next event must re-query (no stale empty cache)
    # and deliver to the now-visible connection.
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_chat", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=True)
    await conn._forward_to_frontends({"type": "chunk", "content": "hi"})
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

    await conn._forward_to_frontends({"type": "chunk"})
    conn._conn_svc.delete_connection.assert_not_called()


@pytest.mark.asyncio
async def test_forward_does_not_cache_partial_result_missing_local_conn(make_conn):
    """A non-empty-but-STALE GSI result that omits a locally-registered (just-
    connected) tab must NOT be cached — otherwise the new tab misses its own
    first chunks for the TTL. Detected by cross-checking _frontend_connections;
    the next event must re-query until the GSI includes the new conn."""
    conn = make_conn(owner_id="org_1")
    # Tab A is indexed; tab B just lazy-registered locally but isn't in the GSI yet.
    conn._frontend_connections.add("c_a")
    conn._frontend_connections.add("c_b_new")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_a", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=True)

    await conn._forward_to_frontends({"type": "chunk"})
    # Partial (c_b_new missing) → not cached → next event re-queries.
    await conn._forward_to_frontends({"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 2

    # Once the GSI catches up (both present), it caches and stops re-querying.
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_a", "m1", "chat"), ("c_b_new", "m1", "chat")])
    await conn._forward_to_frontends({"type": "chunk"})
    await conn._forward_to_frontends({"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 1  # cached after first


@pytest.mark.asyncio
async def test_stale_local_id_does_not_block_caching(make_conn):
    """A local id registered longer ago than the GSI propagation window that is
    still absent from the GSI is STALE (its $disconnect deleted the row before
    we unregistered it). It must NOT permanently veto caching — otherwise one
    missed disconnect pins the owner to a per-chunk DDB query forever (Codex
    P2). A live tab IS in the GSI, so caching should kick in on the 2nd event."""
    conn = make_conn(owner_id="org_1")
    conn._frontend_connections.add("c_live")
    conn._frontend_connections.add("c_stale")
    # c_stale registered 100s ago (>> _GSI_PROPAGATION_WINDOW_S) and never made
    # it into the GSI → treated as stale, ignored by the freshness gate.
    conn._frontend_conn_registered_at["c_live"] = time.monotonic()
    conn._frontend_conn_registered_at["c_stale"] = time.monotonic() - 100
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_live", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=True)

    await conn._forward_to_frontends({"type": "chunk"})
    await conn._forward_to_frontends({"type": "chunk"})
    # Cached after the first query despite c_stale's absence from the GSI.
    assert conn._conn_svc.query_by_owner.call_count == 1


@pytest.mark.asyncio
async def test_recent_local_id_still_blocks_caching(make_conn):
    """The freshness gate still protects a genuinely just-registered tab: a
    local id registered within the propagation window that is absent from the
    GSI suppresses caching so it isn't dropped from a cached partial set."""
    conn = make_conn(owner_id="org_1")
    conn._frontend_connections.add("c_a")
    conn._frontend_connections.add("c_new")
    conn._frontend_conn_registered_at["c_a"] = time.monotonic()
    conn._frontend_conn_registered_at["c_new"] = time.monotonic()  # just now → pending
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_a", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=True)

    await conn._forward_to_frontends({"type": "chunk"})
    await conn._forward_to_frontends({"type": "chunk"})
    # c_new is recent + missing from GSI → not cached → re-queries each time.
    assert conn._conn_svc.query_by_owner.call_count == 2


@pytest.mark.asyncio
async def test_forward_prunes_on_gone_false(make_conn):
    """send_message returns False ONLY for a gone connection (GoneException) —
    prune it."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.delete_connection = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._management_api.send_message = MagicMock(return_value=False)

    await conn._forward_to_frontends({"type": "chunk"})
    conn._conn_svc.delete_connection.assert_called_once_with("c_dead")


@pytest.mark.asyncio
async def test_prune_discards_from_in_process_set(make_conn):
    """Pruning a gone conn must also remove it from the in-process
    _frontend_connections set (which still backs grace/idle teardown +
    member broadcast), not just delete the DDB row."""
    conn = make_conn(owner_id="org_1")
    conn._frontend_connections.add("c_dead")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._conn_svc.delete_connection = MagicMock()
    conn._management_api.send_message = MagicMock(return_value=False)

    await conn._forward_to_frontends({"type": "chunk"})
    assert "c_dead" not in conn._frontend_connections


@pytest.mark.asyncio
async def test_forward_prunes_dead_conn_and_invalidates_cache(make_conn):
    """A gone connection (send_message → False) is pruned via delete_connection
    and busts the cache so the next forward re-queries."""
    conn = make_conn(owner_id="org_1")
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[("c_dead", "m1", "chat")])
    conn._conn_svc.delete_connection = MagicMock()
    conn._management_api.send_message = MagicMock(return_value=False)

    await conn._forward_to_frontends({"type": "chunk"})
    conn._conn_svc.delete_connection.assert_called_once_with("c_dead")

    # Cache invalidated → next forward re-queries instead of re-hitting the
    # dead conn.
    conn._management_api.send_message = MagicMock(return_value=True)
    await conn._forward_to_frontends({"type": "chunk"})
    assert conn._conn_svc.query_by_owner.call_count == 2


# ---------------------------------------------------------------------------
# Registration-time tracking (feeds the stale-vs-pending freshness gate above)
# ---------------------------------------------------------------------------


def test_add_remove_tracks_registration_time():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool.add_frontend_connection("org_1", "c_1", member_id="m1")
    assert "c_1" in pool._frontend_conn_registered_at
    # Shared by reference into any GatewayConnection created for this owner.
    assert pool._frontend_conn_registered_at is not None
    pool.remove_frontend_connection("org_1", "c_1")
    assert "c_1" not in pool._frontend_conn_registered_at


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
    assert set(sent) == {"c_1", "c_2"}


@pytest.mark.asyncio
async def test_broadcast_to_member_query_failure_drops_silently():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._conn_svc = MagicMock()
    pool._conn_svc.query_by_owner = AsyncMock(side_effect=RuntimeError("ddb down"))
    pool._management_api.send_message = MagicMock(return_value=True)

    await pool.broadcast_to_member("org_1", "m1", {"type": "event"})  # must not raise
    pool._management_api.send_message.assert_not_called()
