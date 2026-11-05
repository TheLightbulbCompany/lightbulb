"""memory_sync: AgentCore facts <-> network log entries, and room turns -> searchable records."""

from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import ANY, Stubber
from moto import mock_aws

from core.repositories import messaging_repo
from core.services import memory_sync
from tests.unit.services.agentcore_filters import check_filters

OWNER = "org_1"
NS = {"facts": "/owners/org_1/", "turns": "/turns/org_1/", "entries": "/entries/org_1/"}


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
    # The stubber checks shapes only; refuse the filters AgentCore refuses, as the service does.
    client.meta.events.register(
        "before-parameter-build.bedrock-agentcore.*",
        lambda params, model, **_: check_filters(params.get("metadataFilters"), model.name),
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


DAY0 = int(T0.timestamp())


def fact(record_id, text, valid_from=DAY0, **meta):
    """A facts record as ListMemoryRecords returns it: an AgentCore kind, validFrom a NUMBER."""
    meta = {"entity": "api", "attribute": "port", "kind": "claim", **meta}
    metadata = {k: {"stringValue": v} for k, v in meta.items()}
    if valid_from is not None:
        metadata["validFrom"] = {"numberValue": float(valid_from)}
    return {
        "memoryRecordId": record_id,
        "content": {"text": text},
        "memoryStrategyId": "s",
        "namespaces": [NS["facts"]],
        "createdAt": T0,
        "metadata": metadata,
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
    """A ``memory.list`` row as the CLI emits it: the stored entry (``at`` epoch seconds) plus ``seq``."""
    return {"v": 1, "id": eid, "author": author, "seq": seq, "op": op, "at": DAY0 + 86400, "sig": "s"}


def remember(subject, fact_, kind="fact", scope="team", on_behalf_of=None, replaces=()):
    source = {"harness": "workspace", "repo": None, "session": None, "on_behalf_of": on_behalf_of}
    return {
        "t": "remember", "subject": subject, "fact": fact_, "kind": kind, "scope": scope,
        "when": None, "source": source, "replaces": list(replaces),
    }  # fmt: skip


def tombstoned(seq, eid, author="workspace"):
    """A row whose body a Forget removed (src/memory.rs page_since): no op."""
    return {"id": eid, "author": author, "tombstone": True, "seq": seq}


def created(*pairs):
    return {
        "successfulRecords": [{"memoryRecordId": r, "status": "SUCCEEDED", "requestIdentifier": q} for q, r in pairs],
        "failedRecords": [],
    }


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
        "when": DAY0,
        "source": {"harness": "workspace", "repo": None, "session": None, "on_behalf_of": "Ana"},
        "replaces": [],
        "key": ANY,
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


async def test_second_sync_filters_on_the_updated_at_system_field(table, agentcore):
    agentcore.add_response(
        "list_memory_records", {"memoryRecordSummaries": []}, {"memoryId": MEM, "namespace": NS["facts"]}
    )
    no_sessions(agentcore)
    member = Member()
    await run(member)
    after = memory_sync._get(OWNER, "MEMSYNC")["records_after"]
    agentcore.add_response(
        "list_memory_records",
        {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]},
        {
            "memoryId": MEM,
            "namespace": NS["facts"],
            "metadataFilters": [
                {
                    "left": {"metadataKey": "x-amz-agentcore-memory-updatedAt"},
                    "operator": "AFTER",
                    "right": {"metadataValue": {"dateTimeValue": after.replace("+00:00", "Z")}},
                }
            ],
        },
    )
    no_sessions(agentcore)
    assert (await run(member))["imported"] == 1


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
                    "timestamp": datetime.fromtimestamp(DAY0 + 86400, timezone.utc),
                    "metadata": {
                        "subject": {"stringValue": "db host"},
                        "status": {"stringValue": "current"},
                        "kind": {"stringValue": "entry"},
                        "logKind": {"stringValue": "fact"},
                        "author": {"stringValue": "ana"},
                        "entryId": {"stringValue": "x1"},
                        "validFrom": {"numberValue": DAY0 + 86400},
                    },
                }
            ],
            "clientToken": memory_sync._token(["x1"]),
        },
    )
    no_sessions(agentcore)
    op = remember("db host", "pg1")
    out = await run(Member([entry(1, "x1", op, author="ana")]))
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
    out = await run(Member([entry(1, "x1", op), entry(2, "t1", {"t": "forget", "target": "x1"})]))
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
    await run(Member([entry(2, "x2", remember("db host", "pg2", replaces=["x1"]))]))


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
                    "requestIdentifier": memory_sync._turn_id("s1", "ev1", 0),
                    "namespaces": [NS["turns"]],
                    "content": {"text": "Ana: ship it"},
                    "timestamp": T0,
                    "metadata": {"speaker": {"stringValue": "Ana"}, "session": {"stringValue": "s1"}},
                }
            ],
            "clientToken": ANY,
        },
    )
    out = await run(Member())
    assert out["turns"] == 1
    assert memory_sync._get(OWNER, "MEMSYNC")["events_after"] == T0.isoformat()


