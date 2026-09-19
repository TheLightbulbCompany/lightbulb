"""Tests for ConnectionService's gateway-relay presence rows: TTL-stamped
put, conditional refresh (False when the row is gone), and node count."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services import task_address
from core.services.connection_service import ConnectionService
from core.services.machine_auth import MachineIdentity

IDENT = MachineIdentity(
    user_id="user_1",
    org_id="org_1",
    owner_id="org_1",
    kind="node",
    auth_kind="device",
    client_id="openclaw-macos",
    device_id="a" * 64,
)


def _svc():
    svc = ConnectionService(table_name="t")
    svc._client = MagicMock()
    return svc


@pytest.fixture(autouse=True)
def _stub_task_address(monkeypatch):
    """put_relay_presence / refresh_relay_presence resolve task_address.own_address(),
    which shells out to the OS resolver -- stub it so no test in this file
    touches the real socket.gethostbyname/gethostname."""
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.9.9:8000")


@pytest.mark.asyncio
async def test_put_writes_the_presence_row_with_a_ttl():
    svc = _svc()
    with patch("core.services.connection_service.time.time", return_value=1_000):
        await svc.put_relay_presence(connection_id="relay#1", identity=IDENT)
    item = svc._client.put_item.call_args.kwargs["Item"]
    assert (
        item["connectionId"] == {"S": "relay#1"}
        and item["ownerId"] == {"S": "org_1"}
        and item["connectionType"] == {"S": "node"}
    )
    assert item["deviceId"] == {"S": "a" * 64} and item["ttl"] == {"N": "1120"}


@pytest.mark.asyncio
async def test_refresh_reports_a_deleted_row():
    svc = _svc()
    assert await svc.refresh_relay_presence("relay#1") is True
    assert svc._client.update_item.call_args.kwargs["ConditionExpression"] == "attribute_exists(connectionId)"
    svc._client.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    assert await svc.refresh_relay_presence("relay#1") is False


@pytest.mark.asyncio
async def test_put_never_touches_the_real_os_resolver(monkeypatch):
    """put_relay_presence resolves task_address.own_address(), which shells
    out to socket.gethostbyname/gethostname -- the autouse fixture above must
    stub it so no test in this file depends on (or blocks on) the sandbox's
    real hostname resolution. If own_address were NOT stubbed, this would
    call the resolver and raise."""

    def _boom(host):
        raise AssertionError("real DNS resolver was called")

    monkeypatch.setattr(task_address.socket, "gethostbyname", _boom)
    svc = _svc()
    await svc.put_relay_presence(connection_id="relay#1", identity=IDENT)
