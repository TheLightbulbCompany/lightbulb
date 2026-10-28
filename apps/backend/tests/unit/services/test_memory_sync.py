"""memory_sync: AgentCore facts <-> network log entries, and room turns -> searchable records."""

from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import ANY, Stubber
from moto import mock_aws

from core.repositories import messaging_repo
from core.services import memory_sync

OWNER = "org_1"
NS = {"facts": "/owners/org_1/", "turns": "/owners/org_1/turns/", "entries": "/owners/org_1/entries/"}


def rid(x):
    return x.ljust(40, "0")


MEM = "mem-1234567890"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def table():
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="msg",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch.object(messaging_repo, "_table", return_value=t):
            yield t


@pytest.fixture
def agentcore():
    client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(client) as stub, patch.object(memory_sync, "_client", return_value=client):
        yield stub
        stub.assert_no_pending_responses()


class Member:
    """Fake cloud member: records ops, answers memory.import with sequential ids."""

    def __init__(self, entries=None, up=True):
        self.calls, self.entries, self.up, self.n = [], entries or [], up, 0

    async def __call__(self, owner_id, op, params, timeout=30.0):
        if not self.up:
            return None
        self.calls.append((op, params))
        if op == "memory.import":
            ids = [f"e{self.n + i}" for i in range(len(params["entries"]))]
            self.n += len(ids)
            return {"ids": ids}
        if op == "memory.list":
            return {"entries": [e for e in self.entries if e["seq"] > params["since_seq"]]}
        return {}

    def ops(self, name):
        return [p for o, p in self.calls if o == name]


def fact(record_id, text, **meta):
    meta = {"entity": "api", "attribute": "port", "kind": "fact", "validFrom": "2026-10-01", **meta}
    return {
        "memoryRecordId": record_id,
        "content": {"text": text},
        "memoryStrategyId": "s",
        "namespaces": [NS["facts"]],
        "createdAt": T0,
        "metadata": {k: {"stringValue": v} for k, v in meta.items()},
    }


def no_sessions(stub):
    stub.add_response("list_sessions", {"sessionSummaries": []})


async def run(member, **kw):
    with (
        patch("core.services.network_member.call_op", member),
        patch("core.services.network_member.drain_ops", return_value=0),
    ):
        return await memory_sync.sync_workspace(OWNER, memory_id=MEM, namespaces=kw.get("ns", NS))


def entry(seq, eid, op, author="workspace"):
    return {"id": eid, "author": author, "seq": seq, "op": op, "at": "2026-10-02T00:00:00+00:00"}


async def test_sync_imports_new_facts_as_entries(table, agentcore):
    agentcore.add_response(
        "list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080 (since 2026-10-01)")]}
    )
    no_sessions(agentcore)
    member = Member()
    out = await run(member)
    assert out["imported"] == 1
    (call,) = member.ops("memory.import")
    assert call["entries"][0] == {
        "subject": "api port",
        "fact": "8080",
        "kind": "fact",
        "scope": "team",
        "when": "2026-10-01",
        "source": {"harness": "workspace", "repo": None, "session": None, "on_behalf_of": "Ana"},
        "replaces": [],
    }
    assert memory_sync._get(OWNER, "MEMLINK#" + rid("r1"))["entry"] == "e0"


async def test_sync_rewrite_becomes_replace_entry(table, agentcore):
    for text in ("Ana: api port: 8080", "Ana: api port: 9090 (was 8080)"):
        agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), text)]})
        no_sessions(agentcore)
    member = Member()
    await run(member)
    await run(member)
    first, second = member.ops("memory.import")
    assert second["entries"][0]["replaces"] == ["e0"] and second["entries"][0]["fact"] == "9090"
    assert memory_sync._get(OWNER, "MEMLINK#" + rid("r1"))["entry"] == "e1"


async def test_sync_rerun_imports_nothing_new(table, agentcore):
    for _ in range(2):
        agentcore.add_response(
            "list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]}
        )
        no_sessions(agentcore)
    member = Member()
    await run(member)
    assert (await run(member))["imported"] == 0
    assert len(member.ops("memory.import")) == 1