async def test_import_carries_idempotency_key(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]})
    no_sessions(agentcore)
    member = Member()
    await run(member)
    key = member.ops("memory.import")[0]["entries"][0]["key"]
    assert key.startswith(rid("r1") + ":") and len(key) > len(rid("r1")) + 10


async def test_partial_create_failure_keeps_mappings_of_the_rest(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_create_memory_records",
        {
            "successfulRecords": [{"memoryRecordId": rid("ar1"), "status": "SUCCEEDED", "requestIdentifier": "x1"}],
            "failedRecords": [
                {"memoryRecordId": rid("ar2"), "requestIdentifier": "x2", "status": "FAILED", "errorCode": 500}
            ],
        },
    )
    op = remember("s", "f")
    with pytest.raises(RuntimeError):
        await run(Member([entry(1, "x1", op), entry(2, "x2", op)]))
    assert memory_sync._get(OWNER, "MEMENTRY#x1")["arec"] == rid("ar1")
    assert "entries_seq" not in memory_sync._get(OWNER, "MEMSYNC")


async def test_tombstone_retry_tolerates_already_deleted_records(table, agentcore):
    memory_sync._put(OWNER, "MEMENTRY#x1", arec=rid("ar1"))
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_delete_memory_records",
        {
            "successfulRecords": [],
            "failedRecords": [
                {"memoryRecordId": rid("ar1"), "status": "FAILED", "errorCode": 404, "errorMessage": "ResourceNotFound"}
            ],
        },
    )
    no_sessions(agentcore)
    out = await run(Member([entry(3, "t1", {"t": "forget", "target": "x1"})]))
    assert out["mirrored"] == 1


async def test_mirror_turns_copies_tags_and_skips_room_source(table, agentcore):
    """Worker events carry tags onto their turn records; room-ingested events already have a record."""
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "list_sessions", {"sessionSummaries": [{"sessionId": "s1", "actorId": OWNER, "createdAt": T0}]}
    )

    def ev(eid, text, **tags):
        return {
            "memoryId": MEM,
            "actorId": OWNER,
            "sessionId": "s1",
            "eventId": eid,
            "eventTimestamp": T0,
            "payload": [{"conversational": {"role": "USER", "content": {"text": text}}}],
            "metadata": {k: {"stringValue": v} for k, v in tags.items()},
        }

    agentcore.add_response(
        "list_events",
        {"events": [ev("ev1", "Ana: ship it", taskId="t1", source="worker"), ev("ev2", "Bo: hi", source="room")]},
    )
    agentcore.add_response(
        "batch_create_memory_records",
        {"successfulRecords": [], "failedRecords": []},
        {
            "memoryId": MEM,
            "records": [
                {
                    "requestIdentifier": memory_sync._turn_id("s1", "ev1", 0),
                    "namespaces": [NS["turns"]],
                    "content": {"text": "Ana: ship it"},
                    "timestamp": T0,
                    "metadata": {
                        "taskId": {"stringValue": "t1"},
                        "source": {"stringValue": "worker"},
                        "speaker": {"stringValue": "Ana"},
                        "session": {"stringValue": "s1"},
                    },
                }
            ],
            "clientToken": ANY,
        },
    )
    out = await run(Member())
    assert out["turns"] == 1


