"""The single-flight wake (``runtime_transport._ensure_session``) and the one stop helper.

AgentCore hands N concurrent invokes on a session with no compute N microVMs (dev repro and two
prod splits, 2026-09-25). These drive the real lease against moto's conditional writes.
"""

import asyncio
import json
import pathlib
import re
import threading
import time

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from unittest.mock import patch

from core.repositories import container_repo
from core.services import hermes_provisioning, runtime_transport as rt
from core.services.agentcore_runtime import IDLE_TIMEOUT_S, MAX_LIFETIME_S

OWNER = "org_split"
KEY = {"owner_id": f"__session__#{OWNER}"}
ARN = "arn:runtime/x"
#: Captured at import, before the conftest autouse fixture stubs it.
_REAL_CLEAR_SESSION = container_repo.clear_session


@pytest.fixture
def _runtime_session_gate_off():
    """Overrides the conftest autouse fixture: this module drives the real gate."""
    yield


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setattr(rt, "_WAKE_POLL_S", 0.02)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # moto's ConditionExpression handling reads-then-mutates the item across several Python
        # calls (parse, validate, execute) with no lock of its own -- real DynamoDB serializes a
        # conditional write per item server-side, moto does not. Under real OS threads (this
        # module's whole point) the GIL can switch mid-window and let two threads both pass the
        # same "attribute_not_exists(waking_until)" check before either writes, i.e. two winners
        # of one lease -- a moto gap, not a hole in the lease (verified: flaked 461/500 with
        # sys.setswitchinterval(0.00005); a real conditional write cannot double-win). Serializing
        # calls into moto here restores the atomicity DynamoDB itself guarantees, without changing
        # anything the tests below observe or assert.
        api_lock = threading.Lock()
        real_call = resource.meta.client._make_api_call

        def _serialized_call(*args, **kwargs):
            with api_lock:
                return real_call(*args, **kwargs)

        resource.meta.client._make_api_call = _serialized_call
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield t


def _row(table):
    return table.get_item(Key=KEY, ConsistentRead=True).get("Item")


def _far():
    return time.monotonic() + 30


class TestLease:
    def test_warm_session_invokes_without_waking(self, table):
        table.put_item(Item={**KEY, "warm_until": int(time.time()) + 600, "started_at": int(time.time()), "arn": ARN})
        woke = []
        assert rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1))["warm_until"] > time.time()
        assert woke == []

    def test_cold_session_wakes_once_and_records_warm(self, table):
        woke = []
        before = int(time.time())
        rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1))
        row = _row(table)
        assert woke == [1]
        assert "waking_until" not in row and "wake_tok" not in row
        assert row["started_at"] >= before
        # Idle-bound (lifetime is far away), and never past the lifetime cap from the claim.
        assert (
            row["started_at"] + IDLE_TIMEOUT_S - rt.WARM_MARGIN_S <= row["warm_until"] <= time.time() + IDLE_TIMEOUT_S
        )
        assert row["warm_until"] <= row["started_at"] + MAX_LIFETIME_S - rt.WARM_MARGIN_S

    def test_concurrent_cold_callers_wake_exactly_once(self, table):
        woke = []

        def _wake():
            woke.append(1)
            time.sleep(0.3)  # a cold start the others must wait out, not race

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(rt._ensure_session(OWNER, ARN, _far(), _wake)))
            for _ in range(5)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert woke == [1]
        assert len(results) == 5 and all(r and r["warm_until"] > time.time() for r in results)

    def test_a_waiter_out_of_budget_takes_the_retry_path_and_never_invokes(self, table):
        table.put_item(Item={**KEY, "waking_until": int(time.time()) + 120, "wake_tok": "other"})
        woke = []
        with pytest.raises(httpx.ReadTimeout):
            rt._ensure_session(OWNER, ARN, time.monotonic() + 0.1, lambda: woke.append(1))
        assert woke == []

    def test_warm_on_a_different_runtime_is_cold(self, table):
        # A re-created runtime (new ARN) has no session, whatever the old estimate says.
        table.put_item(Item={**KEY, "warm_until": int(time.time()) + 600, "started_at": 1, "arn": "arn:old"})
        woke = []
        rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1))
        assert woke == [1] and _row(table)["arn"] == ARN

    def test_winner_failure_drops_the_lease_and_the_next_caller_wins(self, table):
        def _boom():
            raise httpx.ConnectError("health failed")

        with pytest.raises(httpx.ConnectError):
            rt._ensure_session(OWNER, ARN, _far(), _boom)
        row = _row(table) or {}
        assert "waking_until" not in row and "warm_until" not in row
        woke = []
        rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1))
        assert woke == [1]

    def test_an_expired_lease_is_taken_over(self, table):
        # A winner whose backend task died: its lease lapses and a waiter takes it.
        table.put_item(Item={**KEY, "waking_until": int(time.time()) - 1, "wake_tok": "dead"})
        woke = []
        rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1))
        assert woke == [1]
        assert _row(table)["warm_until"] > time.time()

    def test_a_dead_winner_cannot_mark_warm_over_its_successor(self, table):
        container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="new", now=int(time.time()), lease_s=120)
        container_repo.finish_wake_sync(OWNER, token="old", warm_until=int(time.time()) + 999, started_at=1, arn=ARN)
        row = _row(table)
        assert row["wake_tok"] == "new" and "warm_until" not in row

    def test_dynamodb_fault_invokes_without_the_gate(self, table, monkeypatch):
        def _fault(owner_id):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")

        monkeypatch.setattr(container_repo, "get_session_sync", _fault)
        woke = []
        assert rt._ensure_session(OWNER, ARN, _far(), lambda: woke.append(1)) is None
        assert woke == []


