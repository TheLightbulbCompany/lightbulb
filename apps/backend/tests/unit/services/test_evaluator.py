"""The evaluator: sessions into the episodic strategy, reflections out as drafts."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import Stubber

from core.config import settings
from core.services import evaluator, session_store, workspace_memory
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

MEM = "mem-test-000001"
BUCKET = "test-sessions"


@pytest.fixture(autouse=True)
def env(table):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    with (
        patch.object(settings, "AGENTCORE_MEMORY_ID", MEM),
        patch.object(settings, "OWNER_STATE_BUCKET", BUCKET),
        patch.object(settings, "AWS_REGION", "us-east-1"),
    ):
        yield


@pytest.fixture
def agentcore():
    client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub, patch.object(evaluator, "_client", return_value=client):
        with patch.object(workspace_memory, "_client", return_value=client):
            yield stub
        stub.assert_no_pending_responses()


def sess(sid="s1"):
    return {"id": sid, "harness": "claude-code", "repo": "lb", "branch": "main", "started": "2026-10-27T10:00:00Z"}


def calls(n, sid="s1"):
    return [
        {"session_id": sid, "seq": i, "at": "2026-10-27T10:01:00Z", "tool": "Bash", "summary": f"ran {i}"}
        for i in range(n)
    ]


def event_ok():
    ev = {
        "memoryId": MEM,
        "actorId": "u",
        "sessionId": "s1",
        "eventId": "e",
        "eventTimestamp": datetime.now(UTC),
        "payload": [],
    }
    return {"event": ev}


@pytest.fixture
def sent(agentcore):
    """The payload list of every CreateEvent the stubbed client is asked to send."""
    out = []
    evaluator._client().meta.events.register(  # noqa: SLF001
        "provide-client-params.bedrock-agentcore.CreateEvent", lambda params, **_: out.append(params)
    )
    return out


async def test_ingest_session_writes_events_with_tool_role(agentcore, sent):
    agentcore.add_response("create_event", event_ok())
    assert await evaluator.ingest_session("u", sess(), calls(2), workspace_id=None) is True
    (params,) = sent
    assert (params["actorId"], params["sessionId"]) == ("u", "s1")
    roles = [p["conversational"]["role"] for p in params["payload"]]
    assert roles == ["USER", "TOOL", "TOOL"]
    assert params["payload"][1]["conversational"]["content"]["text"] == "Bash: ran 0"
    # the same session uploaded again is not ingested twice
    assert await evaluator.ingest_session("u", sess(), calls(2), workspace_id=None) is False


async def test_ingest_batches_at_100_payloads(agentcore, sent):
    agentcore.add_response("create_event", event_ok())
    agentcore.add_response("create_event", event_ok())
    await evaluator.ingest_session("u", sess(), calls(120), workspace_id="org_x")  # 121 payloads
    assert [len(p["payload"]) for p in sent] == [100, 21]


async def test_free_account_cap_skips_after_50(agentcore):
    month = datetime.now(UTC).strftime("%Y-%m")
    from core.repositories import messaging_repo as store

    store._table().put_item(Item={"pk": f"MEMCAP#u#{month}", "sk": "COUNT", "n": 50})
    assert await evaluator.ingest_session("u", sess("a"), calls(1, "a"), workspace_id=None) is False
    # a workspace member is not capped, and does not count
    agentcore.add_response("create_event", event_ok())
    assert await evaluator.ingest_session("u", sess("b"), calls(1, "b"), workspace_id="org_x") is True
    assert int(store._table().get_item(Key={"pk": f"MEMCAP#u#{month}", "sk": "COUNT"})["Item"]["n"]) == 50


async def test_free_ingest_counts_once(agentcore):
    from core.repositories import messaging_repo as store

    agentcore.add_response("create_event", event_ok())
    assert await evaluator.ingest_session("u", sess(), calls(1), workspace_id=None)
    assert not await evaluator.ingest_session("u", sess(), calls(1), workspace_id=None)
    month = datetime.now(UTC).strftime("%Y-%m")
    assert int(store._table().get_item(Key={"pk": f"MEMCAP#u#{month}", "sk": "COUNT"})["Item"]["n"]) == 1


def rid(name):
    return name.ljust(40, "-")


def rec(rid_, text, at, namespaces=("/users/u/",)):
    return {"memoryRecordId": rid(rid_), "content": {"text": text}, "createdAt": at, "namespaces": list(namespaces),
            "memoryStrategyId": "session_episodes", "score": 1.0}  # fmt: skip


def listing(*records):
    return {"memoryRecordSummaries": list(records)}


async def test_collect_drafts_creates_rows_from_new_reflections(agentcore):
    await session_store.put_sessions("u", [sess("s1")], False, "org_x")
    t1, t2 = datetime(2026, 10, 27, 12, tzinfo=UTC), datetime(2026, 10, 27, 13, tzinfo=UTC)
    agentcore.add_response("list_memory_records", listing(
        rec("r1", "pytest kept failing in s1; pinning the fixture fixed it", t1),
        rec("r2", "an API key appeared in a Bash call", t2)), {"memoryId": MEM, "namespace": "/users/u/"})  # fmt: skip
    assert await evaluator.collect_drafts("u", workspace_id="org_x") == 2
    drafts = {d["id"]: d for d in await evaluator.list_drafts(user_id="u")}
    assert drafts[rid("r1")]["kind"] == "lesson" and drafts[rid("r1")]["evidence"] == ["s1"]
    assert drafts[rid("r2")]["kind"] == "finding" and drafts[rid("r2")]["evidence"] == []
    assert drafts[rid("r1")]["status"] == "proposed" and drafts[rid("r1")]["workspace_id"] == "org_x"
    assert len(await evaluator.list_drafts(workspace_id="org_x")) == 2
    # only newer than the watermark next time
    agentcore.add_response(
        "list_memory_records",
        listing(rec("r2", "an API key appeared", t2)),
        {"memoryId": MEM, "namespace": "/users/u/"},
    )
    assert await evaluator.collect_drafts("u", workspace_id="org_x") == 0


async def test_collect_drafts_ignores_episode_subnamespace(agentcore):
    t = datetime(2026, 10, 27, 12, tzinfo=UTC)
    agentcore.add_response("list_memory_records", listing(
        rec("ep", "an episode", t, ("/users/u/episodes/",)),
        rec("rf", "a reflection", t)), {"memoryId": MEM, "namespace": "/users/u/"})  # fmt: skip
    assert await evaluator.collect_drafts("u", workspace_id=None) == 1
    assert [d["id"] for d in await evaluator.list_drafts(user_id="u")] == [rid("rf")]


async def test_member_wipe_removes_sessions_drafts_episodes(agentcore):
    t = datetime(2026, 10, 27, 12, tzinfo=UTC)
    await session_store.put_sessions("user_a", [sess("s1")], True, "org_x")
    agentcore.add_response("list_memory_records", listing(rec("rf", "a lesson", t, ("/users/user_a/",))),
                           {"memoryId": MEM, "namespace": "/users/user_a/"})  # fmt: skip
    await evaluator.collect_drafts("user_a", workspace_id="org_x")
    # delete_episodes: both namespaces, then one batch delete
    agentcore.add_response(
        "list_memory_records", listing(rec("rf", "x", t)), {"memoryId": MEM, "namespace": "/users/user_a/"}
    )
    agentcore.add_response(
        "list_memory_records", listing(rec("ep", "x", t)), {"memoryId": MEM, "namespace": "/users/user_a/episodes/"}
    )
    agentcore.add_response("batch_delete_memory_records", {"successfulRecords": [], "failedRecords": []},
                           {"memoryId": MEM, "records": [{"memoryRecordId": rid("rf")}, {"memoryRecordId": rid("ep")}]})  # fmt: skip
    agentcore.add_response("list_sessions", {"sessionSummaries": []}, {"memoryId": MEM, "actorId": "user_a"})
    from unittest.mock import AsyncMock

    from core.services import network_login

    keys, forget = AsyncMock(return_value=[]), AsyncMock()  # the computer-key registry is not in this fixture
    with (
        patch.object(workspace_memory, "_wipe", return_value=None),
        patch.object(workspace_memory, "_tail"),
        patch.object(network_login, "computer_keys", keys),
        patch.object(network_login, "forget_keys", forget),
    ):
        await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    keys.assert_awaited_once_with("user_a")
    forget.assert_awaited_once_with("user_a")
    assert await session_store.list_sessions(user_id="user_a") == []
    assert await evaluator.list_drafts(user_id="user_a") == []
    assert await evaluator.list_drafts(workspace_id="org_x") == []


async def test_delete_episodes_removes_events_and_records(agentcore):
    from unittest.mock import AsyncMock

    t = datetime(2026, 10, 27, 12, tzinfo=UTC)
    agentcore.add_response(
        "list_memory_records", listing(rec("rf", "x", t)), {"memoryId": MEM, "namespace": "/users/u/"}
    )
    agentcore.add_response(
        "list_memory_records", listing(rec("ep", "x", t)), {"memoryId": MEM, "namespace": "/users/u/episodes/"}
    )
    agentcore.add_response("batch_delete_memory_records", {"successfulRecords": [], "failedRecords": []})
    agentcore.add_response("list_sessions", {"sessionSummaries": [
        {"sessionId": "s1", "actorId": "u", "createdAt": t}]}, {"memoryId": MEM, "actorId": "u"})  # fmt: skip
    agentcore.add_response("list_events", {"events": [
        {"eventId": "e1", "memoryId": MEM, "actorId": "u", "sessionId": "s1", "eventTimestamp": t, "payload": []}]},
        {"memoryId": MEM, "sessionId": "s1", "actorId": "u", "includePayloads": False})  # fmt: skip
    agentcore.add_response("delete_event", {"eventId": "e1"},
                           {"memoryId": MEM, "sessionId": "s1", "eventId": "e1", "actorId": "u"})  # fmt: skip
    with patch.object(workspace_memory, "_tail", new=AsyncMock()) as tail:
        assert await evaluator.delete_episodes("u", resweep_after_s=0) == 2
        await asyncio.sleep(0)
    assert tail.await_args.args[2] == ["/users/u/", "/users/u/episodes/"]


async def test_cap_reservation_is_atomic(table):
    from core.repositories import messaging_repo as store

    t = store._table()
    key = {"pk": "MEMCAP#u#2026-10", "sk": "COUNT"}
    t.put_item(Item={**key, "n": evaluator.FREE_MONTHLY_CAP - 1})
    assert await evaluator._reserve_cap(t, key) is True  # noqa: SLF001
    assert await evaluator._reserve_cap(t, key) is False  # noqa: SLF001
    assert int(t.get_item(Key=key)["Item"]["n"]) == evaluator.FREE_MONTHLY_CAP


async def test_concurrent_same_session_ingests_once(agentcore):
    agentcore.add_response("create_event", event_ok())
    results = await asyncio.gather(
        evaluator.ingest_session("u", sess(), calls(1), workspace_id=None),
        evaluator.ingest_session("u", sess(), calls(1), workspace_id=None),
    )
    assert sorted(results) == [False, True]