async def test_concurrent_sync_returns_at_once_while_lease_is_held(table, agentcore):
    assert memory_sync._take_lease(OWNER)
    member = Member()
    assert await run(member) == {"imported": 0, "mirrored": 0, "turns": 0}
    assert member.calls == []  # and no AgentCore call (nothing is queued on the stubber)
    memory_sync._release_lease(OWNER)
    assert memory_sync._take_lease(OWNER)


async def test_sync_releases_lease_even_when_it_fails(table, agentcore):
    agentcore.add_client_error("list_memory_records", service_error_code="ThrottlingException")
    with pytest.raises(Exception):
        await run(Member())
    assert memory_sync._take_lease(OWNER)


# --- network_member: call_op / queue_op / drain_ops ---------------------------------------------

from core.services import network_member  # noqa: E402


async def test_call_op_returns_none_when_parked():
    with (
        patch.object(network_member, "_session_is_warm", return_value=False),
        patch("core.services.container_rpc.call") as rpc,
    ):
        assert await network_member.call_op(OWNER, "memory.list", {}, 5.0) is None
    rpc.assert_not_called()


async def test_call_op_sends_over_the_runtime_rpc_when_warm():
    with (
        patch.object(network_member, "_session_is_warm", return_value=True),
        patch("core.services.container_rpc.call", return_value={"ids": []}) as rpc,
    ):
        assert await network_member.call_op(OWNER, "memory.import", {"entries": []}, 5.0) == {"ids": []}
    rpc.assert_awaited_once_with(OWNER, "memory.import", {"entries": []}, timeout_s=5.0)


async def test_queue_op_then_drain_sends_in_order_and_deletes(table):
    await network_member.queue_op(OWNER, "memory.forget_author", {"author": "a"})
    await network_member.queue_op(OWNER, "memory.remember", {"fact": "b"})
    sent = []

    async def fake(owner, op, params, timeout):
        sent.append((op, params))
        return {}

    with patch.object(network_member, "call_op", fake):
        assert await network_member.drain_ops(OWNER) == 2
    assert sent == [("memory.forget_author", {"author": "a"}), ("memory.remember", {"fact": "b"})]
    assert table.scan()["Items"] == []


async def test_drain_skips_unclaimable_rows(table):
    await network_member.queue_op(OWNER, "memory.remember", {"fact": "claimed"})
    await network_member.queue_op(OWNER, "memory.remember", {"fact": "free"})
    first = sorted(table.scan()["Items"], key=lambda r: r["sk"])[0]
    table.update_item(
        Key={"pk": first["pk"], "sk": first["sk"]},
        UpdateExpression="SET claimed_until = :u",
        ExpressionAttributeValues={":u": 4102444800},
    )
    sent = []

    async def fake(owner, op, params, timeout):
        sent.append(params)
        return {}

    with patch.object(network_member, "call_op", fake):
        assert await network_member.drain_ops(OWNER) == 1
    assert sent == [{"fact": "free"}]
    assert len(table.scan()["Items"]) == 1


async def test_drain_retries_then_drops_a_failing_op_and_never_raises(table):
    await network_member.queue_op(OWNER, "memory.remember", {"fact": "x"})

    async def boom(owner, op, params, timeout):
        raise RuntimeError("{}")

    with patch.object(network_member, "call_op", boom):
        for _ in range(4):
            await network_member.drain_ops(OWNER)
        assert len(table.scan()["Items"]) == 1
        await network_member.drain_ops(OWNER)
    assert table.scan()["Items"] == []


async def test_drain_swallows_storage_errors():
    with patch("core.repositories.messaging_repo._table", side_effect=RuntimeError("down")):
        assert await network_member.drain_ops(OWNER) == 0


