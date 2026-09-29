"""The relay presence row carries the accepting backend task's address.

Spec §4.4 (M2 node exec): the desktop's WebSocket is pinned to whichever
backend task accepted the upgrade, so the presence row `put_relay_presence`
writes must record that task's own address, `refresh_relay_presence` must
re-assert it on every 60s tick, and `get_connection` must project it so a
peer task can read it back.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services import task_address
from core.services import connection_service
from core.services.connection_service import (
    ConnectionService,
    DesktopTargetForbidden,
    DesktopTargetNotReady,
    DesktopTargetStale,
    resolve_desktop_target,
)
from core.services.machine_auth import MachineIdentity


@pytest.fixture
def ready_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="desktop-ready-test",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "connectionId", "AttributeType": "S"},
                {"AttributeName": "ownerId", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner-id",
                    "KeySchema": [{"AttributeName": "ownerId", "KeyType": "HASH"}],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": ["userId", "connectionType"],
                    },
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        service = ConnectionService(table_name="desktop-ready-test", region_name="us-east-1")
        monkeypatch.setattr(connection_service, "_singleton", service)
        yield service


def _identity(device_id="cli-1", client_id="node-host"):
    return MachineIdentity(
        user_id="member-1",
        org_id="org-1",
        owner_id="org-1",
        kind="node",
        auth_kind="device",
        client_id=client_id,
        device_id=device_id,
    )


def _paired_devices():
    return {
        "mac-1": {
            "userId": "member-1",
            "clientId": "openclaw-macos",
            "pairingGroupId": "physical-mac-1",
            "commands": ["device.info"],
        },
        "cli-1": {
            "userId": "member-1",
            "clientId": "node-host",
            "pairingGroupId": "physical-mac-1",
            "commands": ["system.run"],
        },
        "mac-2": {
            "userId": "member-1",
            "clientId": "openclaw-macos",
            "pairingGroupId": "physical-mac-2",
            "commands": ["device.info"],
        },
        "cli-2": {
            "userId": "member-1",
            "clientId": "node-host",
            "pairingGroupId": "physical-mac-2",
            "commands": ["system.run"],
        },
    }


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


@pytest.mark.asyncio
async def test_exact_device_generation_resolves_to_the_accepting_task(ready_table, monkeypatch):
    paired = _paired_devices()
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.2.8:8000")
    await ready_table.publish_desktop_ready(
        identity=_identity("cli-2"), generation="generation-2", commands={"system.run"}
    )
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-1", commands={"system.run"})
    pairing_read = AsyncMock(return_value={"paired_devices": paired})
    with patch("core.services.connection_service.container_repo.get_by_owner_id", pairing_read):
        target = await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "system.run")

    pairing_read.assert_awaited_once_with("org-1", consistent=True)
    assert target == {
        "taskAddress": "http://10.0.1.7:8000",
        "ownerId": "org-1",
        "memberId": "member-1",
        "deviceId": "cli-1",
        "clientId": "node-host",
        "pairingGroupId": "physical-mac-1",
        "generation": "generation-1",
        "commands": ["system.run"],
    }


@pytest.mark.asyncio
async def test_competing_generation_fences_old_refresh_disconnect_and_resolution(ready_table, monkeypatch):
    paired = _paired_devices()
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-old", commands={"system.run"})
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.2.8:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-new", commands={"system.run"})

    assert await ready_table.refresh_desktop_ready("org-1", "cli-1", "generation-old") is False
    await ready_table.delete_desktop_ready("org-1", "cli-1", "generation-old")
    with patch(
        "core.services.connection_service.container_repo.get_by_owner_id",
        AsyncMock(return_value={"paired_devices": paired}),
    ):
        with pytest.raises(DesktopTargetStale):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-old", "system.run")
        current = await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-new", "system.run")

    assert current["taskAddress"] == "http://10.0.2.8:8000"


@pytest.mark.asyncio
async def test_resolver_rechecks_generation_after_reading_the_current_grant(ready_table, monkeypatch):
    paired = _paired_devices()
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-old", commands={"system.run"})

    async def _reconnect_during_grant_read(*_args, **_kwargs):
        monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.2.8:8000")
        await ready_table.publish_desktop_ready(
            identity=_identity(), generation="generation-new", commands={"system.run"}
        )
        return {"paired_devices": paired}

    with patch("core.services.connection_service.container_repo.get_by_owner_id", _reconnect_during_grant_read):
        with pytest.raises(DesktopTargetStale):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-old", "system.run")


@pytest.mark.asyncio
async def test_competing_first_publishers_leave_exactly_one_current_generation(ready_table, monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    real_get = ready_table._client.get_item
    both_read_absent = threading.Barrier(2)
    put_lock = threading.Lock()

    def _racing_get(**kwargs):
        result = real_get(**kwargs)
        both_read_absent.wait(timeout=5)
        return result

    real_put = ready_table._client.put_item

    def _serialized_put(**kwargs):
        # Moto's condition-check/write is non-atomic; model DynamoDB's atomic PutItem.
        with put_lock:
            return real_put(**kwargs)

    monkeypatch.setattr(ready_table._client, "get_item", _racing_get)
    monkeypatch.setattr(ready_table._client, "put_item", _serialized_put)
    results = await asyncio.gather(
        ready_table.publish_desktop_ready(identity=_identity(), generation="generation-a", commands={"system.run"}),
        ready_table.publish_desktop_ready(identity=_identity(), generation="generation-b", commands={"system.run"}),
        return_exceptions=True,
    )

    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], ClientError)
    assert failures[0].response["Error"]["Code"] == "ConditionalCheckFailedException"
    current = real_get(
        TableName=ready_table.table_name,
        Key={"connectionId": {"S": ready_table.desktop_ready_id("org-1", "cli-1")}},
        ConsistentRead=True,
    )["Item"]
    assert current["generation"]["S"] in {"generation-a", "generation-b"}


@pytest.mark.asyncio
async def test_resolution_checks_expiry_linkage_member_and_current_grant(ready_table, monkeypatch):
    paired = _paired_devices()
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-1", commands={"system.run"})

    async def _row(*_args, **_kwargs):
        return {"paired_devices": paired}

    with patch("core.services.connection_service.container_repo.get_by_owner_id", _row):
        with pytest.raises(DesktopTargetForbidden):
            await resolve_desktop_target("org-1", "wrong-member", "cli-1", "generation-1", "system.run")

        paired["cli-1"]["commands"] = []
        with pytest.raises(DesktopTargetForbidden):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "system.run")

        paired["cli-1"]["commands"] = ["system.run"]
        paired["mac-1"].pop("pairingGroupId")
        with pytest.raises(DesktopTargetNotReady):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "system.run")


@pytest.mark.asyncio
async def test_device_revoke_removes_its_authoritative_ready_record(ready_table, monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-1", commands={"system.run"})

    assert await ready_table.delete_relay_rows_for_device("org-1", "cli-1") == 1
    with pytest.raises(DesktopTargetNotReady):
        await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "system.run")

    ready_table._client.update_item(
        TableName=ready_table.table_name,
        Key={"connectionId": {"S": ready_table.desktop_ready_id("org-1", "cli-1")}},
        UpdateExpression="SET #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": {"N": str(int(time.time()) - 1)}},
    )
    paired = _paired_devices()
    with patch(
        "core.services.connection_service.container_repo.get_by_owner_id",
        AsyncMock(return_value={"paired_devices": paired}),
    ):
        with pytest.raises(DesktopTargetNotReady):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "system.run")


@pytest.mark.asyncio
async def test_a_device_being_unpaired_refuses_every_command_but_the_internal_clear(ready_table, monkeypatch):
    paired = _paired_devices()
    paired["mac-1"]["revoking"] = True  # DELETE /desktop/devices/mac-1 is clearing sign-ins
    paired["cli-1"]["commands"] = ["system.run", "file.fetch"]
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(
        identity=_identity(), generation="generation-1", commands={"system.run", "file.fetch"}
    )
    with patch(
        "core.services.connection_service.container_repo.get_by_owner_id",
        AsyncMock(return_value={"paired_devices": paired}),
    ):
        for command in ("system.run", "file.fetch"):  # a shell or file action during the clear
            with pytest.raises(DesktopTargetForbidden):
                await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", command)
        target = await ready_table._resolve_desktop_target(
            "org-1", "member-1", "cli-1", "generation-1", "file.fetch", allow_revoking=True
        )
    assert target["deviceId"] == "cli-1"


@pytest.mark.asyncio
async def test_the_internal_clear_reaches_a_device_without_that_grant(ready_table, monkeypatch):
    paired = _paired_devices()  # cli-1 grants system.run only: Files is off
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    await ready_table.publish_desktop_ready(identity=_identity(), generation="generation-1", commands={"system.run"})
    with patch(
        "core.services.connection_service.container_repo.get_by_owner_id",
        AsyncMock(return_value={"paired_devices": paired}),
    ):
        with pytest.raises(DesktopTargetForbidden):
            await resolve_desktop_target("org-1", "member-1", "cli-1", "generation-1", "file.fetch")
        target = await ready_table._resolve_desktop_target(
            "org-1", "member-1", "cli-1", "generation-1", "file.fetch", allow_ungranted=True
        )
    assert target["deviceId"] == "cli-1"
