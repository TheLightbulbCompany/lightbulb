"""Unit tests for TeamsEventBroker — the per-user fanout layer that owns
PaperclipEventClient instances and routes events to subscribed browser
connection IDs via the API Gateway Management API.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


class FakeClient:
    """Stand-in for PaperclipEventClient — captures lifecycle calls and
    exposes a ``trigger(event)`` helper to simulate upstream events."""

    def __init__(self, on_event: Callable[[dict], Awaitable[None]]):
        self._on_event = on_event
        self.start_called = 0
        self.close_called = 0
        self._alive = True

    async def start(self) -> None:
        self.start_called += 1

    async def close(self) -> None:
        self.close_called += 1
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        """Test helper: simulate the reconnect loop terminating
        (e.g. max-attempts give-up). Mirrors PaperclipEventClient
        behaviour where the background task ends but close() is not
        called by the broker yet."""
        self._alive = False

    async def trigger(self, event: dict) -> None:
        await self._on_event(event)


@pytest.fixture
def fake_components(monkeypatch):
    """Patch the broker's dependencies so we never hit real DDB / WS / API GW."""
    fake_clients: list[FakeClient] = []
    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock(return_value=True)
    fake_conn_svc = MagicMock()
    fake_conn_svc.query_by_user_id = AsyncMock(return_value=[])
    fake_conn_svc.delete_connection = MagicMock()

    def _client_factory(*, user_id, company_id, cookie, on_event):
        c = FakeClient(on_event)
        fake_clients.append(c)
        return c

    async def _resolve_company_id(_user_id: str) -> str:
        return f"co_{_user_id}"

    async def _resolve_cookie(_user_id: str) -> str:
        return "fake-cookie"

    return {
        "clients": fake_clients,
        "mgmt": fake_mgmt,
        "conn_svc": fake_conn_svc,
        "client_factory": _client_factory,
        "resolve_company_id": _resolve_company_id,
        "resolve_cookie": _resolve_cookie,
    }


def _build_broker(fc, *, grace_seconds: float = 30.0):
    from core.services.teams_event_broker import TeamsEventBroker

    return TeamsEventBroker(
        client_factory=fc["client_factory"],
        management_api=fc["mgmt"],
        connection_service=fc["conn_svc"],
        resolve_company_id=fc["resolve_company_id"],
        resolve_session_cookie=fc["resolve_cookie"],
        grace_seconds=grace_seconds,
    )


@pytest.mark.asyncio
async def test_subscribe_starts_backing_client_once_per_user(fake_components):
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    await broker.subscribe("user_a", "conn_2")  # second tab

    assert len(fake_components["clients"]) == 1  # ONE client for the user
    assert fake_components["clients"][0].start_called == 1
    await broker.shutdown()


@pytest.mark.asyncio
async def test_subscribe_distinct_users_creates_distinct_clients(fake_components):
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_a")
    await broker.subscribe("user_b", "conn_b")

    assert len(fake_components["clients"]) == 2
    await broker.shutdown()


@pytest.mark.asyncio
async def test_event_fanout_to_all_user_subscribers(fake_components):
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    await broker.subscribe("user_a", "conn_2")
    fake_components["conn_svc"].query_by_user_id.return_value = ["conn_1", "conn_2"]

    await fake_components["clients"][0].trigger(
        {
            "id": 1,
            "companyId": "co_user_a",
            "type": "activity.logged",
            "createdAt": "2026-05-04T01:00:00Z",
            "payload": {"actor": "x"},
        }
    )

    sent_to = [c.kwargs.get("connection_id") or c.args[0] for c in fake_components["mgmt"].send_message.call_args_list]
    assert sorted(sent_to) == ["conn_1", "conn_2"]

    # Verify wrapper shape: type=event, event prefix teams., payload pass-through.
    payload = fake_components["mgmt"].send_message.call_args_list[0].args[1]
    assert payload["type"] == "event"
    assert payload["event"] == "teams.activity.logged"
    assert payload["payload"] == {"actor": "x"}
    await broker.shutdown()


@pytest.mark.asyncio
async def test_synthetic_resumed_event_fans_out_as_teams_stream_resumed(fake_components):
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    fake_components["conn_svc"].query_by_user_id.return_value = ["conn_1"]

    # Client emits the synthetic resumed (no companyId / payload).
    await fake_components["clients"][0].trigger({"type": "stream.resumed"})

    payload = fake_components["mgmt"].send_message.call_args_list[0].args[1]
    assert payload["type"] == "event"
    assert payload["event"] == "teams.stream.resumed"
    assert payload["payload"] == {}
    await broker.shutdown()