async def test_import_skips_refused_entries_logs_them_and_advances_the_watermark(table, agentcore, caplog):
    agentcore.add_response(
        "list_memory_records",
        {
            "memoryRecordSummaries": [
                fact(rid("r1"), "Ana: api port: 8080"),
                fact(rid("r2"), "Ana: api key: sk-secret"),
            ]
        },
    )
    no_sessions(agentcore)

    async def refuse(owner_id, op, params, timeout=30.0):
        if op == "memory.import":
            return {"ids": ["e0", None], "refused": [{"index": 1, "reason": "secret:api_key"}]}
        return {}

    with caplog.at_level("WARNING", logger="core.services.memory_sync"):
        out = await run(refuse)
    assert out["imported"] == 1
    assert memory_sync._get(OWNER, "MEMLINK#" + rid("r1"))["entry"] == "e0"
    assert not memory_sync._get(OWNER, "MEMLINK#" + rid("r2"))
    assert memory_sync._get(OWNER, "MEMSYNC")["records_after"]
    assert "secret" in caplog.text and "sk-secret" not in caplog.text


async def test_import_sends_entries_in_batches(table, agentcore):
    from scripts import memory_import

    n = memory_sync._BATCH * 2 + 5
    facts = [fact(rid(f"r{i}"), f"Ana: api port: {i}") for i in range(n)]
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": facts})
    no_sessions(agentcore)
    member = Member()
    with (
        patch("core.services.network_member.call_op", member),
        patch("core.services.network_member.drain_ops", return_value=0),
        patch.object(memory_import.settings, "AGENTCORE_MEMORY_ID", MEM),
    ):
        assert await memory_import.import_workspace(OWNER) == n
    sizes = [len(p["entries"]) for p in member.ops("memory.import")]
    assert sizes == [memory_sync._BATCH, memory_sync._BATCH, 5]


async def test_proof_registers_member_key(table):
    from core.services import network_login

    k1, k2 = "a" * 52, "b" * 52
    await network_login.register_key("user_a", k1)
    first = table.get_item(Key=network_login._registry_key("user_a", k1))["Item"]["first_seen"]
    with patch("time.time", return_value=first + 500):
        await network_login.register_key("user_a", k1)
        await network_login.register_key("user_a", k2)
    rows = table.scan()["Items"]
    assert sorted(r["sk"] for r in rows) == [f"NETKEY#{k1}", f"NETKEY#{k2}"]
    assert table.get_item(Key=network_login._registry_key("user_a", k1))["Item"]["first_seen"] == first


async def test_computer_keys_union_registry_live_and_signed_out(table):
    from core.services import network_login

    reg, live, out = "r" * 52, "a" * 52, "b" * 52
    await network_login.register_key("user_a", reg)

    class Conn:
        async def routes_for_user(self, user_id):
            return [{"key": live}]

    with (
        patch("core.services.network_route._connections", return_value=Conn()),
        patch.object(network_login, "revoked_keys", return_value=[out, live]),
    ):
        assert await network_login.computer_keys("user_a") == sorted([reg, live, out])
    await network_login.forget_keys("user_a")
    assert table.scan()["Items"] == []


# --- the CLI's real shapes and idempotency (final review #2-#5, #18, #19) -----------------------


async def test_import_maps_every_agentcore_kind_and_reads_a_number_valid_from(table, agentcore):
    kinds = ["claim", "observation", "rule", "mention", "anchor"]
    agentcore.add_response(
        "list_memory_records",
        {"memoryRecordSummaries": [fact(rid(f"r{i}"), f"Ana: api port: {i}", kind=k) for i, k in enumerate(kinds)]},
    )
    no_sessions(agentcore)
    member = Member()
    await run(member)
    sent = member.ops("memory.import")[0]["entries"]
    assert [e["kind"] for e in sent] == ["fact", "fact", "decision", "fact", "fact"]
    assert all(e["when"] == DAY0 for e in sent)  # the CLI reads when as i64 epoch seconds


async def test_undated_fact_imports_with_no_when(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "x", valid_from=None)]})
    no_sessions(agentcore)
    member = Member()
    await run(member)
    assert member.ops("memory.import")[0]["entries"][0]["when"] is None


