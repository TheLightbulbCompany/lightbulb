"""The workspace-memory wipe: every record in the owner's namespace, then every event of every
session under the owner's actor id -- and nothing of anyone else's."""

import asyncio
from unittest.mock import MagicMock

import pytest

from core.services import workspace_memory


def _fake_client(record_ids: list[str], sessions: dict[str, list[str]]) -> MagicMock:
    fake = MagicMock()
    # Two pages of records, so pagination is exercised.
    half = len(record_ids) // 2
    fake.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [{"memoryRecordId": r} for r in record_ids[:half]], "nextToken": "p2"},
        {"memoryRecordSummaries": [{"memoryRecordId": r} for r in record_ids[half:]]},
        # The late re-sweep finds one record extraction produced after the first pass.
        {"memoryRecordSummaries": [{"memoryRecordId": "late"}]},
    ]
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    fake.list_sessions.return_value = {"sessionSummaries": [{"sessionId": s} for s in sessions]}
    fake.list_events.side_effect = lambda **kw: {"events": [{"eventId": e} for e in sessions[kw["sessionId"]]]}
    return fake


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)


@pytest.mark.asyncio
async def test_wipe_deletes_the_owners_records_in_chunks_and_every_event(monkeypatch, configured):
    records = [f"r{i}" for i in range(250)]
    fake = _fake_client(records, {"s1": ["e1", "e2"], "s2": ["e3"]})
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    await asyncio.gather(*workspace_memory._resweeps)

    # Events go first: they are what extraction reads.
    names = [c[0] for c in fake.method_calls]
    assert names.index("delete_event") < names.index("batch_delete_memory_records")
    for call in fake.list_memory_records.call_args_list:
        # EXACT namespace, never namespacePath (a prefix match would also reach /owners/org_xy/).
        assert call.kwargs["namespace"] == "/owners/org_x/"
        assert "namespacePath" not in call.kwargs
    chunks = [c.kwargs["records"] for c in fake.batch_delete_memory_records.call_args_list]
    assert [len(c) for c in chunks] == [100, 100, 50, 1]
    assert [r["memoryRecordId"] for c in chunks for r in c] == [*records, "late"]
    fake.list_sessions.assert_called_once_with(memoryId="mem-1", actorId="org_x")
    deleted = {(c.kwargs["sessionId"], c.kwargs["eventId"]) for c in fake.delete_event.call_args_list}
    assert deleted == {("s1", "e1"), ("s1", "e2"), ("s2", "e3")}
    assert all(c.kwargs["actorId"] == "org_x" for c in fake.delete_event.call_args_list)


@pytest.mark.asyncio
async def test_wipe_is_a_no_op_when_memory_is_not_configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("no AWS call without a memory"))
    await workspace_memory.delete_owner_memory("org_x")


@pytest.mark.asyncio
async def test_wipe_refuses_an_owner_id_that_would_widen_the_namespace(monkeypatch, configured):
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("must refuse before any call"))
    for bad in ("", "org_x/", "../org_y"):
        with pytest.raises(ValueError):
            await workspace_memory.delete_owner_memory(bad)