@pytest.mark.asyncio
async def test_unsubscribe_starts_grace_period_then_closes(fake_components):
    broker = _build_broker(fake_components, grace_seconds=0.05)
    await broker.subscribe("user_a", "conn_1")
    await broker.unsubscribe("user_a", "conn_1")

    # Within the grace period, client is still alive.
    assert fake_components["clients"][0].close_called == 0
    await asyncio.sleep(0.15)
    assert fake_components["clients"][0].close_called == 1
    await broker.shutdown()


@pytest.mark.asyncio
async def test_resubscribe_during_grace_cancels_teardown(fake_components):
    broker = _build_broker(fake_components, grace_seconds=0.1)
    await broker.subscribe("user_a", "conn_1")
    await broker.unsubscribe("user_a", "conn_1")
    await asyncio.sleep(0.02)
    await broker.subscribe("user_a", "conn_2")  # rejoin within grace
    await asyncio.sleep(0.2)  # well past the original grace window

    assert fake_components["clients"][0].close_called == 0
    assert len(fake_components["clients"]) == 1  # no new client opened
    await broker.shutdown()


@pytest.mark.asyncio
async def test_stale_connection_cleaned_up_on_send_false(fake_components):
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    fake_components["conn_svc"].query_by_user_id.return_value = ["conn_1"]
    fake_components["mgmt"].send_message.return_value = False  # GoneException

    await fake_components["clients"][0].trigger(
        {
            "id": 1,
            "companyId": "co_user_a",
            "type": "agent.status",
            "createdAt": "2026-05-04T01:00:00Z",
            "payload": {},
        }
    )

    fake_components["conn_svc"].delete_connection.assert_called_once_with("conn_1")
    await broker.shutdown()


@pytest.mark.asyncio
async def test_stale_fanout_conn_removed_from_local_subscribers(fake_components):
    """When send_message returns False, the conn-id must be removed from
    self._subscribers (not just from DDB).
    Otherwise _grace_teardown sees a non-empty subscriber set and never
    closes the backing client, pinning the upstream WS forever for any
    user whose Management API send raced a browser close."""
    broker = _build_broker(fake_components, grace_seconds=0.05)
    await broker.subscribe("user_a", "conn_1")
    assert "conn_1" in broker._subscribers["user_a"]

    fake_components["conn_svc"].query_by_user_id.return_value = ["conn_1"]
    fake_components["mgmt"].send_message.return_value = False

    await fake_components["clients"][0].trigger(
        {
            "id": 1,
            "companyId": "co_user_a",
            "type": "activity.logged",
            "createdAt": "2026-05-04T01:00:00Z",
            "payload": {},
        }
    )

    # Local subscriber set must be empty (or the user_id key removed entirely).
    assert not broker._subscribers.get("user_a")
    # And grace teardown must fire — wait past the grace window and
    # confirm the backing client was closed.
    await asyncio.sleep(0.15)
    assert fake_components["clients"][0].close_called == 1
    await broker.shutdown()


@pytest.mark.asyncio
async def test_concurrent_subscribes_do_not_double_open_client(fake_components):
    broker = _build_broker(fake_components)
    await asyncio.gather(
        broker.subscribe("user_a", "c1"),
        broker.subscribe("user_a", "c2"),
        broker.subscribe("user_a", "c3"),
    )
    assert len(fake_components["clients"]) == 1
    await broker.shutdown()


@pytest.mark.asyncio
async def test_subscribe_emits_subscribe_ok_metric(fake_components):
    """Happy path: first subscribe emits teams.broker.subscribe outcome=ok."""
    with patch("core.services.teams_event_broker.put_metric") as put_metric_mock:
        broker = _build_broker(fake_components)
        await broker.subscribe("user_a", "conn_1")
        await broker.shutdown()

    matching = [
        c
        for c in put_metric_mock.call_args_list
        if c.args and c.args[0] == "teams.broker.subscribe" and c.kwargs.get("dimensions", {}).get("outcome") == "ok"
    ]
    assert matching, (
        f"expected teams.broker.subscribe ok; got: {[(c.args, c.kwargs) for c in put_metric_mock.call_args_list]}"
    )