async def test_sync_mirrors_entries_and_tombstones(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_create_memory_records",
        {
            "successfulRecords": [{"memoryRecordId": rid("ar1"), "status": "SUCCEEDED", "requestIdentifier": "x1"}],
            "failedRecords": [],
        },
        {
            "memoryId": MEM,
            "records": [
                {
                    "requestIdentifier": "x1",
                    "namespaces": [NS["entries"]],
                    "content": {"text": "db host: pg1"},
                    "timestamp": ANY,
                    "metadata": {
                        "subject": {"stringValue": "db host"},
                        "status": {"stringValue": "current"},
                        "kind": {"stringValue": "fact"},
                        "author": {"stringValue": "ana"},
                        "entryId": {"stringValue": "x1"},
                    },
                }
            ],
        },
    )
    no_sessions(agentcore)
    remember = {"t": "remember", "subject": "db host", "fact": "pg1", "kind": "fact"}
    out = await run(Member([entry(1, "x1", remember, author="ana")]))
    assert out["mirrored"] == 1
    assert memory_sync._get(OWNER, "MEMENTRY#x1")["arec"] == rid("ar1")
    assert int(memory_sync._get(OWNER, "MEMSYNC")["entries_seq"]) == 1

    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_delete_memory_records",
        {"successfulRecords": [], "failedRecords": []},
        {"memoryId": MEM, "records": [{"memoryRecordId": rid("ar1")}]},
    )
    no_sessions(agentcore)
    out = await run(Member([entry(1, "x1", remember), entry(2, "t1", {"t": "forget", "target": "x1"})]))
    assert out["mirrored"] == 1 and memory_sync._get(OWNER, "MEMENTRY#x1") == {}


async def test_sync_replace_marks_replaced_record_status(table, agentcore):
    memory_sync._put(OWNER, "MEMENTRY#x1", arec=rid("ar1"))
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_create_memory_records",
        {
            "successfulRecords": [{"memoryRecordId": rid("ar2"), "status": "SUCCEEDED", "requestIdentifier": "x2"}],
            "failedRecords": [],
        },
    )
    agentcore.add_response(
        "batch_update_memory_records",
        {"successfulRecords": [], "failedRecords": []},
        {
            "memoryId": MEM,
            "records": [
                {"memoryRecordId": rid("ar1"), "timestamp": ANY, "metadata": {"status": {"stringValue": "replaced"}}}
            ],
        },
    )
    no_sessions(agentcore)
    op = {"t": "remember", "subject": "db host", "fact": "pg2", "kind": "fact", "replaces": ["x1"]}
    await run(Member([entry(2, "x2", op)]))


async def test_sync_tombstone_of_imported_entry_deletes_facts_record(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]})
    no_sessions(agentcore)
    await run(Member())
    memory_sync._put(OWNER, "MEMENTRY#e0", arec=rid("ar0"), facts_rec=rid("r1"))
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_delete_memory_records",
        {"successfulRecords": [], "failedRecords": []},
        {"memoryId": MEM, "records": [{"memoryRecordId": rid("ar0")}, {"memoryRecordId": rid("r1")}]},
    )
    no_sessions(agentcore)
    await run(Member([entry(5, "t1", {"t": "forget", "target": "e0"})]))
    assert memory_sync._get(OWNER, "MEMLINK#" + rid("r1")) == {}


async def test_sync_skips_private_namespace(table, agentcore):
    member = Member()
    private = {**NS, "facts": "/owners/org_1:user_a/"}
    assert await run(member, ns=private) == {"imported": 0, "mirrored": 0, "turns": 0}
    assert member.calls == []  # and no AgentCore call: the stubber has nothing queued


async def test_sync_if_stale_noop_within_max_age(table, agentcore):
    memory_sync._put(OWNER, "MEMSYNC", synced_at=int(__import__("time").time()))
    await memory_sync.sync_if_stale(OWNER, memory_id=MEM, namespaces=NS)  # any AgentCore call would fail


async def test_sync_when_parked_leaves_watermark(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]})
    no_sessions(agentcore)
    out = await run(Member(up=False))
    assert out["imported"] == 0
    state = memory_sync._get(OWNER, "MEMSYNC")
    assert "records_after" not in state and "synced_at" not in state
    assert memory_sync._get(OWNER, "MEMLINK#" + rid("r1")) == {}


async def test_sync_mirrors_events_into_turns(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "list_sessions", {"sessionSummaries": [{"sessionId": "s1", "actorId": OWNER, "createdAt": T0}]}
    )
    payload = [{"conversational": {"role": "USER", "content": {"text": "Ana: ship it"}}}, {"blob": {}}]
    agentcore.add_response(
        "list_events",
        {
            "events": [
                {
                    "memoryId": MEM,
                    "actorId": OWNER,
                    "sessionId": "s1",
                    "eventId": "ev1",
                    "eventTimestamp": T0,
                    "payload": payload,
                }
            ]
        },
    )
    agentcore.add_response(
        "batch_create_memory_records",
        {"successfulRecords": [], "failedRecords": []},
        {
            "memoryId": MEM,
            "records": [
                {
                    "requestIdentifier": "s1:ev1:0",
                    "namespaces": [NS["turns"]],
                    "content": {"text": "Ana: ship it"},
                    "timestamp": T0,
                    "metadata": {"speaker": {"stringValue": "Ana"}, "session": {"stringValue": "s1"}},
                }
            ],
        },
    )
    out = await run(Member())
    assert out["turns"] == 1
    assert memory_sync._get(OWNER, "MEMSYNC")["events_after"] == T0.isoformat()
