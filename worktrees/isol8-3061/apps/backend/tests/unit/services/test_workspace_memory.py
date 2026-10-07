"""The workspace-memory wipe: the owner's records now (loudly), then a background tail that deletes
the owner's events and re-sweeps records extraction produced late -- and nothing of anyone else's."""

import asyncio
from unittest.mock import MagicMock

import pytest

from core.services import workspace_memory


def _fake_client(record_pages: list[tuple[list[str], bool]], sessions: dict[str, list[list[str]]]) -> MagicMock:
    """``record_pages``: (ids, has_next) per ListMemoryRecords call, in order. ``sessions``: event
    pages per session."""
    fake = MagicMock()
    fake.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [{"memoryRecordId": r} for r in page], **({"nextToken": "n"} if nxt else {})}
        for page, nxt in record_pages
    ]
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    fake.list_sessions.return_value = {"sessionSummaries": [{"sessionId": s} for s in sessions]}
    pages = {sid: list(ps) for sid, ps in sessions.items()}

    def _list_events(**kw):
        page = pages[kw["sessionId"]].pop(0)
        more = bool(pages[kw["sessionId"]])
        return {"events": [{"eventId": e} for e in page], **({"nextToken": "t"} if more else {})}

    fake.list_events.side_effect = _list_events
    return fake


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)


@pytest.mark.asyncio
async def test_wipe_deletes_records_now_and_events_plus_late_records_in_the_tail(monkeypatch, configured):
    records = [f"r{i}" for i in range(250)]
    fake = _fake_client(
        # first pass: two pages; the late re-sweep finds one record extraction produced afterwards
        [(records[:125], True), (records[125:], False), (["late"], False)],
        {"s1": [["e1", "e2"], ["e3"]], "s2": [["e4"]]},
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    # Records are gone before the call returns; events are the tail's job.
    assert fake.batch_delete_memory_records.call_count == 3
    await asyncio.gather(*workspace_memory._tails)

    for call in fake.list_memory_records.call_args_list:
        # EXACT namespace, never namespacePath (a prefix match would also reach /owners/org_xy/).
        assert call.kwargs["namespace"] == "/owners/org_x/"
        assert "namespacePath" not in call.kwargs
    chunks = [c.kwargs["records"] for c in fake.batch_delete_memory_records.call_args_list]
    assert [len(c) for c in chunks] == [100, 100, 50, 1]
    assert [r["memoryRecordId"] for c in chunks for r in c] == [*records, "late"]
    fake.list_sessions.assert_called_once_with(memoryId="mem-1", actorId="org_x")
    deleted = {(c.kwargs["sessionId"], c.kwargs["eventId"]) for c in fake.delete_event.call_args_list}
    # Multi-page sessions are fully listed BEFORE deletion starts, so nothing is skipped.
    assert deleted == {("s1", "e1"), ("s1", "e2"), ("s1", "e3"), ("s2", "e4")}
    assert all(c.kwargs["actorId"] == "org_x" for c in fake.delete_event.call_args_list)


@pytest.mark.asyncio
async def test_a_record_that_fails_to_delete_fails_the_wipe(monkeypatch, configured):
    """Records never expire: a partial delete must say 'retry me', not report success."""
    fake = _fake_client([(["r1", "r2"], False)], {})
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": [{"id": "r2"}]}
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)
    with pytest.raises(RuntimeError, match="1 of 2 records"):
        await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)


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
