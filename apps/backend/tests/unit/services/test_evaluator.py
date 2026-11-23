"""The evaluator's wipes: what it wrote for uploaded sessions goes with the person, the member or the workspace."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.stub import Stubber

from core.config import settings
from core.repositories import messaging_repo as store
from core.services import evaluator, workspace_memory
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

MEM = "mem-test-000001"  # the sessions memory: the workspace memory is never the evaluator's
BUCKET = "test-sessions"


@pytest.fixture(autouse=True)
def env(table):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    with (
        patch.object(settings, "AGENTCORE_MEMORY_ID", "mem-test-workspace"),
        patch.object(settings, "AGENTCORE_SESSIONS_MEMORY_ID", MEM),
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


def rec(rid_, text, at, namespaces=("/users/u/",)):
    return {"memoryRecordId": rid_.ljust(40, "-"), "content": {"text": text}, "createdAt": at,
            "namespaces": list(namespaces), "memoryStrategyId": "session_episodes", "score": 1.0}  # fmt: skip


def listing(*records):
    return {"memoryRecordSummaries": list(records)}


def put(**item):
    store._table().put_item(Item=item)


def session(user_id, sid, workspace_id=None):
    """A stored upload: the person's row, the workspace's copy, and its S3 object."""
    row = {"id": sid, "user_id": user_id, **({"workspace_id": workspace_id} if workspace_id else {})}
    put(pk=f"SESSIONS#{user_id}", sk=f"S#{sid}", **row)
    if workspace_id:
        put(pk=f"WSESSIONS#{workspace_id}", sk=f"S#{user_id}#{sid}", **row)
    key = f"sessions/{'workspace' if workspace_id else 'free'}/{user_id}/{sid}.json"
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=BUCKET, Key=key, Body=b"{}")


def draft(user_id, did, workspace_id=None, mirrored=False):
    row = {
        "id": did,
        "user_id": user_id,
        "status": "proposed",
        **({"workspace_id": workspace_id} if workspace_id else {}),
    }
    put(pk=f"DRAFTS#{user_id}", sk=f"D#{did}", **row)
    if mirrored:
        put(pk=f"WDRAFTS#{workspace_id}", sk=f"D#{user_id}#{did}", **row)


def due(user_id, workspace_id=None):
    put(pk="EVALDUE", sk=f"U#{user_id}#W#{workspace_id or 'free'}", user_id=user_id)


def rows(pk):
    return sorted(i["sk"] for i in store._table().scan()["Items"] if i["pk"] == pk)


def s3_keys():
    return sorted(
        o["Key"] for o in boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=BUCKET).get("Contents", [])
    )


async def test_person_wipe_removes_everything_of_the_persons_own():
    session("user_a", "s1", "org_x")
    session("user_a", "s3")
    draft("user_a", "dx", "org_x", mirrored=True)
    put(pk="EVAL#user_a", sk="S#s9")
    put(pk=f"MEMCAP#user_a#{datetime.now(UTC).isoformat()[:7]}", sk="COUNT", n=1)
    due("user_a")
    episodes, forget = AsyncMock(return_value=0), AsyncMock()
    with patch.object(evaluator, "delete_episodes", episodes), patch.object(workspace_memory, "_forget_keys", forget):
        await workspace_memory.delete_person_memory("user_a", resweep_after_s=0)
    episodes.assert_awaited_once_with("user_a", resweep_after_s=0)
    forget.assert_awaited_once_with("user_a")
    left = [i for i in store._table().scan()["Items"] if "user_a" in i["pk"] + i["sk"]]
    assert left == []  # SESSIONS#, WSESSIONS#, DRAFTS#, WDRAFTS#, EVAL#, MEMCAP#, EVALDUE
    assert s3_keys() == []


async def test_owner_wipe_deletes_every_members_workspace_sessions_and_drafts_directly():
    session("user_a", "s1", "org_x")
    session("user_b", "s2", "org_x")
    session("user_b", "s3")  # user_b's free tier stays
    draft("user_a", "lesson", "org_x", mirrored=True)
    draft("user_a", "finding", "org_x")  # never mirrored: found through the session rows
    due("user_a", "org_x")
    due("user_b")
    with patch.object(settings, "AGENTCORE_MEMORY_ID", ""):
        await workspace_memory.delete_owner_memory("org_x")
    assert rows("WSESSIONS#org_x") == [] and rows("SESSIONS#user_a") == [] and rows("SESSIONS#user_b") == ["S#s3"]
    assert rows("WDRAFTS#org_x") == [] and rows("DRAFTS#user_a") == []
    assert rows("EVALDUE") == ["U#user_b#W#free"]
    assert s3_keys() == ["sessions/free/user_b/s3.json"]


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


async def test_delete_episodes_for_a_person_who_never_wrote_memory_succeeds(agentcore):
    """AgentCore answers ListSessions for an unknown actor with ResourceNotFoundException
    ("Actor <id> not found"): nothing to delete, so the wipe succeeds."""
    from unittest.mock import AsyncMock

    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/"})
    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/episodes/"})
    agentcore.add_client_error(
        "list_sessions",
        "ResourceNotFoundException",
        "Actor u not found",
        expected_params={"memoryId": MEM, "actorId": "u"},
    )
    with patch.object(workspace_memory, "_tail", new=AsyncMock()):
        assert await evaluator.delete_episodes("u", resweep_after_s=0) == 0
        await asyncio.sleep(0)


async def test_delete_episodes_treats_an_already_deleted_event_and_record_as_deleted(agentcore):
    from unittest.mock import AsyncMock

    t = datetime(2026, 10, 27, 12, tzinfo=UTC)
    agentcore.add_response(
        "list_memory_records", listing(rec("rf", "x", t)), {"memoryId": MEM, "namespace": "/users/u/"}
    )
    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/episodes/"})
    agentcore.add_response(
        "batch_delete_memory_records",
        {"successfulRecords": [], "failedRecords": [
            {"memoryRecordId": "rf".ljust(40, "-"), "status": "FAILED", "errorCode": 404, "errorMessage": "not found"}]},
    )  # fmt: skip
    agentcore.add_response("list_sessions", {"sessionSummaries": [{"sessionId": "s1", "actorId": "u", "createdAt": t}]})
    agentcore.add_response("list_events", {"events": [
        {"eventId": "e1", "memoryId": MEM, "actorId": "u", "sessionId": "s1", "eventTimestamp": t, "payload": []}]})  # fmt: skip
    agentcore.add_client_error("delete_event", "ResourceNotFoundException", "Event e1 not found")
    with patch.object(workspace_memory, "_tail", new=AsyncMock()):
        assert await evaluator.delete_episodes("u", resweep_after_s=0) == 1
        await asyncio.sleep(0)


@pytest.mark.parametrize("code", ["ThrottledException", "AccessDeniedException", "ValidationException"])
async def test_delete_episodes_still_fails_on_any_other_error(agentcore, code):
    from botocore.exceptions import ClientError

    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/"})
    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/episodes/"})
    agentcore.add_client_error("list_sessions", code, "nope")
    with pytest.raises(ClientError):
        await evaluator.delete_episodes("u", resweep_after_s=0)


async def test_delete_episodes_still_fails_when_a_record_delete_fails_for_another_reason(agentcore):
    t = datetime(2026, 10, 27, 12, tzinfo=UTC)
    agentcore.add_response(
        "list_memory_records", listing(rec("rf", "x", t)), {"memoryId": MEM, "namespace": "/users/u/"}
    )
    agentcore.add_response("list_memory_records", listing(), {"memoryId": MEM, "namespace": "/users/u/episodes/"})
    agentcore.add_response(
        "batch_delete_memory_records",
        {"successfulRecords": [], "failedRecords": [
            {"memoryRecordId": "rf".ljust(40, "-"), "status": "FAILED", "errorCode": 429, "errorMessage": "throttled"}]},
    )  # fmt: skip
    with pytest.raises(RuntimeError, match="1 of 1 records failed"):
        await evaluator.delete_episodes("u", resweep_after_s=0)


async def test_delete_episodes_still_fails_when_the_memory_itself_is_missing(agentcore):
    """A missing memory store is not "nothing to delete": the person's memory may sit in a store we did not reach."""
    from botocore.exceptions import ClientError

    agentcore.add_client_error("list_memory_records", "ResourceNotFoundException", f"Memory {MEM} not found")
    with pytest.raises(ClientError):
        await evaluator.delete_episodes("u", resweep_after_s=0)