async def test_tombstoned_row_is_skipped_and_the_watermark_still_advances(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    no_sessions(agentcore)
    out = await run(Member([tombstoned(3, "x1", author="ana")]))
    assert out["mirrored"] == 1
    state = memory_sync._get(OWNER, "MEMSYNC")
    assert int(state["entries_seq"]) == 3 and state["synced_at"]


async def test_entries_route_by_scope(table, agentcore):
    rows = [
        entry(1, "t1", remember("team fact", "a", scope="team", on_behalf_of="user_b")),
        entry(2, "m1", remember("ada's note", "b", scope="me", on_behalf_of="user_b")),
        entry(3, "w1", remember("workspace note", "c", scope="me")),
        entry(4, "n1", remember("unnamed", "d", scope="me", on_behalf_of="Ana")),
    ]
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "batch_create_memory_records",
        created(("t1", rid("a1")), ("m1", rid("a2")), ("w1", rid("a3"))),
        {
            "memoryId": MEM,
            "clientToken": memory_sync._token(["t1", "m1", "w1"]),
            "records": [
                {
                    "requestIdentifier": "t1", "namespaces": ["/entries/org_1/"],
                    "content": ANY, "timestamp": ANY, "metadata": ANY,
                },
                {
                    "requestIdentifier": "m1", "namespaces": ["/owners/org_1:user_b/"],
                    "content": ANY, "timestamp": ANY, "metadata": ANY,
                },
                {
                    "requestIdentifier": "w1", "namespaces": ["/entries/org_1/"],
                    "content": ANY, "timestamp": ANY, "metadata": ANY,
                },
            ],
        },
    )  # fmt: skip
    no_sessions(agentcore)
    assert (await run(Member(rows)))["mirrored"] == 4
    assert memory_sync._get(OWNER, "MEMENTRY#n1") == {}  # a me entry no namespace can name stays unmirrored


async def test_metadata_leaves_out_values_agentcore_would_refuse():
    assert memory_sync._strings(subject="Bob's deploy", author="", entryId="x1") == {"entryId": {"stringValue": "x1"}}


async def test_already_mirrored_entry_is_not_created_again(table, agentcore):
    # A sync that wrote the record but died before saving entries_seq lists the same row again.
    memory_sync._put(OWNER, "MEMENTRY#x1", arec=rid("ar1"))
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    no_sessions(agentcore)  # and no batch_create: the stubber would fail on an unexpected call
    out = await run(Member([entry(1, "x1", remember("db host", "pg1"))]))
    assert out["mirrored"] == 1 and memory_sync._get(OWNER, "MEMENTRY#x1")["arec"] == rid("ar1")


