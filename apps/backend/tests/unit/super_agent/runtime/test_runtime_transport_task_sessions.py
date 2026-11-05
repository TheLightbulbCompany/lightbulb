"""Per-task AgentCore sessions (wave-2 V1): ids, routing, wake rows, stop.

A task session is a second microVM for one task. The 16 Sep invariant (one writer per S3
profile prefix) holds because a task session only ever writes its own task prefix (bridge
task mode, V2); here we only prove the backend addresses and tracks it apart from the
workspace session, and that nothing changes when no ``task_id`` is passed.
"""

import hashlib
import json
import re
import time

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch

from core.repositories import container_repo
from core.services import hermes_provisioning, runtime_transport as rt

OWNER = "org_abc"
TASK = "7d1e0c6a-4b1f-4d55-9c1e-1f2a3b4c5d6e"
ARN = "arn:aws:bedrock-agentcore:::runtime/r"
_REAL_CLEAR_SESSION = container_repo.clear_session


def test_workspace_session_id_unchanged():
    # The pre-change formula, copied literally: the workspace id must not move by one byte.
    digest = hashlib.sha256(OWNER.encode()).hexdigest()
    assert rt.session_id_for(OWNER) == f"lb-{OWNER}-{digest[:16]}".ljust(33, "0")
    assert rt.session_id_for(OWNER, None) == rt.session_id_for(OWNER)


def test_task_session_id_shape():
    sid = rt.session_id_for(OWNER, TASK)
    assert re.fullmatch(r"lb-org_abc-t-[0-9a-f]{20}", sid)
    assert sid == f"lb-{OWNER}-t-{hashlib.sha256(TASK.encode()).hexdigest()[:20]}"
    assert len(sid) >= 33
    assert sid == rt.session_id_for(OWNER, TASK)
    assert sid != rt.session_id_for(OWNER, TASK + "x")
    assert sid != rt.session_id_for(OWNER)
    short = rt.session_id_for("u_1", TASK)
    assert len(short) == 33 and short.startswith("lb-u_1-t-") and short.endswith("0")


class _Body:
    def read(self):
        return json.dumps({"status": 200, "body": {"ok": True}}).encode()


@pytest.mark.asyncio
async def test_request_routes_task_id_to_invoke(monkeypatch):
    seen: list[str] = []

    class _Client:
        def invoke_agent_runtime(self, **kw):
            seen.append(kw["runtimeSessionId"])
            return {"statusCode": 200, "response": _Body()}

    monkeypatch.setattr("boto3.client", lambda *a, **k: _Client())
    container = {"agentcore_runtime_arn": ARN, "owner_id": OWNER}
    await rt.request(container, "/x", task_id=TASK)
    await rt.request(container, "/x")
    assert seen == [rt.session_id_for(OWNER, TASK), rt.session_id_for(OWNER)]


@pytest.fixture
def _runtime_session_gate_off():
    """Overrides the conftest autouse fixture: these tests drive the real rows."""
    yield


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(container_repo, "clear_session", _REAL_CLEAR_SESSION),
        ):
            yield t


def _keys(table) -> set[str]:
    return {item["owner_id"] for item in table.scan()["Items"]}


def test_wake_rows_are_per_session(table):
    now = int(time.time())
    assert container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="t", now=now, lease_s=120, task_id=TASK)
    # The task's lease does not block the workspace's, and vice versa.
    assert container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="w", now=now, lease_s=120)
    assert not container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="x", now=now, lease_s=120, task_id=TASK)
    assert not container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="x", now=now, lease_s=120)
    assert _keys(table) == {f"__session__#{OWNER}", f"__session__#{OWNER}#task#{TASK}"}

    container_repo.finish_wake_sync(OWNER, token="t", warm_until=now + 600, started_at=now, arn=ARN, task_id=TASK)
    assert container_repo.get_session_sync(OWNER, task_id=TASK)["warm_until"] == now + 600
    assert "warm_until" not in container_repo.get_session_sync(OWNER)
    container_repo.extend_session_warm_sync(OWNER, warm_until=now + 900, task_id=TASK)
    assert container_repo.get_session_sync(OWNER, task_id=TASK)["warm_until"] == now + 900


def test_ensure_session_uses_the_task_row(table):
    woke: list[int] = []
    row = rt._ensure_session(OWNER, ARN, time.monotonic() + 30, lambda: woke.append(1), TASK)
    assert woke == [1] and row["arn"] == ARN
    assert _keys(table) == {f"__session__#{OWNER}#task#{TASK}"}


@pytest.mark.asyncio
async def test_stop_task_session_leaves_workspace_warm(table, monkeypatch):
    now = int(time.time())
    table.put_item(Item={"owner_id": f"__session__#{OWNER}", "warm_until": now + 600, "started_at": now, "arn": ARN})
    table.put_item(
        Item={"owner_id": f"__session__#{OWNER}#task#{TASK}", "warm_until": now + 600, "started_at": now, "arn": ARN}
    )
    stopped: list[tuple[str, str]] = []
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda arn, sid: stopped.append((arn, sid)))

    assert await hermes_provisioning.stop_runtime_session(OWNER, ARN, task_id=TASK) is True

    assert stopped == [(ARN, rt.session_id_for(OWNER, TASK))]
    assert _keys(table) == {f"__session__#{OWNER}"}