class TestWarmEstimate:
    def test_extends_only_when_it_moves_more_than_the_step(self, table):
        now = int(time.time())
        fresh = {"warm_until": rt._warm_until(now, now), "started_at": now}
        table.put_item(Item={**KEY, **fresh})
        rt._note_invoked(OWNER, fresh)
        assert _row(table)["warm_until"] == fresh["warm_until"]  # no write for a small move

        stale = {"warm_until": now + 60, "started_at": now}
        table.put_item(Item={**KEY, **stale})
        before = int(time.time())
        rt._note_invoked(OWNER, stale)
        # _note_invoked reads the clock itself; a second boundary may pass on a slow runner.
        assert rt._warm_until(before, now) <= _row(table)["warm_until"] <= rt._warm_until(int(time.time()), now)

    def test_never_resurrects_a_row_a_stop_cleared(self, table):
        now = int(time.time())
        rt._note_invoked(OWNER, {"warm_until": now + 60, "started_at": now})
        assert _row(table) is None

    def test_lifetime_caps_the_estimate(self):
        started = 1_000_000
        now = started + MAX_LIFETIME_S - IDLE_TIMEOUT_S // 2  # less lifetime left than one idle window
        assert rt._warm_until(now, started) == started + MAX_LIFETIME_S - rt.WARM_MARGIN_S


class TestRequestEndToEnd:
    @pytest.mark.asyncio
    async def test_three_concurrent_cold_requests_send_one_health_first(self, table, monkeypatch):
        calls = []
        lock = threading.Lock()

        class Client:
            def invoke_agent_runtime(self, **kw):
                env = json.loads(kw["payload"])
                with lock:
                    calls.append(env["body"].get("method") if env["path"].endswith("/rpc") else env["path"])
                if env["path"].endswith("/rpc"):
                    time.sleep(0.2)  # the cold start
                return {"statusCode": 200, "response": '{"status":202,"body":"accepted"}'}

        monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
        container = {"owner_id": OWNER, "agentcore_runtime_arn": "arn:runtime/x", "gateway_token": "gw"}
        replies = await asyncio.gather(*(rt.request(container, f"/in/{i}", timeout_s=10) for i in range(3)))
        assert [r.status_code for r in replies] == [202, 202, 202]
        assert calls[0] == "health" and calls.count("health") == 1 and len(calls) == 4


class TestStopHelper:
    @pytest.fixture(autouse=True)
    def _real_clear(self, table):
        # The conftest fixture stubs clear_session; restore the real one for these tests.
        with patch.object(container_repo, "clear_session", _REAL_CLEAR_SESSION):
            yield

    @pytest.mark.asyncio
    async def test_stop_clears_the_estimate(self, table, monkeypatch):
        table.put_item(Item={**KEY, "arn": ARN, "warm_until": int(time.time()) + 600, "started_at": 1})
        monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda arn, sid: None)
        assert await hermes_provisioning.stop_runtime_session(OWNER, "arn:runtime/x") is True
        assert _row(table) is None

    @pytest.mark.asyncio
    async def test_not_found_is_a_stop_and_a_failure_still_clears(self, table, monkeypatch):
        def _gone(arn, sid):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "StopRuntimeSession")

        monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", _gone)
        assert await hermes_provisioning.stop_runtime_session(OWNER, "arn") is True

        def _throttled(arn, sid):
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "StopRuntimeSession")

        table.put_item(Item={**KEY, "arn": "arn", "warm_until": int(time.time()) + 600, "started_at": 1})
        monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", _throttled)
        assert await hermes_provisioning.stop_runtime_session(OWNER, "arn") is False
        assert _row(table) is None  # "cold" is the safe error


def test_every_stop_goes_through_the_helper():
    """A stop that skips the helper leaves the estimate reading "warm" over a dead session."""
    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", ".venv/")) or "/site-packages/" in rel:
            continue
        text = path.read_text()
        if rel == "core/services/hermes_provisioning.py":
            # The raw call lives in exactly two places: its own def and the helper.
            assert len(re.findall(r"_stop_runtime_session\b", text)) == 3, "raw stop used outside the helper"
            continue
        if re.search(r"_stop_runtime_session\b|\.stop_runtime_session\(\s*agentRuntimeArn", text):
            offenders.append(rel)
    assert offenders == []