async def test_imported_fact_is_not_mirrored_back_into_entries(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": [fact(rid("r1"), "Ana: api port: 8080")]})
    no_sessions(agentcore)
    member = Member()
    await run(member)  # imported as e0; memory.list now returns it
    member.entries = [entry(1, "e0", remember("api port", "8080", on_behalf_of="Ana"))]
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    no_sessions(agentcore)
    assert (await run(member))["mirrored"] == 1  # listed, but no batch_create
    assert "arec" not in memory_sync._get(OWNER, "MEMENTRY#e0")


async def test_entries_seq_is_saved_per_flushed_batch(table, agentcore):
    n = memory_sync._BATCH + 1
    rows = [entry(i + 1, f"x{i}", remember("s", f"f{i}")) for i in range(n)]
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response("batch_create_memory_records", created(*((f"x{i}", rid(f"a{i}")) for i in range(n - 1))))
    agentcore.add_client_error("batch_create_memory_records", service_error_code="ThrottlingException")
    with pytest.raises(Exception):
        await run(Member(rows))
    assert int(memory_sync._get(OWNER, "MEMSYNC")["entries_seq"]) == memory_sync._BATCH


async def test_repeated_turn_sync_creates_nothing_new(table, agentcore):
    def events():
        agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
        agentcore.add_response(
            "list_sessions", {"sessionSummaries": [{"sessionId": "s1", "actorId": OWNER, "createdAt": T0}]}
        )
        payload = [{"conversational": {"role": "USER", "content": {"text": "Ana: ship it"}}}]
        agentcore.add_response(
            "list_events",
            {
                "events": [
                    {"memoryId": MEM, "actorId": OWNER, "sessionId": "s1", "eventId": "ev1",
                     "eventTimestamp": T0, "payload": payload}
                ]
            },
        )  # fmt: skip

    events()
    agentcore.add_response("batch_create_memory_records", created((memory_sync._turn_id("s1", "ev1", 0), rid("t1"))))
    assert (await run(Member()))["turns"] == 1
    events()  # the same event sits on the watermark: it is listed again, and skipped
    assert (await run(Member()))["turns"] == 0


async def test_a_queued_op_expires_after_thirty_days(table):
    import time

    await network_member.queue_op(OWNER, "memory.remember", {"fact": "x"})
    (row,) = table.scan()["Items"]
    assert abs(int(row["expiresAt"]) - (time.time() + 30 * 86400)) < 60  # the messaging table's TTL attribute


async def test_mirrored_turn_rows_expire(table, agentcore):
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response(
        "list_sessions", {"sessionSummaries": [{"sessionId": "s1", "actorId": OWNER, "createdAt": T0}]}
    )
    payload = [{"conversational": {"role": "USER", "content": {"text": "Ana: hi"}}}]
    agentcore.add_response("list_events", {"events": [
        {"memoryId": MEM, "actorId": OWNER, "sessionId": "s1", "eventId": "ev1", "eventTimestamp": T0, "payload": payload}]})  # fmt: skip
    turn = memory_sync._turn_id("s1", "ev1", 0)
    agentcore.add_response("batch_create_memory_records", created((turn, rid("t1"))))
    await run(Member())
    assert int(memory_sync._get(OWNER, f"MEMTURN#{turn}")["expiresAt"]) > T0.timestamp()


async def test_a_parked_member_is_owed_the_wipe_forward_and_the_sweep_finishes_it(table):
    from unittest.mock import AsyncMock

    from core.services import memory_api

    wake = AsyncMock(return_value=True)
    with (
        patch.object(network_member, "call_op", AsyncMock(return_value=None)),
        patch.object(network_member, "wake", wake),
    ):
        await memory_api.forward_on_behalf_of(OWNER, "user_a")  # never raises
        assert await memory_api.retry_member_wipes(OWNER) == 0  # still parked: the row stays
    wake.assert_awaited_once_with(OWNER)
    (row,) = table.scan()["Items"]
    assert (row["pk"], row["sk"]) == (f"MEMWIPE#{OWNER}", "U#user_a")

    mine = {"id": "e1", "author": "ws", "seq": 1, "at": 1, "op": remember("s", "f", scope="me", on_behalf_of="user_a")}
    pages = {0: {"entries": [mine], "next": 1}, 1: {"entries": [], "next": 1}}
    sent = []

    async def warm(owner_id, op, params, timeout=30.0):
        sent.append((op, params))
        return pages[params["since_seq"]] if op == "memory.list" else {}

    with patch.object(network_member, "call_op", warm):
        assert await memory_api.retry_member_wipes(OWNER) == 1
    assert ("memory.forget", {"entry": "e1"}) in sent
    assert table.scan()["Items"] == []


# RFC 8032 section 7.1, test 1: an Ed25519 seed and its public key.
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


async def test_owner_wipe_forgets_what_the_cloud_member_signed_by_its_public_key(monkeypatch):
    import base64
    from unittest.mock import AsyncMock

    from core.services import agentcore_runtime, memory_api, network_login, workspace_memory

    secret = {"networkKey": base64.b64encode(RFC_SEED).decode()}
    monkeypatch.setattr(agentcore_runtime, "_read_secret", lambda sm, name: secret)
    forward = AsyncMock()
    monkeypatch.setattr(memory_api, "forward_forget_author", forward)
    await workspace_memory.forget_workspace_authored(OWNER)
    key = base64.b32encode(RFC_PUBLIC).decode().lower().rstrip("=")  # the engine's member key spelling
    forward.assert_awaited_once_with(OWNER, key)
    assert network_login.KEY_RE.match(key)

    secret = None  # no boot secret: never a member, nothing to forget, and never a raise
    monkeypatch.setattr(agentcore_runtime, "_read_secret", lambda sm, name: secret)
    await workspace_memory.forget_workspace_authored(OWNER)
    monkeypatch.setattr(agentcore_runtime, "_read_secret", lambda sm, name: 1 / 0)
    await workspace_memory.forget_workspace_authored(OWNER)
    assert forward.await_count == 1


async def test_owner_wipe_forget_author_goes_through_the_member_or_is_owed_with_a_wake(table):
    from unittest.mock import AsyncMock

    from core.services import memory_api

    key = "k" * 52
    sent = []

    async def warm(owner_id, op, params, timeout=30.0):
        sent.append((op, params))
        return {"forgotten": 3}

    with patch.object(network_member, "call_op", warm):
        await memory_api.forward_forget_author(OWNER, key)
    assert sent == [("memory.forget_author", {"author": key})] and table.scan()["Items"] == []

    wake = AsyncMock(return_value=True)
    with (
        patch.object(network_member, "call_op", AsyncMock(return_value=None)),
        patch.object(network_member, "wake", wake),
        patch.object(network_member, "queue_op", AsyncMock(side_effect=AssertionError("owed, not queued"))),
    ):
        await memory_api.forward_forget_author(OWNER, key)  # parked: never raises
        assert await memory_api.retry_member_wipes(OWNER) == 0
    wake.assert_awaited_once_with(OWNER)
    (row,) = table.scan()["Items"]
    assert (row["pk"], row["sk"]) == (f"MEMWIPE#{OWNER}", f"A#{key}")

    sent.clear()
    with patch.object(network_member, "call_op", warm):
        assert await memory_api.retry_member_wipes(OWNER) == 1
    assert sent == [("memory.forget_author", {"author": key})] and table.scan()["Items"] == []


async def test_the_sweep_retries_owed_wipes_for_every_workspace(monkeypatch):
    from unittest.mock import AsyncMock

    from core.services import memory_api

    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(memory_api.container_repo, "scan_all", AsyncMock(return_value=[{"owner_id": "org_1"}]))
    retry = AsyncMock(return_value=1)
    monkeypatch.setattr(memory_api, "retry_member_wipes", retry)
    monkeypatch.setattr(memory_api.evaluator, "collect_due", AsyncMock(return_value=0))
    await memory_api.sweep_once()
    retry.assert_awaited_once_with("org_1")


async def test_tab_delete_of_a_mirrored_record_forgets_its_entry_and_drops_both_rows(table, agentcore, monkeypatch):
    from unittest.mock import AsyncMock

    from core.auth import AuthContext
    from routers import memory as routes

    record = "mem-" + rid("ar1")  # the route's id shape
    agentcore.add_response("list_memory_records", {"memoryRecordSummaries": []})
    agentcore.add_response("batch_create_memory_records", created(("x1", record)))
    no_sessions(agentcore)
    await run(Member([entry(1, "x1", remember("db host", "pg1"))]))
    link = memory_sync._get(OWNER, f"MEMLINK#{record}")
    assert (link["entry"], link["mirror"]) == ("x1", True)

    monkeypatch.setattr(memory_sync.settings, "AGENTCORE_MEMORY_ID", MEM, raising=False)
    queue, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr("core.services.network_member.call_op", AsyncMock(return_value=None))  # parked
    monkeypatch.setattr("core.services.network_member.queue_op", queue)
    monkeypatch.setattr(routes.workspace_memory, "check_deletable", AsyncMock())  # in the caller's scope
    monkeypatch.setattr(routes.workspace_memory, "delete_memory", delete)
    agentcore.add_response(  # the route deleted the record already: drop_mirror tolerates it
        "batch_delete_memory_records",
        {"successfulRecords": [], "failedRecords": [{"memoryRecordId": record, "status": "FAILED", "errorCode": 404}]},
        {"memoryId": MEM, "records": [{"memoryRecordId": record}]},
    )
    await routes.delete_memory(record, AuthContext(user_id="user_me", org_id=OWNER, org_role="org:member"))
    queue.assert_awaited_once_with(OWNER, "memory.forget", {"entry": "x1"})
    delete.assert_awaited_once_with(OWNER, "user_me", record)
    assert memory_sync._get(OWNER, f"MEMLINK#{record}") == {} and memory_sync._get(OWNER, "MEMENTRY#x1") == {}
