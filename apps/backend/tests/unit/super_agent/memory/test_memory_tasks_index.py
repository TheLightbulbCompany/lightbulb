"""Task cards in memory (wave2-M Task 5): one strategy-less record per task, a MEMTASK# pointer row."""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from core.repositories import messaging_repo
from core.services import memory_sync, memory_tasks_index as mti

OWNER = "org_1"
TASK_ID = "0b6c0d2e-4a8f-4c1e-9d55-7f3a2b1c0e99"


class FakeAgentCore:
    def __init__(self, fail_create: bool = False) -> None:
        self.records: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_create = fail_create
        self._n = 0
        self.tokens: dict[str, str] = {}  # clientToken is idempotent: a replay returns the same record

    def batch_create_memory_records(self, *, memoryId, records, clientToken):
        self.calls.append("create")
        if self.fail_create:
            raise RuntimeError("throttled")
        ok = []
        for r in records:
            if clientToken in self.tokens:
                rid = self.tokens[clientToken]
                ok.append({"requestIdentifier": r["requestIdentifier"], "memoryRecordId": rid, "status": "SUCCEEDED"})
                continue
            self._n += 1
            rid = self.tokens[clientToken] = f"rec-{self._n}"
            self.records[rid] = {"ns": r["namespaces"][0], "text": r["content"]["text"], "metadata": r["metadata"]}
            ok.append({"requestIdentifier": r["requestIdentifier"], "memoryRecordId": rid, "status": "SUCCEEDED"})
        return {"successfulRecords": ok, "failedRecords": []}

    def batch_delete_memory_records(self, *, memoryId, records):
        self.calls.append("delete")
        failed = []
        for r in records:
            if self.records.pop(r["memoryRecordId"], None) is None:
                failed.append({"memoryRecordId": r["memoryRecordId"], "errorCode": 404, "errorMessage": "not found"})
        return {"successfulRecords": [], "failedRecords": failed}


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
def core(monkeypatch):
    fake = FakeAgentCore()
    monkeypatch.setattr(mti.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    monkeypatch.setattr(mti.workspace_memory, "_client", lambda: fake)
    monkeypatch.setattr(mti, "flags_for", AsyncMock(return_value=True))
    return fake


def _task(**over) -> dict:
    base = {
        "taskId": TASK_ID,
        "identifier": "T-42",
        "title": "Renewal pricing for Acme",
        "description": "Acme asked for next year's price. " * 20,
        "status": "working",
        "specialist": "agent-fin",
        "lane": "shared",
        "ownerUserId": "user_sam",
        "hidden": False,
        "version": 3,
        "updatedAt": "2026-11-04T10:00:00+00:00",
    }
    return {**base, **over}


def _pointer() -> dict:
    return memory_sync._get(OWNER, f"MEMTASK#{TASK_ID}")


async def test_index_writes_card_in_lane_namespace_with_metadata(table, core):
    await mti.index_task(OWNER, _task())
    [(rid, rec)] = core.records.items()
    assert rec["ns"] == "/tasks/org_1/"
    assert rec["text"].startswith("T-42 Renewal pricing for Acme — working · agent-fin\nAcme asked")
    assert len(rec["text"].split("\n", 1)[1]) == 200
    assert rec["metadata"] == {
        "taskId": {"stringValue": TASK_ID},
        "status": {"stringValue": "working"},
        "specialist": {"stringValue": "agent-fin"},
        "lane": {"stringValue": "shared"},
    }
    assert _pointer()["recordId"] == rid and int(_pointer()["version"]) == 3

    await mti.index_task(OWNER, _task(lane="private", version=4))
    [rec] = core.records.values()
    assert rec["ns"] == "/tasks/org_1:user_sam/"


async def test_reindex_replaces_previous_record_after_success(table, core):
    await mti.index_task(OWNER, _task(version=3))
    await mti.index_task(OWNER, _task(version=4, status="resolved"))
    assert core.calls == ["create", "create", "delete"]  # old deleted only after the new create
    [rec] = core.records.values()
    assert "— resolved ·" in rec["text"] and int(_pointer()["version"]) == 4


async def test_older_version_never_overwrites_newer(table, core):
    await mti.index_task(OWNER, _task(version=3))
    newest = _pointer()["recordId"]
    await mti.index_task(OWNER, _task(version=2, status="queued"))
    assert _pointer()["recordId"] == newest and int(_pointer()["version"]) == 3
    assert list(core.records) == [newest]  # the v2 record, if written, is gone


async def test_stale_index_racing_a_newer_one_deletes_its_own_record(table, core, monkeypatch):
    """Both read the pointer before either writes: the conditional put decides, the loser cleans up."""
    await mti.index_task(OWNER, _task(version=3))
    newest = _pointer()["recordId"]
    monkeypatch.setattr(memory_sync, "_get", lambda *a: {})  # the stale indexer read before v3 landed
    await mti.index_task(OWNER, _task(version=2))
    assert list(core.records) == [newest]
    assert core.calls == ["create", "create", "delete"]


async def test_unindex_on_hide(table, core):
    await mti.index_task(OWNER, _task(version=3))
    await mti.index_task(OWNER, _task(version=4, hidden=True))
    assert core.records == {}
    assert int(_pointer()["version"]) == 4 and not _pointer().get("recordId")
    # a late, older index cannot bring the hidden card back
    await mti.index_task(OWNER, _task(version=3))
    assert core.records == {}


async def test_lane_change_moves_record(table, core):
    await mti.index_task(OWNER, _task(version=3))
    await mti.index_task(OWNER, _task(version=4, lane="private"))
    assert [r["ns"] for r in core.records.values()] == ["/tasks/org_1:user_sam/"]


async def test_index_failure_never_raises(table, core, caplog):
    core.fail_create = True
    await mti.index_task(OWNER, _task())
    await mti.unindex_task(OWNER, _task())
    assert core.records == {} and not _pointer().get("recordId")
    assert "RuntimeError" in caplog.text


async def test_switch_off_or_no_memory_writes_nothing(table, core, monkeypatch):
    monkeypatch.setattr(mti, "flags_for", AsyncMock(return_value=False))
    await mti.index_task(OWNER, _task())
    monkeypatch.setattr(mti, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(mti.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    await mti.index_task(OWNER, _task())
    assert core.calls == []


async def test_private_task_without_owner_is_not_indexed_shared(table, core):
    await mti.index_task(OWNER, _task(lane="private", ownerUserId=None))
    assert core.records == {}


def test_script_refuses_prod(monkeypatch):
    from scripts import memory_index_tasks

    monkeypatch.setattr(memory_index_tasks.settings, "ENVIRONMENT", "prod", raising=False)
    with pytest.raises(SystemExit):
        memory_index_tasks.main(["--owner", OWNER])


async def test_card_text_redacts_secrets_in_title_and_description(table, core):
    key = "sk-" + "a" * 30
    await mti.index_task(OWNER, _task(title=f"Rotate {key}", description=f"token: {key} then go"))
    [rec] = core.records.values()
    assert key not in rec["text"]
    assert rec["text"].startswith("T-42 Rotate [REDACTED] — working")
    assert rec["text"].endswith("[REDACTED] then go")


async def test_duplicate_event_keeps_live_card(table, core, monkeypatch):
    """The same event twice, both reading the pointer before either claims: the replay's create
    returns the live record id (same clientToken), its claim fails, and it must not delete that card."""
    await mti.index_task(OWNER, _task(version=3))
    live = _pointer()["recordId"]
    real_get, reads = memory_sync._get, []

    def stale_first_read(*a):
        reads.append(a)
        return {} if len(reads) == 1 else real_get(*a)

    monkeypatch.setattr(memory_sync, "_get", stale_first_read)
    await mti.index_task(OWNER, _task(version=3))
    assert list(core.records) == [live] and _pointer()["recordId"] == live
    assert "delete" not in core.calls


async def test_flag_lookup_failure_is_off(table, core, caplog):
    mti.flags_for.side_effect = RuntimeError("flags down")
    await mti.index_task(OWNER, _task())
    assert core.calls == [] and "RuntimeError" in caplog.text


def test_script_dry_run_by_default(monkeypatch):
    from scripts import memory_index_tasks

    monkeypatch.setattr(memory_index_tasks.settings, "ENVIRONMENT", "dev", raising=False)
    monkeypatch.setattr(memory_index_tasks, "_all_tasks", AsyncMock(return_value=[_task()]))
    index = AsyncMock()
    monkeypatch.setattr(memory_index_tasks.memory_tasks_index, "index_task", index)
    memory_index_tasks.main(["--owner", OWNER])
    index.assert_not_called()
    memory_index_tasks.main(["--owner", OWNER, "--apply"])
    index.assert_awaited_once()
