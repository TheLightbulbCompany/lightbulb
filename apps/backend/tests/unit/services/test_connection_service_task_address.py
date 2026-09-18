"""The relay presence row carries the accepting backend task's address.

Spec §4.4 (M2 node exec): the desktop's WebSocket is pinned to whichever
backend task accepted the upgrade, so the presence row `put_relay_presence`
writes must record that task's own address, `refresh_relay_presence` must
re-assert it on every 60s tick, and `get_connection` must project it so a
peer task can read it back.
"""

import pytest

from core.services import task_address
from core.services.connection_service import ConnectionService


def test_the_presence_row_records_the_accepting_tasks_address(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "put_item", lambda **kw: captured.update(kw))
    identity = type(
        "I",
        (),
        {
            "user_id": "u",
            "org_id": "o",
            "owner_id": "org_1",
            "kind": "node",
            "auth_kind": "device",
            "client_id": "node-host",
            "device_id": "dev-1",
        },
    )()
    import asyncio

    asyncio.run(service.put_relay_presence(connection_id="relay#1", identity=identity))
    assert captured["Item"]["taskAddress"] == {"S": "http://10.0.1.7:8000"}


def test_the_refresh_re_asserts_the_address_so_a_row_never_goes_addressless(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "update_item", lambda **kw: captured.update(kw))
    import asyncio

    asyncio.run(service.refresh_relay_presence("relay#1"))
    assert ":addr" in captured["ExpressionAttributeValues"]
    assert "taskAddress" in captured["UpdateExpression"]


def test_own_address_is_the_tasks_own_eni_ip(monkeypatch):
    task_address.own_address.cache_clear()
    monkeypatch.setattr(task_address.socket, "gethostbyname", lambda host: "10.0.2.9")
    assert task_address.own_address() == "http://10.0.2.9:8000"


def test_own_address_propagates_a_resolution_failure_instead_of_a_bogus_address(monkeypatch):
    """Ruling: keep it loud -- a wrong address is worse than a failed connect,
    so a resolver failure must raise rather than being swallowed into some
    placeholder. lru_cache never caches a raised call, so a later successful
    resolution must still work."""
    task_address.own_address.cache_clear()

    def _boom(host):
        raise OSError("no such host")

    monkeypatch.setattr(task_address.socket, "gethostbyname", _boom)
    with pytest.raises(OSError):
        task_address.own_address()

    monkeypatch.setattr(task_address.socket, "gethostbyname", lambda host: "10.0.3.3")
    assert task_address.own_address() == "http://10.0.3.3:8000"