@pytest.mark.asyncio
async def test_event_handling_emits_received_and_fanout_ok_metrics(fake_components):
    """Happy path: an event triggers teams.broker.event.received +
    teams.broker.fanout outcome=ok per connection sent to."""
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    fake_components["conn_svc"].query_by_user_id.return_value = ["conn_1"]

    with patch("core.services.teams_event_broker.put_metric") as put_metric_mock:
        await fake_components["clients"][0].trigger(
            {
                "id": 1,
                "companyId": "co_user_a",
                "type": "activity.logged",
                "createdAt": "2026-05-04T01:00:00Z",
                "payload": {"actor": "x"},
            }
        )

    received_calls = [
        c for c in put_metric_mock.call_args_list if c.args and c.args[0] == "teams.broker.event.received"
    ]
    fanout_ok_calls = [
        c
        for c in put_metric_mock.call_args_list
        if c.args and c.args[0] == "teams.broker.fanout" and c.kwargs.get("dimensions", {}).get("outcome") == "ok"
    ]
    assert received_calls, "expected teams.broker.event.received"
    assert fanout_ok_calls, "expected teams.broker.fanout outcome=ok"
    # event_type dimension should be set
    assert received_calls[0].kwargs.get("dimensions", {}).get("event_type") == "activity.logged"
    await broker.shutdown()


@pytest.mark.asyncio
async def test_grace_teardown_keeps_user_lock_to_avoid_race(fake_components):
    """Do NOT prune _locks[user_id] in _grace_teardown. A subscribe
    racing in between lock release and lock-pop holds the OLD lock while
    a subsequent subscribe
    creates a fresh one, allowing duplicate client creation. The
    bounded memory cost of keeping per-former-user locks is acceptable;
    correctness > the leak."""
    broker = _build_broker(fake_components, grace_seconds=0.05)
    await broker.subscribe("user_a", "conn_1")
    assert "user_a" in broker._locks
    await broker.unsubscribe("user_a", "conn_1")
    await asyncio.sleep(0.2)  # let grace teardown finish
    # Lock entry must still be present after grace teardown.
    assert "user_a" in broker._locks
    # And the client + subscriber state IS cleared.
    assert "user_a" not in broker._clients
    assert "user_a" not in broker._subscribers
    await broker.shutdown()


@pytest.mark.asyncio
async def test_subscribe_replaces_dead_client_after_reconnect_giveup(fake_components):
    """PaperclipEventClient stops its background loop after 30 failed
    reconnect attempts. If the broker treats _clients[user_id]
    as reusable forever, realtime stays permanently dead until grace teardown.
    A subsequent subscribe must detect is_alive()==False and replace the dead
    client with a fresh one."""
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_1")
    assert len(fake_components["clients"]) == 1
    assert fake_components["clients"][0].is_alive()

    # Simulate the upstream WS giving up after exhausted reconnects.
    fake_components["clients"][0].kill()
    assert not fake_components["clients"][0].is_alive()

    # A new subscribe (e.g. user opens a fresh tab) must spin up a NEW client.
    await broker.subscribe("user_a", "conn_2")
    assert len(fake_components["clients"]) == 2
    # Dead client got close()d during replacement.
    assert fake_components["clients"][0].close_called == 1
    assert fake_components["clients"][1].is_alive()
    assert fake_components["clients"][1].start_called == 1
    await broker.shutdown()


@pytest.mark.asyncio
async def test_fanout_skips_non_subscribed_connections(fake_components):
    """Events must fan out only to browsers that subscribed via
    teams.subscribe — NOT to all of the user's WS conns.
    Desktop node-role sockets share the same userId in ws-connections
    but never opt into Teams events; sending to them is wasted bandwidth.
    """
    broker = _build_broker(fake_components)
    await broker.subscribe("user_a", "conn_browser")
    # Even if DDB happened to know about a node socket, the broker's
    # fanout MUST iterate self._subscribers — not query_by_user_id.
    fake_components["conn_svc"].query_by_user_id.return_value = [
        "conn_browser",
        "conn_node_desktop",  # never subscribed to teams events
    ]

    await fake_components["clients"][0].trigger(
        {
            "id": 1,
            "companyId": "co_user_a",
            "type": "activity.logged",
            "createdAt": "2026-05-04T01:00:00Z",
            "payload": {},
        }
    )

    sent_to = [c.args[0] for c in fake_components["mgmt"].send_message.call_args_list]
    assert sent_to == ["conn_browser"]
    assert "conn_node_desktop" not in sent_to
    # Crucially, query_by_user_id is NOT called for fanout (only local
    # subscriber set is consulted).
    fake_components["conn_svc"].query_by_user_id.assert_not_called()
    await broker.shutdown()
