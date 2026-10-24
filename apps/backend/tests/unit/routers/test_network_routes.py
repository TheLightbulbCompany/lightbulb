"""Settings -> Network: connect status, the invite line, and reconnect."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import network_member

LINE = "lightbulb-invite:SECRET-LINE-abc123"


@pytest.fixture
def no_aws(monkeypatch):
    """Any boto call (a DynamoDB put_item, an S3 put) fails the test."""

    def _refuse(self, operation, params):
        raise AssertionError(f"unexpected AWS call {operation}")

    monkeypatch.setattr("botocore.client.BaseClient._make_api_call", _refuse)


@pytest.mark.asyncio
async def test_status_reports_whether_sessions_json_exists(async_client_org_member, monkeypatch):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=False))
    resp = await async_client_org_member.get("/api/v1/settings/network")
    assert resp.status_code == 200 and resp.json() == {"connected": False}


@pytest.mark.asyncio
async def test_invite_route_returns_the_line_once_and_stores_nothing(
    async_client_org_member, monkeypatch, caplog, no_aws
):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=True))
    prepare = AsyncMock(side_effect=AssertionError("a member workspace is not re-prepared"))
    monkeypatch.setattr("core.services.hermes_provisioning.prepare_hermes_workspace", prepare)
    rpc = AsyncMock(return_value={"line": LINE, "expires_at": "2026-10-24T12:00:00Z"})
    monkeypatch.setattr(network_member.container_rpc, "call", rpc)

    with caplog.at_level(logging.DEBUG):
        resp = await async_client_org_member.post("/api/v1/settings/network/invite", json={"name": "Sam"})

    assert resp.status_code == 200
    assert resp.json() == {"line": LINE, "expiresAt": "2026-10-24T12:00:00Z"}
    assert resp.headers["cache-control"] == "no-store"
    rpc.assert_awaited_once_with("org_test_456", "network.invite", {"for": "user_test_789", "name": "Sam"})
    assert not any(LINE in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_invite_on_a_workspace_not_yet_a_member_prepares_and_restarts_first(async_client_org_member, monkeypatch):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=False))
    order = []
    monkeypatch.setattr(
        "core.services.hermes_provisioning.prepare_hermes_workspace",
        AsyncMock(side_effect=lambda o: order.append("prepare") or True),
    )
    monkeypatch.setattr(
        "core.services.hermes_provisioning.stop_runtime_session",
        AsyncMock(side_effect=lambda o, arn: order.append(("stop", arn)) or True),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": "arn:r"})
    )

    async def _rpc(owner_id, method, params=None, **kw):
        order.append(method)
        return {"line": LINE, "expires_at": "t"}

    monkeypatch.setattr(network_member.container_rpc, "call", _rpc)
    resp = await async_client_org_member.post("/api/v1/settings/network/invite")
    assert resp.status_code == 200
    assert order == ["prepare", ("stop", "arn:r"), "network.invite"]


@pytest.mark.asyncio
async def test_reconnect_is_admin_only_and_drops_key_and_state(
    app, async_client_org_member, mock_org_admin_user, monkeypatch
):
    from core.auth import get_current_user

    calls = []

    async def _drop(owner_id):
        calls.append(("drop", owner_id))

    monkeypatch.setattr("core.services.agentcore_runtime.drop_network_key", _drop)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.stop_runtime_session",
        AsyncMock(side_effect=lambda o, arn: calls.append(("stop", arn)) or True),
    )
    monkeypatch.setattr("core.services.hermes_provisioning._delete_prefixes", lambda p: calls.append(("delete", p)))
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": "arn:r"})
    )
    table = MagicMock()
    table.delete_item.side_effect = lambda Key: calls.append(("delete_row", Key["sk"]))
    monkeypatch.setattr("core.repositories.messaging_repo._table", lambda: table)

    member = await async_client_org_member.post("/api/v1/settings/network/reconnect")
    assert member.status_code == 403 and calls == []

    app.dependency_overrides[get_current_user] = mock_org_admin_user
    admin = await async_client_org_member.post("/api/v1/settings/network/reconnect")
    assert admin.status_code == 200 and admin.json() == {}
    assert calls == [
        ("drop", "org_test_456"),
        ("stop", "arn:r"),
        ("delete", ["org_test_456/network/"]),
        ("delete_row", "NETWAKE"),
    ]
