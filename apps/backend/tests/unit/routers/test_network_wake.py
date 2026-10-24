"""The network doorbell: a teammate's Mac wakes this parked workspace with one content-free POST."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import logging
import time
from unittest.mock import AsyncMock

import pytest
from boto3.dynamodb.conditions import Key

from core.repositories import messaging_repo
from core.services import network_member
from tests.unit.repositories.test_messaging_repo import OWNER, table  # noqa: F401


def _rows(t, prefix):
    resp = t.query(KeyConditionExpression=Key("pk").eq(f"OWNER#{OWNER}") & Key("sk").begins_with(prefix))
    return resp["Items"]


@pytest.fixture
def wake(table, monkeypatch):
    monkeypatch.setattr(network_member.settings, "PUBLIC_API_BASE", "https://api-dev.thelightbulb.company")
    return table


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_a_bad_token_is_404_and_writes_nothing(wake, unauthenticated_async_client, monkeypatch):
    refused = []
    monkeypatch.setattr("core.observability.metrics.put_metric", lambda name, *a, **kw: refused.append((name, a, kw)))
    entry = await network_member._wake(OWNER, {})
    assert entry["url"] == "https://api-dev.thelightbulb.company/api/v1/network/wake"
    good = entry["token"]
    headers = [{}, {"Authorization": f"Basic {good}"}]
    for bad in (
        "nodot",
        "user_1.abc",
        f"org_other.{good.split('.', 1)[1]}",
        f"{OWNER}.wrong",
        "org_bad!.y",
        f"{OWNER}.{'x' * 200}",  # over-long: refused before any read
    ):
        headers.append(_bearer(bad))
    for h in headers:
        resp = await unauthenticated_async_client.post("/api/v1/network/wake", headers=h)
        assert resp.status_code == 404 and resp.content == b""
    # the token never rides the path
    assert (await unauthenticated_async_client.post(f"/api/v1/network/wake/{good}")).status_code in (404, 405)
    assert _rows(wake, "WORK#") == []
    assert "lastWakeAt" not in _rows(wake, "NETWAKE")[0]
    assert refused == [("network.wake.refused", (1,), {})] * len(headers)


async def test_one_wake_per_workspace_per_10_s(wake, unauthenticated_async_client):
    token = (await network_member._wake(OWNER, {}))["token"]
    for _ in range(50):
        resp = await unauthenticated_async_client.post("/api/v1/network/wake", headers=_bearer(token))
        assert resp.status_code == 202 and resp.content == b""
    [row] = _rows(wake, "WORK#NETWAKE#")
    assert row["payload"] == {"kind": "network_wake", "ownerId": OWNER}
    assert row["messageGroupId"] == f"netwake#{OWNER}"
    assert row["messageDeduplicationId"] == row["sk"].removeprefix("WORK#NETWAKE#")
    assert abs(int(row["expiresAt"]) - (time.time() + 7 * 24 * 3600)) < 30
    # the token is minted once and reused
    assert (await network_member._wake(OWNER, {"wake": {"token": token}}))["token"] == token


async def test_the_token_never_appears_in_logs(wake, unauthenticated_async_client, caplog):
    token = (await network_member._wake(OWNER, {}))["token"]
    secret = token.split(".", 1)[1]
    with caplog.at_level(logging.DEBUG):
        for t in (token, token, f"{OWNER}.wrong"):  # queued, inside the window, a mismatch
            await unauthenticated_async_client.post("/api/v1/network/wake", headers=_bearer(t))
    assert caplog.records
    for r in caplog.records:
        text = r.getMessage()
        assert secret not in text and token not in text and f"Bearer {token}" not in text


async def test_reconnect_deletes_the_netwake_row(wake, monkeypatch):
    await network_member._wake(OWNER, {})
    monkeypatch.setattr("core.services.agentcore_runtime.drop_network_key", AsyncMock())
    monkeypatch.setattr(network_member, "_runtime_arn", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.hermes_provisioning._delete_prefixes", lambda prefixes: None)
    await network_member.reconnect(OWNER)
    assert await messaging_repo._get(messaging_repo.owner_pk(OWNER), messaging_repo.netwake_sk()) is None
